"""Unit tests for the pull agent."""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


class TestParsePytestOutput:
    """Tests for pytest output parsing."""

    @pytest.fixture
    def agent(self, tmp_path):
        return LabgridPullAgent(
            api_url="https://api.example.com",
            api_token="token",
            lab_name="lab",
            tests_dir=tmp_path,
        )

    def test_parse_summary_line(self, agent):
        output = """
============================= test session starts ==============================
collected 10 items

test_boot.py::test_login PASSED
test_boot.py::test_network FAILED

========================= 8 passed, 2 failed in 5.23s =========================
"""
        result = agent._parse_pytest_output(output)

        assert result.passed == 8
        assert result.failed == 2
        assert result.duration == 5.23

    def test_parse_with_skipped(self, agent):
        output = "3 passed, 1 failed, 2 skipped in 10.5s"
        result = agent._parse_pytest_output(output)

        assert result.passed == 3
        assert result.failed == 1
        assert result.skipped == 2

    def test_empty_output(self, agent):
        result = agent._parse_pytest_output("")
        assert result.passed == 0
        assert result.failed == 0


class TestMapOutcome:
    """Tests for pytest outcome mapping."""

    def test_passed(self):
        assert LabgridPullAgent._map_outcome("passed") == "pass"

    def test_failed(self):
        assert LabgridPullAgent._map_outcome("failed") == "fail"

    def test_skipped(self):
        assert LabgridPullAgent._map_outcome("skipped") == "skip"

    def test_error(self):
        assert LabgridPullAgent._map_outcome("error") == "incomplete"

    def test_unknown(self):
        assert LabgridPullAgent._map_outcome("unknown") == "skip"
