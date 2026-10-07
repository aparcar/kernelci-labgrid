"""KernelCI Labgrid Agent.

A single daemon that:
1. Polls the KernelCI (Maestro) API for available test jobs of this lab
2. Runs periodic health checks on devices (like LAVA, results kept local)
3. Downloads and verifies the firmware referenced by the job definition
4. Runs pytest with labgrid (your existing tests, e.g. openwrt-tests)
5. Uploads logs to kernelci-storage and reports results back to the API

Job protocol (see openwrtci/plan.md):

  job node      kind=job, state=available, data.runtime=<lab>,
                data.platform=<targets/<platform>.yaml>,
                artifacts.job_definition=<URL of a PULL_LABS-shaped JSON>
  definition    {"artifacts": {"firmware": URL},
                 "integrity": {"sha256": {"firmware": HEX}},
                 "tests": [{"parameters": "", "timeout_s": 1800}]}
                (parameters: pytest selection relative to tests_dir, empty = all)
  claim         data.job_id=<lab>:<uuid> (best effort, kernelci-api has no
                compare-and-set yet)
  results       PUT /nodes/<job> with the job node + one child per test
                module + one leaf per test, and a sibling "boot" test node
                under the build (the dashboard only counts "boot*" as boots)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shlex
import shutil
import signal
import socket
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import yaml

from kernelci_labgrid.labgrid import Coordinator, Lease, Place, child_env, kill_group, spawn

logger = logging.getLogger(__name__)

# openwrt-tests' conftest exits pytest with 3 when the firmware never reaches
# a shell; 0/1/5 are "ran normally" (all passed, some failed, none collected).
PYTEST_RC_NO_SHELL = 3
PYTEST_RC_RAN = (0, 1, 5)

# Dropped when PUTting a node back. Only alias/extra fields: PUT /node does
# not use exclude_unset, so omitting e.g. created/timeout/retry_counter would
# reset them to their defaults. Ownership fields are ignored by the API.
NODE_READ_ONLY_FIELDS = ("_id", "user")


# ==================== Data Classes ====================


@dataclass
class TestResult:
    """Result from pytest execution."""

    __test__ = False  # not a pytest test class

    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    total: int = 0
    duration: float = 0.0
    output: str = ""
    test_cases: list[dict[str, Any]] = field(default_factory=list)
    returncode: int | None = None
    outdir: Path | None = None

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

    @property
    def boot_result(self) -> str:
        """Did the firmware reach a shell? test_shell is the canonical check."""
        for tc in self.test_cases:
            if tc["name"] == "test_shell":
                return {"pass": "pass", "fail": "fail"}.get(tc["result"], "incomplete")
        if self.returncode == PYTEST_RC_NO_SHELL:
            return "fail"
        return "pass" if self.passed else "incomplete"


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
    test_path: str = ""  # pytest selection relative to tests_dir, empty = all
    timeout: int = 3600

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
        )


# ==================== Main Agent ====================


class LabgridAgent:
    """Unified agent for KernelCI job execution and device health checks.

    Example:
        agent = LabgridAgent(
            api_url="http://localhost:8001/latest",
            api_token="your-token",
            lab_name="openwrt-local",
            tests_dir="/path/to/openwrt-tests/tests",
            storage_url="http://localhost:3000",
            storage_token="storage-jwt",
            platforms=["qemu_armsr-armv8"],
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
        storage_url: str | None = None,
        storage_token: str | None = None,
        platforms: list[str] | None = None,
        pytest_command: str | None = None,
        poll_interval: int = 30,
        artifact_dir: str | Path | None = None,
        default_timeout: int = 3600,
        # Health check options
        health_checks_dir: str | Path | None = None,
        health_state_file: str | Path | None = None,
        # Real hardware via the lab's labgrid-coordinator
        coordinator: str | None = None,
        labgrid_command: str | None = None,
        strategies_dir: str | Path | None = None,
        reserve_timeout: int = 60,
    ):
        """Initialize the agent.

        Args:
            api_url: KernelCI API URL including the version, e.g. .../latest
            api_token: API token of the lab user (runtime:<lab>:node-editor)
            lab_name: Name of this lab, matched against the job's data.runtime
            tests_dir: pytest tests incl. conftest.py (e.g. openwrt-tests/tests);
                pytest runs there
            targets_dir: labgrid target YAMLs (default: tests_dir/../targets)
            storage_url: kernelci-storage URL for log uploads (optional)
            storage_token: kernelci-storage JWT with upload rights below
                logs/<lab_name>/
            platforms: Platforms this lab serves; default: every target YAML
            pytest_command: Command to run pytest (default: `python -m pytest`
                with this environment's Python, which includes labgrid)
            poll_interval: Seconds between API polls
            artifact_dir: Directory for downloaded artifacts
            default_timeout: Default test timeout in seconds
            health_checks_dir: Directory with health check YAML configs (optional)
            health_state_file: JSON file to persist device health state
            coordinator: The lab's labgrid-coordinator (host:port). Enables real
                hardware: platforms are discovered from the places' device
                tags, places are reserved per job. Without it: QEMU only.
            labgrid_command: labgrid-client command, used for power off
                (default: the labgrid-client of this environment)
            strategies_dir: labgrid strategies the target files import as
                `../strategies/<file>.py` (default: targets_dir/../strategies)
            reserve_timeout: Seconds to wait for a free place before leaving
                a job for later
        """
        self.api_url = api_url.rstrip("/")
        self.api_token = api_token
        self.lab_name = lab_name
        self.tests_dir = Path(tests_dir)
        self.targets_dir = Path(targets_dir) if targets_dir else self.tests_dir.parent / "targets"
        self.strategies_dir = (Path(strategies_dir) if strategies_dir
                               else self.targets_dir.parent / "strategies")
        self.storage_url = storage_url.rstrip("/") if storage_url else None
        self.storage_token = storage_token
        # Configured platforms: an allow-list for discovered hardware
        # platforms, plus the local (QEMU) platforms to run
        self.configured_platforms = list(platforms or [])
        self.platforms = list(self.configured_platforms)
        self.reserve_timeout = reserve_timeout
        if not labgrid_command:
            labgrid_command = shlex.join([str(Path(sys.executable).parent / "labgrid-client")])
        self._labgrid = (Coordinator(coordinator, labgrid_command, self.tests_dir)
                         if coordinator else None)
        self._places: list[Place] = []
        self._job_places: dict[str, str] = {}
        self.pytest_command = (shlex.split(pytest_command) if pytest_command
                               else [sys.executable, "-m", "pytest"])
        self.poll_interval = poll_interval
        self.artifact_dir = Path(artifact_dir or tempfile.mkdtemp(prefix="kci-labgrid-"))
        self.default_timeout = default_timeout
        self.worker_id = f"{lab_name}@{socket.gethostname()}"

        # Health check configuration
        self.health_checks_dir = Path(health_checks_dir) if health_checks_dir else None
        self.health_state_file = Path(health_state_file) if health_state_file else None

        # Runtime state
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._current_jobs: set[str] = set()
        self._busy_platforms: set[str] = set()
        self._health_configs: dict[str, HealthCheckConfig] = {}
        self._device_health: dict[str, DeviceHealth] = {}

    async def __aenter__(self) -> "LabgridAgent":
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.stop()

    async def start(self) -> None:
        """Start the agent."""
        logger.info(f"Starting agent for lab: {self.lab_name}")
        logger.info(f"Tests directory: {self.tests_dir}")
        logger.info(f"Targets directory: {self.targets_dir}")
        logger.info(f"Strategies directory: {self.strategies_dir}"
                    f"{'' if self.strategies_dir.is_dir() else ' (missing)'}")

        self.artifact_dir.mkdir(parents=True, exist_ok=True)

        if self._labgrid:
            logger.info(f"labgrid coordinator: {self._labgrid.address}")
            await self._refresh_platforms()
        else:
            # QEMU-only lab: exactly the configured platforms
            logger.info("No labgrid coordinator: local (QEMU) platforms only")
            if not self.platforms:
                raise RuntimeError("No coordinator and no platforms configured: set "
                                   "`coordinator` for real hardware or `platforms` for QEMU")
            # (with a coordinator, _refresh_platforms logs the list)
            logger.info(f"Platforms: {', '.join(self.platforms)}")

        # Load health check configs if directory provided
        if self.health_checks_dir:
            self._load_health_configs()
            self._load_health_state()
            logger.info(f"Health checks enabled: {len(self._health_configs)} device(s)")

        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600))

        self._running = True
        logger.info("Agent started")

    async def stop(self) -> None:
        """Stop the agent."""
        logger.info("Stopping agent")
        self._running = False

        # Save health state
        self._save_health_state()

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

        if self._labgrid:
            await self._labgrid.close()

        logger.info("Agent stopped")

    async def run(self, once: bool = False) -> None:
        """Main loop - poll for jobs and run health checks.

        With once=True, do a single round and wait for started jobs.
        """
        if not self._running:
            await self.start()

        logger.info(f"Polling every {self.poll_interval}s")

        while self._running:
            try:
                # Run health checks if due
                await self._check_health()

                # Poll and execute KernelCI jobs
                await self._poll_and_execute()

            except Exception as e:
                logger.exception(f"Loop error: {e}")

            if once:
                while self._current_jobs:
                    await asyncio.sleep(1)
                break

            await asyncio.sleep(self.poll_interval)

    # ==================== Health Check Methods ====================
    #
    # Health results are kept local for now: they only gate job execution
    # and are persisted to health_state_file, not published to the API.

    def _load_health_configs(self) -> None:
        """Load health check configurations from directory."""
        self._health_configs.clear()

        if not self.health_checks_dir or not self.health_checks_dir.exists():
            return

        for yaml_file in self.health_checks_dir.glob("*.yaml"):
            try:
                config = HealthCheckConfig.from_yaml(yaml_file)
                self._health_configs[config.device] = config
                logger.info(f"Loaded health check: {config.device} (every {config.frequency_hours}h)")
            except Exception as e:
                logger.error(f"Failed to load {yaml_file}: {e}")

    def _load_health_state(self) -> None:
        """Load persisted health state."""
        if not self.health_state_file or not self.health_state_file.exists():
            return

        try:
            with open(self.health_state_file) as f:
                data = json.load(f)

            for device_data in data.get("devices", []):
                health = DeviceHealth.from_dict(device_data)
                self._device_health[health.device] = health

            logger.info(f"Loaded health state for {len(self._device_health)} device(s)")
        except Exception as e:
            logger.error(f"Failed to load health state: {e}")

    def _save_health_state(self) -> None:
        """Persist health state to file."""
        if not self.health_state_file:
            return

        try:
            self.health_state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "updated": datetime.now().isoformat(),
                "devices": [h.to_dict() for h in self._device_health.values()],
            }
            with open(self.health_state_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save health state: {e}")

    async def _check_health(self) -> None:
        """Check which devices need health checks and run them."""
        if not self._health_configs:
            return

        now = datetime.now()

        for device, config in self._health_configs.items():
            health = self._device_health.get(device, DeviceHealth(device=device))

            # Check if health check is due
            if health.last_check:
                next_check = health.last_check + timedelta(hours=config.frequency_hours)
                if now < next_check:
                    continue

            logger.info(f"Running health check for {device}")
            await self._run_health_check(device, config)

    async def _run_health_check(self, device: str, config: HealthCheckConfig) -> None:
        """Run a health check for a device."""
        health = self._device_health.get(device, DeviceHealth(device=device))
        workdir = self.artifact_dir / f"health-{device}"

        # Hardware: needs a free place; if busy, retry at the next poll
        lease = None
        if self._is_remote(device):
            lease = await self._reserve(device)
            if not lease:
                logger.info(f"Health check for {device} postponed: no free place")
                return

        try:
            target_yaml = self.targets_dir / config.target
            if not target_yaml.exists():
                raise RuntimeError(f"Target not found: {target_yaml}")

            extra_env = self._labgrid.env(lease) if lease else None

            firmware = await self._fetch_firmware(
                config.golden_image["firmware"],
                config.golden_image.get("sha256"),
                self.artifact_dir / "firmware",
            )

            result = await self._run_pytest(
                target_yaml=target_yaml,
                firmware=firmware,
                pytest_args=config.test_path,
                timeout=config.timeout,
                outdir=workdir,
                extra_env=extra_env,
            )

            # Update health state
            health.last_check = datetime.now()

            if result.success and result.returncode in PYTEST_RC_RAN:
                health.state = "good"
                health.last_success = datetime.now()
                health.failure_count = 0
                health.failure_reason = ""
                logger.info(f"Health check PASSED for {device}")
            else:
                health.state = "bad"
                health.failure_count += 1
                health.failure_reason = result.output[-500:] if result.output else "Test failed"
                logger.warning(f"Health check FAILED for {device}: {health.failure_reason}")

        except Exception as e:
            health.last_check = datetime.now()
            health.state = "bad"
            health.failure_count += 1
            health.failure_reason = str(e)
            logger.exception(f"Health check error for {device}")

        finally:
            if lease:
                await self._labgrid.release(lease)

        self._device_health[device] = health
        self._save_health_state()

    def is_device_healthy(self, device: str) -> bool:
        """Check if a device is healthy (good or unknown state)."""
        health = self._device_health.get(device)
        if not health:
            return True  # Unknown devices are assumed healthy
        return health.state != "bad"

    def get_device_health(self, device: str) -> DeviceHealth | None:
        """Get health state for a device."""
        return self._device_health.get(device)

    # ==================== API Methods ====================

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_token}"}

    async def _api_request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        """Request to the KernelCI API; raises with the API's error detail."""
        if not self._session:
            raise RuntimeError("Session not initialized")

        url = f"{self.api_url}{endpoint}"
        async with self._session.request(method, url, headers=self._auth, **kwargs) as resp:
            if resp.status not in (200, 201):
                raise RuntimeError(f"API {method} {endpoint}: {resp.status} {await resp.text()}")
            return await resp.json()

    async def _api_get(self, endpoint: str, **params: Any) -> Any:
        """GET request to KernelCI API."""
        return await self._api_request("GET", endpoint, params=params)

    async def _api_put(self, endpoint: str, data: dict[str, Any]) -> Any:
        """PUT request to KernelCI API."""
        return await self._api_request("PUT", endpoint, json=data)

    async def _api_post(self, endpoint: str, data: dict[str, Any]) -> Any:
        """POST request to KernelCI API."""
        return await self._api_request("POST", endpoint, json=data)

    @staticmethod
    def _writable(node: dict[str, Any]) -> dict[str, Any]:
        """Strip alias/extra fields before PUTting a node back."""
        return {k: v for k, v in node.items() if k not in NODE_READ_ONLY_FIELDS}

    # ==================== Job Processing ====================

    def _is_remote(self, platform: str) -> bool:
        """Hardware target (labgrid RemotePlace) or local (QEMU)?"""
        target = self._find_target_yaml(platform)
        return bool(target) and "RemotePlace" in target.read_text()

    async def _refresh_platforms(self) -> None:
        """Hardware platforms = device tags of the coordinator's places.

        Configured platforms restrict the hardware list and add local (QEMU)
        targets. On coordinator errors the previous list is kept.
        """
        try:
            self._places = await self._labgrid.places()
        except Exception as e:
            logger.error(f"Cannot list labgrid places: {e}")
            return
        hardware = {p.device for p in self._places if p.device}
        missing = sorted(d for d in hardware if not self._find_target_yaml(d))
        if missing:
            logger.debug(f"Places without a target file: {', '.join(missing)}")
        hardware = {d for d in hardware if d not in missing}
        if self.configured_platforms:
            hardware &= set(self.configured_platforms)
        local = {p for p in self.configured_platforms
                 if self._find_target_yaml(p) and not self._is_remote(p)}
        platforms = sorted(hardware | local)
        if platforms != self.platforms:
            logger.info(f"Platforms: {', '.join(platforms) or '-'}")
        self.platforms = platforms

    async def _poll_and_execute(self) -> None:
        """Poll for available jobs of our platforms and execute them."""
        if self._labgrid:
            await self._refresh_platforms()

        for platform in self.platforms:
            if platform in self._busy_platforms:
                continue

            if not self.is_device_healthy(platform):
                # Leave the job for another lab; the pipeline's timeout
                # service closes it if nobody picks it up.
                logger.debug(f"Skipping {platform}: device unhealthy")
                continue

            try:
                response = await self._api_get(
                    "/nodes",
                    kind="job",
                    state="available",
                    limit=10,
                    **{"data.runtime": self.lab_name, "data.platform": platform},
                )
            except Exception as e:
                logger.error(f"Failed to get jobs for {platform}: {e}")
                continue

            jobs = [j for j in response.get("items", [])
                    if j.get("id") and j["id"] not in self._current_jobs]
            if not jobs:
                continue

            # Hardware: hold a place before claiming, so a job is only taken
            # when it can run; otherwise it stays available for later
            lease = None
            if self._is_remote(platform):
                lease = await self._reserve(platform)
                if not lease:
                    continue

            for job in jobs:
                claimed = await self._claim(job)
                if not claimed:
                    continue
                self._current_jobs.add(claimed["id"])
                self._busy_platforms.add(platform)
                asyncio.create_task(self._execute_job(claimed, lease))
                break  # one job per platform at a time
            else:
                if lease:
                    await self._labgrid.release(lease)

    async def _reserve(self, platform: str) -> Lease | None:
        """Reserve and lock a free place for a hardware platform, or None."""
        if not self._labgrid:
            logger.warning(f"{platform} needs a labgrid place, but no coordinator is configured")
            return None
        if not any(p.free for p in self._places if p.device == platform):
            logger.debug(f"No free place for {platform}")
            return None
        try:
            return await self._labgrid.reserve(platform, self.reserve_timeout)
        except Exception as e:
            logger.error(f"Reserving a place for {platform} failed: {e}")
            return None

    async def _claim(self, job: dict[str, Any]) -> dict[str, Any] | None:
        """Claim a job by writing data.job_id.

        Best effort, like kernelci/pullab_cloud: kernelci-api has no
        compare-and-set yet, so two agents serving the same platform can still
        race. Replace with the atomic claim endpoint (plan.md, phase 2).
        """
        node_id = job["id"]
        try:
            current = await self._api_get(f"/node/{node_id}")
        except Exception as e:
            logger.warning(f"Could not re-read job {node_id}: {e}")
            return None

        if current.get("state") != "available":
            return None
        if current.get("data", {}).get("job_id"):
            logger.debug(f"Job {node_id} already claimed: {current['data']['job_id']}")
            return None

        current.setdefault("data", {})["job_id"] = f"{self.lab_name}:{uuid.uuid4().hex}"
        current["data"]["worker"] = self.worker_id
        try:
            claimed = await self._api_put(f"/node/{node_id}", self._writable(current))
        except Exception as e:
            logger.warning(f"Failed to claim job {node_id}: {e}")
            return None

        logger.info(f"Claimed {current.get('name')} ({node_id}) for {current['data'].get('platform')}")
        return claimed

    async def _execute_job(self, job: dict[str, Any], lease: Lease | None = None) -> None:
        """Execute a test job (on a reserved, locked labgrid place if leased)."""
        node_id = job["id"]
        job_name = job.get("name", "unknown")
        platform = job.get("data", {}).get("platform", "")
        outdir = self.artifact_dir / node_id

        logger.info(f"Executing: {job_name} ({node_id}) on {platform}")

        try:
            target_yaml = self._find_target_yaml(platform)
            if not target_yaml:
                raise RuntimeError(f"No target YAML found for platform: {platform}")

            extra_env = None
            if lease:
                self._job_places[node_id] = lease.place
                extra_env = self._labgrid.env(lease)

            job_def = await self._fetch_json(job["artifacts"]["job_definition"])
            test = (job_def.get("tests") or [{}])[0]
            firmware = await self._fetch_firmware(
                job_def["artifacts"]["firmware"],
                job_def.get("integrity", {}).get("sha256", {}).get("firmware"),
                self.artifact_dir / "firmware",
            )

            result = await self._run_pytest(
                target_yaml=target_yaml,
                firmware=firmware,
                pytest_args=test.get("parameters", ""),
                timeout=test.get("timeout_s", self.default_timeout),
                outdir=outdir,
                extra_env=extra_env,
            )

            await self._report_results(job, result)

            logger.info(f"Completed {job_name}: {self._job_result(result)} "
                        f"({result.passed} passed, {result.failed} failed, "
                        f"{result.skipped} skipped)")

        except Exception as e:
            logger.exception(f"Job failed: {job_name}")
            await self._report_failure(job, str(e), outdir)

        finally:
            if lease:
                await self._labgrid.release(lease)
            self._job_places.pop(node_id, None)
            self._current_jobs.discard(node_id)
            self._busy_platforms.discard(platform)

    # ==================== Artifact Management ====================

    async def _fetch_json(self, url: str) -> dict[str, Any]:
        if not self._session:
            raise RuntimeError("Session not initialized")
        async with self._session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"GET {url}: HTTP {resp.status}")
            return await resp.json(content_type=None)

    async def _fetch_firmware(self, url: str, sha256: str | None, dest_dir: Path) -> Path:
        """Download firmware (cached by name), verify sha256, gunzip .gz."""
        if not self._session:
            raise RuntimeError("Session not initialized")

        # Keyed by checksum: main snapshot images have the same file name for
        # every build, a name-only cache would serve a stale image.
        dest_dir = dest_dir / (sha256[:16] if sha256 else "unverified")
        dest_dir.mkdir(parents=True, exist_ok=True)
        self._make_traversable(dest_dir)
        path = dest_dir / url.rsplit("/", 1)[-1]

        if not path.exists():
            logger.info(f"Downloading {url}")
            tmp = path.with_suffix(path.suffix + ".part")
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"GET {url}: HTTP {resp.status}")
                with tmp.open("wb") as f:
                    async for chunk in resp.content.iter_chunked(1 << 20):
                        f.write(chunk)
            tmp.rename(path)

        if sha256:
            digest = await asyncio.to_thread(self._sha256, path)
            if digest != sha256:
                path.unlink()
                raise RuntimeError(f"sha256 mismatch for {path.name}: {digest}")

        if path.suffix == ".gz":
            unpacked = path.with_suffix("")
            if not unpacked.exists():
                # OpenWrt images carry trailing metadata that Python's gzip
                # rejects; gzip(1) warns about it and still unpacks.
                proc = await asyncio.create_subprocess_exec(
                    "gzip", "-dkf", str(path),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                if not unpacked.exists():
                    raise RuntimeError(f"Failed to unpack {path.name}")
            path = unpacked

        # labgrid stages firmware for TFTP by symlinking it when the exporter
        # is on this host, so the TFTP server's user must be able to read it
        path.chmod(0o644)
        return path

    def _make_traversable(self, directory: Path) -> None:
        """chmod 0755 from artifact_dir down to `directory` (mkdtemp is 0700)."""
        directory = directory.resolve()
        root = self.artifact_dir.resolve()
        chain = [directory, *directory.parents]
        for d in chain[: chain.index(root) + 1] if root in chain else [directory]:
            d.chmod(0o755)

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    async def _upload_logs(self, node_id: str, outdir: Path) -> dict[str, str]:
        """Upload console log, pytest log and JUnit XML to kernelci-storage."""
        artifacts: dict[str, str] = {}
        if not (self.storage_url and self.storage_token and self._session):
            return artifacts

        uploads = [(p, "test_log") for p in sorted(outdir.glob("console_*"))[:1]]
        uploads += [(outdir / "pytest.log", "pytest_log"), (outdir / "results.xml", "junit")]
        dest = f"logs/{self.lab_name}/{node_id}"

        for path, key in uploads:
            if not path.exists():
                continue
            form = aiohttp.FormData()
            form.add_field("path", dest)
            form.add_field("file0", path.read_bytes(), filename=path.name)
            try:
                async with self._session.post(
                    f"{self.storage_url}/v1/file",
                    data=form,
                    headers={"Authorization": f"Bearer {self.storage_token}"},
                ) as resp:
                    if resp.status not in (200, 201):
                        raise RuntimeError(f"HTTP {resp.status} {await resp.text()}")
                artifacts[key] = f"{self.storage_url}/{dest}/{path.name}"
            except Exception as e:
                logger.error(f"Failed to upload {path.name}: {e}")

        # KCIDB takes the log excerpt from test_log; fall back to pytest output
        if "test_log" not in artifacts and "pytest_log" in artifacts:
            artifacts["test_log"] = artifacts["pytest_log"]
        return artifacts

    # ==================== Test Execution ====================

    def _find_target_yaml(self, platform: str) -> Path | None:
        """Find labgrid target YAML for platform."""
        # Try exact match first
        candidates = [
            self.targets_dir / f"{platform}.yaml",
            self.targets_dir / f"{platform}.yml",
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

    def _selection(self, pytest_args: str) -> list[str]:
        """Job selection relative to tests_dir; empty = all tests.

        Jobs created against the openwrt-tests repo layout say `tests/...`;
        strip that prefix when tests_dir itself has no tests/ subdirectory.
        """
        args = shlex.split(pytest_args or "")
        if not (self.tests_dir / "tests").is_dir():
            args = [a[len("tests/"):] if a.startswith("tests/") else a for a in args]
            args = [a for a in args if a not in ("tests", "")]
        return args or ["."]

    def _stage_target(self, target_yaml: Path, stage: Path) -> Path:
        """Symlink the target file and strategies into a per-run directory.

        Target files import strategies relative to themselves
        (`../strategies/tftpstrategy.py`), and labgrid resolves that against
        the target file's path without resolving symlinks. So
        <stage>/targets/<file> -> target and <stage>/strategies ->
        strategies_dir let targets and strategies live anywhere.
        """
        targets = stage / "targets"
        targets.mkdir(parents=True, exist_ok=True)
        staged = targets / target_yaml.name
        strategies = stage / "strategies"
        for link, dest in ((staged, target_yaml.resolve()), (strategies, self.strategies_dir.resolve())):
            if link.is_symlink() or link.exists():
                link.unlink()
            if dest.exists():
                link.symlink_to(dest)
        return staged

    async def _run_pytest(
        self,
        target_yaml: Path,
        firmware: Path,
        pytest_args: str,
        timeout: int,
        outdir: Path,
        extra_env: dict[str, str] | None = None,
    ) -> TestResult:
        """Run pytest with the labgrid environment in the tests repo."""
        outdir.mkdir(parents=True, exist_ok=True)
        junit_xml = outdir / "results.xml"
        pytest_log = outdir / "pytest.log"

        cmd = [
            *self.pytest_command,
            *self._selection(pytest_args),
            "--lg-env", str(self._stage_target(target_yaml, outdir / "env")),
            "--firmware", str(firmware),
            f"--lg-log={outdir}",
            f"--junit-xml={junit_xml}",
            "--log-cli-level=CONSOLE",
            "--tb=short",
            "-v",
            "-p", "no:cacheprovider",
        ]

        env = child_env()
        env["LG_IMAGE"] = str(firmware)
        env.update(extra_env or {})

        logger.info(f"Running: {shlex.join(cmd)}")

        returncode: int | None = None
        try:
            with pytest_log.open("wb") as log:
                # Own process group: on timeout also stop QEMU and anything a
                # wrapper like `uv run` started, so they don't hold the device
                proc = await spawn(
                    *cmd,
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=self.tests_dir,
                    env=env,
                )
                try:
                    returncode = await asyncio.wait_for(proc.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    await kill_group(proc)
                    log.write(f"\nlabgrid-agent: timed out after {timeout}s\n".encode())
        except Exception as e:
            return TestResult(errors=1, output=str(e), outdir=outdir)

        output = pytest_log.read_text(errors="replace")
        if returncode is None:
            return TestResult(errors=1, output=output, outdir=outdir)

        result = self._parse_junit_xml(junit_xml)
        result.output = output
        result.returncode = returncode
        result.outdir = outdir
        logger.info(f"pytest exited with {returncode}")
        return result

    def _parse_junit_xml(self, junit_xml: Path) -> TestResult:
        """Parse JUnit XML output from pytest."""
        result = TestResult()

        if not junit_xml.exists():
            logger.warning(f"JUnit XML file not found: {junit_xml}")
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
                result.total += int(testsuite.get("tests", 0))
                result.errors += int(testsuite.get("errors", 0))
                result.failed += int(testsuite.get("failures", 0))
                result.skipped += int(testsuite.get("skipped", 0))
                result.duration += float(testsuite.get("time", 0))

                for testcase in testsuite.findall("testcase"):
                    name = testcase.get("name", "unknown")
                    classname = testcase.get("classname", "")
                    duration = float(testcase.get("time", 0))

                    if testcase.find("failure") is not None:
                        tc_result = "fail"
                        failure = testcase.find("failure")
                        message = failure.get("message", "") if failure is not None else ""
                    elif testcase.find("error") is not None:
                        tc_result = "error"
                        error = testcase.find("error")
                        message = error.get("message", "") if error is not None else ""
                    elif testcase.find("skipped") is not None:
                        tc_result = "skip"
                        skipped = testcase.find("skipped")
                        message = skipped.get("message", "") if skipped is not None else ""
                    else:
                        tc_result = "pass"
                        message = ""

                    result.test_cases.append({
                        "name": name,
                        "classname": classname,
                        "module": self._module_name(classname),
                        "result": tc_result,
                        "duration": duration,
                        "message": message,
                    })

            result.passed = result.total - result.failed - result.errors - result.skipped

        except ET.ParseError as e:
            logger.error(f"Failed to parse JUnit XML: {e}")
            result.errors = 1

        return result

    @staticmethod
    def _module_name(classname: str) -> str:
        """'tests.test_base' / 'tests.test_wifi.TestAp' -> 'base' / 'wifi'."""
        parts = [p for p in classname.split(".") if p]
        if not parts:
            return "tests"
        module = parts[1] if parts[0] == "tests" and len(parts) > 1 else parts[0]
        return module.removeprefix("test_") or module

    # ==================== Result Reporting ====================

    @staticmethod
    def _job_result(result: TestResult) -> str:
        if result.returncode == PYTEST_RC_NO_SHELL:
            return "fail"
        if result.returncode not in PYTEST_RC_RAN:
            return "incomplete"
        if result.total == 0:
            return "incomplete"
        return "fail" if result.failed or result.errors else result.result

    def _build_hierarchy(self, job: dict[str, Any], result: TestResult,
                         artifacts: dict[str, str]) -> dict[str, Any]:
        """Job node + one child per test module + one leaf per test."""
        job_path = job["path"]
        node_data = self._node_data(job)

        modules: dict[str, list[dict[str, Any]]] = {}
        for tc in result.test_cases:
            modules.setdefault(tc["module"], []).append(tc)

        children = []
        for module, cases in modules.items():
            leaves = []
            for tc in cases:
                data: dict[str, Any] = {**node_data, "duration_ms": int(tc["duration"] * 1000)}
                if tc.get("message"):
                    data["error_msg"] = tc["message"][:1000]
                leaves.append({"node": {
                    "name": tc["name"],
                    "kind": "test",
                    "path": job_path + [module, tc["name"]],
                    "state": "done",
                    "result": self._map_outcome(tc["result"]),
                    "data": data,
                }, "child_nodes": []})
            children.append({"node": {
                "name": module,
                "kind": "test",
                "path": job_path + [module],
                "state": "done",
                "result": self._aggregate([leaf["node"]["result"] for leaf in leaves]),
                "data": dict(node_data),
            }, "child_nodes": leaves})

        node = self._writable(job)
        node["state"] = "done"
        node["result"] = self._job_result(result)
        node["artifacts"] = {**(job.get("artifacts") or {}), **artifacts}
        node["data"] = {**(job.get("data") or {}), **node_data,
                        "duration_ms": int(result.duration * 1000),
                        # lets dashboards show counts without fetching the tests
                        "summary": {
                            "total": result.total,
                            "passed": result.passed,
                            "failed": result.failed,
                            "errors": result.errors,
                            "skipped": result.skipped,
                            "boot": result.boot_result,
                        }}
        if result.returncode == PYTEST_RC_NO_SHELL:
            node["data"]["error_msg"] = "firmware did not reach a shell"
        elif node["result"] == "incomplete":
            node["data"]["error_code"] = "Infrastructure"
            node["data"]["error_msg"] = (f"pytest exited with {result.returncode}"
                                         if result.returncode is not None
                                         else "pytest did not finish")
        return {"node": node, "child_nodes": children}

    def _node_data(self, job: dict[str, Any]) -> dict[str, Any]:
        data = job.get("data") or {}
        platform = data.get("platform")
        return {
            "runtime": self.lab_name,
            "platform": platform,
            "arch": data.get("arch"),
            # the labgrid place for hardware, <lab>-<platform> for QEMU
            "device": self._job_places.get(job.get("id"), f"{self.lab_name}-{platform}"),
        }

    @staticmethod
    def _aggregate(results: list[str]) -> str:
        if "fail" in results:
            return "fail"
        if "incomplete" in results:
            return "incomplete"
        if "pass" in results:
            return "pass"
        return "skip" if results else "incomplete"

    async def _report_results(self, job: dict[str, Any], result: TestResult) -> None:
        """Report test results to the KernelCI API."""
        node_id = job["id"]
        artifacts = await self._upload_logs(node_id, result.outdir) if result.outdir else {}

        hierarchy = self._build_hierarchy(job, result, artifacts)
        await self._api_put(f"/nodes/{node_id}", hierarchy)

        # Sibling "boot" node under the build: the dashboard counts only
        # KCIDB paths "boot"/"boot.*" as boots.
        if job.get("parent") and result.returncode is not None:
            boot_artifacts = {k: v for k, v in artifacts.items() if k == "test_log"}
            await self._api_post("/node", {
                "kind": "test",
                "name": "boot",
                "path": job["path"][:-1] + ["boot"],
                "parent": job["parent"],
                "state": "done",
                "result": result.boot_result,
                "artifacts": boot_artifacts,
                "data": self._node_data(job),
            })

    async def _report_failure(self, job: dict[str, Any], error: str,
                              outdir: Path | None = None) -> None:
        """Report an infrastructure failure (tests never ran or crashed)."""
        node_id = job["id"]
        try:
            artifacts = await self._upload_logs(node_id, outdir) if outdir and outdir.exists() else {}
            current = await self._api_get(f"/node/{node_id}")
            current["state"] = "done"
            current["result"] = "incomplete"
            current["artifacts"] = {**(current.get("artifacts") or {}), **artifacts}
            current["data"] = {**(current.get("data") or {}), **self._node_data(job),
                               "error_code": "Infrastructure", "error_msg": error[:1000]}
            await self._api_put(f"/node/{node_id}", self._writable(current))
        except Exception as e:
            logger.error(f"Failed to report failure: {e}")

    @staticmethod
    def _map_outcome(outcome: str) -> str:
        """Map pytest/JUnit outcome to KernelCI result."""
        mapping = {
            "pass": "pass",
            "fail": "fail",
            "skip": "skip",
            "error": "incomplete",
            "passed": "pass",
            "failed": "fail",
            "skipped": "skip",
            "xfailed": "skip",
            "xpassed": "pass",
        }
        return mapping.get(outcome.lower(), "skip")


# Keep old name for backwards compatibility
LabgridPullAgent = LabgridAgent


# ==================== CLI ====================


def main() -> None:
    """CLI entry point."""
    import argparse

    from kernelci_labgrid.config import describe, find_config, resolve_settings

    parser = argparse.ArgumentParser(
        description="KernelCI Labgrid Agent - polls for jobs & runs health checks",
        epilog="Settings come from (later wins): a TOML config file, --env-file, "
               "environment variables (LAB_NAME, KCI_API_URL, LAB_API_TOKEN, "
               "KCI_STORAGE_URL, LAB_STORAGE_TOKEN, LG_COORDINATOR) and these options. The config "
               "file is --config, $LABGRID_AGENT_CONFIG, ./labgrid-agent.toml, "
               "~/.config/labgrid-agent/config.toml or /etc/labgrid-agent/config.toml.",
    )
    parser.add_argument("--config", help="TOML config file (see above)")
    parser.add_argument("--env-file", help="KEY=VALUE file with the variables above")
    parser.add_argument("--show-config", action="store_true",
                        help="Print the effective settings (secrets masked) and exit")

    # All defaults are None: unset options don't override other sources
    parser.add_argument("--lab-name", "-l", help="Lab name, matched against the job's data.runtime")
    parser.add_argument("--tests-dir", "-t", help="Tests directory (e.g. openwrt-tests/tests)")
    parser.add_argument("--targets-dir", help="labgrid targets (default: tests_dir/../targets)")
    parser.add_argument("--strategies-dir",
                        help="labgrid strategies imported by the targets (default: targets_dir/../strategies)")
    parser.add_argument("--api-url", help="KernelCI API URL including version")
    parser.add_argument("--api-token", help="Lab API token")
    parser.add_argument("--storage-url", help="kernelci-storage URL for log uploads")
    parser.add_argument("--storage-token", help="kernelci-storage JWT")
    parser.add_argument("--platform", dest="platforms", action="append",
                        help="Platform served by this lab (targets/<name>.yaml); repeatable")
    parser.add_argument("--pytest-command", help="Command to run pytest in tests_dir "
                                                 "(default: python -m pytest of this environment)")
    parser.add_argument("--artifact-dir", "-a", help="Directory for firmware and job outputs")
    parser.add_argument("--poll-interval", "-p", type=int, help="Seconds between polls (default: 30)")
    parser.add_argument("--once", action="store_true", help="Poll once, wait for started jobs, exit")
    parser.add_argument("--health-checks-dir", "-c",
                        help="Directory with health check YAML configs (enables health checks)")
    parser.add_argument("--health-state-file", "-s", help="JSON file to persist device health state")
    parser.add_argument("--coordinator", help="The lab's labgrid-coordinator host:port (default: "
                                              "127.0.0.1:20408 or LG_COORDINATOR; '' for QEMU only)")
    parser.add_argument("--labgrid-command", help="labgrid-client command for power off "
                                                  "(default: labgrid-client of this environment)")
    parser.add_argument("--reserve-timeout", type=int,
                        help="Seconds to wait for a free place before leaving a job (default: 60)")
    parser.add_argument("--debug", "-d", action="store_true", help="Debug logging")

    args = parser.parse_args()
    meta = {"config", "env_file", "show_config", "once", "debug"}
    cli = {k: v for k, v in vars(args).items() if k not in meta}

    config_path = find_config(args.config)
    try:
        settings = resolve_settings(
            cli, config_path=config_path,
            env_file=Path(args.env_file) if args.env_file else None,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    if args.show_config:
        print(f"# config file: {config_path or '-'}")
        print(describe(settings))
        return

    for key, hint in (("lab_name", "--lab-name / lab_name"),
                      ("api_token", "--api-token / [api] token"),
                      ("tests_dir", "--tests-dir / tests_dir")):
        if not settings[key]:
            parser.error(f"{key} required ({hint})")

    if not settings["coordinator"] and not settings["platforms"]:
        parser.error("no coordinator and no platforms: set `coordinator` (real hardware) "
                     "or `platforms` (QEMU-only lab, with coordinator = \"\")")

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if config_path:
        logger.info(f"Config file: {config_path}")

    agent = LabgridAgent(
        api_url=settings["api_url"],
        api_token=settings["api_token"],
        lab_name=settings["lab_name"],
        tests_dir=Path(settings["tests_dir"]).resolve(),
        targets_dir=settings["targets_dir"],
        strategies_dir=settings["strategies_dir"],
        storage_url=settings["storage_url"],
        storage_token=settings["storage_token"],
        platforms=settings["platforms"],
        pytest_command=settings["pytest_command"],
        poll_interval=int(settings["poll_interval"]),
        artifact_dir=settings["artifact_dir"],
        health_checks_dir=settings["health_checks_dir"],
        health_state_file=settings["health_state_file"],
        coordinator=settings["coordinator"],
        labgrid_command=settings["labgrid_command"],
        reserve_timeout=int(settings["reserve_timeout"]),
    )

    # Signal handling
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(agent.stop()))

    try:
        loop.run_until_complete(agent.run(once=args.once))
        loop.run_until_complete(agent.stop())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
