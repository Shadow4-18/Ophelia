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
pkg install -y python git tmux termux-api

echo "[2/4] Python package..."
# Termux manages pip via pkg; do not self-upgrade pip.
# openai>=1.40 depends on jiter (Rust) — no wheel on Termux; cap via constraints.
python -m pip install -U setuptools wheel
python -m pip install --no-cache-dir -e . -c "$ROOT/scripts/termux-constraints.txt"

echo "[3/4] Auto-setup (~/.ophelia)..."
ophelia setup --do

echo "[4/4] Step-by-step guide..."
ophelia setup

echo ""
echo "When ready: termux-wake-lock && tmux new -s ophelia && ophelia run"
