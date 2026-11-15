"""Health reports: healthcheck nodes tell the server which devices a lab has."""

import asyncio
from datetime import datetime, timedelta

import pytest

from kernelci_labgrid.labgrid import Lease, Place
from kernelci_labgrid.scheduler.pull_agent import (
    HEARTBEAT_HOURS,
    AgentLockedError,
    DeviceHealth,
    LabgridAgent,
    TestResult,
)

from .test_labgrid import FakeLabgrid

RELEASE_URL = "https://downloads.openwrt.org/releases/25.12.5/targets/x86/64/fw.img.gz"


@pytest.fixture
def agent(tmp_path):
    targets = tmp_path / "targets"
    targets.mkdir()
    (targets / "openwrt_one.yaml").write_text("resources:\n  RemotePlace: {}\n")
    (targets / "qemu_x86-64.yaml").write_text("drivers:\n  - QEMUDriver: {}\n")
    a = LabgridAgent(
        api_url="http://localhost:8001/latest", api_token="t", lab_name="aparcar",
        tests_dir=tmp_path / "tests", targets_dir=targets, artifact_dir=tmp_path / "a",
        platforms=["qemu_x86-64"], coordinator="",
    )
    a._session = object()  # only checked for presence; requests are faked
    return a


@pytest.fixture
def posted(agent, monkeypatch):
    nodes = []

    async def fake_post(endpoint, data):
        assert endpoint == "/node"
        nodes.append(data)
        return {"id": f"n{len(nodes)}", **data}

    monkeypatch.setattr(agent, "_api_post", fake_post)
    return nodes


async def test_device_without_health_check_reports_unknown(agent, posted):
    agent.platforms = ["qemu_x86-64"]
    await agent._check_health(wait=True)
    [node] = posted
    assert node["kind"] == "job" and node["name"] == "healthcheck" and "parent" not in node
    assert node["state"] == "done" and node["result"] == "skip"
    assert node["data"]["platform"] == "qemu_x86-64"
    assert node["data"]["runtime"] == "aparcar"
    assert node["data"]["device"] == "aparcar-qemu_x86-64"
    assert node["data"]["error_msg"] == "no health check"


async def test_heartbeat_only_on_change_or_when_old(agent, posted):
    await agent._report_health("qemu_x86-64", "skip", "no health check", heartbeat=True)
    await agent._report_health("qemu_x86-64", "skip", "no health check", heartbeat=True)
    assert len(posted) == 1
    await agent._report_health("qemu_x86-64", "fail", "no shell", heartbeat=True)
    assert len(posted) == 2 and posted[1]["result"] == "fail"
    await agent._report_health("qemu_x86-64", "fail", "maintenance: new_psu", heartbeat=True)
    assert len(posted) == 3
    agent._device_health["qemu_x86-64"].last_report -= timedelta(hours=HEARTBEAT_HOURS + 1)
    await agent._report_health("qemu_x86-64", "fail", "maintenance: new_psu", heartbeat=True)
    assert len(posted) == 4


async def test_failed_report_is_retried(agent, monkeypatch):
    async def failing_post(endpoint, data):
        raise RuntimeError("API down")

    monkeypatch.setattr(agent, "_api_post", failing_post)
    await agent._report_health("qemu_x86-64", "pass", heartbeat=True)
    assert agent._device_health["qemu_x86-64"].reported == ""


async def test_report_state_survives_restarts(tmp_path):
    health = DeviceHealth("x", state="good", reported="pass:", last_report=datetime(2026, 11, 2, 8))
    assert DeviceHealth.from_dict(health.to_dict()) == health


async def test_health_check_reports_result(agent, posted, monkeypatch):
    agent.health_checks = {}

    async def golden(config):
        return RELEASE_URL, None

    async def fetch(url, sha256, dest):
        return dest / "fw.img.gz"

    async def pytest_run(**kwargs):
        kwargs["outdir"].mkdir(parents=True)
        return TestResult(passed=2, total=2, returncode=0)

    monkeypatch.setattr(agent, "_golden_image", golden)
    monkeypatch.setattr(agent, "_fetch_firmware", fetch)
    monkeypatch.setattr(agent, "_run_pytest", pytest_run)
    await agent._check_health(wait=True)
    [node] = posted
    assert node["result"] == "pass"
    assert "error_msg" not in node["data"]
    check = node["data"]["misc"]["healthcheck"]
    assert check["release"] == "25.12.5"
    assert check["passed"] == 2
    # done: the next round only sends heartbeats, none due yet
    await agent._check_health(wait=True)
    assert len(posted) == 1


