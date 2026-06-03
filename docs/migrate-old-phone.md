# Migrate Hermes from old phone → S21 Ultra

Hermes lives only on your **old phone in Termux**. You never need Hermes running on the S21 — only a **one-time copy** of `~/.hermes`.

## Step 1 — Old phone (export)

```bash
cd ~/Ophelia   # or clone Ophelia there first
bash scripts/termux-export-hermes.sh
```

Creates: `~/storage/downloads/ophelia-hermes-bundle.tar.gz`

Includes: `SOUL.md`, `auth.json` (SuperGrok OAuth), `memories/`, `skills/`, `state.db`, `config.yaml`, `.env`, `honcho.json` if present.

### Transfer options

| Method | How |
|--------|-----|
| USB | Copy `Download/ophelia-hermes-bundle.tar.gz` to PC or S21 |
| Nearby / Drive | `termux-open ~/storage/downloads/ophelia-hermes-bundle.tar.gz` |
| Same Wi‑Fi | `python -m http.server 8080` in downloads folder, wget on S21 |

## Step 2 — S21 Ultra (import)

```bash
# Put bundle in ~/storage/downloads/ then:
cd ~/Ophelia
bash scripts/termux-import-hermes.sh
```

This runs:

- `ophelia migrate hermes` — personality, memories, skills, state.db archive
- `ophelia auth import-hermes` — OAuth + **refresh token** for SuperGrok

## Step 3 — Configure Telegram on S21

Edit `~/.ophelia/.env`:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=your_id
OPHELIA_PROVIDER=xai-oauth
OPHELIA_CONSCIOUSNESS=true
```

Optional Honcho:

```env
HONCHO_API_KEY=...
```

## Step 4 — Run

```bash
termux-wake-lock
tmux new -s ophelia
ophelia auth refresh    # verify OAuth
ophelia run
```

## OAuth refresh

Ophelia refreshes SuperGrok tokens automatically (same as Hermes):

- Before each chat request
- Every 10 minutes in background
- Manual: `ophelia auth refresh`

If refresh fails, re-export `auth.json` from old phone or run `hermes auth add xai-oauth` on a device with a browser, then import again.

## What you get on S21

- **Continuous consciousness** — not isolated cron sessions
- **Hermes memory search** — `state.db` FTS / keyword search tool
- **Honcho** — if you used it in Hermes
- **Voice notes** — send voice in Telegram; `/voice on` for TTS replies
- **Tool use while alone** — consciousness `act` action (images, etc.)
