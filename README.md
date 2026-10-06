# KernelCI Labgrid Agent

A single daemon that connects your Labgrid test lab to KernelCI. Runs locally in your lab, polls KernelCI for jobs, executes your existing pytest tests, runs periodic health checks, and reports results back.

**No modifications needed to KernelCI infrastructure.**

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                   KERNELCI CLOUD (unchanged)                    │
│                                                                 │
│   ┌──────────────┐       ┌──────────────┐                      │
│   │ KernelCI API │       │   Maestro    │                      │
│   │              │       │   Pipeline   │                      │
│   └──────────────┘       └──────────────┘                      │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                              ▲
                              │ HTTPS (poll jobs, report results)
                              │
┌─────────────────────────────┼───────────────────────────────────┐
│               YOUR LAB      │                                   │
│                             │                                   │
│   ┌─────────────────────────┴─────────────────────────────┐    │
│   │                   labgrid-agent                       │    │
│   │                                                       │    │
│   │  1. Poll KernelCI API for pending jobs               │    │
│   │  2. Run periodic health checks (like LAVA)           │    │
│   │  3. Download artifacts (kernel, rootfs)              │    │
│   │  4. pytest --lg-env targets/xxx.yaml tests/          │    │
│   │  5. Report results back to API                       │    │
│   └───────────────────────────┬───────────────────────────┘    │
│                               │                                 │
│                               │ runs your tests                 │
│                               ▼                                 │
│   ┌───────────────────────────────────────────────────────┐    │
│   │              openwrt-tests (your repo)                │    │
│   │                                                       │    │
│   │  tests/test_*.py      <- pytest tests                │    │
│   │  targets/*.yaml       <- labgrid environments        │    │
│   │  conftest.py          <- fixtures                    │    │
│   └───────────────────────────┬───────────────────────────┘    │
│                               │                                 │
│                               │ pytest-labgrid                  │
│                               ▼                                 │
│                    ┌─────────────────────┐                     │
│                    │  labgrid-exporter   │                     │
│                    │  + your boards      │                     │
│                    └─────────────────────┘                     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Requirements

- Python 3.10+
- Your existing test repository (e.g., openwrt-tests) with:
  - pytest tests in `tests/`
  - labgrid target YAMLs in `targets/` (one per platform)
  - a `uv` project at its root (the agent runs `uv run pytest` there by default)
- A KernelCI (Maestro) API user in group `runtime:<lab-name>:node-editor`
- Optional: a kernelci-storage token allowed to upload below `logs/<lab-name>/`

## Installation

```bash
git clone https://github.com/aparcar/kernelci-labgrid.git
cd kernelci-labgrid
uv sync          # or: pip install -e .
```

## Usage

```bash
# Settings from a KEY=VALUE file (LAB_NAME, KCI_API_URL, LAB_API_TOKEN,
# KCI_STORAGE_URL, LAB_STORAGE_TOKEN), e.g. openwrtci/deploy/runtime/tokens.env
labgrid-agent \
    --env-file /path/to/tokens.env \
    --tests-dir /path/to/openwrt-tests/tests \
    --platform qemu_armsr-armv8

# With health checks
labgrid-agent \
    --env-file /path/to/tokens.env \
    --tests-dir /path/to/openwrt-tests/tests \
    --platform qemu_armsr-armv8 \
    --health-checks-dir examples/health_checks \
    --health-state-file /var/lib/labgrid/health_state.json
```

### Options

| Option | Description |
|--------|-------------|
| `--lab-name`, `-l` | Lab name, matched against the job's `data.runtime` (or `LAB_NAME`) |
| `--tests-dir`, `-t` | Path to your pytest tests directory; its parent is the repo pytest runs in |
| `--targets-dir` | Path to labgrid target YAMLs (default: tests/../targets) |
| `--platform` | Platform this lab serves (`targets/<name>.yaml`), repeatable; default: all targets |
| `--api-url` | API URL including version, e.g. `http://localhost:8001/latest` (or `KCI_API_URL`) |
| `--api-token` | Lab API token (or `LAB_API_TOKEN` / `KCI_API_TOKEN`) |
| `--storage-url`, `--storage-token` | kernelci-storage for log uploads (or `KCI_STORAGE_URL` / `LAB_STORAGE_TOKEN`) |
| `--env-file` | Read the settings above from a KEY=VALUE file |
| `--pytest-command` | How to run pytest in the tests repo (default: `uv run pytest`) |
| `--poll-interval`, `-p` | Seconds between polls (default: 30) |
| `--once` | Poll once, wait for started jobs, exit |
| `--artifact-dir`, `-a` | Where to download firmware and keep job outputs |
| `--health-checks-dir`, `-c` | Directory with health check YAML configs (enables health checks) |
| `--health-state-file`, `-s` | JSON file to persist device health state |
| `--debug`, `-d` | Enable debug logging |

## How It Works

1. **Checks health** - runs scheduled health checks if due (results stay local)
2. **Polls** `GET /nodes?kind=job&state=available&data.runtime=<lab>&data.platform=<p>`
   for each healthy platform, one job per platform at a time
3. **Claims the job** by writing `data.job_id=<lab>:<uuid>` (best effort, like
   kernelci/pullab_cloud, until kernelci-api has an atomic claim)
4. **Fetches the job definition** from `artifacts.job_definition`, downloads the
   firmware and verifies its sha256
5. **Runs pytest** in the tests repo:
   ```bash
   uv run pytest <tests> --lg-env targets/<platform>.yaml --firmware <image> \
       --lg-log=<out> --junit-xml=<out>/results.xml
   ```
6. **Uploads** console log, pytest log and JUnit XML to kernelci-storage
7. **Reports back** with `PUT /nodes/<job>`: the job node plus one child per test
   module and one leaf per test (paths `…/openwrt-tests/<module>/<test>`), and
   creates a sibling `boot` test node under the build (result from `test_shell`)

Failures where tests never ran (download, crash, timeout) mark the job
`incomplete` with `error_code: Infrastructure`. Firmware that never reaches a
shell (pytest exit code 3) is a real `fail`, not an infrastructure error.

## Health Checks

Similar to LAVA, the agent can run periodic health checks with a known-good
image. Results are kept local (state file) and only gate job execution: jobs
for a `bad` device are left `available` for other labs.

### Health Check Configuration

Create YAML files in your health checks directory:

```yaml
# /etc/labgrid/health_checks/qemu_armsr-armv8.yaml
device: qemu_armsr-armv8
target: qemu_armsr-armv8.yaml
frequency_hours: 24

golden_image:
  firmware: https://downloads.openwrt.org/releases/25.12.5/targets/armsr/armv8/openwrt-25.12.5-armsr-armv8-generic-initramfs-kernel.bin
  sha256: f510b0c73c1ee70a64df384d7e2ad4404caf83e6bc7cce9ac13426f77b9ae3be

test_path: tests/test_base.py::test_shell tests/test_base.py::test_ssh
timeout: 600
```

### Device Health States

| State | Description |
|-------|-------------|
| `good` | Last health check passed |
| `bad` | Last health check failed - device is offline |
| `unknown` | No health check has run yet |

When a device is in `bad` state, the agent skips jobs for that device until
the next health check passes.

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
  "tests": [{"id": "openwrt-tests", "type": "pytest", "parameters": "tests/", "timeout_s": 1800}],
  "environment": {"platform": "qemu_armsr-armv8", "arch": "aarch64_generic", "requirements": ["wan_port"]}
}
```

`parameters` is the pytest selection, relative to the tests repo.

## Systemd Service

```ini
# /etc/systemd/system/labgrid-agent.service
[Unit]
Description=KernelCI Labgrid Agent
After=network-online.target

[Service]
Type=simple
User=labgrid
ExecStart=/usr/local/bin/labgrid-agent \
    --env-file /etc/labgrid/tokens.env \
    --tests-dir /opt/openwrt-tests/tests \
    --health-checks-dir /etc/labgrid/health_checks \
    --health-state-file /var/lib/labgrid/health_state.json
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
