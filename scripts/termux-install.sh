#!/data/data/com.termux/files/usr/bin/bash
# Ophelia on Termux (S21 / non-root). Run from project root after git clone.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo ""
echo "=== Ophelia Project — Termux install ==="
echo ""

echo "[1/4] Termux packages..."
pkg update -y
# rust + binutils are needed to build jiter (a Rust dep of the openai SDK);
# Termux/Android has no prebuilt jiter wheel, so pip compiles it from source.
pkg install -y python git tmux termux-api rust binutils

echo "[2/4] Python package..."
# Termux manages pip via pkg; do not self-upgrade pip.
# pyo3/maturin cannot auto-detect the Android API level, so we set it
# explicitly — otherwise the jiter build fails with
# "Failed to determine Android API level". Override via env if needed.
export ANDROID_API_LEVEL="${ANDROID_API_LEVEL:-24}"
python -m pip install -U setuptools wheel
python -m pip install -e .

echo "[3/4] Auto-setup (~/.ophelia)..."
ophelia setup --do

echo "[4/4] Step-by-step guide..."
ophelia setup

echo ""
echo "When ready: termux-wake-lock && tmux new -s ophelia && ophelia run"