async def test_health_check_logs_only_the_last_run(agent, posted, monkeypatch):
    """labgrid appends to its console log: the reused workdir starts empty."""
    agent.health_checks = {}
    seen = []

    async def golden(config):
        return RELEASE_URL, None

    async def fetch(url, sha256, dest):
        return dest / "fw.img.gz"

    async def pytest_run(**kwargs):
        outdir = kwargs["outdir"]
        outdir.mkdir(parents=True, exist_ok=True)
        log = outdir / "console_main"
        with log.open("a") as f:
            f.write(f"run {len(seen)}\n")
        seen.append(log.read_text())
        return TestResult(passed=1, total=1, returncode=0)

    monkeypatch.setattr(agent, "_golden_image", golden)
    monkeypatch.setattr(agent, "_fetch_firmware", fetch)
    monkeypatch.setattr(agent, "_run_pytest", pytest_run)
    await agent._check_health(wait=True)
    agent._device_health["qemu_x86-64"].last_check -= timedelta(days=30)
    await agent._check_health(wait=True)
    assert seen == ["run 0\n", "run 1\n"]


async def test_no_golden_image_reports_unknown(agent, posted, monkeypatch):
    agent.health_checks = {}

    async def golden(config):
        raise RuntimeError("openwrt_one is in no release")

    monkeypatch.setattr(agent, "_golden_image", golden)
    await agent._check_health(wait=True)
    [node] = posted
    assert node["result"] == "skip"
    assert "no release" in node["data"]["error_msg"]


async def test_maintenance_tag(agent, posted):
    agent._labgrid = FakeLabgrid([])
    agent._places = [Place("labgrid-aparcar-openwrt_one",
                           {"device": "openwrt_one", "maintenance": "new_psu"})]
    agent.platforms = ["openwrt_one"]
    agent.health_checks = {}  # would check, but maintenance wins
    await agent._check_health(wait=True)
    [node] = posted
    assert node["result"] == "fail"
    assert node["data"]["error_msg"] == "maintenance: new_psu"
    assert node["data"]["device"] == "labgrid-aparcar-openwrt_one"
    assert not agent.is_device_healthy("openwrt_one")
    assert await agent._reserve("openwrt_one") is None
    assert agent._labgrid.calls == []


async def test_maintenance_place_is_not_reserved(agent):
    fake = FakeLabgrid([])
    agent._labgrid = fake
    agent._places = [Place("p1", {"device": "openwrt_one"}),
                     Place("p2", {"device": "openwrt_one", "maintenance": "flaky"})]
    assert agent.is_device_healthy("openwrt_one")  # p1 still serves it
    lease = await agent._reserve("openwrt_one")
    assert lease.place == "p1"
    assert fake.calls == [("reserve", "openwrt_one", "p1")]


async def test_maintenance_place_handed_out_is_released(agent):
    """The tag was set after the agent listed the places."""
    fake = FakeLabgrid([], place="p2")
    agent._labgrid = fake
    agent._places = [Place("p1", {"device": "openwrt_one"}), Place("p2", {"device": "openwrt_one"})]
    agent._places[1].tags["maintenance"] = "flaky"  # seen only after reserving

    async def reserve(platform, timeout, place=None):
        fake.calls.append(("reserve", platform))
        return Lease(reservation_id="RES1", place="p2")

    fake.reserve = reserve
    assert await agent._reserve("openwrt_one", "p1") is None
    assert fake.calls == [("reserve", "openwrt_one"), ("release", "RES1")]


@pytest.fixture
def three_places(agent, posted, monkeypatch):
    """One platform with three places (copies of a board), a health check
    that fails on p2."""
    agent._labgrid = FakeLabgrid([])
    agent._places = [Place(f"p{i}", {"device": "openwrt_one"}) for i in (1, 2, 3)]
    agent.platforms = ["openwrt_one"]
    agent.health_checks = {}

    async def golden(config):
        return RELEASE_URL, None

    async def fetch(url, sha256, dest):
        return dest / "fw.img.gz"

    async def pytest_run(**kwargs):
        kwargs["outdir"].mkdir(parents=True, exist_ok=True)
        if kwargs["extra_env"]["LG_PLACE"] == "p2":
            return TestResult(passed=0, failed=1, total=1, returncode=3, output="no shell")
        return TestResult(passed=1, total=1, returncode=0)

    monkeypatch.setattr(agent, "_golden_image", golden)
    monkeypatch.setattr(agent, "_fetch_firmware", fetch)
    monkeypatch.setattr(agent, "_run_pytest", pytest_run)
    return agent


