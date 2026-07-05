# Installation guide

Step-by-step setup for **The Ophelia Project** on **any host**: PC, laptop, home server, VPS, cloud VM, or optionally Termux on Android.

**You do not need a phone.** Most installs are brain-only (Ollama + Telegram/Discord/UI). A phone is only involved if you optionally enable the phone body (screen/tap) or choose Termux as your host.

**Short path:** run the install script for your platform, then follow `ophelia setup` until every required step shows `[OK]`.

---

## Before you start

### Requirements

| | PC / server / VPS | Phone as host (Termux) |
|---|-----|----------------|
| **Python** | 3.11+ | 3.11+ (via Termux) |
| **Git** | Yes | Yes |
| **RAM** | 8 GB+ (16 GB+ for local models) | 8 GB+ phone |
| **Phone body** | **Not required** | Optional (Shizuku) |
| **Typical** | Ollama + `ophelia run` or `ophelia ui` | Always-on pocket daemon |

### Get the code

```bash
git clone https://github.com/Shadow4-18/Ophelia.git
cd Ophelia
```

Or download and unzip from GitHub, then `cd` into the folder.

### Where config lives

Everything persistent goes in **`~/.ophelia/`** (Windows: `C:\Users\You\.ophelia\`):

```
~/.ophelia/
  .env              # secrets and settings
  SOUL.md           # personality
  data/memory.db    # conversations
  skills/           # learned procedures
  goals.yaml        # her goals
```

Never commit `.env` or tokens to git.

---

## Choose your path

| I want to… | Start here |
|------------|------------|
| **PC, laptop, server, or VPS** (most common) | [PC install](#pc-install-windows--macos--linux) |
| **24/7 on a VPS** | [PC install](#pc-install-windows--macos--linux) + Telegram/Discord, `OPHELIA_ANDROID_ENABLED=false` |
| **Run on my phone (Termux)** | [Termux install](#optional-termux-phone-as-host) |
| **Add a phone as body** (from PC/server) | PC install + [ADB body](remote-adb.md) — optional |
| **Import from Hermes** | [Hermes migration](#optional-import-from-hermes) |
| **Interactive checklist** | `ophelia setup` anytime |

---

## PC install (Windows / macOS / Linux / server / VPS)

Same steps for a desktop, homelab box, or cloud VPS. No phone required.

### Step 1 — Run the install script

**Windows (PowerShell):**

```powershell
cd E:\Projects\Ophelia   # your clone path
.\scripts\install.ps1
```

**macOS / Linux:**

```bash
cd ~/Ophelia
bash scripts/install.sh
```

**Manual equivalent:**

```bash
pip install -e .
ophelia setup --do
ophelia setup
```

### Step 2 — Install Ollama (recommended)

Local AI is the default — no cloud quota burn.

1. Download: [https://ollama.com/download](https://ollama.com/download)
2. Start the daemon (usually runs automatically after install)
3. Pull models:

```bash
ollama pull llama3.2:3b
ollama pull llava:7b          # optional: phone screen vision
ophelia models              # RAM-aware recommendations
```

### Step 3 — Edit config

Open `~/.ophelia/.env` (created by `ophelia setup --do`):

```env
OPHELIA_PROVIDER=ollama
OLLAMA_MODEL=llama3.2:3b
OPHELIA_CONSCIOUSNESS=true
```

See `config.example.env` in the repo for all options.

### Step 4 — Add personality

**Option A — write your own:**

Create `~/.ophelia/SOUL.md` with who Ophelia is.

**Option B — import Hermes:**

```bash
ophelia migrate hermes
```

### Step 5 — Verify

```bash
ophelia check --chat-only
ophelia chat "hello, who are you?"
ophelia ui
```

- `check --chat-only` — no Telegram/Discord required on PC
- `ui` — browser workstation at http://127.0.0.1:8765

### Step 6 — Chat channels (optional)

**Telegram:** see [channels.md](channels.md#telegram)

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=your_numeric_id
```

**Discord:** see [channels.md](channels.md#discord)

```env
DISCORD_BOT_TOKEN=...
DISCORD_ALLOWED_USER_IDS=your_snowflake_id
```

Then:

```bash
ophelia check
ophelia run
```

### Step 7 — Phone body (optional)

**Skip this** for a software-only Ophelia on PC/server/VPS — consciousness, chat, and channels all work without it.

If you want a physical phone for screen vision and tap/swipe while Ophelia runs elsewhere:

1. Install [platform-tools](https://developer.android.com/tools/releases/platform-tools) (`adb` in PATH)
2. Enable USB debugging on the phone
3. `adb connect PHONE_IP:5555` (wireless) or plug in USB
4. Add to `.env`:

```env
OPHELIA_ANDROID_ENABLED=true
OPHELIA_ADB_DEVICE=192.168.1.50:5555
OPHELIA_VISION_ENABLED=true
```

Full guide: [remote-adb.md](remote-adb.md)

---

## Optional: Termux (phone as host)

Use this if you want the **daemon on the phone itself** — not required for most users. A VPS or home PC is often easier for 24/7 + Ollama.

For Samsung S21 Ultra or any Termux-capable device.

### Step 1 — Install Termux

Use [F-Droid Termux](https://f-droid.org/en/packages/com.termux/) (not Play Store build).

### Step 2 — Clone and install

```bash
pkg install git
cd ~
git clone https://github.com/Shadow4-18/Ophelia.git
cd Ophelia
bash scripts/termux-install.sh
```

This installs Python deps, the `ophelia` CLI, creates `~/.ophelia/`, and prints the step-by-step wizard.

### Step 3 — Configure brain

Edit `~/.ophelia/.env`:

**Local (if you run Ollama on PC and tunnel — advanced):** or use cloud:

```env
OPHELIA_PROVIDER=xai-oauth
```

Then import OAuth:

```bash
ophelia auth import-hermes
# or: grok login && ophelia auth import-grok
```

### Step 4 — Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy token
2. Message [@userinfobot](https://t.me/userinfobot) → copy your id
3. Add to `.env`:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=123456789
```

### Step 5 — Phone body (optional)

Only if you want on-device screen/tap. Skip for chat-only on Termux.

```bash
bash scripts/termux-shizuku-setup.sh
```

On the phone (once per reboot):

1. Open **Shizuku** → Start (wireless debugging)
2. Export to Termux → fix `~/rish` line 11: `PKG=com.termux`
3. Test: `bash ~/phone_control.sh ui-dump | head`

Details: [tier1-setup.md](tier1-setup.md)

### Step 6 — Keep alive and run

```bash
termux-wake-lock
tmux new -s ophelia
ophelia check
ophelia run
```

Detach: `Ctrl+B` then `D`  
Reattach: `tmux attach -t ophelia`

Optional: Termux:Boot for auto-start — see `scripts/termux-boot.sh`.

---

## Optional: Import from Hermes

If you used Hermes on an old phone:

**Same phone / Termux:**

```bash
ophelia migrate hermes
ophelia auth import-hermes
```

**Old phone → new phone (bundle):**

```bash
# Old phone
bash scripts/termux-export-hermes.sh
# Copy bundle to new phone, then:
bash scripts/termux-import-hermes.sh
```

**Phone → PC:**

```bash
# Phone
ophelia transfer cloud-upload

# PC
ophelia transfer cloud-download "PASTE_URL_HERE"
```

See [transfer.md](transfer.md) and [migrate-old-phone.md](migrate-old-phone.md).

---

## Verify installation

```bash
ophelia check                  # full self-check
ophelia check --chat-only      # PC without bots
ophelia providers              # which models per role
ophelia setup                  # human checklist with [OK] / [  ]
```

**Exit code 0** = ready. Fix any required `FAIL` lines and re-run.

| Check | What it means |
|-------|----------------|
| Ophelia package | `pip install -e .` worked |
| Dependencies | httpx, discord.py, telegram, etc. |
| Provider chat | Ollama running or cloud credentials set |
| Chat channels | Telegram and/or Discord configured |
| Phone body (optional) | Shizuku or ADB working |

---

## Daily commands

| Command | When |
|---------|------|
| `ophelia run` | 24/7 — bots + consciousness |
| `ophelia ui` | PC browser workstation |
| `ophelia chat "..."` | One-shot message |
| `ophelia setup` | "What step am I on?" |
| `ophelia check` | Something broke — diagnose |

---

## Troubleshooting

### `ophelia: command not found`

The `ophelia` command is **not part of the git repo** — pip creates it at install time in `$PREFIX/bin` or `~/.local/bin`. If install failed (Python upgrade, pydantic-core, etc.), the command disappears. **Your project files in `~/Ophelia` are still there.**

**Quick run without reinstalling** (from the repo):

```bash
cd ~/Ophelia
bash scripts/ophelia --help
bash scripts/ophelia run
```

**Full fix (Termux, Python 3.14):**

```bash
cd ~/Ophelia
git pull
bash scripts/termux-repair.sh
```

This installs Python 3.13 from TUR if needed, reinstalls Ophelia, and recreates `~/.local/bin/ophelia`.

If `ophelia` still not found after repair:

```bash
export PATH="$HOME/.local/bin:$PATH"
# add permanently:
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

**PC:**

```bash
pip install -e .
```

### `bad interpreter` / `python3.13: No such file or directory` (Termux)

**Why:** Termux upgraded Python (e.g. 3.13 → 3.14). The `ophelia` wrapper pip wrote earlier still points at the **old** interpreter path in its shebang line.

**Fix:** re-run the Termux repair script (removes stale wrappers, reinstalls with the current Python):

```bash
cd ~/Ophelia
git pull
bash scripts/termux-repair.sh
```

Until that finishes, you can still run Ophelia directly:

```bash
cd ~/Ophelia
python -m ophelia check
python -m ophelia run
```

### Python 3.14 + `pydantic-core` / `crate 'std' required in rlib format` (Termux)

**Why:** Termux's default `python` may be **3.14**, but prebuilt Android `pydantic-core` wheels only exist for **Python 3.9–3.13**. There is **no `python3.13` package** in TUR (3.13 is already the main interpreter on many mirrors).

**Fix:** your mirror has **`python-is-python3.11`** (not `python3.13`). Install that to switch default Python to 3.11:

```bash
pkg install tur-repo
pkg update
pkg install python-is-python3.11
python --version    # should show 3.11.x

cd ~/Ophelia
git pull
bash scripts/termux-repair.sh
```

If `python-is-python3.11` is unavailable, try `python-is-python3.10`.

Then run Ophelia:

```bash
python -m ophelia run
# or: bash scripts/ophelia run
```

If no TUR Python is available, the repair script will try compiling `pydantic-core` on 3.14 (slow). Ensure rust is fixed first:

```bash
export PATH="$(echo "$PATH" | tr ':' '\n' | grep -v "$HOME/.cargo/bin" | paste -sd: -)"
export ANDROID_API_LEVEL=24
pkg install -y rust rust-std-aarch64-linux-android
```

### `duplicate ... ophelia/ui/static/app.css` during `pip install`

**Why:** An older wheel build config included the UI static files twice. Fixed in current `main` — `git pull` then reinstall.

**Fix (Termux):**

```bash
cd ~/Ophelia
git pull
bash scripts/termux-repair.sh
```

**Fix (PC):**

```bash
cd ~/Ophelia
git pull
pip install -e .
```

### Kokoro TTS not working / "connection refused" on port 8880

**Why:** Kokoro is **not** a Python package Ophelia installs. It is a **separate local server** (Kokoros on Termux, or Kokoro-FastAPI on PC) that exposes an OpenAI-compatible API on `http://127.0.0.1:8880/v1`.

Do **not** ask Ophelia to `pip install kokoro` — that downloads ~300MB of model weights, often times out, and does not start the server.

**Termux (offline on phone)** — native Android builds usually **fail at the ONNX link step** (`__fprintf_chk` / `std::__cxx11`). Use **proot Ubuntu** (recommended):

```bash
cd ~/Ophelia
git pull
bash scripts/termux-kokoro-proot-setup.sh
proot-distro login ubuntu
# follow printed steps — proot Ubuntu, cargo build --release, port 8880
```

Ophelia stays on native Termux; Kokoros runs inside proot on `127.0.0.1:8880`.

**Native Termux build** (experimental — often fails after ~10 min compile):

```bash
bash scripts/termux-kokoro-setup.sh
```

If you already hit the ONNX linker error, do **not** retry native — switch to proot above.

Then in `~/.ophelia/.env`:

```env
OPHELIA_TTS_PROVIDER=kokoro
KOKORO_TTS_URL=http://127.0.0.1:8880/v1
KOKORO_TTS_VOICE=af_heart
```

**Quick voice fallback** while Kokoro is down — use xAI TTS (if you have OAuth configured):

```env
OPHELIA_TTS_PROVIDER=xai
```

**Verify Kokoro server:**

```bash
curl -s http://127.0.0.1:8880/v1/audio/voices | head
ophelia tts voices
ophelia tts speak "test" --play
```

**Auto-start with Ophelia (Termux):** you do not need a separate tmux window for
`koko` on the phone. When `OPHELIA_TTS_PROVIDER=kokoro`, `ophelia run` spawns
the server if it is down (proot Ubuntu build is auto-detected). Disable with
`OPHELIA_KOKORO_AUTOSTART=false`. Logs: `~/.ophelia/kokoro.log`.

### `crate 'core' required to be available in rlib format` (Termux / Kokoro pip install)

**Why:** `pip install kokoro` (or related packages) tries to **compile Rust wheels** on Termux. After `pkg upgrade`, this often fails because:

- **`rustup` hijacked PATH** — `~/.cargo/bin` must not come before Termux's `rustc`
- **Missing std library** — need `rust-std-aarch64-linux-android` (or your arch) alongside `rust`
- **Python 3.14** — see section above; use Python 3.13 for Ophelia

**Fix:** do not pip-install Kokoro. Fix Rust, then build Kokoros:

```bash
export PATH="$(echo "$PATH" | tr ':' '\n' | grep -v "$HOME/.cargo/bin" | paste -sd: -)"
pkg install -y rust rust-std-aarch64-linux-android binutils clang libopus
cd ~/Ophelia
bash scripts/termux-kokoro-setup.sh
```

If you previously ran `rustup-init`, edit `~/.cargo/env` and remove the line that prepends `~/.cargo/bin` to PATH.

### `cmake` / `cannot locate symbol "_ZN4Json5Value` (Kokoros espeak-rs-sys build)

**Why:** `cmake` on Termux links against `jsoncpp`. A polluted `LD_LIBRARY_PATH` (often `~/.local/lib`, `/system/lib`, or `/vendor/lib` from other hacks) loads the wrong `.so` and cmake crashes when building `espeak-ng`.

**Fix:**

```bash
pkg upgrade -y cmake jsoncpp
unset LD_LIBRARY_PATH
cmake --version    # must print a version, not CANNOT LINK EXECUTABLE
cd ~/Ophelia
bash scripts/termux-kokoro-setup.sh
```

If `cmake --version` only works after `unset LD_LIBRARY_PATH`, edit `~/.bashrc` and remove the `LD_LIBRARY_PATH=...` line (or the paths that break cmake). The Kokoro setup script now unsets it during `cargo build` automatically.

Resume a failed build (audiopus patch is cached):

```bash
cd ~/Kokoros
export ORT_CACHE_DIR=$HOME/.cache/ort
mkdir -p "$ORT_CACHE_DIR"
unset LD_LIBRARY_PATH
cargo build --release
```

### `ort-sys` / `could not determine cache directory` (Kokoros ONNX build)

**Why:** `ort-sys` (ONNX Runtime) does not know where to cache downloaded prebuilt binaries on Android/Termux. It panics at `build/main.rs` with `could not determine cache directory` even though prebuilt `aarch64-linux-android` binaries exist.

**Fix:** set a cache path before building:

```bash
export ORT_CACHE_DIR=$HOME/.cache/ort
mkdir -p "$ORT_CACHE_DIR"
cd ~/Ophelia
git pull
bash scripts/termux-kokoro-setup.sh
```

Or resume manually:

```bash
export ORT_CACHE_DIR=$HOME/.cache/ort
mkdir -p "$ORT_CACHE_DIR"
cd ~/Kokoros
unset LD_LIBRARY_PATH
cargo build --release
```

The setup script sets `ORT_CACHE_DIR` automatically on future runs.

### `audio_object_*` / `sonic*` linker errors (espeak-ng)

**Why:** Kokoros uses `espeak-rs-sys` for phonemes. On Termux, if `libpcaudio` is installed, espeak-ng compiles with audio output enabled but the final `koko` link does not pull in `-lpcaudio`. `libsonic` is also not in Termux repos and must be linked explicitly.

**Fix:** pull latest Ophelia and re-run the setup script (patches `espeak-rs-sys`, builds `libsonic.a`):

```bash
cd ~/Ophelia
git pull
bash scripts/termux-kokoro-setup.sh
```

Or resume manually after pulling:

```bash
export ORT_CACHE_DIR=$HOME/.cache/ort
mkdir -p "$ORT_CACHE_DIR" ~/.cache/ophelia/sonic
unset LD_LIBRARY_PATH
unset RUSTFLAGS
cd ~/Ophelia
bash scripts/termux-kokoro-setup.sh   # applies patches + libsonic
```

Do **not** set global `RUSTFLAGS` with `-l sonic` or `-lc++abi` — that breaks `proc-macro2` and other build-script crates on Termux.

### `unable to find library -lc++abi` / `-lsonic` during proc-macro2 build

**Why:** Global `RUSTFLAGS` from a previous attempt polluted the build environment.

**Fix:**

```bash
unset RUSTFLAGS
cd ~/Ophelia && git pull
bash scripts/termux-kokoro-setup.sh
```

### `std::__cxx11` / `__fprintf_chk` linker errors (ONNX Runtime)

**Why:** The ONNX Runtime prebuild for `aarch64-linux-android` is a **static library built for a different C++ ABI** than Termux (Android bionic). Native Termux Kokoros **cannot link it** — this is not fixable with more flags.

**Fix — use proot Ubuntu** (works on S21 and most Termux phones):

```bash
cd ~/Ophelia
bash scripts/termux-kokoro-proot-setup.sh
proot-distro login ubuntu
```

Inside Ubuntu, follow the script output (`cargo build --release` — **not** `install.sh` XNNPACK; that feature no longer exists).

**proot: final link — `audio_object_*`, `sonic*`, or `OrtGetApiBase`**

Three common causes when `cargo build --release` fails at the **final link** inside proot:

1. **Termux env leaked into proot** — `ORT_SKIP_DOWNLOAD` or `ORT_LIB_LOCATION` pointing at Android ONNX
2. **Termux `.cargo/config.toml` patches** — `espeak-rs-sys` / `audiopus_sys` Android patches from `termux-kokoro-setup.sh`
3. **Missing link flags** — espeak needs `-l sonic -l pcaudio` on the final `koko` link

**Fix (recommended):**

```bash
cd /data/data/com.termux/files/home/Ophelia && git pull
cd ~/Kokoros
bash /data/data/com.termux/files/home/Ophelia/scripts/kokoro-proot-build.sh
```

Inside **proot Ubuntu**, `~` is `/root`. Ophelia is cloned in the **Termux** home (`/data/data/com.termux/files/home/Ophelia`), not `/root/Ophelia`. Do not use `~/Ophelia/...` from proot unless you symlinked it.

The script unsets bad ONNX vars, removes Termux Cargo patches, sets espeak link flags, and re-fetches ONNX for `aarch64-unknown-linux-gnu`.

Or manually:

```bash
# Remove Termux Android patches if present
mv ~/Kokoros/.cargo/config.toml ~/Kokoros/.cargo/config.toml.termux.bak 2>/dev/null || true

unset ORT_SKIP_DOWNLOAD ORT_LIB_LOCATION ORT_PREFER_DYNAMIC_LINK CARGO_NET_OFFLINE
export ORT_CACHE_DIR=$HOME/.cache/ort
export RUSTFLAGS="-L /usr/lib/aarch64-linux-gnu -l espeak-ng -l sonic -l pcaudio"
# If using DevGitPit fork: set ort default-features = true in kokoros/Cargo.toml
cargo clean -p ort-sys espeak-rs-sys audiopus_sys
cargo build --release   # NOT --offline
```

**Alternatives while Kokoro is down:**

```env
OPHELIA_TTS_PROVIDER=xai
```

Or run [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) on a PC and point `KOKORO_TTS_URL` at your LAN IP.

**Why:** `audiopus_sys` 0.2.2's `build.rs` has no Android branch — rustc fails to compile the build script itself on Termux (`expected bool, found ()`). `OPUS_STATIC=1` alone does not fix this.

**Fix:** pull latest Ophelia and re-run. The setup script vendors a patched `audiopus_sys` via Cargo `[patch.crates-io]` (editing the registry copy does not work — Cargo checksums it):

```bash
cd ~/Ophelia
git pull
bash scripts/termux-kokoro-setup.sh
```

First run downloads `audiopus_sys` (~few MB) into `scripts/kokoro-patches/` and writes `~/Kokoros/.cargo/config.toml`.

### Ollama not reachable

```bash
ollama serve
ollama pull llama3.2:3b
ophelia check --chat-only
```

### Telegram unauthorized

- `TELEGRAM_ALLOWED_USER_IDS` must be **your** numeric id (not the bot's)
- No spaces in the id list

### Discord bot ignores messages

- Enable **Message Content Intent** in Discord Developer Portal
- Bot needs permission to read/send in the channel or DM
- Commands use `!` prefix: `!start`, `!pause`

### `jiter`, `pydantic-core`, `maturin`, or `ANDROID_API_LEVEL` on Termux

**Why:** Termux is **not** a standard Linux glibc target. PyPI `manylinux` wheels often **do not install**, so pip tries to **compile Rust/C packages from source** (`pydantic-core`, `jiter`, `uvloop`, …). That fails with `maturin` / `ANDROID_API_LEVEL` errors.

Hermes/OpenClaw avoid this by:
- Using the **Termux User Repository** (TUR) for Android-native wheels
- Capping `openai<1.40` (no `jiter`)
- Installing build tools + `ANDROID_API_LEVEL` when compilation is unavoidable
- Skipping `uvicorn[standard]` extras that pull `uvloop`/`httptools`

**Fix:** always use the Termux install script (do not plain `pip install -e .`):

```bash
cd ~/Ophelia
git pull
bash scripts/termux-install.sh
```

Manual equivalent:

```bash
source scripts/termux-pip-env.sh
pkg install -y clang rust binutils libffi openssl pkg-config
termux_preinstall_native_wheels
termux_pip_install -e . -c scripts/termux-constraints.txt
```

This installs `pydantic-core` from TUR, caps `openai` at 1.39.x, pins `httpx<0.28`, and uses plain `uvicorn` (no `uvloop`). Ollama, xAI OAuth, Telegram, and Discord still work.

### `unexpected keyword argument 'proxies'` / consciousness errors on Termux

**Why:** Termux caps `openai` below 1.40 (no `jiter`), but `openai` 1.39 still passes `proxies=` to httpx. **httpx 0.28+** removed that argument — consciousness ticks and chat fail at runtime.

**Fix:**

```bash
cd ~/Ophelia
python -m pip install 'httpx>=0.27,<0.28'
# or re-run:
bash scripts/termux-install.sh
ophelia check
```

### Shizuku / ADB body fails

- Termux: Shizuku running? `~/rish` exists?
- PC: `adb devices` shows device? See [remote-adb.md](remote-adb.md)

### Consciousness spams or stays quiet

- `/pause` or `!pause` to pause outreach
- Adjust `OPHELIA_INITIATIVE_THRESHOLD` in `.env` (lower = more active)

### Still stuck

```bash
ophelia setup -i          # interactive walkthrough
ophelia check -v          # verbose hints
```

---

## Next steps

- [local-first.md](local-first.md) — Ollama strategy and training path
- [channels.md](channels.md) — Telegram + Discord together
- [pc-ui.md](pc-ui.md) — workstation UI
- [tier2-setup.md](tier2-setup.md) — inner log, curator, prompter
- [games.md](games.md) — mobile games layer
- [neuro-ensemble.md](neuro-ensemble.md) — future multi-mind architecture

---

## Quick reference card

```
INSTALL     scripts/install.ps1 | install.sh | termux-install.sh
WIZARD      ophelia setup [--do] [-i]
VERIFY      ophelia check [--chat-only]
CHAT (PC)   ophelia ui | ophelia chat "hi"
RUN         ophelia run
CONFIG      ~/.ophelia/.env
PERSONA     ~/.ophelia/SOUL.md
```
