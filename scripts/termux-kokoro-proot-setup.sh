#!/data/data/com.termux/files/usr/bin/bash
# Kokoros on Termux via proot-distro Ubuntu (recommended when native build fails).
#
# Native Termux (aarch64-linux-android) often fails at the final link step:
#   - espeak-ng / libsonic / libpcaudio
#   - ONNX Runtime static C++ (__fprintf_chk, std::__cxx11)
#
# This script installs proot Ubuntu and prints the exact commands to build
# DevGitPit/Kokoros inside proot (glibc Linux — much higher success rate).
#
# Usage:
#   bash scripts/termux-kokoro-proot-setup.sh          # install proot + ubuntu
#   bash scripts/termux-kokoro-proot-setup.sh instructions
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

termux_install_proot() {
    echo "=== proot-distro + Ubuntu ==="
    pkg update -y
    pkg install -y proot-distro
    if ! proot-distro list 2>/dev/null | grep -q ubuntu; then
        echo "Installing Ubuntu (one-time, ~few hundred MB)..."
        proot-distro install ubuntu
    else
        echo "  Ubuntu already installed"
    fi
}

termux_print_instructions() {
    cat <<'EOF'
=== Build Kokoros inside proot Ubuntu ===

1. Enter Ubuntu:
     proot-distro login ubuntu

2. Inside Ubuntu:
     apt update && apt upgrade -y
     apt install -y git build-essential cmake curl clang \
         libssl-dev pkg-config \
         espeak-ng libespeak-ng-dev libsonic-dev libpcaudio-dev

     curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
     source "$HOME/.cargo/env"

     git clone https://github.com/DevGitPit/Kokoros
     cd Kokoros
     chmod +x install.sh
     ./install.sh
     # Choose XNNPACK (option 1). Thread count: 5 for SD 7+ Gen 3, else 4.

3. Run server (keep this tmux session open):
     tmux new -s kokoro
     ./target/release/koko openai --port 8880

   S21 / Snapdragon 8xx: use thread count 4 or 5 when install.sh asks.

4. Ophelia on Termux (outside proot) — in ~/.ophelia/.env:
     OPHELIA_TTS_PROVIDER=kokoro
     KOKORO_TTS_URL=http://127.0.0.1:8880/v1
     KOKORO_TTS_VOICE=af_heart

If ONNX download fails inside proot, download in native Termux first:
     curl -L -o ~/onnxruntime.tgz \
       https://cdn.pyke.io/0/pyke:ort-rs/ms@1.23.2/aarch64-unknown-linux-gnu.tar.lzma2
Then in proot, extract and:
     export ORT_LIB_LOCATION=/path/to/extracted
     export ORT_SKIP_DOWNLOAD=1
     cargo build --release --features xnnpack

EOF
}

case "${1:-install}" in
    install|"")
        termux_install_proot
        echo ""
        termux_print_instructions
        ;;
    instructions|help)
        termux_print_instructions
        ;;
    *)
        echo "Usage: $0 [install|instructions]"
        exit 1
        ;;
esac
