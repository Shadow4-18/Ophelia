"""Public website tool handlers (pages, assets, files, preview, deploy).

Mixed into ToolRegistry; methods use its attributes (settings, memory,
android, senders, …). Split out of tools/registry.py."""

from __future__ import annotations

import json
from pathlib import Path

import structlog


log = structlog.get_logger()


class SiteToolsMixin:
    async def _site_store(self):
        from ophelia.site.store import SiteStore

        store = SiteStore(self.settings.site_dir)
        await store.init()
        return store

    def _site_url(self) -> str:
        public = (self.settings.site_public_url or "").strip()
        if public:
            return public.rstrip("/")
        return f"http://{self.settings.site_host}:{self.settings.site_port}"

    async def _site_status(self) -> str:
        from ophelia.site.cloudflare import deploy_ready

        store = await self._site_store()
        st = await store.status(public_url=self._site_url())
        enabled = "yes" if self.settings.site_enabled else "no (OPHELIA_SITE_ENABLED=false)"
        cf = deploy_ready(
            account_id=self.settings.cloudflare_account_id,
            api_token=self.settings.cloudflare_api_token,
            project=self.settings.site_cf_project,
        )
        if cf["ready"]:
            hint = (
                "Landing / priority: (1) www/index.html via site_write_file, "
                "(2) site_set_meta(home_slug='about'), (3) default wiki listing. "
                "Then call site_deploy to push to Cloudflare Pages."
            )
        else:
            missing = ", ".join(cf["missing"]) or "credentials"
            hint = (
                "Landing / priority: www/index.html > home_slug > wiki listing. "
                f"Cloudflare deploy not ready — missing: {missing}. "
                "Owner must set CLOUDFLARE_API_TOKEN (Pages Edit), "
                "CLOUDFLARE_ACCOUNT_ID, OPHELIA_SITE_CF_PROJECT, and "
                "OPHELIA_SITE_PUBLIC_URL in ~/.ophelia/.env."
            )
        return json.dumps(
            {
                **st,
                "server_enabled": enabled,
                "cloudflare": cf,
                "public_url": self._site_url(),
                "hint": hint,
            },
            indent=2,
            default=str,
        )

    async def _site_list_pages(
        self,
        kind: str = "",
        published_only: bool = False,
        tag: str = "",
        limit: int = 50,
    ) -> str:
        store = await self._site_store()
        rows = await store.list_pages(
            kind=kind or None,
            published_only=bool(published_only),
            tag=tag or None,
            limit=limit or 50,
        )
        return json.dumps({"count": len(rows), "pages": rows}, indent=2, default=str)

    async def _site_get_page(self, slug: str) -> str:
        store = await self._site_store()
        row = await store.get_page(slug)
        if not row:
            return f"No page with slug '{slug}'."
        return json.dumps(row, indent=2, default=str)

    async def _site_upsert_page(
        self,
        title: str,
        body_md: str,
        slug: str = "",
        kind: str = "wiki",
        summary: str = "",
        tags: str = "",
        published: bool | None = None,
        featured: bool | None = None,
        body_format: str = "markdown",
        sort_order: int | None = None,
    ) -> str:
        store = await self._site_store()
        try:
            row = await store.upsert_page(
                slug=slug or None,
                title=title,
                body_md=body_md,
                kind=kind or "wiki",
                summary=summary or "",
                tags=tags or "",
                published=published,
                featured=featured,
                body_format=body_format or "markdown",
                sort_order=sort_order,
            )
        except Exception as e:
            return f"site_upsert_page error: {e}"
        url = f"{self._site_url()}/p/{row.get('slug')}"
        preview = f"{self._site_url()}/preview/{row.get('slug')}"
        return json.dumps(
            {
                "ok": True,
                "page": row,
                "public_url": url if row.get("published") else None,
                "preview_url": preview,
                "note": (
                    "Published — visible on your site."
                    if row.get("published")
                    else (
                        "Saved as draft — open preview_url to check render, "
                        "then set published=true when ready."
                    )
                ),
            },
            indent=2,
            default=str,
        )

    async def _site_delete_page(self, slug: str) -> str:
        store = await self._site_store()
        ok = await store.delete_page(slug)
        return "Deleted." if ok else f"No page with slug '{slug}'."

    async def _site_reorder_pages(self, slugs: str) -> str:
        store = await self._site_store()
        raw = (slugs or "").strip()
        if not raw:
            return "site_reorder_pages error: pass slugs (comma-separated or JSON array)."
        parsed: list[str]
        if raw.startswith("["):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                return f"site_reorder_pages error: invalid JSON — {e}"
            if not isinstance(data, list):
                return "site_reorder_pages error: JSON must be an array of slugs."
            parsed = [str(x) for x in data]
        else:
            parsed = [p.strip() for p in raw.replace(";", ",").split(",")]
        try:
            result = await store.reorder_pages(parsed)
        except Exception as e:
            return f"site_reorder_pages error: {e}"
        result["note"] = (
            "Order updated locally. Call site_deploy to push the new order to Cloudflare."
        )
        return json.dumps(result, indent=2)

    async def _site_set_meta(
        self,
        site_title: str = "",
        tagline: str = "",
        author: str = "",
        footer: str = "",
        custom_head: str = "",
        home_slug: str | None = None,
        not_found_glyph: str | None = None,
        not_found_line: str | None = None,
    ) -> str:
        store = await self._site_store()
        kwargs = {}
        if site_title:
            kwargs["site_title"] = site_title
        if tagline:
            kwargs["tagline"] = tagline
        if author:
            kwargs["author"] = author
        if footer:
            kwargs["footer"] = footer
        if custom_head:
            kwargs["custom_head"] = custom_head
        # Allow explicitly clearing home_slug with empty string
        if home_slug is not None:
            kwargs["home_slug"] = (home_slug or "").strip().lower()
        if not_found_glyph is not None:
            kwargs["not_found_glyph"] = not_found_glyph
        if not_found_line is not None:
            kwargs["not_found_line"] = not_found_line
        if not kwargs:
            return (
                "Nothing to update — pass site_title, tagline, author, footer, "
                "custom_head, home_slug, not_found_glyph, and/or not_found_line."
            )
        meta = await store.set_meta(**kwargs)
        note = None
        hs = (meta.get("home_slug") or "").strip()
        if hs:
            note = (
                f"Landing / is now the published page '{hs}' "
                "(unless www/index.html exists — that always wins). "
                "Re-export/redeploy to update Cloudflare."
            )
        return json.dumps({"ok": True, "meta": meta, "note": note}, indent=2)

    async def _site_write_file(self, path: str, content: str) -> str:
        store = await self._site_store()
        try:
            info = store.write_www_file(path, content)
        except Exception as e:
            return f"site_write_file error: {e}"
        return json.dumps(
            {
                "ok": True,
                **info,
                "public_url": f"{self._site_url()}{info['url']}",
                "note": (
                    "Live on your site immediately. "
                    "index.html becomes the home page; theme.css/theme.js restyle wiki chrome."
                ),
            },
            indent=2,
        )

    async def _site_read_file(self, path: str) -> str:
        store = await self._site_store()
        try:
            info = store.read_www_file(path)
        except Exception as e:
            return f"site_read_file error: {e}"
        # Cap huge files in tool output
        content = info.get("content") or ""
        if len(content) > 12000:
            info = {
                **info,
                "content": content[:12000],
                "truncated": True,
                "note": "Content truncated in tool result; file on disk is complete.",
            }
        return json.dumps(info, indent=2)

    async def _site_list_files(self, prefix: str = "") -> str:
        store = await self._site_store()
        try:
            files = store.list_www_files(prefix)
        except Exception as e:
            return f"site_list_files error: {e}"
        return json.dumps({"count": len(files), "files": files}, indent=2)

    async def _site_delete_file(self, path: str) -> str:
        store = await self._site_store()
        try:
            ok = store.delete_www_file(path)
        except Exception as e:
            return f"site_delete_file error: {e}"
        return "Deleted." if ok else f"No file at www/{path}."

    async def _site_import_pages(self, pages_json: str) -> str:
        store = await self._site_store()
        try:
            rows = json.loads(pages_json)
        except json.JSONDecodeError as e:
            return f"Invalid JSON: {e}"
        if not isinstance(rows, list):
            return "pages_json must be a JSON array of page objects."
        result = await store.import_pages(rows)
        return json.dumps(result, indent=2)

    async def _site_add_asset(self, path: str, filename: str = "") -> str:
        store = await self._site_store()
        try:
            asset = await store.add_asset(Path(path), filename=filename or None)
        except Exception as e:
            return f"site_add_asset error: {e}"
        asset["public_url"] = f"{self._site_url()}{asset['url']}"
        asset["gallery_url"] = f"{self._site_url()}/gallery"
        asset["markdown"] = f"![]({asset['url']})"
        return json.dumps({"ok": True, **asset}, indent=2)

    async def _site_list_assets(self, kind: str = "all", limit: int = 50) -> str:
        store = await self._site_store()
        filter_kind = None if (kind or "all") in ("", "all") else kind
        rows = await store.list_assets(kind=filter_kind, limit=limit or 50)
        return json.dumps(
            {
                "count": len(rows),
                "assets": rows,
                "gallery_url": f"{self._site_url()}/gallery",
            },
            indent=2,
            default=str,
        )

    async def _site_preview_page(self, slug: str) -> str:
        store = await self._site_store()
        row = await store.get_page(slug)
        if not row:
            return f"No page with slug '{slug}'."
        preview = f"{self._site_url()}/preview/{row.get('slug')}"
        public = (
            f"{self._site_url()}/p/{row.get('slug')}" if row.get("published") else None
        )
        return json.dumps(
            {
                "ok": True,
                "slug": row.get("slug"),
                "published": bool(row.get("published")),
                "preview_url": preview,
                "public_url": public,
                "note": (
                    "Open preview_url on the local site server to see rendered HTML. "
                    "Drafts are never included in site_deploy / Cloudflare export."
                ),
            },
            indent=2,
            default=str,
        )

    async def _site_export_static(self) -> str:
        store = await self._site_store()
        try:
            manifest = await store.export_static()
        except Exception as e:
            return f"site_export_static error: {e}"
        return json.dumps({"ok": True, **manifest}, indent=2, default=str)

    async def _site_deploy(self) -> str:
        from ophelia.site.cloudflare import (
            CloudflarePagesError,
            deploy_directory_async,
            deploy_ready,
        )

        cf = deploy_ready(
            account_id=self.settings.cloudflare_account_id,
            api_token=self.settings.cloudflare_api_token,
            project=self.settings.site_cf_project,
        )
        if not cf["ready"]:
            missing = ", ".join(cf["missing"]) or "credentials"
            return (
                "site_deploy not configured. Owner must add to ~/.ophelia/.env: "
                f"{missing}. Token needs Cloudflare Pages:Edit. "
                "Also set OPHELIA_SITE_PUBLIC_URL to the custom domain."
            )
        store = await self._site_store()
        try:
            manifest = await store.export_static()
        except Exception as e:
            return f"site_deploy export error: {e}"
        try:
            result = await deploy_directory_async(
                store.export_dir,
                account_id=self.settings.cloudflare_account_id or "",
                api_token=self.settings.cloudflare_api_token or "",
                project=self.settings.site_cf_project or "",
                branch=self.settings.site_cf_branch or "main",
                create_project=bool(self.settings.site_cf_create_project),
            )
        except CloudflarePagesError as e:
            return f"site_deploy error: {e}"
        except Exception as e:
            return f"site_deploy error: {type(e).__name__}: {e}"
        public = self._site_url()
        return json.dumps(
            {
                "ok": True,
                "exported_pages": manifest.get("pages"),
                "export_path": manifest.get("path"),
                "project": result.project,
                "method": result.method,
                "files": result.files,
                "uploaded": result.uploaded,
                "duration_sec": round(result.duration, 2),
                "deployment_url": result.url or None,
                "public_url": public,
                "note": (
                    f"Live site should be at {public}. "
                    "If the custom domain was already attached in Cloudflare, "
                    "visitors see this export after CDN refresh."
                ),
            },
            indent=2,
            default=str,
        )
