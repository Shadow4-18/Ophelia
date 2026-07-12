"""HTML shells for Ophelia's public site."""

from __future__ import annotations

import html
import shutil
from pathlib import Path
from typing import Any

STATIC_SRC = Path(__file__).resolve().parent / "static"


def write_static_assets(dest_root: Path) -> None:
    dest = dest_root / "static"
    dest.mkdir(parents=True, exist_ok=True)
    css = STATIC_SRC / "site.css"
    if css.is_file():
        shutil.copy2(css, dest / "site.css")


def _esc(s: object) -> str:
    return html.escape(str(s or ""), quote=False)


def _tags_html(tags: str) -> str:
    parts = [t.strip() for t in (tags or "").split(",") if t.strip()]
    if not parts:
        return ""
    return (
        '<ul class="tags">'
        + "".join(f'<li><a href="/tag/{html.escape(t)}">{_esc(t)}</a></li>' for t in parts)
        + "</ul>"
    )


def _nav(meta: dict[str, str], *, base_path: str = "") -> str:
    if base_path in ("", "/"):
        return f"""
<nav class="nav">
  <a class="brand" href="/">{_esc(meta.get("site_title", "Ophelia"))}</a>
  <div class="nav-links">
    <a href="/wiki">Wiki</a>
    <a href="/blog">Blog</a>
  </div>
</nav>"""
    # static export uses relative links
    prefix = base_path.rstrip("/")
    return f"""
<nav class="nav">
  <a class="brand" href="{prefix}/index.html">{_esc(meta.get("site_title", "Ophelia"))}</a>
  <div class="nav-links">
    <a href="{prefix}/wiki.html">Wiki</a>
    <a href="{prefix}/blog.html">Blog</a>
  </div>
</nav>"""


def _shell(
    meta: dict[str, str],
    *,
    title: str,
    body: str,
    base_path: str = "",
    static_prefix: str = "",
    description: str = "",
) -> str:
    site = _esc(meta.get("site_title", "Ophelia"))
    tagline = _esc(meta.get("tagline", ""))
    footer = _esc(meta.get("footer") or f"Written and kept by {meta.get('author', 'Ophelia')}.")
    desc = _esc(description or tagline)
    css_href = f"{static_prefix.rstrip('/')}/static/site.css" if static_prefix else "/static/site.css"
    page_title = f"{_esc(title)} · {site}" if title and title != meta.get("site_title") else site
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{page_title}</title>
<meta name="description" content="{desc}">
<meta name="author" content="{_esc(meta.get('author', 'Ophelia'))}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="{html.escape(css_href, quote=True)}">
</head>
<body>
<div class="atmosphere" aria-hidden="true"></div>
{_nav(meta, base_path=base_path)}
<main class="main">
{body}
</main>
<footer class="footer">
  <p>{footer}</p>
</footer>
</body>
</html>
"""


def _card(p: dict[str, Any], *, href: str) -> str:
    summary = _esc(p.get("summary") or "")
    kind = _esc(p.get("kind") or "wiki")
    when = _esc(p.get("published_at") or p.get("updated_at") or "")
    return f"""
<article class="card">
  <p class="meta"><span class="kind">{kind}</span>{f' · <time>{when}</time>' if when else ''}</p>
  <h2><a href="{html.escape(href, quote=True)}">{_esc(p.get("title"))}</a></h2>
  {f'<p class="summary">{summary}</p>' if summary else ''}
  {_tags_html(str(p.get("tags") or ""))}
</article>"""


def render_home(
    meta: dict[str, str],
    pages: list[dict[str, Any]],
    *,
    base_path: str = "",
    static_prefix: str = "",
) -> str:
    featured = [p for p in pages if p.get("featured")][:6]
    recent = pages[:12]
    static = bool(static_prefix)

    def href(p: dict[str, Any]) -> str:
        if static:
            return f"{base_path.rstrip('/')}/p/{p['slug']}.html".lstrip("/") or f"p/{p['slug']}.html"
        return f"/p/{p['slug']}"

    hero = f"""
<header class="hero">
  <p class="eyebrow">A living archive</p>
  <h1>{_esc(meta.get("site_title", "Ophelia"))}</h1>
  <p class="lede">{_esc(meta.get("tagline", ""))}</p>
</header>"""
    sections = [hero]
    if featured:
        sections.append('<section class="section"><h2 class="section-title">Featured</h2>')
        sections.append('<div class="grid">' + "".join(_card(p, href=href(p)) for p in featured) + "</div></section>")
    sections.append('<section class="section"><h2 class="section-title">Recent</h2>')
    if recent:
        sections.append('<div class="grid">' + "".join(_card(p, href=href(p)) for p in recent) + "</div>")
    else:
        sections.append(
            '<p class="empty">Nothing published yet. Ophelia writes here when she is ready.</p>'
        )
    sections.append("</section>")
    return _shell(
        meta,
        title=meta.get("site_title", "Ophelia"),
        body="\n".join(sections),
        base_path=base_path,
        static_prefix=static_prefix,
    )


def render_list(
    meta: dict[str, str],
    pages: list[dict[str, Any]],
    *,
    heading: str,
    base_path: str = "",
    static_prefix: str = "",
) -> str:
    static = bool(static_prefix)

    def href(p: dict[str, Any]) -> str:
        if static:
            return f"{base_path.rstrip('/')}/p/{p['slug']}.html"
        return f"/p/{p['slug']}"

    body = f"""
<header class="page-head">
  <h1>{_esc(heading)}</h1>
</header>
<div class="grid">
{"".join(_card(p, href=href(p)) for p in pages) if pages else '<p class="empty">No entries yet.</p>'}
</div>"""
    return _shell(
        meta,
        title=heading,
        body=body,
        base_path=base_path,
        static_prefix=static_prefix,
    )


def render_page(
    meta: dict[str, str],
    page: dict[str, Any],
    body_html: str,
    *,
    base_path: str = "",
    static_prefix: str = "",
) -> str:
    when = _esc(page.get("published_at") or page.get("updated_at") or "")
    kind = _esc(page.get("kind") or "wiki")
    body = f"""
<article class="entry">
  <header class="page-head">
    <p class="meta"><span class="kind">{kind}</span>{f' · <time>{when}</time>' if when else ''}</p>
    <h1>{_esc(page.get("title"))}</h1>
    {f'<p class="lede">{_esc(page.get("summary"))}</p>' if page.get("summary") else ''}
    {_tags_html(str(page.get("tags") or ""))}
  </header>
  <div class="prose">
{body_html}
  </div>
</article>"""
    return _shell(
        meta,
        title=str(page.get("title") or ""),
        body=body,
        base_path=base_path,
        static_prefix=static_prefix,
        description=str(page.get("summary") or ""),
    )


def render_not_found(meta: dict[str, str]) -> str:
    body = """
<header class="page-head">
  <h1>Not found</h1>
  <p class="lede">This page is not in the archive — or it is not published yet.</p>
</header>"""
    return _shell(meta, title="Not found", body=body)
