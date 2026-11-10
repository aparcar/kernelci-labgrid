"""pytest plugin of labgrid-agent: what pytest is doing, as it happens.

The agent copies this file next to a run as `kci_progress.py` and loads it
with `-p kci_progress` and PYTHONPATH, so it works in whatever environment
runs pytest (the agent's own, or the tests repo's via `uv run`) without
being installed there. It has no imports from the agent for that reason.

One JSON object per line goes to $KCI_PROGRESS_FILE, flushed at once:

  {"event": "collected", "tests": [nodeid, ...]}
  {"event": "start", "nodeid": ..., "console": <bytes>}
  {"event": "result", "nodeid": ..., "outcome": ..., "duration": <s>, "message": ...}
  {"event": "finish", "exitstatus": <int>}

each with "t" (Unix time). "console" is the size of the labgrid console
log (console_* in $KCI_CONSOLE_DIR, written by --lg-log) when the test
started: where its part of the console begins. outcome is passed, failed
(the test itself), error (setup or teardown), skipped or xfailed; message
(not for passed) says why: the failure, or the skip or xfail reason.
"""

import json
import os
import time
from pathlib import Path

_out = None
_running: dict[str, dict] = {}


def _write(event: dict) -> None:
    if _out is None:
        return
    event["t"] = round(time.time(), 3)
    _out.write(json.dumps(event) + "\n")
    _out.flush()


def _console_size() -> int | None:
    directory = os.environ.get("KCI_CONSOLE_DIR")
    if not directory:
        return None
    logs = sorted(Path(directory).glob("console_*"))
    try:
        return logs[0].stat().st_size if logs else 0
    except OSError:
        return None


def pytest_configure(config):
    global _out
    path = os.environ.get("KCI_PROGRESS_FILE")
    # xdist workers would write over each other: only the main process
    if path and not hasattr(config, "workerinput"):
        _out = open(path, "a", encoding="utf-8")


def pytest_unconfigure(config):
    global _out
    if _out is not None:
        _out.close()
        _out = None


def pytest_collection_finish(session):
    _write({"event": "collected", "tests": [item.nodeid for item in session.items]})


def pytest_runtest_logstart(nodeid, location):
    _running[nodeid] = {"outcome": "passed", "duration": 0.0}
    _write({"event": "start", "nodeid": nodeid, "console": _console_size()})


def _message(report) -> str:
    """Why a test failed, errored, was skipped or is expected to fail: what
    the result nodes get as error_msg later (as far as a report tells)."""
    if hasattr(report, "wasxfail"):
        return str(report.wasxfail or "")
    longrepr = report.longrepr
    if report.skipped and isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2]).removeprefix("Skipped: ")
    crash = getattr(longrepr, "reprcrash", None)
    if crash is not None and getattr(crash, "message", None):
        return str(crash.message)
    text = (getattr(report, "longreprtext", "") or "").strip()
    return text.splitlines()[-1] if text else ""


def pytest_runtest_logreport(report):
    state = _running.setdefault(report.nodeid, {"outcome": "passed", "duration": 0.0})
    state["duration"] += report.duration
    if report.failed:
        # the first failure counts: a teardown error after a failed test
        # leaves it failed
        if state["outcome"] not in ("failed", "error"):
            state["outcome"] = "failed" if report.when == "call" else "error"
            state["message"] = _message(report)
    elif report.skipped and state["outcome"] == "passed":
        state["outcome"] = "xfailed" if hasattr(report, "wasxfail") else "skipped"
        state["message"] = _message(report)


def pytest_runtest_logfinish(nodeid, location):
    state = _running.pop(nodeid, {"outcome": "passed", "duration": 0.0})
    event = {"event": "result", "nodeid": nodeid, "outcome": state["outcome"],
             "duration": round(state["duration"], 3)}
    if state.get("message"):
        event["message"] = state["message"][:1000]
    _write(event)


def pytest_sessionfinish(session, exitstatus):
    _write({"event": "finish", "exitstatus": int(exitstatus)})
