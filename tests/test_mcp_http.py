"""Hosted MCP servers over plain HTTP: no mcp package involved."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ophelia.tools import mcp_bridge as mb


class _Handler(BaseHTTPRequestHandler):
    sse = False
    token = "tok"
    sessions: set[str] = set()
    calls: list[str] = []

    def log_message(self, *a):  # quiet
        pass

    def _send(self, status, body=None, headers=None):
        data = b"" if body is None else body.encode()
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_DELETE(self):
        self._send(200)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            return self._send(401, "{}", {"Content-Type": "application/json"})
        method = body.get("method")
        sid = self.headers.get("Mcp-Session-Id")
        if method != "initialize" and sid not in self.sessions:
            return self._send(404, "no session")
        if "id" not in body:
            return self._send(202)
        headers = {}
        if method == "initialize":
            result = {"protocolVersion": body["params"]["protocolVersion"],
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "t", "version": "1"}}
            sid = f"s{len(self.sessions)}"
            self.sessions.add(sid)
            headers["Mcp-Session-Id"] = sid
        elif method == "tools/list":
            result = {"tools": [{"name": "now_playing", "description": "What's playing",
                                 "inputSchema": {"type": "object", "properties": {}}},
                                {"name": "play_song", "description": "Play",
                                 "inputSchema": {"type": "object",
                                                 "properties": {"song": {"type": "string"}}}}]}
        elif method == "tools/call":
            name = body["params"]["name"]
            type(self).calls.append(name)
            if name == "play_song" and not body["params"]["arguments"].get("song"):
                result = {"content": [{"type": "text", "text": "Need a song"}], "isError": True}
            else:
                result = {"content": [{"type": "text", "text": f"ok {name}"}], "isError": False}
        else:
            reply = {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "nope"}}
            return self._send(200, json.dumps(reply), {"Content-Type": "application/json"})
        reply = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if self.sse:
            headers["Content-Type"] = "text/event-stream"
            note = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
            text = f"event: message\ndata: {json.dumps(note)}\n\ndata: {json.dumps(reply)}\n\n"
        else:
            headers["Content-Type"] = "application/json"
            text = json.dumps(reply)
        self._send(200, text, headers)


@pytest.fixture(params=[False, True], ids=["json", "sse"])
def server(request):
    handler = type("H", (_Handler,), {"sse": request.param, "sessions": set(), "calls": []})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd, handler
    httpd.shutdown()


def _bridge(httpd, token="tok", tmp_path=None):
    url = f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
    return mb.MCPBridge({"servers": {"music": {"url": url,
                                               "headers": {"Authorization": f"Bearer {token}"}}}},
                        artifacts_dir=tmp_path)


@pytest.mark.asyncio
async def test_hosted_server_without_the_mcp_package(server, tmp_path, monkeypatch):
    httpd, handler = server
    monkeypatch.setattr(mb, "mcp_available", lambda: False)
    bridge = _bridge(httpd, tmp_path=tmp_path)
    try:
        await bridge.initialize()
        assert isinstance(bridge.servers["music"], mb.HttpMCPServer)
        names = {d["function"]["name"] for d in bridge.definitions()}
        assert names == {"mcp_music_now_playing", "mcp_music_play_song"}
        assert await bridge.dispatch("mcp_music_now_playing", {}) == "ok now_playing"
        assert await bridge.dispatch("mcp_music_play_song", {}) == "MCP music/play_song error: Need a song"
        # The server forgets the session (restart): reconnect and retry once.
        handler.sessions.clear()
        assert await bridge.dispatch("mcp_music_now_playing", {}) == "ok now_playing"
        assert bridge.status()[0]["connected"]
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_wrong_token_says_so(server, tmp_path):
    httpd, _ = server
    bridge = _bridge(httpd, token="wrong", tmp_path=tmp_path)
    try:
        await bridge.initialize()
        err = bridge.status()[0]["error"]
        assert "token" in err and not bridge.status()[0]["connected"]
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_unreachable_says_so(tmp_path):
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    bridge = mb.MCPBridge({"servers": {"music": {"url": f"http://127.0.0.1:{port}/mcp"}}},
                          artifacts_dir=tmp_path)
    try:
        await bridge.initialize()
        assert "couldn't connect" in bridge.status()[0]["error"]
    finally:
        await bridge.close()
