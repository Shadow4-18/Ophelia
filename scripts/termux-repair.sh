#!/data/data/com.termux/files/usr/bin/bash
# Repair a broken Ophelia install on Termux after a failed pip install or Python upgrade.
#
# Symptoms:
#   ophelia: bad interpreter: .../python3.13: No such file or directory
#   pip install . fails: duplicate app.css in wheel / jiter / ANDROID_API_LEVEL
#   pydantic-core: crate 'std' required to be available in rlib format
#
# Run from the Ophelia repo root:
#   cd ~/Ophelia && git pull && bash scripts/termux-repair.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck source=termux-pip-env.sh
source "$ROOT/scripts/termux-pip-env.sh"

termux_fix_rust_path
termux_ensure_python313
export TERMUX_PYTHON="${PYTHON:-$(termux_resolve_python)}"

echo ""
echo "=== Ophelia Termux repair ==="
echo ""
echo "Python: $(command -v "$TERMUX_PYTHON") ($("$TERMUX_PYTHON" --version 2>&1))"
echo "ANDROID_API_LEVEL=${ANDROID_API_LEVEL}"
echo ""

# Remove stale console-script wrappers that point at an old python3.x shebang.
for bin in ophelia; do
    for prefix in \
        "${PREFIX:-/data/data/com.termux/files/usr}/bin" \
        "$HOME/.local/bin"; do
        if [[ -f "$prefix/$bin" ]]; then
            shebang="$(head -n1 "$prefix/$bin" 2>/dev/null || true)"
            if [[ "$shebang" == \#!* ]] && [[ ! -x "${shebang#\#!}" ]]; then
                echo "Removing stale wrapper: $prefix/$bin ($shebang)"
                rm -f "$prefix/$bin"
            fi
        fi
    done
done

echo "[1/4] Termux build packages..."
pkg install -y \
    python python-pip git \
    clang rust binutils libffi openssl pkg-config 2>/dev/null || \
pkg install -y \
    python python-pip git \
    clang rust binutils libffi openssl pkg-config

echo "[2/4] Native wheels (pydantic-core)..."
termux_preinstall_native_wheels

echo "[3/4] Reinstall Ophelia (editable, Termux constraints)..."
"$TERMUX_PYTHON" -m pip install -U setuptools wheel
termux_pip_install -e "$ROOT" -c "$ROOT/scripts/termux-constraints.txt"

echo "[4/4] Verify CLI..."
termux_install_ophelia_wrapper "$TERMUX_PYTHON"

if ! command -v ophelia >/dev/null 2>&1; then
  export PATH="$HOME/.local/bin:$PATH"
fi
if command -v ophelia >/dev/null 2>&1; then
  ophelia_cli="$(command -v ophelia)"
  ophelia_shebang="$(head -n1 "$ophelia_cli")"
  echo "  ophelia -> $ophelia_cli"
  echo "  shebang -> $ophelia_shebang"
  if ! ophelia --help >/dev/null 2>&1; then
    echo "WARNING: ophelia wrapper broken — use: $TERMUX_PYTHON -m ophelia"
  fi
fi

if ! "$TERMUX_PYTHON" -m ophelia --help >/dev/null 2>&1; then
    echo "ERROR: Ophelia package not importable."
    exit 1
fi

echo ""
echo "Repair complete. Next:"
echo "  ophelia check          # or: $TERMUX_PYTHON -m ophelia check"
echo "  ophelia run"
echo "  bash scripts/ophelia run   # fallback if PATH not updated yet"
echo ""
echo "Kokoro TTS is a separate local server — Ophelia does not pip-install it."
echo "  bash scripts/termux-kokoro-setup.sh"
