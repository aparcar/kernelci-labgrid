"""Pytest configuration and shared fixtures."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_runtime_config(temp_dir):
    """Create a sample runtime configuration file."""
    config = {
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

    config_path = temp_dir / "runtime.yaml"
    with open(config_path, "w") as f:
        yaml.dump(config, f)

    return config_path


@pytest.fixture
def sample_scheduler_config(temp_dir):
    """Create a sample scheduler configuration file."""
    config = {
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
                "platforms": ["test-platform"],
                "test_suite": "baseline",
            }
        ]
    }

    config_path = temp_dir / "scheduler.yaml"
    with open(config_path, "w") as f:
        yaml.dump(config, f)

    return config_path


@pytest.fixture
def mock_env_vars():
    """Set up mock environment variables for tests."""
    original = os.environ.copy()

    os.environ["KCI_API_URL"] = "https://test-api.kernelci.org"
    os.environ["KCI_API_TOKEN"] = "test-token-12345"
    os.environ["LABGRID_COORDINATOR"] = "test-coordinator:20408"

    yield

    # Restore original environment
    os.environ.clear()
    os.environ.update(original)


@pytest.fixture
def sample_test_job():
    """Create a sample test job for tests."""
    from kernelci_labgrid.runtime.labgrid import TestJob

    return TestJob(
        node_id="test-node-123",
        name="baseline-arm64",
        platform="bcm2711-rpi-4-b",
        kernel_revision="v6.8-rc1",
        kernel_url="https://storage.example.com/kernel",
        dtb_url="https://storage.example.com/dtb",
        rootfs_url="https://storage.example.com/rootfs",
        test_suite="baseline",
        timeout=1800,
    )


@pytest.fixture
def sample_place():
    """Create a sample Labgrid place for tests."""
    from kernelci_labgrid.runtime.labgrid_client import Place

    return Place(
        name="rpi4-slot1",
        aliases=["rpi4-1"],
        comment="Raspberry Pi 4 test board",
        tags={
            "arch": "arm64",
            "platform": "bcm2711-rpi-4-b",
            "compatible": "brcm,bcm2711",
        },
    )


# Markers for skipping tests
def pytest_configure(config):
    """Configure custom markers."""
    config.addinivalue_line(
        "markers", "integration: mark test as integration test"
    )
    config.addinivalue_line(
        "markers", "requires_coordinator: mark test as requiring a running coordinator"
    )
    config.addinivalue_line(
        "markers", "requires_api: mark test as requiring KernelCI API access"
    )
