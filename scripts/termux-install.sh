#!/data/data/com.termux/files/usr/bin/bash
# Ophelia on Termux (S21 / non-root). Run from project root after git clone.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck source=termux-pip-env.sh
source "$ROOT/scripts/termux-pip-env.sh"

echo ""
echo "=== Ophelia Project — Termux install ==="
echo ""
echo "ANDROID_API_LEVEL=${ANDROID_API_LEVEL}"
echo ""

echo "[1/5] Termux packages..."
pkg update -y
pkg install -y \
    python python-pip git tmux termux-api \
    clang rust binutils libffi openssl pkg-config
# Optional — RAM detection in ophelia models (cookbook); not required to run.
pkg install -y python-psutil 2>/dev/null || true

echo "[2/5] Pre-install Termux-native wheels (pydantic-core)..."
termux_preinstall_native_wheels

echo "[3/5] Ophelia + dependencies..."
# Termux manages pip via pkg; do not self-upgrade pip.
python -m pip install -U setuptools wheel
termux_pip_install -e "$ROOT" -c "$ROOT/scripts/termux-constraints.txt"

echo "[4/5] Auto-setup (~/.ophelia)..."
ophelia setup --do

echo "[5/5] Step-by-step guide..."
ophelia setup

echo ""
echo "Verify: ophelia check"
echo "When ready: termux-wake-lock && tmux new -s ophelia && ophelia run"
