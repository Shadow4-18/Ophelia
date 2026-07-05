#!/data/data/com.termux/files/usr/bin/bash
# Ophelia on Termux (S21 / non-root). Run from project root after git clone.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck source=termux-pip-env.sh
source "$ROOT/scripts/termux-pip-env.sh"

termux_fix_rust_path
termux_enable_plain_pip
export TERMUX_PYTHON="${PYTHON:-$(termux_resolve_python)}"

echo ""
echo "=== Ophelia Project — Termux install ==="
echo ""
echo "Python: $(command -v "$TERMUX_PYTHON") ($("$TERMUX_PYTHON" --version 2>&1))"
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
"$TERMUX_PYTHON" -m pip install -U setuptools wheel
termux_pip_install_editable "$ROOT"

echo "[4/5] Auto-setup (~/.ophelia)..."
"$TERMUX_PYTHON" -m ophelia setup --do

echo "[5/5] Step-by-step guide..."
"$TERMUX_PYTHON" -m ophelia setup

echo ""
echo "Verify: $TERMUX_PYTHON -m ophelia check"
echo "When ready: termux-wake-lock && tmux new -s ophelia && $TERMUX_PYTHON -m ophelia run"
