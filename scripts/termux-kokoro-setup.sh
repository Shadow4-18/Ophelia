#!/data/data/com.termux/files/usr/bin/bash
# Build and run Kokoros (local Kokoro TTS server) on Termux.
#
# Do NOT `pip install kokoro` — that pulls Rust/Python deps that fail to compile
# on Termux (rlib / jiter / maturin errors) and still does not start a server.
#
# Usage:
#   bash scripts/termux-kokoro-setup.sh          # build + download models
#   bash scripts/termux-kokoro-setup.sh run    # start server on :8880
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=termux-pip-env.sh
source "$ROOT/scripts/termux-pip-env.sh"

KOKOROS_DIR="${KOKOROS_DIR:-$HOME/Kokoros}"
KOKOROS_REPO="${KOKOROS_REPO:-https://github.com/lucasjinreal/Kokoros}"
PORT="${KOKORO_PORT:-8880}"
AUDIOOPUS_PATCH_DIR="$ROOT/scripts/kokoro-patches/audiopus_sys"

termux_fix_rust_path

termux_prepare_kokoros_build() {
    arch="$(uname -m)"
    std_pkg=""
    case "$arch" in
        aarch64) std_pkg="rust-std-aarch64-linux-android" ;;
        arm)     std_pkg="rust-std-arm-linux-androideabi" ;;
        i686)    std_pkg="rust-std-i686-linux-android" ;;
        x86_64)  std_pkg="rust-std-x86_64-linux-android" ;;
        *)       echo "Unknown arch: $arch"; exit 1 ;;
    esac

    echo "=== Termux Rust toolchain ==="
    pkg update -y
    pkg install -y rust "$std_pkg" binutils clang git curl libopus pkg-config cmake
    echo "rustc: $(command -v rustc) ($(rustc --version 2>&1))"

    export OPUS_STATIC=1
    export LIBOPUS_STATIC=1
}

termux_patch_audiopus_build_rs() {
    local build_rs="$1"
    local py="python3.11"
    command -v "$py" &>/dev/null || py="python"
    "$py" <<'PY' "$build_rs"
import re
import sys

path = sys.argv[1]
text = open(path).read()
if 'target_os = "android"' in text:
    sys.exit(0)

pat = r"fn default_library_linking\(\) -> bool \{.*?\n\}"
m = re.search(pat, text, flags=re.DOTALL)
if not m:
    print("ERROR: audiopus_sys build.rs layout changed — cannot patch", file=sys.stderr)
    sys.exit(1)

block = m.group(0)
if not block.rstrip().endswith("}"):
    print("ERROR: unexpected default_library_linking block", file=sys.stderr)
    sys.exit(1)

fixed = block[:-1] + """    #[cfg(target_os = "android")]
    {
        false
    }
}
"""
open(path, "w").write(text[: m.start()] + fixed + text[m.end() :])
print(f"  Patched {path}")
PY
}

termux_download_audiopus_crate() {
    local cache="${HOME}/.cache/ophelia"
    local crate="$cache/audiopus_sys-0.2.2.crate"
    local extract="$cache/audiopus_sys-0.2.2"
    mkdir -p "$cache"

    if [[ -d "$extract/Cargo.toml" ]]; then
        echo "$extract"
        return 0
    fi

    echo "  Downloading audiopus_sys crate to \$HOME/.cache/ophelia (avoids /tmp write errors)..."
    rm -f "$crate"
    local py="python3.11"
    command -v "$py" &>/dev/null || py="python"
    if ! "$py" <<'PY' "$crate"
import sys
import urllib.request

url = "https://static.crates.io/crates/audiopus_sys/audiopus_sys-0.2.2.crate"
path = sys.argv[1]
req = urllib.request.Request(url, headers={"User-Agent": "ophelia-termux-kokoro/1.0"})
with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
    while True:
        chunk = r.read(65536)
        if not chunk:
            break
        f.write(chunk)
print(f"  Downloaded {path}")
PY
    then
        curl -fL -A "ophelia-termux-kokoro/1.0" \
            "https://static.crates.io/crates/audiopus_sys/audiopus_sys-0.2.2.crate" \
            -o "$crate"
    fi

    rm -rf "$extract"
    tar xf "$crate" -C "$cache"
    echo "$extract"
}

