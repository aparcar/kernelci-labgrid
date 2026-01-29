"""Health Check Scheduler for Labgrid devices.

Similar to LAVA's health check system, this module:
1. Runs periodic health checks on devices using a golden image
2. Tracks device health state (good, bad, unknown)
3. Sends email notifications when health checks fail
4. Takes devices offline when health checks fail

Configuration example (health_checks/qemu-x86.yaml):
    device: qemu-x86
    target: qemu-x86.yaml  # labgrid target file
    frequency_hours: 24    # run every 24 hours
    golden_image:
      kernel: https://example.com/golden/kernel
      rootfs: https://example.com/golden/rootfs.img
    test_path: tests/health/test_boot.py
    notifications:
      emails:
        - admin@example.com
      on_failure: true
      on_recovery: true
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import smtplib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import aiohttp
import yaml

logger = logging.getLogger(__name__)


@dataclass
class DeviceHealth:
    """Health state of a device."""

    device: str
    state: str = "unknown"  # good, bad, unknown
    last_check: datetime | None = None
    last_success: datetime | None = None
    failure_count: int = 0
    failure_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "state": self.state,
            "last_check": self.last_check.isoformat() if self.last_check else None,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "failure_count": self.failure_count,
            "failure_reason": self.failure_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeviceHealth":
        return cls(
            device=data["device"],
            state=data.get("state", "unknown"),
            last_check=datetime.fromisoformat(data["last_check"]) if data.get("last_check") else None,
            last_success=datetime.fromisoformat(data["last_success"]) if data.get("last_success") else None,
            failure_count=data.get("failure_count", 0),
            failure_reason=data.get("failure_reason", ""),
        )


@dataclass
class HealthCheckConfig:
    """Configuration for a device health check."""

    device: str
    target: str  # labgrid target YAML filename
    frequency_hours: int = 24
    golden_image: dict[str, str] = field(default_factory=dict)
    test_path: str = ""  # specific test file, empty = all tests
    timeout: int = 3600
    notifications: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: Path) -> "HealthCheckConfig":
        """Load config from YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)

        return cls(
            device=data["device"],
            target=data.get("target", f"{data['device']}.yaml"),
            frequency_hours=data.get("frequency_hours", 24),
            golden_image=data.get("golden_image", {}),
            test_path=data.get("test_path", ""),
            timeout=data.get("timeout", 3600),
            notifications=data.get("notifications", {}),
        )


