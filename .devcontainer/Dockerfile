FROM debian:13.4

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential nodejs npm \
        python3 python3-pip python3-dev python3-venv python3-setuptools \
        ripgrep ffmpeg gcc libffi-dev curl git && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Pre-install setuptools+wheel so editable installs work
RUN uv pip install --system --break-system-packages setuptools wheel
