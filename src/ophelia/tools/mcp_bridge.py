"""MCP tool bridge: plug outside tools into her from ~/.ophelia/mcp.json.

MCP (Model Context Protocol) servers are small programs (or hosted
services) that expose tools: Spotify, a calendar, GitHub, smart home, file
search... Each configured server is connected once and kept open; its tools
are discovered from the server itself and given to her as
``mcp_<server>_<tool>``. A server that drops is reconnected on the next
call. Owner-only: guests and friends never see or call MCP tools.

Config (the Claude Desktop "mcpServers" layout works as is)::

    {
      "servers": {
        "spotify": {"command": "npx", "args": ["-y", "some-spotify-mcp"],
                    "env": {"SPOTIFY_TOKEN": "${SPOTIFY_TOKEN}"}},
        "notes":   {"url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer ${NOTES_KEY}"}},
        "old":     {"url": "https://example.com/sse", "transport": "sse"}
      }
    }

Per server, optional: "enabled": false, "tools": ["only", "these"],
"timeout": seconds per call (default 120), "cwd" (local servers).
``${VAR}`` in env/headers/args/url is filled from the environment (so keys
can stay in ~/.ophelia/.env). Editing mcp.json takes effect on her next turn.

Works with the ``mcp`` package 1.x and 2.x (``pip install -e ".[mcp]"``).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import structlog

from ophelia.config import OPHELIA_HOME

log = structlog.get_logger()

CONNECT_TIMEOUT_S = 30.0
CALL_TIMEOUT_S = 120.0
MAX_RESULT_CHARS = 12000
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def load_mcp_config(path: Path | None = None) -> dict:
    p = path or OPHELIA_HOME / "mcp.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("mcp.config_unreadable", path=str(p), error=str(e))
        return {}


def mcp_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("mcp") is not None


def _expand(value: Any) -> Any:
    """${VAR} → the environment value (missing → empty)."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def servers_from(config: dict) -> dict[str, dict]:
    raw = config.get("servers") or config.get("mcpServers") or {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def tool_name(server_id: str, tool: str) -> str:
    """A function name every chat API accepts: [A-Za-z0-9_-], max 64."""
    name = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp_{server_id}_{tool}")
    if len(name) > 64:
        name = name[:55] + "_" + hashlib.sha1(name.encode()).hexdigest()[:8]
    return name


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    """Read a field under its 2.x (snake) or 1.x (camel) name."""
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


@contextlib.asynccontextmanager
async def open_transport(spec: dict):
    """(read, write) streams for a server spec: a local program over stdio,
    or a hosted server over streamable HTTP (default) or SSE."""
    url = spec.get("url")
    if not url:
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = spec.get("env")
        if env:
            try:
                from mcp.client.stdio import get_default_environment

                env = {**get_default_environment(), **env}
            except ImportError:
                env = {**os.environ, **env}
        kw: dict[str, Any] = {"command": spec["command"], "args": list(spec.get("args") or []),
                              "env": env}
        if spec.get("cwd"):
            kw["cwd"] = spec["cwd"]
        async with stdio_client(StdioServerParameters(**kw)) as streams:
            yield streams[0], streams[1]
        return
    headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
    if str(spec.get("transport") or "").lower() == "sse":
        from mcp.client.sse import sse_client

        async with sse_client(url, headers=headers or None) as streams:
            yield streams[0], streams[1]
        return
    from mcp.client import streamable_http as sh

    if hasattr(sh, "streamable_http_client"):  # mcp 2.x
        client = sh.create_mcp_http_client(headers=headers or None)
        async with client:
            async with sh.streamable_http_client(url, http_client=client) as streams:
                yield streams[0], streams[1]
    else:  # mcp 1.x
        async with sh.streamablehttp_client(url, headers=headers or None) as streams:
            yield streams[0], streams[1]


class MCPServer:
    """One configured server, connected in its own task and kept open."""

    def __init__(self, server_id: str, spec: dict) -> None:
        self.id = server_id
        self.spec = spec
        self.session: Any = None
        self.tools: list[Any] = []
        self.error = ""
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self.session is not None and self._task is not None and not self._task.done()

    @property
    def call_timeout(self) -> float:
        try:
            return float(self.spec.get("timeout") or CALL_TIMEOUT_S)
        except (TypeError, ValueError):
            return CALL_TIMEOUT_S

    async def start(self) -> bool:
        """Connect and list tools. False (with .error) when it can't."""
        await self.stop()
        self._stop, self._ready = asyncio.Event(), asyncio.Event()
        self.error = ""
        self._task = asyncio.create_task(self._run(), name=f"mcp:{self.id}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=CONNECT_TIMEOUT_S)
        except asyncio.TimeoutError:
            self.error = f"didn't connect within {CONNECT_TIMEOUT_S:.0f}s"
            await self.stop()
            return False
        if self.session is None:
            await self.stop()
            return False
        return True

    async def _run(self) -> None:
        # Opened and closed in this one task: the transports' task groups
        # must exit in the task that entered them.
        from mcp import ClientSession

        try:
            async with open_transport(self.spec) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self.tools = await self._list_tools(session)
                    self.session = session
                    self._ready.set()
                    log.info("mcp.connected", server=self.id, tools=len(self.tools))
                    await self._stop.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - ExceptionGroup from task groups
            self.error = _describe(e)
            log.warning("mcp.server_failed", server=self.id, error=self.error)
        finally:
            self.session = None
            self._ready.set()

    async def _list_tools(self, session: Any) -> list[Any]:
        tools: list[Any] = []
        cursor = None
        for _ in range(20):  # pages
            result = await (session.list_tools(cursor=cursor) if cursor else session.list_tools())
            tools.extend(result.tools)
            cursor = _field(result, "next_cursor", "nextCursor")
            if not cursor:
                break
        only = self.spec.get("tools")
        if only:  # a list of names (or the old static {"name": ...} entries)
            keep = {t.get("name") if isinstance(t, dict) else str(t) for t in only}
            tools = [t for t in tools if t.name in keep]
        return tools

    async def call(self, name: str, args: dict) -> Any:
        async with self._lock:
            if not self.connected and not await self.start():
                raise RuntimeError(self.error or "not connected")
        try:
            return await asyncio.wait_for(self.session.call_tool(name, args),
                                          timeout=self.call_timeout)
        except asyncio.TimeoutError:
            raise
        except Exception as e:
            # The server died under us (crash, restart, dropped link):
            # reconnect once and retry. A real tool error is passed on.
            if self.connected and not _is_disconnect(e):
                raise
            log.info("mcp.reconnecting", server=self.id, error=_describe(e))
            async with self._lock:
                if not await self.start():
                    raise RuntimeError(self.error or "reconnect failed")
            return await asyncio.wait_for(self.session.call_tool(name, args),
                                          timeout=self.call_timeout)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.TimeoutError, Exception):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        self.session = None


_DISCONNECT_TYPES = {"ClosedResourceError", "BrokenResourceError", "EndOfStream",
                     "BrokenPipeError", "ConnectionResetError", "ConnectionError",
                     "RemoteProtocolError", "ReadError", "WriteError"}


def _is_disconnect(e: BaseException) -> bool:
    inner = getattr(e, "exceptions", None)
    if inner:
        return any(_is_disconnect(x) for x in inner)
    if type(e).__name__ in _DISCONNECT_TYPES:
        return True
    text = str(e).lower()
    return "connection closed" in text or "closed resource" in text


def _describe(e: BaseException) -> str:
    inner = getattr(e, "exceptions", None)
    if inner:
        return "; ".join(_describe(x) for x in inner[:3])
    return f"{type(e).__name__}: {e}"[:300]


class MCPBridge:
    """Every configured server, its tools as ``mcp_<server>_<tool>``."""

    def __init__(self, config: dict | None = None, *, config_path: Path | None = None,
                 artifacts_dir: Path | None = None) -> None:
        self.config_path = config_path
        self.config = config if config is not None else load_mcp_config(config_path)
        self.artifacts_dir = artifacts_dir
        self.servers: dict[str, MCPServer] = {}
        self._routes: dict[str, tuple[MCPServer, str]] = {}
        self._definitions: list[dict[str, Any]] = []
        self._initialized = False
        self._mtime = self._config_mtime()
        self._init_lock = asyncio.Lock()

    def _config_mtime(self) -> float | None:
        try:
            return self.config_path.stat().st_mtime if self.config_path else None
        except OSError:
            return None

    async def initialize(self) -> None:
        async with self._init_lock:
            if self._initialized:
                return
            self._initialized = True
            specs = {sid: _expand(spec) for sid, spec in servers_from(self.config).items()
                     if spec.get("enabled", True) and (spec.get("command") or spec.get("url"))}
            if not specs:
                return
            if not mcp_available():
                log.warning("mcp.missing", servers=len(specs),
                            hint='pip install -e ".[mcp]" for MCP server support')
                return
            self.servers = {sid: MCPServer(sid, spec) for sid, spec in specs.items()}
            await asyncio.gather(*(s.start() for s in self.servers.values()))
            self._rebuild()

    def _rebuild(self) -> None:
        self._routes.clear()
        self._definitions = []
        for server in self.servers.values():
            for tool in server.tools:
                full = tool_name(server.id, tool.name)
                if full in self._routes:
                    continue
                self._routes[full] = (server, tool.name)
                schema = _field(tool, "input_schema", "inputSchema") or {}
                if not isinstance(schema, dict):
                    schema = dict(schema)
                schema = {"type": "object", "properties": {}, **schema}
                desc = (_field(tool, "description") or _field(tool, "title") or tool.name)
                self._definitions.append({
                    "type": "function",
                    "function": {
                        "name": full,
                        "description": f"[{server.id}] {str(desc)[:900]}",
                        "parameters": schema,
                    },
                })

    async def refresh(self) -> None:
        """Pick up an edited mcp.json without a restart."""
        mtime = self._config_mtime()
        if mtime == self._mtime:
            return
        self._mtime = mtime
        log.info("mcp.config_changed", path=str(self.config_path))
        await self.close()
        self.config = load_mcp_config(self.config_path)
        self._initialized = False
        await self.initialize()

    def definitions(self) -> list[dict[str, Any]]:
        return list(self._definitions)

    def handles(self, name: str) -> bool:
        return name in self._routes

    async def dispatch(self, name: str, args: dict) -> str | None:
        route = self._routes.get(name)
        if route is None:
            return None
        server, tool = route
        started = time.monotonic()
        try:
            result = await server.call(tool, args or {})
        except asyncio.TimeoutError:
            return f"MCP {server.id}/{tool} timed out after {server.call_timeout:.0f}s."
        except Exception as e:
            log.warning("mcp.call_failed", server=server.id, tool=tool, error=_describe(e))
            return f"MCP {server.id}/{tool} failed: {_describe(e)}"
        log.info("mcp.call", server=server.id, tool=tool,
                 ms=int((time.monotonic() - started) * 1000))
        return self._format(server.id, tool, result)

    def _format(self, server_id: str, tool: str, result: Any) -> str:
        parts: list[str] = []
        for block in _field(result, "content", default=[]) or []:
            kind = getattr(block, "type", "")
            if getattr(block, "text", None):
                parts.append(block.text)
            elif kind in ("image", "audio") and getattr(block, "data", None):
                parts.append(self._save_media(server_id, block))
            elif kind == "resource":
                res = getattr(block, "resource", None)
                text = getattr(res, "text", None)
                parts.append(text if text else f"(resource {getattr(res, 'uri', '')})")
            elif kind == "resource_link":
                parts.append(f"(link: {getattr(block, 'uri', '')})")
        structured = _field(result, "structured_content", "structuredContent")
        if structured and not parts:
            parts.append(json.dumps(structured, ensure_ascii=False, indent=1)[:MAX_RESULT_CHARS])
        text = "\n".join(p for p in parts if p).strip() or "(no output)"
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + f"\n…(cut, {len(text)} chars total)"
        if _field(result, "is_error", "isError"):
            return f"MCP {server_id}/{tool} error: {text}"
        return text

    def _save_media(self, server_id: str, block: Any) -> str:
        if self.artifacts_dir is None:
            return f"({block.type} returned, not saved)"
        mime = str(getattr(block, "mimeType", None) or getattr(block, "mime_type", "") or "")
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
               "image/gif": ".gif", "audio/mpeg": ".mp3", "audio/wav": ".wav",
               "audio/ogg": ".ogg"}.get(mime, ".bin")
        try:
            data = base64.b64decode(block.data)
            out = Path(self.artifacts_dir) / "mcp" / (
                f"{server_id}_{hashlib.sha1(data).hexdigest()[:12]}{ext}")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
        except Exception as e:
            return f"({block.type} returned but couldn't be saved: {e})"
        return f"{block.type.capitalize()} saved to {out} (send it with send_file)"

    def status(self) -> list[dict[str, Any]]:
        """Each configured server: connected?, its tools, or why not."""
        out = []
        for sid, spec in servers_from(self.config).items():
            server = self.servers.get(sid)
            enabled = spec.get("enabled", True)
            out.append({
                "server": sid,
                "kind": "hosted" if spec.get("url") else "local",
                "enabled": enabled,
                "connected": bool(server and server.connected),
                "tools": [tool_name(sid, t.name) for t in (server.tools if server else [])],
                "error": (server.error if server else
                          ("" if not enabled else
                           "mcp package not installed" if not mcp_available() else
                           "not started")),
            })
        return out

    async def close(self) -> None:
        await asyncio.gather(*(s.stop() for s in self.servers.values()),
                             return_exceptions=True)
        self.servers = {}
        self._rebuild()
