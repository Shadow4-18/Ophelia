"""Honcho memory (optional) — reads Hermes honcho.json config."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import structlog

log = structlog.get_logger()


def load_honcho_config(
    ophelia_home: Path,
    hermes_home: Path,
) -> dict[str, Any] | None:
    candidates = [
        ophelia_home / "honcho.json",
        hermes_home / "honcho.json",
        Path.home() / ".honcho" / "config.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            continue
    return None


class HonchoClient:
    def __init__(self, config: dict[str, Any], api_key: str | None = None) -> None:
        self.base_url = (config.get("baseUrl") or "https://api.honcho.dev").rstrip("/")
        self.api_key = api_key or config.get("apiKey") or ""
        hosts = config.get("hosts") or {}
        hermes = hosts.get("hermes") or hosts.get("default") or {}
        self.workspace = hermes.get("workspace") or "hermes"
        self.ai_peer = hermes.get("aiPeer") or "hermes"
        self.user_peer = hermes.get("peerName") or "user"
        self.enabled = bool(hermes.get("enabled", True)) and bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def get_context(self, session_id: str, *, tokens: int = 2000) -> str:
        if not self.enabled:
            return ""
        url = (
            f"{self.base_url}/v3/workspaces/{self.workspace}/sessions/"
            f"{session_id}/context"
        )
        try:
            async with httpx.AsyncClient(timeout=30.0) as http:
                r = await http.get(
                    url,
                    headers=self._headers(),
                    params={
                        "tokens": tokens,
                        "peer_target": self.user_peer,
                        "summary": True,
                    },
                )
                if r.status_code == 404:
                    return ""
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            log.warning("honcho.context_failed", error=str(e))
            return ""

        parts: list[str] = []
        summary = data.get("summary")
        if summary:
            parts.append(f"Summary: {summary}")
        for msg in data.get("messages") or []:
            if isinstance(msg, dict):
                peer = msg.get("peer_id") or msg.get("role") or "?"
                content = msg.get("content") or ""
                parts.append(f"[{peer}] {content}")
        return "\n".join(parts)[:6000]

    async def save_turn(
        self,
        session_id: str,
        *,
        user_text: str,
        assistant_text: str,
    ) -> None:
        if not self.enabled:
            return
        url = (
            f"{self.base_url}/v3/workspaces/{self.workspace}/sessions/"
            f"{session_id}/messages"
        )
        body = {
            "messages": [
                {"peer_id": self.user_peer, "content": user_text},
                {"peer_id": self.ai_peer, "content": assistant_text},
            ]
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as http:
                r = await http.post(url, headers=self._headers(), json=body)
                r.raise_for_status()
        except Exception as e:
            log.warning("honcho.save_failed", error=str(e))
