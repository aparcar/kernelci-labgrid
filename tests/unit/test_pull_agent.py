"""Unit tests for the pull agent."""

import pytest
from pathlib import Path

from kernelci_labgrid.scheduler.pull_agent import (
    LabgridPullAgent,
    TestResult,
)


class TestTestResult:
    """Tests for TestResult dataclass."""

    def test_success_when_no_failures(self):
        result = TestResult(passed=5, failed=0, errors=0)
        assert result.success is True
        assert result.result == "pass"

    def test_failure_when_tests_fail(self):
        result = TestResult(passed=3, failed=2, errors=0)
        assert result.success is False
        assert result.result == "fail"

    def test_incomplete_when_errors(self):
        result = TestResult(passed=3, failed=0, errors=1)
        assert result.success is False
        assert result.result == "incomplete"

    def test_skip_when_only_skipped(self):
        result = TestResult(passed=0, failed=0, skipped=5, errors=0)
        assert result.result == "skip"


class TestLabgridPullAgent:
    """Tests for LabgridPullAgent class."""

    def test_initialization(self, tmp_path):
        agent = LabgridPullAgent(
            api_url="https://api.example.com",
            api_token="test-token",
            lab_name="test-lab",
            tests_dir=tmp_path / "tests",
            targets_dir=tmp_path / "targets",
        )

        assert agent.api_url == "https://api.example.com"
        assert agent.lab_name == "test-lab"
        assert agent.tests_dir == tmp_path / "tests"
        assert agent.targets_dir == tmp_path / "targets"

    def test_url_trailing_slash_removed(self, tmp_path):
        agent = LabgridPullAgent(
            api_url="https://api.example.com/",
            api_token="token",
            lab_name="lab",
            tests_dir=tmp_path,
        )
        assert agent.api_url == "https://api.example.com"

    def test_default_targets_dir(self, tmp_path):
        tests_dir = tmp_path / "repo" / "tests"
        tests_dir.mkdir(parents=True)

        agent = LabgridPullAgent(
            api_url="https://api.example.com",
            api_token="token",
            lab_name="lab",
            tests_dir=tests_dir,
        )

        assert agent.targets_dir == tmp_path / "repo" / "targets"


class TestFindTargetYaml:
    """Tests for target YAML file discovery."""

    def test_exact_match(self, tmp_path):
        targets_dir = tmp_path / "targets"
        targets_dir.mkdir()
        (targets_dir / "qemu-x86-64.yaml").touch()

        agent = LabgridPullAgent(
            api_url="https://api.example.com",
            api_token="token",
            lab_name="lab",
            tests_dir=tmp_path / "tests",
            targets_dir=targets_dir,
        )

        result = agent._find_target_yaml("qemu-x86-64")
        assert result == targets_dir / "qemu-x86-64.yaml"

    def test_glob_match(self, tmp_path):
        targets_dir = tmp_path / "targets"
        targets_dir.mkdir()
        (targets_dir / "rpi4-board.yaml").touch()

        agent = LabgridPullAgent(
            api_url="https://api.example.com",
            api_token="token",
            lab_name="lab",
            tests_dir=tmp_path / "tests",
            targets_dir=targets_dir,
        )

        result = agent._find_target_yaml("rpi4")
        assert result is not None
        assert "rpi4" in result.name

    def test_no_match(self, tmp_path):
        targets_dir = tmp_path / "targets"
        targets_dir.mkdir()

        agent = LabgridPullAgent(
            api_url="https://api.example.com",
            api_token="token",
            lab_name="lab",
            tests_dir=tmp_path / "tests",
            targets_dir=targets_dir,
        )

        result = agent._find_target_yaml("nonexistent")
        assert result is None


