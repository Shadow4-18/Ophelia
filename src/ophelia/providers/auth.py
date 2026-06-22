"""Resolve xAI credentials: SuperGrok OAuth first, API key fallback."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ophelia.platform import is_termux

from ophelia.providers.oauth_refresh import (
    load_oauth_state,
    oauth_auth_paths,
    parse_xai_oauth_state,
    save_oauth_state,
)


def _read_json(path: Path):
    from ophelia.providers.oauth_refresh import _read_json as read

    return read(path)


def token_from_grok_cli(path: Path) -> str | None:
    data = _read_json(path)
    state = parse_xai_oauth_state(data) if data else None
    return state.get("access_token") if state else None


def token_from_oauth_cache(path: Path) -> str | None:
    state = load_oauth_state(path)
    return state.get("access_token") if state else None


def token_from_hermes_auth(path: Path) -> str | None:
    state = load_oauth_state(path)
    return state.get("access_token") if state else None


def resolve_xai_bearer(
    *,
    api_key: str | None,
    oauth_path: Path,
    grok_cli_path: Path,
    hermes_auth_path: Path,
    hermes_home: Path | None = None,
    prefer_oauth: bool = True,
) -> str | None:
    """Resolve an xAI bearer token.

    prefer_oauth=True  -> xai-oauth mode: OAuth access token first, API key
                          as a last-resort fallback (kept for compatibility
                          with setups that only have a key).
    prefer_oauth=False -> xai mode: API key ONLY. Does NOT fall back to OAuth,
                          because SuperGrok OAuth tokens are a different tier
                          and may not have access to the same models — silently
                          using OAuth when the user asked for an API key causes
                          cryptic 400s at runtime. Return None and let the
                          caller report the missing key clearly.
    """
    sources: list[dict | None] = []
    for path in oauth_auth_paths(
        hermes_home=hermes_home,
        hermes_auth_path=hermes_auth_path,
        oauth_path=oauth_path,
    ):
        sources.append(load_oauth_state(path))
    sources.append(parse_xai_oauth_state(_read_json(grok_cli_path) or {}))
    if prefer_oauth:
        for state in sources:
            if state and state.get("access_token"):
                return state["access_token"]
        if api_key and api_key.strip():
            return api_key.strip()
        return None
    # xai (API key) mode — strict, no OAuth fallback.
    if api_key and api_key.strip():
        return api_key.strip()
    return None


def import_hermes_auth_full(hermes_auth: Path, ophelia_auth: Path) -> bool:
    if not hermes_auth.is_file():
        return False
    ophelia_auth.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(hermes_auth, ophelia_auth)
    state = load_oauth_state(ophelia_auth)
    if state and state.get("access_token"):
        save_oauth_state(ophelia_auth, state)
    return bool(state and state.get("access_token"))


def hermes_xai_oauth_login_argv() -> list[str]:
    cmd = ["hermes", "auth", "add", "xai-oauth", "--type", "oauth"]
    if is_termux():
        cmd.append("--no-browser")
    return cmd


def run_hermes_xai_oauth_login() -> int:
    """Run Hermes browser OAuth; on Termux use manual callback (--no-browser)."""
    hermes = shutil.which("hermes")
    if not hermes:
        return 127
    argv = hermes_xai_oauth_login_argv()
    argv[0] = hermes
    return subprocess.run(argv).returncode


def print_termux_oauth_login_help() -> None:
    print("Termux tip: Android browser often cannot callback to 127.0.0.1:56121.")
    print("Use --no-browser (already set) — open the URL Hermes prints, sign in,")
    print("then paste the FULL redirect URL back into Termux when prompted.")
    print()
    print("If you have stale Hermes credentials, clear first:")
    print("  hermes auth logout xai-oauth")


def sync_oauth_from_hermes_home(
    hermes_home: Path,
    *,
    ophelia_auth_path: Path,
    ophelia_oauth_path: Path,
) -> tuple[bool, str]:
    """Copy live ~/.hermes/auth.json into Ophelia's auth stores."""
    auth = hermes_home.expanduser() / "auth.json"
    if not auth.is_file():
        return False, f"No {auth} — run: hermes auth add xai-oauth"
    if not import_hermes_auth_full(auth, ophelia_auth_path):
        return False, "auth.json found but no xai-oauth tokens inside"
    state = load_oauth_state(ophelia_auth_path)
    if state:
        save_oauth_token(
            ophelia_oauth_path,
            state["access_token"],
            state.get("refresh_token"),
        )
    return True, f"Synced xAI OAuth from {auth} -> Ophelia"


def save_oauth_token(path: Path, access_token: str, refresh_token: str | None = None) -> None:
    existing = load_oauth_state(path) or {}
    save_oauth_state(
        path,
        {
            "access_token": access_token,
            "refresh_token": (
                refresh_token
                if refresh_token is not None
                else existing.get("refresh_token", "")
            ),
            "client_id": existing.get("client_id") or "",
            "token_endpoint": existing.get("token_endpoint")
            or "https://auth.x.ai/oauth2/token",
        },
    )
