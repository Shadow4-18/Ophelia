# Mobile games layer

Ophelia can **see**, **tap**, **swipe**, and **comment** on Android games via Shizuku — tuned for **idle**, **puzzle**, and **menu** games, not reflex PvP.

## Setup

1. Tier 1 Shizuku working (`phone_control.sh ui-dump` OK)
2. First run copies `games.example.yaml` → `~/.ophelia/games.yaml`
3. Edit packages — find yours:

```bash
pm list packages | grep -i honkai
```

4. Enable a game (`enabled: true`) with correct `package`

## Telegram

| Command | Action |
|---------|--------|
| `/game list` | Configured games |
| `/game play <id> [minutes]` | Launch app + start session |
| `/game look` | Vision turn for active game |
| `/game stop` | End session |
| `/game status` | Session + list |

## Agent tools

| Tool | Use |
|------|-----|
| `phone_game_open` | Launch + session |
| `phone_game_look` | Game-tuned vision (counts as session turn) |
| `phone_tap` / `phone_swipe` / `phone_key` | Act on screen |
| `phone_see_screen` | Generic (non-game) |

During an active session, consciousness prefers `phone_game_look`.

## Env

```env
OPHELIA_GAMES=true
OPHELIA_GAME_SESSION_MINUTES=15
OPHELIA_GAME_MAX_TURNS=40
```

Sessions auto-stop when time or turn cap hits (limits Grok vision cost).

## Good vs bad games

| Good | Bad |
|------|-----|
| Turn-based, idle, puzzles | Rhythm, shooters, real-time PvP |
| Daily login menus | Anything needing <1s reactions |
| Single-player grind | Competitive ranked |

## Stream vibe

Ask her to narrate play-by-play in Telegram. Use `/pause` if you take the phone. Battery + API quota: keep sessions short.

## Goal

Add to `~/.ophelia/goals.yaml` (see `goals.example.yaml` `play-mobile-game`).

## Find package names

```bash
bash ~/phone_control.sh shell pm list packages -3
```

Or launch the game, then:

```bash
bash ~/phone_control.sh shell dumpsys window | grep -E 'mCurrentFocus'
```
