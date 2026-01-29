# KernelCI Labgrid Pull Agent

A simple agent that connects your Labgrid test lab to KernelCI. Runs locally in your lab, polls KernelCI for jobs, executes your existing pytest tests, and reports results back.

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
│   │              labgrid-pull-agent                       │    │
│   │                                                       │    │
│   │  1. Poll KernelCI API for pending jobs               │    │
│   │  2. Download artifacts (kernel, rootfs)              │    │
│   │  3. pytest --lg-env targets/xxx.yaml tests/          │    │
│   │  4. Report results back to API                       │    │
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
  - labgrid target YAMLs in `targets/`
- labgrid + pytest-labgrid installed
- KernelCI API token

## Installation

```bash
pip install kernelci-labgrid
```

Or from source:

```bash
git clone https://github.com/kernelci/kernelci-labgrid.git
cd kernelci-labgrid
pip install -e .
```

## Usage

```bash
# Set your API token
export KCI_API_TOKEN="your-kernelci-api-token"

# Run the agent
labgrid-pull-agent \
    --lab-name my-lab \
    --tests-dir /path/to/openwrt-tests/tests \
    --targets-dir /path/to/openwrt-tests/targets
```

### Options

| Option | Description |
|--------|-------------|
| `--lab-name`, `-l` | Your lab name (jobs are filtered by this) |
| `--tests-dir`, `-t` | Path to your pytest tests directory |
| `--targets-dir` | Path to labgrid target YAMLs (default: tests/../targets) |
| `--api-url` | KernelCI API URL (default: https://api.kernelci.org) |
| `--api-token` | API token (or set `KCI_API_TOKEN` env) |
| `--poll-interval`, `-p` | Seconds between polls (default: 30) |
| `--artifact-dir`, `-a` | Where to download artifacts |
| `--debug`, `-d` | Enable debug logging |

### Environment Variables

```bash
KCI_API_URL=https://api.kernelci.org
KCI_API_TOKEN=your-token
LG_KERNEL=/path/to/kernel      # Set by agent, available in tests
LG_ROOTFS=/path/to/rootfs      # Set by agent, available in tests
LG_DTB=/path/to/dtb            # Set by agent, available in tests
```

## How It Works

1. **Agent polls** KernelCI API for pending jobs matching your lab name
2. **Claims job** by setting state to "running"
3. **Downloads artifacts** (kernel, rootfs, etc.) to local directory
4. **Runs pytest** with your tests and labgrid environment:
   ```bash
   pytest --lg-env targets/platform.yaml tests/
   ```
5. **Parses results** from pytest output
6. **Reports back** to KernelCI API (pass/fail, test cases, duration)

## Job Format

Jobs in KernelCI API should have this structure:

```json
{
  "id": "node-id-123",
  "name": "boot-test",
  "state": "pending",
  "data": {
    "lab": "my-lab",
    "platform": "qemu-x86-64",
    "artifacts": {
      "kernel": "https://storage.kernelci.org/.../bzImage",
      "rootfs": "https://storage.kernelci.org/.../rootfs.cpio.gz"
    },
    "test_path": "test_boot.py",
    "timeout": 600
  }
}
```

## Example with openwrt-tests

```bash
# Clone your test repo
git clone https://github.com/aparcar/openwrt-tests.git
cd openwrt-tests

# Install dependencies
pip install pytest pytest-labgrid labgrid kernelci-labgrid

# Run the agent
labgrid-pull-agent \
    --lab-name openwrt-lab \
    --tests-dir ./tests \
    --targets-dir ./targets \
    --debug
```

## Systemd Service

```ini
# /etc/systemd/system/labgrid-agent.service
[Unit]
Description=KernelCI Labgrid Pull Agent
After=network-online.target

[Service]
Type=simple
User=labgrid
Environment="KCI_API_TOKEN=your-token"
ExecStart=/usr/local/bin/labgrid-pull-agent \
    --lab-name my-lab \
    --tests-dir /opt/openwrt-tests/tests
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Health Checks

Similar to LAVA, you can configure periodic health checks to validate your devices are working correctly. Health checks run a golden image at regular intervals and notify maintainers when devices fail.

### Health Check Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        YOUR LAB                                 │
│                                                                 │
│   ┌─────────────────────────────────────────────────────────┐  │
│   │              labgrid-health-check                       │  │
│   │                                                         │  │
│   │  - Runs every N hours (configurable per device)        │  │
│   │  - Downloads golden image artifacts                    │  │
│   │  - Runs health check tests via pytest                  │  │
│   │  - Tracks device health state (good/bad/unknown)       │  │
│   │  - Sends email notifications on failure/recovery       │  │
│   └────────────────────────┬────────────────────────────────┘  │
│                            │                                    │
│   ┌────────────────────────┼────────────────────────────────┐  │
│   │                        ▼                                │  │
│   │   Device: qemu-x86     Device: rpi4      Device: ...   │  │
│   │   State: GOOD          State: BAD        State: GOOD   │  │
│   │   Last: 2h ago         Last: 1h ago      Last: 30m     │  │
│   └─────────────────────────────────────────────────────────┘  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Health Check Configuration

Create YAML files in your health checks directory:

```yaml
# /etc/labgrid/health_checks/qemu-x86.yaml
device: qemu-x86
target: qemu-x86.yaml
frequency_hours: 24

golden_image:
  kernel: https://storage.example.com/golden/bzImage
  rootfs: https://storage.example.com/golden/rootfs.cpio.gz

test_path: tests/health/test_boot.py
timeout: 600

notifications:
  emails:
    - lab-admin@example.com
  on_failure: true
  on_recovery: true
```

### Running the Health Check Scheduler

```bash
labgrid-health-check \
    --health-checks-dir /etc/labgrid/health_checks \
    --targets-dir /opt/openwrt-tests/targets \
    --tests-dir /opt/openwrt-tests/tests \
    --state-file /var/lib/labgrid/health_state.json \
    --smtp-host smtp.example.com \
    --smtp-from labgrid@example.com
```

### Health Check Options

| Option | Description |
|--------|-------------|
| `--health-checks-dir`, `-c` | Directory with health check YAML configs |
| `--targets-dir`, `-t` | Directory with labgrid target YAMLs |
| `--tests-dir`, `-T` | Directory with test files |
| `--state-file`, `-s` | JSON file to persist health state |
| `--check-interval`, `-i` | Seconds between scheduler checks (default: 300) |
| `--smtp-host` | SMTP server for notifications |
| `--smtp-port` | SMTP port (default: 587) |
| `--smtp-user` | SMTP username |
| `--smtp-password` | SMTP password |
| `--smtp-from` | From address for emails |

### SMTP Environment Variables

```bash
SMTP_HOST=smtp.example.com
SMTP_USER=username
SMTP_PASSWORD=password
SMTP_FROM=labgrid@example.com
```

### Device Health States

| State | Description |
|-------|-------------|
| `good` | Last health check passed |
| `bad` | Last health check failed - device is offline |
| `unknown` | No health check has run yet |

When a device is in `bad` state, the pull agent will skip jobs for that device until an admin manually sets the health to `good` or `unknown`, or the next health check passes.

### Health Check Systemd Service

```ini
# /etc/systemd/system/labgrid-health-check.service
[Unit]
Description=Labgrid Health Check Scheduler
After=network-online.target

[Service]
Type=simple
User=labgrid
Environment="SMTP_HOST=smtp.example.com"
Environment="SMTP_FROM=labgrid@example.com"
ExecStart=/usr/local/bin/labgrid-health-check \
    --health-checks-dir /etc/labgrid/health_checks \
    --targets-dir /opt/openwrt-tests/targets \
    --tests-dir /opt/openwrt-tests/tests \
    --state-file /var/lib/labgrid/health_state.json
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Format code
black src/ tests/
ruff check --fix src/ tests/
```

## License

LGPL-2.1-or-later
