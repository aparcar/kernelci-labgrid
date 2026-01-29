"""Unit tests for Labgrid runtime."""

import pytest

from kernelci_labgrid.runtime.labgrid import (
    LabgridRuntime,
    LabgridRuntimeError,
    TestJob,
    TestResult,
    PlaceMapping,
)


class TestTestJob:
    """Tests for TestJob dataclass."""

    def test_basic_job(self):
        """Test creating a basic test job."""
        job = TestJob(
            node_id="node-123",
            name="baseline-test",
            platform="bcm2711-rpi-4-b",
            kernel_revision="v6.8-rc1",
            kernel_url="https://example.com/kernel",
        )

        assert job.node_id == "node-123"
        assert job.name == "baseline-test"
        assert job.platform == "bcm2711-rpi-4-b"
        assert job.test_suite == "baseline"
        assert job.timeout == 3600

    def test_job_with_artifacts(self):
        """Test creating a job with all artifacts."""
        job = TestJob(
            node_id="node-123",
            name="test",
            platform="test-platform",
            kernel_revision="v6.8",
            kernel_url="https://example.com/kernel",
            dtb_url="https://example.com/dtb",
            rootfs_url="https://example.com/rootfs",
            modules_url="https://example.com/modules",
        )

        assert job.dtb_url is not None
        assert job.rootfs_url is not None
        assert job.modules_url is not None


class TestTestResult:
    """Tests for TestResult dataclass."""

    def test_successful_result(self):
        """Test creating a successful result."""
        job = TestJob(
            node_id="node",
            name="test",
            platform="platform",
            kernel_revision="v6.8",
            kernel_url="url",
        )

        result = TestResult(
            job=job,
            success=True,
            result="pass",
            test_cases=[
                {"name": "login", "result": "pass"},
                {"name": "kernelmsg", "result": "pass"},
            ],
            duration=120.5,
        )

        assert result.success
        assert result.result == "pass"
        assert len(result.test_cases) == 2
        assert result.duration == 120.5
        assert result.error is None

    def test_failed_result(self):
        """Test creating a failed result."""
        job = TestJob(
            node_id="node",
            name="test",
            platform="platform",
            kernel_revision="v6.8",
            kernel_url="url",
        )

        result = TestResult(
            job=job,
            success=False,
            result="fail",
            error="Kernel panic detected",
        )

        assert not result.success
        assert result.result == "fail"
        assert result.error == "Kernel panic detected"


class TestPlaceMapping:
    """Tests for PlaceMapping dataclass."""

    def test_basic_mapping(self):
        """Test creating a basic place mapping."""
        mapping = PlaceMapping(
            place="rpi4-slot1",
            platform="bcm2711-rpi-4-b",
            compatible=["brcm,bcm2711"],
        )

        assert mapping.place == "rpi4-slot1"
        assert mapping.platform == "bcm2711-rpi-4-b"
        assert "brcm,bcm2711" in mapping.compatible


class TestLabgridRuntime:
    """Tests for LabgridRuntime class."""

    def test_initialization(self):
        """Test runtime initialization."""
        config = {
            "coordinator_address": "localhost:20408",
            "mode": "push",
            "place_mapping": [
                {
                    "place": "board-1",
                    "platform": "test-platform",
                    "compatible": ["test,compat"],
                }
            ],
        }

        runtime = LabgridRuntime(config)

        assert runtime.coordinator_address == "localhost:20408"
        assert runtime.mode == "push"
        assert "test-platform" in runtime.place_mappings

    def test_get_place_for_platform(self):
        """Test getting place name for platform."""
        config = {
            "coordinator_address": "localhost:20408",
            "place_mapping": [
                {"place": "rpi4", "platform": "bcm2711-rpi-4-b", "compatible": []},
                {"place": "imx8", "platform": "imx8mm-evk", "compatible": []},
            ],
        }

        runtime = LabgridRuntime(config)

        assert runtime.get_place_for_platform("bcm2711-rpi-4-b") == "rpi4"
        assert runtime.get_place_for_platform("imx8mm-evk") == "imx8"
        assert runtime.get_place_for_platform("unknown") is None

    def test_not_connected_error(self):
        """Test error when runtime not connected."""
        runtime = LabgridRuntime({"coordinator_address": "localhost:20408"})

        with pytest.raises(LabgridRuntimeError, match="not connected"):
            _ = runtime.client

    def test_parse_tap_output(self):
        """Test TAP output parsing."""
        runtime = LabgridRuntime({"coordinator_address": "localhost:20408"})

        output = """
TAP version 13
1..5
ok 1 - test_login
ok 2 - test_boot
not ok 3 - test_network
ok 4 - test_memory
# SKIP test_gpu - no GPU present
"""

        results = runtime._parse_tap_output(output)

        assert len(results) >= 3
        # Check that we have passes and failures
        pass_count = sum(1 for r in results if r["result"] == "pass")
        fail_count = sum(1 for r in results if r["result"] == "fail")
        assert pass_count >= 2
        assert fail_count >= 1

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """Test runtime context manager."""
        config = {"coordinator_address": "invalid:99999"}
        runtime = LabgridRuntime(config)

        # This will fail to connect, but tests the structure
        with pytest.raises(Exception):
            async with runtime:
                pass


class TestRuntimeTemplateGeneration:
    """Tests for test template generation."""

    @pytest.fixture
    def runtime(self):
        """Create a runtime for testing."""
        return LabgridRuntime({
            "coordinator_address": "localhost:20408",
            "place_mapping": [
                {
                    "place": "board-1",
                    "platform": "test-platform",
                    "compatible": ["test,compat-1", "test,compat-2"],
                }
            ],
        })

    def test_generate_baseline_test(self, runtime):
        """Test generating baseline test script."""
        from kernelci_labgrid.runtime.labgrid_client import Place

        job = TestJob(
            node_id="node-123",
            name="baseline-test",
            platform="test-platform",
            kernel_revision="v6.8",
            kernel_url="url",
            test_suite="baseline",
        )

        place = Place(name="board-1")

        script = runtime.generate_test_script(job, place)

        assert "baseline-test" in script
        assert "node-123" in script
        assert "board-1" in script
        assert "class TestBaseline" in script

    def test_generate_kselftest(self, runtime):
        """Test generating kselftest script."""
        from kernelci_labgrid.runtime.labgrid_client import Place

        job = TestJob(
            node_id="node-456",
            name="kselftest-timers",
            platform="test-platform",
            kernel_revision="v6.8",
            kernel_url="url",
            test_suite="kselftest",
            test_config={"test_name": "timers"},
        )

        place = Place(name="board-1")

        script = runtime.generate_test_script(job, place)

        assert "kselftest-timers" in script
        assert "class TestKselftest" in script
        assert "timers" in script
