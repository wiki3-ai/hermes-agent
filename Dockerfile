FROM debian:13.4

# Install system dependencies in one layer, clear APT cache
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential nodejs npm python3 python3-pip ripgrep ffmpeg gcc python3-dev libffi-dev curl && \
    rm -rf /var/lib/apt/lists/*

# Install uv — pip's resolver chokes on the [all] dependency graph (resolution-too-deep)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY . /opt/hermes
WORKDIR /opt/hermes

# Install Python and Node dependencies in one layer, no cache
RUN uv pip install --no-cache --system -e ".[all]" && \
    npm install --prefer-offline --no-audit && \
    npx playwright install --with-deps chromium --only-shell && \
    cd /opt/hermes/scripts/whatsapp-bridge && \
    npm install --prefer-offline --no-audit && \
    npm cache clean --force

WORKDIR /opt/hermes
RUN chmod +x /opt/hermes/docker/entrypoint.sh

ENV HERMES_HOME=/opt/data
VOLUME [ "/opt/data" ]
ENTRYPOINT [ "/opt/hermes/docker/entrypoint.sh" ]
