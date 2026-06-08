# PC → phone control (ADB)

When Ophelia runs on your **PC**, she can still use your **phone as a body** over ADB — no Termux required on the PC side.

Works **without root** for most actions (tap, swipe, screencap, shell as `shell` user). Optional **root** unlocks privileged commands on rooted devices.

## Without root (recommended)

### 1. Enable Developer options on the phone

Settings → About phone → tap Build number 7× → Developer options → **USB debugging** ON.

### 2. Install platform-tools on PC

- Windows: [Android platform-tools](https://developer.android.com/tools/releases/platform-tools) — add `adb` to PATH
- Or: `winget install Google.PlatformTools`

### 3. Pair (pick one)

**USB:** plug in, accept the RSA prompt on the phone.

```bash
adb devices
# should show your device serial
```

**Wireless (same Wi‑Fi):**

```bash
adb tcpip 5555          # once, over USB
adb connect PHONE_IP:5555
```

Android 11+ wireless pairing:

```bash
adb pair PHONE_IP:PAIRING_PORT
adb connect PHONE_IP:5555
```

### 4. Configure Ophelia

`~/.ophelia/.env`:

```env
OPHELIA_ANDROID_ENABLED=true
OPHELIA_VISION_ENABLED=true
OPHELIA_ADB_DEVICE=192.168.1.50:5555   # omit for USB default device
OPHELIA_ADB_ROOT=false
```

```bash
ophelia doctor --chat-only
# Android body: adb → 192.168.1.50:5555 (no root)
```

### 5. Test

```bash
ophelia chat "dump my phone UI and tell me what's on screen"
```

Tools: `phone_ui_dump`, `phone_tap`, `phone_swipe`, `phone_see_screen`, `phone_shell`, etc.

## With root (optional)

On a **rooted** phone with `adbd` root support:

```env
OPHELIA_ADB_ROOT=true
```

Ophelia runs `adb root` at connect time. If root is unavailable, she falls back to normal shell (no error — just limited commands).

Use root only when you need it (system apps, protected paths). Most game/vision flows work fine without it.

## Phone-native body (Termux + Shizuku)

When Ophelia runs **on the phone**, she uses Shizuku / `phone_control.sh` instead of ADB. Same tool names — different transport.

See [tier1-setup.md](tier1-setup.md) and `scripts/termux-shizuku-setup.sh`.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `adb: device unauthorized` | Re-plug USB, accept RSA dialog |
| `no devices` | `adb kill-server && adb start-server` |
| Wireless drops | Phone sleep — disable battery optimization for Developer options / keep screen on while testing |
| Screencap black | Some banking apps block capture — normal |
| Vision fails locally | Pull a vision model: `ollama pull llava:7b`, set `OPHELIA_PROVIDER_VISION=ollama` |

## Security note

ADB gives your PC full control of the phone while connected. Use wireless ADB only on trusted networks; disconnect when not in use:

```bash
adb disconnect 192.168.1.50:5555
```
