"""Deploy Ophelia's static site export to Cloudflare Pages (Direct Upload).

Talks to the same undocumented asset endpoints wrangler uses:
check-missing → upload → upsert-hashes → create deployment.
Falls back to the ``wrangler`` / ``npx wrangler`` CLI when blake3 is missing.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

API = "https://api.cloudflare.com/client/v4"
MAX_FILE_SIZE = 25 * 1024 * 1024
MAX_FILES = 20_000
BATCH_BYTES = 30 * 1024 * 1024
BATCH_FILES = 500
RETRY_DELAYS = (2.0, 4.0, 8.0)
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
SPECIAL_FILES = ("_headers", "_redirects")
USER_AGENT = "ophelia-site-deploy/0.2 (+https://github.com/Shadow4-18/Ophelia)"

ProgressFn = Callable[[str], None]


class CloudflarePagesError(Exception):
    """Expected deploy failure (auth, missing project, API, or local tools)."""


@dataclass
class DeployResult:
    url: str
    files: int
    unique: int
    uploaded: int
    duration: float
    method: str  # "api" | "wrangler"
    project: str


def _noop(_msg: str) -> None:
    pass


def _blake3_hash(data: bytes, suffix: str) -> str:
    """Wrangler-compatible content hash: blake3(base64(data) + suffix)[:32]."""
    try:
        from blake3 import blake3
    except ImportError as e:
        raise CloudflarePagesError(
            "blake3 is required for API deploy. Install with: pip install blake3 "
            "(or install Node wrangler so Ophelia can fall back to the CLI)."
        ) from e
    b64 = base64.b64encode(data).decode()
    return blake3((b64 + suffix).encode()).hexdigest()[:32]


def collect_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(n for n in dirnames if not n.startswith("."))
        d = Path(dirpath)
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            p = d / name
            if not p.is_file():
                continue
            if p.stat().st_size > MAX_FILE_SIZE:
                raise CloudflarePagesError(
                    f"{p} exceeds the {MAX_FILE_SIZE // (1024 * 1024)} MiB Pages limit"
                )
            rel = p.relative_to(root).as_posix()
            files["/" + rel] = p
    if not files:
        raise CloudflarePagesError(f"no files to deploy under {root}")
    if len(files) > MAX_FILES:
        raise CloudflarePagesError(
            f"{len(files)} files exceed the {MAX_FILES} files-per-deployment limit"
        )
    return files


def _request(client: httpx.Client, method: str, url: str, **kw: Any) -> httpx.Response:
    last = ""
    for delay in (*RETRY_DELAYS, None):
        try:
            resp = client.request(method, url, **kw)
        except httpx.TransportError as exc:
            last = f"network error: {exc}"
        else:
            if resp.status_code not in RETRY_STATUS:
                return resp
            last = f"HTTP {resp.status_code}"
        if delay is None:
            break
        time.sleep(delay)
    raise CloudflarePagesError(f"{last} after retries: {method} {url}")


def _ok(resp: httpx.Response) -> Any:
    try:
        body = resp.json()
    except ValueError as e:
        raise CloudflarePagesError(
            f"non-JSON response (HTTP {resp.status_code}): {resp.request.url}"
        ) from e
    if not body.get("success"):
        raise CloudflarePagesError(
            f"Cloudflare API error: {resp.request.url}\n"
            f"{json.dumps(body.get('errors'), ensure_ascii=False)}"
        )
    return body.get("result")


def deploy_ready(
    *,
    account_id: str | None,
    api_token: str | None,
    project: str | None,
) -> dict[str, Any]:
    """Describe whether Cloudflare deploy credentials are configured."""
    account = (account_id or "").strip()
    token = (api_token or "").strip()
    proj = (project or "").strip()
    has_wrangler = bool(shutil.which("wrangler") or shutil.which("npx"))
    try:
        import blake3  # noqa: F401

        has_blake3 = True
    except ImportError:
        has_blake3 = False
    ok = bool(account and token and proj and (has_blake3 or has_wrangler))
    missing: list[str] = []
    if not token:
        missing.append("CLOUDFLARE_API_TOKEN")
    if not account:
        missing.append("CLOUDFLARE_ACCOUNT_ID")
    if not proj:
        missing.append("OPHELIA_SITE_CF_PROJECT")
    if account and token and proj and not has_blake3 and not has_wrangler:
        missing.append("blake3 (pip install blake3) or wrangler CLI")
    return {
        "ready": ok,
        "account_id_set": bool(account),
        "api_token_set": bool(token),
        "project": proj or None,
        "blake3": has_blake3,
        "wrangler_available": has_wrangler,
        "missing": missing,
    }


def _deploy_via_api(
    root: Path,
    *,
    account_id: str,
    api_token: str,
    project: str,
    branch: str,
    create_project: bool,
    on_progress: ProgressFn,
) -> DeployResult:
    started = time.monotonic()
    files = collect_files(root)
    special: dict[str, bytes] = {}
    for name in SPECIAL_FILES:
        p = files.pop("/" + name, None)
        if p is not None:
            special[name] = p.read_bytes()

    manifest: dict[str, str] = {}
    by_hash: dict[str, Path] = {}
    for url_path, p in files.items():
        h = _blake3_hash(p.read_bytes(), p.suffix.lstrip("."))
        manifest[url_path] = h
        by_hash[h] = p
    on_progress(f"{len(files)} files ({len(by_hash)} unique)")

    headers = {"Authorization": f"Bearer {api_token}", "User-Agent": USER_AGENT}
    base = f"{API}/accounts/{account_id}/pages"
    with httpx.Client(timeout=120.0, headers=headers) as client:
        resp = _request(client, "GET", f"{base}/projects/{project}")
        exists = resp.status_code == 200 and resp.json().get("success", False)
        if not exists:
            if not create_project:
                raise CloudflarePagesError(f"no such Pages project: {project}")
            _ok(
                _request(
                    client,
                    "POST",
                    f"{base}/projects",
                    json={"name": project, "production_branch": "main"},
                )
            )
            on_progress(f"created project: {project}")

        jwt = _ok(_request(client, "GET", f"{base}/projects/{project}/upload-token"))["jwt"]

    upload_headers = {"Authorization": f"Bearer {jwt}", "User-Agent": USER_AGENT}
    with httpx.Client(timeout=300.0, headers=upload_headers) as client:
        missing = _ok(
            _request(
                client,
                "POST",
                f"{API}/pages/assets/check-missing",
                json={"hashes": list(by_hash)},
            )
        )
        on_progress(f"uploading {len(missing)} / {len(by_hash)} new files")

        batch: list[dict[str, Any]] = []
        size = 0
        for h in missing:
            p = by_hash[h]
            data = p.read_bytes()
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            batch.append(
                {
                    "key": h,
                    "value": base64.b64encode(data).decode(),
                    "metadata": {"contentType": ctype},
                    "base64": True,
                }
            )
            size += len(data)
            if size >= BATCH_BYTES or len(batch) >= BATCH_FILES:
                _ok(_request(client, "POST", f"{API}/pages/assets/upload", json=batch))
                on_progress(f"  sent {len(batch)} files")
                batch, size = [], 0
        if batch:
            _ok(_request(client, "POST", f"{API}/pages/assets/upload", json=batch))
            on_progress(f"  sent {len(batch)} files")

        _ok(
            _request(
                client,
                "POST",
                f"{API}/pages/assets/upsert-hashes",
                json={"hashes": list(by_hash)},
            )
        )

    form_files: dict[str, Any] = {"manifest": (None, json.dumps(manifest))}
    for name, content in special.items():
        form_files[name] = (name, content)
    with httpx.Client(timeout=120.0, headers=headers) as client:
        result = _ok(
            _request(
                client,
                "POST",
                f"{base}/projects/{project}/deployments",
                data={"branch": branch},
                files=form_files,
            )
        )
    url = (result or {}).get("url") or ""
    on_progress(f"deployed: {url or '(no URL in response)'}")
    return DeployResult(
        url=url,
        files=len(files),
        unique=len(by_hash),
        uploaded=len(missing),
        duration=time.monotonic() - started,
        method="api",
        project=project,
    )


def _deploy_via_wrangler(
    root: Path,
    *,
    account_id: str,
    api_token: str,
    project: str,
    branch: str,
    on_progress: ProgressFn,
) -> DeployResult:
    started = time.monotonic()
    env = os.environ.copy()
    env["CLOUDFLARE_API_TOKEN"] = api_token
    env["CLOUDFLARE_ACCOUNT_ID"] = account_id
    wrangler = shutil.which("wrangler")
    if wrangler:
        cmd = [wrangler, "pages", "deploy", str(root), "--project-name", project, "--branch", branch]
    else:
        npx = shutil.which("npx")
        if not npx:
            raise CloudflarePagesError("neither wrangler nor npx found on PATH")
        cmd = [
            npx,
            "--yes",
            "wrangler",
            "pages",
            "deploy",
            str(root),
            "--project-name",
            project,
            "--branch",
            branch,
        ]
    on_progress(f"running: {' '.join(cmd)}")
    import subprocess

    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    if proc.returncode != 0:
        raise CloudflarePagesError(f"wrangler failed ({proc.returncode}):\n{out[-2000:]}")
    url = ""
    for line in out.splitlines():
        low = line.lower()
        if "https://" in low and ("pages.dev" in low or "deploy" in low or "visit" in low):
            for part in line.split():
                if part.startswith("https://"):
                    url = part.strip().rstrip(".,)")
                    break
        if url:
            break
    n_files = len(collect_files(root))
    on_progress(f"deployed via wrangler: {url or 'ok'}")
    return DeployResult(
        url=url,
        files=n_files,
        unique=n_files,
        uploaded=n_files,
        duration=time.monotonic() - started,
        method="wrangler",
        project=project,
    )


def deploy_directory(
    directory: str | Path,
    *,
    account_id: str,
    api_token: str,
    project: str,
    branch: str = "main",
    create_project: bool = False,
    on_progress: ProgressFn | None = None,
) -> DeployResult:
    """Upload ``directory`` to a Cloudflare Pages project."""
    progress = on_progress or _noop
    root = Path(directory)
    if not root.is_dir():
        raise CloudflarePagesError(f"not a directory: {root}")
    account = account_id.strip()
    token = api_token.strip()
    proj = project.strip()
    if not account or not token or not proj:
        raise CloudflarePagesError(
            "set CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, and OPHELIA_SITE_CF_PROJECT"
        )

    try:
        import blake3  # noqa: F401

        has_blake3 = True
    except ImportError:
        has_blake3 = False

    if has_blake3:
        return _deploy_via_api(
            root,
            account_id=account,
            api_token=token,
            project=proj,
            branch=branch,
            create_project=create_project,
            on_progress=progress,
        )
    if shutil.which("wrangler") or shutil.which("npx"):
        return _deploy_via_wrangler(
            root,
            account_id=account,
            api_token=token,
            project=proj,
            branch=branch,
            on_progress=progress,
        )
    raise CloudflarePagesError(
        "Need blake3 (pip install blake3) or the wrangler CLI to deploy to Cloudflare Pages."
    )


async def deploy_directory_async(
    directory: str | Path,
    *,
    account_id: str,
    api_token: str,
    project: str,
    branch: str = "main",
    create_project: bool = False,
) -> DeployResult:
    return await asyncio.to_thread(
        deploy_directory,
        directory,
        account_id=account_id,
        api_token=api_token,
        project=project,
        branch=branch,
        create_project=create_project,
    )
