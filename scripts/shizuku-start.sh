#!/data/data/com.termux/files/usr/bin/bash
# After reboot: start Shizuku via wireless debugging (Android 11+).
# Add Quick Settings tile for Wireless debugging — tap it, then run: sh ~/shizuku-start.sh
set -euo pipefail

if [[ -x "$HOME/rish" ]]; then
  if sh "$HOME/rish" -c "whoami" 2>/dev/null | grep -q shell; then
    echo "Shizuku already running."
    exit 0
  fi
fi

if command -v shizuku >/dev/null 2>&1; then
  shizuku
  echo "Ran shizuku helper."
elif [[ -f "$PREFIX/bin/shizuku" ]]; then
  shizuku
else
  echo "Open Shizuku app → Start (wireless debugging)."
  echo "Or install shizuku package in Termux if available."
fi

if [[ -x "$HOME/rish" ]]; then
  sh "$HOME/rish" -c "whoami" && echo "✓ rish OK" || echo "⚠ rish not ready"
fi
