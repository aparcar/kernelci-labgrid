#!/usr/bin/env python3
"""OpenWrt Build Monitor for KernelCI Maestro Integration.

This script monitors OpenWrt downloads for new builds by checking the
version.buildinfo file. When a new build is detected, it creates a
checkout node in KernelCI which triggers the test pipeline.

The version.buildinfo file contains a revision string like:
- Snapshots: r24106-10cc5fcd00
- Releases: v23.05.4

Usage:
    build_monitor.py [--interval MINUTES] [--once]

Environment:
    KCI_API_URL: KernelCI API URL (default: https://api.kernelci.org)
    KCI_API_TOKEN: KernelCI API token (required)
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class OpenWrtSource:
    """Configuration for an OpenWrt source to monitor."""

    name: str
    base_url: str
    branch: str
    platform: str
    tree: str = "openwrt"


# Default OpenWrt sources to monitor
DEFAULT_SOURCES = [
    OpenWrtSource(
        name="openwrt-main-x86",
        base_url="https://downloads.openwrt.org/snapshots/targets/x86/64/",
        branch="main",
        platform="x86_64",
        tree="openwrt",
    ),
    OpenWrtSource(
        name="openwrt-2512-x86",
        base_url="https://downloads.openwrt.org/releases/25.12-SNAPSHOT/targets/x86/64/",
        branch="openwrt-25.12",
        platform="x86_64",
        tree="openwrt",
    ),
]

# Pattern to extract image filename
ROOTFS_PATTERN = re.compile(
    r'href="(openwrt-[^"]+x86-64-generic-squashfs-combined-efi\.img\.gz)"'
)


class BuildState:
    """Persistent state tracking for monitored builds."""

    def __init__(self, state_file: str | Path):
        self.state_file = Path(state_file)
        self.state: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        """Load state from file."""
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    self.state = json.load(f)
                logger.debug(f"Loaded state from {self.state_file}")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load state: {e}")
                self.state = {}

    def _save(self) -> None:
        """Save state to file."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(self.state, f, indent=2)

    def get_version(self, source_name: str) -> str | None:
        """Get last known version for a source."""
        return self.state.get(source_name, {}).get("version")

    def set_version(
        self, source_name: str, version: str, node_id: str | None = None
    ) -> None:
        """Set version for a source."""
        self.state[source_name] = {
            "version": version,
            "node_id": node_id,
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        self._save()


class OpenWrtBuildMonitor:
    """Monitors OpenWrt downloads for new builds."""

    def __init__(
        self,
        api_url: str,
        api_token: str,
        state_file: str | Path,
        sources: list[OpenWrtSource] | None = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.api_token = api_token
        self.state = BuildState(state_file)
        self.sources = sources or DEFAULT_SOURCES

    def _fetch_url(self, url: str, timeout: int = 30) -> str | None:
        """Fetch content from URL."""
        try:
            req = Request(url, headers={"User-Agent": "KernelCI-BuildMonitor/1.0"})
            with urlopen(req, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except (URLError, HTTPError) as e:
            logger.error(f"Failed to fetch {url}: {e}")
            return None

    def get_version_info(self, source: OpenWrtSource) -> dict[str, str] | None:
        """Fetch version information for a source."""
        version_url = f"{source.base_url}version.buildinfo"
        content = self._fetch_url(version_url)

        if content is None:
            return None

        version = content.strip()
        if not version:
            logger.warning(f"Empty version.buildinfo for {source.name}")
            return None

        # Find the rootfs image
        index_content = self._fetch_url(source.base_url)
        if index_content is None:
            return None

        matches = ROOTFS_PATTERN.findall(index_content)
        if not matches:
            logger.warning(f"No rootfs image found for {source.name}")
            return None

        rootfs_name = matches[0]
        rootfs_url = f"{source.base_url}{rootfs_name}"

        # Generate a unique revision ID
        revision = hashlib.sha256(
            f"{source.name}:{version}".encode()
        ).hexdigest()[:12]

        return {
            "version": version,
            "revision": revision,
            "rootfs_url": rootfs_url,
            "rootfs_name": rootfs_name,
        }

    def create_checkout_node(
        self,
        source: OpenWrtSource,
        version_info: dict[str, str],
    ) -> dict[str, Any] | None:
        """Create a checkout node in KernelCI for the new build."""
        node_data = {
            "kind": "checkout",
            "name": f"{source.tree}-{source.branch}-{source.platform}",
            "path": ["checkout"],
            "state": "done",
            "result": "pass",
            "data": {
                "kernel_revision": {
                    "tree": source.tree,
                    "branch": source.branch,
                    "commit": version_info["revision"],
                    "describe": version_info["version"],
                    "url": "https://github.com/openwrt/openwrt",
                },
                "platform": source.platform,
            },
            "artifacts": {
                "rootfs": version_info["rootfs_url"],
            },
            "created": datetime.now(timezone.utc).isoformat(),
        }

        try:
            logger.info(f"Creating checkout node for {source.name} ({version_info['version']})")
            req = Request(
                f"{self.api_url}/api/node",
                data=json.dumps(node_data).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_token}",
                    "Content-Type": "application/json",
                    "User-Agent": "KernelCI-BuildMonitor/1.0",
                },
                method="POST",
            )
            with urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
                logger.info(f"Created checkout node: {result.get('id', 'unknown')}")
                return result
        except (URLError, HTTPError) as e:
            logger.error(f"Failed to create checkout node: {e}")
            return None

    def create_test_node(
        self,
        source: OpenWrtSource,
        version_info: dict[str, str],
        parent_id: str,
        lab_name: str = "openwrt-qemu-lab",
    ) -> dict[str, Any] | None:
        """Create a test job node linked to the checkout."""
        node_data = {
            "kind": "job",
            "name": f"openwrt-{source.branch}-boot-test",
            "path": ["checkout", "openwrt-boot-test"],
            "parent": parent_id,
            "state": "running",
            "data": {
                "lab": lab_name,
                "platform": source.name,
                "branch": source.branch,
                "version": version_info["version"],
                "artifacts": {
                    "rootfs": version_info["rootfs_url"],
                },
                "test_path": "test_openwrt.py",
                "timeout": 600,
            },
            "created": datetime.now(timezone.utc).isoformat(),
        }

        try:
            logger.info(f"Creating test job for {source.name}")
            req = Request(
                f"{self.api_url}/api/node",
                data=json.dumps(node_data).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_token}",
                    "Content-Type": "application/json",
                    "User-Agent": "KernelCI-BuildMonitor/1.0",
                },
                method="POST",
            )
            with urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
                logger.info(f"Created test job: {result.get('id', 'unknown')}")
                return result
        except (URLError, HTTPError) as e:
            logger.error(f"Failed to create test job: {e}")
            return None

    def check_source(self, source: OpenWrtSource, lab_name: str) -> bool:
        """Check a single source for new builds."""
        logger.info(f"Checking {source.name} at {source.base_url}")

        version_info = self.get_version_info(source)
        if version_info is None:
            logger.warning(f"Could not get version info for {source.name}")
            return False

        last_version = self.state.get_version(source.name)
        current_version = version_info["version"]

        if last_version == current_version:
            logger.info(f"{source.name}: No change (version: {current_version})")
            return False

        logger.info(
            f"{source.name}: New build detected! {last_version or 'none'} -> {current_version}"
        )

        # Create checkout node
        checkout = self.create_checkout_node(source, version_info)
        if checkout is None:
            logger.error(f"Failed to create checkout node for {source.name}")
            return False

        checkout_id = checkout.get("id")

        # Create test job
        if checkout_id:
            self.create_test_node(source, version_info, checkout_id, lab_name)

        # Update state
        self.state.set_version(source.name, current_version, checkout_id)
        return True

    def check_all_sources(self, lab_name: str) -> int:
        """Check all sources for new builds."""
        new_builds = 0
        for source in self.sources:
            try:
                if self.check_source(source, lab_name):
                    new_builds += 1
            except Exception as e:
                logger.error(f"Error checking {source.name}: {e}")
        return new_builds


