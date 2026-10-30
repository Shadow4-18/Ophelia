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
| `phone_game_state` / `phone_game_act` / `phone_game_query` | Game link, for patched games (below) |
| `phone_tap` / `phone_swipe` / `phone_key` | Act on screen |
| `phone_see_screen` | Generic (non-game) |

During an active session, consciousness prefers `phone_game_look`.

## Game link (O.M.G.I.)

For supported games she can skip the screen entirely. [O.M.G.I.](https://github.com/Shadow4-18/O.M.G.I.)
patches a mod into the game that serves its **real state** (cards, money,
score, shop, blind…) and the **exact actions** available, which she performs
through the game's own logic. No misreads, no mis-taps, and no vision cost.

| Game | Adapter |
|------|---------|
| Balatro | [LÖVE mod](https://github.com/Shadow4-18/O.M.G.I./blob/main/src/omgi/adapters/balatro/README.md) |

```bash
pip install -e '.[games]'                     # installs the omgi package
ophelia apk pull com.playstack.balatro.android
omgi patch balatro ~/.ophelia/apks/com.playstack.balatro.android/<version>/ -o out/balatro
omgi install out/balatro --uninstall --yes    # replaces the store copy (read its warning)
```

Then in `~/.ophelia/games.yaml` add `omgi: true` to the game. On Termux it
connects to the game on 127.0.0.1; with an ADB phone body she runs
`adb forward` for the game's port herself.

| Tool | Use |
|------|-----|
| `phone_game_state` | The game's state as text + the actions possible right now |
| `phone_game_act` | Do one: `action=play_hand args={"cards":[1,3,4]}`; waits for the game to settle, returns the new state |
| `phone_game_query` | Read-only questions the game offers (e.g. `hand_info`: what would these cards make?) |

`phone_game_open` says whether the link is up, and `phone_game_look` adds the
link's state under the vision answer. If the link is down (store build, game
still loading) she falls back to looking and tapping. Link calls don't count
toward `OPHELIA_GAME_MAX_TURNS`, which caps vision turns.

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
