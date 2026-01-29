# KernelCI Labgrid Scheduler

Labgrid runtime scheduler for KernelCI's Maestro pipeline. This project enables integration between [KernelCI](https://kernelci.org/) and [Labgrid](https://labgrid.readthedocs.io/) test labs for automated Linux kernel testing.

## Overview

This scheduler provides two operation modes:

- **Push Mode**: For Labgrid coordinators accessible from KernelCI infrastructure
- **Pull Mode**: For labs behind firewalls that poll for pending jobs

## Architecture

```
                          KernelCI Infrastructure
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│  ┌─────────────┐    ┌──────────────┐    ┌───────────────┐  │
│  │ KernelCI    │    │ Maestro      │    │ labgrid-      │  │
│  │ API         │◄──►│ Scheduler    │◄──►│ scheduler     │  │
│  │ (Pub/Sub)   │    │ Service      │    │ (this)        │  │
│  └─────────────┘    └──────────────┘    └───────┬───────┘  │
│                                                  │          │
└──────────────────────────────────────────────────┼──────────┘
                                                   │
                    ┌──────────────────────────────┼──────────┐
                    │          gRPC                │          │
                    ▼                              ▼          │
              ┌───────────┐                  ┌──────────┐     │
              │ Labgrid   │                  │ Result   │     │
              │Coordinator│                  │ Reporter │     │
              └─────┬─────┘                  └──────────┘     │
                    │                                         │
              ┌─────┴─────┐                                   │
              │           │            Labgrid Lab            │
         ┌────┴───┐  ┌────┴───┐                              │
         │Exporter│  │Exporter│                              │
         │(Board1)│  │(Board2)│                              │
         └────────┘  └────────┘                              │
└─────────────────────────────────────────────────────────────┘
```

## Requirements

- Python 3.10+
- Labgrid 25.0+ (gRPC-based coordinator)
- Access to KernelCI API
- Network access to Labgrid coordinator (push mode) or KernelCI API (pull mode)

## Installation

```bash
# From PyPI (when published)
pip install kernelci-labgrid

# From source
git clone https://github.com/kernelci/kernelci-labgrid.git
cd kernelci-labgrid
pip install -e ".[dev]"
```

## Configuration

### Runtime Configuration

Create a runtime configuration file (see `config/labgrid-runtime.yaml`):

```yaml
runtimes:
  labgrid-mylab:
    lab_type: labgrid
    coordinator_address: "labgrid.example.com:20408"
    mode: push
    place_mapping:
      - place: rpi4-slot1
        platform: bcm2711-rpi-4-b
        compatible:
          - "brcm,bcm2711"
```

### Scheduler Configuration

Define which jobs trigger Labgrid tests (see `config/labgrid-scheduler.yaml`):

```yaml
scheduler:
  - job: baseline-arm64
    event:
      channel: node
      name: kbuild-gcc-12-arm64
      result: pass
    runtime:
      type: labgrid
      name: labgrid-mylab
    platforms:
      - bcm2711-rpi-4-b
```

## Usage

### Push Mode Scheduler

For labs with accessible Labgrid coordinators:

```bash
export KCI_API_URL=https://api.kernelci.org
export KCI_API_TOKEN=your-token
export LABGRID_COORDINATOR=labgrid.example.com:20408

labgrid-scheduler --config config/labgrid-runtime.yaml
```

### Pull Mode Agent

For labs behind firewalls:

```bash
export KCI_API_URL=https://api.kernelci.org
export KCI_API_TOKEN=your-token
export LABGRID_COORDINATOR=localhost:20408

labgrid-pull-agent --lab-name mylab --poll-interval 30
```

### Docker Deployment

```bash
cd docker
docker-compose up -d
```

## Development

### Setup

```bash
# Clone the repository
git clone https://github.com/kernelci/kernelci-labgrid.git
cd kernelci-labgrid

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install with development dependencies
pip install -e ".[dev]"

# Generate gRPC stubs (if proto files change)
python -m grpc_tools.protoc \
    -I./proto \
    --python_out=./src/kernelci_labgrid/generated \
    --grpc_python_out=./src/kernelci_labgrid/generated \
    ./proto/labgrid_coordinator.proto
```

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=kernelci_labgrid --cov-report=html

# Run specific test file
pytest tests/unit/test_runtime.py
```

### Code Quality

```bash
# Format code
black src/ tests/
ruff check --fix src/ tests/

# Type checking
mypy src/
```

## Integration with kernelci-pipeline

To integrate with an existing KernelCI pipeline deployment, add the Labgrid runtime to your `pipeline.yaml`:

```yaml
runtimes:
  labgrid-mylab:
    lab_type: labgrid
    coordinator_address: "coordinator.example.com:20408"
    mode: push
    place_mapping:
      - place: board-1
        platform: bcm2711-rpi-4-b
```

## License

LGPL-2.1-or-later

## Contributing

Contributions are welcome! Please read the [KernelCI contribution guidelines](https://kernelci.org/docs/contribute/) before submitting pull requests.

## Resources

- [KernelCI Documentation](https://docs.kernelci.org/)
- [Labgrid Documentation](https://labgrid.readthedocs.io/)
- [KernelCI API](https://github.com/kernelci/kernelci-api)
- [KernelCI Pipeline](https://github.com/kernelci/kernelci-pipeline)
