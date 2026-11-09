"""The live view: chunked logs, progress from the pytest plugin, the index."""

import asyncio
import json
import shutil
import subprocess
import sys

import pytest

from kernelci_labgrid.live import (
    CHUNK_BYTES,
    EventReader,
    LiveLog,
    LiveSession,
    Progress,
    split_nodeid,
)
from kernelci_labgrid.scheduler.pull_agent import PROGRESS_MODULE, PROGRESS_PLUGIN, LabgridAgent


class FakeStorage:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.fail = False

    async def upload(self, dest, name, content):
        if self.fail:
            return None
        self.files[f"{dest}/{name}"] = content
        return f"http://storage/{dest}/{name}"

    def index(self, dest="live"):
        return json.loads(self.files[f"{dest}/index.json"])


# ---------------- LiveLog ----------------

def test_chunks_end_at_line_ends(tmp_path):
    path = tmp_path / "log"
    path.write_bytes(b"one\ntwo\nthr")
    log = LiveLog("x", lambda: path)
    data = log.peek()
    assert data == b"one\ntwo\n"
    log.commit(data)
    assert log.peek() is None  # "thr" waits for its line end
    with path.open("ab") as f:
        f.write(b"ee\n")
    assert log.peek() == b"three\n"


def test_final_takes_the_rest(tmp_path):
    path = tmp_path / "log"
    path.write_bytes(b"no newline")
    log = LiveLog("x", lambda: path)
    assert log.peek() is None
    assert log.peek(final=True) == b"no newline"


def test_long_line_goes_out_in_pieces(tmp_path):
    path = tmp_path / "log"
    path.write_bytes(b"x" * 10)
    log = LiveLog("x", lambda: path, chunk_bytes=4)
    assert log.peek() == b"xxxx"


def test_not_taken_until_committed(tmp_path):
    path = tmp_path / "log"
    path.write_bytes(b"a\n")
    log = LiveLog("x", lambda: path)
    assert log.peek() == log.peek() == b"a\n"
    assert log.state() == {"chunks": 0, "bytes": 0}


def test_missing_file(tmp_path):
    assert LiveLog("x", lambda: None).peek() is None
    assert LiveLog("x", lambda: tmp_path / "nope").peek() is None


# ---------------- progress ----------------

@pytest.mark.parametrize("nodeid,expected", [
    ("tests/test_base.py::test_shell", ("base", "test_shell")),
    ("tests/test_wifi.py::TestAp::test_scan[2g]", ("wifi", "test_scan[2g]")),
    ("test_x.py::test_y", ("x", "test_y")),
])
def test_split_nodeid(nodeid, expected):
    assert split_nodeid(nodeid) == expected


def test_progress_from_events():
    p = Progress()
    assert p.state()["total"] is None
    p.feed({"event": "collected", "tests": ["t/test_a.py::test_1", "t/test_a.py::test_2"]})
    p.feed({"event": "start", "nodeid": "t/test_a.py::test_1", "console": 0, "t": 0})
    s = p.state()
    assert s["phase"] == "testing" and s["total"] == 2 and s["done"] == 0
    assert s["current"] == "t/test_a.py::test_1"
    p.feed({"event": "result", "nodeid": "t/test_a.py::test_1", "outcome": "failed", "duration": 1.5})
    p.feed({"event": "start", "nodeid": "t/test_a.py::test_2", "console": 120, "t": 1})
    s = p.state()
    assert s["done"] == 1 and s["counts"] == {"fail": 1}
    first, second = s["tests"]
    assert first == {"id": "t/test_a.py::test_1", "module": "a", "name": "test_1",
                     "started": "1970-01-01T00:00:00Z", "console": 0, "result": "fail", "ms": 1500}
    assert second["console"] == 120 and "result" not in second
    p.feed({"event": "result", "nodeid": "t/test_a.py::test_2", "outcome": "xfailed", "duration": 0})
    p.feed({"event": "finish", "exitstatus": 1})
    s = p.state()
    assert s["current"] is None and s["counts"] == {"fail": 1, "xfail": 1}


def test_event_reader_waits_for_whole_lines(tmp_path):
    path = tmp_path / "p.jsonl"
    path.write_text('{"event": "a"}\n{"event": ')
    reader = EventReader(path)
    assert reader.read() == [{"event": "a"}]
    assert reader.read() == []
    with path.open("a") as f:
        f.write('"b"}\n')
    assert reader.read() == [{"event": "b"}]


# ---------------- the session ----------------

