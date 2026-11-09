# KernelCI Labgrid Agent

A single daemon that connects your Labgrid test lab to KernelCI. Runs locally in your lab, polls KernelCI for jobs, executes your existing pytest tests, runs periodic health checks, and reports results back.

**No modifications needed to KernelCI infrastructure.**

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ KERNELCI INSTANCE (e.g. openwrtci, unchanged upstream services)  │
│                                                                  │
│  ┌──────────────────┐  ┌──────────────────┐  ┌────────────────┐  │
│  │ Maestro API      │  │ kernelci-storage │  │ KCIDB +        │  │
│  │ jobs, results    │  │ firmware, logs   │  │ dashboard      │  │
│  └──────────────────┘  └──────────────────┘  └────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
              ▲                      ▲
              │ HTTPS: poll, claim,  │ HTTPS: download firmware,
              │ report results       │ upload logs
              │                      │
┌─────────────┼──────────────────────┼─────────────────────────────┐
│ YOUR LAB    │                      │                             │
│             │                      │                             │
│  ┌──────────┴──────────────────────┴──────────────────────────┐  │
│  │ labgrid-agent                                              │  │
│  │                                                            │  │
│  │  1. run due health checks, report device health            │  │
│  │  2. poll available jobs for this lab's platforms, claim    │  │
│  │  3. fetch job definition, download + verify firmware       │  │
│  │  4. pytest --lg-env targets/<platform>.yaml                │  │
│  │                   --firmware <image>                       │  │
│  │  5. upload logs, submit test nodes + boot node             │  │
│  └─────────────────────────────┬──────────────────────────────┘  │
│                                │ runs your tests                 │
│                                ▼                                 │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │ openwrt-tests (your repo)                                  │  │
│  │   tests/test_*.py   pytest tests                           │  │
│  │   targets/*.yaml    labgrid environments                   │  │
│  └─────────────────────────────┬──────────────────────────────┘  │
│                                │ labgrid                         │
│                                ▼                                 │
│                  ┌───────────────────────────┐                   │
│                  │ QEMU, or labgrid exporter │                   │
│                  │ + your boards             │                   │
│                  └───────────────────────────┘                   │
└──────────────────────────────────────────────────────────────────┘
```

## Requirements

- Python 3.13+ (labgrid's `staging` branch needs it; the agent brings labgrid,
  pytest, pytest-check and pytest-harvest)
- A test suite, e.g. from openwrt-tests, in three directories that can live
  anywhere:
  - `tests_dir`: pytest tests incl. `conftest.py`; pytest runs there
  - `targets_dir`: labgrid target YAMLs, one per platform
  - `strategies_dir`: labgrid strategies the targets import as
    `../strategies/<file>.py` (default: `<targets_dir>/../strategies`). The
    agent stages symlinks per run, so the imports resolve wherever the
    directories are.
- A KernelCI (Maestro) API user in group `runtime:<lab-name>:node-editor`,
  plus `runtime:<pool>:node-editor` when taking jobs from a shared pool
- Optional: a kernelci-storage token allowed to upload below `logs/<lab-name>/`

## Installation

```bash
git clone https://github.com/aparcar/kernelci-labgrid.git
cd kernelci-labgrid
uv sync          # or: pip install -e .
```

## Usage

Put the settings into a TOML file (see
[`examples/labgrid-agent.toml`](examples/labgrid-agent.toml)) and run:

```bash
labgrid-agent --config labgrid-agent.toml
labgrid-agent --config labgrid-agent.toml --show-config   # effective settings, tokens masked
```

Without `--config` the agent reads `./labgrid-agent.toml`,
`~/.config/labgrid-agent/config.toml` or `/etc/labgrid-agent/config.toml`
(or `$LABGRID_AGENT_CONFIG`). Settings precedence, later wins: config file <
`--env-file` < environment variables < command line options. So the file can
hold everything, and single settings can be overridden ad hoc:

```bash
labgrid-agent --platform qemu_x86-64 --once
```

Without a config file, everything can be given as options and environment
variables, also readable from a KEY=VALUE `--env-file`. A QEMU-only lab sets
`--coordinator ""` (the default is a local coordinator, see
[Real hardware](#real-hardware-labgrid)):

```bash
labgrid-agent \
    --env-file /path/to/lab.env \
    --tests-dir /path/to/openwrt-tests/tests \
    --coordinator "" \
    --platform qemu_armsr-armv8 \
    --health-checks-dir examples/health_checks \
    --health-state-file /var/lib/labgrid/health_state.json
```

### Environment variables

| Variable | Setting |
|----------|---------|
| `LAB_NAME` | `lab_name` |
| `KCI_API_URL` | `api_url` (default: `https://api.kernelci.org/latest`) |
| `LAB_API_TOKEN`, `KCI_API_TOKEN` | `api_token` |
| `KCI_STORAGE_URL`, `LAB_STORAGE_TOKEN` | `storage_url`, `storage_token` |
| `LG_COORDINATOR` | `coordinator` |
| `LAB_POOL` | `pool` |
| `LAB_MAX_JOBS` | `max_jobs` |
| `LABGRID_TESTS_DIR`, `LABGRID_TARGETS_DIR`, `LABGRID_STRATEGIES_DIR` | test suite paths |
| `LABGRID_ARTIFACT_DIR`, `LABGRID_HEALTH_CHECKS_DIR`, `LABGRID_HEALTH_STATE_FILE` | agent paths |
| `LABGRID_AGENT_CONFIG` | config file, if no `--config` |

### Options

| Option | Description |
|--------|-------------|
| `--lab-name`, `-l` | Lab name, matched against the job's `data.runtime` (or `LAB_NAME`) |
| `--tests-dir`, `-t` | pytest tests incl. `conftest.py`; pytest runs there |
| `--targets-dir` | labgrid target YAMLs (default: `<tests_dir>/../targets`) |
| `--strategies-dir` | labgrid strategies imported by the targets (default: `<targets_dir>/../strategies`) |
| `--platform` | Platform this lab serves (`targets/<name>.yaml`), repeatable. With a coordinator: adds QEMU targets, hardware ones restrict the discovered list; without: the full list |
| `--api-url` | API URL including version, e.g. `http://localhost:8001/latest` (or `KCI_API_URL`) |
| `--api-token` | Lab API token (or `LAB_API_TOKEN` / `KCI_API_TOKEN`) |
| `--storage-url`, `--storage-token` | kernelci-storage for log uploads (or `KCI_STORAGE_URL` / `LAB_STORAGE_TOKEN`) |
| `--config` | TOML config file (see above) |
| `--show-config` | Print the effective settings and exit |
| `--env-file` | Read the settings above from a KEY=VALUE file |
| `--pytest-command` | How to run pytest in `tests_dir` (default: `python -m pytest` of the agent's environment) |
| `--pool` | Shared runtime to also take jobs from, e.g. `openwrt-labs` (or `LAB_POOL`) |
| `--coordinator` | The lab's labgrid-coordinator `host:port` (default: `127.0.0.1:20408`, or `LG_COORDINATOR`); `""` for a QEMU-only lab |
| `--labgrid-command` | labgrid-client command for power off (default: the agent environment's `labgrid-client`) |
| `--reserve-timeout` | Seconds to wait for a free place before leaving a job (default: 60) |
| `--max-jobs` | At most this many jobs at once, e.g. for QEMU on a small machine (default: one per platform, no limit; or `LAB_MAX_JOBS`). Also caps parallel health checks, counting running jobs (at least one check at a time) |
| `--poll-interval`, `-p` | Seconds between polls (default: 30) |
| `--once` | Poll once, wait for started jobs, exit |
| `--artifact-dir`, `-a` | Where to download firmware and keep job outputs |
| `--health-checks-dir`, `-c` | Directory with health check TOML files, one per device |
| `--health-state-file`, `-s` | JSON file to persist device health state |
| `--no-health-checks` | No health checks for all platforms (only `--health-checks-dir` ones) |
| `--debug`, `-d` | Enable debug logging |

## How It Works

1. **Checks health** - runs scheduled health checks if due, all devices in
   parallel (at most `max_jobs` at once, if set), and reports each device's
   health to the API (see [Health Reports](#health-reports))
2. **Polls** `GET /nodes?kind=job&state=available&data.runtime=<lab>&data.platform=<p>`
   for each healthy platform, one job per platform at a time; with `pool` set,
   then also `data.runtime=<pool>`. A pool lets several labs offering the same
   device share jobs: each job runs once, in whichever lab claims it first.
3. **Claims the job** by writing `data.job_id=<lab>:<uuid>` (pool jobs also get
   `data.runtime=<lab>`, `data.pool=<pool>`), then re-reads it after a random
   0.5-2 s and backs off if another lab's claim overwrote it (best effort, like
   kernelci/pullab_cloud, until kernelci-api has an atomic claim)
4. **Fetches the job definition** from `artifacts.job_definition`, downloads the
   firmware and verifies its sha256
5. **Runs pytest** in `tests_dir`, with the agent's own Python:
   ```bash
   python -m pytest <selection> --lg-env <run>/env/targets/<platform>.yaml \
       --firmware <image> --lg-log=<out> --junit-xml=<out>/results.xml \
       -o junit_logging=all
   ```
   `<run>/env/targets/<platform>.yaml` is a symlink to the target file next to
   `<run>/env/strategies` → `strategies_dir`.
6. **Uploads** console log, pytest log and JUnit XML to kernelci-storage, plus
   one log per test (`tests/<module>.<test>.log`: the console lines and log
   records during that test, from the JUnit XML, then its traceback). A test
   node's `test_log` is its own log; the job and boot nodes keep the full console
7. **Reports back** with `PUT /nodes/<job>`: the job node plus one child per test
   module and one leaf per test (paths `…/openwrt-tests/<module>/<test>`), and
   creates a sibling `boot` test node under the build (result from `test_shell`)

Failures where tests never ran (download, crash, timeout) mark the job
`incomplete` with `error_code: Infrastructure`. Firmware that never reaches a
shell (pytest exit code 3) is a real `fail`, not an infrastructure error.

Ctrl-C (SIGINT/SIGTERM) stops right away: running jobs are cancelled (pytest
and QEMU killed, the place powered off and unlocked) and given back to the
queue, pool jobs to the pool, so another lab or the next run picks them up.
A second Ctrl-C kills the test processes and exits immediately, leaving
places locked.

## Real hardware (labgrid)

Each lab runs its own labgrid-coordinator; the agent runs in the lab next to
it and only needs outbound HTTPS to the KernelCI instance. `coordinator`
defaults to `127.0.0.1:20408` (or `LG_COORDINATOR`), so real hardware is the
default case; a QEMU-only lab sets `coordinator = ""` and then serves exactly
its configured `platforms` (the agent refuses to start with neither). With a
coordinator:

- **Platforms** are the `device=<platform>` tags of the coordinator's places
  (openwrt-tests sets them from `labnet.yaml`). Local QEMU targets in
  `platforms` are added to them; hardware platforms in `platforms` restrict
  the discovered list, so a lab can list only its `qemu_*` targets and still
  serve all of its boards. On coordinator errors the previous list is kept.
- **Maintenance:** a place tagged `maintenance=<reason>`
  (`labgrid-client -p <place> set-tags maintenance=new_psu`) gets no jobs or
  health checks; a device whose places are all in maintenance is reported as
  failed (`maintenance: <reason>`). `set-tags maintenance=` (empty value)
  removes the tag again.
- **Before claiming** a job for a hardware platform, the agent reserves a place
  with `device=<platform>` and locks it. If no place is free within
  `reserve_timeout` (someone is debugging on the board), the job stays
  available for later or for another lab.
- **pytest** runs with `LG_COORDINATOR`, `LG_PLACE=<place>` and
  `LG_IMAGE=<firmware>`, exactly like a manual openwrt-tests run. The
  reservation is kept alive while the job runs.
- **Afterwards** the board is powered off (`labgrid-client power off`), the
  place unlocked and the reservation cancelled, also on errors and timeouts.
- The job's `data.device` is the place name, so results show which board ran.

Targets without `RemotePlace` (the `qemu_*` ones) run locally without a
place. The agent talks to the coordinator through labgrid's own client
library, pinned to the same labgrid as openwrt-tests.

## Health Checks

Similar to LAVA, the agent runs periodic health checks with a known-good
image. Results gate job execution (jobs for a `bad` device are left
`available` for other labs) and are reported to the API, where they are the
server's list of the lab's devices (see [Health Reports](#health-reports)).

### Health Check Configuration

Health checks run for all platforms of the lab by default. To turn them off,
set `health_checks = false` in the config file (a plain key, before `[api]`)
or pass `--no-health-checks`; then only devices in `health_checks_dir` are
checked. Settings go in a `[health_checks]` table (keep it after the plain
keys, like `[api]`):

```toml
[health_checks]
```

The golden image comes from the device's target file in openwrt-tests, so
every lab boots the same one: its `openwrt:` section names the image
(`target`, `profile`, `image.type`/`filesystem`, as in openwrt-tests'
`scripts/healthcheck.sh`), the sha256 comes from the release's
`profiles.json`. The release is the target's `healthcheck_version` if set
(for boards that regressed), `SNAPSHOT` for `snapshots_only` targets, else
the lab's `release` setting, else the current stable release
(downloads.openwrt.org/.versions.json).

```yaml
# openwrt-tests targets/enterasys_ws-ap3710i.yaml
openwrt:
  target: mpc85xx-p1020
  profile: enterasys_ws-ap3710i
  image: {type: kernel}
  healthcheck_version: "23.05.5"
```

Plain keys in `[health_checks]` apply to all devices, `[health_checks.<device>]`
tables override them per device:

```toml
[health_checks]
frequency_hours = 12

[health_checks.rpi-4]
enabled = false                  # no health check for this device

[health_checks.openwrt_one]
firmware = "https://example.org/my-known-good.itb"   # explicit golden image
sha256 = "…"
```

| Key | Description |
|-----|-------------|
| `test_path` | pytest selection relative to `tests_dir` (default: `test_base.py::test_shell test_base.py::test_ssh`) |
| `frequency_hours` | How often to run (default: 24) |
| `timeout` | Seconds (default: 600) |
| `release` | Golden image release unless the target pins one (default: current stable) |
| `firmware`, `sha256` | Explicit golden image instead of the target file's |
| `enabled` | `false` skips the device |
| `target` | labgrid target file (default: `<device>.yaml`) |

Set `health_state_file` to keep results across restarts. Alternatively,
`health_checks_dir` holds one TOML file per device with the same keys plus
`device` (default: the file name); a file overrides the `[health_checks]`
settings for its device, and with `health_checks = false` only those devices
are checked. See [`examples/health_checks/`](examples/health_checks/).

### Device Health States

| State | Description |
|-------|-------------|
| `good` | Last health check passed |
| `bad` | Last health check failed - device is offline |
| `unknown` | No health check has run yet |

When a device is `bad` (or all its places are in maintenance), the agent
skips jobs for that device (`bad`: until the next health check passes).

### Health Reports

Each device's health goes to the API as a top-level job node (no parent),
after every health check, and otherwise when it changes and at least every
12 hours (a heartbeat; the OpenWrt CI server forgets devices without a
report for 48 hours):

```json
{
  "kind": "job",
  "name": "healthcheck",
  "path": ["healthcheck"],
  "state": "done",
  "result": "pass",
  "artifacts": {"test_log": "http://storage/logs/<lab>/healthcheck/openwrt_one/20261102T080000Z/console_main"},
  "data": {
    "platform": "openwrt_one",
    "runtime": "<lab>",
    "device": "labgrid-aparcar-openwrt_one",
    "misc": {
      "healthcheck": {"firmware": "https://downloads.openwrt.org/releases/25.12.5/...",
                      "release": "25.12.5", "tests": "test_base.py::test_shell test_base.py::test_ssh",
                      "duration": 74, "passed": 2, "failed": 0, "total": 2}
    }
  }
}
```

`result` is `pass` (the check passed), `fail` (it failed, or the device is in
maintenance) or `skip` (no check possible: health checks off, no golden image,
not checked yet); `data.error_msg` says why for `fail` and `skip`.
`misc.healthcheck` is only set when a check ran. Maestro sets `owner` to the
lab's API user, so the server can tell which lab sent a report.

## Job Format

A job node in the KernelCI API (created by the scheduler):

```json
{
  "id": "6a1f...",
  "kind": "job",
  "name": "openwrt-tests",
  "path": ["checkout", "armsr-armv8-generic", "openwrt-tests"],
  "parent": "<build node id>",
  "state": "available",
  "artifacts": {"job_definition": "http://storage/openwrt/jobs/6a1f....json"},
  "data": {"runtime": "openwrt-local", "platform": "qemu_armsr-armv8", "arch": "aarch64_generic"}
}
```

The job definition it points to (PULL_LABS-shaped, plus a firmware artifact):

```json
{
  "artifacts": {"firmware": "http://storage/openwrt/releases/25.12.5/armsr/armv8/openwrt-...-initramfs-kernel.bin"},
  "integrity": {"sha256": {"firmware": "f510b0c7..."}},
  "tests": [{"id": "openwrt-tests", "type": "pytest", "parameters": "", "timeout_s": 1800}],
  "environment": {"platform": "qemu_armsr-armv8", "arch": "aarch64_generic", "requirements": ["wan_port"]}
}
```

`parameters` is the pytest selection relative to `tests_dir`, e.g.
`test_base.py::test_shell`; empty runs all tests. A leading `tests/` (from jobs
created for the openwrt-tests repository layout) is stripped.

## Container

The [`Dockerfile`](Dockerfile) builds an image with the agent (and labgrid),
QEMU and openwrt-tests' `tests/`, `targets/` and `strategies/` baked in
(`OPENWRT_TESTS_REPO`, `OPENWRT_TESTS_REF` build args). It reads
`/etc/labgrid-agent/config.toml`:

```bash
podman build -t labgrid-agent .
podman run -d --name labgrid-agent --restart unless-stopped \
    --network host --privileged -v /dev:/dev \
    -v ./my-lab.toml:/etc/labgrid-agent/config.toml:ro \
    -v labgrid-agent-data:/var/lib/labgrid-agent \
    labgrid-agent
```

`--network host` gives labgrid the exporters, coordinator and DUT networks
as on the host; `--privileged -v /dev:/dev` gives it serial adapters, USB SD
muxes and KVM, also when they are plugged in later.

The image sets the `LABGRID_*` path variables to its own layout
(`/opt/openwrt-tests/…`, state and artifacts in `/var/lib/labgrid-agent`,
health check files in `/etc/labgrid-agent/health_checks`). They override the
paths of the mounted config file, so a lab file generated for a bare host
works unchanged; mount your own test suite and set them with `-e` to use it
instead.

[`examples/docker/`](examples/docker/) is a complete Docker Compose setup
with a coordinator, a QEMU exporter and the agent.

## Systemd Service

```ini
# /etc/systemd/system/labgrid-agent.service
[Unit]
Description=KernelCI Labgrid Agent
After=network-online.target

[Service]
Type=simple
User=labgrid
# all settings in /etc/labgrid-agent/config.toml (see examples/labgrid-agent.toml)
ExecStart=/usr/local/bin/labgrid-agent
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Development

```bash
# Run tests
uv run --extra dev pytest tests/unit

# Format code
black src/ tests/
ruff check --fix src/ tests/
```

## License

LGPL-2.1-or-later
