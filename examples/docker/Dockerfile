# KernelCI Labgrid Agent Docker Image
FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    qemu-system-x86 \
    qemu-utils \
    && rm -rf /var/lib/apt/lists/*

# Install labgrid and pytest
RUN pip install --no-cache-dir \
    labgrid \
    pytest \
    pytest-labgrid \
    pyyaml \
    aiohttp

# Install kernelci-labgrid
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e .

# Create directories
RUN mkdir -p /opt/tests /artifacts /var/lib/labgrid /etc/labgrid

WORKDIR /opt/tests

# Default command
CMD ["labgrid-agent", "--help"]
