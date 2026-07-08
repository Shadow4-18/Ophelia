# AGENTS.md

## Cursor Cloud specific instructions

### Product overview

Single Python app (**Ophelia**): autonomous local-first AI agent with CLI, optional Telegram/Discord bots, and a FastAPI workstation UI (`ophelia ui`).

### Services (typical PC dev)

| Service | Required? | How to start |
|---------|-----------|--------------|
| **Ophelia** (`ophelia ui` / `ophelia chat`) | Yes | See commands below |
| **Ollama** (default LLM) | Yes for default `OPHELIA_PROVIDER=ollama` | `ollama serve` in tmux (see Ollama section) |
| Telegram / Discord | Optional | `ophelia run` + tokens in `~/.ophelia/.env` |
| Phone body (ADB/Shizuku) | Optional | Off by default on PC |

### PATH

`pip install -e .` installs the `ophelia` CLI to `~/.local/bin`. Ensure it is on `PATH` (already appended in `~/.bashrc` on this VM).

### First-time / manual setup (not in update script)

1. **Config:** `ophelia setup --do` creates `~/.ophelia/.env`, `goals.yaml`, etc. Add `~/.ophelia/SOUL.md` for persona (required for meaningful chat).
2. **Ollama:** Not bundled in the repo. On this cloud VM, **Ollama v0.30.x segfaults** during inference; use **v0.24.0** extracted to `~/.local`:

```bash
# One-time (if not already installed)
mkdir -p /tmp/ollama-install && cd /tmp/ollama-install
curl -fsSL -o ollama-linux-amd64.tar.zst \
  "https://github.com/ollama/ollama/releases/download/v0.24.0/ollama-linux-amd64.tar.zst"
zstd -d -f ollama-linux-amd64.tar.zst -o ollama.tar
tar -xf ollama.tar -C "$HOME/.local"
export PATH="$HOME/.local/bin:$PATH"
export LD_LIBRARY_PATH="$HOME/.local/lib/ollama:${LD_LIBRARY_PATH:-}"

# Start daemon (tmux recommended)
tmux -f /exec-daemon/tmux.portal.conf new-session -d -s ollama-serve \
  'export PATH="$HOME/.local/bin:$PATH"; export LD_LIBRARY_PATH="$HOME/.local/lib/ollama:${LD_LIBRARY_PATH:-}"; ollama serve'

# Pull a tool-capable model (llama3.2:1b works on 15GB RAM)
ollama pull llama3.2:1b
```

Set in `~/.ophelia/.env`: `OLLAMA_MODEL=llama3.2:1b`, `OPHELIA_ANDROID_ENABLED=false`, `OPHELIA_UI_OPEN_BROWSER=false`.

3. **Channels:** Leave `TELEGRAM_BOT_TOKEN` / `DISCORD_BOT_TOKEN` empty for PC dev; use `ophelia ui` or `ophelia chat`.

### Run / verify

```bash
export PATH="$HOME/.local/bin:$PATH"
export LD_LIBRARY_PATH="$HOME/.local/lib/ollama:${LD_LIBRARY_PATH:-}"

ophelia check --chat-only    # or: ophelia doctor --chat-only
ophelia chat "hello"
ophelia ui --no-browser      # http://127.0.0.1:8765
```

Ports: UI **8765**, Ollama **11434**, transfer receive **8777** (optional).

### Lint / tests

Tests live under `tests/` and run with `python -m pytest tests/ -q`. As of this
writing the suite has 200+ tests covering the agent loop, channels, memory,
mind, tools, and guest experience. No separate linter config — `python3 -m
compileall -q src/ophelia` is a quick sanity check for import/syntax errors.

### Gotchas

- `ophelia check --chat-only` can pass while `ophelia chat` fails if Ollama inference segfaults (use v0.24.0) or the model lacks tool support (use `llama3.2:1b`+, not `tinyllama`).
- Consciousness ticks yield to local-provider (Ollama) inference via the model gate; pause via UI API `POST /api/consciousness/pause` when debugging chat. Cloud providers (xai/openai) run concurrently per-role, so chat/vision/image/video can overlap with consciousness.
- Image/video providers default to `xai-oauth` and show optional FAIL in check without credentials — expected on PC dev.
