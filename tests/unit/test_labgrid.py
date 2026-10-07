"""Unit tests for real hardware via the lab's labgrid-coordinator."""

import pytest

from kernelci_labgrid.labgrid import Lease, Place
from kernelci_labgrid.scheduler.pull_agent import LabgridAgent

PLACES = [
    Place("labgrid-aparcar-openwrt_one", {"device": "openwrt_one"}),
    Place("labgrid-aparcar-bananapi_bpi-r4", {"device": "bananapi_bpi-r4", "rack": "2"},
          acquired="laptop/paul", reservation="Q3KX7ABC"),
]


def test_place_free():
    one, r4 = PLACES
    assert one.device == "openwrt_one" and one.free
    assert not r4.free


class FakeLabgrid:
    address = "127.0.0.1:20408"

    def __init__(self, places, place="p1"):
        self._places = places
        self.place = place
        self.calls = []

    async def places(self):
        return self._places

    async def reserve(self, platform, timeout):
        self.calls.append(("reserve", platform))
        return Lease(token="TOKEN1", place=self.place)

    async def release(self, lease):
        self.calls.append(("release", lease.token))

    def env(self, lease):
        return {"LG_COORDINATOR": self.address, "LG_PLACE": lease.place}


@pytest.fixture
def agent(tmp_path):
    targets = tmp_path / "targets"
    targets.mkdir()
    (targets / "openwrt_one.yaml").write_text("targets:\n  main:\n    resources:\n      RemotePlace:\n        name: x\n")
    (targets / "bananapi_bpi-r4.yaml").write_text("resources:\n  RemotePlace: {}\n")
    (targets / "qemu_x86-64.yaml").write_text("drivers:\n  - QEMUDriver: {}\n")
    a = LabgridAgent(
        api_url="http://localhost:8001/latest", api_token="t", lab_name="aparcar",
        tests_dir=tmp_path / "tests", targets_dir=targets, artifact_dir=tmp_path / "a",
        coordinator="127.0.0.1:20408",
    )
    return a


async def test_platforms_from_places(agent):
    agent._labgrid = FakeLabgrid(PLACES)
    await agent._refresh_platforms()
    assert agent.platforms == ["bananapi_bpi-r4", "openwrt_one"]


async def test_configured_platforms_restrict_hardware_and_add_qemu(agent):
    agent._labgrid = FakeLabgrid(PLACES)
    agent.configured_platforms = ["openwrt_one", "qemu_x86-64"]
    await agent._refresh_platforms()
    assert agent.platforms == ["openwrt_one", "qemu_x86-64"]


async def test_place_without_target_file_is_ignored(agent):
    agent._labgrid = FakeLabgrid([Place("p", {"device": "unknown_board"})])
    await agent._refresh_platforms()
    assert agent.platforms == []


def _jobs(monkeypatch, agent, claim_ok=True):
    started = []

    async def fake_get(endpoint, **params):
        return {"items": [{"id": "job1", "state": "available", "data": {"platform": params.get("data.platform")}}]}

    async def fake_claim(job):
        return dict(job) if claim_ok else None

    async def fake_execute(job, lease=None):
        started.append((job["id"], lease.place if lease else None))

    monkeypatch.setattr(agent, "_api_get", fake_get)
    monkeypatch.setattr(agent, "_claim", fake_claim)
    monkeypatch.setattr(agent, "_execute_job", fake_execute)
    return started


async def _settle():
    import asyncio
    for _ in range(3):
        await asyncio.sleep(0)


async def test_hardware_job_runs_on_reserved_place(agent, monkeypatch):
    fake = FakeLabgrid([Place("p1", {"device": "openwrt_one"})])
    agent._labgrid = fake
    started = _jobs(monkeypatch, agent)
    await agent._poll_and_execute()
    await _settle()
    assert ("reserve", "openwrt_one") in fake.calls
    assert started == [("job1", "p1")]


async def test_no_free_place_leaves_job(agent, monkeypatch):
    fake = FakeLabgrid([Place("p1", {"device": "openwrt_one"}, acquired="someone/else")])
    agent._labgrid = fake
    started = _jobs(monkeypatch, agent)
    await agent._poll_and_execute()
    await _settle()
    assert started == []
    assert not any(c[0] == "reserve" for c in fake.calls)


async def test_failed_claim_releases_place(agent, monkeypatch):
    fake = FakeLabgrid([Place("p1", {"device": "openwrt_one"})])
    agent._labgrid = fake
    started = _jobs(monkeypatch, agent, claim_ok=False)
    await agent._poll_and_execute()
    assert started == []
    assert ("release", "TOKEN1") in fake.calls


def test_device_is_the_place(agent):
    agent._job_places["job1"] = "labgrid-aparcar-openwrt_one"
    data = agent._node_data({"id": "job1", "data": {"platform": "openwrt_one"}})
    assert data["device"] == "labgrid-aparcar-openwrt_one"
    assert agent._node_data({"id": "job2", "data": {"platform": "qemu_x86-64"}})["device"] == "aparcar-qemu_x86-64"


async def test_no_coordinator_needs_platforms(tmp_path):
    agent = LabgridAgent(api_url="http://x/latest", api_token="t", lab_name="lab",
                         tests_dir=tmp_path, artifact_dir=tmp_path / "a")
    with pytest.raises(RuntimeError, match="no platforms"):
        await agent.start()


async def test_no_coordinator_serves_exactly_configured(tmp_path):
    agent = LabgridAgent(api_url="http://x/latest", api_token="t", lab_name="lab",
                         tests_dir=tmp_path, artifact_dir=tmp_path / "a",
                         platforms=["qemu_x86-64"], coordinator="")
    await agent.start()
    try:
        assert agent.platforms == ["qemu_x86-64"]
    finally:
        await agent.stop()