class HealthCheckScheduler:
    """Schedules and runs health checks for Labgrid devices.

    Example:
        scheduler = HealthCheckScheduler(
            health_checks_dir="/path/to/health_checks",
            targets_dir="/path/to/targets",
            tests_dir="/path/to/tests",
            state_file="/var/lib/labgrid/health_state.json",
        )
        await scheduler.run()
    """

    def __init__(
        self,
        health_checks_dir: str | Path,
        targets_dir: str | Path,
        tests_dir: str | Path,
        state_file: str | Path | None = None,
        artifact_dir: str | Path | None = None,
        *,
        smtp_host: str | None = None,
        smtp_port: int = 587,
        smtp_user: str | None = None,
        smtp_password: str | None = None,
        smtp_from: str | None = None,
        check_interval: int = 300,  # check every 5 minutes
    ):
        """Initialize the health check scheduler.

        Args:
            health_checks_dir: Directory containing health check YAML configs
            targets_dir: Directory containing labgrid target YAMLs
            tests_dir: Directory containing test files
            state_file: JSON file to persist device health state
            artifact_dir: Directory for downloaded artifacts
            smtp_host: SMTP server for notifications
            smtp_port: SMTP port
            smtp_user: SMTP username
            smtp_password: SMTP password
            smtp_from: From address for emails
            check_interval: Seconds between scheduler checks
        """
        self.health_checks_dir = Path(health_checks_dir)
        self.targets_dir = Path(targets_dir)
        self.tests_dir = Path(tests_dir)
        self.state_file = Path(state_file) if state_file else None
        self.artifact_dir = Path(artifact_dir) if artifact_dir else Path("/tmp/health-check-artifacts")
        self.check_interval = check_interval

        # SMTP configuration
        self.smtp_host = smtp_host or os.environ.get("SMTP_HOST")
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user or os.environ.get("SMTP_USER")
        self.smtp_password = smtp_password or os.environ.get("SMTP_PASSWORD")
        self.smtp_from = smtp_from or os.environ.get("SMTP_FROM", "labgrid-health@localhost")

        # Runtime state
        self._configs: dict[str, HealthCheckConfig] = {}
        self._health: dict[str, DeviceHealth] = {}
        self._running = False
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "HealthCheckScheduler":
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.stop()

    async def start(self) -> None:
        """Start the health check scheduler."""
        logger.info("Starting health check scheduler")

        self.artifact_dir.mkdir(parents=True, exist_ok=True)

        # Load health check configs
        self._load_configs()

        # Load persisted state
        self._load_state()

        # Initialize health for new devices
        for device in self._configs:
            if device not in self._health:
                self._health[device] = DeviceHealth(device=device)

        self._session = aiohttp.ClientSession()
        self._running = True

        logger.info(f"Loaded {len(self._configs)} health check config(s)")

    async def stop(self) -> None:
        """Stop the scheduler."""
        logger.info("Stopping health check scheduler")
        self._running = False
        self._save_state()
        if self._session:
            await self._session.close()
            self._session = None

    def _load_configs(self) -> None:
        """Load health check configurations from directory."""
        self._configs.clear()

        if not self.health_checks_dir.exists():
            logger.warning(f"Health checks directory not found: {self.health_checks_dir}")
            return

        for yaml_file in self.health_checks_dir.glob("*.yaml"):
            try:
                config = HealthCheckConfig.from_yaml(yaml_file)
                self._configs[config.device] = config
                logger.info(f"Loaded health check: {config.device} (every {config.frequency_hours}h)")
            except Exception as e:
                logger.error(f"Failed to load {yaml_file}: {e}")

    def _load_state(self) -> None:
        """Load persisted health state."""
        if not self.state_file or not self.state_file.exists():
            return

        try:
            with open(self.state_file) as f:
                data = json.load(f)

            for device_data in data.get("devices", []):
                health = DeviceHealth.from_dict(device_data)
                self._health[health.device] = health

            logger.info(f"Loaded state for {len(self._health)} device(s)")
        except Exception as e:
            logger.error(f"Failed to load state: {e}")

    def _save_state(self) -> None:
        """Persist health state to file."""
        if not self.state_file:
            return

        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "updated": datetime.now().isoformat(),
                "devices": [h.to_dict() for h in self._health.values()],
            }
            with open(self.state_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    async def run(self) -> None:
        """Main scheduler loop."""
        if not self._running:
            await self.start()

        logger.info(f"Scheduler running, checking every {self.check_interval}s")

        while self._running:
            try:
                await self._check_and_run()
            except Exception as e:
                logger.exception(f"Scheduler error: {e}")

            await asyncio.sleep(self.check_interval)

    async def _check_and_run(self) -> None:
        """Check which devices need health checks and run them."""
        now = datetime.now()

        for device, config in self._configs.items():
            health = self._health.get(device, DeviceHealth(device=device))

            # Check if health check is due
            if health.last_check:
                next_check = health.last_check + timedelta(hours=config.frequency_hours)
                if now < next_check:
                    continue

            logger.info(f"Running health check for {device}")
            await self._run_health_check(device, config)

    async def _run_health_check(self, device: str, config: HealthCheckConfig) -> None:
        """Run a health check for a device."""
        health = self._health.get(device, DeviceHealth(device=device))
        previous_state = health.state

        try:
            # Download golden image artifacts
            artifacts = await self._download_golden_image(device, config)

            # Run pytest
            result = await self._run_pytest(device, config, artifacts)

            # Update health state
            health.last_check = datetime.now()

            if result["success"]:
                health.state = "good"
                health.last_success = datetime.now()
                health.failure_count = 0
                health.failure_reason = ""

                logger.info(f"Health check PASSED for {device}")

                # Notify on recovery
                if previous_state == "bad" and config.notifications.get("on_recovery"):
                    await self._send_notification(
                        config, device, recovered=True,
                        message=f"Device {device} has recovered and passed health check"
                    )
            else:
                health.state = "bad"
                health.failure_count += 1
                health.failure_reason = result.get("error", "Unknown failure")

                logger.warning(f"Health check FAILED for {device}: {health.failure_reason}")

                # Notify on failure
                if config.notifications.get("on_failure", True):
                    await self._send_notification(
                        config, device, recovered=False,
                        message=f"Device {device} failed health check: {health.failure_reason}",
                        details=result.get("output", ""),
                    )

        except Exception as e:
            health.last_check = datetime.now()
            health.state = "bad"
            health.failure_count += 1
            health.failure_reason = str(e)

            logger.exception(f"Health check error for {device}")

            if config.notifications.get("on_failure", True):
                await self._send_notification(
                    config, device, recovered=False,
                    message=f"Device {device} health check error: {e}",
                )

        self._health[device] = health
        self._save_state()

    async def _download_golden_image(
        self, device: str, config: HealthCheckConfig
    ) -> dict[str, Path]:
        """Download golden image artifacts."""
        artifacts = {}
        device_dir = self.artifact_dir / device
        device_dir.mkdir(exist_ok=True)

        if not self._session or not config.golden_image:
            return artifacts

        for name, url in config.golden_image.items():
            if not url:
                continue

            local_path = device_dir / name
            logger.info(f"Downloading golden {name} for {device}...")

            try:
                async with self._session.get(url) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        local_path.write_bytes(content)
                        artifacts[name] = local_path
                        logger.info(f"  Downloaded {name}: {len(content)} bytes")
                    else:
                        logger.warning(f"  {name}: HTTP {resp.status}")
            except Exception as e:
                logger.error(f"  {name}: {e}")

        return artifacts

    async def _run_pytest(
        self, device: str, config: HealthCheckConfig, artifacts: dict[str, Path]
    ) -> dict[str, Any]:
        """Run pytest health check and return results."""
        target_yaml = self.targets_dir / config.target

        if not target_yaml.exists():
            return {"success": False, "error": f"Target not found: {target_yaml}"}

        # JUnit XML output
        junit_xml = self.artifact_dir / f"junit-health-{device}.xml"

        # Build pytest command
        cmd = [
            "pytest",
            "--lg-env", str(target_yaml),
            "--tb=short",
            "-v",
            f"--junit-xml={junit_xml}",
        ]

        # Add artifact paths as environment variables
        env = os.environ.copy()
        if "kernel" in artifacts:
            env["LG_KERNEL"] = str(artifacts["kernel"])
        if "rootfs" in artifacts:
            env["LG_ROOTFS"] = str(artifacts["rootfs"])
        if "dtb" in artifacts:
            env["LG_DTB"] = str(artifacts["dtb"])

        # Test path
        if config.test_path:
            cmd.append(str(self.tests_dir / config.test_path))
        else:
            cmd.append(str(self.tests_dir))

        logger.info(f"Running: {' '.join(cmd)}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.tests_dir.parent,
                env=env,
            )

            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=config.timeout,
                )
                output = stdout.decode() if stdout else ""
            except asyncio.TimeoutError:
                proc.kill()
                return {"success": False, "error": "Test timed out", "output": ""}

            # Parse JUnit XML
            result = self._parse_junit_xml(junit_xml)
            result["output"] = output

            # Cleanup
            if junit_xml.exists():
                junit_xml.unlink()

            return result

        except Exception as e:
            return {"success": False, "error": str(e), "output": ""}

    def _parse_junit_xml(self, junit_xml: Path) -> dict[str, Any]:
        """Parse JUnit XML output from pytest."""
        result = {
            "success": False,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
            "total": 0,
            "duration": 0.0,
            "error": "",
        }

        if not junit_xml.exists():
            result["error"] = "JUnit XML not found"
            return result

        try:
            tree = ET.parse(junit_xml)
            root = tree.getroot()

            # Handle both <testsuites> and <testsuite> as root
            if root.tag == "testsuites":
                testsuites = root.findall("testsuite")
            else:
                testsuites = [root]

            for testsuite in testsuites:
                result["total"] += int(testsuite.get("tests", 0))
                result["errors"] += int(testsuite.get("errors", 0))
                result["failed"] += int(testsuite.get("failures", 0))
                result["skipped"] += int(testsuite.get("skipped", 0))
                result["duration"] += float(testsuite.get("time", 0))

            result["passed"] = result["total"] - result["failed"] - result["errors"] - result["skipped"]
            result["success"] = result["failed"] == 0 and result["errors"] == 0

            if not result["success"]:
                # Extract first failure message
                for testsuite in testsuites:
                    for testcase in testsuite.findall("testcase"):
                        failure = testcase.find("failure")
                        if failure is not None:
                            result["error"] = failure.get("message", "Test failed")
                            break
                        error = testcase.find("error")
                        if error is not None:
                            result["error"] = error.get("message", "Test error")
                            break

        except ET.ParseError as e:
            result["error"] = f"Failed to parse JUnit XML: {e}"

        return result

    async def _send_notification(
        self,
        config: HealthCheckConfig,
        device: str,
        recovered: bool,
        message: str,
        details: str = "",
    ) -> None:
        """Send email notification."""
        emails = config.notifications.get("emails", [])
        if not emails:
            logger.debug("No notification emails configured")
            return

        if not self.smtp_host:
            logger.warning("SMTP not configured, skipping notification")
            return

        subject = f"[Labgrid] Device {device} {'RECOVERED' if recovered else 'FAILED'}"

        body = f"""
Health Check Notification
========================

Device: {device}
Status: {'RECOVERED' if recovered else 'FAILED'}
Time: {datetime.now().isoformat()}

{message}
"""

        if details:
            body += f"""
Details:
--------
{details[:2000]}
"""

        try:
            msg = MIMEMultipart()
            msg["From"] = self.smtp_from
            msg["To"] = ", ".join(emails)
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain"))

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                if self.smtp_user and self.smtp_password:
                    server.starttls()
                    server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.smtp_from, emails, msg.as_string())

            logger.info(f"Sent notification to {len(emails)} recipient(s)")

        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

    def get_device_health(self, device: str) -> DeviceHealth | None:
        """Get health state for a device."""
        return self._health.get(device)

    def get_all_health(self) -> dict[str, DeviceHealth]:
        """Get health states for all devices."""
        return dict(self._health)

    def is_device_healthy(self, device: str) -> bool:
        """Check if a device is healthy (good or unknown state)."""
        health = self._health.get(device)
        if not health:
            return True  # Unknown devices are assumed healthy
        return health.state != "bad"

    def set_device_health(self, device: str, state: str) -> None:
        """Manually set device health state (admin override)."""
        if device not in self._health:
            self._health[device] = DeviceHealth(device=device)

        self._health[device].state = state
        if state == "good":
            self._health[device].failure_count = 0
            self._health[device].failure_reason = ""

        self._save_state()
        logger.info(f"Device {device} health set to {state}")

    async def run_health_check_now(self, device: str) -> dict[str, Any]:
        """Manually trigger a health check for a device."""
        if device not in self._configs:
            return {"success": False, "error": f"No health check config for {device}"}

        await self._run_health_check(device, self._configs[device])
        health = self._health.get(device)

        return {
            "success": health.state == "good" if health else False,
            "state": health.state if health else "unknown",
            "failure_reason": health.failure_reason if health else "",
        }


