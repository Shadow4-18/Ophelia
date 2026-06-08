"""Web search without API keys (DuckDuckGo + optional page fetch)."""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import quote_plus, urlparse

import httpx

HEADERS = {
    "User-Agent": "Ophelia/1.0 (research; +https://github.com/Shadow4-18/Ophelia)"
}


async def search_web(query: str, max_results: int = 8) -> str:
    query = query.strip()
    if not query:
        return "Empty query."

    lines: list[str] = []

    # Instant answers API (no key)
    async with httpx.AsyncClient(timeout=20.0, headers=HEADERS) as client:
        try:
            r = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_redirect": 1},
            )
            if r.status_code == 200:
                data = r.json()
                abstract = (data.get("AbstractText") or "").strip()
                if abstract:
                    src = data.get("AbstractURL") or data.get("AbstractSource") or ""
                    lines.append(f"Summary: {abstract}" + (f" ({src})" if src else ""))
                for topic in (data.get("RelatedTopics") or [])[:5]:
                    if isinstance(topic, dict) and topic.get("Text"):
                        lines.append(f"- {topic['Text'][:300]}")
        except httpx.HTTPError:
            pass

        # HTML results fallback
        try:
            r = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
            )
            if r.status_code == 200:
                for block in re.findall(
                    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                    r.text,
                    re.I | re.S,
                )[:max_results]:
                    url, title = block
                    title = unescape(re.sub(r"<[^>]+>", "", title)).strip()
                    if title and url.startswith("http"):
                        lines.append(f"- {title}\n  {url}")
        except httpx.HTTPError:
            pass

    if not lines:
        return f"No results for '{query}'. Try a more specific query."
    return f"Web search: {query}\n\n" + "\n".join(lines[: max_results + 3])


async def fetch_url(url: str, max_chars: int = 8000) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return "URL must start with http:// or https://"
    host = urlparse(url).netloc
    async with httpx.AsyncClient(timeout=25.0, headers=HEADERS, follow_redirects=True) as client:
        try:
            r = await client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            return f"Fetch failed: {e}"
    text = unescape(re.sub(r"<script[\s\S]*?</script>", "", r.text, flags=re.I))
    text = unescape(re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return f"Fetched {url} but no readable text."
    return f"From {host}:\n{text[:max_chars]}"
