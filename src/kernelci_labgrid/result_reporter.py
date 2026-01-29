"""Result Reporter for KernelCI API.

This module handles reporting test results back to the KernelCI API,
creating and updating nodes in the Maestro node hierarchy.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class APIError(Exception):
    """Raised when API operations fail."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class LabgridResultReporter:
    """Reports Labgrid test results to KernelCI API.

    This class handles all interactions with the KernelCI API for
    creating test nodes and reporting results.

    Example:
        async with LabgridResultReporter(api_url, api_token) as reporter:
            # Create a job node
            job_node = await reporter.create_job_node(
                parent_id="build_node_123",
                job_name="baseline-arm64",
                platform="bcm2711-rpi-4-b",
            )

            # Update with running state
            await reporter.update_node(job_node["id"], state="running")

            # Report test results
            for test_case in results:
                await reporter.create_test_case(
                    parent_id=job_node["id"],
                    name=test_case["name"],
                    result=test_case["result"],
                )

            # Mark job as complete
            await reporter.update_node(
                job_node["id"],
                state="done",
                result="pass",
            )
    """

    def __init__(
        self,
        api_url: str,
        api_token: str,
        *,
        timeout: int = 30,
        retry_count: int = 3,
    ):
        """Initialize the result reporter.

        Args:
            api_url: KernelCI API base URL
            api_token: API authentication token
            timeout: Request timeout in seconds
            retry_count: Number of retries for failed requests
        """
        self.api_url = api_url.rstrip("/")
        self.api_token = api_token
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.retry_count = retry_count
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "LabgridResultReporter":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    async def connect(self) -> None:
        """Create HTTP session."""
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self.api_token}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )

    async def close(self) -> None:
        """Close HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        """Get the HTTP session."""
        if self._session is None:
            raise RuntimeError("Reporter not connected. Call connect() first.")
        return self._session

    async def _request(
        self,
        method: str,
        endpoint: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Make an HTTP request with retries.

        Args:
            method: HTTP method
            endpoint: API endpoint (relative to base URL)
            **kwargs: Additional arguments for aiohttp request

        Returns:
            Response JSON data

        Raises:
            APIError: If request fails after retries
        """
        url = f"{self.api_url}{endpoint}"
        last_error: Exception | None = None

        for attempt in range(self.retry_count):
            try:
                async with self.session.request(method, url, **kwargs) as resp:
                    if resp.status >= 400:
                        error_text = await resp.text()
                        raise APIError(
                            f"API error: {resp.status} - {error_text}",
                            status_code=resp.status,
                        )
                    return await resp.json()

            except aiohttp.ClientError as e:
                last_error = e
                logger.warning(
                    f"Request failed (attempt {attempt + 1}/{self.retry_count}): {e}"
                )
                if attempt < self.retry_count - 1:
                    import asyncio
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff

        raise APIError(f"Request failed after {self.retry_count} attempts: {last_error}")

    # ============ Node Operations ============

    async def get_node(self, node_id: str) -> dict[str, Any]:
        """Get a node by ID.

        Args:
            node_id: Node ID

        Returns:
            Node data dictionary
        """
        return await self._request("GET", f"/api/latest/nodes/{node_id}")

    async def create_node(self, node_data: dict[str, Any]) -> dict[str, Any]:
        """Create a new node.

        Args:
            node_data: Node data dictionary

        Returns:
            Created node data with ID
        """
        response = await self._request("POST", "/api/latest/nodes", json=node_data)
        logger.info(f"Created node: {response.get('id')} ({node_data.get('name')})")
        return response

    async def update_node(
        self,
        node_id: str,
        **updates: Any,
    ) -> dict[str, Any]:
        """Update an existing node.

        Args:
            node_id: Node ID to update
            **updates: Fields to update

        Returns:
            Updated node data
        """
        response = await self._request(
            "PUT",
            f"/api/latest/nodes/{node_id}",
            json=updates,
        )
        logger.debug(f"Updated node {node_id}: {updates}")
        return response

    async def query_nodes(
        self,
        **filters: Any,
    ) -> list[dict[str, Any]]:
        """Query nodes with filters.

        Args:
            **filters: Query filters (state, kind, parent, etc.)

        Returns:
            List of matching nodes
        """
        response = await self._request("GET", "/api/latest/nodes", params=filters)
        return response.get("nodes", [])

    # ============ Job Nodes ============

    async def create_job_node(
        self,
        parent_id: str,
        job_name: str,
        platform: str,
        *,
        artifacts: dict[str, str] | None = None,
        runtime: str = "labgrid",
        lab_name: str | None = None,
    ) -> dict[str, Any]:
        """Create a test job node.

        Args:
            parent_id: Parent node ID (usually a build node)
            job_name: Job name (e.g., "baseline-arm64")
            platform: Target platform
            artifacts: Dictionary of artifact URLs
            runtime: Runtime type
            lab_name: Lab name for tracking

        Returns:
            Created job node
        """
        node_data = {
            "name": job_name,
            "parent": parent_id,
            "kind": "job",
            "state": "pending",
            "data": {
                "platform": platform,
                "runtime": runtime,
                "lab": lab_name,
                "artifacts": artifacts or {},
            },
        }

        return await self.create_node(node_data)

    async def start_job(
        self,
        node_id: str,
        *,
        place_name: str | None = None,
    ) -> dict[str, Any]:
        """Mark a job as running.

        Args:
            node_id: Job node ID
            place_name: Labgrid place name (for tracking)

        Returns:
            Updated node
        """
        updates: dict[str, Any] = {
            "state": "running",
            "data.started_at": datetime.now(timezone.utc).isoformat(),
        }

        if place_name:
            updates["data.labgrid_place"] = place_name

        return await self.update_node(node_id, **updates)

    async def complete_job(
        self,
        node_id: str,
        result: str,
        *,
        log_url: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        """Mark a job as completed.

        Args:
            node_id: Job node ID
            result: Result (pass, fail, skip, incomplete)
            log_url: URL to full test log
            error_message: Error message if failed

        Returns:
            Updated node
        """
        updates: dict[str, Any] = {
            "state": "done",
            "result": result,
            "data.completed_at": datetime.now(timezone.utc).isoformat(),
        }

        if log_url:
            updates["data.log_url"] = log_url

        if error_message:
            updates["data.error"] = error_message

        logger.info(f"Completed job {node_id} with result: {result}")
        return await self.update_node(node_id, **updates)

    # ============ Test Suite/Case Nodes ============

    async def create_test_suite(
        self,
        parent_id: str,
        suite_name: str,
    ) -> dict[str, Any]:
        """Create a test suite node.

        Args:
            parent_id: Parent job node ID
            suite_name: Test suite name

        Returns:
            Created suite node
        """
        node_data = {
            "name": suite_name,
            "parent": parent_id,
            "kind": "test_suite",
            "state": "running",
        }

        return await self.create_node(node_data)

    async def create_test_case(
        self,
        parent_id: str,
        name: str,
        result: str,
        *,
        log: str | None = None,
        duration: float | None = None,
        measurements: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a test case node.

        Args:
            parent_id: Parent node ID (job or suite)
            name: Test case name
            result: Test result (pass, fail, skip)
            log: Test output log
            duration: Test duration in seconds
            measurements: Performance measurements

        Returns:
            Created test case node
        """
        node_data = {
            "name": name,
            "parent": parent_id,
            "kind": "test_case",
            "state": "done",
            "result": result,
            "data": {},
        }

        if log:
            node_data["data"]["log"] = log

        if duration is not None:
            node_data["data"]["duration"] = duration

        if measurements:
            node_data["data"]["measurements"] = measurements

        return await self.create_node(node_data)

    async def complete_test_suite(
        self,
        node_id: str,
        result: str,
    ) -> dict[str, Any]:
        """Mark a test suite as completed.

        Args:
            node_id: Suite node ID
            result: Overall suite result

        Returns:
            Updated node
        """
        return await self.update_node(
            node_id,
            state="done",
            result=result,
        )

    # ============ Batch Operations ============

    async def report_test_results(
        self,
        job_node_id: str,
        test_cases: list[dict[str, Any]],
        *,
        suite_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Report multiple test case results.

        Args:
            job_node_id: Parent job node ID
            test_cases: List of test case dictionaries with keys:
                - name: Test case name
                - result: pass/fail/skip
                - log: Optional log output
                - duration: Optional duration
            suite_name: Optional suite name to group tests under

        Returns:
            List of created test case nodes
        """
        parent_id = job_node_id

        # Create suite if specified
        if suite_name:
            suite = await self.create_test_suite(job_node_id, suite_name)
            parent_id = suite["id"]

        # Create test cases
        created = []
        for tc in test_cases:
            node = await self.create_test_case(
                parent_id,
                name=tc["name"],
                result=tc.get("result", "skip"),
                log=tc.get("log"),
                duration=tc.get("duration"),
                measurements=tc.get("measurements"),
            )
            created.append(node)

        # Complete suite with aggregate result
        if suite_name:
            # Determine suite result from test cases
            failed = any(tc.get("result") == "fail" for tc in test_cases)
            suite_result = "fail" if failed else "pass"
            await self.complete_test_suite(parent_id, suite_result)

        logger.info(f"Reported {len(created)} test cases for job {job_node_id}")
        return created

    # ============ Utility Methods ============

    async def claim_pending_job(
        self,
        node_id: str,
        lab_name: str,
    ) -> bool:
        """Atomically claim a pending job.

        Uses optimistic locking to prevent duplicate claims.

        Args:
            node_id: Job node ID
            lab_name: Lab name claiming the job

        Returns:
            True if claim successful, False if already claimed
        """
        try:
            node = await self.get_node(node_id)

            # Check if already claimed or running
            if node.get("state") != "pending":
                return False

            # Try to claim by updating state
            await self.update_node(
                node_id,
                state="running",
                **{"data.claimed_by": lab_name},
            )
            return True

        except APIError as e:
            if e.status_code == 409:  # Conflict - already claimed
                return False
            raise

    async def get_pending_jobs(
        self,
        runtime: str = "labgrid",
        lab_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get pending jobs for a runtime.

        Args:
            runtime: Runtime type to filter
            lab_name: Optional lab name filter

        Returns:
            List of pending job nodes
        """
        filters: dict[str, str] = {
            "state": "pending",
            "kind": "job",
            "data.runtime": runtime,
        }

        if lab_name:
            filters["data.lab"] = lab_name

        return await self.query_nodes(**filters)

    def parse_pytest_output(self, output: str) -> list[dict[str, Any]]:
        """Parse pytest output into test case results.

        Args:
            output: Raw pytest output

        Returns:
            List of test case dictionaries
        """
        results = []

        for line in output.split("\n"):
            line = line.strip()

            # Match pytest result lines: PASSED/FAILED test_name
            if " PASSED" in line or " FAILED" in line:
                parts = line.split()
                if len(parts) >= 2:
                    # Find the test name (usually contains ::)
                    test_name = None
                    for part in parts:
                        if "::" in part:
                            test_name = part.split("::")[-1]
                            break

                    if test_name:
                        result = "pass" if "PASSED" in line else "fail"
                        results.append({"name": test_name, "result": result})

            # Match collected test count
            elif "passed" in line.lower() or "failed" in line.lower():
                # Summary line like "5 passed, 2 failed in 10.5s"
                continue

        return results

    def parse_tap_output(self, output: str) -> list[dict[str, Any]]:
        """Parse TAP (Test Anything Protocol) output.

        Args:
            output: TAP-formatted output

        Returns:
            List of test case dictionaries
        """
        results = []

        for line in output.split("\n"):
            line = line.strip()

            if line.startswith("ok "):
                # ok 1 - test description
                parts = line.split(" - ", 1)
                name = parts[1] if len(parts) > 1 else line[3:].strip()
                # Remove test number
                name = " ".join(name.split()[1:]) if name.split() else name
                results.append({"name": name, "result": "pass"})

            elif line.startswith("not ok "):
                parts = line.split(" - ", 1)
                name = parts[1] if len(parts) > 1 else line[7:].strip()
                name = " ".join(name.split()[1:]) if name.split() else name
                results.append({"name": name, "result": "fail"})

        return results
