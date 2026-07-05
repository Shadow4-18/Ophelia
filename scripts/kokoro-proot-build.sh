#!/usr/bin/env bash
# Build Kokoros inside proot-distro Ubuntu (aarch64-unknown-linux-gnu).
# Run from the Kokoros repo root:
#   bash ~/Ophelia/scripts/kokoro-proot-build.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KOKOROS_DIR="${KOKOROS_DIR:-$(pwd)}"
KOKOROS_CARGO="$KOKOROS_DIR/kokoros/Cargo.toml"
CARGO_CONFIG="$KOKOROS_DIR/.cargo/config.toml"

if [[ ! -f "$KOKOROS_DIR/Cargo.toml" || ! -f "$KOKOROS_CARGO" ]]; then
    echo "ERROR: run from Kokoros repo root (need Cargo.toml and kokoros/Cargo.toml)" >&2
    echo "  cd ~/Kokoros && bash $ROOT/scripts/kokoro-proot-build.sh" >&2
    exit 1
fi

cd "$KOKOROS_DIR"

proot_check_deps() {
    local missing=()
    for pkg in libespeak-ng-dev libsonic-dev libpcaudio-dev libopus-dev pkg-config; do
        if ! dpkg -s "$pkg" &>/dev/null; then
            missing+=("$pkg")
        fi
    done
    if ((${#missing[@]})); then
        echo "ERROR: install Ubuntu build deps first:" >&2
        echo "  apt install -y build-essential cmake curl clang libssl-dev pkg-config \\" >&2
        echo "      libopus-dev espeak-ng libespeak-ng-dev libsonic-dev libpcaudio-dev" >&2
        echo "  missing: ${missing[*]}" >&2
        exit 1
    fi
}

proot_fix_ort_toml() {
  local py="python3"
  command -v "$py" &>/dev/null || py="python"
  "$py" - "$KOKOROS_CARGO" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()
new = re.sub(
    r'ort\s*=\s*\{[^}]*default-features\s*=\s*false[^}]*\}',
    'ort = { version = "2.0.0-rc.11", default-features = true }',
    text,
    count=1,
)
if new != text:
    path.write_text(new)
    print("  patched kokoros/Cargo.toml: ort default-features = true")
elif "default-features = false" in text and "ort" in text:
    print("ERROR: could not auto-fix ort in kokoros/Cargo.toml", file=sys.stderr)
    sys.exit(1)
PY
}

proot_clear_termux_cargo_config() {
    if [[ ! -f "$CARGO_CONFIG" ]]; then
        return 0
    fi
    if grep -q 'termux-kokoro-setup\|espeak-rs-sys\|audiopus_sys' "$CARGO_CONFIG" 2>/dev/null; then
        local backup="${CARGO_CONFIG}.termux.bak"
        echo "=== Removing Termux Android Cargo patches (not for proot glibc) ==="
        mv -f "$CARGO_CONFIG" "$backup"
        echo "  moved $CARGO_CONFIG -> $backup"
        echo "=== cargo clean espeak-rs-sys audiopus_sys (rebuild from crates.io) ==="
        cargo clean -p espeak-rs-sys -p audiopus_sys 2>/dev/null || true
    fi
}

proot_check_deps

# Termux Android patches break proot; use stock crates + apt libs instead.
proot_clear_termux_cargo_config

# DevGitPit fork disables ort prebuilt downloads — re-enable for proot Linux.
if grep -q 'default-features = false' "$KOKOROS_CARGO" 2>/dev/null; then
    echo "=== Fixing ort dependency (enable prebuilt ONNX download) ==="
    proot_fix_ort_toml
fi

# Clear Termux/Android ONNX overrides that break proot glibc builds.
echo "=== Clearing Termux ONNX env overrides ==="
for var in ORT_SKIP_DOWNLOAD ORT_LIB_LOCATION ORT_PREFER_DYNAMIC_LINK OPHELIA_SONIC_LIB_DIR; do
    if [[ -n "${!var:-}" ]]; then
        echo "  unset $var (was: ${!var})"
    fi
    unset "$var"
done

export ORT_CACHE_DIR="${ORT_CACHE_DIR:-$HOME/.cache/ort}"
mkdir -p "$ORT_CACHE_DIR"
echo "  ORT_CACHE_DIR=$ORT_CACHE_DIR"

if [[ "${CARGO_NET_OFFLINE:-}" == "true" ]]; then
    echo "ERROR: CARGO_NET_OFFLINE=true — ort-sys cannot download ONNX Runtime" >&2
    echo "  unset CARGO_NET_OFFLINE and retry (do not use cargo build --offline)" >&2
    exit 1
fi

# espeak-ng phonemizer needs sonic + pcaudio at final link (Kokoros #61).
export RUSTFLAGS="-L /usr/lib/aarch64-linux-gnu -l espeak-ng -l sonic -l pcaudio"
echo "  RUSTFLAGS=$RUSTFLAGS"

echo "=== cargo clean ort-sys (re-fetch ONNX for aarch64-unknown-linux-gnu) ==="
cargo clean -p ort-sys 2>/dev/null || true

echo "=== cargo build --release ==="
cargo build --release

echo ""
echo "Built: $KOKOROS_DIR/target/release/koko"
echo "Run:   ./target/release/koko openai --port 8880"
