"""OpenWrt firmware images described by labgrid target files.

openwrt-tests' target files carry an `openwrt:` section naming the device's
image on downloads.openwrt.org:

    openwrt:
      target: mediatek-filogic          # <target>-<subtarget>
      profile: openwrt_one
      image: {type: kernel}             # optional (default kernel), + filesystem
      healthcheck_version: "23.05.5"    # optional: known-good release
      snapshots_only: true              # optional: no release images

The health check's golden image comes from there (same selection as
openwrt-tests' scripts/healthcheck.sh), so it is the same in every lab.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DOWNLOADS = "https://downloads.openwrt.org"
VERSIONS_URL = f"{DOWNLOADS}/.versions.json"


class _TargetLoader(yaml.SafeLoader):
    """Safe loader that reads labgrid tags (`!template $LG_PLACE`) as plain
    values; only the `openwrt:` section is used."""


def _tagged(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_TargetLoader.add_multi_constructor("!", _tagged)


@dataclass
class TargetImage:
    """The `openwrt:` section of a target file."""

    target: str                 # downloads path, e.g. mediatek/filogic
    profile: str
    image_type: str = "kernel"
    filesystem: str | None = None
    healthcheck_version: str | None = None
    snapshots_only: bool = False

    @classmethod
    def from_target_file(cls, path: Path) -> "TargetImage":
        data = yaml.load(path.read_text(), Loader=_TargetLoader) or {}
        meta = data.get("openwrt") or {}
        if not meta.get("target") or not meta.get("profile"):
            raise ValueError(f"{path.name} has no openwrt.target/openwrt.profile")
        image = meta.get("image") or {}
        version = meta.get("healthcheck_version")
        return cls(
            target=meta["target"].replace("-", "/", 1),
            profile=meta["profile"],
            image_type=image.get("type", "kernel"),
            filesystem=image.get("filesystem"),
            healthcheck_version=str(version) if version else None,
            snapshots_only=bool(meta.get("snapshots_only", False)),
        )

    def release(self, default: str) -> str:
        """Known-good release: the target's pin, SNAPSHOT for snapshot-only
        targets, else `default` (lab setting or current stable)."""
        if self.healthcheck_version:
            return self.healthcheck_version
        if self.snapshots_only:
            return "SNAPSHOT"
        return default

    def profiles_url(self, release: str) -> str:
        return f"{targets_url(release)}/{self.target}/profiles.json"

    def select(self, release: str, profiles: dict[str, Any]) -> tuple[str, str]:
        """(image URL, sha256) from the release's profiles.json."""
        profile = (profiles.get("profiles") or {}).get(self.profile)
        if not profile:
            raise ValueError(f"{release} {self.target} has no profile {self.profile}")
        for image in profile.get("images", []):
            if image.get("type") == self.image_type and (
                    not self.filesystem or image.get("filesystem") == self.filesystem):
                return f"{targets_url(release)}/{self.target}/{image['name']}", image["sha256"]
        raise ValueError(f"no {self.image_type}/{self.filesystem or 'any'} image for "
                         f"{self.profile} in {release}")


def targets_url(release: str) -> str:
    if release == "SNAPSHOT":
        return f"{DOWNLOADS}/snapshots/targets"
    return f"{DOWNLOADS}/releases/{release}/targets"
