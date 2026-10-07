"""Health checks: TOML config, golden images from the target files."""

from pathlib import Path

import pytest

from kernelci_labgrid.config import resolve_settings
from kernelci_labgrid.firmware import TargetImage
from kernelci_labgrid.scheduler.pull_agent import (
    DEFAULT_HEALTH_TESTS,
    HealthCheckConfig,
    LabgridAgent,
)

EXAMPLE = Path(__file__).parents[2] / "examples" / "health_checks" / "qemu_armsr-armv8.toml"

ONE = """\
openwrt:
  name: OpenWrt One
  target: mediatek-filogic
  profile: openwrt_one
targets:
  main:
    resources:
      RemotePlace:
        name: !template "$LG_PLACE"
"""

PINNED = """\
openwrt:
  target: mpc85xx-p1020
  profile: enterasys_ws-ap3710i
  image: {type: kernel}
  healthcheck_version: "23.05.5"
"""

SNAPSHOT_ONLY = """\
openwrt:
  target: sunxi-cortexa7
  profile: xunlong_orangepi-2
  image: {type: sdcard, filesystem: ext4}
  snapshots_only: true
"""

PROFILES = {
    "profiles": {"openwrt_one": {"images": [
        {"type": "sysupgrade", "name": "one-sysupgrade.itb", "sha256": "s"},
        {"type": "kernel", "name": "one-initramfs.itb", "sha256": "k"},
    ]}},
}


@pytest.fixture
def targets(tmp_path):
    d = tmp_path / "targets"
    d.mkdir()
    (d / "openwrt_one.yaml").write_text(ONE)
    (d / "enterasys_ws-ap3710i.yaml").write_text(PINNED)
    (d / "xunlong_orangepi-2.yaml").write_text(SNAPSHOT_ONLY)
    (d / "qemu_x86-64.yaml").write_text("targets: {}\n")
    return d


# --- target files -------------------------------------------------------------


def test_target_image(targets):
    image = TargetImage.from_target_file(targets / "openwrt_one.yaml")
    assert (image.target, image.profile, image.image_type) == ("mediatek/filogic", "openwrt_one", "kernel")
    assert image.release("25.12.5") == "25.12.5"
    assert image.profiles_url("25.12.5") == (
        "https://downloads.openwrt.org/releases/25.12.5/targets/mediatek/filogic/profiles.json")
    assert image.select("25.12.5", PROFILES) == (
        "https://downloads.openwrt.org/releases/25.12.5/targets/mediatek/filogic/one-initramfs.itb", "k")


def test_target_pins_release(targets):
    assert TargetImage.from_target_file(targets / "enterasys_ws-ap3710i.yaml").release("25.12.5") == "23.05.5"


def test_snapshot_only_target(targets):
    image = TargetImage.from_target_file(targets / "xunlong_orangepi-2.yaml")
    assert image.release("25.12.5") == "SNAPSHOT"
    assert image.profiles_url("SNAPSHOT").startswith("https://downloads.openwrt.org/snapshots/targets/")


def test_no_matching_image(targets):
    image = TargetImage.from_target_file(targets / "openwrt_one.yaml")
    image.image_type = "factory"
    with pytest.raises(ValueError, match="no factory"):
        image.select("25.12.5", PROFILES)


def test_target_without_openwrt_section(targets):
    with pytest.raises(ValueError, match="openwrt.target"):
        TargetImage.from_target_file(targets / "qemu_x86-64.yaml")


# --- TOML settings --------------------------------------------------------------


def test_config_table(tmp_path):
    path = tmp_path / "lab.toml"
    path.write_text('lab_name = "lab"\n\n[health_checks]\ntimeout = 300\n\n'
                    '[health_checks.rpi-4]\nenabled = false\n')
    table = resolve_settings({}, config_path=path, environ={})["health_checks"]
    assert HealthCheckConfig.split_table(table) == ({"timeout": 300}, {"rpi-4": {"enabled": False}})


def test_no_table_means_none(tmp_path):
    assert resolve_settings({}, environ={})["health_checks"] is None


def test_defaults():
    check = HealthCheckConfig.from_dict("openwrt_one", {})
    assert check.golden_image == {}
    assert check.test_path == DEFAULT_HEALTH_TESTS
    assert check.target == "openwrt_one.yaml"
    assert check.enabled


def test_example_file():
    check = HealthCheckConfig.from_toml(EXAMPLE)
    assert check.device == "qemu_armsr-armv8"
    assert check.frequency_hours == 24


def test_device_defaults_to_file_name(tmp_path):
    path = tmp_path / "openwrt_one.toml"
    path.write_text('frequency_hours = 12\n')
    assert HealthCheckConfig.from_toml(path).device == "openwrt_one"


@pytest.mark.parametrize("data, error", [
    ({"sha256": "abc"}, "without `firmware`"),
    ({"golden_image": {}}, "unknown"),
])
def test_invalid(data, error):
    with pytest.raises(ValueError, match=error):
        HealthCheckConfig.from_dict("dev", data)


# --- agent ------------------------------------------------------------------------


def make_agent(tmp_path, targets, **kwargs):
    return LabgridAgent(
        api_url="http://x/latest", api_token="t", lab_name="lab", tests_dir=tmp_path,
        targets_dir=targets, artifact_dir=tmp_path / "a", **kwargs)


def test_all_platforms_with_table(tmp_path, targets):
    agent = make_agent(tmp_path, targets, health_checks={
        "frequency_hours": 12, "rpi-4": {"enabled": False},
        "openwrt_one": {"test_path": "test_base.py"}})
    agent._load_health_configs()
    assert agent._health_config("enterasys_ws-ap3710i").frequency_hours == 12  # defaults
    assert agent._health_config("openwrt_one").test_path == "test_base.py"
    assert agent._health_config("openwrt_one").frequency_hours == 12
    assert agent._health_config("rpi-4") is None


def test_only_listed_devices_without_table(tmp_path, targets):
    checks = tmp_path / "checks"
    checks.mkdir()
    (checks / "openwrt_one.toml").write_text('frequency_hours = 6\n')
    agent = make_agent(tmp_path, targets, health_checks_dir=checks)
    agent._load_health_configs()
    assert agent._health_config("openwrt_one").frequency_hours == 6
    assert agent._health_config("enterasys_ws-ap3710i") is None


async def test_golden_image_from_target(tmp_path, targets, monkeypatch):
    agent = make_agent(tmp_path, targets, health_checks={})
    fetched = []

    async def fake_fetch(url):
        fetched.append(url)
        if url.endswith(".versions.json"):
            return {"stable_version": "25.12.5"}
        return PROFILES

    monkeypatch.setattr(agent, "_fetch_json", fake_fetch)
    url, sha256 = await agent._golden_image(HealthCheckConfig.from_dict("openwrt_one", {}))
    assert url.endswith("/releases/25.12.5/targets/mediatek/filogic/one-initramfs.itb")
    assert sha256 == "k"
    assert fetched[0] == "https://downloads.openwrt.org/.versions.json"

    fetched.clear()
    await agent._golden_image(HealthCheckConfig.from_dict("openwrt_one", {"release": "24.10.4"}))
    assert fetched == ["https://downloads.openwrt.org/releases/24.10.4/targets/mediatek/filogic/profiles.json"]


async def test_explicit_firmware_wins(tmp_path, targets):
    agent = make_agent(tmp_path, targets)
    config = HealthCheckConfig.from_dict("openwrt_one", {"firmware": "https://x/fw.bin", "sha256": "a"})
    assert await agent._golden_image(config) == ("https://x/fw.bin", "a")
