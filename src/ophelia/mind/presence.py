"""Owner presence signals — sharper "is he home?" than schedule + silence (Tier B #7).

LifeContext today infers owner presence from work schedule + Telegram silence.
That's coarse: a quiet Tuesday afternoon could be "at work", "asleep on the
couch", or "phone on silent". This module adds concrete signals:

  - Bluetooth proximity (Termux:API `termux-bt-scan`) — is the owner's phone
    / watch / earbuds visible? Strongest signal, when available.
  - Router device list (optional; via a small HTTP call to a router API or a
    Termux helper) — is the owner's device on the home network?
  - Last-seen tracking — when did any presence signal last fire?

Each signal is best-effort: missing binaries / failed scans degrade gracefully
to "unknown" and the existing schedule-based inference still runs. The summary
is injected into the LifeContext block so Ophelia can reason about it.

Configure target devices in .env:
  OPHELIA_OWNER_BT_DEVICES=00:11:22:33:44:55,My-AirPods
  OPHELIA_OWNER_ROUTER_API_URL=http://192.168.1.1/api/devices
  OPHELIA_OWNER_ROUTER_API_HEADER=Authorization: Bearer ...
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from dataclasses import dataclass, field

import httpx
import structlog

from ophelia.config import Settings

log = structlog.get_logger()

_BT_SCAN_INTERVAL = 90.0      # seconds between BT scans
_ROUTER_POLL_INTERVAL = 120.0  # seconds between router polls


@dataclass
class PresenceState:
    bt_visible: bool = False
    bt_device: str = ""
    router_visible: bool = False
    router_device: str = ""
    last_seen_bt: float = 0.0
    last_seen_router: float = 0.0
    last_refreshed: float = 0.0
    _bt_scanned_at: float = 0.0
    _router_scanned_at: float = 0.0


class PresenceSignals:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = PresenceState()
        self._bt_bin = shutil.which("termux-bt-scan")
        # Optional router poll config.
        self._router_url = (settings.owner_router_api_url or "").strip() or None
        self._router_header = (settings.owner_router_api_header or "").strip() or None
        self._bt_targets = [
            t.strip().lower() for t in (settings.owner_bt_devices or "").split(",")
            if t.strip()
        ]

    def available(self) -> bool:
        """True if any presence signal source is configured."""
        return bool(self._bt_bin) or bool(self._router_url)

    async def refresh(self) -> None:
        """Poll BT and router at their own intervals. Best-effort; failures
        don't reset existing state — they just leave it stale."""
        now = time.time()
        tasks: list = []
        if self._bt_bin and now - self.state._bt_scanned_at > _BT_SCAN_INTERVAL:
            self.state._bt_scanned_at = now
            tasks.append(self._scan_bt())
        if self._router_url and now - self.state._router_scanned_at > _ROUTER_POLL_INTERVAL:
            self.state._router_scanned_at = now
            tasks.append(self._poll_router())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.state.last_refreshed = now

    async def _scan_bt(self) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._bt_bin,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
            text = out.decode("utf-8", errors="replace")
        except Exception as e:
            log.debug("presence.bt_scan_failed", error=str(e))
            return
        # termux-bt-scan output: "MAC  Name" lines, or JSON if --format json.
        # Be tolerant: look for any configured target (MAC or name) in raw text.
        if not self._bt_targets:
            # No targets configured — just note whether ANY device was seen.
            self.state.bt_visible = bool(text.strip())
            if text.strip():
                self.state.last_seen_bt = time.time()
            return
        low = text.lower()
        for target in self._bt_targets:
            if target in low:
                self.state.bt_visible = True
                self.state.bt_device = target
                self.state.last_seen_bt = time.time()
                return
        self.state.bt_visible = False

    async def _poll_router(self) -> None:
        if not self._router_url:
            return
        headers = {}
        if self._router_header:
            key, _, val = self._router_header.partition(":")
            headers[key.strip()] = val.strip()
        try:
            async with httpx.AsyncClient(timeout=8.0) as http:
                r = await http.get(self._router_url, headers=headers or None)
                r.raise_for_status()
                text = r.text
        except Exception as e:
            log.debug("presence.router_poll_failed", error=str(e))
            return
        # Look for any configured BT target MAC/name in the router's device
        # list too — same device identifiers usually apply.
        if not self._bt_targets:
            self.state.router_visible = bool(text.strip())
            if text.strip():
                self.state.last_seen_router = time.time()
            return
        low = text.lower()
        for target in self._bt_targets:
            if target in low:
                self.state.router_visible = True
                self.state.router_device = target
                self.state.last_seen_router = time.time()
                return
        self.state.router_visible = False

    def is_home(self) -> bool | None:
        """Best-effort 'is the owner home' guess.

        Returns True if a presence signal currently fires, False if signals
        are available and none fire, None if no signals are configured.
        """
        if not self.available():
            return None
        # Recent BT sighting is the strongest signal.
        if self.state.bt_visible and time.time() - self.state.last_seen_bt < 600:
            return True
        if self.state.router_visible and time.time() - self.state.last_seen_router < 600:
            return True
        # If we have BT/router data but it's stale, say "probably not home".
        if self.state.last_seen_bt or self.state.last_seen_router:
            return False
        return None

    def summary(self) -> str:
        """Compact presence summary for the LifeContext prompt block."""
        if not self.available():
            return ""
        home = self.is_home()
        parts: list[str] = []
        if home is True:
            parts.append("Owner presence: home (BT/router saw a known device recently).")
        elif home is False:
            parts.append("Owner presence: away (no known device seen recently).")
        else:
            parts.append("Owner presence: unknown (no signal yet).")
        if self.state.bt_device and self.state.bt_visible:
            parts.append(f"  BT device last seen: {self.state.bt_device}")
        if self.state.router_device and self.state.router_visible:
            parts.append(f"  Router device: {self.state.router_device}")
        return "\n".join(parts)
