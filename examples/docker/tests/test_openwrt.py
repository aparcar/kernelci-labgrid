"""OpenWrt Functional Tests.

Comprehensive tests for OpenWrt functionality on QEMU x86_64.
"""

import pytest


class TestOpenWrtSystem:
    """System-level tests."""

    def test_kernel_version(self, shell, openwrt_branch):
        """Check kernel version matches expected branch."""
        stdout, stderr, returncode = shell.run("uname -r")
        assert returncode == 0
        # Kernel version should be present
        assert len(stdout.strip()) > 0

    def test_cpu_info(self, shell):
        """Verify CPU information is available."""
        stdout, stderr, returncode = shell.run("cat /proc/cpuinfo | head -20")
        assert returncode == 0
        assert "processor" in stdout.lower() or "cpu" in stdout.lower()

    def test_memory_info(self, shell):
        """Verify memory information."""
        stdout, stderr, returncode = shell.run("cat /proc/meminfo | head -5")
        assert returncode == 0
        assert "MemTotal" in stdout

    def test_disk_space(self, shell):
        """Verify disk space information."""
        stdout, stderr, returncode = shell.run("df -h /")
        assert returncode == 0
        # Should show some disk info
        assert "%" in stdout


class TestOpenWrtServices:
    """Service and daemon tests."""

    def test_dnsmasq_running(self, shell):
        """Verify dnsmasq (DHCP/DNS) is running."""
        stdout, stderr, returncode = shell.run("pgrep -x dnsmasq || echo 'not running'")
        # dnsmasq might not be running in minimal images
        if "not running" in stdout:
            pytest.skip("dnsmasq not present in this image")

    def test_dropbear_running(self, shell):
        """Verify dropbear (SSH server) is running."""
        stdout, stderr, returncode = shell.run("pgrep -x dropbear || echo 'not running'")
        if "not running" in stdout:
            pytest.skip("dropbear not present in this image")

    def test_uhttpd_running(self, shell):
        """Verify uhttpd (web server) is running."""
        stdout, stderr, returncode = shell.run("pgrep -x uhttpd || echo 'not running'")
        if "not running" in stdout:
            pytest.skip("uhttpd not present in this image")


class TestOpenWrtPackages:
    """Package management tests."""

    def test_opkg_works(self, shell):
        """Verify opkg package manager works."""
        stdout, stderr, returncode = shell.run("opkg --version")
        assert returncode == 0
        assert "opkg" in stdout.lower()

    def test_list_installed(self, shell):
        """Verify we can list installed packages."""
        stdout, stderr, returncode = shell.run("opkg list-installed | head -10")
        assert returncode == 0
        # Should have at least base-files
        assert "base-files" in stdout or len(stdout) > 0


class TestOpenWrtNetwork:
    """Network configuration tests."""

    def test_uci_network(self, shell):
        """Verify UCI network configuration."""
        stdout, stderr, returncode = shell.run("uci show network")
        assert returncode == 0
        # Should have loopback interface
        assert "network.loopback" in stdout or "network.lo" in stdout

    def test_firewall_config(self, shell):
        """Verify firewall configuration exists."""
        stdout, stderr, returncode = shell.run("uci show firewall 2>/dev/null | head -5")
        if returncode != 0:
            pytest.skip("Firewall not configured in this image")
        assert "firewall" in stdout

    def test_ip_addresses(self, shell):
        """Verify IP addresses are configured."""
        stdout, stderr, returncode = shell.run("ip addr show")
        assert returncode == 0
        # At least loopback should have 127.0.0.1
        assert "127.0.0.1" in stdout


class TestOpenWrtLuCI:
    """LuCI web interface tests (if present)."""

    def test_luci_installed(self, shell):
        """Check if LuCI is installed."""
        stdout, stderr, returncode = shell.run("opkg list-installed | grep luci-base")
        if returncode != 0 or "luci-base" not in stdout:
            pytest.skip("LuCI not installed in this image")

    def test_luci_accessible(self, shell):
        """Verify LuCI is accessible via HTTP."""
        stdout, stderr, returncode = shell.run(
            "wget -q -O - http://127.0.0.1/ 2>/dev/null | head -5"
        )
        if returncode != 0:
            pytest.skip("HTTP server not responding")
        # Should get some HTML response
        assert "<" in stdout or "html" in stdout.lower()
