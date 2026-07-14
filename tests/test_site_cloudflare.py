"""Tests for Cloudflare Pages deploy helpers + site_deploy readiness."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from ophelia.site.cloudflare import (
    CloudflarePagesError,
    collect_files,
    deploy_directory,
    deploy_ready,
)


def test_deploy_ready_missing_all():
    st = deploy_ready(account_id=None, api_token=None, project=None)
    assert st["ready"] is False
    assert "CLOUDFLARE_API_TOKEN" in st["missing"]
    assert "CLOUDFLARE_ACCOUNT_ID" in st["missing"]
    assert "OPHELIA_SITE_CF_PROJECT" in st["missing"]


def test_deploy_ready_when_configured(monkeypatch):
    monkeypatch.setattr(
        "ophelia.site.cloudflare.shutil.which",
        lambda _name: "/usr/bin/wrangler",
    )
    st = deploy_ready(
        account_id="acct",
        api_token="tok",
        project="ophelia-site",
    )
    assert st["ready"] is True
    assert st["project"] == "ophelia-site"
    assert st["missing"] == []


def test_collect_files(tmp_path: Path):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    sub = tmp_path / "p"
    sub.mkdir()
    (sub / "hi.html").write_text("hi", encoding="utf-8")
    (tmp_path / ".hidden").write_text("nope", encoding="utf-8")
    files = collect_files(tmp_path)
    assert "/index.html" in files
    assert "/p/hi.html" in files
    assert "/.hidden" not in files


def test_deploy_directory_requires_creds(tmp_path: Path):
    (tmp_path / "index.html").write_text("x", encoding="utf-8")
    with pytest.raises(CloudflarePagesError, match="CLOUDFLARE"):
        deploy_directory(
            tmp_path,
            account_id="",
            api_token="",
            project="",
        )


def test_deploy_via_api_mocked(tmp_path: Path, monkeypatch):
    (tmp_path / "index.html").write_text("<h1>Ophelia</h1>", encoding="utf-8")
    (tmp_path / "site.css").write_text("body{}", encoding="utf-8")

    # Force API path (blake3 present in this env from earlier install; still ok).
    monkeypatch.setattr(
        "ophelia.site.cloudflare.shutil.which",
        lambda _name: None,
    )

    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        url = str(request.url)
        if request.method == "GET" and url.endswith("/projects/ophelia-site"):
            return httpx.Response(200, json={"success": True, "result": {"name": "ophelia-site"}})
        if request.method == "GET" and url.endswith("/upload-token"):
            return httpx.Response(200, json={"success": True, "result": {"jwt": "upload-jwt"}})
        if url.endswith("/pages/assets/check-missing"):
            body = json.loads(request.content.decode())
            return httpx.Response(200, json={"success": True, "result": body["hashes"]})
        if url.endswith("/pages/assets/upload"):
            return httpx.Response(200, json={"success": True, "result": True})
        if url.endswith("/pages/assets/upsert-hashes"):
            return httpx.Response(200, json={"success": True, "result": True})
        if request.method == "POST" and url.endswith("/deployments"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {"url": "https://ophelia-site.pages.dev", "id": "dep1"},
                },
            )
        return httpx.Response(404, json={"success": False, "errors": [{"message": url}]})

    transport = httpx.MockTransport(handler)

    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    with patch("ophelia.site.cloudflare.httpx.Client", side_effect=client_factory):
        result = deploy_directory(
            tmp_path,
            account_id="acct123",
            api_token="tok",
            project="ophelia-site",
            branch="main",
        )

    assert result.method == "api"
    assert result.project == "ophelia-site"
    assert result.url == "https://ophelia-site.pages.dev"
    assert result.files == 2
    assert result.uploaded == 2
    assert any(u.endswith("/deployments") for _, u in calls)


@pytest.mark.asyncio
async def test_site_deploy_tool_reports_missing_creds(isolated_env, settings):
    # Import session first to break the tools↔channels circular import.
    from ophelia.channels.session import ChannelSession  # noqa: F401
    from ophelia.tools.registry import ToolRegistry

    settings.cloudflare_api_token = None
    settings.cloudflare_account_id = None
    settings.site_cf_project = None
    tools = ToolRegistry(settings, isolated_env / "artifacts")
    tools.set_owner(True)
    out = await tools.dispatch("site_deploy", "{}")
    assert "not configured" in out
    assert "CLOUDFLARE_API_TOKEN" in out
