"""The MCP bridge against a real stdio server: tools discovered, one
connection kept open, env passed through, reconnect after a crash."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from ophelia.tools import mcp_bridge as mb

SERVER = str(Path(__file__).parent / "fixtures" / "mcp_echo_server.py")
needs_mcp = pytest.mark.skipif(not mb.mcp_available(), reason="mcp package not installed")


def _config(**extra):
    return {"servers": {"echo": {"command": sys.executable, "args": [SERVER], **extra}}}


@needs_mcp
@pytest.mark.asyncio
async def test_discovers_tools_and_keeps_one_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_SECRET", "hunter2")
    bridge = mb.MCPBridge(_config(env={"ECHO_SECRET": "${MY_SECRET}"}), artifacts_dir=tmp_path)
    try:
        await bridge.initialize()
        names = {d["function"]["name"] for d in bridge.definitions()}
        assert {"mcp_echo_echo", "mcp_echo_count", "mcp_echo_secret"} <= names
        echo = next(d for d in bridge.definitions() if d["function"]["name"] == "mcp_echo_echo")
        assert "text" in echo["function"]["parameters"]["properties"]
        assert echo["function"]["description"].startswith("[echo] ")
        assert await bridge.dispatch("mcp_echo_echo", {"text": "hi"}) == "echo: hi"
        first = await bridge.dispatch("mcp_echo_count", {})
        second = await bridge.dispatch("mcp_echo_count", {})
        assert first.split()[1] == second.split()[1]  # same process
        assert first.endswith("call 1") and second.endswith("call 2")
        assert await bridge.dispatch("mcp_echo_secret", {}) == "hunter2"
        assert await bridge.dispatch("not_mcp", {}) is None
    finally:
        await bridge.close()


@needs_mcp
@pytest.mark.asyncio
async def test_reconnects_after_the_server_dies(tmp_path):
    bridge = mb.MCPBridge(_config(), artifacts_dir=tmp_path)
    try:
        await bridge.initialize()
        before = await bridge.dispatch("mcp_echo_count", {})
        out = await bridge.dispatch("mcp_echo_die", {})
        assert "failed" in out or "error" in out
        after = await bridge.dispatch("mcp_echo_count", {})
        assert after.startswith("pid ") and after.split()[1] != before.split()[1]
    finally:
        await bridge.close()


@needs_mcp
@pytest.mark.asyncio
async def test_tool_filter_and_a_broken_server_dont_take_down_the_rest(tmp_path):
    config = _config(tools=["echo"])
    config["servers"]["broken"] = {"command": sys.executable, "args": ["-c", "pass"]}
    bridge = mb.MCPBridge(config, artifacts_dir=tmp_path)
    try:
        await bridge.initialize()
        names = {d["function"]["name"] for d in bridge.definitions()}
        assert names == {"mcp_echo_echo"}
        status = {r["server"]: r for r in bridge.status()}
        assert status["echo"]["connected"] and not status["broken"]["connected"]
        assert status["broken"]["error"]
    finally:
        await bridge.close()


@needs_mcp
@pytest.mark.asyncio
async def test_editing_mcp_json_is_picked_up(tmp_path):
    import os

    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": {}}))
    bridge = mb.MCPBridge(config_path=path, artifacts_dir=tmp_path)
    try:
        await bridge.initialize()
        assert bridge.definitions() == []
        path.write_text(json.dumps(_config()))
        st = path.stat()
        os.utime(path, (st.st_atime, st.st_mtime + 5))
        await bridge.refresh()
        assert "mcp_echo_echo" in {d["function"]["name"] for d in bridge.definitions()}
    finally:
        await bridge.close()


def test_names_are_safe_for_every_api():
    assert mb.tool_name("my server", "get.thing") == "mcp_my_server_get_thing"
    long = mb.tool_name("s" * 40, "t" * 40)
    assert len(long) <= 64 and long.startswith("mcp_")
    assert mb.servers_from({"mcpServers": {"a": {"command": "x"}}}) == {"a": {"command": "x"}}


def test_result_formatting(tmp_path):
    from types import SimpleNamespace as NS
    import base64

    bridge = mb.MCPBridge({}, artifacts_dir=tmp_path)
    img = NS(type="image", data=base64.b64encode(b"png").decode(), mimeType="image/png")
    out = bridge._format("s", "t", NS(content=[NS(type="text", text="hello"), img],
                                       is_error=False))
    assert out.startswith("hello\nImage saved to ") and "send_file" in out
    err = bridge._format("s", "t", NS(content=[NS(type="text", text="nope")], isError=True))
    assert err == "MCP s/t error: nope"
    structured = bridge._format("s", "t", NS(content=[], structured_content={"a": 1}))
    assert '"a": 1' in structured


@pytest.mark.asyncio
async def test_guests_never_get_mcp_tools(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg._mcp_ready = True
    reg.mcp = MagicMock()
    reg.mcp.refresh = AsyncMock()
    reg.mcp.dispatch = AsyncMock(return_value="ran")
    reg._handlers = {}
    for owner, friend in ((False, False), (False, True)):
        reg.set_owner(owner)
        reg.set_friend(friend)
        assert "aren't available" in await reg.dispatch("mcp_echo_echo", "{}")
    reg.mcp.dispatch.assert_not_awaited()


def test_vars_come_from_her_env_file_too(tmp_path, monkeypatch):
    """Ophelia's settings read ~/.ophelia/.env but don't export keys they
    don't know, so ${MUSIC_REMOTE_TOKEN} has to be read from the file."""
    monkeypatch.delenv("MUSIC_REMOTE_TOKEN", raising=False)
    monkeypatch.setenv("FROM_ENV", "env wins")
    (tmp_path / ".env").write_text(
        "# comment\nMUSIC_REMOTE_TOKEN=abc123\nexport QUOTED=\"two words\"\nFROM_ENV=file\n")
    env = mb.env_lookup(tmp_path / "mcp.json")
    assert env["MUSIC_REMOTE_TOKEN"] == "abc123"
    assert env["QUOTED"] == "two words"
    assert env["FROM_ENV"] == "env wins"
    missing: set[str] = set()
    spec = mb._expand({"headers": {"Authorization": "Bearer ${MUSIC_REMOTE_TOKEN}"},
                       "url": "http://x/${NOPE}"}, env, missing)
    assert spec["headers"]["Authorization"] == "Bearer abc123"
    assert missing == {"NOPE"}


