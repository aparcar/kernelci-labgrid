"""Unit tests for agent settings resolution."""

import pytest

from kernelci_labgrid.config import describe, find_config, load_config, resolve_settings

CONFIG = """\
lab_name = "lynxis"
platforms = ["qemu_armsr-armv8"]
tests_dir = "../openwrt-tests/tests"
poll_interval = 60

[api]
url = "https://api.example.org/latest"
token = "api-secret-token"

[storage]
url = "https://files.example.org/"
token = "storage-secret-token"
"""


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "lab" / "lynxis.toml"
    path.parent.mkdir()
    path.write_text(CONFIG)
    return path


class TestLoadConfig:
    def test_flattens_tables(self, config):
        s = load_config(config)
        assert s["lab_name"] == "lynxis"
        assert s["platforms"] == ["qemu_armsr-armv8"]
        assert s["api_url"] == "https://api.example.org/latest"
        assert s["api_token"] == "api-secret-token"
        assert s["storage_token"] == "storage-secret-token"
        assert s["poll_interval"] == 60

    def test_relative_paths_against_config_file(self, config, tmp_path):
        assert load_config(config)["tests_dir"] == str(tmp_path / "openwrt-tests" / "tests")

    def test_platforms_string(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text('lab_name = "x"\nplatforms = "qemu_x86-64"\n')
        assert load_config(path)["platforms"] == ["qemu_x86-64"]

    @pytest.mark.parametrize("text", ['[agent]\nlab_name = "x"\n', 'health_checks_dir = "d"\n'])
    def test_removed_settings_are_unknown(self, tmp_path, text):
        path = tmp_path / "c.toml"
        path.write_text(text)
        with pytest.raises(ValueError, match="unknown"):
            load_config(path)

    def test_unknown_key(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text('lab_nmae = "typo"\n')
        with pytest.raises(ValueError, match="lab_nmae"):
            load_config(path)


class TestResolveSettings:
    def test_defaults(self):
        s = resolve_settings({})
        assert s["api_url"] == "https://api.kernelci.org/latest"
        assert s["pytest_command"] is None  # agent uses its own python -m pytest
        assert s["platforms"] == []
        assert s["coordinator"] == "127.0.0.1:20408"

    def test_pool(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text('pool = "openwrt-labs"\n')
        assert resolve_settings({})["pool"] is None
        assert resolve_settings({}, config_path=path)["pool"] == "openwrt-labs"

    def test_empty_coordinator_disables_hardware(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text('coordinator = ""\nplatforms = ["qemu_x86-64"]\n')
        assert resolve_settings({}, config_path=path)["coordinator"] == ""

    def test_precedence(self, config):
        s = resolve_settings(
            {"lab_name": "from-cli", "platforms": None, "poll_interval": None},
            config_path=config,
        )
        assert s["lab_name"] == "from-cli"             # CLI beats the file
        assert s["platforms"] == ["qemu_armsr-armv8"]  # unset CLI keeps config
        assert s["poll_interval"] == 60

    def test_environment_is_ignored(self, config, monkeypatch):
        """Settings and tokens come from the TOML file only."""
        monkeypatch.setenv("LAB_API_TOKEN", "from-environ")
        monkeypatch.setenv("LAB_NAME", "from-environ")
        s = resolve_settings({}, config_path=config)
        assert s["api_token"] == "api-secret-token"
        assert s["lab_name"] == "lynxis"

    def test_cli_platforms_replace_config(self, config):
        s = resolve_settings({"platforms": ["qemu_x86-64"]}, config_path=config)
        assert s["platforms"] == ["qemu_x86-64"]


class TestFindConfig:
    def test_explicit(self, tmp_path):
        assert find_config(str(tmp_path / "x.toml")) == tmp_path / "x.toml"

    def test_working_directory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "labgrid-agent.toml").write_text("")
        assert find_config(None).name == "labgrid-agent.toml"


def test_describe_masks_secrets(config):
    out = describe(resolve_settings({}, config_path=config))
    assert "api-secret-token" not in out
    assert "storage-secret-token" not in out
    assert "lynxis" in out

