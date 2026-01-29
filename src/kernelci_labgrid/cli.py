"""Command-line interface for KernelCI Labgrid Scheduler.

This module provides the main CLI entry point with commands for
running the scheduler, agent, and utility operations.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

import click

from kernelci_labgrid import __version__


def setup_logging(debug: bool = False) -> None:
    """Configure logging for the application."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


@click.group()
@click.version_option(version=__version__)
@click.option("--debug", "-d", is_flag=True, help="Enable debug logging")
@click.pass_context
def main(ctx: click.Context, debug: bool) -> None:
    """KernelCI Labgrid Scheduler - Test execution on Labgrid hardware."""
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug
    setup_logging(debug)


@main.command()
@click.option(
    "--config",
    "-c",
    default="config/labgrid-runtime.yaml",
    help="Path to runtime configuration file",
)
@click.option(
    "--scheduler-config",
    "-s",
    default="config/labgrid-scheduler.yaml",
    help="Path to scheduler configuration file",
)
@click.option(
    "--workers",
    "-w",
    type=int,
    default=4,
    help="Number of worker tasks",
)
@click.pass_context
def scheduler(
    ctx: click.Context,
    config: str,
    scheduler_config: str,
    workers: int,
) -> None:
    """Run the push mode scheduler.

    Subscribes to KernelCI API events and submits tests to Labgrid coordinators.
    """
    from kernelci_labgrid.config import LabgridConfig
    from kernelci_labgrid.scheduler.push_scheduler import LabgridPushScheduler

    click.echo(f"Starting push scheduler with {workers} workers")

    # Load configuration
    cfg = LabgridConfig.from_files(config, scheduler_config)

    if not cfg.api_token:
        raise click.ClickException(
            "API token required. Set KCI_API_TOKEN environment variable."
        )

    # Run scheduler
    sched = LabgridPushScheduler(cfg)

    async def run() -> None:
        async with sched:
            await sched.run(num_workers=workers)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        click.echo("\nShutdown requested")


@main.command()
@click.option(
    "--api-url",
    default=os.environ.get("KCI_API_URL", "https://api.kernelci.org"),
    help="KernelCI API URL",
)
@click.option(
    "--api-token",
    default=os.environ.get("KCI_API_TOKEN"),
    help="KernelCI API token",
)
@click.option(
    "--lab-name",
    "-l",
    required=True,
    help="Lab name for job filtering",
)
@click.option(
    "--coordinator",
    "-c",
    default=os.environ.get("LABGRID_COORDINATOR", "localhost:20408"),
    help="Labgrid coordinator address",
)
@click.option(
    "--poll-interval",
    "-p",
    type=int,
    default=30,
    help="Seconds between API polls",
)
@click.pass_context
def agent(
    ctx: click.Context,
    api_url: str,
    api_token: str | None,
    lab_name: str,
    coordinator: str,
    poll_interval: int,
) -> None:
    """Run the pull mode agent.

    Polls KernelCI API for pending jobs and executes them locally.
    """
    from kernelci_labgrid.scheduler.pull_agent import LabgridPullAgent

    if not api_token:
        raise click.ClickException(
            "API token required. Set KCI_API_TOKEN or use --api-token."
        )

    click.echo(f"Starting pull agent for lab: {lab_name}")
    click.echo(f"Coordinator: {coordinator}")
    click.echo(f"Poll interval: {poll_interval}s")

    # Run agent
    pull_agent = LabgridPullAgent(
        api_url=api_url,
        api_token=api_token,
        lab_name=lab_name,
        coordinator_address=coordinator,
        poll_interval=poll_interval,
    )

    async def run() -> None:
        async with pull_agent:
            await pull_agent.run()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        click.echo("\nShutdown requested")


@main.command()
@click.option(
    "--coordinator",
    "-c",
    default=os.environ.get("LABGRID_COORDINATOR", "localhost:20408"),
    help="Labgrid coordinator address",
)
@click.pass_context
def places(ctx: click.Context, coordinator: str) -> None:
    """List available Labgrid places."""
    from kernelci_labgrid.runtime.labgrid_client import LabgridClient

    async def list_places() -> None:
        async with LabgridClient(coordinator) as client:
            places = await client.get_places()

            if not places:
                click.echo("No places found")
                return

            click.echo(f"{'Place':<20} {'Acquired':<10} {'Tags'}")
            click.echo("-" * 60)

            for place in places:
                acquired = "Yes" if place.is_acquired else "No"
                tags = ", ".join(f"{k}={v}" for k, v in place.tags.items())
                click.echo(f"{place.name:<20} {acquired:<10} {tags}")

    try:
        asyncio.run(list_places())
    except Exception as e:
        raise click.ClickException(f"Failed to list places: {e}")


