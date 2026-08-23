# -----------------
# Builder Stage
# -----------------
FROM python:3.13-alpine AS builder

# Install build dependencies (psutil/pyfuse3 compile from source on musl).
RUN apk add --no-cache gcc musl-dev libffi-dev python3-dev build-base curl curl-dev openssl-dev fuse3-dev pkgconf fuse3

# Install uv (fast package manager)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# Install dependencies with uv (no dev in builder).
# NOTE: no --mount=type=cache here — a shared cache mount is racy when buildx
# builds amd64/arm64/armv7 in parallel (uv's cache lock collides).
COPY pyproject.toml uv.lock* ./
RUN uv venv .venv && uv sync --no-dev --frozen

# -----------------
# Final Stage
# -----------------
FROM python:3.13-alpine
LABEL name="Riven TPDB" \
      description="Adult-only Riven fork backed by ThePornDB (TPDB)" \
      url="https://github.com/riven-tpdb/riven-tpdb"

# Install only runtime dependencies
RUN apk add --no-cache curl libcurl shadow unzip ffmpeg libpq fuse3 libcap libcap-utils postgresql17-client

# Configure FUSE
RUN sed -i 's/^#\s*user_allow_other/user_allow_other/' /etc/fuse.conf || \
    echo 'user_allow_other' >> /etc/fuse.conf

WORKDIR /riven

# Copy the virtual environment from the builder
COPY --from=builder /app/.venv /riven/.venv

# Grant the necessary capabilities to the Python binary
RUN setcap cap_sys_admin+ep /usr/local/bin/python3.13

# Activate the virtual environment by adding it to the PATH
ENV PATH="/riven/.venv/bin:$PATH"

# Copy application code and entrypoint
COPY src/ ./src
COPY pyproject.toml uv.lock* ./
COPY entrypoint.sh ./

RUN chmod +x ./entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
