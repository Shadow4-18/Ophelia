"""Read and update ~/.ophelia/.env without manual editing."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ophelia.config import OPHELIA_HOME

_ENV_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def env_path() -> Path:
    """Resolve the live .env path, honoring OPHELIA_HOME if set after import."""
    home = os.environ.get("OPHELIA_HOME")
    if home:
        return Path(home).expanduser() / ".env"
    return OPHELIA_HOME / ".env"


def read_env_key(key: str) -> str:
    path = env_path()
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return _unquote(v.strip())
    return ""


def read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    path = env_path()
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = _unquote(v.strip())
    return out


def write_env_updates(updates: dict[str, str | None]) -> list[str]:
    """Merge keys into .env. None values remove the key line."""
    path = env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    touched: list[str] = []
    remaining = dict(updates)

    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            val = remaining.pop(key)
            touched.append(key)
            if val is not None and val != "":
                new_lines.append(f"{key}={_quote(val)}")
            continue
        new_lines.append(line)

    for key, val in remaining.items():
        touched.append(key)
        if val is not None and val != "":
            new_lines.append(f"{key}={_quote(val)}")

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return touched


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _quote(value: str) -> str:
    if re.search(r"[\s#\"']", value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value
