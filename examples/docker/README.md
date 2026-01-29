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