# ==================== CLI ====================

def main() -> None:
    """CLI entry point for health check scheduler."""
    import argparse
    import signal

    parser = argparse.ArgumentParser(
        description="Labgrid Health Check Scheduler"
    )
    parser.add_argument(
        "--health-checks-dir", "-c",
        required=True,
        help="Directory with health check YAML configs",
    )
    parser.add_argument(
        "--targets-dir", "-t",
        required=True,
        help="Directory with labgrid target YAMLs",
    )
    parser.add_argument(
        "--tests-dir", "-T",
        required=True,
        help="Directory with test files",
    )
    parser.add_argument(
        "--state-file", "-s",
        help="JSON file to persist health state",
    )
    parser.add_argument(
        "--artifact-dir", "-a",
        help="Directory for downloaded artifacts",
    )
    parser.add_argument(
        "--check-interval", "-i",
        type=int,
        default=300,
        help="Interval between checks (seconds)",
    )
    parser.add_argument(
        "--smtp-host",
        help="SMTP server (or SMTP_HOST env)",
    )
    parser.add_argument(
        "--smtp-port",
        type=int,
        default=587,
        help="SMTP port",
    )
    parser.add_argument(
        "--smtp-user",
        help="SMTP username (or SMTP_USER env)",
    )
    parser.add_argument(
        "--smtp-password",
        help="SMTP password (or SMTP_PASSWORD env)",
    )
    parser.add_argument(
        "--smtp-from",
        help="From address (or SMTP_FROM env)",
    )
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="Debug logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    scheduler = HealthCheckScheduler(
        health_checks_dir=args.health_checks_dir,
        targets_dir=args.targets_dir,
        tests_dir=args.tests_dir,
        state_file=args.state_file,
        artifact_dir=args.artifact_dir,
        check_interval=args.check_interval,
        smtp_host=args.smtp_host,
        smtp_port=args.smtp_port,
        smtp_user=args.smtp_user,
        smtp_password=args.smtp_password,
        smtp_from=args.smtp_from,
    )

    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(scheduler.stop()))

    try:
        loop.run_until_complete(scheduler.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
