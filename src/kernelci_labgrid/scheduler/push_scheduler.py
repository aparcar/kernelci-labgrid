"""Push Mode Scheduler for KernelCI Labgrid Integration.

This scheduler subscribes to KernelCI API events and pushes test jobs
to Labgrid coordinators when build nodes complete successfully.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

import aiohttp

from kernelci_labgrid.config import LabgridConfig, SchedulerJobConfig
from kernelci_labgrid.result_reporter import LabgridResultReporter
from kernelci_labgrid.runtime.labgrid import LabgridRuntime, TestJob, TestResult

logger = logging.getLogger(__name__)


class LabgridPushScheduler:
    """Push mode scheduler for Labgrid test execution.

    This scheduler:
    1. Subscribes to KernelCI API Pub/Sub events
    2. Matches events to configured scheduler jobs
    3. Creates test nodes in the API
    4. Submits tests to Labgrid runtimes
    5. Reports results back to the API

    Example:
        config = LabgridConfig.from_files(
            "config/labgrid-runtime.yaml",
            "config/labgrid-scheduler.yaml",
        )
        scheduler = LabgridPushScheduler(config)
        await scheduler.run()
    """

    def __init__(self, config: LabgridConfig):
        """Initialize the scheduler.

        Args:
            config: Labgrid configuration
        """
        self.config = config
        self._runtimes: dict[str, LabgridRuntime] = {}
        self._reporter: LabgridResultReporter | None = None
        self._running = False
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []

    async def __aenter__(self) -> "LabgridPushScheduler":
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.stop()

    async def start(self) -> None:
        """Start the scheduler and connect to services."""
        logger.info("Starting Labgrid push scheduler")

        # Create result reporter
        self._reporter = LabgridResultReporter(
            self.config.api_url,
            self.config.api_token,
        )
        await self._reporter.connect()

        # Create runtime connections
        for name, runtime_config in self.config.runtimes.items():
            if runtime_config.mode != "push":
                logger.info(f"Skipping non-push runtime: {name}")
                continue

            runtime = LabgridRuntime(
                {
                    "coordinator_address": runtime_config.coordinator_address,
                    "secure": runtime_config.secure,
                    "mode": runtime_config.mode,
                    "ssh": {
                        "proxy_host": runtime_config.ssh.proxy_host,
                        "proxy_user": runtime_config.ssh.proxy_user,
                        "key_file": runtime_config.ssh.key_file,
                    },
                    "storage": {
                        "type": runtime_config.storage.type,
                        "host": runtime_config.storage.host,
                        "path": runtime_config.storage.path,
                    },
                    "place_mapping": [
                        {
                            "place": pm.place,
                            "platform": pm.platform,
                            "compatible": pm.compatible,
                        }
                        for pm in runtime_config.place_mapping
                    ],
                }
            )
            await runtime.connect()
            self._runtimes[name] = runtime
            logger.info(f"Connected to runtime: {name}")

        self._running = True

    async def stop(self) -> None:
        """Stop the scheduler and close connections."""
        logger.info("Stopping Labgrid push scheduler")
        self._running = False

        # Cancel workers
        for worker in self._workers:
            worker.cancel()

        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

        # Close runtimes
        for name, runtime in self._runtimes.items():
            await runtime.close()
            logger.info(f"Disconnected from runtime: {name}")

        self._runtimes.clear()

        # Close reporter
        if self._reporter:
            await self._reporter.close()
            self._reporter = None

    async def run(self, num_workers: int = 4) -> None:
        """Run the scheduler main loop.

        Args:
            num_workers: Number of concurrent job workers
        """
        if not self._running:
            await self.start()

        # Start worker tasks
        for i in range(num_workers):
            worker = asyncio.create_task(
                self._job_worker(f"worker-{i}"),
                name=f"job-worker-{i}",
            )
            self._workers.append(worker)

        # Start event subscriber
        subscriber = asyncio.create_task(
            self._subscribe_to_events(),
            name="event-subscriber",
        )
        self._workers.append(subscriber)

        logger.info(f"Scheduler running with {num_workers} workers")

        # Wait for shutdown
        try:
            await asyncio.gather(*self._workers)
        except asyncio.CancelledError:
            logger.info("Scheduler cancelled")

    async def _subscribe_to_events(self) -> None:
        """Subscribe to KernelCI API events via Server-Sent Events."""
        url = f"{self.config.api_url}/api/latest/subscribe/node"
        headers = {"Authorization": f"Bearer {self.config.api_token}"}

        while self._running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers) as response:
                        if response.status != 200:
                            logger.error(f"Failed to subscribe: {response.status}")
                            await asyncio.sleep(5)
                            continue

                        logger.info("Subscribed to node events")

                        async for line in response.content:
                            if not self._running:
                                break

                            line = line.decode().strip()
                            if line.startswith("data:"):
                                try:
                                    import json
                                    event = json.loads(line[5:])
                                    await self._handle_event(event)
                                except Exception as e:
                                    logger.error(f"Failed to parse event: {e}")

            except aiohttp.ClientError as e:
                logger.error(f"Connection error: {e}")
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Handle an incoming node event.

        Args:
            event: Event data from API
        """
        node_name = event.get("name", "")
        node_state = event.get("state", "")
        node_result = event.get("result", "")
        node_id = event.get("id", "")

        # Only process completed nodes
        if node_state != "done":
            return

        logger.debug(f"Received event: {node_name} -> {node_result}")

        # Find matching scheduler jobs
        matching_jobs = self.config.get_jobs_for_event(
            channel="node",
            name=node_name,
            result=node_result,
        )

        for job_config in matching_jobs:
            logger.info(f"Event matched job: {job_config.job}")

            # Queue job for execution
            await self._event_queue.put({
                "parent_id": node_id,
                "parent_name": node_name,
                "job_config": job_config,
                "event": event,
            })

    async def _job_worker(self, worker_name: str) -> None:
        """Worker task that processes jobs from the queue.

        Args:
            worker_name: Name for logging
        """
        logger.info(f"Started {worker_name}")

        while self._running:
            try:
                # Get job from queue with timeout
                try:
                    job_data = await asyncio.wait_for(
                        self._event_queue.get(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    continue

                await self._execute_job(job_data, worker_name)
                self._event_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"{worker_name} error: {e}")

        logger.info(f"Stopped {worker_name}")

    async def _execute_job(
        self,
        job_data: dict[str, Any],
        worker_name: str,
    ) -> None:
        """Execute a scheduled test job.

        Args:
            job_data: Job data from queue
            worker_name: Worker name for logging
        """
        job_config: SchedulerJobConfig = job_data["job_config"]
        parent_id = job_data["parent_id"]
        event = job_data["event"]

        logger.info(f"{worker_name}: Executing {job_config.job}")

        # Get runtime
        runtime = self._runtimes.get(job_config.runtime_name)
        if not runtime:
            logger.error(f"Runtime not found: {job_config.runtime_name}")
            return

        if not self._reporter:
            logger.error("Reporter not initialized")
            return

        # Get artifacts from parent node
        artifacts = event.get("data", {}).get("artifacts", {})
        kernel_url = artifacts.get("kernel", "")
        dtb_url = artifacts.get("dtb", "")
        rootfs_url = artifacts.get("rootfs", "")
        modules_url = artifacts.get("modules", "")

        # Execute for each platform
        for platform in job_config.platforms:
            try:
                # Create job node in API
                job_node = await self._reporter.create_job_node(
                    parent_id=parent_id,
                    job_name=f"{job_config.job}-{platform}",
                    platform=platform,
                    artifacts=artifacts,
                    runtime="labgrid",
                    lab_name=job_config.runtime_name,
                )
                node_id = job_node["id"]

                # Create test job
                test_job = TestJob(
                    node_id=node_id,
                    name=job_config.job,
                    platform=platform,
                    kernel_revision=event.get("data", {}).get("kernel_revision", ""),
                    kernel_url=kernel_url,
                    dtb_url=dtb_url,
                    rootfs_url=rootfs_url,
                    modules_url=modules_url,
                    test_suite=job_config.test_suite,
                    test_config=job_config.test_config,
                    timeout=job_config.timeout,
                )

                # Mark as running
                await self._reporter.start_job(node_id)

                # Submit to runtime
                result = await runtime.submit(test_job)

                # Report results
                await self._report_result(node_id, result)

                logger.info(
                    f"{worker_name}: Completed {job_config.job} on {platform}: {result.result}"
                )

            except Exception as e:
                logger.exception(f"{worker_name}: Failed {job_config.job} on {platform}")

                # Report failure if node was created
                if "node_id" in locals():
                    await self._reporter.complete_job(
                        node_id,
                        result="incomplete",
                        error_message=str(e),
                    )

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


async def run_scheduler(config: LabgridConfig) -> None:
    """Run the push scheduler.

    Args:
        config: Configuration
    """
    scheduler = LabgridPushScheduler(config)

    # Handle shutdown signals
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig,
            lambda: asyncio.create_task(scheduler.stop()),
        )

    try:
        async with scheduler:
            await scheduler.run()
    except asyncio.CancelledError:
        pass


def main() -> None:
    """CLI entry point for push scheduler."""
    import argparse

    parser = argparse.ArgumentParser(description="KernelCI Labgrid Push Scheduler")
    parser.add_argument(
        "--config",
        "-c",
        default="config/labgrid-runtime.yaml",
        help="Path to runtime configuration file",
    )
    parser.add_argument(
        "--scheduler-config",
        "-s",
        default="config/labgrid-scheduler.yaml",
        help="Path to scheduler configuration file",
    )
    parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=4,
        help="Number of worker tasks",
    )
    parser.add_argument(
        "--debug",
        "-d",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Load configuration
    config = LabgridConfig.from_files(args.config, args.scheduler_config)

    # Run scheduler
    asyncio.run(run_scheduler(config))


if __name__ == "__main__":
    main()
