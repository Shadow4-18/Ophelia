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
ESPEAK_PATCH_DIR="$ROOT/scripts/kokoro-patches/espeak-rs-sys"
SONIC_LIB_DIR="${HOME}/.cache/ophelia/sonic"

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

termux_prepare_ort_cache() {
    echo "=== ort-sys ONNX cache (Termux/Android has no XDG default) ==="
    export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
    export ORT_CACHE_DIR="${ORT_CACHE_DIR:-$HOME/.cache/ort}"
    mkdir -p "$ORT_CACHE_DIR"
    echo "  ORT_CACHE_DIR=$ORT_CACHE_DIR"
}

termux_prepare_ort_link() {
    termux_prepare_ort_cache
    local target arch ort_root ort_dir
    arch="$(uname -m)"
    case "$arch" in
        aarch64) target="aarch64-linux-android" ;;
        arm|armv7*) target="armv7-linux-androideabi" ;;
        i686) target="i686-linux-android" ;;
        x86_64) target="x86_64-linux-android" ;;
        *) return 0 ;;
    esac
    ort_root="$ORT_CACHE_DIR/dfbin/$target"
    [[ -d "$ort_root" ]] || return 0
    ort_dir="$(find "$ort_root" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -1)"
    [[ -n "$ort_dir" ]] || return 0

    shopt -s nullglob
    local so_files=("$ort_dir"/libonnxruntime*.so)
    shopt -u nullglob
    if ((${#so_files[@]})); then
        echo "=== ort-sys dynamic ONNX link ==="
        export ORT_LIB_LOCATION="$ort_dir"
        export ORT_PREFER_DYNAMIC_LINK=1
        export TERMUX_KOKORO_ORT_LIB_DIR="$ort_dir"
        echo "  ORT_LIB_LOCATION=$ORT_LIB_LOCATION (shared)"
        return 0
    fi

    if [[ -f "$ort_dir/libonnxruntime.a" ]]; then
        echo "=== ort-sys static ONNX prebuild ==="
        echo "  static prebuild at $ort_dir"
        echo "  (ort-sys links libc++_shared itself — no global RUSTFLAGS)"
        echo "  NOTE: if link still fails with __fprintf_chk / std::__cxx11, use proot:"
        echo "        bash scripts/termux-kokoro-proot-setup.sh"
    fi
}

termux_build_sonic_lib() {
    echo "=== libsonic (espeak-ng — not packaged on Termux) ==="
    mkdir -p "$SONIC_LIB_DIR"
    if [[ -f "$SONIC_LIB_DIR/libsonic.a" ]]; then
        echo "  using $SONIC_LIB_DIR/libsonic.a"
        return 0
    fi
    local sonic_rev="fbf75c3d6d846bad3bb3d456cbc5d07d9fd8c104"
    if [[ ! -f "$SONIC_LIB_DIR/sonic.c" ]]; then
        curl -fsSL \
            "https://raw.githubusercontent.com/waywardgeek/sonic/${sonic_rev}/sonic.c" \
            -o "$SONIC_LIB_DIR/sonic.c"
        curl -fsSL \
            "https://raw.githubusercontent.com/waywardgeek/sonic/${sonic_rev}/sonic.h" \
            -o "$SONIC_LIB_DIR/sonic.h"
    fi
    (
        cd "$SONIC_LIB_DIR"
        "${CC:-clang}" -O2 -c sonic.c -o sonic.o
        ar rcs libsonic.a sonic.o
    )
    echo "  built $SONIC_LIB_DIR/libsonic.a"
}

termux_cargo_build_release() {
    termux_prepare_ort_link
    termux_build_sonic_lib
    export OPHELIA_SONIC_LIB_DIR="$SONIC_LIB_DIR"
    local -a env_args=()
    if [[ "${TERMUX_KOKORO_UNSET_LD:-}" == "1" ]]; then
        echo "  (unset LD_LIBRARY_PATH for build — avoids broken cmake/jsoncpp)"
        env_args+=(-u LD_LIBRARY_PATH)
    fi
    # Do NOT set global RUSTFLAGS (-l sonic / -lc++abi) — that breaks proc-macro
    # and other build-script crates on Termux. espeak-rs-sys emits link lines instead.
    unset RUSTFLAGS
    env "${env_args[@]}" cargo build --release
}

termux_write_cargo_patches() {
    mkdir -p "$KOKOROS_DIR/.cargo"
    cat >"$KOKOROS_DIR/.cargo/config.toml" <<EOF
# Written by Ophelia scripts/termux-kokoro-setup.sh — Termux Android fixes
[patch.crates-io]
audiopus_sys = { path = "$AUDIOOPUS_PATCH_DIR" }
espeak-rs-sys = { path = "$ESPEAK_PATCH_DIR" }
EOF
}

termux_prepare_cmake() {
    echo "=== cmake / jsoncpp (espeak-ng build needs working cmake) ==="
    pkg install -y cmake jsoncpp
    # Polluted LD_LIBRARY_PATH (~/.local/lib, /system/lib) breaks cmake+jsoncpp on Termux.
    if ! env -u LD_LIBRARY_PATH cmake --version >/dev/null 2>&1; then
        echo "ERROR: cmake cannot run (jsoncpp symbol missing)." >&2
        echo "  Try: pkg upgrade -y cmake jsoncpp" >&2
        echo "  Then: unset LD_LIBRARY_PATH && cmake --version" >&2
        echo "  If that works, remove bad paths from ~/.bashrc (see docs/INSTALL.md)." >&2
        exit 1
    fi
    echo "  cmake ok: $(env -u LD_LIBRARY_PATH cmake --version | head -1)"
    export TERMUX_KOKORO_UNSET_LD=1
}

termux_patch_audiopus_build_rs() {
    local build_rs="$1"
    local py="python3.11"
    command -v "$py" &>/dev/null || py="python"
    "$py" "$ROOT/scripts/kokoro-patches/patch-audiopus-build-rs.py" "$build_rs"
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
    if ! "$py" - "$crate" <<'PY'
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
        rm -f "$KOKOROS_DIR/.cargo/config.toml"
        echo "  cargo fetch (pulls crates into ~/.cargo/registry)..."
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
}

termux_patch_espeak_build_rs() {
    local build_rs="$1"
    local py="python3.11"
    command -v "$py" &>/dev/null || py="python"
    "$py" "$ROOT/scripts/kokoro-patches/patch-espeak-rs-sys-build-rs.py" "$build_rs"
}

termux_download_espeak_crate() {
    local cache="${HOME}/.cache/ophelia"
    local crate="$cache/espeak-rs-sys-0.1.9.crate"
    local extract="$cache/espeak-rs-sys-0.1.9"
    mkdir -p "$cache"

    if [[ -f "$extract/Cargo.toml" ]]; then
        echo "$extract"
        return 0
    fi

    echo "  Downloading espeak-rs-sys crate to \$HOME/.cache/ophelia..."
    rm -f "$crate"
    curl -fL -A "ophelia-termux-kokoro/1.0" \
        "https://static.crates.io/crates/espeak-rs-sys/espeak-rs-sys-0.1.9.crate" \
        -o "$crate"
    rm -rf "$extract"
    tar xf "$crate" -C "$cache"
    echo "$extract"
}

termux_setup_espeak_cargo_patch() {
    echo "=== espeak-rs-sys Termux patch (disable pcaudio on Android) ==="

    local fresh=0
    if [[ -f "$ESPEAK_PATCH_DIR/Cargo.toml" ]] && \
       grep -q 'target_os = "android"' "$ESPEAK_PATCH_DIR/build.rs" 2>/dev/null && \
       grep -q 'OPHELIA_SONIC_LIB_DIR' "$ESPEAK_PATCH_DIR/build.rs" 2>/dev/null; then
        echo "  Using cached patched espeak-rs-sys at $ESPEAK_PATCH_DIR"
    else
        fresh=1
        local src=""
        src="$(find "$HOME/.cargo/registry/src" -type d -path '*/espeak-rs-sys-0.1.9' 2>/dev/null | head -1)"
        if [[ -z "$src" || ! -f "$src/Cargo.toml" ]]; then
            src="$(termux_download_espeak_crate)"
        fi
        echo "  Copying espeak-rs-sys from: $src"
        rm -rf "$ESPEAK_PATCH_DIR"
        mkdir -p "$ROOT/scripts/kokoro-patches"
        cp -a "$src" "$ESPEAK_PATCH_DIR"
        termux_patch_espeak_build_rs "$ESPEAK_PATCH_DIR/build.rs"
    fi

    if [[ "$fresh" == "1" ]] && [[ -d "$KOKOROS_DIR/target" ]]; then
        echo "  cargo clean -p espeak-rs-sys (rebuild with Android patch)"
        (cd "$KOKOROS_DIR" && cargo clean -p espeak-rs-sys 2>/dev/null || true)
    fi
}

termux_setup_kokoros_cargo_patches() {
    termux_setup_audiopus_cargo_patch
    termux_setup_espeak_cargo_patch
    termux_write_cargo_patches
    echo "  Cargo patches -> audiopus_sys + espeak-rs-sys"
}

termux_build_kokoros() {
    echo ""
    echo "=== Kokoros build ==="
    if [[ ! -d "$KOKOROS_DIR/.git" ]]; then
        git clone "$KOKOROS_REPO" "$KOKOROS_DIR"
    fi
    cd "$KOKOROS_DIR"

    # Patches must run after Kokoros clone (uses cargo fetch + registry copy).
    termux_setup_kokoros_cargo_patches

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
    termux_cargo_build_release
    echo ""
    echo "Built: $KOKOROS_DIR/target/release/koko"
}

termux_run_kokoros() {
    cd "$KOKOROS_DIR"
    if [[ ! -x target/release/koko ]]; then
        echo "Binary missing — run without 'run' first."
        exit 1
    fi
    if [[ -n "${TERMUX_KOKORO_ORT_LIB_DIR:-}" ]]; then
        export LD_LIBRARY_PATH="${TERMUX_KOKORO_ORT_LIB_DIR}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    else
        local ort_dir
        ort_dir="$(find "${HOME}/.cache/ort/dfbin" -name 'libonnxruntime*.so' -print -quit 2>/dev/null)"
        ort_dir="${ort_dir%/*}"
        if [[ -n "$ort_dir" && -d "$ort_dir" ]]; then
            export LD_LIBRARY_PATH="${ort_dir}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
        fi
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
        termux_prepare_cmake
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
