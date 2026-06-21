"""Versioned self-modification of persona (SOUL.md) and policy (PROMPTER.md).

Every edit is backed up to ~/.ophelia/versions/ with a timestamp so the AI —
or a human — can revert a bad self-edit. This is the controlled surface for
Ophelia rewriting her own personality and idle policy.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import structlog

from ophelia.config import OPHELIA_HOME

log = structlog.get_logger()

VERSIONS_DIR = OPHELIA_HOME / "versions"
SOUL_PATH = OPHELIA_HOME / "SOUL.md"
PROMPTER_PATH = OPHELIA_HOME / "PROMPTER.md"

_REWRITE_LOG = OPHELIA_HOME / "data" / "self_rewrite_log.jsonl"


def _backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = VERSIONS_DIR / f"{path.stem}-{ts}{path.suffix}"
    shutil.copy2(path, bak)
    return bak


def _log_rewrite(target: str, reason: str, backup: Path | None) -> None:
    import json

    _REWRITE_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.time(),
        "target": target,
        "reason": reason,
        "backup": str(backup) if backup else None,
    }
    with _REWRITE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def rewrite_soul(content: str, reason: str = "") -> str:
    """Overwrite SOUL.md with new content, backing up the previous version."""
    backup = _backup(SOUL_PATH)
    SOUL_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOUL_PATH.write_text(content, encoding="utf-8")
    _log_rewrite("SOUL.md", reason, backup)
    log.info("self_rewrite.soul", reason=reason, backup=str(backup) if backup else None)
    return f"SOUL.md rewritten. Previous version backed up to {backup}." if backup else "SOUL.md created (no previous version)."


def rewrite_prompter(content: str, reason: str = "") -> str:
    """Overwrite PROMPTER.md with new content, backing up the previous version."""
    backup = _backup(PROMPTER_PATH)
    PROMPTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROMPTER_PATH.write_text(content, encoding="utf-8")
    _log_rewrite("PROMPTER.md", reason, backup)
    log.info("self_rewrite.prompter", reason=reason, backup=str(backup) if backup else None)
    return f"PROMPTER.md rewritten. Previous version backed up to {backup}." if backup else "PROMPTER.md created (no previous version)."


def list_versions(target: str = "") -> list[Path]:
    """List available backups, optionally filtered by target stem (e.g. 'SOUL')."""
    if not VERSIONS_DIR.is_dir():
        return []
    items = sorted(VERSIONS_DIR.iterdir(), reverse=True)
    if target:
        stem = target.replace(".md", "").upper()
        items = [p for p in items if p.stem.upper().startswith(stem)]
    return items


def revert(path_name: str, version: str) -> str:
    """Restore a named file from a backed-up version."""
    target = {
        "soul": SOUL_PATH,
        "SOUL": SOUL_PATH,
        "SOUL.md": SOUL_PATH,
        "prompter": PROMPTER_PATH,
        "PROMPTER": PROMPTER_PATH,
        "PROMPTER.md": PROMPTER_PATH,
    }.get(path_name)
    if not target:
        return f"Unknown target: {path_name}"
    bak = VERSIONS_DIR / version
    if not bak.is_file():
        return f"No backup named {version} in {VERSIONS_DIR}"
    shutil.copy2(bak, target)
    _log_rewrite(f"revert {path_name}", f"restored {version}", bak)
    return f"Restored {path_name} from {version}."
