from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ophelia.config import OPHELIA_HOME
from ophelia.memory.bootstrap import load_hermes_memories
from ophelia.providers.auth import import_hermes_auth_full, save_oauth_token
from ophelia.providers.oauth_refresh import load_oauth_state


@dataclass
class MigrationReport:
    copied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def migrate_from_hermes(
    hermes_home: Path,
    ophelia_home: Path = OPHELIA_HOME,
    *,
    dry_run: bool = False,
) -> MigrationReport:
    report = MigrationReport()
    hermes_home = hermes_home.expanduser().resolve()
    ophelia_home = ophelia_home.expanduser().resolve()

    if not hermes_home.is_dir():
        report.warnings.append(f"Hermes home not found: {hermes_home}")
        return report

    def _copy(src: Path, dst: Path) -> None:
        if not src.exists():
            report.skipped.append(f"missing: {src.name}")
            return
        if dry_run:
            report.copied.append(f"[dry-run] {src} → {dst}")
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                report.skipped.append(f"exists: {dst}")
            else:
                shutil.copytree(src, dst)
                report.copied.append(str(dst))
        else:
            if dst.exists() and dst.name == "SOUL.md":
                report.skipped.append(f"SOUL.md already at {dst}")
            else:
                shutil.copy2(src, dst)
                report.copied.append(str(dst))

    # Persona
    soul_src = hermes_home / "SOUL.md"
    _copy(soul_src, ophelia_home / "SOUL.md")

    # Memories → bootstrap files Ophelia loads at runtime
    mem_entries, user_entries = load_hermes_memories(hermes_home)
    if mem_entries or user_entries:
        mem_out = ophelia_home / "memories"
        if not dry_run:
            mem_out.mkdir(parents=True, exist_ok=True)
            if mem_entries:
                (mem_out / "MEMORY.md").write_text(
                    "\n§\n".join(mem_entries), encoding="utf-8"
                )
            if user_entries:
                (mem_out / "USER.md").write_text(
                    "\n§\n".join(user_entries), encoding="utf-8"
                )
        report.copied.append(
            f"memories: {len(mem_entries)} agent + {len(user_entries)} user entries"
        )

    # SuperGrok OAuth
    auth_src = hermes_home / "auth.json"
    if auth_src.is_file():
        if dry_run:
            report.copied.append("[dry-run] auth.json")
        else:
            if import_hermes_auth_full(auth_src, ophelia_home / "hermes_auth.json"):
                state = load_oauth_state(ophelia_home / "hermes_auth.json")
                if state:
                    save_oauth_token(
                        ophelia_home / "xai_oauth.json",
                        state["access_token"],
                        state.get("refresh_token"),
                    )
                    report.copied.append("xai-oauth token + refresh imported")
                else:
                    report.warnings.append("auth.json copied but no xai-oauth token found")
            else:
                report.warnings.append("auth.json present but could not parse xai-oauth")

    # Skills
    skills_src = hermes_home / "skills"
    if skills_src.is_dir():
        _copy(skills_src, ophelia_home / "skills" / "hermes-import")

    # Config hints → .env suggestions file
    cfg = hermes_home / "config.yaml"
    if cfg.is_file():
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            data = {}
        env_lines: list[str] = []
        model = data.get("model") or {}
        if isinstance(model, dict):
            if model.get("default"):
                env_lines.append(f"XAI_MODEL={model['default']}")
            if model.get("provider") in ("xai-oauth", "xai"):
                env_lines.append(f"OPHELIA_PROVIDER={model['provider']}")
        messaging = data.get("messaging") or data.get("telegram") or {}
        if isinstance(messaging, dict):
            users = messaging.get("allowed_users") or messaging.get("allowed_user_ids")
            if users:
                env_lines.append(f"TELEGRAM_ALLOWED_USER_IDS={','.join(map(str, users))}")
        if env_lines and not dry_run:
            hint = ophelia_home / "from-hermes.env"
            hint.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
            report.copied.append(f"env hints → {hint}")

    # Session DB (archive for search migration later)
    state_db = hermes_home / "state.db"
    if state_db.is_file():
        _copy(state_db, ophelia_home / "data" / "hermes_state.db")

    # Optional holographic / memory_store
    for extra in ("memory_store.db",):
        p = hermes_home / extra
        if p.is_file():
            _copy(p, ophelia_home / "data" / extra)

    honcho_src = hermes_home / "honcho.json"
    if honcho_src.is_file():
        _copy(honcho_src, ophelia_home / "honcho.json")

    # .env secrets (telegram etc.) — merge keys only if ophelia .env missing
    hermes_env = hermes_home / ".env"
    ophelia_env = ophelia_home / ".env"
    if hermes_env.is_file() and not ophelia_env.exists() and not dry_run:
        shutil.copy2(hermes_env, ophelia_env)
        report.copied.append(str(ophelia_env))

    report_path = ophelia_home / "migration_report.json"
    if not dry_run:
        report_path.write_text(
            json.dumps(
                {
                    "copied": report.copied,
                    "skipped": report.skipped,
                    "warnings": report.warnings,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return report


def format_report(report: MigrationReport) -> str:
    lines = ["Hermes → Ophelia migration", ""]
    if report.copied:
        lines.append("Copied / imported:")
        lines.extend(f"  + {x}" for x in report.copied)
    if report.skipped:
        lines.append("\nSkipped:")
        lines.extend(f"  - {x}" for x in report.skipped)
    if report.warnings:
        lines.append("\nWarnings:")
        lines.extend(f"  ! {x}" for x in report.warnings)
    return "\n".join(lines)