async def test_session_uploads_chunks_and_index(tmp_path):
    storage = FakeStorage()
    log = tmp_path / "pytest.log"
    log.write_bytes(b"line 1\nline 2\n")
    events = tmp_path / "progress.jsonl"
    events.write_text(json.dumps({"event": "collected", "tests": ["tests/test_a.py::test_1"]}) + "\n")
    live = LiveSession(storage.upload, "live")
    live.add_log("pytest", lambda: log)
    live.add_log("console", lambda: None)  # not there yet
    live.follow_events(events)

    await live.tick()
    assert storage.files["live/pytest/000001.txt"] == b"line 1\nline 2\n"
    index = storage.index()
    assert index["v"] == 1 and index["kind"] == "test" and index["done"] is False
    assert index["logs"] == {"pytest": {"chunks": 1, "bytes": 14}, "console": {"chunks": 0, "bytes": 0}}
    assert index["progress"]["total"] == 1

    with log.open("ab") as f:
        f.write(b"line 3\npartial")
    await live.tick()
    assert storage.files["live/pytest/000002.txt"] == b"line 3\n"

    await live.finish("reporting")
    assert storage.files["live/pytest/000003.txt"] == b"partial"
    index = storage.index()
    assert index["done"] is True and index["progress"]["phase"] == "reporting"
    assert index["logs"]["pytest"] == {"chunks": 3, "bytes": len(log.read_bytes())}
    # the chunks are the log
    assert b"".join(storage.files[f"live/pytest/00000{n}.txt"] for n in (1, 2, 3)) == log.read_bytes()


async def test_failed_upload_is_tried_again(tmp_path):
    storage = FakeStorage()
    log = tmp_path / "log"
    log.write_bytes(b"a\n")
    live = LiveSession(storage.upload, "live")
    live.add_log("pytest", lambda: log)
    storage.fail = True
    await live.tick()
    assert live.logs["pytest"].state() == {"chunks": 0, "bytes": 0}
    storage.fail = False
    await live.tick()
    assert storage.files["live/pytest/000001.txt"] == b"a\n"


async def test_index_only_rewritten_on_change_or_heartbeat(tmp_path):
    storage = FakeStorage()
    now = [0.0]
    live = LiveSession(storage.upload, "live", clock=lambda: now[0])
    uploads = []

    async def counting(dest, name, content):
        uploads.append(name)
        return await storage.upload(dest, name, content)

    live.upload = counting
    await live.tick()
    await live.tick()
    assert uploads == ["index.json"]
    now[0] = 31.0
    await live.tick()
    assert uploads == ["index.json", "index.json"]


