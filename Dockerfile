# labgrid-agent (with labgrid and pytest) plus openwrt-tests' test suite,
# targets and strategies, and QEMU, for lab hosts.
#
#   podman build -t labgrid-agent .
#   podman run -d --name labgrid-agent --restart unless-stopped \
#       --network host --privileged -v /dev:/dev \
#       -v ./my-lab.toml:/etc/labgrid-agent/config.toml:ro \
#       -v labgrid-agent-data:/var/lib/labgrid-agent \
#       labgrid-agent
#
# --network host: labgrid exporters, coordinator and DUT networks as on the
# host. --privileged -v /dev:/dev: serial adapters, USB SD muxes, KVM, also
# when they are plugged in later. See README.md ("Container").

FROM docker.io/library/debian:trixie-slim

# openwrt-tests revision whose tests/, targets/ and strategies/ are baked in
# (branch, tag or commit). Mount your own and set LABGRID_*_DIR to override.
ARG OPENWRT_TESTS_REPO=https://github.com/aparcar/openwrt-tests.git
ARG OPENWRT_TESTS_REF=main

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gzip \
        openssh-client \
        python3 \
        qemu-system-arm \
        qemu-system-mips \
        qemu-system-x86 \
        qemu-utils \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_CACHE_DIR=/var/cache/uv

# openwrt-tests files only: the agent's own environment (labgrid fork,
# pytest, pytest-check, pytest-harvest) runs them
RUN git clone "$OPENWRT_TESTS_REPO" /opt/openwrt-tests \
    && git -C /opt/openwrt-tests checkout "$OPENWRT_TESTS_REF" \
    && rm -rf /opt/openwrt-tests/.git

# The agent with labgrid, in its own virtual environment
COPY pyproject.toml README.md /opt/kernelci-labgrid/
COPY src/ /opt/kernelci-labgrid/src/
RUN uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python --no-cache /opt/kernelci-labgrid
ENV PATH=/opt/venv/bin:$PATH

# Container layout. These override the paths of a mounted config file (a lab
# file generated for bare hosts points tests_dir elsewhere); command line
# options or -e still override them.
ENV LABGRID_TESTS_DIR=/opt/openwrt-tests/tests \
    LABGRID_TARGETS_DIR=/opt/openwrt-tests/targets \
    LABGRID_STRATEGIES_DIR=/opt/openwrt-tests/strategies \
    LABGRID_ARTIFACT_DIR=/var/lib/labgrid-agent/artifacts \
    LABGRID_HEALTH_STATE_FILE=/var/lib/labgrid-agent/health.json \
    LABGRID_HEALTH_CHECKS_DIR=/etc/labgrid-agent/health_checks

RUN mkdir -p /etc/labgrid-agent/health_checks /var/lib/labgrid-agent
VOLUME /var/lib/labgrid-agent

# Reads /etc/labgrid-agent/config.toml (mount your lab's file there)
ENTRYPOINT ["labgrid-agent"]
