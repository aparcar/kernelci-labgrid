"""Unit tests for the KernelCI API protocol side of the agent."""

import pytest

from kernelci_labgrid.scheduler.pull_agent import (
    PYTEST_RC_NO_SHELL,
    LabgridAgent,
    TestResult,
)

JOB = {
    "id": "job1",
    "parent": "build1",
    "name": "openwrt-tests",
    "kind": "job",
    "path": ["checkout", "armsr-armv8-generic", "openwrt-tests"],
    "state": "available",
    "created": "2026-10-01T10:00:00",
    "artifacts": {"job_definition": "http://storage/openwrt/jobs/job1.json"},
    "data": {"runtime": "openwrt-local", "platform": "qemu_armsr-armv8", "arch": "aarch64_generic"},
}


@pytest.fixture
def agent(tmp_path):
    return LabgridAgent(
        api_url="http://localhost:8001/latest",
        api_token="token",
        lab_name="openwrt-local",
        tests_dir=tmp_path / "tests",
        artifact_dir=tmp_path / "artifacts",
        platforms=["qemu_armsr-armv8"],
    )


def case(module, name, result, duration=1.0, message=""):
    return {"module": module, "name": name, "classname": f"tests.test_{module}",
            "result": result, "duration": duration, "message": message}


class TestModuleName:
    def test_openwrt_tests_module(self):
        assert LabgridAgent._module_name("tests.test_base") == "base"

    def test_class_in_module(self):
        assert LabgridAgent._module_name("tests.test_wifi.TestAp") == "wifi"

    def test_no_package(self):
        assert LabgridAgent._module_name("test_boot") == "boot"

    def test_empty(self):
        assert LabgridAgent._module_name("") == "tests"


class TestJobResult:
    def test_pass(self):
        r = TestResult(passed=2, total=2, returncode=0)
        assert LabgridAgent._job_result(r) == "pass"

    def test_fail(self):
        r = TestResult(passed=1, failed=1, total=2, returncode=1)
        assert LabgridAgent._job_result(r) == "fail"

    def test_no_shell_is_fail_not_infra(self):
        r = TestResult(errors=1, total=1, returncode=PYTEST_RC_NO_SHELL)
        assert LabgridAgent._job_result(r) == "fail"

    def test_crash_is_incomplete(self):
        r = TestResult(passed=1, total=1, returncode=2)
        assert LabgridAgent._job_result(r) == "incomplete"

    def test_nothing_ran_is_incomplete(self):
        assert LabgridAgent._job_result(TestResult(returncode=5)) == "incomplete"


class TestBootResult:
    def test_from_test_shell(self):
        r = TestResult(test_cases=[case("base", "test_shell", "pass")], returncode=1)
        assert r.boot_result == "pass"

    def test_shell_failed(self):
        r = TestResult(test_cases=[case("base", "test_shell", "fail")], returncode=1)
        assert r.boot_result == "fail"

    def test_no_shell_exit_code(self):
        assert TestResult(returncode=PYTEST_RC_NO_SHELL).boot_result == "fail"

    def test_other_tests_passed(self):
        r = TestResult(passed=1, test_cases=[case("lan", "test_lan", "pass")], returncode=0)
        assert r.boot_result == "pass"


class TestBuildHierarchy:
    def test_layout_and_paths(self, agent):
        result = TestResult(
            passed=2, failed=1, skipped=1, total=4, duration=12.5, returncode=1,
            test_cases=[
                case("base", "test_shell", "pass", 2.0),
                case("base", "test_ssh", "fail", 3.0, "timeout"),
                case("lan", "test_lan_dhcp", "pass"),
                case("wifi", "test_ap", "skip"),
            ],
        )
        h = agent._build_hierarchy(JOB, result, {"test_log": "http://storage/console"})

        node = h["node"]
        assert node["id"] == "job1"
        assert node["state"] == "done"
        assert node["result"] == "fail"
        assert node["created"] == JOB["created"]  # not reset by the PUT
        assert node["artifacts"]["job_definition"] == JOB["artifacts"]["job_definition"]
        assert node["artifacts"]["test_log"] == "http://storage/console"
        assert node["data"]["device"] == "openwrt-local-qemu_armsr-armv8"
        assert node["data"]["summary"] == {
            "total": 4, "passed": 2, "failed": 1, "errors": 0, "skipped": 1, "boot": "pass"}

        modules = {c["node"]["name"]: c for c in h["child_nodes"]}
        assert set(modules) == {"base", "lan", "wifi"}
        assert modules["base"]["node"]["result"] == "fail"
        assert modules["base"]["node"]["path"] == JOB["path"] + ["base"]
        assert modules["wifi"]["node"]["result"] == "skip"

        ssh = modules["base"]["child_nodes"][1]["node"]
        assert ssh["name"] == "test_ssh"
        assert ssh["kind"] == "test"
        assert ssh["path"] == JOB["path"] + ["base", "test_ssh"]
        assert ssh["result"] == "fail"
        assert ssh["data"]["error_msg"] == "timeout"
        assert ssh["data"]["duration_ms"] == 3000

    def test_infra_error_code(self, agent):
        h = agent._build_hierarchy(JOB, TestResult(returncode=2), {})
        assert h["node"]["result"] == "incomplete"
        assert h["node"]["data"]["error_code"] == "Infrastructure"

    def test_no_shell_is_not_infra(self, agent):
        h = agent._build_hierarchy(JOB, TestResult(returncode=PYTEST_RC_NO_SHELL), {})
        assert h["node"]["result"] == "fail"
        assert "error_code" not in h["node"]["data"]


