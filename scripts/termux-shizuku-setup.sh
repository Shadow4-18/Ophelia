#!/data/data/com.termux/files/usr/bin/bash
# Wire Shizuku → Termux for Ophelia's Android body (non-root S21).
set -euo pipefail

pkg install -y android-tools termux-api 2>/dev/null || pkg install -y android-tools

echo "=== Shizuku + Termux setup for Ophelia ==="
echo ""
echo "On your phone (once per reboot):"
echo "  1. Shizuku app → Pair via Wireless debugging → Start"
echo "  2. Shizuku → 'Use in terminal apps' → Export to Termux folder"
echo "  3. Fix rish line 11: replace PKG with com.termux (nano ~/rish)"
echo "  4. chmod +x ~/rish"
echo ""

if [[ -f "$HOME/rish" ]]; then
  chmod +x "$HOME/rish" 2>/dev/null || true
  echo "✓ Found ~/rish"
  sh "$HOME/rish" -c "whoami" && echo "✓ Shizuku shell works" || echo "⚠ rish failed — is Shizuku started?"
else
  echo "⚠ ~/rish not found — complete Shizuku export first"
fi

OPHELIA_DIR="${OPHELIA_DIR:-$HOME/Ophelia}"
if [[ -f "$OPHELIA_DIR/scripts/phone_control.sh" ]]; then
  cp "$OPHELIA_DIR/scripts/phone_control.sh" "$HOME/phone_control.sh"
  chmod +x "$HOME/phone_control.sh"
  echo "✓ Installed ~/phone_control.sh"
fi

grep -q "OPHELIA_ANDROID" "$HOME/.ophelia/.env" 2>/dev/null || {
  mkdir -p "$HOME/.ophelia"
  cat >> "$HOME/.ophelia/.env" <<'EOF'

# Android body (Shizuku)
OPHELIA_ANDROID_ENABLED=true
OPHELIA_PHONE_CONTROL=~/phone_control.sh
OPHELIA_INITIATIVE_THRESHOLD=0.55
EOF
  echo "✓ Added Android settings to ~/.ophelia/.env"
}

echo ""
echo "Test: bash ~/phone_control.sh ui-dump | head"
echo "Then: ophelia doctor"