async def test_each_place_is_checked_and_reported(three_places, posted):
    agent = three_places
    await agent._check_health(wait=True)
    assert sorted((n["data"]["device"], n["result"]) for n in posted) == [
        ("p1", "pass"), ("p2", "fail"), ("p3", "pass")]
    assert all(n["data"]["platform"] == "openwrt_one" for n in posted)
    assert ("reserve", "openwrt_one", "p2") in agent._labgrid.calls
    assert agent._device_health["p2"].state == "bad"
    # nothing due: no new reports
    await agent._check_health(wait=True)
    assert len(posted) == 3


@pytest.fixture
def concurrency(three_places, monkeypatch):
    """Records how many health checks run at once."""
    seen = {"now": 0, "max": 0}

    async def pytest_run(**kwargs):
        seen["now"] += 1
        seen["max"] = max(seen["max"], seen["now"])
        await asyncio.sleep(0.01)
        seen["now"] -= 1
        kwargs["outdir"].mkdir(parents=True, exist_ok=True)
        return TestResult(passed=1, total=1, returncode=0)

    monkeypatch.setattr(three_places, "_run_pytest", pytest_run)
    return seen


async def test_health_checks_run_in_parallel(three_places, concurrency, posted):
    await three_places._check_health(wait=True)
    assert concurrency["max"] == 3
    assert len(posted) == 3


@pytest.mark.parametrize("max_jobs, busy, expected", [(2, 0, 2), (2, 1, 1), (1, 1, 1)])
async def test_max_jobs_caps_health_checks(three_places, concurrency, posted,
                                           max_jobs, busy, expected):
    """Running jobs count against max_jobs, one check always runs; the
    others start at later polls."""
    three_places.max_jobs = max_jobs
    three_places._busy_platforms = {f"job{i}" for i in range(busy)}
    await three_places._check_health(wait=True)
    assert concurrency["max"] == expected
    assert len(posted) == expected
    for _ in range(3):
        await three_places._check_health(wait=True)
    assert concurrency["max"] == expected
    assert len(posted) == 3


async def test_health_checks_run_in_the_background(three_places, posted, monkeypatch):
    """The poll loop doesn't wait for a check: a later poll leaves a running
    check alone and doesn't start it twice."""
    release = asyncio.Event()
    runs = []

    async def pytest_run(**kwargs):
        runs.append(kwargs["extra_env"]["LG_PLACE"])
        await release.wait()
        kwargs["outdir"].mkdir(parents=True, exist_ok=True)
        return TestResult(passed=1, total=1, returncode=0)

    monkeypatch.setattr(three_places, "_run_pytest", pytest_run)
    await asyncio.wait_for(three_places._check_health(), timeout=1)
    await asyncio.sleep(0.01)
    assert sorted(three_places._health_tasks) == ["p1", "p2", "p3"]
    await asyncio.wait_for(three_places._check_health(), timeout=1)
    assert sorted(runs) == ["p1", "p2", "p3"]
    assert posted == []
    release.set()
    await asyncio.wait(list(three_places._health_tasks.values()), timeout=1)
    await asyncio.sleep(0)  # done callbacks
    assert not three_places._health_tasks
    assert len(posted) == 3


async def test_busy_qemu_platform_is_checked_after_its_job(agent, posted, monkeypatch):
    agent.health_checks = {}
    agent._busy_platforms = {"qemu_x86-64"}
    ran = []

    async def run_check(platform, config, place=None):
        ran.append(platform)
        return True

    monkeypatch.setattr(agent, "_run_health_check", run_check)
    await agent._check_health(wait=True)
    assert ran == [] and not agent._health_tasks
    assert posted[0]["result"] == "skip"  # heartbeat: not checked yet
    agent._busy_platforms.clear()
    await agent._check_health(wait=True)
    assert ran == ["qemu_x86-64"]


def test_one_agent_per_health_state_file(tmp_path):
    def make():
        return LabgridAgent(
            api_url="http://x/latest", api_token="t", lab_name="lab", tests_dir=tmp_path,
            health_state_file=tmp_path / "health.json", coordinator="")

    first, second = make(), make()
    first._lock_health_state()
    first._device_health["d"] = DeviceHealth("d", state="good")
    first._save_health_state()
    with pytest.raises(AgentLockedError, match="another agent"):
        second._lock_health_state()
    # the second never writes the first one's file, also not when stopping
    second._save_health_state()
    assert '"good"' in (tmp_path / "health.json").read_text()
    first._unlock_health_state()
    third = make()
    third._lock_health_state()  # free again (also when the first dies)
    third._unlock_health_state()


