"""Live view of a running job: its logs and test progress while it runs.

While a job runs, the agent uploads to kernelci-storage, every few seconds:

  <dir>/<log>/000001.txt, 000002.txt, ...   the new part of each log
  <dir>/index.json                          what exists so far, and progress

<dir> is logs/<lab>/<job id>/live; the job node points at the index
(data.misc.live). Chunks are written once and never changed: a reader never
sees a file that is being replaced, and needs no Range requests. A chunk
ends at a line end (except the last one, and lines longer than a chunk),
so chunk n+1 continues chunk n byte for byte: byte offsets into a log
(a test's "console" offset) are offsets into the chunks one after another.
The index is replaced on every write; a reader that gets a half-written
one (invalid JSON) tries again next time. Layout and fields: openwrtci's
deploy/DATA.md, "Live view".
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

INDEX_VERSION = 1
CHUNK_BYTES = 256 * 1024
# at most this many chunks per log and tick (a log that grew by a lot is
# caught up over several ticks instead of blocking the others)
CHUNKS_PER_TICK = 16
INTERVAL_S = 5.0
# write the index at least this often, so readers see the job is alive
INDEX_HEARTBEAT_S = 30.0

# the plugin's outcomes -> the results Maestro and the web UI use
OUTCOMES = {"passed": "pass", "failed": "fail", "error": "incomplete",
            "skipped": "skip", "xfailed": "xfail"}


def iso(t: float | None = None) -> str:
    moment = datetime.fromtimestamp(t, UTC) if t is not None else datetime.now(UTC)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def chunk_name(n: int) -> str:
    return f"{n:06d}.txt"


class LiveLog:
    """A growing file, taken in chunks that end at a line end."""

    def __init__(self, name: str, find: Callable[[], Path | None],
                 chunk_bytes: int = CHUNK_BYTES):
        self.name = name
        self.find = find  # the file, once it exists (e.g. labgrid's console_*)
        self.chunk_bytes = chunk_bytes
        self.offset = 0  # bytes uploaded
        self.chunks = 0

    def peek(self, final: bool = False) -> bytes | None:
        """The next chunk, or None. Not taken until commit(): an upload
        that fails is tried again."""
        path = self.find()
        if not path:
            return None
        try:
            with path.open("rb") as f:
                f.seek(self.offset)
                data = f.read(self.chunk_bytes)
        except OSError:
            return None
        if not data:
            return None
        if final:
            return data
        end = data.rfind(b"\n")
        if end >= 0:
            return data[:end + 1]
        # a line longer than a chunk goes out in pieces; a short one waits
        return data if len(data) >= self.chunk_bytes else None

    def commit(self, data: bytes) -> None:
        self.offset += len(data)
        self.chunks += 1

    def state(self) -> dict[str, int]:
        return {"chunks": self.chunks, "bytes": self.offset}


def split_nodeid(nodeid: str) -> tuple[str, str]:
    """'tests/test_wifi.py::TestAp::test_scan[2g]' -> ('wifi', 'test_scan[2g]'):
    the module and test names the result nodes get (the agent's _module_name
    on the JUnit classname, and the JUnit test name)."""
    path, _, rest = nodeid.partition("::")
    stem = Path(path).stem
    module = stem.removeprefix("test_") or stem or "tests"
    return module, (rest.rsplit("::", 1)[-1] if rest else nodeid)


class Progress:
    """What pytest is doing, from the plugin's events (progress_plugin.py)."""

    def __init__(self) -> None:
        self.phase = "starting"
        self.started = iso()
        self.tests: dict[str, dict[str, Any]] = {}  # nodeid -> test, in order
        self.collected = False
        self.current: str | None = None
        self.exitstatus: int | None = None

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def _test(self, nodeid: str) -> dict[str, Any]:
        if nodeid not in self.tests:
            module, name = split_nodeid(nodeid)
            self.tests[nodeid] = {"id": nodeid, "module": module, "name": name}
        return self.tests[nodeid]

    def feed(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "collected":
            self.collected = True
            for nodeid in event.get("tests") or []:
                self._test(nodeid)
            self.phase = "testing"
        elif kind == "start" and event.get("nodeid"):
            test = self._test(event["nodeid"])
            test["started"] = iso(event.get("t"))
            if event.get("console") is not None:
                test["console"] = event["console"]
            self.current = event["nodeid"]
            self.phase = "testing"
        elif kind == "result" and event.get("nodeid"):
            test = self._test(event["nodeid"])
            test["result"] = OUTCOMES.get(event.get("outcome"), "incomplete")
            test["ms"] = int(float(event.get("duration") or 0) * 1000)
            if event.get("message"):
                test["message"] = event["message"]
            if self.current == event["nodeid"]:
                self.current = None
        elif kind == "finish":
            self.exitstatus = event.get("exitstatus")
            self.current = None

    def state(self) -> dict[str, Any]:
        tests = list(self.tests.values())
        counts: dict[str, int] = {}
        for t in tests:
            if "result" in t:
                counts[t["result"]] = counts.get(t["result"], 0) + 1
        return {
            "phase": self.phase,
            "started": self.started,
            # null until pytest collected the tests
            "total": len(tests) if self.collected else None,
            "done": sum(counts.values()),
            "counts": counts,
            "current": self.current,
            "tests": tests,
        }


class EventReader:
    """New lines of the plugin's JSON lines file."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0

    def read(self) -> list[dict[str, Any]]:
        try:
            with self.path.open("rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []
        self.offset += end + 1
        events = []
        for line in data[:end].splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                logger.debug(f"bad progress line: {line[:200]!r}")
        return events


# upload(dest dir, file name, content) -> URL or None
Upload = Callable[[str, str, bytes], Awaitable[str | None]]


class LiveSession:
    """Uploads a job's logs and progress while it runs (see the module doc)."""

    def __init__(self, upload: Upload, dest: str, *, kind: str = "test",
                 interval: float = INTERVAL_S, clock: Callable[[], float] = time.monotonic):
        self.upload = upload
        self.dest = dest  # .../live
        self.kind = kind
        self.interval = interval
        self.clock = clock
        self.logs: dict[str, LiveLog] = {}
        self.progress = Progress()
        self.events: EventReader | None = None
        self.done = False
        self._last_index: str | None = None
        self._last_index_at = 0.0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    def add_log(self, name: str, find: Callable[[], Path | None]) -> None:
        self.logs[name] = LiveLog(name, find)

    def follow_events(self, path: Path) -> None:
        self.events = EventReader(path)

    def set_phase(self, phase: str) -> None:
        self.progress.set_phase(phase)

    def index(self) -> dict[str, Any]:
        return {
            "v": INDEX_VERSION,
            "kind": self.kind,
            "updated": iso(),
            "done": self.done,
            "logs": {name: log.state() for name, log in self.logs.items()},
            "progress": self.progress.state(),
        }

    async def tick(self, final: bool = False) -> None:
        """Upload what is new; on `final`, everything left and done=true."""
        async with self._lock:
            if self.events:
                for event in self.events.read():
                    self.progress.feed(event)
            for log in self.logs.values():
                for _ in range(CHUNKS_PER_TICK if not final else 1 << 30):
                    data = log.peek(final)
                    if not data:
                        break
                    if not await self.upload(f"{self.dest}/{log.name}", chunk_name(log.chunks + 1), data):
                        break  # tried again next tick
                    log.commit(data)
            if final:
                self.done = True
            await self._write_index()

    async def _write_index(self) -> None:
        """Upload the index if it changed (or as a heartbeat)."""
        index = self.index()
        # "updated" changes every second: compare without it
        key = json.dumps({**index, "updated": None}, sort_keys=True)
        now = self.clock()
        if key == self._last_index and now - self._last_index_at < INDEX_HEARTBEAT_S:
            return
        if await self.upload(self.dest, "index.json", json.dumps(index, separators=(",", ":")).encode()):
            self._last_index = key
            self._last_index_at = now

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as e:  # never break the job for its live view
                logger.warning(f"live update failed: {e}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None:
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._run())

    async def finish(self, phase: str | None = None) -> None:
        """Stop the loop and upload the rest. Never raises.

        The loop finishes its current tick instead of being cancelled: a
        chunk cut off mid-upload would be uploaded again with more in it,
        and a reader that already had the first version would miss that."""
        if self._task:
            self._stop.set()
            try:
                await asyncio.wait_for(self._task, timeout=60)
            except (asyncio.CancelledError, Exception):
                self._task.cancel()
            self._task = None
        try:
            await self.tick(final=True)
            if phase:  # after pytest's last events
                async with self._lock:
                    self.progress.set_phase(phase)
                    await self._write_index()
        except Exception as e:
            logger.warning(f"final live update failed: {e}")
