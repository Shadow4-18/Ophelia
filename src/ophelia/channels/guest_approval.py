"""Guest approval flow — let strangers ask to talk to Ophelia without the owner
having to know/collect their user IDs up front.

When an unknown user messages her (not the owner, not already in the allowlist),
their request is held and the owner is prompted (Telegram inline Accept/Decline
buttons, or a Discord ``!approve``/``!deny`` command). Accepting appends the
user's ID to the platform allowlist in ``~/.ophelia/.env`` and updates the live
Settings in memory, so they're admitted as a sandboxed guest from then on with
no restart.

Pending/denied state is kept in a small JSON file so it survives restarts and so
a stranger who keeps messaging while waiting isn't re-prompted to the owner
every time.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import structlog

from ophelia.config import OPHELIA_HOME

log = structlog.get_logger()

# platform -> (Settings field, .env key, accepted alias keys)
_PLATFORM_FIELDS = {
    "telegram": ("telegram_allowed_user_ids", "TELEGRAM_ALLOWED_USER_IDS", ("TELEGRAM_ALLOWED_USERS",)),
    "discord": ("discord_allowed_user_ids", "DISCORD_ALLOWED_USER_IDS", ()),
}


class GuestApprovals:
    def __init__(self, state_path: Path | None = None) -> None:
        self.state_path = state_path or (OPHELIA_HOME / "data" / "pending_guests.json")
        self._cache: dict[str, dict[str, Any]] | None = None

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._cache is not None:
            return self._cache
        try:
            self._cache = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(self._cache, dict):
                self._cache = {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._cache = {}
        return self._cache

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(self._load(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning("guest_approvals.save_failed", error=str(e))

    @staticmethod
    def _key(platform: str, user_id: int) -> str:
        return f"{platform}:{user_id}"

    def status(self, platform: str, user_id: int) -> str:
        """One of: 'none', 'pending', 'approved', 'denied'."""
        rec = self._load().get(self._key(platform, user_id))
        if not rec:
            return "none"
        return rec.get("status", "none")

    def is_pending(self, platform: str, user_id: int) -> bool:
        return self.status(platform, user_id) == "pending"

    def is_denied(self, platform: str, user_id: int) -> bool:
        return self.status(platform, user_id) == "denied"

    def get(self, platform: str, user_id: int) -> dict[str, Any] | None:
        return self._load().get(self._key(platform, user_id))

    def add_pending(
        self,
        platform: str,
        user_id: int,
        display_name: str,
        first_message: str,
    ) -> bool:
        """Record a pending request. Returns True if newly created (so the
        caller knows to prompt the owner), False if one already existed."""
        data = self._load()
        key = self._key(platform, user_id)
        if key in data and data[key].get("status") == "pending":
            return False
        data[key] = {
            "platform": platform,
            "user_id": user_id,
            "display_name": display_name,
            "first_message": first_message[:500],
            "ts": time.time(),
            "status": "pending",
        }
        self._save()
        return True

    def set_status(self, platform: str, user_id: int, status: str) -> dict[str, Any] | None:
        data = self._load()
        key = self._key(platform, user_id)
        rec = data.get(key)
        if not rec:
            return None
        rec["status"] = status
        rec["decided_at"] = time.time()
        self._save()
        return rec


def append_user_to_allowlist(
    settings, platform: str, user_id: int, env_path: Path | None = None
) -> bool:
    """Add a user ID to the platform allowlist: update the live Settings string
    AND persist it to ~/.ophelia/.env (or `env_path` if given) so it survives
    restarts. Returns True if the list actually changed."""
    info = _PLATFORM_FIELDS.get(platform)
    if not info:
        return False
    field, env_key, aliases = info
    current = getattr(settings, field, "") or ""
    ids = [x.strip() for x in current.split(",") if x.strip()]
    sid = str(user_id)
    if sid in ids:
        return False
    ids.append(sid)
    new_val = ",".join(ids)
    setattr(settings, field, new_val)
    _persist_env_var(env_key, new_val, aliases, env_path=env_path)
    log.info("guest_approvals.added_to_allowlist", platform=platform, user_id=user_id)
    return True


def _persist_env_var(
    key: str, value: str, aliases: tuple[str, ...], env_path: Path | None = None
) -> None:
    """Write `key=value` into ~/.ophelia/.env (or `env_path`), replacing an
    existing line for `key` or any known alias, or appending if absent."""
    env_path = env_path or (OPHELIA_HOME / ".env")
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    except OSError as e:
        log.warning("guest_approvals.env_read_failed", error=str(e))
        return
    keys = {key, *aliases}
    replaced = False
    out: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if not replaced and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in keys:
                out.append(f"{key}={value}")
                replaced = True
                continue
        out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    except OSError as e:
        log.warning("guest_approvals.env_write_failed", error=str(e))
