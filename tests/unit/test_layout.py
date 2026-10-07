"""Tests, targets and strategies in separate, freely placed directories."""

import sys
from pathlib import Path

import pytest

from kernelci_labgrid.scheduler.pull_agent import LabgridAgent


@pytest.fixture
def layout(tmp_path):
    """tests, targets and strategies in unrelated places."""
    tests = tmp_path / "suite"
    targets = tmp_path / "elsewhere" / "boards"
    strategies = tmp_path / "shared" / "strategies"
    for d in (tests, targets, strategies):
        d.mkdir(parents=True)
    (tests / "conftest.py").write_text("")
    (strategies / "tftpstrategy.py").write_text("MARKER = 'tftp'\n")
    (targets / "openwrt_one.yaml").write_text(
        "targets:\n  main:\n    drivers: {}\nimports:\n  - ../strategies/tftpstrategy.py\n")
    return tests, targets, strategies


def make_agent(tmp_path, tests, targets=None, strategies=None):
    return LabgridAgent(
        api_url="http://x/latest", api_token="t", lab_name="lab", tests_dir=tests,
        targets_dir=targets, strategies_dir=strategies, artifact_dir=tmp_path / "artifacts",
    )


def test_defaults_use_this_environment(tmp_path, layout):
    tests, _, _ = layout
    agent = make_agent(tmp_path, tests)
    assert agent.pytest_command == [sys.executable, "-m", "pytest"]
    assert agent.targets_dir == tests.parent / "targets"
    assert agent.strategies_dir == tests.parent / "strategies"


def test_staged_target_finds_strategies(tmp_path, layout):
    tests, targets, strategies = layout
    agent = make_agent(tmp_path, tests, targets, strategies)
    staged = agent._stage_target(targets / "openwrt_one.yaml", tmp_path / "run" / "env")

    assert staged.is_symlink() and staged.resolve() == (targets / "openwrt_one.yaml").resolve()
    # labgrid resolves imports against dirname(abspath(target)), not realpath
    from labgrid.config import Config
    config = Config(str(staged))
    (import_path,) = config.get_imports()
    assert Path(import_path).read_text() == "MARKER = 'tftp'\n"


def test_staging_is_repeatable(tmp_path, layout):
    tests, targets, strategies = layout
    agent = make_agent(tmp_path, tests, targets, strategies)
    stage = tmp_path / "run" / "env"
    agent._stage_target(targets / "openwrt_one.yaml", stage)
    staged = agent._stage_target(targets / "openwrt_one.yaml", stage)
    assert staged.exists()


@pytest.mark.parametrize("given, expected", [
    ("", ["."]),
    ("tests/", ["."]),
    ("tests/test_base.py::test_shell tests/test_base.py::test_ssh",
     ["test_base.py::test_shell", "test_base.py::test_ssh"]),
    ("test_lan.py -k dhcp", ["test_lan.py", "-k", "dhcp"]),
])
def test_selection_relative_to_tests_dir(tmp_path, layout, given, expected):
    tests, _, _ = layout
    assert make_agent(tmp_path, tests)._selection(given) == expected


def test_selection_keeps_real_tests_subdir(tmp_path, layout):
    tests, _, _ = layout
    (tests / "tests").mkdir()
    assert make_agent(tmp_path, tests)._selection("tests/test_x.py") == ["tests/test_x.py"]
