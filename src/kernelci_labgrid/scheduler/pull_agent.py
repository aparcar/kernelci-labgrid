"""Pull Mode Agent for KernelCI Labgrid Integration.

This agent runs within Labgrid labs that cannot be directly accessed
from the KernelCI infrastructure. It polls the API for pending jobs
and executes them locally.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import tempfile
from pathlib import Path
from typing import Any

import aiohttp

from kernelci_labgrid.config import LabgridConfig, RuntimeConfig
from kernelci_labgrid.result_reporter import LabgridResultReporter
from kernelci_labgrid.runtime.labgrid import LabgridRuntime, TestJob, TestResult

logger = logging.getLogger(__name__)


class LabgridPullAgent:
    """Pull mode agent for labs behind firewalls.

    This agent:
    1. Polls KernelCI API for pending jobs assigned to this lab
    2. Claims jobs atomically to prevent duplicate execution
    3. Downloads artifacts to local storage
    4. Executes tests on local Labgrid infrastructure
    5. Reports results back to the API

    Example:
        agent = LabgridPullAgent(
            api_url="https://api.kernelci.org",
            api_token="your-token",
            lab_name="my-lab",
            coordinator_address="localhost:20408",
        )
        await agent.run()
    """

    def __init__(
        self,
        api_url: str,
        api_token: str,
        lab_name: str,
        coordinator_address: str = "localhost:20408",
        *,
        poll_interval: int = 30,
        artifact_dir: str | Path | None = None,
        runtime_config: RuntimeConfig | None = None,
    ):
        """Initialize the pull agent.

        Args:
            api_url: KernelCI API URL
            api_token: API authentication token
            lab_name: Name of this lab for job filtering
            coordinator_address: Local Labgrid coordinator address
            poll_interval: Seconds between API polls
            artifact_dir: Directory for downloaded artifacts
            runtime_config: Optional runtime configuration
        """
        self.api_url = api_url
        self.api_token = api_token
        self.lab_name = lab_name
        self.coordinator_address = coordinator_address
        self.poll_interval = poll_interval
        self.artifact_dir = Path(artifact_dir or tempfile.mkdtemp(prefix="labgrid-"))

        self._runtime_config = runtime_config or RuntimeConfig(
            name=lab_name,
            coordinator_address=coordinator_address,
            mode="pull",
        )

        self._runtime: LabgridRuntime | None = None
        self._reporter: LabgridResultReporter | None = None
        self._running = False
        self._current_jobs: set[str] = set()  # Track in-progress job IDs

    async def __aenter__(self) -> "LabgridPullAgent":
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.stop()

    async def start(self) -> None:
        """Start the agent and connect to services."""
        logger.info(f"Starting Labgrid pull agent for lab: {self.lab_name}")

        # Ensure artifact directory exists
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

        # Create result reporter
        self._reporter = LabgridResultReporter(self.api_url, self.api_token)
        await self._reporter.connect()

        # Create runtime
        self._runtime = LabgridRuntime(
            {
                "coordinator_address": self.coordinator_address,
                "mode": "pull",
                "ssh": {
                    "proxy_host": self._runtime_config.ssh.proxy_host,
                    "proxy_user": self._runtime_config.ssh.proxy_user,
                    "key_file": self._runtime_config.ssh.key_file,
                },
                "storage": {
                    "type": "local",
                    "path": str(self.artifact_dir),
                },
                "place_mapping": [
                    {
                        "place": pm.place,
                        "platform": pm.platform,
                        "compatible": pm.compatible,
                    }
                    for pm in self._runtime_config.place_mapping
                ],
            }
        )
        await self._runtime.connect()

        self._running = True
        logger.info("Pull agent started")

    async def stop(self) -> None:
        """Stop the agent and close connections."""
        logger.info("Stopping pull agent")
        self._running = False

        # Wait for current jobs to complete (with timeout)
        if self._current_jobs:
            logger.info(f"Waiting for {len(self._current_jobs)} jobs to complete...")
            for _ in range(30):  # 30 second timeout
                if not self._current_jobs:
                    break
                await asyncio.sleep(1)

        if self._runtime:
            await self._runtime.close()
            self._runtime = None

        if self._reporter:
            await self._reporter.close()
            self._reporter = None

        logger.info("Pull agent stopped")

    async def run(self) -> None:
        """Run the agent main loop."""
        if not self._running:
            await self.start()

        logger.info(f"Polling for jobs every {self.poll_interval}s")

        while self._running:
            try:
                await self._poll_and_execute()
            except Exception as e:
                logger.exception(f"Error in poll loop: {e}")

            await asyncio.sleep(self.poll_interval)

    async def _poll_and_execute(self) -> None:
        """Poll for pending jobs and execute them."""
        if not self._reporter:
            return

        # Get pending jobs for this lab
        try:
            pending_jobs = await self._reporter.get_pending_jobs(
                runtime="labgrid",
                lab_name=self.lab_name,
            )
        except Exception as e:
            logger.error(f"Failed to get pending jobs: {e}")
            return

        if not pending_jobs:
            logger.debug("No pending jobs")
            return

        logger.info(f"Found {len(pending_jobs)} pending job(s)")

        # Process each job
        for job_node in pending_jobs:
            node_id = job_node.get("id")

            # Skip if already processing
            if node_id in self._current_jobs:
                continue

            # Try to claim the job
            claimed = await self._reporter.claim_pending_job(node_id, self.lab_name)
            if not claimed:
                logger.debug(f"Job {node_id} already claimed")
                continue

            # Execute in background
            self._current_jobs.add(node_id)
            asyncio.create_task(self._execute_job(job_node))

    async def _execute_job(self, job_node: dict[str, Any]) -> None:
        """Execute a single test job.

        Args:
            job_node: Job node data from API
        """
        node_id = job_node["id"]
        job_name = job_node.get("name", "unknown")

        logger.info(f"Executing job: {job_name} ({node_id})")

        try:
            # Mark as running
            if self._reporter:
                await self._reporter.start_job(node_id)

            # Download artifacts
            job_data = job_node.get("data", {})
            artifacts = await self._download_artifacts(node_id, job_data)

            # Create test job
            test_job = TestJob(
                node_id=node_id,
                name=job_name,
                platform=job_data.get("platform", ""),
                kernel_revision=job_data.get("kernel_revision", ""),
                kernel_url=artifacts.get("kernel", ""),
                dtb_url=artifacts.get("dtb"),
                rootfs_url=artifacts.get("rootfs"),
                modules_url=artifacts.get("modules"),
                test_suite=job_data.get("test_suite", "baseline"),
                test_config=job_data.get("test_config", {}),
                timeout=job_data.get("timeout", 3600),
            )

            # Execute test
            if self._runtime:
                result = await self._runtime.submit(test_job)
            else:
                result = TestResult(
                    job=test_job,
                    success=False,
                    result="incomplete",
                    error="Runtime not initialized",
                )

            # Report results
            await self._report_result(node_id, result)

            logger.info(f"Completed job {job_name}: {result.result}")

        except Exception as e:
            logger.exception(f"Failed to execute job {job_name}")

            # Report failure
            if self._reporter:
                await self._reporter.complete_job(
                    node_id,
                    result="incomplete",
                    error_message=str(e),
                )

        finally:
            self._current_jobs.discard(node_id)
            # Cleanup artifacts
            await self._cleanup_artifacts(node_id)

    async def _download_artifacts(
        self,
        node_id: str,
        job_data: dict[str, Any],
    ) -> dict[str, str]:
        """Download job artifacts to local storage.

        Args:
            node_id: Node ID for directory naming
            job_data: Job data containing artifact URLs

        Returns:
            Dictionary mapping artifact names to local paths
        """
        artifacts = job_data.get("artifacts", {})
        local_artifacts = {}

        # Create job-specific directory
        job_dir = self.artifact_dir / node_id
        job_dir.mkdir(exist_ok=True)

        async with aiohttp.ClientSession() as session:
            for name, url in artifacts.items():
                if not url:
                    continue

                local_path = job_dir / name
                logger.debug(f"Downloading {name} from {url}")

                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            content = await resp.read()
                            local_path.write_bytes(content)
                            local_artifacts[name] = str(local_path)
                            logger.debug(f"Downloaded {name}: {len(content)} bytes")
                        else:
                            logger.warning(
                                f"Failed to download {name}: HTTP {resp.status}"
                            )
                except Exception as e:
                    logger.error(f"Failed to download {name}: {e}")

        return local_artifacts

    async def _cleanup_artifacts(self, node_id: str) -> None:
        """Clean up downloaded artifacts.

        Args:
            node_id: Node ID for directory identification
        """
        job_dir = self.artifact_dir / node_id

        if job_dir.exists():
            import shutil

            try:
                shutil.rmtree(job_dir)
                logger.debug(f"Cleaned up artifacts for {node_id}")
            except Exception as e:
                logger.warning(f"Failed to cleanup {job_dir}: {e}")

    async def _report_result(self, node_id: str, result: TestResult) -> None:
        """Report test result to API.

        Args:
            node_id: Job node ID
            result: Test result
        """
        if not self._reporter:
            return

        # Report individual test cases
        if result.test_cases:
            await self._reporter.report_test_results(
                node_id,
                result.test_cases,
                suite_name=result.job.test_suite,
            )

        # Complete the job
        await self._reporter.complete_job(
            node_id,
            result=result.result,
            error_message=result.error,
        )

    async def health_check(self) -> dict[str, Any]:
        """Check agent health status.

        Returns:
            Health status dictionary
        """
        status: dict[str, Any] = {
            "status": "healthy" if self._running else "stopped",
            "lab_name": self.lab_name,
            "coordinator": self.coordinator_address,
            "current_jobs": len(self._current_jobs),
        }

        # Check runtime health
        if self._runtime:
            runtime_health = await self._runtime.health_check()
            status["runtime"] = runtime_health

        return status


async def run_agent(
    api_url: str,
    api_token: str,
    lab_name: str,
    coordinator_address: str = "localhost:20408",
    poll_interval: int = 30,
) -> None:
    """Run the pull agent.

    Args:
        api_url: KernelCI API URL
        api_token: API token
        lab_name: Lab name
        coordinator_address: Labgrid coordinator address
        poll_interval: Poll interval in seconds
    """
    agent = LabgridPullAgent(
        api_url=api_url,
        api_token=api_token,
        lab_name=lab_name,
        coordinator_address=coordinator_address,
        poll_interval=poll_interval,
    )

    # Handle shutdown signals
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig,
            lambda: asyncio.create_task(agent.stop()),
        )

    try:
        async with agent:
            await agent.run()
    except asyncio.CancelledError:
        pass


def main() -> None:
    """CLI entry point for pull agent."""
    import argparse

    parser = argparse.ArgumentParser(description="KernelCI Labgrid Pull Agent")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("KCI_API_URL", "https://api.kernelci.org"),
        help="KernelCI API URL",
    )
    parser.add_argument(
        "--api-token",
        default=os.environ.get("KCI_API_TOKEN"),
        help="KernelCI API token",
    )
    parser.add_argument(
        "--lab-name",
        "-l",
        required=True,
        help="Lab name for job filtering",
    )
    parser.add_argument(
        "--coordinator",
        "-c",
        default=os.environ.get("LABGRID_COORDINATOR", "localhost:20408"),
        help="Labgrid coordinator address",
    )
    parser.add_argument(
        "--poll-interval",
        "-p",
        type=int,
        default=30,
        help="Seconds between API polls",
    )
    parser.add_argument(
        "--artifact-dir",
        "-a",
        help="Directory for downloaded artifacts",
    )
    parser.add_argument(
        "--debug",
        "-d",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if not args.api_token:
        parser.error("API token required (--api-token or KCI_API_TOKEN env var)")

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Run agent
    asyncio.run(
        run_agent(
            api_url=args.api_url,
            api_token=args.api_token,
            lab_name=args.lab_name,
            coordinator_address=args.coordinator,
            poll_interval=args.poll_interval,
        )
    )


if __name__ == "__main__":
    main()
