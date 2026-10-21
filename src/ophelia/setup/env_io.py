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
    """Value of ``key`` as Ophelia loads it: if the key appears more than
    once, the last line wins (python-dotenv's rule)."""
    path = env_path()
    if not path.is_file():
        return ""
    found = ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if _name(k) == key:
            found = _unquote(v.strip())
    return found


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
        out[_name(k)] = _unquote(v.strip())
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
    written: set[str] = set()

    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        key = _name(stripped.split("=", 1)[0])
        if key in written:
            # A duplicate of a key we just set. The last line wins when
            # Ophelia loads .env, so leaving it would silently keep the old
            # value (a replaced API key that "still fails").
            continue
        if key in remaining:
            val = remaining.pop(key)
            touched.append(key)
            written.add(key)
            if val is not None and val != "":
                new_lines.append(f"{key}={_quote(val)}")
            continue
        new_lines.append(line)

    for key, val in remaining.items():
        if val is not None and val != "":
            touched.append(key)
            new_lines.append(f"{key}={_quote(val)}")

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return touched


def _name(raw: str) -> str:
    """Key name of a .env line's left side (dotenv allows `export KEY=`)."""
    raw = raw.strip()
    return raw[len("export "):].strip() if raw.startswith("export ") else raw


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == '"':
            # Undo _quote's escaping (python-dotenv reads it the same way);
            # without this every re-save doubled the backslashes.
            inner = re.sub(r'\\([\\"])', r"\1", inner)
        return inner
    return value


def _quote(value: str) -> str:
    if re.search(r"[\s#\"']", value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value
