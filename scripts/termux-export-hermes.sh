#!/data/data/com.termux/files/usr/bin/bash
# Run on OLD phone (Hermes in Termux). Creates a transfer bundle on internal storage.
set -euo pipefail

OUT="${1:-$HOME/storage/downloads/ophelia-hermes-bundle.tar.gz}"
HERMES="${HERMES_HOME:-$HOME/.hermes}"

if [[ ! -d "$HERMES" ]]; then
  echo "No Hermes data at $HERMES"
  exit 1
fi

termux-setup-storage 2>/dev/null || true

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/hermes"
for item in SOUL.md auth.json config.yaml .env memories skills state.db honcho.json memory_store.db; do
  if [[ -e "$HERMES/$item" ]]; then
    cp -a "$HERMES/$item" "$TMP/hermes/" 2>/dev/null || cp -r "$HERMES/$item" "$TMP/hermes/"
  fi
done

tar -czf "$OUT" -C "$TMP" hermes
echo "Bundle written: $OUT"
echo ""
echo "Transfer to S21 Ultra:"
echo "  USB cable → copy from Download/"
echo "  Or: termux-open $OUT  (share to Drive / Nearby)"
echo "  Or: scp from PC if you SSH into old phone"
