"""Public FastAPI reader for Ophelia's wiki/blog (write path is tools-only)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from ophelia.config import Settings
from ophelia.site.render import markdown_to_html
from ophelia.site.store import SiteStore
from ophelia.site.templates import (
    render_home,
    render_list,
    render_not_found,
    render_page,
)

log = structlog.get_logger()

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_site_app(store: SiteStore, settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Ophelia Site", docs_url=None, redoc_url=None)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        st = await store.status()
        return {"ok": True, "counts": st.get("counts", {})}

    @app.get("/", response_class=HTMLResponse)
    async def home() -> HTMLResponse:
        meta = await store.get_meta()
        pages = await store.list_pages(published_only=True, limit=24)
        return HTMLResponse(render_home(meta, pages))

    @app.get("/wiki", response_class=HTMLResponse)
    async def wiki_index() -> HTMLResponse:
        meta = await store.get_meta()
        pages = await store.list_pages(kind="wiki", published_only=True, limit=200)
        return HTMLResponse(render_list(meta, pages, heading="Wiki"))

    @app.get("/blog", response_class=HTMLResponse)
    async def blog_index() -> HTMLResponse:
        meta = await store.get_meta()
        pages = await store.list_pages(kind="blog", published_only=True, limit=200)
        return HTMLResponse(render_list(meta, pages, heading="Blog"))

    @app.get("/tag/{tag}", response_class=HTMLResponse)
    async def tag_index(tag: str) -> HTMLResponse:
        meta = await store.get_meta()
        pages = await store.list_pages(published_only=True, tag=tag, limit=200)
        return HTMLResponse(render_list(meta, pages, heading=f"Tag: {tag}"))

    @app.get("/p/{slug}", response_class=HTMLResponse)
    async def page(slug: str) -> HTMLResponse:
        meta = await store.get_meta()
        row = await store.get_page(slug, published_only=True)
        if not row:
            return HTMLResponse(render_not_found(meta), status_code=404)
        body_html = markdown_to_html(row.get("body_md") or "")
        return HTMLResponse(render_page(meta, row, body_html))

    @app.get("/assets/{filename}")
    async def asset(filename: str) -> FileResponse:
        # Prevent path traversal
        safe = Path(filename).name
        path = (store.assets_dir / safe).resolve()
        if not str(path).startswith(str(store.assets_dir.resolve())) or not path.is_file():
            raise HTTPException(404, "asset not found")
        return FileResponse(path)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


async def run_site(settings: Settings) -> None:
    import uvicorn

    root = settings.site_dir
    store = SiteStore(root)
    await store.init()
    app = create_site_app(store, settings)
    host = settings.site_host
    port = settings.site_port
    url = f"http://{host}:{port}/"
    log.info("site.public", url=url, root=str(root))
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
