#!/data/data/com.termux/files/usr/bin/bash
# Tier 1: keep Ophelia alive on Android (S21). Run once after Termux install.
set -euo pipefail

echo "=== Ophelia Android survival setup ==="

pkg install -y termux-api tmux 2>/dev/null || true

# Wake lock helper
if ! grep -q "termux-wake-lock" "$HOME/.bashrc" 2>/dev/null; then
  echo 'termux-wake-lock' >> "$HOME/.bashrc"
  echo "✓ Added termux-wake-lock to .bashrc"
fi

# Termux:Boot
BOOT_DIR="$HOME/.termux/boot"
mkdir -p "$BOOT_DIR"
OPHELIA_DIR="${OPHELIA_DIR:-$HOME/Ophelia}"
if [[ -f "$OPHELIA_DIR/scripts/termux-boot.sh" ]]; then
  cp "$OPHELIA_DIR/scripts/termux-boot.sh" "$BOOT_DIR/ophelia.sh"
  chmod +x "$BOOT_DIR/ophelia.sh"
  echo "✓ Installed $BOOT_DIR/ophelia.sh (requires Termux:Boot app from F-Droid)"
else
  echo "⚠ Clone Ophelia to $OPHELIA_DIR first"
fi

# Shizuku reminder script
if [[ -f "$OPHELIA_DIR/scripts/shizuku-start.sh" ]]; then
  cp "$OPHELIA_DIR/scripts/shizuku-start.sh" "$HOME/shizuku-start.sh"
  chmod +x "$HOME/shizuku-start.sh"
fi

mkdir -p "$HOME/.ophelia/data"
if [[ ! -f "$HOME/.ophelia/goals.yaml" ]] && [[ -f "$OPHELIA_DIR/goals.example.yaml" ]]; then
  cp "$OPHELIA_DIR/goals.example.yaml" "$HOME/.ophelia/goals.yaml"
  echo "✓ Created ~/.ophelia/goals.yaml"
fi

cat >> "$HOME/.ophelia/.env" 2>/dev/null <<'EOF' || true

# Tier 1 tuning
OPHELIA_CONSCIOUSNESS_INTERVAL=60
OPHELIA_INITIATIVE_THRESHOLD=0.50
OPHELIA_MAX_SPONTANEOUS_PER_HOUR=4
OPHELIA_QUIET_HOURS=23-08
OPHELIA_VISION_ENABLED=true
EOF

echo ""
echo "Manual steps (Android settings):"
echo "  • Battery → unrestricted for Termux + Shizuku"
echo "  • Install Termux:Boot from F-Droid, open once"
echo "  • After each reboot: Shizuku Start → ~/shizuku-start.sh (optional)"
echo ""
echo "Run Ophelia:"
echo "  termux-wake-lock"
echo "  tmux new -s ophelia"
echo "  ophelia run"
