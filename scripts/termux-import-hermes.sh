#!/data/data/com.termux/files/usr/bin/bash
# Run on S21 Ultra after copying ophelia-hermes-bundle.tar.gz to ~/storage/downloads/
set -euo pipefail

BUNDLE="${1:-$HOME/storage/downloads/ophelia-hermes-bundle.tar.gz}"
OPHELIA="${OPHELIA_HOME:-$HOME/.ophelia}"

if [[ ! -f "$BUNDLE" ]]; then
  echo "Bundle not found: $BUNDLE"
  echo "Copy ophelia-hermes-bundle.tar.gz from old phone first."
  exit 1
fi

mkdir -p "$OPHELIA"
TMP=$(mktemp -d)
tar -xzf "$BUNDLE" -C "$TMP"
HERMES_SRC="$TMP/hermes"

if [[ ! -d "$HERMES_SRC" ]]; then
  echo "Invalid bundle layout"
  exit 1
fi

# Stage for ophelia migrate (reads ~/.hermes by default)
mkdir -p "$HOME/.hermes"
cp -a "$HERMES_SRC/"* "$HOME/.hermes/" 2>/dev/null || true

cd "${OPHELIA_PROJECT:-$HOME/Ophelia}"
# shellcheck source=termux-pip-env.sh
source scripts/termux-pip-env.sh
pkg install -y clang rust binutils libffi openssl pkg-config 2>/dev/null || true
termux_preinstall_native_wheels
termux_pip_install -e . -q -c scripts/termux-constraints.txt

ophelia migrate hermes --source "$HOME/.hermes"
ophelia auth import-hermes --hermes-home "$HOME/.hermes"

if [[ -f "$OPHELIA/from-hermes.env" ]]; then
  echo ""
  echo "Merge these into ~/.ophelia/.env:"
  cat "$OPHELIA/from-hermes.env"
fi

ophelia doctor
ophelia setup
echo ""
echo "Done. Start with:"
echo "  termux-wake-lock"
echo "  tmux new -s ophelia"
echo "  ophelia run"
