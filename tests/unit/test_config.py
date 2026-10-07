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

    def test_agent_table(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text('[agent]\nlab_name = "x"\nplatforms = "qemu_x86-64"\n')
        s = load_config(path)
        assert s["lab_name"] == "x"
        assert s["platforms"] == ["qemu_x86-64"]

    def test_unknown_key(self, tmp_path):
        path = tmp_path / "c.toml"
        path.write_text('lab_nmae = "typo"\n')
        with pytest.raises(ValueError, match="lab_nmae"):
            load_config(path)


class TestResolveSettings:
    def test_defaults(self):
        s = resolve_settings({}, environ={})
        assert s["api_url"] == "https://api.kernelci.org/latest"
        assert s["pytest_command"] is None  # agent uses its own python -m pytest
        assert s["platforms"] == []

    def test_precedence(self, config, tmp_path):
        env_file = tmp_path / "lab.env"
        env_file.write_text("LAB_API_TOKEN=from-env-file\nLAB_NAME=from-env-file\n")
        s = resolve_settings(
            {"lab_name": "from-cli", "platforms": None, "poll_interval": None},
            config_path=config,
            env_file=env_file,
            environ={"LAB_NAME": "from-environ"},
        )
        assert s["lab_name"] == "from-cli"           # CLI beats everything
        assert s["api_token"] == "from-env-file"     # env file beats config
        assert s["platforms"] == ["qemu_armsr-armv8"]  # unset CLI keeps config
        assert s["poll_interval"] == 60

    def test_environ_beats_env_file(self, tmp_path):
        env_file = tmp_path / "lab.env"
        env_file.write_text("LAB_NAME=from-env-file\n")
        s = resolve_settings({}, env_file=env_file, environ={"LAB_NAME": "from-environ"})
        assert s["lab_name"] == "from-environ"

    def test_cli_platforms_replace_config(self, config):
        s = resolve_settings({"platforms": ["qemu_x86-64"]}, config_path=config, environ={})
        assert s["platforms"] == ["qemu_x86-64"]


class TestFindConfig:
    def test_explicit(self, tmp_path):
        assert find_config(str(tmp_path / "x.toml")) == tmp_path / "x.toml"

    def test_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LABGRID_AGENT_CONFIG", str(tmp_path / "y.toml"))
        assert find_config(None) == tmp_path / "y.toml"

    def test_working_directory(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LABGRID_AGENT_CONFIG", raising=False)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "labgrid-agent.toml").write_text("")
        assert find_config(None).name == "labgrid-agent.toml"


def test_describe_masks_secrets(config):
    out = describe(resolve_settings({}, config_path=config, environ={}))
    assert "api-secret-token" not in out
    assert "storage-secret-token" not in out
    assert "lynxis" in out


def test_path_environment_beats_config(config):
    """Container images point tests_dir etc. at their own layout."""
    s = resolve_settings({}, config_path=config,
                         environ={"LABGRID_TESTS_DIR": "/opt/openwrt-tests/tests"})
    assert s["tests_dir"] == "/opt/openwrt-tests/tests"
    assert s["lab_name"] == "lynxis"
