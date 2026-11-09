"""Health reports: healthcheck nodes tell the server which devices a lab has."""

from datetime import datetime, timedelta

import pytest

from kernelci_labgrid.labgrid import Lease, Place
from kernelci_labgrid.scheduler.pull_agent import (
    HEARTBEAT_HOURS,
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
    await agent._check_health()
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
    await agent._check_health()
    [node] = posted
    assert node["result"] == "pass"
    assert "error_msg" not in node["data"]
    check = node["data"]["misc"]["healthcheck"]
    assert check["release"] == "25.12.5"
    assert check["passed"] == 2
    # done: the next round only sends heartbeats, none due yet
    await agent._check_health()
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
    await agent._check_health()
    agent._device_health["qemu_x86-64"].last_check -= timedelta(days=30)
    await agent._check_health()
    assert seen == ["run 0\n", "run 1\n"]


async def test_no_golden_image_reports_unknown(agent, posted, monkeypatch):
    agent.health_checks = {}

    async def golden(config):
        raise RuntimeError("openwrt_one is in no release")

    monkeypatch.setattr(agent, "_golden_image", golden)
    await agent._check_health()
    [node] = posted
    assert node["result"] == "skip"
    assert "no release" in node["data"]["error_msg"]


async def test_maintenance_tag(agent, posted):
    agent._labgrid = FakeLabgrid([])
    agent._places = [Place("labgrid-aparcar-openwrt_one",
                           {"device": "openwrt_one", "maintenance": "new_psu"})]
    agent.platforms = ["openwrt_one"]
    agent.health_checks = {}  # would check, but maintenance wins
    await agent._check_health()
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
    await agent._check_health()
    assert [(n["data"]["device"], n["result"]) for n in posted] == [
        ("p1", "pass"), ("p2", "fail"), ("p3", "pass")]
    assert all(n["data"]["platform"] == "openwrt_one" for n in posted)
    assert ("reserve", "openwrt_one", "p2") in agent._labgrid.calls
    assert agent.get_device_health("p2").state == "bad"
    # nothing due: no new reports
    await agent._check_health()
    assert len(posted) == 3


async def test_jobs_go_to_healthy_places_only(three_places):
    agent = three_places
    await agent._check_health()
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
    await agent._check_health()
    assert set(agent._device_health) == {"p1", "p2", "p3"}


def test_release_of():
    assert LabgridAgent._release_of(RELEASE_URL) == "25.12.5"
    assert LabgridAgent._release_of("https://downloads.openwrt.org/snapshots/targets/x/fw") == "SNAPSHOT"
    assert LabgridAgent._release_of("https://example.org/fw.bin") is None