async def test_large_backlog_is_spread_over_ticks(tmp_path):
    storage = FakeStorage()
    log = tmp_path / "log"
    line = b"x" * 1023 + b"\n"
    log.write_bytes(line * (CHUNK_BYTES // 1024) * 20)  # 20 chunks
    live = LiveSession(storage.upload, "live")
    live.add_log("build", lambda: log)
    await live.tick()
    assert live.logs["build"].chunks == 16
    await live.tick()
    assert live.logs["build"].chunks == 20


async def test_loop_runs_until_finished(tmp_path):
    storage = FakeStorage()
    log = tmp_path / "log"
    log.write_bytes(b"a\n")
    live = LiveSession(storage.upload, "live", interval=0.01)
    live.add_log("pytest", lambda: log)
    live.start()
    for _ in range(100):
        if "live/pytest/000001.txt" in storage.files:
            break
        await asyncio.sleep(0.01)
    await live.finish("done")
    assert storage.index()["done"] is True


# ---------------- the plugin, in a real pytest run ----------------

PLUGIN_TESTS = '''
import pytest

@pytest.fixture
def broken():
    raise RuntimeError("setup")

def test_pass():
    print("hello")

def test_fail():
    assert False

def test_skip():
    pytest.skip("no")

@pytest.mark.xfail(reason="known")
def test_xfail():
    assert False

def test_error(broken):
    pass

@pytest.mark.parametrize("band", ["2g", "5g"])
def test_param(band):
    pass
'''


def test_plugin_reports_a_real_run(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text(PLUGIN_TESTS)
    console = tmp_path / "out" / "console_main"
    console.parent.mkdir()
    console.write_bytes(b"boot\n")
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    shutil.copyfile(PROGRESS_PLUGIN, plugins / f"{PROGRESS_MODULE}.py")
    progress = tmp_path / "progress.jsonl"
    env = {"PYTHONPATH": str(plugins), "KCI_PROGRESS_FILE": str(progress),
           "KCI_CONSOLE_DIR": str(console.parent), "PATH": "/usr/bin:/bin"}
    subprocess.run([sys.executable, "-m", "pytest", "-p", PROGRESS_MODULE, "-p", "no:cacheprovider",
                    "-p", "no:labgrid", "-q", "tests"], cwd=tmp_path, env=env, capture_output=True)

    p = Progress()
    for event in EventReader(progress).read():
        p.feed(event)
    s = p.state()
    results = {t["name"]: t["result"] for t in s["tests"]}
    assert results == {"test_pass": "pass", "test_fail": "fail", "test_skip": "skip",
                       "test_xfail": "xfail", "test_error": "incomplete",
                       "test_param[2g]": "pass", "test_param[5g]": "pass"}
    assert s["total"] == 7 and s["done"] == 7 and s["current"] is None
    assert all(t["module"] == "demo" and t["console"] == 5 for t in s["tests"])
    assert p.exitstatus == 1


# ---------------- the agent ----------------

@pytest.fixture
def agent(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    return LabgridAgent(api_url="http://x/latest", api_token="t", lab_name="lab",
                        tests_dir=tests, artifact_dir=tmp_path / "artifacts",
                        storage_url="http://storage/", storage_token="s",
                        platforms=["qemu_x86-64"])


async def test_claim_announces_the_live_view(agent, monkeypatch):
    stored = {"id": "j1", "state": "available", "name": "openwrt-tests",
              "data": {"runtime": "lab", "platform": "qemu_x86-64", "misc": {"openwrt": {}}}}

    async def fake_get(endpoint, **params):
        return json.loads(json.dumps(stored))

    async def fake_put(endpoint, data):
        stored.update(json.loads(json.dumps(data)))
        return data

    monkeypatch.setattr(agent, "_api_get", fake_get)
    monkeypatch.setattr(agent, "_api_put", fake_put)
    monkeypatch.setattr("kernelci_labgrid.scheduler.pull_agent.random.uniform", lambda a, b: 0)
    claimed = await agent._claim({"id": "j1"})
    assert claimed["data"]["misc"]["live"] == "http://storage/logs/lab/j1/live/index.json"


def test_no_live_view_without_storage(tmp_path):
    agent = LabgridAgent(api_url="http://x", api_token="t", lab_name="lab", tests_dir=tmp_path)
    assert not agent._live_enabled


def test_results_drop_the_live_pointer(agent):
    from kernelci_labgrid.scheduler.pull_agent import TestResult
    job = {"id": "j1", "path": ["checkout", "b", "openwrt-tests"],
           "data": {"platform": "qemu_x86-64", "misc": {"openwrt": {"name": "x"},
                                                         "live": "http://storage/i.json"}}}
    node = agent._build_hierarchy(job, TestResult(total=1, passed=1, returncode=0), {})["node"]
    assert "live" not in node["data"]["misc"]
    assert node["data"]["misc"]["openwrt"] == {"name": "x"}


async def test_run_pytest_with_live_view(agent, tmp_path):
    """The agent's own pytest run with the plugin: progress and the console
    offset arrive while it runs, the live session uploads them."""
    # labgrid's own pytest plugin runs (--lg-env, --lg-log); --firmware is
    # openwrt-tests' option
    (agent.tests_dir / "conftest.py").write_text(
        "def pytest_addoption(parser):\n"
        "    parser.addoption('--firmware')\n")
    (agent.tests_dir / "test_base.py").write_text("def test_shell():\n    print('ok')\n")
    agent.pytest_command = [sys.executable, "-m", "pytest"]
    target = tmp_path / "targets" / "qemu_x86-64.yaml"
    target.parent.mkdir()
    target.write_text("targets: {}\n")
    outdir = tmp_path / "out"
    outdir.mkdir()
    (outdir / "console_main").write_bytes(b"stale console of an earlier run\n")

    storage = FakeStorage()
    live = agent._new_live("j1", outdir)
    live.upload = storage.upload
    result = await agent._run_pytest(target_yaml=target, firmware=tmp_path / "fw", pytest_args="",
                                     timeout=60, outdir=outdir, live=live)
    await live.finish("reporting")

    assert result.returncode == 0, result.output
    assert not (outdir / "console_main").exists()  # the earlier run's is gone
    index = storage.index("logs/lab/j1/live")
    assert index["done"] and index["progress"]["counts"] == {"pass": 1}
    assert index["progress"]["tests"][0]["name"] == "test_shell"
    assert b"test_shell" in storage.files["logs/lab/j1/live/pytest/000001.txt"]