class TestClaim:
    @pytest.fixture
    def api(self, agent, monkeypatch):
        calls = {"put": []}
        state = {"node": dict(JOB, data=dict(JOB["data"]))}

        async def fake_get(endpoint, **params):
            calls.setdefault("get", []).append((endpoint, params))
            if endpoint == "/nodes":
                return {"items": [state["node"]]}
            return dict(state["node"], data=dict(state["node"]["data"]))

        async def fake_put(endpoint, data):
            calls["put"].append((endpoint, data))
            state["node"] = dict(data, data=dict(data["data"]))
            return data

        monkeypatch.setattr(agent, "_api_get", fake_get)
        monkeypatch.setattr(agent, "_api_put", fake_put)
        monkeypatch.setattr("kernelci_labgrid.scheduler.pull_agent.random.uniform", lambda a, b: 0)
        return calls, state

    async def test_claims_available_job(self, agent, api):
        calls, _ = api
        claimed = await agent._claim(JOB)
        assert claimed["data"]["job_id"].startswith("openwrt-local:")
        assert claimed["state"] == "available"
        endpoint, body = calls["put"][0]
        assert endpoint == "/node/job1"
        assert body["created"] == JOB["created"]

    async def test_skips_already_claimed(self, agent, api):
        _, state = api
        state["node"]["data"]["job_id"] = "other-lab:abc"
        assert await agent._claim(JOB) is None

    async def test_skips_not_available(self, agent, api):
        _, state = api
        state["node"]["state"] = "done"
        assert await agent._claim(JOB) is None

    async def test_poll_query(self, agent, api, monkeypatch):
        calls, _ = api
        started = []
        monkeypatch.setattr(agent, "_execute_job", lambda job, lease=None: _record(started, job))
        await agent._poll_and_execute()
        endpoint, params = calls["get"][0]
        assert endpoint == "/nodes"
        assert params["kind"] == "job"
        assert params["state"] == "available"
        assert params["data.runtime"] == "openwrt-local"
        assert params["data.platform"] == "qemu_armsr-armv8"
        assert "qemu_armsr-armv8" in agent._busy_platforms

    async def test_lost_race_backs_off(self, agent, api, monkeypatch):
        calls, state = api

        async def racing_put(endpoint, data):
            calls["put"].append((endpoint, data))
            # Another lab writes its claim right after ours
            state["node"] = dict(data, data=dict(data["data"], job_id="other-lab:xyz"))
            return data

        monkeypatch.setattr(agent, "_api_put", racing_put)
        assert await agent._claim(JOB) is None

    async def test_pool_job_moves_to_lab(self, agent, api):
        calls, state = api
        agent.pool = "openwrt-labs"
        state["node"]["data"]["runtime"] = "openwrt-labs"
        claimed = await agent._claim(state["node"])
        assert claimed["data"]["runtime"] == "openwrt-local"
        assert claimed["data"]["pool"] == "openwrt-labs"

    async def test_polls_lab_then_pool(self, agent, api, monkeypatch):
        calls, state = api
        agent.pool = "openwrt-labs"
        state["node"]["data"]["job_id"] = "other-lab:abc"  # nothing claimable
        await agent._poll_and_execute()
        runtimes = [p["data.runtime"] for e, p in calls["get"] if e == "/nodes"]
        assert runtimes == ["openwrt-local", "openwrt-labs"]
        assert not calls["put"]

    async def test_unhealthy_platform_not_polled(self, agent, api):
        from kernelci_labgrid.scheduler.pull_agent import DeviceHealth

        calls, _ = api
        agent._device_health["qemu_armsr-armv8"] = DeviceHealth("qemu_armsr-armv8", state="bad")
        await agent._poll_and_execute()
        assert "get" not in calls


async def _record(started, job):
    started.append(job)
