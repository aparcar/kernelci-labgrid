#!/usr/bin/env python3
"""KernelCI Job Scheduler for OpenWrt x86_64 Testing.

This script periodically submits test jobs to KernelCI for the latest
OpenWrt x86_64 images. It runs as a daemon and schedules jobs every 24 hours.

Usage:
    job_scheduler.py [--interval HOURS] [--once]

Environment:
    KCI_API_URL: KernelCI API URL (default: https://api.kernelci.org)
    KCI_API_TOKEN: KernelCI API token (required)
    LAB_NAME: Lab name to target (default: openwrt-qemu-lab)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.request import urlopen, Request
from urllib.error import URLError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# OpenWrt image sources
OPENWRT_SOURCES = {
    "main": {
        "base_url": "https://downloads.openwrt.org/snapshots/targets/x86/64/",
        "branch": "main",
        "platform": "openwrt-main-x86",
    },
    "25.12": {
        "base_url": "https://downloads.openwrt.org/releases/25.12-SNAPSHOT/targets/x86/64/",
        "branch": "openwrt-25.12",
        "platform": "openwrt-2512-x86",
    },
}

# Image filename patterns
ROOTFS_PATTERN = re.compile(r"openwrt-[^\"]+x86-64-generic-squashfs-combined-efi\.img\.gz")


def fetch_latest_image_url(base_url: str) -> str | None:
    """Fetch the latest rootfs image URL from OpenWrt downloads."""
    try:
        logger.info(f"Fetching image list from {base_url}")
        req = Request(base_url, headers={"User-Agent": "KernelCI-Scheduler/1.0"})
        with urlopen(req, timeout=30) as response:
            html = response.read().decode("utf-8")

        matches = ROOTFS_PATTERN.findall(html)
        if matches:
            image_name = matches[0]
            image_url = f"{base_url}{image_name}"
            logger.info(f"Found image: {image_url}")
            return image_url
        else:
            logger.warning(f"No matching image found at {base_url}")
            return None
    except URLError as e:
        logger.error(f"Failed to fetch image list: {e}")
        return None


def submit_job_to_kernelci(
    api_url: str,
    api_token: str,
    lab_name: str,
    platform: str,
    branch: str,
    rootfs_url: str,
    test_path: str = "test_openwrt.py",
    timeout: int = 600,
) -> dict[str, Any] | None:
    """Submit a test job to KernelCI API."""
    job_data = {
        "kind": "job",
        "name": f"openwrt-{branch}-x86-test",
        "state": "running",
        "data": {
            "lab": lab_name,
            "platform": platform,
            "branch": branch,
            "artifacts": {
                "rootfs": rootfs_url,
            },
            "test_path": test_path,
            "timeout": timeout,
        },
        "created": datetime.now(timezone.utc).isoformat(),
    }

    try:
        logger.info(f"Submitting job for {platform} ({branch})")
        req = Request(
            f"{api_url}/api/node",
            data=json.dumps(job_data).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
                "User-Agent": "KernelCI-Scheduler/1.0",
            },
            method="POST",
        )
        with urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            logger.info(f"Job submitted successfully: {result.get('id', 'unknown')}")
            return result
    except URLError as e:
        logger.error(f"Failed to submit job: {e}")
        return None


def schedule_jobs(
    api_url: str,
    api_token: str,
    lab_name: str,
    sources: dict[str, dict] | None = None,
) -> list[dict]:
    """Schedule test jobs for all configured OpenWrt sources."""
    if sources is None:
        sources = OPENWRT_SOURCES

    results = []
    for name, config in sources.items():
        logger.info(f"Processing source: {name}")

        # Fetch latest image URL
        rootfs_url = fetch_latest_image_url(config["base_url"])
        if not rootfs_url:
            logger.error(f"Skipping {name}: could not find image")
            continue

        # Submit job
        result = submit_job_to_kernelci(
            api_url=api_url,
            api_token=api_token,
            lab_name=lab_name,
            platform=config["platform"],
            branch=config["branch"],
            rootfs_url=rootfs_url,
        )
        if result:
            results.append(result)

    return results


def run_scheduler(
    api_url: str,
    api_token: str,
    lab_name: str,
    interval_hours: int = 24,
    run_once: bool = False,
) -> None:
    """Run the scheduler loop."""
    interval_seconds = interval_hours * 3600

    logger.info(f"Starting job scheduler")
    logger.info(f"  API URL: {api_url}")
    logger.info(f"  Lab name: {lab_name}")
    logger.info(f"  Interval: {interval_hours} hours")
    logger.info(f"  Sources: {', '.join(OPENWRT_SOURCES.keys())}")

    while True:
        try:
            logger.info("=" * 60)
            logger.info(f"Scheduling jobs at {datetime.now(timezone.utc).isoformat()}")

            results = schedule_jobs(api_url, api_token, lab_name)
            logger.info(f"Scheduled {len(results)} jobs")

            if run_once:
                logger.info("Run-once mode, exiting")
                break

            logger.info(f"Sleeping for {interval_hours} hours...")
            time.sleep(interval_seconds)

        except KeyboardInterrupt:
            logger.info("Interrupted, shutting down")
            break
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
            if run_once:
                sys.exit(1)
            # Sleep before retrying
            time.sleep(60)


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="KernelCI Job Scheduler for OpenWrt testing"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=24,
        help="Interval between job submissions in hours (default: 24)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Submit jobs once and exit (don't loop)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Get configuration from environment
    api_url = os.environ.get("KCI_API_URL", "https://api.kernelci.org")
    api_token = os.environ.get("KCI_API_TOKEN")
    lab_name = os.environ.get("LAB_NAME", "openwrt-qemu-lab")

    if not api_token:
        logger.error("KCI_API_TOKEN environment variable is required")
        sys.exit(1)

    run_scheduler(
        api_url=api_url,
        api_token=api_token,
        lab_name=lab_name,
        interval_hours=args.interval,
        run_once=args.once,
    )


if __name__ == "__main__":
    main()
