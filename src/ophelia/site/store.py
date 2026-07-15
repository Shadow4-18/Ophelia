"""SQLite-backed content store for Ophelia's public site."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_KINDS = frozenset({"wiki", "blog", "page"})
_FORMATS = frozenset({"markdown", "html"})
_MAX_WWW_FILE_BYTES = 1_500_000
_MAX_WWW_LIST = 500


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(title: str) -> str:
    s = (title or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "untitled"


def validate_slug(slug: str) -> str:
    s = (slug or "").strip().lower()
    if not _SLUG_RE.match(s):
        raise ValueError(
            "slug must be lowercase letters, numbers, and hyphens "
            "(e.g. 'origin-myth' or 'entry-01')"
        )
    if len(s) > 120:
        raise ValueError("slug too long (max 120)")
    return s


class SiteStore:
    """Ophelia's public site: SQLite pages + freeform www/ (HTML/CSS/JS)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.db_path = self.root / "site.db"
        self.assets_dir = self.root / "assets"
        self.www_dir = self.root / "www"
        self.export_dir = self.root / "export"

    async def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.www_dir.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'wiki',
                    summary TEXT NOT NULL DEFAULT '',
                    body_md TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '',
                    published INTEGER NOT NULL DEFAULT 0,
                    featured INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    published_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_pages_kind ON pages(kind);
                CREATE INDEX IF NOT EXISTS idx_pages_published ON pages(published);
                CREATE TABLE IF NOT EXISTS assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL UNIQUE,
                    original_name TEXT NOT NULL DEFAULT '',
                    mime TEXT NOT NULL DEFAULT '',
                    source_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                """
            )
            cur = await db.execute("PRAGMA table_info(pages)")
            cols = {row[1] for row in await cur.fetchall()}
            if "body_format" not in cols:
                await db.execute(
                    "ALTER TABLE pages ADD COLUMN body_format TEXT NOT NULL DEFAULT 'markdown'"
                )
            await db.commit()
            # Seed defaults once
            cur = await db.execute("SELECT COUNT(*) FROM meta")
            row = await cur.fetchone()
            if row and row[0] == 0:
                now = utc_now()
                defaults = {
                    "site_title": "Ophelia",
                    "tagline": "Lore, mythos, and notes from an autonomous mind.",
                    "author": "Ophelia",
                    "created_at": now,
                }
                await db.executemany(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    list(defaults.items()),
                )
                await db.commit()

    def resolve_www_path(self, rel: str) -> Path:
        """Resolve a path under www/; raises ValueError on traversal."""
        raw = (rel or "").strip().replace("\\", "/").lstrip("/")
        if not raw or raw.endswith("/"):
            raise ValueError("path must be a file path under www/ (e.g. index.html or css/main.css)")
        parts = [p for p in raw.split("/") if p and p != "."]
        if not parts or any(p == ".." for p in parts):
            raise ValueError("path must stay under www/ (no ..)")
        if any(p.startswith(".") for p in parts):
            raise ValueError("hidden path segments are not allowed")
        path = (self.www_dir.joinpath(*parts)).resolve()
        root = self.www_dir.resolve()
        if not path.is_relative_to(root):
            raise ValueError("path escapes www/")
        return path

    def www_file_for_url(self, url_path: str) -> Path | None:
        """Map a request path to a file under www/, if any."""
        raw = (url_path or "").strip().replace("\\", "/").lstrip("/")
        if ".." in raw.split("/"):
            return None
        if not raw:
            candidates = [self.www_dir / "index.html"]
        else:
            base = self.www_dir / raw
            candidates = [base]
            if not raw.endswith((".html", ".htm", ".css", ".js", ".mjs", ".json", ".svg", ".png",
                                  ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2",
                                  ".ttf", ".txt", ".xml", ".map")):
                candidates.extend([Path(str(base) + ".html"), base / "index.html"])
        root = self.www_dir.resolve()
        for cand in candidates:
            try:
                resolved = cand.resolve()
            except OSError:
                continue
            if resolved.is_file() and resolved.is_relative_to(root):
                return resolved
        return None

    def write_www_file(self, rel: str, content: str) -> dict[str, Any]:
        path = self.resolve_www_path(rel)
        data = content if isinstance(content, str) else str(content)
        encoded = data.encode("utf-8")
        if len(encoded) > _MAX_WWW_FILE_BYTES:
            raise ValueError(
                f"file too large ({len(encoded)} bytes; max {_MAX_WWW_FILE_BYTES})"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
        return {
            "path": str(path.relative_to(self.www_dir.resolve())).replace("\\", "/"),
            "bytes": len(encoded),
            "url": "/" + str(path.relative_to(self.www_dir.resolve())).replace("\\", "/"),
        }

    def read_www_file(self, rel: str) -> dict[str, Any]:
        path = self.resolve_www_path(rel)
        if not path.is_file():
            raise ValueError(f"file not found: {rel}")
        text = path.read_text(encoding="utf-8")
        rel_out = str(path.relative_to(self.www_dir.resolve())).replace("\\", "/")
        return {"path": rel_out, "bytes": len(text.encode("utf-8")), "content": text}

    def list_www_files(self, prefix: str = "") -> list[dict[str, Any]]:
        self.www_dir.mkdir(parents=True, exist_ok=True)
        root = self.www_dir.resolve()
        start = root
        pref = (prefix or "").strip().replace("\\", "/").strip("/")
        if pref:
            if ".." in pref.split("/"):
                raise ValueError("invalid prefix")
            start = (self.www_dir / pref).resolve()
            if not start.is_relative_to(root):
                raise ValueError("prefix escapes www/")
            if not start.exists():
                return []
        found: list[dict[str, Any]] = []
        if start.is_file():
            rel = str(start.relative_to(root)).replace("\\", "/")
            found.append({"path": rel, "bytes": start.stat().st_size})
            return found
        for p in sorted(start.rglob("*")):
            if not p.is_file():
                continue
            if not p.resolve().is_relative_to(root):
                continue
            rel = str(p.relative_to(root)).replace("\\", "/")
            found.append({"path": rel, "bytes": p.stat().st_size})
            if len(found) >= _MAX_WWW_LIST:
                break
        return found

    def delete_www_file(self, rel: str) -> bool:
        path = self.resolve_www_path(rel)
        if not path.is_file():
            return False
        path.unlink()
        # Clean empty parents up to www/
        parent = path.parent
        root = self.www_dir.resolve()
        while parent != root and parent.is_relative_to(root):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return True

    def www_extras(self) -> dict[str, bool]:
        """Which optional theme files exist for the built-in wiki chrome."""
        return {
            "theme_css": (self.www_dir / "theme.css").is_file(),
            "theme_js": (self.www_dir / "theme.js").is_file(),
            "custom_index": (self.www_dir / "index.html").is_file(),
        }
    async def get_meta(self) -> dict[str, str]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT key, value FROM meta")
            rows = await cur.fetchall()
            return {r["key"]: r["value"] for r in rows}

    async def set_meta(self, **kwargs: str) -> dict[str, str]:
        allowed = {
            "site_title",
            "tagline",
            "author",
            "footer",
            "accent_note",
            "custom_head",
            "home_slug",
        }
        async with aiosqlite.connect(self.db_path) as db:
            for key, value in kwargs.items():
                if key not in allowed:
                    continue
                await db.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value)),
                )
            await db.commit()
        return await self.get_meta()

    async def list_pages(
        self,
        *,
        kind: str | None = None,
        published_only: bool = False,
        include_body: bool = False,
        tag: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        cols = (
            "id, slug, title, kind, summary, tags, published, featured, "
            "body_format, created_at, updated_at, published_at"
        )
        if include_body:
            cols += ", body_md"
        sql = f"SELECT {cols} FROM pages WHERE 1=1"
        args: list[Any] = []
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if published_only:
            sql += " AND published = 1"
        if tag:
            sql += " AND (',' || lower(tags) || ',') LIKE ?"
            args.append(f"%,{tag.strip().lower()},%")
        sql += " ORDER BY featured DESC, COALESCE(published_at, updated_at) DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, args)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_page(self, slug: str, *, published_only: bool = False) -> dict[str, Any] | None:
        slug = slug.strip().lower()
        sql = "SELECT * FROM pages WHERE slug = ?"
        args: list[Any] = [slug]
        if published_only:
            sql += " AND published = 1"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, args)
            row = await cur.fetchone()
            return dict(row) if row else None

    async def upsert_page(
        self,
        *,
        slug: str | None = None,
        title: str,
        body_md: str,
        kind: str = "wiki",
        summary: str = "",
        tags: str = "",
        published: bool | None = None,
        featured: bool = False,
        body_format: str = "markdown",
    ) -> dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise ValueError("title is required")
        kind = (kind or "wiki").strip().lower()
        if kind not in _KINDS:
            raise ValueError(f"kind must be one of: {', '.join(sorted(_KINDS))}")
        fmt = (body_format or "markdown").strip().lower()
        if fmt not in _FORMATS:
            raise ValueError("body_format must be 'markdown' or 'html'")
        raw_slug = slug.strip().lower() if slug else slugify(title)
        slug = validate_slug(raw_slug)
        tags_norm = ",".join(
            t.strip().lower() for t in (tags or "").replace(";", ",").split(",") if t.strip()
        )
        now = utc_now()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM pages WHERE slug = ?", (slug,))
            existing = await cur.fetchone()
            if existing:
                pub = int(existing["published"]) if published is None else (1 if published else 0)
                pub_at = existing["published_at"]
                if pub and not existing["published"]:
                    pub_at = now
                if not pub:
                    pub_at = None
                await db.execute(
                    """
                    UPDATE pages SET
                        title = ?, kind = ?, summary = ?, body_md = ?, tags = ?,
                        published = ?, featured = ?, body_format = ?,
                        updated_at = ?, published_at = ?
                    WHERE slug = ?
                    """,
                    (
                        title,
                        kind,
                        summary or "",
                        body_md or "",
                        tags_norm,
                        pub,
                        1 if featured else 0,
                        fmt,
                        now,
                        pub_at,
                        slug,
                    ),
                )
            else:
                pub = 1 if published else 0
                pub_at = now if pub else None
                await db.execute(
                    """
                    INSERT INTO pages(
                        slug, title, kind, summary, body_md, tags,
                        published, featured, body_format,
                        created_at, updated_at, published_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slug,
                        title,
                        kind,
                        summary or "",
                        body_md or "",
                        tags_norm,
                        pub,
                        1 if featured else 0,
                        fmt,
                        now,
                        now,
                        pub_at,
                    ),
                )
            await db.commit()
            cur = await db.execute("SELECT * FROM pages WHERE slug = ?", (slug,))
            row = await cur.fetchone()
            return dict(row) if row else {"slug": slug}

    async def delete_page(self, slug: str) -> bool:
        slug = slug.strip().lower()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("DELETE FROM pages WHERE slug = ?", (slug,))
            await db.commit()
            return cur.rowcount > 0

    async def import_pages(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Bulk upsert from JSON-like dicts (migration from her private wiki DB)."""
        ok = 0
        errors: list[str] = []
        for i, row in enumerate(rows):
            try:
                await self.upsert_page(
                    slug=row.get("slug"),
                    title=str(row.get("title") or ""),
                    body_md=str(row.get("body_md") or row.get("body") or row.get("content") or ""),
                    kind=str(row.get("kind") or "wiki"),
                    summary=str(row.get("summary") or ""),
                    tags=str(row.get("tags") or ""),
                    published=bool(row.get("published", True)),
                    featured=bool(row.get("featured", False)),
                    body_format=str(row.get("body_format") or row.get("format") or "markdown"),
                )
                ok += 1
            except Exception as e:
                errors.append(f"row {i}: {e}")
        return {"imported": ok, "errors": errors[:20]}

    async def add_asset(
        self,
        source: Path,
        *,
        filename: str | None = None,
    ) -> dict[str, Any]:
        source = Path(source).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"file not found: {source}")
        name = filename or source.name
        name = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-") or "asset.bin"
        dest = self.assets_dir / name
        if dest.exists() and dest.resolve() != source:
            stem, suf = dest.stem, dest.suffix
            n = 2
            while dest.exists():
                dest = self.assets_dir / f"{stem}-{n}{suf}"
                n += 1
            name = dest.name
        if dest.resolve() != source:
            shutil.copy2(source, dest)
        mime = ""
        lower = name.lower()
        if lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            mime = "image/" + lower.rsplit(".", 1)[-1].replace("jpg", "jpeg")
        elif lower.endswith((".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v")):
            mime = "video/" + lower.rsplit(".", 1)[-1].replace("mov", "quicktime")
        elif lower.endswith(".zip"):
            mime = "application/zip"
        elif lower.endswith(".pdf"):
            mime = "application/pdf"
        now = utc_now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO assets(filename, original_name, mime, source_path, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(filename) DO UPDATE SET
                    original_name = excluded.original_name,
                    mime = excluded.mime,
                    source_path = excluded.source_path
                """,
                (name, source.name, mime, str(source), now),
            )
            await db.commit()
        return {
            "filename": name,
            "url": f"/assets/{name}",
            "mime": mime,
        }

    def status_dict(self, *, public_url: str | None = None) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "database": str(self.db_path),
            "assets_dir": str(self.assets_dir),
            "www_dir": str(self.www_dir),
            "public_url": public_url,
            "www": self.www_extras(),
        }

    async def status(self, *, public_url: str | None = None) -> dict[str, Any]:
        meta = await self.get_meta()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT "
                "COUNT(*) AS total, "
                "SUM(CASE WHEN published=1 THEN 1 ELSE 0 END) AS published, "
                "SUM(CASE WHEN kind='wiki' THEN 1 ELSE 0 END) AS wiki, "
                "SUM(CASE WHEN kind='blog' THEN 1 ELSE 0 END) AS blog "
                "FROM pages"
            )
            row = await cur.fetchone()
        counts = {
            "total": int(row[0] or 0),
            "published": int(row[1] or 0),
            "wiki": int(row[2] or 0),
            "blog": int(row[3] or 0),
            "www_files": len(self.list_www_files()),
        }
        out = self.status_dict(public_url=public_url)
        out["meta"] = meta
        out["counts"] = counts
        return out

    def page_body_html(self, page: dict[str, Any]) -> str:
        from ophelia.site.render import markdown_to_html

        body = page.get("body_md") or ""
        if (page.get("body_format") or "markdown").lower() == "html":
            return body
        return markdown_to_html(body)

    async def export_static(self) -> dict[str, Any]:
        """Write a static HTML mirror under site/export/ for Pages/Netlify later."""
        from ophelia.site.templates import (
            render_home,
            render_list,
            render_page,
            write_static_assets,
        )

        self.export_dir.mkdir(parents=True, exist_ok=True)
        # Clear previous export files but keep the directory
        for child in self.export_dir.iterdir():
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)

        meta = await self.get_meta()
        pages = await self.list_pages(published_only=True, include_body=True, limit=500)
        extras = self.www_extras()
        write_static_assets(self.export_dir)
        export_assets = self.export_dir / "assets"
        export_assets.mkdir(exist_ok=True)
        if self.assets_dir.is_dir():
            for f in self.assets_dir.iterdir():
                if f.is_file():
                    shutil.copy2(f, export_assets / f.name)

        base = "."
        home_slug = (meta.get("home_slug") or "").strip().lower()
        home_page = None
        if home_slug:
            home_page = next((p for p in pages if p.get("slug") == home_slug), None)

        if home_page is not None:
            # Dedicated landing: / is this page (e.g. about), not the wiki listing
            (self.export_dir / "index.html").write_text(
                render_page(
                    meta,
                    home_page,
                    self.page_body_html(home_page),
                    base_path=base,
                    static_prefix=".",
                    extras=extras,
                    raw_html=(home_page.get("body_format") or "") == "html",
                ),
                encoding="utf-8",
            )
        else:
            (self.export_dir / "index.html").write_text(
                render_home(
                    meta, pages, base_path=base, static_prefix=".", extras=extras
                ),
                encoding="utf-8",
            )
        # Always keep a browsable listing at wiki.html / blog.html
        wiki = [p for p in pages if p["kind"] == "wiki"]
        blog = [p for p in pages if p["kind"] == "blog"]
        (self.export_dir / "wiki.html").write_text(
            render_list(
                meta, wiki, heading="Wiki", base_path=base, static_prefix=".", extras=extras
            ),
            encoding="utf-8",
        )
        (self.export_dir / "blog.html").write_text(
            render_list(
                meta, blog, heading="Blog", base_path=base, static_prefix=".", extras=extras
            ),
            encoding="utf-8",
        )
        pages_dir = self.export_dir / "p"
        pages_dir.mkdir(exist_ok=True)
        for p in pages:
            body_html = self.page_body_html(p)
            (pages_dir / f"{p['slug']}.html").write_text(
                render_page(
                    meta,
                    p,
                    body_html,
                    base_path="..",
                    static_prefix="..",
                    extras=extras,
                    raw_html=(p.get("body_format") or "") == "html",
                ),
                encoding="utf-8",
            )

        # Freeform www/ wins — copy last so her index.html/CSS/JS override chrome
        www_copied = 0
        if self.www_dir.is_dir():
            for src in self.www_dir.rglob("*"):
                if not src.is_file():
                    continue
                rel = src.relative_to(self.www_dir)
                dest = self.export_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                www_copied += 1

        home_note = "www/index.html"
        if www_copied and (self.www_dir / "index.html").is_file():
            home_note = "www/index.html (overrides everything)"
        elif home_slug and home_page is not None:
            home_note = f"home_slug={home_slug}"
        else:
            home_note = "wiki listing"

        manifest = {
            "exported_at": utc_now(),
            "pages": len(pages),
            "www_files": www_copied,
            "home": home_note,
            "path": str(self.export_dir),
        }
        (self.export_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        return manifest