def run_monitor(
    api_url: str,
    api_token: str,
    state_file: str,
    lab_name: str,
    interval_minutes: int = 60,
    run_once: bool = False,
) -> None:
    """Run the build monitor loop."""
    monitor = OpenWrtBuildMonitor(
        api_url=api_url,
        api_token=api_token,
        state_file=state_file,
    )

    logger.info("Starting OpenWrt Build Monitor")
    logger.info(f"  API URL: {api_url}")
    logger.info(f"  Lab name: {lab_name}")
    logger.info(f"  Interval: {interval_minutes} minutes")
    logger.info(f"  State file: {state_file}")
    logger.info(f"  Monitoring {len(monitor.sources)} sources")

    while True:
        try:
            logger.info("=" * 60)
            logger.info(f"Checking for new builds at {datetime.now(timezone.utc).isoformat()}")

            new_builds = monitor.check_all_sources(lab_name)
            logger.info(f"Found {new_builds} new build(s)")

            if run_once:
                logger.info("Run-once mode, exiting")
                break

            logger.info(f"Sleeping for {interval_minutes} minutes...")
            time.sleep(interval_minutes * 60)

        except KeyboardInterrupt:
            logger.info("Interrupted, shutting down")
            break
        except Exception as e:
            logger.error(f"Monitor error: {e}")
            if run_once:
                sys.exit(1)
            time.sleep(60)


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="OpenWrt Build Monitor for KernelCI"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Check interval in minutes (default: 60)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Check once and exit",
    )
    parser.add_argument(
        "--state-file",
        default="/var/lib/labgrid/build_state.json",
        help="Path to state file (default: /var/lib/labgrid/build_state.json)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    api_url = os.environ.get("KCI_API_URL", "https://api.kernelci.org")
    api_token = os.environ.get("KCI_API_TOKEN")
    lab_name = os.environ.get("LAB_NAME", "openwrt-qemu-lab")

    if not api_token:
        logger.error("KCI_API_TOKEN environment variable is required")
        sys.exit(1)

    run_monitor(
        api_url=api_url,
        api_token=api_token,
        state_file=args.state_file,
        lab_name=lab_name,
        interval_minutes=args.interval,
        run_once=args.once,
    )


if __name__ == "__main__":
    main()
