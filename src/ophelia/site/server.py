"""Public FastAPI reader for Ophelia's site (www/ files + optional wiki chrome)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from ophelia.config import Settings
from ophelia.site.store import SiteStore
from ophelia.site.templates import (
    render_home,
    render_list,
    render_not_found,
    render_page,
)

log = structlog.get_logger()

STATIC_DIR = Path(__file__).resolve().parent / "static"

_WWW_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}


def _file_response(path: Path) -> Response:
    media = _WWW_TYPES.get(path.suffix.lower())
    if media and media.startswith(("text/", "application/json", "application/xml", "image/svg")):
        return Response(content=path.read_bytes(), media_type=media)
    return FileResponse(path, media_type=media)


def create_site_app(store: SiteStore, settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Ophelia Site", docs_url=None, redoc_url=None)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        st = await store.status()
        return {"ok": True, "counts": st.get("counts", {}), "www": st.get("www", {})}

    @app.get("/", response_class=HTMLResponse)
    async def home() -> Response:
        # 1) Freeform www/index.html always wins
        custom = store.www_file_for_url("")
        if custom:
            return _file_response(custom)
        meta = await store.get_meta()
        extras = store.www_extras()
        # 2) Optional: make a published page the landing (e.g. home_slug=about)
        home_slug = (meta.get("home_slug") or "").strip().lower()
        if home_slug:
            row = await store.get_page(home_slug, published_only=True)
            if row:
                return HTMLResponse(
                    render_page(
                        meta,
                        row,
                        store.page_body_html(row),
                        extras=extras,
                        raw_html=(row.get("body_format") or "") == "html",
                    )
                )
        # 3) Default wiki listing home
        pages = await store.list_pages(published_only=True, limit=24)
        return HTMLResponse(render_home(meta, pages, extras=extras))

    @app.get("/wiki", response_class=HTMLResponse)
    async def wiki_index() -> Response:
        custom = store.www_file_for_url("wiki") or store.www_file_for_url("wiki.html")
        if custom:
            return _file_response(custom)
        meta = await store.get_meta()
        pages = await store.list_pages(kind="wiki", published_only=True, limit=200)
        return HTMLResponse(
            render_list(meta, pages, heading="Wiki", extras=store.www_extras())
        )

    @app.get("/blog", response_class=HTMLResponse)
    async def blog_index() -> Response:
        custom = store.www_file_for_url("blog") or store.www_file_for_url("blog.html")
        if custom:
            return _file_response(custom)
        meta = await store.get_meta()
        pages = await store.list_pages(kind="blog", published_only=True, limit=200)
        return HTMLResponse(
            render_list(meta, pages, heading="Blog", extras=store.www_extras())
        )

    @app.get("/tag/{tag}", response_class=HTMLResponse)
    async def tag_index(tag: str) -> HTMLResponse:
        meta = await store.get_meta()
        pages = await store.list_pages(published_only=True, tag=tag, limit=200)
        return HTMLResponse(
            render_list(meta, pages, heading=f"Tag: {tag}", extras=store.www_extras())
        )

    @app.get("/p/{slug}", response_class=HTMLResponse)
    async def page(slug: str) -> Response:
        custom = store.www_file_for_url(f"p/{slug}") or store.www_file_for_url(
            f"p/{slug}.html"
        )
        if custom:
            return _file_response(custom)
        meta = await store.get_meta()
        row = await store.get_page(slug, published_only=True)
        if not row:
            return HTMLResponse(
                render_not_found(meta, extras=store.www_extras()), status_code=404
            )
        body_html = store.page_body_html(row)
        return HTMLResponse(
            render_page(
                meta,
                row,
                body_html,
                extras=store.www_extras(),
                raw_html=(row.get("body_format") or "") == "html",
            )
        )

    @app.get("/assets/{filename}")
    async def asset(filename: str) -> FileResponse:
        safe = Path(filename).name
        path = (store.assets_dir / safe).resolve()
        if not path.is_relative_to(store.assets_dir.resolve()) or not path.is_file():
            raise HTTPException(404, "asset not found")
        return FileResponse(path)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/{path:path}")
    async def www_catch_all(path: str) -> Response:
        """Serve freeform files from ~/.ophelia/site/www/ (her full HTML/CSS/JS)."""
        if path.startswith(("api/", "static/", "assets/")):
            raise HTTPException(404, "not found")
        found = store.www_file_for_url(path)
        if not found:
            raise HTTPException(404, "not found")
        return _file_response(found)

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
    log.info("site.public", url=url, root=str(root), www=str(store.www_dir))
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
