# Chat channels

Ophelia is not Telegram-only. Enable **one or more** interfaces; they share the same agent, memory, and consciousness.

| Platform | Channel id in memory | Commands |
|----------|----------------------|----------|
| **Telegram** | `telegram:{user_id}` | `/pause`, `/game`, … |
| **Discord** | `discord:{user_id}` | `!pause`, `!game`, … |
| **PC UI** | `ui:local` | browser workstation |
| **CLI** | `cli` | `ophelia chat` |

## Telegram

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=123456789
OPHELIA_TELEGRAM_ENABLED=true
```

Get token: [@BotFather](https://t.me/BotFather) → `/newbot`  
Your id: [@userinfobot](https://t.me/userinfobot)

## Discord

```env
DISCORD_BOT_TOKEN=...
DISCORD_ALLOWED_USER_IDS=987654321012345678
OPHELIA_DISCORD_ENABLED=true
DISCORD_GUILD_ID=123456789012345678
OPHELIA_DISCORD_LOG=true
```

Setup:

1. [Discord Developer Portal](https://discord.com/developers/applications) → New Application → Bot → **Reset Token**
2. Enable **Message Content Intent** (Bot → Privileged Gateway Intents)
3. OAuth2 → URL Generator → scopes: `bot` → permissions: **Send Messages**, **Read Message History**, **Manage Channels**, **Attach Files**
4. Invite bot to your server or DM it
5. Your user id: Settings → Advanced → Developer Mode → right-click your profile → **Copy User ID**
6. For logging: create (or use) a dedicated server → right-click the server icon → **Copy Server ID** → set `DISCORD_GUILD_ID`

On first connect, Ophelia creates four categories in that server:

| Category | Purpose |
|----------|---------|
| **Main** | `activity`, `consciousness`, `inner-thoughts`, `system` |
| **Telegram** | One channel per Telegram chat (text, photos, voice, etc.) |
| **DMs** | One channel per Discord DM partner |
| **Discord Servers** | One channel per Discord server she is in |

New people and servers get a channel automatically the first time she talks there or joins. Channel IDs are saved in `~/.ophelia/data/discord_log_channels.json` so restarts do not duplicate channels.

Commands use `!` prefix: `!start`, `!pause`, `!resume`, `!game`, `!inner`, `!listen`, `!voice`

Plain messages (no `!`) are chat with Ophelia. Works in DMs and any channel the bot can read.

## Both at once

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=111
DISCORD_BOT_TOKEN=...
DISCORD_ALLOWED_USER_IDS=222
```

`ophelia run` starts **both** bots. Consciousness outreach is sent to **all** allowed users on **all** enabled platforms.

## Primary channel (consciousness memory)

Spontaneous messages are stored in the channel from `OPHELIA_PRIMARY_CHANNEL`, or the first configured platform:

```env
OPHELIA_PRIMARY_CHANNEL=discord:987654321012345678
```

If unset: Telegram user id first, else Discord.

## Disable a platform

```env
OPHELIA_TELEGRAM_ENABLED=false
OPHELIA_DISCORD_ENABLED=false
```

Use `ophelia ui` or `ophelia chat` with no bots configured.

## Verify

```bash
ophelia check
```

Look for **Chat channels**, **Telegram bot**, **Discord bot** under `[SERVICES]`.
