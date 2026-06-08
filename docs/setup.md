# Installation guide

Full walkthrough: **[docs/INSTALL.md](INSTALL.md)** (PC, Termux, Ollama, Telegram, Discord, Hermes, troubleshooting).

`ophelia setup` opens **Hermes-style menus** — arrow keys, Space, Enter, Esc — and writes `~/.ophelia/.env` for you (no nano).

## Quick start

| Platform | Command |
|----------|---------|
| **Windows** | `.\scripts\install.ps1` |
| **macOS / Linux** | `bash scripts/install.sh` |
| **Termux (S21)** | `bash scripts/termux-install.sh` |
| **Already installed** | `ophelia setup` |

## Wizard options

```bash
ophelia setup              # interactive menus (default on a real terminal)
ophelia setup --do         # create ~/.ophelia + .env, then open menus
ophelia setup --checklist  # text-only [OK]/[ ] checklist (no TUI)
ophelia setup --step 4     # show one checklist step only
ophelia setup --pc         # force PC guide on Termux
ophelia setup --phone      # force phone guide on PC
```

### Menu controls

| Key | Action |
|-----|--------|
| Up / Down | Move highlight |
| Space | Toggle (multi-select) |
| Enter | Confirm |
| Esc | Go back / keep current |

## What it checks

**PC:** package installed, `.env`, Ollama, provider, SOUL, doctor/chat, Telegram, ADB.

**Phone:** Termux deps, package, `.env`, provider, SOUL, Telegram, wake-lock, Shizuku, doctor, tmux run.

Re-run anytime after you change config — it is the canonical “what’s left?” view.

## Self-check

After install or when something breaks:

```bash
ophelia check                  # full: version, deps, config, providers, Telegram, Ollama, ADB
ophelia check --chat-only      # PC: Telegram not required
ophelia check --quick           # skip network probes (fast)
ophelia doctor -v               # show fix hints for passing checks too
```

Verifies: Python version, Ophelia package version vs install, all dependencies, `~/.ophelia`, memory DB, each provider role, Ollama daemon version, Telegram/Discord `getMe`, optional phone body probe, optional adb/MCP.

Exit code **0** = all required checks passed, **1** = fix FAIL lines and re-run.

## After setup

```bash
ophelia doctor --chat-only   # PC
ophelia doctor               # phone
ophelia run                  # Telegram + consciousness
ophelia ui                   # PC workstation
```

See [local-first.md](local-first.md), [pc-setup.md](pc-setup.md), [tier1-setup.md](tier1-setup.md).
