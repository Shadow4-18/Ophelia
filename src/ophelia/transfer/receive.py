"""Local receive server — phone uploads to PC on same Wi-Fi."""

from __future__ import annotations

import secrets
import socket
from collections.abc import Callable
from pathlib import Path

import structlog
from fastapi import FastAPI, File, Header, HTTPException, UploadFile

log = structlog.get_logger()


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def create_receive_app(
    *,
    token: str,
    on_received: Callable[[bytes, str], Path],
) -> FastAPI:
    app = FastAPI(title="Ophelia Transfer", docs_url=None, redoc_url=None)

    @app.get("/")
    async def index() -> dict:
        return {
            "service": "ophelia-transfer",
            "upload": "POST /upload with X-Ophelia-Token header",
        }

    @app.post("/upload")
    async def upload(
        file: UploadFile = File(...),
        x_ophelia_token: str | None = Header(default=None),
    ) -> dict:
        if x_ophelia_token != token:
            raise HTTPException(status_code=403, detail="invalid token")
        data = await file.read()
        if len(data) < 100:
            raise HTTPException(status_code=400, detail="file too small")
        dest = on_received(data, file.filename or "bundle.tar.gz")
        log.info("transfer.received", bytes=len(data), path=str(dest))
        return {"ok": True, "bytes": len(data), "path": str(dest)}

    return app


async def run_receive_server(
    *,
    host: str,
    port: int,
    token: str | None,
    dest_dir: Path,
    auto_import: bool,
) -> None:
    import uvicorn

    from ophelia.transfer.import_bundle import import_bundle

    dest_dir.mkdir(parents=True, exist_ok=True)
    tok = token or secrets.token_urlsafe(12)
    received: list[Path] = []

    def on_received(data: bytes, filename: str) -> Path:
        path = dest_dir / (filename or "ophelia-hermes-bundle.tar.gz")
        path.write_bytes(data)
        received.append(path)
        if auto_import:
            print("\nImporting...")
            print(import_bundle(path))
        return path

    app = create_receive_app(token=tok, on_received=on_received)
    ip = _lan_ip()
    url = f"http://{ip}:{port}"

    print("Ophelia transfer — waiting for upload")
    print(f"  URL:   {url}")
    print(f"  Token: {tok}")
    print()
    print("On phone (Termux, same Wi-Fi):")
    print(f"  ophelia transfer send {url} --token {tok}")
    print()
    print("Or via free cloud (any network):")
    print("  ophelia transfer cloud-upload")
    print("  ophelia transfer cloud-download <link>")
    print()
    print("Ctrl+C to stop after upload completes.")

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
