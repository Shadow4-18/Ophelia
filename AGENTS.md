# AGENTS.md

Guidance for cloud agents working in this repository.

## Cursor Cloud specific instructions

### Product overview

Single Python package (`ophelia`) — autonomous AI with CLI, browser workstation (`ophelia ui`), and optional Telegram/Discord bots (`ophelia run`). Config and data live under `~/.ophelia/`.

### Install (first time)

See [docs/INSTALL.md](docs/INSTALL.md) and [scripts/install.sh](scripts/install.sh). Minimal path:

```bash
pip install -e .
export PATH="$HOME/.local/bin:$PATH"
ophelia setup --do
```

Add `~/.ophelia/SOUL.md` (persona) if missing — optional for `ophelia check` but needed for meaningful chat.

### Ollama (local LLM)

Default provider is Ollama. On this cloud VM:

1. **System dep:** `zstd` is required before installing Ollama (`apt-get install zstd`).
2. **Version:** Ollama **0.30.x segfaults** during model warmup in this environment. Use **0.5.13**:
   ```bash
   curl -fsSL https://ollama.com/install.sh | OLLAMA_VERSION=0.5.13 sh
   ```
3. **No systemd:** start the daemon manually in tmux:
   ```bash
   tmux -f /exec-daemon/tmux.portal.conf new-session -d -s ollama-serve -- ollama serve
   ```
4. Pull the configured chat model: `ollama pull llama3.2:3b` (or set `OLLAMA_MODEL` in `~/.ophelia/.env`).

### Running services

| Service | Command | URL / notes |
|---------|---------|-------------|
| Ollama | `ollama serve` (tmux) | http://127.0.0.1:11434 |
| Workstation UI | `OPHELIA_UI_OPEN_BROWSER=false ophelia ui --no-browser` (tmux) | http://127.0.0.1:8765/ |
| One-shot chat | `ophelia chat "hello"` | CLI smoke test |
| Full daemon | `ophelia run` | Needs Telegram/Discord tokens |

Ensure `PATH` includes `$HOME/.local/bin` so the `ophelia` entrypoint is found.

### Verify

```bash
ophelia check --chat-only   # PC mode, no Telegram required
ophelia chat "hello"
```

### Lint / tests

No linter or pytest suite is configured in `pyproject.toml`. Validation is via `ophelia check` / `ophelia doctor`.

### Optional secrets

Telegram (`TELEGRAM_BOT_TOKEN`), Discord (`DISCORD_BOT_TOKEN`), and xAI/OpenAI keys in `~/.ophelia/.env` — only needed for those integrations, not for `ophelia ui` / `ophelia chat` with Ollama.
