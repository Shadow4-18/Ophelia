# Tier 2 setup

## 1. Inner monologue stream

Every consciousness thought appends to:

`~/.ophelia/data/inner_monologue.md`

Watch live on phone:

```bash
tail -f ~/.ophelia/data/inner_monologue.md
```

Telegram:

- `/inner tail` — last thoughts
- `/inner on` — mirror new thoughts as `💭 ...` messages
- `/inner off` — file only

Env:

```env
OPHELIA_INNER_LOG=true
OPHELIA_INNER_MIRROR_TELEGRAM=false
```

## 2. Local listen loop (speech without Telegram)

Requires **Termux:API** app + `pkg install termux-api`.

```bash
pkg install termux-api
```

Telegram: `/listen on`  
Or env: `OPHELIA_LISTEN=true`

Records `OPHELIA_LISTEN_SECONDS` (default 5s) every `OPHELIA_LISTEN_INTERVAL` (45s), STT → Ophelia → TTS → `termux-media-player`.

Best with headset / quiet room. Heavy on battery and Grok quota.

## 3. Prompter rules

Copy `PROMPTER.example.md` → `~/.ophelia/PROMPTER.md`

Defines **what to do when idle** (bored → see screen, lonely → specific check-in). Separate from SOUL personality.

## 4. Memory curator

Every `OPHELIA_CURATOR_HOURS` (default 6):

- Pulls recent chats
- Searches Hermes `state.db` for context
- Appends new facts to `~/.ophelia/memories/MEMORY.md` (§ format)
- Ingests consciousness `memory_note` entries

Manual run:

```bash
ophelia curator
```

```env
OPHELIA_CURATOR=true
OPHELIA_CURATOR_HOURS=6
```

After curation, restart or wait — agent reloads MEMORY on next `ophelia run` startup (curator reloads in background loop).

## Verify

```bash
ophelia doctor
ophelia run
```

Tier 1 + 2 together = see phone, remember, think out loud, optionally speak locally.
