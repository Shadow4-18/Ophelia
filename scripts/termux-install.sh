#!/data/data/com.termux/files/usr/bin/bash
# Ophelia on Termux (S21 / non-root). Run from project root after git clone.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo ""
echo "=== Ophelia Project — Termux install ==="
echo ""

echo "[1/5] Termux packages..."
pkg update -y
# rust/clang/binutils: compile jiter (openai dep) via maturin on Android
pkg install -y python git tmux termux-api rust binutils clang make

echo "[2/5] Android build environment..."
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

echo "[3/5] Python build tools..."
# Termux manages pip via pkg; do not self-upgrade pip.
python -m pip install -U setuptools wheel maturin

echo "[4/5] Ophelia package (first install may take 10–30 min — compiling jiter)..."
python -m pip install -e .

echo "[5/5] Auto-setup (~/.ophelia)..."
ophelia setup --do

echo ""
echo "Step-by-step guide:"
ophelia setup

echo ""
echo "When ready: termux-wake-lock && tmux new -s ophelia && ophelia run"
