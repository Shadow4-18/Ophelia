"""Resolve xAI credentials: SuperGrok OAuth first, API key fallback."""

from __future__ import annotations

import shutil
from pathlib import Path

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
    if api_key and api_key.strip():
        return api_key.strip()
    for state in sources:
        if state and state.get("access_token"):
            return state["access_token"]
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
    save_oauth_state(
        path,
        {
            "access_token": access_token,
            "refresh_token": refresh_token or "",
            "client_id": "",
            "token_endpoint": "https://auth.x.ai/oauth2/token",
        },
    )
