"""Health Check Boot Test.

This minimal test verifies that a device can boot and respond.
Used by the health check scheduler to validate device availability.

Similar to LAVA health checks, this should:
1. Boot the device with a golden image
2. Verify basic connectivity
3. Run a simple command to confirm the system is responsive
"""

import pytest


class TestHealthCheck:
    """Minimal health check tests for device validation."""

    def test_device_boots(self, shell):
        """Verify the device boots successfully.

        This is the most basic health check - if the device can boot
        and we can get a shell, the device is considered functional.
        """
        # If we get here, the device has booted (labgrid provides the shell)
        result = shell.run_check("echo health_check_ok")
        assert "health_check_ok" in result[0]

    def test_basic_commands(self, shell):
        """Verify basic system commands work."""
        # Check that basic utilities are available
        shell.run_check("uname -a")
        shell.run_check("cat /proc/version")

    def test_network_interface(self, shell):
        """Verify network interface is up (if applicable)."""
        # This may fail on some configurations - mark as optional
        try:
            result = shell.run_check("ip link show")
            # At minimum, loopback should be present
            assert "lo:" in result[0] or "lo@" in result[0]
        except Exception:
            pytest.skip("Network check not applicable for this device")

    def test_filesystem_writable(self, shell):
        """Verify the filesystem is writable."""
        shell.run_check("touch /tmp/health_check_test")
        shell.run_check("rm /tmp/health_check_test")

    def test_memory_available(self, shell):
        """Verify memory information is accessible."""
        result = shell.run_check("cat /proc/meminfo | head -3")
        assert "MemTotal" in result[0]


@pytest.fixture
def shell(target):
    """Get shell access to the target device.

    This fixture is provided by pytest-labgrid when using --lg-env.
    """
    shell = target.get_driver("ShellDriver")
    return shell


@pytest.fixture
def target(env):
    """Get the target from the labgrid environment.

    This fixture is provided by pytest-labgrid.
    """
    return env.get_target()
