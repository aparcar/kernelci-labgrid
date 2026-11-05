"""Test suite from a git repository, and pytest in a container."""

import asyncio
import getpass
import os
import socket
import subprocess
from pathlib import Path

import pytest

from kernelci_labgrid.config import load_config
from kernelci_labgrid.scheduler.pull_agent import LabgridAgent
from kernelci_labgrid.suite import Container, TestsRepo


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def upstream(tmp_path):
    """A tests repository in the openwrt-tests layout."""
    repo = tmp_path / "upstream"
    for d in ("tests", "targets", "strategies"):
        (repo / d).mkdir(parents=True)
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    (repo / "targets" / "qemu_x86-64.yaml").write_text("targets: {}\n")
    (repo / "strategies" / "s.py").write_text("")
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "one")
    return repo


def commit(repo: Path, name: str) -> str:
    (repo / "tests" / f"{name}.py").write_text("")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", name)
    return git(repo, "rev-parse", "HEAD")


async def test_repo_snapshots_per_commit(tmp_path, upstream):
    repo = TestsRepo(str(upstream), "main", tmp_path / "work")
    first = await repo.update()
    assert first.revision == git(upstream, "rev-parse", "HEAD")
    assert (first.tests_dir / "test_a.py").is_file()
    assert (first.targets_dir / "qemu_x86-64.yaml").is_file()
    assert first.strategies_dir == first.tests_dir.parent / "strategies"
    # Read-only: a test can't change the suite later runs get
    assert not os.access(first.tests_dir / "test_a.py", os.W_OK) or os.geteuid() == 0
    assert not (first.tests_dir / "test_a.py").stat().st_mode & 0o222

    # Unchanged: same snapshot
    assert await repo.update() is first

    # A new commit while a run uses the old one: the old one stays until done
    with repo.use() as running:
        second_rev = commit(upstream, "test_b")
        second = await repo.update()
        assert second.revision == second_rev
        assert (second.tests_dir / "test_b.py").is_file()
        assert running is first and first.tests_dir.is_dir()
    assert not first.tests_dir.parent.exists()
    assert second.tests_dir.is_dir()


async def test_repo_ref_can_be_a_commit(tmp_path, upstream):
    first = git(upstream, "rev-parse", "HEAD")
    commit(upstream, "test_b")
    git(upstream, "config", "uploadpack.allowAnySHA1InWant", "true")
    suite = await TestsRepo(str(upstream), first, tmp_path / "work").update()
    assert suite.revision == first
    assert not (suite.tests_dir / "test_b.py").exists()


async def test_repo_fetch_failure_keeps_current(tmp_path, upstream):
    agent = LabgridAgent(api_url="http://x/latest", api_token="t", lab_name="lab",
                         tests_dir=".", artifact_dir=tmp_path / "a", platforms=["qemu_x86-64"],
                         coordinator="", tests_repo=str(upstream), tests_ref="main",
                         tests_update_interval=0)
    await agent._update_suite()
    suite = agent.suite
    assert agent.targets_dir == suite.targets_dir
    assert agent._find_target_yaml("qemu_x86-64") == suite.targets_dir / "qemu_x86-64.yaml"
    assert agent._node_data({"id": "n1", "data": {"platform": "p"}}).get("tests_revision") is None

    agent.tests_repo.url = str(tmp_path / "gone")
    agent.tests_repo._last_fetch = None
    await agent._update_suite()  # logs a warning
    assert agent.suite is suite


async def test_repo_unreachable_at_start_fails(tmp_path):
    agent = LabgridAgent(api_url="http://x/latest", api_token="t", lab_name="lab",
                         tests_dir=".", artifact_dir=tmp_path / "a", platforms=["qemu_x86-64"],
                         coordinator="", tests_repo=str(tmp_path / "gone"))
    with pytest.raises(RuntimeError, match="git fetch failed"):
        await agent.start()


def test_container_command(tmp_path):
    (tmp_path / "suite" / "tests").mkdir(parents=True)
    (tmp_path / "out").mkdir()
    fw = tmp_path / "fw.bin"
    fw.write_bytes(b"")
    c = Container("localhost/runner", args=("--userns=keep-id",))
    cmd = c.run_command("lg-1", ["python3", "-m", "pytest", "-v"],
                        workdir=tmp_path / "suite" / "tests",
                        mounts=[(tmp_path / "suite" / "tests", False), (tmp_path / "suite" / "tests", False),
                                (tmp_path / "missing", False), (fw, False), (tmp_path / "out", True)],
                        env={"LG_IMAGE": str(fw)})
    assert cmd[:5] == ["podman", "run", "--rm", "--name", "lg-1"]
    vols = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--volume"]
    assert vols == [f"{tmp_path}/suite/tests:{tmp_path}/suite/tests:ro",
                    f"{fw}:{fw}:ro", f"{tmp_path}/out:{tmp_path}/out:rw"]
    assert "--cap-drop=all" in cmd and ["--network", "host"] == cmd[5:7]
    assert f"LG_IMAGE={fw}" in cmd
    assert cmd[-7:] == ["--userns=keep-id", "--entrypoint", "python3", "localhost/runner",
                        "-m", "pytest", "-v"]


