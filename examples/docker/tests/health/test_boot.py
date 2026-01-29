"""Health Check: OpenWrt Boot Test.

Minimal tests to verify the QEMU device boots and is responsive.
Used by the health check scheduler to validate device availability.
"""

import pytest


class TestOpenWrtBoot:
    """Basic boot and health verification tests."""

    def test_device_boots(self, shell):
        """Verify the device boots and we can get a shell.

        If we reach this point, the device has successfully:
        1. Started QEMU
        2. Booted the kernel
        3. Mounted rootfs
        4. Started init
        5. Presented a login prompt
        """
        # Run a simple command to verify shell works
        stdout, stderr, returncode = shell.run("echo health_check_ok")
        assert returncode == 0
        assert "health_check_ok" in stdout

    def test_openwrt_version(self, shell):
        """Verify OpenWrt version information is available."""
        stdout, stderr, returncode = shell.run("cat /etc/openwrt_release")
        assert returncode == 0
        assert "DISTRIB_ID" in stdout
        assert "OpenWrt" in stdout

    def test_uname(self, shell):
        """Verify kernel is running."""
        stdout, stderr, returncode = shell.run("uname -a")
        assert returncode == 0
        assert "Linux" in stdout

    def test_network_interfaces(self, shell):
        """Verify network interfaces are present."""
        stdout, stderr, returncode = shell.run("ip link show")
        assert returncode == 0
        # At minimum, loopback should be present
        assert "lo" in stdout

    def test_filesystem_writable(self, shell):
        """Verify we can write to /tmp."""
        stdout, stderr, returncode = shell.run(
            "echo test > /tmp/health_test && cat /tmp/health_test && rm /tmp/health_test"
        )
        assert returncode == 0
        assert "test" in stdout

    def test_procd_running(self, shell):
        """Verify procd (OpenWrt init system) is running."""
        stdout, stderr, returncode = shell.run("pgrep -x procd")
        assert returncode == 0, "procd is not running"

    def test_ubus_available(self, shell):
        """Verify ubus (OpenWrt IPC) is available."""
        stdout, stderr, returncode = shell.run("ubus list")
        assert returncode == 0
        # System object should always be present
        assert "system" in stdout
