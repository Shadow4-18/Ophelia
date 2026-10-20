"""Phone body tool handlers (screen, tap, swipe, apps, shell, games).

Mixed into ToolRegistry; methods use its attributes (settings, memory,
android, senders, …). Split out of tools/registry.py."""

from __future__ import annotations

import structlog


log = structlog.get_logger()


# Commands that would let Ophelia accidentally start a second instance of
# herself (which then fights this one over the Telegram bot token — the
# "terminated by other getUpdates request" error) or kill her own runtime.
# Safety guard, not a security boundary.
_PHONE_SHELL_DENIED = (
    "ophelia run",
    "ophelia serve",
    "ophelia daemon",
    "ophelia start",
    "tmux new",
    "tmux kill-session",
    "tmux kill-server",
    "tmux attach",
    "pkill ophelia",
    "killall ophelia",
    "pkill -f ophelia",
)


def _phone_shell_blocked_reason(command: str) -> str | None:
    cmd = (command or "").strip()
    if not cmd:
        return None
    low = cmd.lower()
    for needle in _PHONE_SHELL_DENIED:
        if needle in low:
            return (
                f"Refused: '{needle}' in that shell command would start or stop "
                f"another Ophelia instance. Two instances polling the same Telegram "
                f"bot token fight forever (the 'terminated by other getUpdates "
                f"request' error). To check the running instance, use /status "
                f"instead. If you genuinely need to restart, do it from the Termux "
                f"session yourself (after killing the old one)."
            )
    return None


async def _resolve_tap_coords(
    android: object, x: int | float, y: int | float
) -> tuple[int, int, str]:
    """Normalize a tap coordinate into native-display pixels.

    - Native integer coords (the common case, e.g. from ui-dump bounds or the
      grid labels) pass through unchanged.
    - Floats in 0..1 are treated as normalized fractions and scaled to native
      (unambiguous — no native tap is a non-integer float).
    - Anything outside the native bounds is clamped and flagged so she re-reads
      the screen instead of silently mis-tapping.
    Returns (x, y, note) where `note` is "" or a diagnostic suffix.
    """
    xf, yf = float(x), float(y)
    native = None
    try:
        native = await android.display_size()  # type: ignore[attr-defined]
    except Exception:
        native = None

    note = ""
    # Normalized fractions -> native pixels.
    if native and 0.0 <= xf <= 1.0 and 0.0 <= yf <= 1.0 and (xf != int(xf) or yf != int(yf) or (xf == 0.0 and yf == 0.0)):
        # Only treat as fraction when at least one is a true float, to avoid
        # mis-reading integer 0/1 native taps.
        if xf != int(xf) or yf != int(yf):
            xi = round(xf * native[0])
            yi = round(yf * native[1])
            return xi, yi, f"  [scaled {xf}x{yf} -> {xi},{yi} native]"

    xi, yi = int(round(xf)), int(round(yf))
    if native:
        nw, nh = native
        ox, oy = xi, yi
        xi = max(0, min(xi, nw - 1))
        yi = max(0, min(yi, nh - 1))
        if (xi, yi) != (ox, oy):
            note = (
                f"  [WARN: tap {ox},{oy} out of native bounds {nw}x{nh}; "
                f"clamped to {xi},{yi}. Re-read the screen with phone_see_screen "
                f"and use ui-dump bounds or the grid labels for coordinates.]"
            )
    return xi, yi, note


class PhoneToolsMixin:
    async def _phone_see_screen(self, question: str = "") -> str:
        if not self.vision:
            return "Vision disabled or no phone body."
        return await self.vision.see(question=question or "What is on screen?")

    def _phone_unavailable_reason(self) -> str | None:
        """Precise reason the phone body can't be used right now, or None if OK.

        Distinguishes 'feature disabled' from 'bridge not wired' so the agent
        doesn't collapse both into 'I have no phone access' — the latter is
        fixable by running termux-shizuku-setup.sh, the former is an intentional
        config choice.
        """
        if not self.android:
            return "Phone body disabled (optional — enable OPHELIA_ANDROID_ENABLED)."
        mode = getattr(self.android, "mode", None)
        if mode in ("termux_only", "none"):
            return (
                "Phone bridge not wired — Shizuku/phone_control.sh missing. "
                "Run: bash scripts/termux-shizuku-setup.sh (and start Shizuku on the phone)."
            )
        return None

    async def _phone_ui_dump(self) -> str:
        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        return await self.android.ui_dump()

    async def _phone_tap(self, x: int | float, y: int | float) -> str:
        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        x, y, note = await _resolve_tap_coords(self.android, x, y)
        result = await self.android.tap(x, y)
        return f"{result}{note}"

    async def _phone_open_app(self, package: str) -> str:
        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        return await self.android.open_app(package)

    async def _phone_shell(self, command: str) -> str:
        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        blocked = _phone_shell_blocked_reason(command)
        if blocked:
            # Don't let her accidentally start a second Ophelia (which would
            # fight with this one over the Telegram bot token) or kill her own
            # runtime. This is a safety guard, not a security boundary.
            return blocked
        return await self.android.shell(command)

    async def _phone_swipe(
        self,
        x1: int | float,
        y1: int | float,
        x2: int | float,
        y2: int | float,
        duration_ms: int = 300,
    ) -> str:
        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        x1, y1, n1 = await _resolve_tap_coords(self.android, x1, y1)
        x2, y2, n2 = await _resolve_tap_coords(self.android, x2, y2)
        result = await self.android.swipe(x1, y1, x2, y2, duration_ms)
        return f"{result}{n1}{n2}"

    async def _phone_key(self, key: str) -> str:
        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        return await self.android.key(key)

    async def _phone_game_look(self, game_id: str = "", intent: str = "") -> str:
        if not self.vision:
            return "Vision disabled or no phone body."
        if not self.games:
            return "Games layer disabled (OPHELIA_GAMES=false)."
        profile = None
        if game_id.strip():
            profile = self.games.get(game_id.strip())
            if not profile:
                return f"Unknown game_id '{game_id}'. {self.games.format_list()}"
        else:
            profile = self.games.active_profile()
            if not profile:
                return (
                    "No active game session. Use phone_game_open or /game play <id>. "
                    + self.games.format_list()
                )
        result = await self.vision.see_for_game(profile, intent)
        self.games.record_turn()
        left = self.games.minutes_left()
        return f"{result}\n\n[session: {profile.id}, ~{left:.0f}m left]"

    async def _phone_game_open(self, game_id: str, minutes: float | None = None) -> str:
        if not self.games:
            return "Games layer disabled."
        return await self.games.start_session(
            game_id,
            minutes=minutes,
            android=self.android,
        )
