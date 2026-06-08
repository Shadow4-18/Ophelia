#!/data/data/com.termux/files/usr/bin/bash
# Ophelia on Termux (S21 / non-root). Run from project root after git clone.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
CONSTRAINTS="$ROOT/constraints-termux.txt"

echo ""
echo "=== Ophelia Project — Termux install ==="
echo ""

echo "[1/4] Termux packages..."
pkg update -y
pkg install -y python git tmux termux-api

echo "[2/4] Android build environment (for any Rust wheels pip may compile)..."
if [[ -z "${ANDROID_API_LEVEL:-}" ]]; then
  ANDROID_API_LEVEL="$(getprop ro.build.version.sdk 2>/dev/null || true)"
fi
if [[ -z "${ANDROID_API_LEVEL:-}" ]]; then
  ANDROID_API_LEVEL=24
fi
export ANDROID_API_LEVEL
echo "  ANDROID_API_LEVEL=$ANDROID_API_LEVEL"

if ! grep -q 'ANDROID_API_LEVEL' "$HOME/.bashrc" 2>/dev/null; then
  echo 'export ANDROID_API_LEVEL="$(getprop ro.build.version.sdk)"' >> "$HOME/.bashrc"
  echo "  Added ANDROID_API_LEVEL to ~/.bashrc"
fi

echo "[3/4] Python package (openai pinned <1.40 to skip jiter Rust build)..."
# Termux manages pip via pkg; do not self-upgrade pip.
python -m pip install -U setuptools wheel
ANDROID_API_LEVEL="$ANDROID_API_LEVEL" python -m pip install -e . -c "$CONSTRAINTS"

echo "[4/4] Auto-setup (~/.ophelia)..."
ophelia setup --do

echo ""
echo "Step-by-step guide:"
ophelia setup

echo ""
echo "When ready: termux-wake-lock && tmux new -s ophelia && ophelia run"
