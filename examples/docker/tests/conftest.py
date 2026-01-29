"""Pytest configuration and fixtures for OpenWrt testing with Labgrid."""

import os
import pytest


def pytest_addoption(parser):
    """Add custom command line options."""
    parser.addoption(
        "--openwrt-branch",
        default="main",
        help="OpenWrt branch being tested (main, openwrt-25.12, etc.)",
    )


@pytest.fixture(scope="session")
def openwrt_branch(request):
    """Get the OpenWrt branch being tested."""
    return request.config.getoption("--openwrt-branch")


@pytest.fixture(scope="session")
def kernel_path():
    """Get kernel path from environment (set by labgrid-agent)."""
    return os.environ.get("LG_KERNEL")


@pytest.fixture(scope="session")
def rootfs_path():
    """Get rootfs path from environment (set by labgrid-agent)."""
    return os.environ.get("LG_ROOTFS")


@pytest.fixture
def shell(target):
    """Get shell access to the target device."""
    return target.get_driver("ShellDriver")


@pytest.fixture
def ssh(target):
    """Get SSH access to the target device (if available)."""
    try:
        return target.get_driver("SSHDriver")
    except Exception:
        pytest.skip("SSH not available on this target")


@pytest.fixture
def qemu(target):
    """Get QEMU driver for the target."""
    return target.get_driver("QEMUDriver")
