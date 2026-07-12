"""Tests for Ophelia's public wiki/blog store + markdown render."""

from __future__ import annotations

import pytest

from ophelia.site.render import markdown_to_html
from ophelia.site.store import SiteStore


def test_markdown_basic():
    html = markdown_to_html("# Hello\n\nThis is **bold** and *italic*.\n\n- a\n- b")
    assert "<h1>Hello</h1>" in html
    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html
    assert "<ul>" in html
    assert "<li>a</li>" in html


def test_markdown_escapes_html():
    html = markdown_to_html("Hi <script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.asyncio
async def test_site_store_publish_and_draft(isolated_env):
    store = SiteStore(isolated_env / "site")
    await store.init()

    draft = await store.upsert_page(
        title="Origin Myth",
        body_md="## Beginning\n\nShe woke in the dark.",
        kind="wiki",
        tags="mythos,origin",
        published=False,
    )
    assert draft["slug"] == "origin-myth"
    assert draft["published"] == 0

    pub = await store.list_pages(published_only=True)
    assert pub == []

    live = await store.upsert_page(
        slug="origin-myth",
        title="Origin Myth",
        body_md="## Beginning\n\nShe woke in the dark.\n\nAnd named herself.",
        published=True,
        featured=True,
    )
    assert live["published"] == 1
    assert live["published_at"]

    pages = await store.list_pages(published_only=True)
    assert len(pages) == 1
    assert pages[0]["slug"] == "origin-myth"

    got = await store.get_page("origin-myth", published_only=True)
    assert got and "named herself" in got["body_md"]

    meta = await store.set_meta(site_title="Ophelia Archive", tagline="Mythos.")
    assert meta["site_title"] == "Ophelia Archive"

    manifest = await store.export_static()
    assert manifest["pages"] == 1
    export_index = isolated_env / "site" / "export" / "index.html"
    assert export_index.is_file()
    assert "Origin Myth" in export_index.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_site_import_pages(isolated_env):
    store = SiteStore(isolated_env / "site")
    await store.init()
    result = await store.import_pages(
        [
            {
                "title": "The River",
                "body": "Water remembers.",
                "kind": "blog",
                "published": True,
                "tags": "lore",
            },
            {"title": "", "body_md": "bad"},  # error row
        ]
    )
    assert result["imported"] == 1
    assert result["errors"]
    pages = await store.list_pages(kind="blog", published_only=True)
    assert len(pages) == 1
    assert pages[0]["slug"] == "the-river"
