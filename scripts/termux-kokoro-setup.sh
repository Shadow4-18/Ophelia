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

KOKOROS_DIR="${KOKOROS_DIR:-$HOME/Kokoros}"
KOKOROS_REPO="${KOKOROS_REPO:-https://github.com/lucasjinreal/Kokoros}"
PORT="${KOKORO_PORT:-8880}"

termux_fix_rust() {
    echo "=== Termux Rust toolchain ==="

    # rustup breaks Termux's patched rustc (core/std rlib not found).
    if [[ -d "$HOME/.cargo/bin" ]] && [[ ":${PATH}:" == *":$HOME/.cargo/bin:"* ]]; then
        echo "WARNING: ~/.cargo/bin is on PATH (rustup). Removing from this session."
        export PATH="$(echo "$PATH" | tr ':' '\n' | grep -v "$HOME/.cargo/bin" | paste -sd: -)"
    fi
    if [[ -f "$HOME/.cargo/env" ]] && grep -q 'cargo/bin' "$HOME/.cargo/env" 2>/dev/null; then
        echo "TIP: comment out the PATH line in ~/.cargo/env if rustc fails after pkg upgrade."
    fi

    arch="$(uname -m)"
    std_pkg=""
    case "$arch" in
        aarch64) std_pkg="rust-std-aarch64-linux-android" ;;
        arm)     std_pkg="rust-std-arm-linux-androideabi" ;;
        i686)    std_pkg="rust-std-i686-linux-android" ;;
        x86_64)  std_pkg="rust-std-x86_64-linux-android" ;;
        *)       echo "Unknown arch: $arch"; exit 1 ;;
    esac

    pkg update -y
    pkg install -y rust "$std_pkg" binutils clang git curl
    echo "rustc: $(command -v rustc) ($(rustc --version 2>&1))"
}

termux_build_kokoros() {
    echo ""
    echo "=== Kokoros build ==="
    if [[ ! -d "$KOKOROS_DIR/.git" ]]; then
        git clone "$KOKOROS_REPO" "$KOKOROS_DIR"
    fi
    cd "$KOKOROS_DIR"

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
        termux_fix_rust
        termux_run_kokoros
        ;;
    build|"")
        termux_fix_rust
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
