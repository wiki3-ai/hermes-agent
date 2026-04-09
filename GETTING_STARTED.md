# Getting Started — Hermes Agent on Mac Studio (Local LLMs)

This guide walks through running Hermes Agent in Docker on your Mac Studio, using **Ollama** and **LM Studio** as local LLM providers. The primary workflow is VS Code, not the terminal CLI.

## Prerequisites

- **Docker Desktop for Mac** — [download](https://www.docker.com/products/docker-desktop/)
- **Ollama** running on the host (default: `http://localhost:11434`)
- **LM Studio** running on the host (default: `http://localhost:1234`)
- At least one model loaded in Ollama or LM Studio

## 1. Prepare the data directory

```sh
mkdir -p ~/.hermes
```

## 2. Build and run setup

From the repo root (`hermes-agent/`):

```sh
docker compose run --rm hermes-cli setup
```

This runs the interactive setup wizard. When prompted:

- **Provider**: Choose `custom` (or `ollama` / `lmstudio` — they all map to the custom OpenAI-compatible provider)
- **Base URL**: Use `http://host.docker.internal:11434/v1` for Ollama or `http://host.docker.internal:1234/v1` for LM Studio
- **API key**: Leave blank — local servers don't need one
- **Model**: Enter the model name loaded in your server (e.g. `qwen3:32b`, `llama3:70b`)

> **Why `host.docker.internal`?** Inside a Docker container on macOS, `localhost` refers to the container itself. `host.docker.internal` is Docker Desktop's DNS name that resolves to the host machine where Ollama and LM Studio are listening.

## 3. Manual config (alternative to setup wizard)

If you prefer to configure directly, create/edit `~/.hermes/config.yaml`:

```yaml
model:
  default: "gemma4:31b"          # Your loaded model name
  provider: "ollama"             # or "lmstudio" or "custom"
  base_url: "http://host.docker.internal:11434/v1"  # Ollama

# To use LM Studio instead:
# model:
#   default: "your-model-name"
#   provider: "lmstudio"
#   base_url: "http://host.docker.internal:1234/v1"

# Optional: configure a second provider for auxiliary tasks (vision, summarization)
# auxiliary:
#   vision:
#     base_url: "http://host.docker.internal:1234/v1"
#     model: "your-vision-model"
#   compression:
#     base_url: "http://host.docker.internal:11434/v1"
#     model: "gemma4:31b"
```

Create `~/.hermes/.env` (can be empty for local-only use):

```sh
touch ~/.hermes/.env
```

## 4. Start the gateway

```sh
docker compose up -d
```

View logs:

```sh
docker compose logs -f hermes
```

## 5. Interactive CLI session

When you need a one-off chat session:

```sh
docker compose run --rm hermes-cli
```

## 6. Working from VS Code

Since the primary workflow is VS Code rather than the terminal CLI:

- Use the **VS Code integrated terminal** to run `docker compose` commands
- Use the **Docker extension** (`ms-azuretools.vscode-docker`) to monitor containers, view logs, and restart services
- Edit `~/.hermes/config.yaml` and `~/.hermes/.env` directly in VS Code — changes take effect on next container restart
- If you have the **Hermes ACP adapter** configured, VS Code can communicate with the agent directly

### Useful VS Code terminal commands

```sh
docker compose up -d              # Start gateway in background
docker compose down               # Stop everything
docker compose restart hermes     # Restart after config changes
docker compose logs -f hermes     # Tail logs
docker compose run --rm hermes-cli  # Interactive session
```

## 7. Switching between Ollama and LM Studio

Both can run simultaneously on the host. To switch which one Hermes uses, edit `~/.hermes/config.yaml`:

| Provider   | Base URL (from container)                         | Default Port |
|------------|---------------------------------------------------|-------------|
| Ollama     | `http://host.docker.internal:11434/v1`           | 11434       |
| LM Studio  | `http://host.docker.internal:1234/v1`            | 1234        |

Then restart: `docker compose restart hermes`

## 8. Context length

Local models often default to short context windows. For best results:

- **Ollama**: Start with `OLLAMA_CONTEXT_LENGTH=32768 ollama serve` or set `num_ctx` in your Modelfile
- **LM Studio**: Set context length in the model settings panel and reload the model

## 9. Named custom providers

To save both Ollama and LM Studio as named providers you can switch between, add to `~/.hermes/config.yaml`:

```yaml
custom_providers:
  - name: "Ollama Local"
    base_url: "http://host.docker.internal:11434/v1"
    model: "qwen3:32b"
    models:
      qwen3:32b:
        context_length: 32768

  - name: "LM Studio Local"
    base_url: "http://host.docker.internal:1234/v1"
    model: "your-model-name"
```

Then use `/model custom:ollama-local` or `/model custom:lm-studio-local` in a Hermes chat session to switch on the fly.

## Troubleshooting

### "Connection refused" to Ollama/LM Studio

- Verify Ollama is running: `curl http://localhost:11434/v1/models` from the host
- Verify LM Studio server is started (it must be manually enabled in LM Studio's UI)
- Ensure you're using `host.docker.internal` (not `localhost`) in the container config

### Container can't resolve host.docker.internal

This requires **Docker Desktop for Mac**. If you're using a different Docker runtime (e.g. colima, lima), you may need to add `--network host` instead, or manually set the host IP.

### Model not found

Make sure the model is loaded/pulled in your local server:
```sh
ollama pull qwen3:32b       # For Ollama
```
For LM Studio, download and load the model from the UI.

### Slow responses

Local inference speed depends on your Mac Studio's hardware. Monitor with `docker compose logs -f hermes` and check Ollama/LM Studio resource usage in Activity Monitor.
