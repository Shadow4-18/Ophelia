"""FastAPI server for the Ophelia workstation UI."""

from __future__ import annotations

import mimetypes
import webbrowser
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ophelia.config import Settings
from ophelia.ui.workstation import Workstation

log = structlog.get_logger()

STATIC_DIR = Path(__file__).resolve().parent / "static"

# VRoid / VRM exports are glTF binary containers; VRChat exports often .glb
mimetypes.add_type("model/gltf-binary", ".vrm")
mimetypes.add_type("model/gltf-binary", ".glb")
mimetypes.add_type("model/gltf+json", ".gltf")


class ChatRequest(BaseModel):
    message: str


class CompareRequest(BaseModel):
    message: str
    models: list[str] = []


def create_app(workstation: Workstation) -> FastAPI:
    app = FastAPI(title="Ophelia Workstation", docs_url=None, redoc_url=None)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return workstation.status_dict()

    @app.get("/api/avatar")
    async def avatar() -> dict[str, Any]:
        return workstation.avatar_dict()

    @app.get("/api/history")
    async def history() -> list[dict]:
        return await workstation.history()

    @app.get("/api/inner")
    async def inner() -> dict[str, str]:
        return {"text": workstation.inner_full_tail(100)}

    @app.get("/api/models")
    async def models_info() -> dict[str, Any]:
        return await workstation.models_info()

    @app.post("/api/compare")
    async def compare(body: CompareRequest) -> dict[str, Any]:
        return await workstation.compare_models(body.message, body.models)

    @app.post("/api/chat")
    async def chat(body: ChatRequest) -> dict[str, str]:
        reply = await workstation.chat(body.message)
        return {"reply": reply}

    @app.post("/api/consciousness/pause")
    async def pause_consciousness() -> dict[str, bool]:
        workstation.signals.autonomy_paused = True
        return {"paused": True}

    @app.post("/api/consciousness/resume")
    async def resume_consciousness() -> dict[str, bool]:
        workstation.signals.autonomy_paused = False
        return {"paused": False}

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        await workstation.bus.connect(ws)
        try:
            await ws.send_json({"type": "status", "data": workstation.status_dict()})
            if workstation.settings.avatar_enabled:
                await ws.send_json({"type": "avatar", "data": workstation.avatar_dict()})
            hist = await workstation.history()
            for row in hist:
                await ws.send_json(
                    {"type": "chat", "role": row["role"], "text": row["content"]}
                )
            await ws.send_json(
                {"type": "inner_block", "text": workstation.inner_full_tail(80)}
            )
            while True:
                raw = await ws.receive_text()
                if raw.strip().lower() in ("ping", '{"type":"ping"}'):
                    await ws.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        finally:
            await workstation.bus.disconnect(ws)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    avatar_dir = workstation.settings.avatar_dir
    if avatar_dir.is_dir():
        app.mount(
            "/avatar",
            StaticFiles(directory=str(avatar_dir)),
            name="avatar",
        )

    return app


async def run_ui(settings: Settings, *, open_browser: bool | None = None) -> None:
    import uvicorn

    ws = Workstation(settings)
    await ws.init()
    app = create_app(ws)
    host = settings.ui_host
    port = settings.ui_port
    url = f"http://{host}:{port}/"
    should_open = (
        settings.ui_open_browser if open_browser is None else open_browser
    )
    if should_open and host in ("127.0.0.1", "localhost"):
        webbrowser.open(url)

    log.info("workstation.ui", url=url)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await ws.shutdown()