@main.command()
@click.option(
    "--coordinator",
    "-c",
    default=os.environ.get("LABGRID_COORDINATOR", "localhost:20408"),
    help="Labgrid coordinator address",
)
@click.pass_context
def resources(ctx: click.Context, coordinator: str) -> None:
    """List available Labgrid resources."""
    from kernelci_labgrid.runtime.labgrid_client import LabgridClient

    async def list_resources() -> None:
        async with LabgridClient(coordinator) as client:
            resources = await client.get_resources()

            if not resources:
                click.echo("No resources found")
                return

            click.echo(f"{'Exporter':<15} {'Group':<15} {'Class':<20} {'Name':<15} {'Avail'}")
            click.echo("-" * 80)

            for res in resources:
                avail = "Yes" if res.avail else "No"
                click.echo(
                    f"{res.exporter:<15} {res.group:<15} {res.cls:<20} {res.name:<15} {avail}"
                )

    try:
        asyncio.run(list_resources())
    except Exception as e:
        raise click.ClickException(f"Failed to list resources: {e}")


@main.command()
@click.option(
    "--coordinator",
    "-c",
    default=os.environ.get("LABGRID_COORDINATOR", "localhost:20408"),
    help="Labgrid coordinator address",
)
@click.option(
    "--api-url",
    default=os.environ.get("KCI_API_URL", "https://api.kernelci.org"),
    help="KernelCI API URL",
)
@click.option(
    "--api-token",
    default=os.environ.get("KCI_API_TOKEN"),
    help="KernelCI API token",
)
@click.pass_context
def health(ctx: click.Context, coordinator: str, api_url: str, api_token: str | None) -> None:
    """Check health of Labgrid and KernelCI connections."""
    from kernelci_labgrid.runtime.labgrid_client import LabgridClient

    click.echo("Checking connections...\n")

    async def check_health() -> None:
        # Check Labgrid coordinator
        click.echo(f"Labgrid Coordinator ({coordinator}):")
        try:
            async with LabgridClient(coordinator) as client:
                places = await client.get_places()
                available = sum(1 for p in places if not p.is_acquired)
                click.echo(f"  Status: Connected")
                click.echo(f"  Places: {len(places)} total, {available} available")
        except Exception as e:
            click.echo(f"  Status: Failed - {e}")

        click.echo()

        # Check KernelCI API
        click.echo(f"KernelCI API ({api_url}):")
        if not api_token:
            click.echo("  Status: Skipped (no token)")
        else:
            import aiohttp

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{api_url}/api/latest/",
                        headers={"Authorization": f"Bearer {api_token}"},
                    ) as resp:
                        if resp.status == 200:
                            click.echo(f"  Status: Connected")
                        else:
                            click.echo(f"  Status: HTTP {resp.status}")
            except Exception as e:
                click.echo(f"  Status: Failed - {e}")

    try:
        asyncio.run(check_health())
    except Exception as e:
        raise click.ClickException(f"Health check failed: {e}")


@main.command()
@click.argument("template", type=click.Choice(["baseline", "kselftest", "custom"]))
@click.option("--job-name", default="test-job", help="Job name")
@click.option("--node-id", default="test-node", help="Node ID")
@click.option("--target", default="test-target", help="Target name")
@click.option("--kernel-version", default="", help="Expected kernel version")
@click.option("--output", "-o", type=click.File("w"), default="-", help="Output file")
@click.pass_context
def generate_test(
    ctx: click.Context,
    template: str,
    job_name: str,
    node_id: str,
    target: str,
    kernel_version: str,
    output: Any,
) -> None:
    """Generate a pytest test file from template."""
    from jinja2 import Environment, PackageLoader

    env = Environment(
        loader=PackageLoader("kernelci_labgrid", "templates"),
    )

    tmpl = env.get_template("labgrid_test.jinja2")

    result = tmpl.render(
        job_name=job_name,
        node_id=node_id,
        target_name=target,
        kernel_version=kernel_version,
        test_suite=template,
        timeout=3600,
        compatible=[],
        commands=[],
    )

    output.write(result)
    click.echo(f"Generated {template} test template", err=True)


@main.command()
@click.option(
    "--config",
    "-c",
    default="config/labgrid-runtime.yaml",
    help="Path to configuration file",
)
@click.pass_context
def validate(ctx: click.Context, config: str) -> None:
    """Validate configuration files."""
    from pathlib import Path

    from kernelci_labgrid.config import LabgridConfig

    config_path = Path(config)

    if not config_path.exists():
        raise click.ClickException(f"Configuration file not found: {config}")

    try:
        cfg = LabgridConfig.from_files(config_path, None)
        click.echo(f"Configuration loaded successfully")
        click.echo(f"  Runtimes: {len(cfg.runtimes)}")

        for name, rt in cfg.runtimes.items():
            click.echo(f"    - {name}: {rt.coordinator_address} ({rt.mode} mode)")
            click.echo(f"      Places mapped: {len(rt.place_mapping)}")

    except Exception as e:
        raise click.ClickException(f"Invalid configuration: {e}")


if __name__ == "__main__":
    main()