class TestParseJunitXml:
    """Tests for JUnit XML parsing."""

    @pytest.fixture
    def agent(self, tmp_path):
        return LabgridPullAgent(
            api_url="https://api.example.com",
            api_token="token",
            lab_name="lab",
            tests_dir=tmp_path,
            artifact_dir=tmp_path / "artifacts",
        )

    def test_parse_basic_junit(self, agent, tmp_path):
        junit_xml = tmp_path / "results.xml"
        junit_xml.write_text("""<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="3" errors="0" failures="1" skipped="0" time="5.5">
    <testcase classname="test_boot" name="test_login" time="1.2"/>
    <testcase classname="test_boot" name="test_network" time="2.3">
        <failure message="AssertionError">assert False</failure>
    </testcase>
    <testcase classname="test_boot" name="test_memory" time="2.0"/>
</testsuite>
""")

        result = agent._parse_junit_xml(junit_xml)

        assert result.total == 3
        assert result.passed == 2
        assert result.failed == 1
        assert result.skipped == 0
        assert result.duration == 5.5
        assert len(result.test_cases) == 3

    def test_parse_with_skipped(self, agent, tmp_path):
        junit_xml = tmp_path / "results.xml"
        junit_xml.write_text("""<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="3" errors="0" failures="0" skipped="1" time="3.0">
    <testcase classname="test_boot" name="test_login" time="1.0"/>
    <testcase classname="test_boot" name="test_skip" time="0.1">
        <skipped message="not implemented"/>
    </testcase>
    <testcase classname="test_boot" name="test_other" time="1.9"/>
</testsuite>
""")

        result = agent._parse_junit_xml(junit_xml)

        assert result.total == 3
        assert result.passed == 2
        assert result.skipped == 1
        assert result.failed == 0

    def test_parse_with_errors(self, agent, tmp_path):
        junit_xml = tmp_path / "results.xml"
        junit_xml.write_text("""<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="2" errors="1" failures="0" skipped="0" time="1.0">
    <testcase classname="test_boot" name="test_login" time="0.5"/>
    <testcase classname="test_boot" name="test_crash" time="0.5">
        <error message="RuntimeError">Traceback...</error>
    </testcase>
</testsuite>
""")

        result = agent._parse_junit_xml(junit_xml)

        assert result.total == 2
        assert result.errors == 1
        assert result.result == "incomplete"

    def test_parse_missing_file(self, agent, tmp_path):
        result = agent._parse_junit_xml(tmp_path / "nonexistent.xml")

        assert result.total == 0
        assert result.passed == 0

    def test_parse_testsuites_wrapper(self, agent, tmp_path):
        """Test parsing when root is <testsuites> (multiple suites)."""
        junit_xml = tmp_path / "results.xml"
        junit_xml.write_text("""<?xml version="1.0" encoding="utf-8"?>
<testsuites>
    <testsuite name="suite1" tests="2" errors="0" failures="0" skipped="0" time="1.0">
        <testcase classname="test_a" name="test_1" time="0.5"/>
        <testcase classname="test_a" name="test_2" time="0.5"/>
    </testsuite>
    <testsuite name="suite2" tests="1" errors="0" failures="1" skipped="0" time="0.5">
        <testcase classname="test_b" name="test_3" time="0.5">
            <failure message="failed"/>
        </testcase>
    </testsuite>
</testsuites>
""")

        result = agent._parse_junit_xml(junit_xml)

        assert result.total == 3
        assert result.passed == 2
        assert result.failed == 1


class TestMapOutcome:
    """Tests for pytest/JUnit outcome mapping."""

    def test_junit_pass(self):
        assert LabgridPullAgent._map_outcome("pass") == "pass"

    def test_junit_fail(self):
        assert LabgridPullAgent._map_outcome("fail") == "fail"

    def test_junit_skip(self):
        assert LabgridPullAgent._map_outcome("skip") == "skip"

    def test_junit_error(self):
        assert LabgridPullAgent._map_outcome("error") == "incomplete"

    def test_pytest_passed(self):
        assert LabgridPullAgent._map_outcome("passed") == "pass"

    def test_pytest_failed(self):
        assert LabgridPullAgent._map_outcome("failed") == "fail"

    def test_pytest_skipped(self):
        assert LabgridPullAgent._map_outcome("skipped") == "skip"

    def test_unknown(self):
        assert LabgridPullAgent._map_outcome("unknown") == "skip"
