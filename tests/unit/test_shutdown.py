"""Ctrl-C: stop at once, kill test processes, return jobs to the queue."""

import asyncio
import os
import time

import pytest

from kernelci_labgrid.scheduler.pull_agent import LabgridAgent


@pytest.fixture
def agent(tmp_path):
    tests = tmp_path / "tests"
    targets = tmp_path / "targets"
    for d in (tests, targets, tmp_path / "strategies"):
        d.mkdir()
    (targets / "qemu_x86-64.yaml").write_text("targets: {}\n")
    return LabgridAgent(
        api_url="http://x/latest", api_token="t", lab_name="lab", tests_dir=tests,
        artifact_dir=tmp_path / "artifacts", platforms=["qemu_x86-64"],
    )


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_stop_wakes_poll_sleep(agent):
    start = time.monotonic()
    sleeper = asyncio.create_task(agent._sleep(30))
    await asyncio.sleep(0.05)
    agent.request_stop()
    await asyncio.wait_for(sleeper, timeout=1)
    assert time.monotonic() - start < 1
    assert agent._stopping and not agent._running


async def test_cancel_kills_whole_process_group(agent, tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    # Like `uv run pytest` starting QEMU: a grandchild in the same group
    agent.pytest_command = ["sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"]
    run = asyncio.create_task(agent._run_pytest(
        target_yaml=agent.targets_dir / "qemu_x86-64.yaml", firmware=tmp_path / "fw.bin",
        pytest_args="", timeout=60, outdir=tmp_path / "out"))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.02)
    grandchild = int(pidfile.read_text())
    assert alive(grandchild)

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, timeout=5)
    await asyncio.sleep(0.1)
    # Reaped by init or a zombie at most; either way not sleeping anymore
    assert not alive(grandchild) or _is_zombie(grandchild)
    assert "labgrid-agent: stopped" in (tmp_path / "out" / "pytest.log").read_text()


def _is_zombie(pid: int) -> bool:
    import subprocess
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return out.stdout.strip().startswith("Z")


@pytest.fixture
def api(agent, monkeypatch):
    state = {}

    async def fake_get(endpoint, **params):
        return dict(state["node"], data=dict(state["node"]["data"]))

    async def fake_put(endpoint, data):
        state["put"] = data
        return data

    monkeypatch.setattr(agent, "_api_get", fake_get)
    monkeypatch.setattr(agent, "_api_put", fake_put)
    return state


async def test_unclaim_returns_pool_job(agent, api):
    api["node"] = {"id": "j1", "state": "available", "created": "x",
                   "data": {"runtime": "lab", "pool": "openwrt-labs",
                            "job_id": "lab:1", "worker": "lab@host"}}
    await agent._unclaim({"id": "j1", "data": {"job_id": "lab:1"}})
    data = api["put"]["data"]
    assert data["runtime"] == "openwrt-labs"
    assert "job_id" not in data and "worker" not in data and "pool" not in data


async def test_unclaim_keeps_jobs_of_others(agent, api):
    api["node"] = {"id": "j1", "state": "available", "created": "x",
                   "data": {"runtime": "other", "job_id": "other:2"}}
    await agent._unclaim({"id": "j1", "data": {"job_id": "lab:1"}})
    assert "put" not in api


async def test_cancelled_job_is_requeued(agent, monkeypatch):
    returned = []

    async def slow_fetch(url):
        await asyncio.sleep(30)

    async def fake_unclaim(job):
        returned.append(job["id"])

    monkeypatch.setattr(agent, "_fetch_json", slow_fetch)
    monkeypatch.setattr(agent, "_unclaim", fake_unclaim)
    job = {"id": "j1", "name": "openwrt-tests", "artifacts": {"job_definition": "u"},
           "data": {"platform": "qemu_x86-64", "job_id": "lab:1"}}
    agent._current_jobs.add("j1")
    agent._busy_platforms.add("qemu_x86-64")
    task = asyncio.create_task(agent._execute_job(job))
    agent._job_tasks.add(task)
    await asyncio.sleep(0.05)

    agent.request_stop()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)
    assert returned == ["j1"]
    assert not agent._current_jobs and not agent._busy_platforms
