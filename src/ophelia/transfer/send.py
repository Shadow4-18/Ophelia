"""Send Hermes bundle to receive server or cloud."""

from __future__ import annotations

from pathlib import Path

import httpx

from ophelia.transfer.bundle import create_hermes_bundle


async def send_bundle(
    target_url: str,
    *,
    hermes_home: Path,
    token: str | None = None,
    keep_bundle: Path | None = None,
) -> int:
    bundle = create_hermes_bundle(hermes_home, dest=keep_bundle)
    size = bundle.stat().st_size
    print(f"Bundle: {bundle} ({size // 1024} KB)")

    url = target_url.rstrip("/") + "/upload"
    headers = {}
    if token:
        headers["X-Ophelia-Token"] = token

    async with httpx.AsyncClient(timeout=600.0) as client:
        with bundle.open("rb") as f:
            resp = await client.post(
                url,
                files={"file": (bundle.name, f, "application/gzip")},
                headers=headers,
            )
        resp.raise_for_status()

    print(f"Uploaded OK ({size} bytes)")
    if not keep_bundle:
        bundle.unlink(missing_ok=True)
    return size