termux_setup_audiopus_cargo_patch() {
    echo "=== audiopus_sys Termux patch (local [patch.crates-io]) ==="

    if [[ -f "$AUDIOOPUS_PATCH_DIR/Cargo.toml" ]] && \
       grep -q 'target_os = "android"' "$AUDIOOPUS_PATCH_DIR/build.rs" 2>/dev/null; then
        echo "  Using cached patched audiopus_sys at $AUDIOOPUS_PATCH_DIR"
    else
        local src=""
        # Prefer cargo registry (no curl) — fetch deps without patch override first.
        rm -f "$KOKOROS_DIR/.cargo/config.toml"
        echo "  cargo fetch (pulls audiopus_sys into ~/.cargo/registry)..."
        (cd "$KOKOROS_DIR" && cargo fetch)

        src="$(find "$HOME/.cargo/registry/src" -type d -path '*/audiopus_sys-0.2.2' 2>/dev/null | head -1)"
        if [[ -z "$src" || ! -f "$src/Cargo.toml" ]]; then
            echo "  Not in cargo registry yet — direct download..."
            src="$(termux_download_audiopus_crate)"
        fi

        echo "  Copying audiopus_sys from: $src"
        rm -rf "$AUDIOOPUS_PATCH_DIR"
        mkdir -p "$ROOT/scripts/kokoro-patches"
        cp -a "$src" "$AUDIOOPUS_PATCH_DIR"
        termux_patch_audiopus_build_rs "$AUDIOOPUS_PATCH_DIR/build.rs"
    fi

    mkdir -p "$KOKOROS_DIR/.cargo"
    cat >"$KOKOROS_DIR/.cargo/config.toml" <<EOF
# Written by Ophelia scripts/termux-kokoro-setup.sh — audiopus_sys Android fix
[patch.crates-io]
audiopus_sys = { path = "$AUDIOOPUS_PATCH_DIR" }
EOF
    echo "  Cargo patch -> $AUDIOOPUS_PATCH_DIR"
}

termux_build_kokoros() {
    echo ""
    echo "=== Kokoros build ==="
    if [[ ! -d "$KOKOROS_DIR/.git" ]]; then
        git clone "$KOKOROS_REPO" "$KOKOROS_DIR"
    fi
    cd "$KOKOROS_DIR"

    # Patch must run after Kokoros clone (uses cargo fetch + registry copy).
    termux_setup_audiopus_cargo_patch

    mkdir -p checkpoints data
    if [[ ! -f checkpoints/kokoro-v1.0.onnx ]]; then
        echo "Downloading Kokoro ONNX model (~300 MB)..."
        curl -L --progress-bar \
            "https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX/resolve/main/onnx/model.onnx" \
            -o checkpoints/kokoro-v1.0.onnx
    fi
    if [[ ! -f data/voices-v1.0.bin ]]; then
        echo "Downloading voice pack..."
        curl -L --progress-bar \
            "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin" \
            -o data/voices-v1.0.bin
    fi

    echo "Building Kokoros (release)..."
    cargo build --release
    echo ""
    echo "Built: $KOKOROS_DIR/target/release/koko"
}

termux_run_kokoros() {
    cd "$KOKOROS_DIR"
    if [[ ! -x target/release/koko ]]; then
        echo "Binary missing — run without 'run' first."
        exit 1
    fi
    echo "Starting Kokoros OpenAI-compatible server on http://127.0.0.1:${PORT}/v1"
    echo "In another tmux pane: ophelia tts speak 'hello' --play"
    exec ./target/release/koko openai --port "$PORT"
}

case "${1:-build}" in
    run|start)
        termux_prepare_kokoros_build
        termux_run_kokoros
        ;;
    build|"")
        termux_prepare_kokoros_build
        termux_build_kokoros
        echo ""
        echo "=== Next steps ==="
        echo "1. Add to ~/.ophelia/.env:"
        echo "     OPHELIA_TTS_PROVIDER=kokoro"
        echo "     KOKORO_TTS_URL=http://127.0.0.1:${PORT}/v1"
        echo "     KOKORO_TTS_VOICE=af_heart"
        echo "2. Start server (own tmux window):"
        echo "     bash scripts/termux-kokoro-setup.sh run"
        echo "3. Start Ophelia: ophelia run"
        ;;
    *)
        echo "Usage: $0 [build|run]"
        exit 1
        ;;
esac