@pytest.mark.asyncio
async def test_a_missing_var_is_named_in_the_status(tmp_path, monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"servers": {"music": {
        "url": "http://127.0.0.1:9/mcp",
        "headers": {"Authorization": "Bearer ${NOT_SET_ANYWHERE}"}}}}))
    bridge = mb.MCPBridge(config_path=cfg, artifacts_dir=tmp_path)
    try:
        await bridge.initialize()
        err = bridge.status()[0]["error"]
        assert "NOT_SET_ANYWHERE" in err and ".env" in err
    finally:
        await bridge.close()


@needs_mcp
@pytest.mark.asyncio
async def test_a_server_that_was_down_is_retried(tmp_path, monkeypatch):
    """Down at startup (Tailscale not up yet, phone off): logged, and tried
    again later instead of never."""
    monkeypatch.setattr(mb, "RETRY_EVERY_S", 0.0)
    bridge = mb.MCPBridge({"servers": {"echo": {"command": sys.executable,
                                                 "args": ["-c", "import sys; sys.exit(1)"]}}},
                          artifacts_dir=tmp_path)
    try:
        await bridge.initialize()
        assert bridge.definitions() == []
        assert bridge.status()[0]["error"]
        # It comes back: point it at the real server and retry.
        bridge.servers["echo"].spec = {"command": sys.executable, "args": [SERVER]}
        await bridge.retry_down()
        for _ in range(100):
            if bridge.definitions():
                break
            await asyncio.sleep(0.1)
        assert "mcp_echo_echo" in {d["function"]["name"] for d in bridge.definitions()}
    finally:
        await bridge.close()


@needs_mcp
@pytest.mark.asyncio
async def test_a_slow_server_doesnt_hold_up_her_turn(tmp_path, monkeypatch):
    import time as _time

    monkeypatch.setattr(mb, "FIRST_WAIT_S", 0.3)
    slow = ("import time, runpy, sys; time.sleep(2); sys.argv=['s']; "
            f"runpy.run_path({SERVER!r}, run_name='__main__')")
    bridge = mb.MCPBridge({"servers": {"echo": {"command": sys.executable, "args": ["-c", slow]}}},
                          artifacts_dir=tmp_path)
    try:
        t0 = _time.monotonic()
        await bridge.initialize()
        assert _time.monotonic() - t0 < 1.5
        assert bridge.definitions() == []
        for _ in range(100):
            if bridge.definitions():
                break
            await asyncio.sleep(0.1)
        assert "mcp_echo_echo" in {d["function"]["name"] for d in bridge.definitions()}
    finally:
        await bridge.close()
