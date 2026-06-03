#!/data/data/com.termux/files/usr/bin/bash
# ~/.termux/boot/ophelia.sh — Termux:Boot (F-Droid). Survives reboot.
export PATH="$PREFIX/bin:$PATH"
termux-wake-lock

# Optional: try Shizuku (may fail if user hasn't paired yet — that's OK)
[[ -x "$HOME/shizuku-start.sh" ]] && sh "$HOME/shizuku-start.sh" 2>/dev/null || true

OPHELIA_DIR="${OPHELIA_DIR:-$HOME/Ophelia}"
cd "$OPHELIA_DIR" 2>/dev/null || cd "$HOME" || exit 0

if ! tmux has-session -t ophelia 2>/dev/null; then
  tmux new-session -d -s ophelia "export PATH=$PREFIX/bin:\$PATH; ophelia run"
fi

# Optional notification (Termux:API)
if command -v termux-notification >/dev/null 2>&1; then
  termux-notification -t "Ophelia" -c "Boot: consciousness session started in tmux:ophelia"
fi