def test_health_state_written_whole(tmp_path):
    agent = LabgridAgent(api_url="http://x/latest", api_token="t", lab_name="lab",
                         tests_dir=tmp_path, health_state_file=tmp_path / "health.json")
    agent._device_health["d"] = DeviceHealth("d", state="bad", failure_reason="no shell")
    agent._save_health_state()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["health.json"]  # no .tmp left
    again = LabgridAgent(api_url="http://x/latest", api_token="t", lab_name="lab",
                         tests_dir=tmp_path, health_state_file=tmp_path / "health.json")
    again._load_health_state()
    assert again._device_health["d"].failure_reason == "no shell"


async def test_reset_file_forgets_health(agent, posted, tmp_path):
    agent.health_state_file = tmp_path / "health.json"
    agent.health_reset_file = tmp_path / "health.reset"
    agent._device_health["qemu_x86-64"] = DeviceHealth(
        "qemu_x86-64", state="bad", last_check=datetime.now(), reported="fail:x")
    agent._take_health_reset()
    assert "qemu_x86-64" in agent._device_health  # no file: nothing happens
    agent.health_reset_file.touch()
    agent._take_health_reset()
    assert not agent.health_reset_file.exists()
    assert agent._device_health == {}
    assert agent.health_state_file.exists()


async def test_one_failing_device_does_not_stop_the_others(three_places, posted, monkeypatch):
    real = three_places._check_device

    async def check_device(platform, place, run=True):
        if place == "p1":
            raise RuntimeError("boom")
        await real(platform, place, run=run)

    monkeypatch.setattr(three_places, "_check_device", check_device)
    await three_places._check_health(wait=True)
    assert sorted(n["data"]["device"] for n in posted) == ["p2", "p3"]


async def test_parallel_fetches_download_once(agent, monkeypatch):
    """Places of one board share the golden image: one download."""
    downloads = []

    class Response:
        status = 200

        async def __aenter__(self):
            downloads.append(1)
            await asyncio.sleep(0.01)
            return self

        async def __aexit__(self, *args):
            pass

        @property
        def content(self):
            async def iter_chunked(size):
                yield b"firmware"
            return type("C", (), {"iter_chunked": staticmethod(iter_chunked)})()

    agent._session = type("S", (), {"get": lambda self, url: Response()})()
    paths = await asyncio.gather(*(agent._fetch_firmware("http://x/fw.bin", None, agent.artifact_dir)
                                   for _ in range(3)))
    assert len(downloads) == 1
    assert {p.read_bytes() for p in paths} == {b"firmware"}


async def test_jobs_go_to_healthy_places_only(three_places):
    agent = three_places
    await agent._check_health(wait=True)
    agent._labgrid.calls.clear()
    agent._places[0].reservation = "OTHER"  # p1 busy
    assert agent.is_device_healthy("openwrt_one")
    lease = await agent._reserve("openwrt_one")
    assert lease.place == "p3"  # not p2 (bad)
    agent._places[2].reservation = "OTHER"
    assert await agent._reserve("openwrt_one") is None


async def test_platform_unhealthy_when_all_places_bad(agent):
    agent._places = [Place(f"p{i}", {"device": "openwrt_one"}) for i in (1, 2)]
    agent._device_health["p1"] = DeviceHealth("p1", state="bad")
    assert agent.is_device_healthy("openwrt_one")
    agent._device_health["p2"] = DeviceHealth("p2", state="bad")
    assert not agent.is_device_healthy("openwrt_one")


async def test_old_per_platform_health_is_forgotten(three_places):
    agent = three_places
    agent._device_health["openwrt_one"] = DeviceHealth("openwrt_one", state="bad")
    await agent._check_health(wait=True)
    assert set(agent._device_health) == {"p1", "p2", "p3"}


def test_release_of():
    assert LabgridAgent._release_of(RELEASE_URL) == "25.12.5"
    assert LabgridAgent._release_of("https://downloads.openwrt.org/snapshots/targets/x/fw") == "SNAPSHOT"
    assert LabgridAgent._release_of("https://example.org/fw.bin") is None
