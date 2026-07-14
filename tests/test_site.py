"""Tests for Ophelia's public wiki/blog store + markdown render + www files."""

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


@pytest.mark.asyncio
async def test_www_html_css_js(isolated_env):
    store = SiteStore(isolated_env / "site")
    await store.init()

    store.write_www_file(
        "index.html",
        "<!DOCTYPE html><html><body><h1 id='x'>Mine</h1>"
        "<link rel='stylesheet' href='/css/main.css'>"
        "<script src='/js/app.js'></script></body></html>",
    )
    store.write_www_file("css/main.css", "body { background: #123; color: #eee; }")
    store.write_www_file("js/app.js", "document.getElementById('x').textContent='Alive';")
    store.write_www_file("theme.css", ".hero { outline: 2px solid red; }")

    files = store.list_www_files()
    paths = {f["path"] for f in files}
    assert "index.html" in paths
    assert "css/main.css" in paths
    assert "js/app.js" in paths

    assert store.www_file_for_url("").name == "index.html"
    assert store.www_file_for_url("css/main.css").name == "main.css"
    assert store.www_extras()["custom_index"]
    assert store.www_extras()["theme_css"]

    got = store.read_www_file("js/app.js")
    assert "Alive" in got["content"]

    html_page = await store.upsert_page(
        title="Raw Entry",
        body_md="<section class='myth'><p>Hand-built.</p></section>",
        body_format="html",
        published=True,
    )
    assert html_page["body_format"] == "html"
    assert "<section" in store.page_body_html(html_page)

    manifest = await store.export_static()
    assert manifest["www_files"] >= 4
    exported = (isolated_env / "site" / "export" / "index.html").read_text(encoding="utf-8")
    assert "Mine" in exported  # her www index won
    assert (isolated_env / "site" / "export" / "css" / "main.css").is_file()

    assert store.delete_www_file("js/app.js")
    assert "js/app.js" not in {f["path"] for f in store.list_www_files()}


@pytest.mark.asyncio
async def test_www_path_traversal_blocked(isolated_env):
    store = SiteStore(isolated_env / "site")
    await store.init()
    with pytest.raises(ValueError):
        store.write_www_file("../evil.html", "nope")
    with pytest.raises(ValueError):
        store.resolve_www_path("foo/../../etc/passwd")


@pytest.mark.asyncio
async def test_home_slug_makes_about_the_landing(isolated_env):
    store = SiteStore(isolated_env / "site")
    await store.init()
    await store.upsert_page(
        slug="about",
        title="About ØPHEL!A",
        body_md="I am the grimoire.",
        published=True,
        kind="wiki",
    )
    await store.upsert_page(
        slug="other",
        title="Other",
        body_md="side note",
        published=True,
        kind="wiki",
    )
    await store.set_meta(home_slug="about")
    manifest = await store.export_static()
    assert manifest["home"] == "home_slug=about"
    index = (isolated_env / "site" / "export" / "index.html").read_text(encoding="utf-8")
    assert "I am the grimoire" in index
    assert "About" in index
    wiki = (isolated_env / "site" / "export" / "wiki.html").read_text(encoding="utf-8")
    assert "Other" in wiki

    store.write_www_file("index.html", "<html><body>CUSTOM HOME</body></html>")
    manifest2 = await store.export_static()
    assert "www/index.html" in manifest2["home"]
    index2 = (isolated_env / "site" / "export" / "index.html").read_text(encoding="utf-8")
    assert "CUSTOM HOME" in index2