FAKE_PODMAN = """#!/bin/sh
echo "$@" >> {log}
case "$1" in
  run) env > {envlog}; exec sleep {sleep} ;;
  rm) exit 0 ;;
esac
"""


def fake_podman(tmp_path, sleep=0):
    script = tmp_path / "podman"
    script.write_text(FAKE_PODMAN.format(log=tmp_path / "podman.log",
                                         envlog=tmp_path / "podman.env", sleep=sleep))
    script.chmod(0o755)
    return script


@pytest.fixture
def container_agent(tmp_path, upstream):
    def make(sleep=0):
        return LabgridAgent(
            api_url="http://x/latest", api_token="secret-token", lab_name="lab",
            tests_dir=".", artifact_dir=tmp_path / "a", platforms=["qemu_x86-64"],
            coordinator="", tests_repo=str(upstream), tests_ref="main",
            container_image="localhost/runner",
            container_command=str(fake_podman(tmp_path, sleep)))
    return make


async def test_pytest_runs_in_container(tmp_path, container_agent):
    agent = container_agent()
    await agent._update_suite()
    suite = agent.suite
    fw = tmp_path / "fw.bin"
    fw.write_bytes(b"")
    with agent._use_suite() as s:
        await agent._run_pytest(target_yaml=s.targets_dir / "qemu_x86-64.yaml", firmware=fw,
                                pytest_args="tests/test_a.py", timeout=30,
                                outdir=tmp_path / "out", extra_env={"LG_PLACE": "p1"}, suite=s)
    args = (tmp_path / "podman.log").read_text().split()
    assert args[0] == "run"
    # The whole snapshot: pytest's settings are in the repository's pyproject.toml
    assert f"{suite.root.resolve()}:{suite.root.resolve()}:ro" in args
    assert "LG_PLACE=p1" in args and f"LG_IMAGE={fw}" in args
    # The place's lock owner, as the agent locked it
    assert f"LG_HOSTNAME={socket.gethostname()}" in args
    assert f"LG_USERNAME={getpass.getuser()}" in args
    i = args.index("--entrypoint")
    assert args[i + 1:i + 5] == ["python3", "localhost/runner", "-m", "pytest"]
    assert "test_a.py" in args
    assert "secret-token" not in " ".join(args)
    # Exited by itself: --rm cleaned up, no podman rm
    assert "rm" not in [line.split()[0] for line in (tmp_path / "podman.log").read_text().splitlines()]


async def test_container_removed_on_timeout(tmp_path, container_agent):
    agent = container_agent(sleep=30)
    await agent._update_suite()
    fw = tmp_path / "fw.bin"
    fw.write_bytes(b"")
    result = await asyncio.wait_for(agent._run_pytest(
        target_yaml=agent.targets_dir / "qemu_x86-64.yaml", firmware=fw, pytest_args="",
        timeout=1, outdir=tmp_path / "out"), timeout=10)
    assert result.errors == 1
    lines = (tmp_path / "podman.log").read_text().splitlines()
    name = lines[0].split()[lines[0].split().index("--name") + 1]
    assert lines[-1] == f"rm --force --time 0 {name}"


def test_config_tables(tmp_path):
    config = tmp_path / "lab.toml"
    config.write_text("""
lab_name = "lab"

[tests]
repo = "https://example.org/tests.git"
ref = "v1"
update_interval = 60

[container]
image = "localhost/runner"
args = "--userns=keep-id --memory 2g"
""")
    s = load_config(config)
    assert s["tests_repo"] == "https://example.org/tests.git"
    assert s["tests_ref"] == "v1" and s["tests_update_interval"] == 60
    assert s["container_image"] == "localhost/runner"
    assert s["container_args"] == ["--userns=keep-id", "--memory", "2g"]

    config.write_text('[container]\nimage = "x"\nimgae = "y"\n')
    with pytest.raises(ValueError, match="imgae"):
        load_config(config)
