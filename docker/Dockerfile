# KernelCI Labgrid Scheduler Docker Image
#
# Build:
#   docker build -t kernelci-labgrid .
#
# Run scheduler:
#   docker run -e KCI_API_TOKEN=xxx kernelci-labgrid scheduler
#
# Run agent:
#   docker run -e KCI_API_TOKEN=xxx kernelci-labgrid agent --lab-name mylab

FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY config/ ./config/

# Build wheel
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

# Runtime image
FROM python:3.11-slim

LABEL maintainer="KernelCI <kernelci@lists.linux.dev>"
LABEL org.opencontainers.image.source="https://github.com/kernelci/kernelci-labgrid"
LABEL org.opencontainers.image.description="KernelCI Labgrid Scheduler"
LABEL org.opencontainers.image.licenses="LGPL-2.1-or-later"

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd --create-home --shell /bin/bash kernelci

# Copy wheels and install
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# Copy configuration files
COPY config/ /app/config/

# Set ownership
RUN chown -R kernelci:kernelci /app

# Switch to non-root user
USER kernelci

# Create directories for SSH keys and artifacts
RUN mkdir -p /home/kernelci/.ssh /home/kernelci/artifacts
RUN chmod 700 /home/kernelci/.ssh

# Environment variables
ENV KCI_API_URL="https://api.kernelci.org"
ENV LABGRID_COORDINATOR="localhost:20408"
ENV PYTHONUNBUFFERED=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD kernelci-labgrid health --coordinator ${LABGRID_COORDINATOR} || exit 1

# Default command
ENTRYPOINT ["kernelci-labgrid"]
CMD ["--help"]
