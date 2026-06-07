# PC Workstation UI

Neuro-style **command center** in your browser — chat, live mood/drives, inner monologue stream. No Telegram required.

## Launch

```powershell
pip install -e .
ophelia ui
```

Opens **http://127.0.0.1:8765/** (configurable).

```powershell
ophelia ui --no-browser
```

## Layout

| Panel | Shows |
|-------|--------|
| **State** | Mood, valence/arousal, drives, urges, last inner thought |
| **Channel** | Chat with Ophelia (same memory as `ui:local` session) |
| **Inner monologue** | Live stream from `~/.ophelia/data/inner_monologue.md` |
| **Status bar** | Model, initiative pressure, consciousness on/paused |

Consciousness runs in the background — spontaneous messages appear in chat (highlighted). **Pause mind** stops outreach without stopping the server.

## Env

```env
OPHELIA_UI_HOST=127.0.0.1
OPHELIA_UI_PORT=8765
OPHELIA_UI_OPEN_BROWSER=true
```

Configure a provider first — see [pc-setup.md](pc-setup.md).

## vs Telegram

| | Workstation UI | Telegram |
|--|----------------|----------|
| Platform | PC browser | Phone/desktop app |
| Phone tools | off by default | N/A on PC |
| Consciousness | yes | yes |
| Voice STT/TTS | no (yet) | yes (xAI) |

Use **both**: run `ophelia ui` on PC and `ophelia run` on phone with Telegram — separate channels unless you unify later.
