# Docker Compose Example - OpenWrt QEMU x86_64

This example sets up a complete KernelCI Labgrid testing environment for OpenWrt on QEMU x86_64.

## Components

```
┌─────────────────────────────────────────────────────────────────┐
│                     Docker Compose Stack                        │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              labgrid-coordinator:20408                  │   │
│  │                                                         │   │
│  │  Manages device resources and locks                     │   │
│  └────────────────────────┬────────────────────────────────┘   │
│                           │                                     │
│  ┌────────────────────────┼────────────────────────────────┐   │
│  │                        │                                │   │
│  │   labgrid-exporter     │      labgrid-agent            │   │
│  │                        │                                │   │
│  │  Exports QEMU devices: │   Polls KernelCI API:         │   │
│  │  - openwrt-main-x86    │   - Claims pending jobs       │   │
│  │  - openwrt-2512-x86    │   - Downloads artifacts       │   │
│  │                        │   - Runs pytest tests         │   │
│  │                        │   - Reports results           │   │
│  └────────────────────────┴────────────────────────────────┘   │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              job-scheduler (optional)                   │   │
│  │                                                         │   │
│  │  Submits test jobs to KernelCI every 24 hours:         │   │
│  │  - Fetches latest OpenWrt x86_64 images                │   │
│  │  - Creates test jobs for main and 25.12 branches       │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              build-monitor (optional)                   │   │
│  │                                                         │   │
│  │  Maestro pipeline integration:                         │   │
│  │  - Monitors version.buildinfo for new builds           │   │
│  │  - Creates checkout nodes in KernelCI                  │   │
│  │  - Triggers test jobs automatically                    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Configure Environment

```bash
cd examples/docker

# Copy and edit environment file
cp .env.example .env
vim .env  # Add your KCI_API_TOKEN
```

### 2. Start Services

```bash
# Build and start everything
docker compose up -d

# View logs
docker compose logs -f labgrid-agent

# Check status
docker compose ps
```

### 3. Verify Setup

```bash
# Check coordinator is running
docker compose exec labgrid-coordinator labgrid-client places

# Check exporter registered devices
docker compose exec labgrid-exporter labgrid-client -p openwrt-main-x86 show

# Check agent is polling
docker compose logs labgrid-agent | tail -20
```

## Scheduling Test Jobs

There are multiple ways to schedule periodic test jobs:

### Option 1: Built-in Scheduler Service

Start the scheduler service to automatically submit jobs every 24 hours:

```bash
# Start with scheduler enabled
docker compose --profile scheduler up -d

# View scheduler logs
docker compose logs -f job-scheduler

# Stop scheduler
docker compose --profile scheduler down
```

### Option 2: Manual Trigger (for host cron)

Submit jobs once using the trigger service:

```bash
# Submit jobs for latest OpenWrt images
docker compose --profile trigger run --rm job-trigger
```

Add to host crontab for scheduled execution:

```bash
# Edit crontab
crontab -e

# Add daily job at 2 AM
0 2 * * * cd /path/to/examples/docker && docker compose --profile trigger run --rm job-trigger
```

### Option 3: Build Monitor (Maestro Integration)

The build monitor watches OpenWrt downloads for new builds by checking the
`version.buildinfo` file. When a new build is detected, it creates a checkout
node in KernelCI, which triggers the test pipeline automatically.

```bash
# Start with build monitor enabled
docker compose --profile pipeline up -d

# View build monitor logs
docker compose logs -f build-monitor

# Check once and exit
docker compose --profile pipeline run --rm build-monitor python /opt/scripts/build_monitor.py --once
```

How it works:
1. Polls `https://downloads.openwrt.org/.../version.buildinfo` every hour
2. Detects version changes (e.g., `r24106-10cc5fcd00` → `r24107-abc1234def`)
3. Creates a checkout node in KernelCI with the new build info
4. Creates a test job node linked to the checkout
5. The labgrid-agent picks up the job and runs tests

This integrates with KernelCI's [Maestro pipeline](https://docs.kernelci.org/maestro/) architecture.

## Testing Locally

You can run tests locally without KernelCI:

```bash
# Enter the agent container
docker compose exec labgrid-agent bash

# Run health check tests manually
cd /opt/tests
pytest tests/health/test_boot.py --lg-env targets/openwrt-main-x86.yaml -v

# Run full test suite
pytest tests/ --lg-env targets/openwrt-main-x86.yaml -v
```

## Devices

### openwrt-main-x86

- **Branch:** OpenWrt main (snapshots)
- **Platform:** x86_64 QEMU
- **SSH Port:** 2222
- **Images:** https://downloads.openwrt.org/snapshots/targets/x86/64/

### openwrt-2512-x86

- **Branch:** OpenWrt 25.12
- **Platform:** x86_64 QEMU
- **SSH Port:** 2223
- **Images:** https://downloads.openwrt.org/releases/25.12-SNAPSHOT/targets/x86/64/

## Directory Structure

```
docker/
├── docker-compose.yml      # Main compose file
├── Dockerfile             # Agent container image
├── .env.example           # Environment template
├── exporter.yaml          # Labgrid exporter config
├── targets/               # Labgrid target definitions
│   ├── openwrt-main-x86.yaml
│   └── openwrt-2512-x86.yaml
├── health_checks/         # Health check configurations
│   ├── openwrt-main-x86.yaml
│   └── openwrt-2512-x86.yaml
├── scripts/               # Utility scripts
│   ├── job_scheduler.py   # Periodic job submission
│   └── build_monitor.py   # Maestro pipeline integration
└── tests/                 # Pytest tests
    ├── conftest.py
    ├── test_openwrt.py
    └── health/
        └── test_boot.py
```

## KernelCI Job Format

Jobs sent to this lab should have:

```json
{
  "name": "openwrt-boot-test",
  "state": "pending",
  "data": {
    "lab": "openwrt-qemu-lab",
    "platform": "openwrt-main-x86",
    "artifacts": {
      "rootfs": "https://downloads.openwrt.org/.../combined.img.gz"
    },
    "test_path": "test_openwrt.py",
    "timeout": 600
  }
}
```

## Troubleshooting

### KVM not available

If QEMU fails with KVM errors, ensure your host has KVM enabled:

```bash
# Check KVM is available
ls -l /dev/kvm

# If not available, load module
sudo modprobe kvm_intel  # or kvm_amd
```

### Coordinator connection failed

```bash
# Check coordinator is healthy
docker compose ps labgrid-coordinator

# Check network
docker compose exec labgrid-agent ping labgrid-coordinator
```

### Tests failing to boot

```bash
# Check QEMU logs
docker compose logs labgrid-exporter

# Try running QEMU manually
docker compose exec labgrid-exporter qemu-system-x86_64 --version
```

## Customization

### Adding More Devices

1. Add device to `exporter.yaml`
2. Create target in `targets/`
3. Optionally add health check in `health_checks/`

### Using Real Hardware

Replace the QEMU configuration with serial port access:

```yaml
# exporter.yaml
my-real-board:
  RawSerialPort:
    port: /dev/ttyUSB0
    speed: 115200
```

## License

LGPL-2.1-or-later
