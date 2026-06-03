# Tier 1 setup (S21 Ultra)

## 1. Vision loop

Requires Shizuku + Grok vision model.

```bash
bash scripts/termux-shizuku-setup.sh
bash ~/phone_control.sh screenshot /sdcard/ophelia_screen.png
ophelia doctor   # should show vision=on
```

Consciousness `explore` and tool `phone_see_screen`:

1. Screenshot via Shizuku  
2. Grok describes the screen  
3. UI dump for tap coordinates  
4. Optional tap / message  

Env:

```env
OPHELIA_VISION_ENABLED=true
XAI_VISION_MODEL=grok-4   # or same as XAI_MODEL if multimodal
```

## 2. Android survival

```bash
bash scripts/termux-survival.sh
```

- `termux-wake-lock` in `.bashrc`  
- `~/.termux/boot/ophelia.sh` → tmux + `ophelia run`  
- Battery: **unrestricted** for Termux + Shizuku  
- Install **Termux:Boot** (F-Droid), open once  

After reboot:

1. Shizuku app → Start  
2. `sh ~/shizuku-start.sh`  
3. Or rely on boot script (Ophelia starts; vision works after Shizuku manual start)

## 3. Goals

Edit `~/.ophelia/goals.yaml` (created from `goals.example.yaml`).

```yaml
goals:
  - id: explore-screen
    description: phone_see_screen — notice anything worth mentioning
    cadence_hours: 4
    priority: 0.65
    tags: [curiosity, android]
```

Due goals are injected into consciousness ticks.

## 4. Initiative tuning

```env
OPHELIA_CONSCIOUSNESS_INTERVAL=60
OPHELIA_INITIATIVE_THRESHOLD=0.50
OPHELIA_MAX_SPONTANEOUS_PER_HOUR=4
OPHELIA_QUIET_HOURS=23-08
```

Logs: `~/.ophelia/data/initiative_log.jsonl`

| Symptom | Fix |
|---------|-----|
| Too quiet | Lower threshold (0.40), interval 45 |
| Spam | Raise threshold (0.60), max 2/hour |
| Night pings | Set QUIET_HOURS |

## Verify

```bash
termux-wake-lock
tmux new -s ophelia
ophelia run
```

Telegram: `/pause` to stop outreach, `/resume` to enable.
