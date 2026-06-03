#!/data/data/com.termux/files/usr/bin/bash
# Ophelia Android body — Shizuku (rish) wrapper, OpenClaw-compatible.
# Install: cp to ~/phone_control.sh && chmod +x
# Requires: Shizuku running, rish in ~ or PATH (see termux-shizuku-setup.sh)

set -euo pipefail

RISH="${RISH:-$HOME/rish}"
ADB="${ADB:-$(command -v adb 2>/dev/null || true)}"

_run_shizuku() {
  if [[ -x "$RISH" ]]; then
    sh "$RISH" -c "$1"
    return $?
  fi
  if [[ -n "$ADB" ]] && adb devices 2>/dev/null | grep -q localhost; then
    adb shell "$1"
    return $?
  fi
  echo "❌ No Shizuku. Open Shizuku app → Start → export rish to Termux."
  return 1
}

cmd="${1:-help}"
shift || true

case "$cmd" in
  shell)
    _run_shizuku "$*"
    ;;
  ui-dump)
    _run_shizuku "uiautomator dump /dev/tty" 2>/dev/null || \
    _run_shizuku "uiautomator dump /sdcard/ophelia_ui.xml && cat /sdcard/ophelia_ui.xml" | \
    sed 's/></>\n</g' | head -c 15000
    ;;
  tap)
    x="${1:?x}"; y="${2:?y}"
    _run_shizuku "input tap $x $y"
    ;;
  swipe)
    x1="${1:?x1}"; y1="${2:?y1}"; x2="${3:?x2}"; y2="${4:?y2}"
    _run_shizuku "input swipe $x1 $y1 $x2 $y2 ${5:-300}"
    ;;
  open-app)
    pkg="${1:?package}"
    _run_shizuku "monkey -p $pkg -c android.intent.category.LAUNCHER 1"
    ;;
  screenshot)
    dest="${1:-/sdcard/ophelia_screen.png}"
    _run_shizuku "screencap -p $dest"
    echo "$dest"
    ;;
  volume-up)   _run_shizuku "input keyevent 24" ;;
  volume-down) _run_shizuku "input keyevent 25" ;;
  home)        _run_shizuku "input keyevent 3" ;;
  back)        _run_shizuku "input keyevent 4" ;;
  help|*)
    echo "Usage: phone_control.sh {ui-dump|tap|swipe|open-app|screenshot|shell|home|back|volume-up|volume-down}"
    ;;
esac
