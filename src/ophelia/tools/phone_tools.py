"""Phone body tool handlers (screen, tap, swipe, apps, shell, games).

Mixed into ToolRegistry; methods use its attributes (settings, memory,
android, senders, …). Split out of tools/registry.py."""

from __future__ import annotations

import asyncio
import time

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

    async def _phone_elements(self, *, fresh: bool = False) -> list:
        """Current numbered screen elements (re-read if stale or asked)."""
        vision = getattr(self, "vision", None)
        if vision is None:
            return []
        if fresh or not vision.last_elements or time.time() - vision.last_elements_at > 120:
            await vision.read_elements()
        return vision.last_elements

    async def _after_action(self) -> str:
        """Close the loop: what's on screen now that she touched it."""
        from ophelia.android.screen_elements import describe

        await asyncio.sleep(0.8)
        elements = await self._phone_elements(fresh=True)
        text = describe(elements, max_lines=14)
        return "\nNow " + text[0].lower() + text[1:]

    async def _phone_ui_dump(self, raw: bool = False) -> str:
        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        if raw:
            return await self.android.ui_dump()
        from ophelia.android.screen_elements import describe

        return describe(await self._phone_elements(fresh=True))

    async def _phone_tap(
        self,
        x: int | float | None = None,
        y: int | float | None = None,
        element: int | None = None,
        text: str | None = None,
        target: str | None = None,
        long_press: bool = False,
    ) -> str:
        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        from ophelia.android.screen_elements import find

        how = ""
        if element is not None or text:
            elements = await self._phone_elements(fresh=element is None)
            hit = None
            if element is not None:
                hit = next((e for e in elements if e.n == int(element)), None)
                if hit is None:
                    return (f"No element {element} on the current listing. Read the screen "
                            "again (phone_ui_dump or phone_see_screen) and use its numbers.")
            else:
                hit = find(elements, text or "")
                if hit is None:
                    return (f"Nothing on screen matches {text!r}. Try phone_ui_dump to see "
                            "what's there, or target=\"...\" to aim by sight.")
            x, y = hit.center
            how = f" on [{hit.n}] {hit.kind} \"{hit.label}\""
        elif target:
            vision = getattr(self, "vision", None)
            if vision is None:
                return "Aiming by sight needs the vision provider."
            try:
                x, y, how_note = await vision.aim(target)
            except Exception as e:
                return f"Couldn't aim at {target!r}: {e}"
            how = f" ({how_note})"
        elif x is None or y is None:
            return "Say what to tap: element=N (from the screen listing), text=\"...\", or target=\"...\"."
        x, y, note = await _resolve_tap_coords(self.android, x, y)
        if long_press:
            result = await self.android.swipe(x, y, x, y, 700)
        else:
            result = await self.android.tap(x, y)
        ok = "error" not in (result or "").lower() and "fail" not in (result or "").lower()
        msg = f"{'Long-pressed' if long_press else 'Tapped'} ({x},{y}){how}.{note}"
        if not ok:
            return f"{msg} Device said: {result}"
        return msg + await self._after_action()

    async def _phone_type(self, text: str, element: int | None = None,
                          enter: bool = False) -> str:
        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        pre = ""
        if element is not None:
            pre = (await self._phone_tap(element=element)).split("\n")[0] + " "
            await asyncio.sleep(0.4)
        result = await self.android.type_text(text)
        if enter:
            await self.android.shell("input keyevent 66")
        tail = " and pressed enter" if enter else ""
        if "error" in (result or "").lower():
            return f"{pre}Typing failed: {result}"
        skipped = "" if text.isascii() else " (non-ASCII characters/emoji can't be typed this way and were skipped)"
        return f"{pre}Typed {text!r}{tail}.{skipped}" + await self._after_action()

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
        x1: int | float | None = None,
        y1: int | float | None = None,
        x2: int | float | None = None,
        y2: int | float | None = None,
        duration_ms: int = 300,
        scroll: str | None = None,
        element: int | None = None,
        amount: float = 0.6,
    ) -> str:
        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        if scroll:
            # Scroll the view (or one scrollable element) instead of guessing
            # coordinates: "down" shows what's below (finger moves up).
            box = None
            if element is not None:
                hit = next((e for e in await self._phone_elements() if e.n == int(element)), None)
                if hit is None:
                    return f"No element {element} on the current listing."
                box = (hit.x1, hit.y1, hit.x2, hit.y2)
            else:
                size = await self.android.display_size()
                if not size:
                    return "Unknown screen size; can't scroll."
                w, h = size
                box = (0, int(h * 0.12), w, int(h * 0.88))
            bx1, by1, bx2, by2 = box
            cx, cy = (bx1 + bx2) // 2, (by1 + by2) // 2
            dx = int((bx2 - bx1) * amount / 2)
            dy = int((by2 - by1) * amount / 2)
            vectors = {
                "down": (cx, cy + dy, cx, cy - dy), "up": (cx, cy - dy, cx, cy + dy),
                "right": (cx + dx, cy, cx - dx, cy), "left": (cx - dx, cy, cx + dx, cy),
            }
            key = scroll.strip().lower()
            if key not in vectors:
                return "scroll must be up, down, left or right."
            x1, y1, x2, y2 = vectors[key]
            duration_ms = max(duration_ms, 350)
        if None in (x1, y1, x2, y2):
            return "Give scroll=up|down|left|right (optionally element=N), or x1,y1,x2,y2."
        x1, y1, n1 = await _resolve_tap_coords(self.android, x1, y1)
        x2, y2, n2 = await _resolve_tap_coords(self.android, x2, y2)
        result = await self.android.swipe(x1, y1, x2, y2, duration_ms)
        if "error" in (result or "").lower() or "fail" in (result or "").lower():
            return f"Swipe failed: {result}{n1}{n2}"
        what = f"Scrolled {scroll}" if scroll else f"Swiped ({x1},{y1})→({x2},{y2})"
        return f"{what}.{n1}{n2}" + await self._after_action()

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

    async def _phone_vibrate(self, duration_ms: int = 200) -> str:
        """Buzz the phone — Termux:API first, then shell vibrator service.

        Works on Termux without Shizuku when termux-vibrate is installed.
        """
        import asyncio
        import shutil

        from ophelia.platform import is_termux

        dur = max(50, min(2000, int(duration_ms or 200)))
        if is_termux() and shutil.which("termux-vibrate"):
            proc = await asyncio.create_subprocess_exec(
                "termux-vibrate",
                "-d",
                str(dur),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode == 0:
                return f"Vibrated {dur}ms (termux-vibrate)."
        reason = self._phone_unavailable_reason()
        if self.android and not reason:
            cmd = (
                f"cmd vibrator vibrate {dur} 2>/dev/null || "
                f"service call vibrator 1 i32 {dur} 2>/dev/null || "
                f"echo fail"
            )
            out = (await self.android.shell(cmd) or "").strip()
            if out and "fail" not in out.lower():
                return f"Vibrated {dur}ms (shell)."
            if out:
                return f"Vibrate attempted via shell: {out[:120]}"
        return (
            "Could not vibrate — need Termux:API (termux-vibrate) or a working "
            "phone shell bridge."
        )

    async def _phone_notifications(self, limit: int = 12) -> str:
        """List recent notifications without a full screen vision pass."""
        import asyncio
        import json
        import shutil

        from ophelia.platform import is_termux

        lim = max(1, min(40, int(limit or 12)))
        if is_termux() and shutil.which("termux-notification-list"):
            proc = await asyncio.create_subprocess_exec(
                "termux-notification-list",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                err = (stderr or b"").decode("utf-8", errors="replace")[:200]
                return f"termux-notification-list failed: {err or proc.returncode}"
            raw = (stdout or b"").decode("utf-8", errors="replace").strip()
            if not raw:
                return "No notifications listed (empty)."
            try:
                items = json.loads(raw)
            except json.JSONDecodeError:
                return raw[:2000]
            if not isinstance(items, list):
                return raw[:2000]
            lines = []
            for item in items[:lim]:
                if not isinstance(item, dict):
                    continue
                app = item.get("packageName") or item.get("id") or "?"
                title = item.get("title") or ""
                content = item.get("content") or item.get("text") or ""
                lines.append(f"- [{app}] {title}: {content}"[:220])
            if not lines:
                return "No notifications listed."
            return f"Recent notifications ({len(lines)}):\n" + "\n".join(lines)

        reason = self._phone_unavailable_reason()
        if reason:
            return (
                reason
                + " Also: install Termux:API and grant notification access for "
                "termux-notification-list."
            )
        # Fallback: dumpsys notification (noisy but better than nothing).
        out = await self.android.shell(
            "dumpsys notification --noredact 2>/dev/null | head -n 80"
        )
        text = (out or "").strip()
        if not text:
            return "No notification dump available."
        return "Notification dump (truncated):\n" + text[:2500]

    async def _phone_battery(self) -> str:
        """Read battery / charging state — cheap body awareness."""
        import asyncio
        import json
        import shutil

        from ophelia.platform import is_termux

        if is_termux() and shutil.which("termux-battery-status"):
            proc = await asyncio.create_subprocess_exec(
                "termux-battery-status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                raw = (stdout or b"").decode("utf-8", errors="replace").strip()
                try:
                    data = json.loads(raw)
                    pct = data.get("percentage", "?")
                    status = data.get("status", "?")
                    plugged = data.get("plugged", "?")
                    temp = data.get("temperature", "?")
                    return (
                        f"Battery {pct}% · status={status} · plugged={plugged} "
                        f"· temp={temp}°C"
                    )
                except json.JSONDecodeError:
                    return raw[:500] or "Battery status unavailable."

        reason = self._phone_unavailable_reason()
        if reason:
            return reason
        out = await self.android.shell("dumpsys battery 2>/dev/null | head -n 40")
        text = (out or "").strip()
        if not text:
            return "Battery dump unavailable."
        # Pull key lines.
        keep = []
        for line in text.splitlines():
            low = line.lower()
            if any(
                k in low
                for k in (
                    "level",
                    "scale",
                    "status",
                    "plugged",
                    "temperature",
                    "voltage",
                    "charge",
                )
            ):
                keep.append(line.strip())
        return "Battery:\n" + "\n".join(keep[:16] if keep else text.splitlines()[:16])
