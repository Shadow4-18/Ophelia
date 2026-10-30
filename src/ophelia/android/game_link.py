"""Direct game link via O.M.G.I. (Ophelia Mobile Game Integration).

A patched game runs a small server inside itself (see the O.M.G.I. repo);
through it she reads the game's real state and acts through the game's own
logic instead of reading pixels and tapping. Optional: needs the `omgi`
package (`pip install -e '.[games]'`) and a game patched with `omgi patch`.
On a PC with an ADB phone body the port is forwarded automatically; on Termux
the game is on the same device, so 127.0.0.1 just works.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import structlog

from ophelia.android import adb as adb_util

if TYPE_CHECKING:
    from ophelia.android.games import GameProfile
    from ophelia.android.shizuku import AndroidBody

log = structlog.get_logger()

INSTALL_HINT = (
    "The game link needs the omgi package: pip install "
    "\"omgi @ git+https://github.com/Shadow4-18/O.M.G.I..git\" (or pip install -e '.[games]')."
)


def omgi_installed() -> bool:
    try:
        import omgi  # noqa: F401
    except ImportError:
        return False
    return True


class GameLink:
    """One game's link. Each call opens a fresh connection, so a game that
    was closed or restarted between turns just works on the next call."""

    def __init__(self, profile: GameProfile, android: AndroidBody | None = None) -> None:
        from omgi import adapters

        adapter = adapters.get(profile.omgi_adapter or profile.id) or adapters.for_package(profile.package)
        self.profile = profile
        self.android = android
        self.adapter_id = adapter.id if adapter else profile.id
        self.host = profile.omgi_host or "127.0.0.1"
        self.port = int(profile.omgi_port or (adapter.default_port if adapter else 47800))
        self.token = profile.omgi_token
        self._forwarded = False

    async def _ensure_forward(self) -> None:
        """PC + ADB: map localhost:port to the phone's port (once)."""
        android = self.android
        if self._forwarded or not android or self.host not in ("127.0.0.1", "localhost"):
            return
        if getattr(android, "mode", "") not in ("adb", "adb_root") or not getattr(android, "_adb_path", None):
            return
        await android.ensure_ready()
        code, out, err = await adb_util.adb_run(
            adb_util.adb_base(android._adb_path, android.adb_device)
            + ["forward", f"tcp:{self.port}", f"tcp:{self.port}"],
            timeout=15.0,
        )
        if code == 0:
            self._forwarded = True
        else:
            log.warning("game_link.forward", port=self.port, error=err or out)

    def _client(self, timeout: float = 5.0):
        from omgi import Client

        return Client(self.host, self.port, token=self.token, timeout=timeout, client_name="ophelia")

    async def _run(self, fn, timeout: float = 5.0):
        await self._ensure_forward()

        def work():
            with self._client(timeout) as c:
                return fn(c)

        return await asyncio.to_thread(work)

    def _explain(self, e: Exception) -> str:
        code = getattr(e, "code", "")
        msg = getattr(e, "message", str(e))
        if code in ("unreachable", "disconnected", "timeout"):
            return (
                f"No game link on {self.host}:{self.port} ({msg}). Is the patched {self.profile.name} "
                "open on the phone? (The store version has no link.) Meanwhile play by sight: "
                "phone_game_look, then phone_tap."
            )
        if code == "busy":
            return "The game is still animating. Call phone_game_state again in a moment."
        if code == "unauthorized":
            return "The game link refused the token: set omgi_token in games.yaml to the one used when patching."
        return f"The game said no ({code}): {msg}. phone_game_state shows what's possible right now."

    # Calls ---------------------------------------------------------------

    async def up(self, timeout: float = 1.5) -> bool:
        try:
            await self._run(lambda c: c.ping(), timeout=timeout)
            return True
        except Exception:
            return False

    async def wait_up(self, seconds: float = 15.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if await self.up():
                return True
            await asyncio.sleep(1.0)
        return False

    async def state_text(self, *, wait: bool = True, timeout: float = 5.0) -> str:
        from omgi import render_state
        from omgi.render import format_actions

        def read(c):
            state = c.wait_stable(10.0) if wait else c.state()
            queries = []
            if "queries" in ((c.hello_info or {}).get("methods") or []):
                queries = c.queries()
            return state, queries

        try:
            state, queries = await self._run(read, timeout=timeout)
        except Exception as e:
            return self._explain(e)
        text = render_state(self.adapter_id, state)
        if queries and state.get("stable"):
            text += "\nQueries (phone_game_query, read-only):\n" + format_actions(queries)
        return text

    async def act(self, action: str, args: dict[str, Any] | None = None) -> str:
        from omgi import render_state

        try:
            result, state = await self._run(lambda c: c.act_and_wait(action, args or {}, timeout=20.0),
                                            timeout=8.0)
        except Exception as e:
            return self._explain(e)
        done = {k: v for k, v in (result or {}).items() if k != "ok"}
        head = f"Did {action}" + (f": {json.dumps(done, ensure_ascii=False)}" if done else ".")
        return head + "\n\n" + render_state(self.adapter_id, state)

    async def query(self, name: str, args: dict[str, Any] | None = None) -> str:
        try:
            result = await self._run(lambda c: c.query(name, args or {}))
        except Exception as e:
            return self._explain(e)
        return json.dumps(result, ensure_ascii=False)
