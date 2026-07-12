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
    """Ophelia's public wiki/blog content under ~/.ophelia/site/."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.db_path = self.root / "site.db"
        self.assets_dir = self.root / "assets"
        self.export_dir = self.root / "export"

    async def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
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
            "created_at, updated_at, published_at"
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
    ) -> dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise ValueError("title is required")
        kind = (kind or "wiki").strip().lower()
        if kind not in _KINDS:
            raise ValueError(f"kind must be one of: {', '.join(sorted(_KINDS))}")
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
                        published = ?, featured = ?, updated_at = ?, published_at = ?
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
                        published, featured, created_at, updated_at, published_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            "public_url": public_url,
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
        }
        out = self.status_dict(public_url=public_url)
        out["meta"] = meta
        out["counts"] = counts
        return out

    async def export_static(self) -> dict[str, Any]:
        """Write a static HTML mirror under site/export/ for Pages/Netlify later."""
        from ophelia.site.render import markdown_to_html
        from ophelia.site.templates import (
            render_home,
            render_list,
            render_page,
            write_static_assets,
        )

        self.export_dir.mkdir(parents=True, exist_ok=True)
        meta = await self.get_meta()
        pages = await self.list_pages(published_only=True, include_body=True, limit=500)
        write_static_assets(self.export_dir)
        # Copy media assets
        export_assets = self.export_dir / "assets"
        export_assets.mkdir(exist_ok=True)
        if self.assets_dir.is_dir():
            for f in self.assets_dir.iterdir():
                if f.is_file():
                    shutil.copy2(f, export_assets / f.name)

        base = "."
        (self.export_dir / "index.html").write_text(
            render_home(meta, pages, base_path=base, static_prefix="."),
            encoding="utf-8",
        )
        wiki = [p for p in pages if p["kind"] == "wiki"]
        blog = [p for p in pages if p["kind"] == "blog"]
        (self.export_dir / "wiki.html").write_text(
            render_list(meta, wiki, heading="Wiki", base_path=base, static_prefix="."),
            encoding="utf-8",
        )
        (self.export_dir / "blog.html").write_text(
            render_list(meta, blog, heading="Blog", base_path=base, static_prefix="."),
            encoding="utf-8",
        )
        pages_dir = self.export_dir / "p"
        pages_dir.mkdir(exist_ok=True)
        for p in pages:
            body_html = markdown_to_html(p.get("body_md") or "")
            (pages_dir / f"{p['slug']}.html").write_text(
                render_page(meta, p, body_html, base_path="..", static_prefix=".."),
                encoding="utf-8",
            )
        manifest = {
            "exported_at": utc_now(),
            "pages": len(pages),
            "path": str(self.export_dir),
        }
        (self.export_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        return manifest
