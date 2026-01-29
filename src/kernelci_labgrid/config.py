"""Configuration management for KernelCI Labgrid Scheduler.

This module handles loading and validating configuration files for
runtime and scheduler settings.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class SSHConfig:
    """SSH connection configuration."""

    proxy_host: str | None = None
    proxy_user: str = "root"
    key_file: str | None = None
    timeout: int = 30


@dataclass
class StorageConfig:
    """Artifact storage configuration."""

    type: str = "http"  # http, ssh, nfs
    host: str | None = None
    path: str = "/srv/labgrid/artifacts"
    user: str = "root"
    key_file: str | None = None
    nfs_path: str | None = None


@dataclass
class AuthConfig:
    """Authentication configuration."""

    type: str = "none"  # none, token, mtls
    token: str | None = None
    cert_file: str | None = None
    key_file: str | None = None
    ca_file: str | None = None


@dataclass
class PlaceMappingConfig:
    """Place to platform mapping configuration."""

    place: str
    platform: str
    compatible: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class RuntimeConfig:
    """Configuration for a single Labgrid runtime."""

    name: str
    lab_type: str = "labgrid"
    coordinator_address: str = "localhost:20408"
    mode: str = "push"  # push or pull
    secure: bool = False
    auth: AuthConfig = field(default_factory=AuthConfig)
    ssh: SSHConfig = field(default_factory=SSHConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    place_mapping: list[PlaceMappingConfig] = field(default_factory=list)


@dataclass
class EventConfig:
    """Event trigger configuration for scheduler."""

    channel: str = "node"
    name: str = ""  # Node name pattern to match
    result: str = "pass"  # Required result to trigger


@dataclass
class SchedulerJobConfig:
    """Configuration for a scheduled job."""

    job: str
    event: EventConfig
    runtime_type: str
    runtime_name: str
    platforms: list[str] = field(default_factory=list)
    test_suite: str = "baseline"
    test_config: dict[str, Any] = field(default_factory=dict)
    timeout: int = 3600
    priority: int = 50


@dataclass
class LabgridConfig:
    """Main configuration container."""

    runtimes: dict[str, RuntimeConfig] = field(default_factory=dict)
    scheduler_jobs: list[SchedulerJobConfig] = field(default_factory=list)
    api_url: str = "https://api.kernelci.org"
    api_token: str = ""
    poll_interval: int = 30  # For pull mode

    @classmethod
    def from_files(
        cls,
        runtime_config_path: str | Path | None = None,
        scheduler_config_path: str | Path | None = None,
    ) -> "LabgridConfig":
        """Load configuration from YAML files.

        Args:
            runtime_config_path: Path to runtime configuration file
            scheduler_config_path: Path to scheduler configuration file

        Returns:
            LabgridConfig instance
        """
        config = cls()

        if runtime_config_path:
            config._load_runtime_config(Path(runtime_config_path))

        if scheduler_config_path:
            config._load_scheduler_config(Path(scheduler_config_path))

        # Load from environment
        config._load_from_env()

        return config

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LabgridConfig":
        """Create configuration from dictionary.

        Args:
            data: Configuration dictionary

        Returns:
            LabgridConfig instance
        """
        config = cls()

        # Load runtimes
        for name, runtime_data in data.get("runtimes", {}).items():
            config.runtimes[name] = cls._parse_runtime(name, runtime_data)

        # Load scheduler jobs
        for job_data in data.get("scheduler", []):
            config.scheduler_jobs.append(cls._parse_scheduler_job(job_data))

        # Load API settings
        config.api_url = data.get("api_url", config.api_url)
        config.api_token = data.get("api_token", config.api_token)
        config.poll_interval = data.get("poll_interval", config.poll_interval)

        return config

    def _load_runtime_config(self, path: Path) -> None:
        """Load runtime configuration from file."""
        if not path.exists():
            logger.warning(f"Runtime config file not found: {path}")
            return

        with open(path) as f:
            data = yaml.safe_load(f)

        for name, runtime_data in data.get("runtimes", {}).items():
            self.runtimes[name] = self._parse_runtime(name, runtime_data)

        logger.info(f"Loaded {len(self.runtimes)} runtime(s) from {path}")

    def _load_scheduler_config(self, path: Path) -> None:
        """Load scheduler configuration from file."""
        if not path.exists():
            logger.warning(f"Scheduler config file not found: {path}")
            return

        with open(path) as f:
            data = yaml.safe_load(f)

        for job_data in data.get("scheduler", []):
            self.scheduler_jobs.append(self._parse_scheduler_job(job_data))

        logger.info(f"Loaded {len(self.scheduler_jobs)} scheduler job(s) from {path}")

    def _load_from_env(self) -> None:
        """Load configuration from environment variables."""
        self.api_url = os.environ.get("KCI_API_URL", self.api_url)
        self.api_token = os.environ.get("KCI_API_TOKEN", self.api_token)
        self.poll_interval = int(os.environ.get("POLL_INTERVAL", self.poll_interval))

        # Allow overriding coordinator address from env
        coordinator = os.environ.get("LABGRID_COORDINATOR")
        if coordinator and self.runtimes:
            # Update first runtime's coordinator
            first_runtime = next(iter(self.runtimes.values()))
            first_runtime.coordinator_address = coordinator

    @staticmethod
    def _parse_runtime(name: str, data: dict[str, Any]) -> RuntimeConfig:
        """Parse runtime configuration from dict."""
        # Expand environment variables in values
        data = LabgridConfig._expand_env_vars(data)

        auth_data = data.get("auth", {})
        auth = AuthConfig(
            type=auth_data.get("type", "none"),
            token=auth_data.get("token"),
            cert_file=auth_data.get("cert_file"),
            key_file=auth_data.get("key_file"),
            ca_file=auth_data.get("ca_file"),
        )

        ssh_data = data.get("ssh", {})
        ssh = SSHConfig(
            proxy_host=ssh_data.get("proxy_host"),
            proxy_user=ssh_data.get("proxy_user", "root"),
            key_file=ssh_data.get("key_file"),
            timeout=ssh_data.get("timeout", 30),
        )

        storage_data = data.get("storage", {})
        storage = StorageConfig(
            type=storage_data.get("type", "http"),
            host=storage_data.get("host"),
            path=storage_data.get("path", "/srv/labgrid/artifacts"),
            user=storage_data.get("user", "root"),
            key_file=storage_data.get("key_file"),
            nfs_path=storage_data.get("nfs_path"),
        )

        place_mapping = []
        for pm_data in data.get("place_mapping", []):
            place_mapping.append(
                PlaceMappingConfig(
                    place=pm_data["place"],
                    platform=pm_data["platform"],
                    compatible=pm_data.get("compatible", []),
                    tags=pm_data.get("tags", {}),
                )
            )

        return RuntimeConfig(
            name=name,
            lab_type=data.get("lab_type", "labgrid"),
            coordinator_address=data.get("coordinator_address", "localhost:20408"),
            mode=data.get("mode", "push"),
            secure=data.get("secure", False),
            auth=auth,
            ssh=ssh,
            storage=storage,
            place_mapping=place_mapping,
        )

    @staticmethod
    def _parse_scheduler_job(data: dict[str, Any]) -> SchedulerJobConfig:
        """Parse scheduler job configuration from dict."""
        event_data = data.get("event", {})
        event = EventConfig(
            channel=event_data.get("channel", "node"),
            name=event_data.get("name", ""),
            result=event_data.get("result", "pass"),
        )

        runtime_data = data.get("runtime", {})

        return SchedulerJobConfig(
            job=data["job"],
            event=event,
            runtime_type=runtime_data.get("type", "labgrid"),
            runtime_name=runtime_data.get("name", ""),
            platforms=data.get("platforms", []),
            test_suite=data.get("test_suite", "baseline"),
            test_config=data.get("test_config", {}),
            timeout=data.get("timeout", 3600),
            priority=data.get("priority", 50),
        )

    @staticmethod
    def _expand_env_vars(data: Any) -> Any:
        """Recursively expand environment variables in configuration values.

        Supports ${VAR} syntax.
        """
        if isinstance(data, str):
            # Pattern: ${VAR_NAME}
            pattern = r"\$\{([^}]+)\}"

            def replace(match: re.Match) -> str:
                var_name = match.group(1)
                return os.environ.get(var_name, match.group(0))

            return re.sub(pattern, replace, data)

        elif isinstance(data, dict):
            return {k: LabgridConfig._expand_env_vars(v) for k, v in data.items()}

        elif isinstance(data, list):
            return [LabgridConfig._expand_env_vars(item) for item in data]

        return data

    def get_runtime(self, name: str) -> RuntimeConfig | None:
        """Get runtime configuration by name."""
        return self.runtimes.get(name)

    def get_jobs_for_event(
        self, channel: str, name: str, result: str
    ) -> list[SchedulerJobConfig]:
        """Get scheduler jobs that should trigger for an event.

        Args:
            channel: Event channel
            name: Event name (node name)
            result: Event result

        Returns:
            List of matching scheduler jobs
        """
        matching = []

        for job in self.scheduler_jobs:
            if job.event.channel != channel:
                continue
            if job.event.result != result:
                continue

            # Check if name matches (supports wildcards)
            pattern = job.event.name.replace("*", ".*")
            if re.match(pattern, name):
                matching.append(job)

        return matching

    def to_dict(self) -> dict[str, Any]:
        """Convert configuration to dictionary."""
        return {
            "runtimes": {
                name: {
                    "lab_type": rt.lab_type,
                    "coordinator_address": rt.coordinator_address,
                    "mode": rt.mode,
                    "secure": rt.secure,
                    "place_mapping": [
                        {
                            "place": pm.place,
                            "platform": pm.platform,
                            "compatible": pm.compatible,
                            "tags": pm.tags,
                        }
                        for pm in rt.place_mapping
                    ],
                }
                for name, rt in self.runtimes.items()
            },
            "scheduler": [
                {
                    "job": job.job,
                    "event": {
                        "channel": job.event.channel,
                        "name": job.event.name,
                        "result": job.event.result,
                    },
                    "runtime": {
                        "type": job.runtime_type,
                        "name": job.runtime_name,
                    },
                    "platforms": job.platforms,
                    "test_suite": job.test_suite,
                    "timeout": job.timeout,
                }
                for job in self.scheduler_jobs
            ],
            "api_url": self.api_url,
            "poll_interval": self.poll_interval,
        }


def load_config(
    runtime_path: str | Path | None = None,
    scheduler_path: str | Path | None = None,
) -> LabgridConfig:
    """Convenience function to load configuration.

    Args:
        runtime_path: Path to runtime config (default: config/labgrid-runtime.yaml)
        scheduler_path: Path to scheduler config (default: config/labgrid-scheduler.yaml)

    Returns:
        Loaded LabgridConfig
    """
    # Use default paths if not specified
    if runtime_path is None:
        runtime_path = Path("config/labgrid-runtime.yaml")
    if scheduler_path is None:
        scheduler_path = Path("config/labgrid-scheduler.yaml")

    return LabgridConfig.from_files(runtime_path, scheduler_path)
