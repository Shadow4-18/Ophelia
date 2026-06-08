"""Free temporary cloud upload/download (transfer.sh)."""

from __future__ import annotations

from pathlib import Path

import httpx

TRANSFER_SH = "https://transfer.sh"
DEFAULT_FILENAME = "ophelia-hermes-bundle.tar.gz"


async def cloud_upload(path: Path, *, filename: str = DEFAULT_FILENAME) -> str:
    """Upload bundle to transfer.sh; returns download URL (free, ~14 days)."""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)

    url = f"{TRANSFER_SH}/{filename}"
    async with httpx.AsyncClient(timeout=600.0, follow_redirects=True) as client:
        with path.open("rb") as f:
            resp = await client.put(url, content=f.read())
        resp.raise_for_status()
    link = resp.text.strip().splitlines()[0].strip()
    if not link.startswith("http"):
        raise RuntimeError(f"Unexpected upload response: {link[:200]}")
    return link


async def cloud_download(url: str, dest: Path) -> Path:
    """Download bundle from a transfer.sh (or any direct) URL."""
    dest = dest.expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=600.0, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with dest.open("wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    f.write(chunk)
    return dest
