"""Pull Mode Agent for KernelCI Labgrid Integration.

This agent runs locally in Labgrid labs and:
1. Polls KernelCI API for pending test jobs
2. Downloads kernel/rootfs artifacts
3. Runs pytest with labgrid (your existing tests)
4. Reports results back to KernelCI API

No modifications needed to KernelCI infrastructure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class TestResult:
    """Result from pytest execution."""

    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    total: int = 0
    duration: float = 0.0
    output: str = ""
    test_cases: list[dict[str, Any]] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.failed == 0 and self.errors == 0

    @property
    def result(self) -> str:
        if self.errors > 0:
            return "incomplete"
        if self.failed > 0:
            return "fail"
        if self.passed == 0 and self.skipped > 0:
            return "skip"
        return "pass"


class LabgridPullAgent:
    """Pull mode agent - runs pytest locally, reports to KernelCI.

    Example:
        agent = LabgridPullAgent(
            api_url="https://api.kernelci.org",
            api_token="your-token",
            lab_name="my-lab",
            tests_dir="/path/to/openwrt-tests",
            targets_dir="/path/to/openwrt-tests/targets",
        )
        await agent.run()
    """

    def __init__(
        self,
        api_url: str,
        api_token: str,
        lab_name: str,
        tests_dir: str | Path,
        targets_dir: str | Path | None = None,
        *,
        poll_interval: int = 30,
        artifact_dir: str | Path | None = None,
        default_timeout: int = 3600,
    ):
        """Initialize the pull agent.

        Args:
            api_url: KernelCI API URL
            api_token: API authentication token
            lab_name: Name of this lab (for job filtering)
            tests_dir: Path to test directory (e.g., openwrt-tests/tests)
            targets_dir: Path to labgrid target YAMLs (e.g., openwrt-tests/targets)
            poll_interval: Seconds between API polls
            artifact_dir: Directory for downloaded artifacts
            default_timeout: Default test timeout in seconds
        """
        self.api_url = api_url.rstrip("/")
        self.api_token = api_token
        self.lab_name = lab_name
        self.tests_dir = Path(tests_dir)
        self.targets_dir = Path(targets_dir) if targets_dir else self.tests_dir.parent / "targets"
        self.poll_interval = poll_interval
        self.artifact_dir = Path(artifact_dir or tempfile.mkdtemp(prefix="kci-labgrid-"))
        self.default_timeout = default_timeout

        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._current_jobs: set[str] = set()

    async def __aenter__(self) -> "LabgridPullAgent":
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.stop()

    async def start(self) -> None:
        """Start the agent."""
        logger.info(f"Starting pull agent for lab: {self.lab_name}")
        logger.info(f"Tests directory: {self.tests_dir}")
        logger.info(f"Targets directory: {self.targets_dir}")

        self.artifact_dir.mkdir(parents=True, exist_ok=True)

        self._session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
            }
        )

        self._running = True
        logger.info("Pull agent started")

    async def stop(self) -> None:
        """Stop the agent."""
        logger.info("Stopping pull agent")
        self._running = False

        # Wait for current jobs
        if self._current_jobs:
            logger.info(f"Waiting for {len(self._current_jobs)} jobs...")
            for _ in range(60):
                if not self._current_jobs:
                    break
                await asyncio.sleep(1)

        if self._session:
            await self._session.close()
            self._session = None

        logger.info("Pull agent stopped")

    async def run(self) -> None:
        """Main loop - poll and execute jobs."""
        if not self._running:
            await self.start()

        logger.info(f"Polling every {self.poll_interval}s")

        while self._running:
            try:
                await self._poll_and_execute()
            except Exception as e:
                logger.exception(f"Poll error: {e}")

            await asyncio.sleep(self.poll_interval)

    # ==================== API Methods ====================

    async def _api_get(self, endpoint: str, **params: Any) -> dict[str, Any]:
        """GET request to KernelCI API."""
        if not self._session:
            raise RuntimeError("Session not initialized")

        url = f"{self.api_url}{endpoint}"
        async with self._session.get(url, params=params) as resp:
            if resp.status != 200:
                raise RuntimeError(f"API error: {resp.status}")
            return await resp.json()

    async def _api_put(self, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
        """PUT request to KernelCI API."""
        if not self._session:
            raise RuntimeError("Session not initialized")

        url = f"{self.api_url}{endpoint}"
        async with self._session.put(url, json=data) as resp:
            if resp.status not in (200, 201):
                raise RuntimeError(f"API error: {resp.status}")
            return await resp.json()

    async def _api_post(self, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
        """POST request to KernelCI API."""
        if not self._session:
            raise RuntimeError("Session not initialized")

        url = f"{self.api_url}{endpoint}"
        async with self._session.post(url, json=data) as resp:
            if resp.status not in (200, 201):
                raise RuntimeError(f"API error: {resp.status}")
            return await resp.json()

    # ==================== Job Processing ====================

    async def _poll_and_execute(self) -> None:
        """Poll for pending jobs and execute them."""
        try:
            # Query for pending jobs for this lab
            response = await self._api_get(
                "/api/latest/nodes",
                state="pending",
                **{"data.lab": self.lab_name},
            )
            pending = response.get("nodes", [])
        except Exception as e:
            logger.error(f"Failed to get jobs: {e}")
            return

        if not pending:
            logger.debug("No pending jobs")
            return

        logger.info(f"Found {len(pending)} pending job(s)")

        for job in pending:
            node_id = job.get("id")
            if not node_id or node_id in self._current_jobs:
                continue

            # Claim the job
            try:
                await self._api_put(
                    f"/api/latest/nodes/{node_id}",
                    {"state": "running"},
                )
                self._current_jobs.add(node_id)
                asyncio.create_task(self._execute_job(job))
            except Exception as e:
                logger.warning(f"Failed to claim job {node_id}: {e}")

    async def _execute_job(self, job: dict[str, Any]) -> None:
        """Execute a test job."""
        node_id = job["id"]
        job_name = job.get("name", "unknown")
        job_data = job.get("data", {})

        logger.info(f"Executing: {job_name} ({node_id})")

        try:
            # 1. Download artifacts
            artifacts = await self._download_artifacts(node_id, job_data)

            # 2. Determine target environment
            platform = job_data.get("platform", "")
            target_yaml = self._find_target_yaml(platform)

            if not target_yaml:
                raise RuntimeError(f"No target YAML found for platform: {platform}")

            # 3. Run pytest
            result = await self._run_pytest(
                target_yaml=target_yaml,
                artifacts=artifacts,
                job_data=job_data,
            )

            # 4. Report results
            await self._report_results(node_id, result)

            logger.info(f"Completed {job_name}: {result.result} "
                       f"({result.passed} passed, {result.failed} failed)")

        except Exception as e:
            logger.exception(f"Job failed: {job_name}")
            await self._report_failure(node_id, str(e))

        finally:
            self._current_jobs.discard(node_id)
            self._cleanup_artifacts(node_id)

    # ==================== Artifact Management ====================

    async def _download_artifacts(
        self,
        node_id: str,
        job_data: dict[str, Any],
    ) -> dict[str, Path]:
        """Download job artifacts."""
        artifacts = job_data.get("artifacts", {})
        local = {}

        job_dir = self.artifact_dir / node_id
        job_dir.mkdir(exist_ok=True)

        if not self._session:
            return local

        for name, url in artifacts.items():
            if not url:
                continue

            local_path = job_dir / name
            logger.info(f"Downloading {name}...")

            try:
                async with self._session.get(url) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        local_path.write_bytes(content)
                        local[name] = local_path
                        logger.info(f"  {name}: {len(content)} bytes")
                    else:
                        logger.warning(f"  {name}: HTTP {resp.status}")
            except Exception as e:
                logger.error(f"  {name}: {e}")

        return local

    def _cleanup_artifacts(self, node_id: str) -> None:
        """Clean up downloaded artifacts."""
        job_dir = self.artifact_dir / node_id
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)

    # ==================== Test Execution ====================

    def _find_target_yaml(self, platform: str) -> Path | None:
        """Find labgrid target YAML for platform."""
        # Try exact match first
        candidates = [
            self.targets_dir / f"{platform}.yaml",
            self.targets_dir / f"{platform}.yml",
            # Common variations
            self.targets_dir / f"qemu-{platform}.yaml",
            self.targets_dir / f"{platform.replace('-', '_')}.yaml",
        ]

        for path in candidates:
            if path.exists():
                return path

        # Try glob match
        for yaml_file in self.targets_dir.glob("*.yaml"):
            if platform in yaml_file.stem:
                return yaml_file

        return None

    async def _run_pytest(
        self,
        target_yaml: Path,
        artifacts: dict[str, Path],
        job_data: dict[str, Any],
    ) -> TestResult:
        """Run pytest with labgrid environment."""
        timeout = job_data.get("timeout", self.default_timeout)
        test_path = job_data.get("test_path", "")  # specific test file/dir

        # Build pytest command
        cmd = [
            "pytest",
            "--lg-env", str(target_yaml),
            "--tb=short",
            "-v",
            "--json-report",
            "--json-report-file=-",  # Output to stdout
        ]

        # Add artifact paths as environment/options
        env = os.environ.copy()
        if "kernel" in artifacts:
            env["LG_KERNEL"] = str(artifacts["kernel"])
        if "rootfs" in artifacts:
            env["LG_ROOTFS"] = str(artifacts["rootfs"])
        if "dtb" in artifacts:
            env["LG_DTB"] = str(artifacts["dtb"])

        # Test path
        if test_path:
            cmd.append(str(self.tests_dir / test_path))
        else:
            cmd.append(str(self.tests_dir))

        logger.info(f"Running: {' '.join(cmd)}")

        # Run pytest
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
                    timeout=timeout,
                )
                output = stdout.decode() if stdout else ""
            except asyncio.TimeoutError:
                proc.kill()
                return TestResult(
                    errors=1,
                    output="Test timed out",
                )

        except Exception as e:
            return TestResult(errors=1, output=str(e))

        # Parse results
        return self._parse_pytest_output(output)

    def _parse_pytest_output(self, output: str) -> TestResult:
        """Parse pytest output for results."""
        result = TestResult(output=output)

        # Try to parse JSON report if available
        try:
            # Look for JSON in output (pytest-json-report outputs to stdout with --)
            for line in output.split("\n"):
                line = line.strip()
                if line.startswith("{") and '"summary"' in line:
                    data = json.loads(line)
                    summary = data.get("summary", {})
                    result.passed = summary.get("passed", 0)
                    result.failed = summary.get("failed", 0)
                    result.skipped = summary.get("skipped", 0)
                    result.errors = summary.get("error", 0)
                    result.total = summary.get("total", 0)
                    result.duration = data.get("duration", 0)

                    # Extract test cases
                    for test in data.get("tests", []):
                        result.test_cases.append({
                            "name": test.get("nodeid", "").split("::")[-1],
                            "result": test.get("outcome", "unknown"),
                            "duration": test.get("duration", 0),
                        })
                    return result
        except (json.JSONDecodeError, KeyError):
            pass

        # Fallback: parse text output
        for line in output.split("\n"):
            # Match summary line: "5 passed, 2 failed, 1 skipped in 10.5s"
            if " passed" in line or " failed" in line:
                import re
                if match := re.search(r"(\d+) passed", line):
                    result.passed = int(match.group(1))
                if match := re.search(r"(\d+) failed", line):
                    result.failed = int(match.group(1))
                if match := re.search(r"(\d+) skipped", line):
                    result.skipped = int(match.group(1))
                if match := re.search(r"(\d+) error", line):
                    result.errors = int(match.group(1))
                if match := re.search(r"in ([\d.]+)s", line):
                    result.duration = float(match.group(1))

        result.total = result.passed + result.failed + result.skipped + result.errors
        return result

    # ==================== Result Reporting ====================

    async def _report_results(self, node_id: str, result: TestResult) -> None:
        """Report test results to KernelCI API."""
        # Create test case nodes
        for tc in result.test_cases:
            try:
                await self._api_post(
                    "/api/latest/nodes",
                    {
                        "name": tc["name"],
                        "parent": node_id,
                        "kind": "test_case",
                        "state": "done",
                        "result": self._map_outcome(tc.get("result", "")),
                        "data": {"duration": tc.get("duration", 0)},
                    },
                )
            except Exception as e:
                logger.warning(f"Failed to create test case node: {e}")

        # Update job node
        await self._api_put(
            f"/api/latest/nodes/{node_id}",
            {
                "state": "done",
                "result": result.result,
                "data": {
                    "passed": result.passed,
                    "failed": result.failed,
                    "skipped": result.skipped,
                    "duration": result.duration,
                },
            },
        )

    async def _report_failure(self, node_id: str, error: str) -> None:
        """Report job failure."""
        try:
            await self._api_put(
                f"/api/latest/nodes/{node_id}",
                {
                    "state": "done",
                    "result": "incomplete",
                    "data": {"error": error},
                },
            )
        except Exception as e:
            logger.error(f"Failed to report failure: {e}")

    @staticmethod
    def _map_outcome(outcome: str) -> str:
        """Map pytest outcome to KernelCI result."""
        mapping = {
            "passed": "pass",
            "failed": "fail",
            "skipped": "skip",
            "error": "incomplete",
            "xfailed": "skip",
            "xpassed": "pass",
        }
        return mapping.get(outcome.lower(), "skip")


# ==================== CLI ====================

def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="KernelCI Labgrid Pull Agent - runs pytest locally"
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("KCI_API_URL", "https://api.kernelci.org"),
        help="KernelCI API URL",
    )
    parser.add_argument(
        "--api-token",
        default=os.environ.get("KCI_API_TOKEN"),
        help="KernelCI API token (or KCI_API_TOKEN env)",
    )
    parser.add_argument(
        "--lab-name", "-l",
        required=True,
        help="Lab name for job filtering",
    )
    parser.add_argument(
        "--tests-dir", "-t",
        required=True,
        help="Path to tests directory (e.g., openwrt-tests/tests)",
    )
    parser.add_argument(
        "--targets-dir",
        help="Path to labgrid targets (default: tests_dir/../targets)",
    )
    parser.add_argument(
        "--poll-interval", "-p",
        type=int,
        default=30,
        help="Poll interval in seconds",
    )
    parser.add_argument(
        "--artifact-dir", "-a",
        help="Directory for artifacts",
    )
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="Debug logging",
    )

    args = parser.parse_args()

    if not args.api_token:
        parser.error("API token required (--api-token or KCI_API_TOKEN)")

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    agent = LabgridPullAgent(
        api_url=args.api_url,
        api_token=args.api_token,
        lab_name=args.lab_name,
        tests_dir=args.tests_dir,
        targets_dir=args.targets_dir,
        poll_interval=args.poll_interval,
        artifact_dir=args.artifact_dir,
    )

    # Signal handling
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(agent.stop()))

    try:
        loop.run_until_complete(agent.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
