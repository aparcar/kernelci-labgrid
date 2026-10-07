"""Agent settings: TOML config file, env file, environment and CLI.

Precedence (later wins): defaults < config file < --env-file < environment
< command line. Environment variables: LAB_NAME, KCI_API_URL, LAB_API_TOKEN,
KCI_STORAGE_URL, LAB_STORAGE_TOKEN, LG_COORDINATOR and the paths LABGRID_TESTS_DIR,
LABGRID_TARGETS_DIR, LABGRID_STRATEGIES_DIR, LABGRID_ARTIFACT_DIR, LABGRID_HEALTH_CHECKS_DIR,
LABGRID_HEALTH_STATE_FILE. Example config (keys before the [api]/[storage] tables, as
TOML assigns later keys to the table above them):

    lab_name = "lynxis"
    platforms = ["qemu_armsr-armv8"]
    tests_dir = "openwrt-tests/tests"      # relative to this file
    targets_dir = "openwrt-tests/targets"  # default: <tests_dir>/../targets
    # strategies_dir: default <targets_dir>/../strategies
    # poll_interval = 30
    # pytest_command: default `python -m pytest` in the agent's environment
    # coordinator = "127.0.0.1:20408"      # real hardware via labgrid
    # artifact_dir, targets_dir, health_checks_dir, health_state_file

    [api]
    url = "https://api.example.org/latest"
    token = "..."

    [storage]
    url = "https://files.example.org/"
    token = "..."
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

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
    "health_checks_dir": None,
    "health_state_file": None,
    # Real hardware: the lab's labgrid-coordinator (host:port); unset = QEMU only
    "coordinator": None,
    "labgrid_command": None,
    "reserve_timeout": 60,
}

# Settings that are paths; relative ones are resolved against the config file
PATH_KEYS = ("tests_dir", "targets_dir", "strategies_dir", "artifact_dir", "health_checks_dir",
             "health_state_file")

# Environment variables (also the keys of --env-file), first match wins
ENV_KEYS = {
    "lab_name": ("LAB_NAME",),
    "api_url": ("KCI_API_URL",),
    "api_token": ("LAB_API_TOKEN", "KCI_API_TOKEN"),
    "storage_url": ("KCI_STORAGE_URL",),
    "storage_token": ("LAB_STORAGE_TOKEN",),
    # Paths, e.g. set by the container image to its own layout
    "tests_dir": ("LABGRID_TESTS_DIR",),
    "targets_dir": ("LABGRID_TARGETS_DIR",),
    "strategies_dir": ("LABGRID_STRATEGIES_DIR",),
    "artifact_dir": ("LABGRID_ARTIFACT_DIR",),
    "health_checks_dir": ("LABGRID_HEALTH_CHECKS_DIR",),
    "health_state_file": ("LABGRID_HEALTH_STATE_FILE",),
    "coordinator": ("LG_COORDINATOR",),
}

SECRET_KEYS = ("api_token", "storage_token")


def default_config_paths() -> list[Path]:
    return [
        Path("labgrid-agent.toml"),
        Path.home() / ".config" / "labgrid-agent" / "config.toml",
        Path("/etc/labgrid-agent/config.toml"),
    ]


def find_config(explicit: str | None) -> Path | None:
    """--config, else $LABGRID_AGENT_CONFIG, else the first default that exists."""
    if explicit:
        return Path(explicit)
    if os.environ.get("LABGRID_AGENT_CONFIG"):
        return Path(os.environ["LABGRID_AGENT_CONFIG"])
    return next((p for p in default_config_paths() if p.is_file()), None)


def load_config(path: Path) -> dict[str, Any]:
    """Read a TOML config file into flat settings."""
    with path.open("rb") as f:
        data = tomllib.load(f)

    settings: dict[str, Any] = {}
    # Plain keys may also live in an [agent] table
    for source in (data, data.get("agent", {})):
        for key, value in source.items():
            if key in DEFAULTS:
                settings[key] = value
    for table in ("api", "storage"):
        for key in ("url", "token"):
            if key in data.get(table, {}):
                settings[f"{table}_{key}"] = data[table][key]

    unknown = set(data) - set(DEFAULTS) - {"agent", "api", "storage"}
    if unknown:
        raise ValueError(f"{path}: unknown settings: {', '.join(sorted(unknown))}")
    if isinstance(settings.get("platforms"), str):
        settings["platforms"] = [settings["platforms"]]

    base = path.resolve().parent
    for key in PATH_KEYS:
        if settings.get(key):
            settings[key] = str((base / os.path.expanduser(settings[key])).resolve())
    return settings


def read_env_file(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def from_env(env: dict[str, str]) -> dict[str, Any]:
    settings = {}
    for key, names in ENV_KEYS.items():
        value = next((env[n] for n in names if env.get(n)), None)
        if value:
            settings[key] = value
    return settings


def resolve_settings(
    cli: dict[str, Any],
    config_path: Path | None = None,
    env_file: Path | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Merge all sources; `cli` holds only options given on the command line."""
    settings = dict(DEFAULTS)
    if config_path:
        settings.update(load_config(config_path))
    if env_file:
        settings.update(from_env(read_env_file(env_file)))
    settings.update(from_env(dict(os.environ if environ is None else environ)))
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
