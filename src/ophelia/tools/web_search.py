"""Web search with pluggable backends.

Default backend is DuckDuckGo (no API key, less reliable). For reliable
AI-friendly results, set an API key for Tavily, Serper, or Brave and select it
via OPHELIA_WEB_SEARCH_PROVIDER (or OPHELIA_WEB_SEARCH_PROVIDER=auto to pick
the first available key). All backends return a plain-text result blob the
agent can read; on failure they fall back to DuckDuckGo so a search never
hard-fails the turn.
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import quote_plus, urlparse

import httpx
import structlog

from ophelia.config import Settings

log = structlog.get_logger()

HEADERS = {
    "User-Agent": "Ophelia/1.0 (research; +https://github.com/Shadow4-18/Ophelia)"
}


async def search_web(query: str, max_results: int = 8, settings: Settings | None = None) -> str:
    query = query.strip()
    if not query:
        return "Empty query."

    provider = (
        settings.web_search_provider_resolved() if settings is not None else "duckduckgo"
    )

    if provider == "tavily" and settings is not None and settings.tavily_api_key:
        result = await _tavily_search(query, settings.tavily_api_key, max_results)
        if result:
            return result
        log.info("web_search.fallback", provider="tavily", reason="no result, falling back to ddg")

    if provider == "serper" and settings is not None and settings.serper_api_key:
        result = await _serper_search(query, settings.serper_api_key, max_results)
        if result:
            return result
        log.info("web_search.fallback", provider="serper", reason="no result, falling back to ddg")

    if provider == "brave" and settings is not None and settings.brave_api_key:
        result = await _brave_search(query, settings.brave_api_key, max_results)
        if result:
            return result
        log.info("web_search.fallback", provider="brave", reason="no result, falling back to ddg")

    return await _duckduckgo_search(query, max_results)


async def _tavily_search(query: str, api_key: str, max_results: int) -> str:
    """Tavily — AI-focused search API. Returns clean content snippets."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": api_key,
                    "query": query,
                    "max_results": max_results,
                    "search_depth": "basic",
                },
            )
            if r.status_code != 200:
                log.warning("web_search.tavily_error", status=r.status_code, body=r.text[:200])
                return ""
            data = r.json()
        lines: list[str] = []
        ans = (data.get("answer") or "").strip()
        if ans:
            lines.append(f"Answer: {ans}")
        for item in data.get("results") or []:
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            content = (item.get("content") or "").strip()
            if not title and not content:
                continue
            lines.append(f"- {title}\n  {url}\n  {content[:400]}")
        if not lines:
            return ""
        return f"Web search (Tavily): {query}\n\n" + "\n".join(lines[: max_results + 2])
    except (httpx.HTTPError, ValueError) as e:
        log.warning("web_search.tavily_exception", error=str(e))
        return ""


async def _serper_search(query: str, api_key: str, max_results: int) -> str:
    """Serper — Google search results API."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": query, "num": max_results},
            )
            if r.status_code != 200:
                log.warning("web_search.serper_error", status=r.status_code, body=r.text[:200])
                return ""
            data = r.json()
        lines: list[str] = []
        kg = data.get("knowledgeGraph") or {}
        if kg.get("description"):
            lines.append(f"Knowledge: {kg['description'][:500]}")
        for item in (data.get("organic") or [])[:max_results]:
            title = (item.get("title") or "").strip()
            link = (item.get("link") or "").strip()
            snippet = (item.get("snippet") or "").strip()
            if not title:
                continue
            lines.append(f"- {title}\n  {link}\n  {snippet[:300]}")
        if not lines:
            return ""
        return f"Web search (Serper/Google): {query}\n\n" + "\n".join(lines)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("web_search.serper_exception", error=str(e))
        return ""


async def _brave_search(query: str, api_key: str, max_results: int) -> str:
    """Brave Search API."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "X-Subscription-Token": api_key,
                    "Accept": "application/json",
                },
                params={"q": query, "count": max_results},
            )
            if r.status_code != 200:
                log.warning("web_search.brave_error", status=r.status_code, body=r.text[:200])
                return ""
            data = r.json()
        lines: list[str] = []
        for item in (data.get("web", {}).get("results") or [])[:max_results]:
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            desc = (item.get("description") or "").strip()
            if not title:
                continue
            lines.append(f"- {title}\n  {url}\n  {desc[:300]}")
        if not lines:
            return ""
        return f"Web search (Brave): {query}\n\n" + "\n".join(lines)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("web_search.brave_exception", error=str(e))
        return ""


async def _duckduckgo_search(query: str, max_results: int) -> str:
    lines: list[str] = []
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
