"""See a web page the way a visitor does: a real browser screenshot.

Phones (Termux) can't run a headless browser, so this asks microlink.io's
free screenshot API (no key, ~50 pages/day) to render the page at a given
viewport width, downloads the PNG, and lets her vision model look at it.
Widths: phone (412), phone at 50% zoom (824), tablet (820), desktop (1366).
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import structlog

log = structlog.get_logger()

API = "https://api.microlink.io/"
WIDTHS = {"phone": 412, "phone-50": 824, "tablet": 820, "desktop": 1366}


def resolve_width(width: str | int | None) -> int:
    if isinstance(width, (int, float)) and width:
        return max(320, min(2560, int(width)))
    key = str(width or "phone").strip().lower().replace("%", "").replace(" ", "-")
    if key.isdigit():
        return max(320, min(2560, int(key)))
    return WIDTHS.get(key, WIDTHS.get(key.replace("zoom", "").strip("-"), 412))


async def screenshot(url: str, out_dir: Path, *, width: int = 412,
                     full_page: bool = False) -> Path:
    """Render ``url`` at ``width`` CSS px and save the PNG. Raises on failure."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    mobile = width < 900
    params = {
        "url": url,
        "screenshot": "true",
        "meta": "false",
        "force": "true",  # no cached render: she's usually checking a fresh deploy
        "viewport.width": str(width),
        "viewport.height": "900" if mobile else "800",
        "viewport.isMobile": "true" if mobile else "false",
        "viewport.deviceScaleFactor": "1",
    }
    if full_page:
        params["screenshot.fullPage"] = "true"
    async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as http:
        r = await http.get(API, params=params)
        body = r.json() if r.content else {}
        if r.status_code >= 400 or body.get("status") != "success":
            msg = body.get("message") or r.text[:200]
            raise RuntimeError(f"screenshot service said: {msg}")
        shot_url = ((body.get("data") or {}).get("screenshot") or {}).get("url")
        if not shot_url:
            raise RuntimeError("screenshot service returned no image")
        img = await http.get(shot_url)
        img.raise_for_status()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"page_{int(time.time())}_{width}.png"
    out.write_bytes(img.content)
    log.info("web_view.saved", url=url, width=width, path=str(out), bytes=len(img.content))
    return out
