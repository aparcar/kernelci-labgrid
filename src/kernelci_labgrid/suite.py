"""The test suite: where it comes from (a local directory or a git
repository the agent keeps up to date) and how it runs (in the agent's own
environment or in a podman container).

Tests repository: the agent fetches `ref` of `repo` every `update_interval`
seconds and extracts each new commit into its own read-only snapshot
directory (`<workdir>/<commit>/`, the repository's tests/, targets/ and
strategies/). A job or health check keeps the snapshot it started with, so
an update never changes the files under a running test; snapshots no run
uses anymore are removed. So the lab's test suite is updated by pushing to
the repository, without logging in to the lab host.

Container: pytest runs as `podman run <image> ...` instead of a local
process. The container sees only what the run needs, mounted at the same
paths as on the host: the snapshot (read-only), the firmware (read-only) and
the run's output directory, plus /dev/kvm for QEMU. It gets none of the
agent's environment, so not its API and storage tokens. See README.md
("Tests repository and container") for what this does and doesn't protect.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import tarfile
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from kernelci_labgrid.labgrid import kill_group, spawn

logger = logging.getLogger(__name__)

GIT_TIMEOUT = 300


@dataclass(frozen=True)
class Suite:
    """One version of the test suite: its three directories, and for a
    tests repository the commit they were extracted from."""

    tests_dir: Path
    targets_dir: Path
    strategies_dir: Path
    revision: str | None = None
    repo: str | None = None
    # The repository snapshot (with pyproject.toml, which holds pytest's
    # settings); None for local directories
    root: Path | None = None

    def mounts(self) -> list[Path]:
        """What a container needs to see of the suite."""
        if self.root:
            return [self.root]
        return [self.tests_dir, self.targets_dir, self.strategies_dir]

    @classmethod
    def local(cls, tests_dir: str | Path, targets_dir: str | Path | None = None,
              strategies_dir: str | Path | None = None) -> Suite:
        tests = Path(tests_dir)
        targets = Path(targets_dir) if targets_dir else tests.parent / "targets"
        strategies = Path(strategies_dir) if strategies_dir else targets.parent / "strategies"
        return cls(tests, targets, strategies)

    def info(self) -> dict[str, str]:
        """Node data describing this suite (empty for a local directory)."""
        if not self.revision:
            return {}
        return {"tests_repo": self.repo or "", "tests_revision": self.revision}


class TestsRepo:
    """A git repository (openwrt-tests layout: tests/, targets/,
    strategies/) kept up to date in read-only snapshots per commit."""

    __test__ = False  # not a pytest test class

    def __init__(self, url: str, ref: str, workdir: str | Path, *,
                 update_interval: float = 300, git_command: str = "git"):
        self.url = url
        self.ref = ref or "HEAD"
        self.workdir = Path(workdir)
        self.update_interval = update_interval
        self.git = shlex.split(git_command)
        self.current: Suite | None = None
        self._in_use: Counter[str] = Counter()
        self._last_fetch: float | None = None

    @property
    def _git_dir(self) -> Path:
        return self.workdir / "repo.git"

    def due(self) -> bool:
        if self._last_fetch is None:
            return True
        return bool(self.update_interval) and \
            time.monotonic() - self._last_fetch >= self.update_interval

    async def _run_git(self, *args: str, stdout=asyncio.subprocess.PIPE) -> bytes:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        proc = await spawn(*self.git, "--git-dir", str(self._git_dir), *args,
                           stdout=stdout, stderr=asyncio.subprocess.PIPE, env=env)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            await kill_group(proc)
            raise
        await kill_group(proc)
        if proc.returncode:
            raise RuntimeError(f"git {args[0]} failed: {err.decode(errors='replace').strip()}")
        return out or b""

    async def update(self) -> Suite:
        """Fetch the ref; extract its commit if new. Returns the current
        suite. Raises if the fetch fails (the previous suite stays current)."""
        self._last_fetch = time.monotonic()
        if not (self._git_dir / "HEAD").exists():
            self._git_dir.mkdir(parents=True, exist_ok=True)
            await self._run_git("init", "--bare", "--quiet")
        # A commit hash, branch or tag; depth 1: only the files are needed
        await self._run_git("fetch", "--quiet", "--depth", "1", "--force", "--no-tags",
                            self.url, self.ref)
        commit = (await self._run_git("rev-parse", "FETCH_HEAD^{commit}")).decode().strip()

        if not self.current or self.current.revision != commit:
            root = self.workdir / commit
            if not root.is_dir():
                await self._extract(commit, root)
            previous, self.current = self.current, Suite(
                root / "tests", root / "targets", root / "strategies", commit, self.url, root)
            logger.info(f"Tests repository {self.url} {self.ref}: "
                        f"{previous.revision[:12] + ' -> ' if previous else ''}{commit[:12]}")
            self._cleanup()
        return self.current

    async def _extract(self, commit: str, root: Path) -> None:
        tmp = self.workdir / f".{commit}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        archive = self.workdir / f".{commit}.tar"
        try:
            with archive.open("wb") as f:
                await self._run_git("archive", "--format=tar", commit, stdout=f)
            await asyncio.to_thread(_untar, archive, tmp)
            tmp.rename(root)
        finally:
            archive.unlink(missing_ok=True)
            shutil.rmtree(tmp, ignore_errors=True)

    @contextmanager
    def use(self) -> Iterator[Suite]:
        """The current suite, kept on disk until the block ends."""
        if not self.current:
            raise RuntimeError(f"Tests repository {self.url} not fetched yet")
        suite = self.current
        self._in_use[suite.revision] += 1
        try:
            yield suite
        finally:
            self._in_use[suite.revision] -= 1
            if not self._in_use[suite.revision]:
                del self._in_use[suite.revision]
                self._cleanup()

    def _cleanup(self) -> None:
        """Remove snapshots that are neither current nor in use."""
        keep = {s for s in self._in_use} | {self.current.revision if self.current else None}
        for path in self.workdir.iterdir():
            # (repo.git, and .<commit>.tmp of an extraction in progress)
            if path.is_dir() and not path.name.startswith((".", "repo.git")) \
                    and path.name not in keep:
                shutil.rmtree(path, ignore_errors=True)
                logger.debug(f"Removed tests snapshot {path.name[:12]}")


def _untar(archive: Path, dest: Path) -> None:
    with tarfile.open(archive) as tar:
        tar.extractall(dest, filter="data")
    # Read-only for everyone: tests can't change the suite of later runs
    for dirpath, dirnames, filenames in os.walk(dest):
        for name in filenames:
            p = Path(dirpath, name)
            if not p.is_symlink():
                p.chmod(p.stat().st_mode & 0o555)


@dataclass
class Container:
    """Runs pytest in a podman (or docker) container."""

    image: str
    command: str = "podman"
    network: str = "host"
    args: tuple[str, ...] = ()

    def run_command(self, name: str, cmd: list[str], *, workdir: Path,
                    mounts: list[tuple[Path, bool]], env: dict[str, str]) -> list[str]:
        """The `podman run` command line running `cmd` (pytest) in the
        container. mounts: (host path, writable), mounted at the same path."""
        run = [*shlex.split(self.command), "run", "--rm", "--name", name,
               "--network", self.network,
               # No privileges beyond what QEMU and pytest need
               "--cap-drop=all", "--security-opt=no-new-privileges",
               "--workdir", str(workdir)]
        seen: set[Path] = set()
        for path, writable in mounts:
            path = Path(path).resolve()
            if path in seen or not path.exists():
                continue
            seen.add(path)
            run += ["--volume", f"{path}:{path}:{'rw' if writable else 'ro'}"]
        if Path("/dev/kvm").exists():
            run += ["--device", "/dev/kvm"]
        for key, value in sorted(env.items()):
            run += ["--env", f"{key}={value}"]
        run += [*self.args, "--entrypoint", cmd[0], self.image, *cmd[1:]]
        return run

    async def remove(self, name: str) -> None:
        """Stop and remove the container (killing `podman run` leaves it
        running); never raises."""
        try:
            proc = await spawn(*shlex.split(self.command), "rm", "--force", "--time", "0", name,
                               stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(proc.wait(), timeout=60)
            finally:
                await kill_group(proc)
        except Exception as e:
            logger.warning(f"Removing container {name} failed: {e}")
