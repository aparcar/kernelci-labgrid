"""Unit tests for configuration management."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from kernelci_labgrid.config import (
    LabgridConfig,
    RuntimeConfig,
    SchedulerJobConfig,
    PlaceMappingConfig,
    load_config,
)


class TestLabgridConfig:
    """Tests for LabgridConfig class."""

    def test_from_dict_empty(self):
        """Test loading empty configuration."""
        config = LabgridConfig.from_dict({})
        assert len(config.runtimes) == 0
        assert len(config.scheduler_jobs) == 0

    def test_from_dict_with_runtime(self):
        """Test loading configuration with runtime."""
        data = {
            "runtimes": {
                "test-lab": {
                    "lab_type": "labgrid",
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
            }
        }

        config = LabgridConfig.from_dict(data)

        assert len(config.runtimes) == 1
        assert "test-lab" in config.runtimes

        runtime = config.runtimes["test-lab"]
        assert runtime.name == "test-lab"
        assert runtime.coordinator_address == "localhost:20408"
        assert runtime.mode == "push"
        assert len(runtime.place_mapping) == 1

    def test_from_dict_with_scheduler(self):
        """Test loading configuration with scheduler jobs."""
        data = {
            "scheduler": [
                {
                    "job": "baseline-test",
                    "event": {
                        "channel": "node",
                        "name": "kbuild-*",
                        "result": "pass",
                    },
                    "runtime": {
                        "type": "labgrid",
                        "name": "test-lab",
                    },
                    "platforms": ["platform-1", "platform-2"],
                    "test_suite": "baseline",
                }
            ]
        }

        config = LabgridConfig.from_dict(data)

        assert len(config.scheduler_jobs) == 1

        job = config.scheduler_jobs[0]
        assert job.job == "baseline-test"
        assert job.event.channel == "node"
        assert job.event.name == "kbuild-*"
        assert job.runtime_name == "test-lab"
        assert len(job.platforms) == 2

    def test_environment_variable_expansion(self):
        """Test that environment variables are expanded in config."""
        os.environ["TEST_COORDINATOR"] = "remote-host:20408"

        data = {
            "runtimes": {
                "test-lab": {
                    "coordinator_address": "${TEST_COORDINATOR}",
                }
            }
        }

        config = LabgridConfig.from_dict(data)
        assert config.runtimes["test-lab"].coordinator_address == "remote-host:20408"

        del os.environ["TEST_COORDINATOR"]

    def test_get_jobs_for_event_exact_match(self):
        """Test matching jobs by exact event name."""
        config = LabgridConfig.from_dict({
            "scheduler": [
                {
                    "job": "test-job",
                    "event": {
                        "channel": "node",
                        "name": "kbuild-gcc-12-arm64",
                        "result": "pass",
                    },
                    "runtime": {"type": "labgrid", "name": "lab"},
                    "platforms": [],
                }
            ]
        })

        # Exact match
        jobs = config.get_jobs_for_event("node", "kbuild-gcc-12-arm64", "pass")
        assert len(jobs) == 1
        assert jobs[0].job == "test-job"

        # No match - different name
        jobs = config.get_jobs_for_event("node", "kbuild-gcc-12-x86", "pass")
        assert len(jobs) == 0

        # No match - different result
        jobs = config.get_jobs_for_event("node", "kbuild-gcc-12-arm64", "fail")
        assert len(jobs) == 0

    def test_get_jobs_for_event_wildcard(self):
        """Test matching jobs by wildcard pattern."""
        config = LabgridConfig.from_dict({
            "scheduler": [
                {
                    "job": "test-job",
                    "event": {
                        "channel": "node",
                        "name": "kbuild-gcc-*-arm64",
                        "result": "pass",
                    },
                    "runtime": {"type": "labgrid", "name": "lab"},
                    "platforms": [],
                }
            ]
        })

        # Should match various GCC versions
        jobs = config.get_jobs_for_event("node", "kbuild-gcc-12-arm64", "pass")
        assert len(jobs) == 1

        jobs = config.get_jobs_for_event("node", "kbuild-gcc-13-arm64", "pass")
        assert len(jobs) == 1

        # Should not match x86
        jobs = config.get_jobs_for_event("node", "kbuild-gcc-12-x86", "pass")
        assert len(jobs) == 0

    def test_from_files(self):
        """Test loading configuration from files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create runtime config
            runtime_config = {
                "runtimes": {
                    "file-lab": {
                        "coordinator_address": "file-host:20408",
                    }
                }
            }
            runtime_path = Path(tmpdir) / "runtime.yaml"
            with open(runtime_path, "w") as f:
                yaml.dump(runtime_config, f)

            # Create scheduler config
            scheduler_config = {
                "scheduler": [
                    {
                        "job": "file-job",
                        "event": {"channel": "node", "name": "test", "result": "pass"},
                        "runtime": {"type": "labgrid", "name": "file-lab"},
                        "platforms": [],
                    }
                ]
            }
            scheduler_path = Path(tmpdir) / "scheduler.yaml"
            with open(scheduler_path, "w") as f:
                yaml.dump(scheduler_config, f)

            # Load config
            config = LabgridConfig.from_files(runtime_path, scheduler_path)

            assert "file-lab" in config.runtimes
            assert len(config.scheduler_jobs) == 1

    def test_to_dict(self):
        """Test converting configuration back to dictionary."""
        data = {
            "runtimes": {
                "test-lab": {
                    "lab_type": "labgrid",
                    "coordinator_address": "localhost:20408",
                    "mode": "push",
                    "place_mapping": [
                        {"place": "board-1", "platform": "platform-1", "compatible": []},
                    ],
                }
            },
            "scheduler": [
                {
                    "job": "test-job",
                    "event": {"channel": "node", "name": "test", "result": "pass"},
                    "runtime": {"type": "labgrid", "name": "test-lab"},
                    "platforms": ["platform-1"],
                    "test_suite": "baseline",
                }
            ],
        }

        config = LabgridConfig.from_dict(data)
        result = config.to_dict()

        assert "runtimes" in result
        assert "test-lab" in result["runtimes"]
        assert len(result["scheduler"]) == 1


class TestPlaceMappingConfig:
    """Tests for PlaceMappingConfig."""

    def test_basic_creation(self):
        """Test creating a place mapping."""
        mapping = PlaceMappingConfig(
            place="rpi4-slot1",
            platform="bcm2711-rpi-4-b",
            compatible=["brcm,bcm2711"],
            tags={"arch": "arm64"},
        )

        assert mapping.place == "rpi4-slot1"
        assert mapping.platform == "bcm2711-rpi-4-b"
        assert "brcm,bcm2711" in mapping.compatible
        assert mapping.tags["arch"] == "arm64"


class TestRuntimeConfig:
    """Tests for RuntimeConfig."""

    def test_default_values(self):
        """Test default values for runtime config."""
        runtime = RuntimeConfig(name="test")

        assert runtime.lab_type == "labgrid"
        assert runtime.coordinator_address == "localhost:20408"
        assert runtime.mode == "push"
        assert runtime.secure is False


class TestSchedulerJobConfig:
    """Tests for SchedulerJobConfig."""

    def test_default_values(self):
        """Test default values for scheduler job config."""
        from kernelci_labgrid.config import EventConfig

        job = SchedulerJobConfig(
            job="test",
            event=EventConfig(),
            runtime_type="labgrid",
            runtime_name="lab",
        )

        assert job.test_suite == "baseline"
        assert job.timeout == 3600
        assert job.priority == 50
