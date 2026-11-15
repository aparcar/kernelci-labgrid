"""Agent settings: a TOML config file and the command line.

Precedence (later wins): defaults < config file < command line. No
environment variables: tokens belong in the (private) config file. Example
config (keys before the [api]/[storage] tables, as TOML assigns later keys to
the table above them):

    lab_name = "lynxis"
    platforms = ["qemu_armsr-armv8"]
    tests_dir = "openwrt-tests/tests"      # relative to this file
    targets_dir = "openwrt-tests/targets"  # default: <tests_dir>/../targets
    # strategies_dir: default <targets_dir>/../strategies
    # poll_interval = 30
    # pytest_command: default `python -m pytest` in the agent's environment
    # coordinator = "127.0.0.1:20408"      # default; "" for a QEMU-only lab
    # artifact_dir, health_state_file

    [api]
    url = "https://api.example.org/latest"
    token = "..."

    [storage]
    url = "https://files.example.org/"
    token = "..."

    # Health checks run for all platforms (golden images from the target
    # files); optional settings, or `health_checks = false` above to disable
    [health_checks]
    # frequency_hours = 24

    [health_checks.rpi-4]
    enabled = false
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "lab_name": None,
    "platforms": [],
    "tests_dir": None,
    "targets_dir": None,
    "strategies_dir": None,
    "api_url": "https://api.kernelci.org/latest",
    "api_token": None,
    "storage_url": None,
    "storage_token": None,
    # Default: pytest/labgrid-client from the agent's own environment
    "pytest_command": None,
    "poll_interval": 30,
    "artifact_dir": None,
    "health_state_file": None,
    # Health checks for all platforms by default; a [health_checks] table
    # holds settings: plain keys apply to all, [health_checks.<device>]
    # tables per device. `health_checks = false`: none
    "health_checks": {},
    # Real hardware: the lab's labgrid-coordinator (host:port); "" = QEMU only
    "coordinator": "127.0.0.1:20408",
    "labgrid_command": None,
    "reserve_timeout": 60,
    # At most this many jobs at once (None: one per platform, no limit)
    "max_jobs": None,
    # Shared runtime several labs take jobs from (e.g. "openwrt-labs")
    "pool": None,
}

# Settings that are paths; relative ones are resolved against the config file
PATH_KEYS = ("tests_dir", "targets_dir", "strategies_dir", "artifact_dir", "health_state_file")

SECRET_KEYS = ("api_token", "storage_token")


def default_config_paths() -> list[Path]:
    return [
        Path("labgrid-agent.toml"),
        Path.home() / ".config" / "labgrid-agent" / "config.toml",
        Path("/etc/labgrid-agent/config.toml"),
    ]


def find_config(explicit: str | None) -> Path | None:
    """--config, else the first default that exists."""
    if explicit:
        return Path(explicit)
    return next((p for p in default_config_paths() if p.is_file()), None)


def load_config(path: Path) -> dict[str, Any]:
    """Read a TOML config file into flat settings."""
    with path.open("rb") as f:
        data = tomllib.load(f)

    settings = {key: value for key, value in data.items() if key in DEFAULTS}
    for table in ("api", "storage"):
        for key in ("url", "token"):
            if key in data.get(table, {}):
                settings[f"{table}_{key}"] = data[table][key]

    unknown = set(data) - set(DEFAULTS) - {"api", "storage"}
    if unknown:
        raise ValueError(f"{path}: unknown settings: {', '.join(sorted(unknown))}")
    if isinstance(settings.get("platforms"), str):
        settings["platforms"] = [settings["platforms"]]
    if settings.get("health_checks") is True:
        settings["health_checks"] = {}

    base = path.resolve().parent
    for key in PATH_KEYS:
        if settings.get(key):
            settings[key] = str((base / os.path.expanduser(settings[key])).resolve())
    return settings


def resolve_settings(cli: dict[str, Any], config_path: Path | None = None) -> dict[str, Any]:
    """Defaults < config file < `cli` (only the options given)."""
    settings = dict(DEFAULTS)
    if config_path:
        settings.update(load_config(config_path))
    settings.update({k: v for k, v in cli.items() if v not in (None, [])})
    return settings


def describe(settings: dict[str, Any]) -> str:
    """Effective settings with secrets masked (for --show-config)."""
    lines = []
    for key, value in settings.items():
        if key in SECRET_KEYS and value:
            value = value[:6] + "…"
        lines.append(f"{key} = {value!r}")
    return "\n".join(lines)
