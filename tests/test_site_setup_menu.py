"""Tests for public-site setup menu helpers."""

from __future__ import annotations

from pathlib import Path

from ophelia.setup.env_io import read_env_key, write_env_updates
from ophelia.setup.interactive import _normalize_site_public_url
from ophelia.setup.wizard import _check_public_site


def test_normalize_site_public_url():
    assert _normalize_site_public_url("") == ""
    assert _normalize_site_public_url("  ") == ""
    assert _normalize_site_public_url("ophelia.example.com") == "https://ophelia.example.com"
    assert (
        _normalize_site_public_url("https://ophelia.example.com/")
        == "https://ophelia.example.com"
    )
    assert (
        _normalize_site_public_url("http://localhost:8788") == "http://localhost:8788"
    )


def test_setup_menu_lists_public_site():
    src = Path("src/ophelia/setup/interactive.py").read_text(encoding="utf-8")
    assert "Public site / Cloudflare Pages" in src
    assert "_section_public_site" in src
    assert "CLOUDFLARE_API_TOKEN" in src
    assert "OPHELIA_SITE_CF_PROJECT" in src


def test_check_public_site_incomplete(isolated_env, monkeypatch):
    monkeypatch.setenv("OPHELIA_HOME", str(isolated_env))
    (isolated_env / ".env").write_text(
        "CLOUDFLARE_ACCOUNT_ID=acct\n",
        encoding="utf-8",
    )
    ok, msg = _check_public_site()
    assert ok is False
    assert "incomplete" in msg or "missing" in msg


def test_check_public_site_ready(isolated_env, monkeypatch):
    monkeypatch.setenv("OPHELIA_HOME", str(isolated_env))
    monkeypatch.setattr(
        "ophelia.site.cloudflare.shutil.which",
        lambda _name: "/usr/bin/wrangler",
    )
    write_env_updates(
        {
            "OPHELIA_SITE_PUBLIC_URL": "https://ophelia.example.com",
            "CLOUDFLARE_API_TOKEN": "tok",
            "CLOUDFLARE_ACCOUNT_ID": "acct",
            "OPHELIA_SITE_CF_PROJECT": "ophelia-site",
        }
    )
    assert read_env_key("OPHELIA_SITE_CF_PROJECT") == "ophelia-site"
    ok, msg = _check_public_site()
    assert ok is True
    assert "ophelia.example.com" in msg
