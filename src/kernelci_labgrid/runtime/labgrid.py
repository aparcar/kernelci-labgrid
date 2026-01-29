"""Labgrid Runtime for KernelCI.

This module provides the main runtime class for executing tests on
Labgrid-managed hardware via KernelCI's Maestro pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import paramiko
from jinja2 import Environment, PackageLoader, select_autoescape

from kernelci_labgrid.runtime.labgrid_client import (
    LabgridClient,
    LabgridClientError,
    LabgridPlaceError,
    Place,
)

logger = logging.getLogger(__name__)


@dataclass
class PlaceMapping:
    """Maps KernelCI platforms to Labgrid places."""

    place: str
    platform: str
    compatible: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class TestJob:
    """Represents a test job to be executed on Labgrid."""

    node_id: str
    name: str
    platform: str
    kernel_revision: str
    kernel_url: str
    dtb_url: str | None = None
    rootfs_url: str | None = None
    modules_url: str | None = None
    test_suite: str = "baseline"
    test_config: dict[str, Any] = field(default_factory=dict)
    timeout: int = 3600


@dataclass
class TestResult:
    """Result from a test execution."""

    job: TestJob
    success: bool
    result: str  # pass, fail, skip, incomplete
    log: str = ""
    test_cases: list[dict[str, Any]] = field(default_factory=list)
    duration: float = 0.0
    error: str | None = None


class LabgridRuntimeError(Exception):
    """Base exception for Labgrid runtime errors."""

    pass


class LabgridRuntime:
    """Labgrid runtime for KernelCI test execution.

    This class implements the runtime interface for executing tests on
    hardware managed by Labgrid coordinators.

    Example:
        config = LabgridConfig.from_file("config/labgrid-runtime.yaml")
        runtime = LabgridRuntime(config)

        async with runtime:
            job = TestJob(
                node_id="abc123",
                name="baseline-arm64",
                platform="bcm2711-rpi-4-b",
                kernel_revision="v6.8-rc1",
                kernel_url="https://storage.kernelci.org/...",
            )
            result = await runtime.submit(job)
            print(f"Test result: {result.result}")
    """

    def __init__(
        self,
        config: dict[str, Any],
        *,
        client: LabgridClient | None = None,
    ):
        """Initialize Labgrid runtime.

        Args:
            config: Runtime configuration dictionary
            client: Optional pre-configured LabgridClient
        """
        self.config = config
        self.coordinator_address = config.get("coordinator_address", "localhost:20408")
        self.mode = config.get("mode", "push")
        self.place_mappings = self._load_place_mappings(config.get("place_mapping", []))
        self.ssh_config = config.get("ssh", {})
        self.storage_config = config.get("storage", {})

        self._client = client
        self._jinja_env = Environment(
            loader=PackageLoader("kernelci_labgrid", "templates"),
            autoescape=select_autoescape(),
        )

    def _load_place_mappings(
        self, mappings: list[dict[str, Any]]
    ) -> dict[str, PlaceMapping]:
        """Load place mappings from config."""
        result = {}
        for m in mappings:
            mapping = PlaceMapping(
                place=m["place"],
                platform=m["platform"],
                compatible=m.get("compatible", []),
                tags=m.get("tags", {}),
            )
            result[mapping.platform] = mapping
        return result

    async def __aenter__(self) -> "LabgridRuntime":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    async def connect(self) -> None:
        """Connect to Labgrid coordinator."""
        if self._client is None:
            self._client = LabgridClient(
                self.coordinator_address,
                secure=self.config.get("secure", False),
            )
        await self._client.connect()
        logger.info(f"Connected to Labgrid coordinator: {self.coordinator_address}")

    async def close(self) -> None:
        """Close connection to coordinator."""
        if self._client:
            await self._client.close()
            logger.info("Disconnected from Labgrid coordinator")

    @property
    def client(self) -> LabgridClient:
        """Get the Labgrid client instance."""
        if self._client is None:
            raise LabgridRuntimeError("Runtime not connected. Call connect() first.")
        return self._client

    # ============ Platform/Place Resolution ============

    def get_place_for_platform(self, platform: str) -> str | None:
        """Get Labgrid place name for a KernelCI platform.

        Args:
            platform: KernelCI platform identifier (e.g., "bcm2711-rpi-4-b")

        Returns:
            Labgrid place name, or None if no mapping exists
        """
        mapping = self.place_mappings.get(platform)
        return mapping.place if mapping else None

    async def find_place_for_job(self, job: TestJob) -> Place | None:
        """Find an available place for a test job.

        Args:
            job: Test job to find a place for

        Returns:
            Available Place object, or None if none found
        """
        mapping = self.place_mappings.get(job.platform)
        if mapping:
            # Try the explicitly mapped place first
            try:
                place = await self.client.get_place(mapping.place)
                if not place.is_acquired:
                    return place
            except LabgridPlaceError:
                pass

        # Fall back to searching by compatible strings
        if mapping and mapping.compatible:
            return await self.client.find_available_place(
                compatible=mapping.compatible
            )

        # Search by platform tag
        return await self.client.find_available_place(platform=job.platform)

    # ============ Job Submission ============

    async def submit(self, job: TestJob) -> TestResult:
        """Submit and execute a test job.

        This is the main entry point for test execution. It:
        1. Finds an available place for the job
        2. Creates a reservation
        3. Acquires the place
        4. Deploys artifacts and runs tests
        5. Collects results
        6. Releases the place

        Args:
            job: Test job to execute

        Returns:
            TestResult with execution outcome
        """
        logger.info(f"Submitting job {job.name} for platform {job.platform}")

        place = await self.find_place_for_job(job)
        if not place:
            return TestResult(
                job=job,
                success=False,
                result="skip",
                error=f"No available place for platform {job.platform}",
            )

        # Create reservation and wait for allocation
        try:
            reservation = await self.client.create_reservation(
                filters={"name": place.name}
            )
            await self.client.poll_reservation_until_allocated(
                reservation.token,
                timeout=300.0,
            )
        except LabgridClientError as e:
            return TestResult(
                job=job,
                success=False,
                result="incomplete",
                error=f"Reservation failed: {e}",
            )

        # Acquire the place
        try:
            await self.client.acquire_place(place.name)
        except LabgridPlaceError as e:
            await self.client.cancel_reservation(reservation.token)
            return TestResult(
                job=job,
                success=False,
                result="incomplete",
                error=f"Failed to acquire place: {e}",
            )

        # Execute the test
        try:
            result = await self._execute_test(job, place)
            return result
        finally:
            # Always release the place
            try:
                await self.client.release_place(place.name)
                await self.client.cancel_reservation(reservation.token)
            except Exception as e:
                logger.error(f"Error releasing place {place.name}: {e}")

    async def _execute_test(self, job: TestJob, place: Place) -> TestResult:
        """Execute test on acquired place.

        Args:
            job: Test job
            place: Acquired place

        Returns:
            TestResult from execution
        """
        import time

        start_time = time.monotonic()

        try:
            # Deploy artifacts to the board
            await self._deploy_artifacts(job, place)

            # Boot the board
            await self._boot_board(job, place)

            # Run tests based on test suite
            test_cases = await self._run_tests(job, place)

            duration = time.monotonic() - start_time

            # Determine overall result from test cases
            failed = any(tc.get("result") == "fail" for tc in test_cases)
            result = "fail" if failed else "pass"

            return TestResult(
                job=job,
                success=not failed,
                result=result,
                test_cases=test_cases,
                duration=duration,
            )

        except asyncio.TimeoutError:
            return TestResult(
                job=job,
                success=False,
                result="incomplete",
                error="Test execution timed out",
                duration=time.monotonic() - start_time,
            )
        except Exception as e:
            logger.exception(f"Test execution failed for {job.name}")
            return TestResult(
                job=job,
                success=False,
                result="incomplete",
                error=str(e),
                duration=time.monotonic() - start_time,
            )

    async def _deploy_artifacts(self, job: TestJob, place: Place) -> None:
        """Deploy kernel and test artifacts to storage.

        Args:
            job: Test job containing artifact URLs
            place: Target place
        """
        logger.info(f"Deploying artifacts for {job.name} to {place.name}")

        storage_type = self.storage_config.get("type", "ssh")

        if storage_type == "ssh":
            await self._deploy_via_ssh(job)
        elif storage_type == "http":
            # For HTTP storage, artifacts are already accessible
            logger.info("Using HTTP storage - artifacts accessible via URL")
        elif storage_type == "nfs":
            await self._deploy_via_nfs(job)
        else:
            raise LabgridRuntimeError(f"Unknown storage type: {storage_type}")

    async def _deploy_via_ssh(self, job: TestJob) -> None:
        """Deploy artifacts via SSH/SCP."""
        storage_host = self.storage_config.get("host")
        storage_path = self.storage_config.get("path", "/srv/labgrid/artifacts")

        if not storage_host:
            logger.warning("No storage host configured, skipping SSH deployment")
            return

        # Create job-specific directory
        job_path = f"{storage_path}/{job.node_id}"

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            key_file = self.storage_config.get("key_file")
            ssh.connect(
                storage_host,
                username=self.storage_config.get("user", "root"),
                key_filename=key_file,
            )

            # Create directory
            ssh.exec_command(f"mkdir -p {job_path}")

            # Download artifacts to storage (kernel, dtb, rootfs)
            artifact_urls = [
                ("kernel", job.kernel_url),
                ("dtb", job.dtb_url),
                ("rootfs", job.rootfs_url),
                ("modules", job.modules_url),
            ]

            for name, url in artifact_urls:
                if url:
                    cmd = f"curl -sL -o {job_path}/{name} '{url}'"
                    ssh.exec_command(cmd)
                    logger.debug(f"Downloaded {name} to {job_path}")

        finally:
            ssh.close()

    async def _deploy_via_nfs(self, job: TestJob) -> None:
        """Deploy artifacts via NFS mount."""
        nfs_path = self.storage_config.get("nfs_path")
        if not nfs_path:
            raise LabgridRuntimeError("NFS path not configured")

        job_path = Path(nfs_path) / job.node_id
        job_path.mkdir(parents=True, exist_ok=True)

        # Download artifacts locally to NFS share
        import aiohttp

        async with aiohttp.ClientSession() as session:
            for name, url in [
                ("kernel", job.kernel_url),
                ("dtb", job.dtb_url),
                ("rootfs", job.rootfs_url),
            ]:
                if url:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            (job_path / name).write_bytes(await resp.read())

    async def _boot_board(self, job: TestJob, place: Place) -> None:
        """Boot the board with the deployed kernel.

        Args:
            job: Test job
            place: Target place
        """
        logger.info(f"Booting {place.name} with kernel {job.kernel_revision}")

        # Get SSH connection info from place tags or config
        ssh_host = place.tags.get("ssh_host") or self.ssh_config.get("proxy_host")
        ssh_user = place.tags.get("ssh_user") or self.ssh_config.get("proxy_user", "root")
        ssh_key = self.ssh_config.get("key_file")

        if not ssh_host:
            logger.warning("No SSH host configured, assuming board is pre-booted")
            return

        # Power cycle and boot
        # This is a simplified implementation - real implementation would:
        # 1. Use Labgrid's power control
        # 2. Configure bootloader (U-Boot) via serial
        # 3. Monitor boot process via serial console

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            ssh.connect(ssh_host, username=ssh_user, key_filename=ssh_key)

            # Wait for board to boot (simplified)
            # Real implementation would monitor serial console
            await asyncio.sleep(30)

            # Verify board is accessible
            stdin, stdout, stderr = ssh.exec_command("uname -r", timeout=30)
            kernel_version = stdout.read().decode().strip()
            logger.info(f"Board booted with kernel: {kernel_version}")

        finally:
            ssh.close()

    async def _run_tests(self, job: TestJob, place: Place) -> list[dict[str, Any]]:
        """Run tests on the booted board.

        Args:
            job: Test job
            place: Target place

        Returns:
            List of test case results
        """
        logger.info(f"Running {job.test_suite} tests on {place.name}")

        test_cases = []

        # Get SSH connection to board
        ssh_host = place.tags.get("ssh_host") or self.ssh_config.get("proxy_host")
        ssh_user = place.tags.get("ssh_user") or self.ssh_config.get("proxy_user", "root")
        ssh_key = self.ssh_config.get("key_file")

        if not ssh_host:
            return [{"name": "connection", "result": "skip", "log": "No SSH access"}]

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            ssh.connect(ssh_host, username=ssh_user, key_filename=ssh_key)

            if job.test_suite == "baseline":
                test_cases = await self._run_baseline_tests(ssh, job)
            elif job.test_suite == "kselftest":
                test_cases = await self._run_kselftest(ssh, job)
            else:
                test_cases = await self._run_custom_tests(ssh, job)

        finally:
            ssh.close()

        return test_cases

    async def _run_baseline_tests(
        self, ssh: paramiko.SSHClient, job: TestJob
    ) -> list[dict[str, Any]]:
        """Run baseline boot tests."""
        test_cases = []

        # Test: login
        stdin, stdout, stderr = ssh.exec_command("whoami", timeout=30)
        exit_code = stdout.channel.recv_exit_status()
        test_cases.append({
            "name": "login",
            "result": "pass" if exit_code == 0 else "fail",
            "log": stdout.read().decode(),
        })

        # Test: kernel messages (no critical errors)
        stdin, stdout, stderr = ssh.exec_command(
            "dmesg | grep -i -E '(error|panic|oops|bug)' | head -20",
            timeout=60,
        )
        dmesg_output = stdout.read().decode()
        exit_code = stdout.channel.recv_exit_status()
        # exit_code 0 means matches found (errors exist), 1 means no matches (good)
        test_cases.append({
            "name": "kernelmsg",
            "result": "pass" if exit_code == 1 else "fail",
            "log": dmesg_output if dmesg_output else "No critical kernel messages",
        })

        # Test: kernel version matches
        stdin, stdout, stderr = ssh.exec_command("uname -r", timeout=30)
        kernel_version = stdout.read().decode().strip()
        matches = job.kernel_revision in kernel_version
        test_cases.append({
            "name": "kernel_version",
            "result": "pass" if matches else "fail",
            "log": f"Expected: {job.kernel_revision}, Got: {kernel_version}",
        })

        return test_cases

    async def _run_kselftest(
        self, ssh: paramiko.SSHClient, job: TestJob
    ) -> list[dict[str, Any]]:
        """Run kernel self-tests."""
        test_cases = []

        test_name = job.test_config.get("test_name", "")
        timeout = job.test_config.get("timeout", 300)

        if test_name:
            cmd = f"cd /opt/kselftest && ./run_kselftest.sh -t {test_name}"
        else:
            cmd = "cd /opt/kselftest && ./run_kselftest.sh"

        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
        output = stdout.read().decode()
        exit_code = stdout.channel.recv_exit_status()

        # Parse TAP output
        test_cases = self._parse_tap_output(output)

        if not test_cases:
            test_cases.append({
                "name": test_name or "kselftest",
                "result": "pass" if exit_code == 0 else "fail",
                "log": output,
            })

        return test_cases

    async def _run_custom_tests(
        self, ssh: paramiko.SSHClient, job: TestJob
    ) -> list[dict[str, Any]]:
        """Run custom test commands."""
        test_cases = []

        commands = job.test_config.get("commands", [])
        for i, cmd_config in enumerate(commands):
            if isinstance(cmd_config, str):
                name = f"test_{i}"
                cmd = cmd_config
                timeout = 60
            else:
                name = cmd_config.get("name", f"test_{i}")
                cmd = cmd_config.get("command")
                timeout = cmd_config.get("timeout", 60)

            stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
            output = stdout.read().decode()
            exit_code = stdout.channel.recv_exit_status()

            test_cases.append({
                "name": name,
                "result": "pass" if exit_code == 0 else "fail",
                "log": output,
            })

        return test_cases

    def _parse_tap_output(self, output: str) -> list[dict[str, Any]]:
        """Parse TAP (Test Anything Protocol) output.

        Args:
            output: TAP-formatted test output

        Returns:
            List of test case results
        """
        test_cases = []

        for line in output.split("\n"):
            line = line.strip()

            if line.startswith("ok "):
                # ok 1 - test name
                parts = line.split(" - ", 1)
                name = parts[1] if len(parts) > 1 else line[3:].strip()
                test_cases.append({"name": name, "result": "pass", "log": ""})

            elif line.startswith("not ok "):
                # not ok 2 - test name
                parts = line.split(" - ", 1)
                name = parts[1] if len(parts) > 1 else line[7:].strip()
                test_cases.append({"name": name, "result": "fail", "log": ""})

            elif line.startswith("# SKIP"):
                # Handle skip annotation on previous test
                if test_cases:
                    test_cases[-1]["result"] = "skip"
                    test_cases[-1]["log"] = line

        return test_cases

    # ============ Template Generation ============

    def generate_test_script(
        self, job: TestJob, place: Place
    ) -> str:
        """Generate a test script from templates.

        Args:
            job: Test job
            place: Target place

        Returns:
            Generated test script content
        """
        template = self._jinja_env.get_template("labgrid_test.jinja2")

        return template.render(
            job_name=job.name,
            node_id=job.node_id,
            target_name=place.name,
            kernel_version=job.kernel_revision,
            test_suite=job.test_suite,
            timeout=job.timeout,
            compatible=self.place_mappings.get(job.platform, PlaceMapping("", "")).compatible,
            **job.test_config,
        )

    # ============ Utility Methods ============

    async def get_available_platforms(self) -> list[str]:
        """Get list of available platforms.

        Returns:
            List of platform names that have available places
        """
        platforms = []
        places = await self.client.get_places()

        for place in places:
            if not place.is_acquired:
                # Check if this place maps to a platform
                for platform, mapping in self.place_mappings.items():
                    if mapping.place == place.name:
                        platforms.append(platform)
                        break
                else:
                    # Use platform tag if no explicit mapping
                    if "platform" in place.tags:
                        platforms.append(place.tags["platform"])

        return list(set(platforms))

    async def health_check(self) -> dict[str, Any]:
        """Check runtime health status.

        Returns:
            Health status dictionary
        """
        try:
            places = await self.client.get_places()
            available = sum(1 for p in places if not p.is_acquired)
            acquired = len(places) - available

            return {
                "status": "healthy",
                "coordinator": self.coordinator_address,
                "total_places": len(places),
                "available_places": available,
                "acquired_places": acquired,
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "coordinator": self.coordinator_address,
                "error": str(e),
            }
