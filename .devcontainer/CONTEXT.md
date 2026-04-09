# Devcontainer Context — Hermes Agent

Give this to Copilot when starting a new chat inside the devcontainer.

---

## What we're working on

We're fixing a timeout loop when using **LM Studio** (or Ollama) as a local LLM provider.
The problem: LM Studio takes minutes for prompt processing (prefilling) on large contexts.
During prefill, no SSE data is sent. The httpx `read` timeout (60s) fires, hermes retries,
LM Studio restarts from 0% — infinite loop.

## Changes already made (in this workspace)

### 1. `agent/model_metadata.py` — line 166
Added `"host.docker.internal"` to `_LOCAL_HOSTS` tuple so Docker containers recognize the
host LLM server as a local endpoint.

### 2. `run_agent.py` — `_call_chat_completions()` (~line 4376)
For local endpoints, the httpx streaming `read` timeout is raised from 60s to the base
timeout (1800s) unless the user explicitly set `HERMES_STREAM_READ_TIMEOUT`. This prevents
ReadTimeout during long prompt processing.

### 3. `run_agent.py` — streaming poll loop (~line 4795)
Added periodic status messages (every 30s, after initial 15s wait) for local providers
during prompt processing so the user sees "Waiting for local model prompt processing (Xs)…"
instead of silence.

## What still needs to happen

- **Verify imports**: `python -c "from run_agent import AIAgent; print('OK')"`
- **Run relevant tests**: `python -m pytest tests/test_model_tools.py tests/tools/ -q`
- **Full test suite** (optional): `python -m pytest tests/ -q`

## Environment info

- Workspace: `/workspace` (bind-mounted from host)
- Config: `/opt/data` (bind-mounted from `~/.hermes`)
- Config has: `model.base_url: http://host.docker.internal:1234/v1` (LM Studio)
- Model: `google/gemma-4-31b` via `custom` provider
- `host.docker.internal` resolves to the Docker host (macOS)
- Python packages installed editably via `uv pip install -e '.[all]'`
- Tests: `python -m pytest tests/ -q` (~3000 tests)

## Key files

- `run_agent.py` — core agent loop, streaming, timeouts
- `agent/model_metadata.py` — `is_local_endpoint()`, `_LOCAL_HOSTS`
- `hermes_cli/config.py` — `DEFAULT_CONFIG`, env vars
- `docker-compose.yaml` — production container (separate from devcontainer)
- `AGENTS.md` — full dev guide for coding assistants
