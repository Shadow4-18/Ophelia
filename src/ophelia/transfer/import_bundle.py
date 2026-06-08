"""Import a Hermes bundle tarball into ~/.ophelia."""

from __future__ import annotations

import shutil
import tarfile
import tempfile
from pathlib import Path

from ophelia.config import OPHELIA_HOME
from ophelia.migration.hermes import format_report, migrate_from_hermes
from ophelia.providers.auth import import_hermes_auth_full, save_oauth_token
from ophelia.providers.oauth_refresh import load_oauth_state


def extract_bundle(bundle: Path) -> Path:
    bundle = bundle.expanduser().resolve()
    if not bundle.is_file():
        raise FileNotFoundError(bundle)

    tmp = Path(tempfile.mkdtemp(prefix="ophelia-import-"))
    with tarfile.open(bundle, "r:gz") as tar:
        try:
            tar.extractall(tmp, filter="data")
        except TypeError:
            tar.extractall(tmp)

    hermes = tmp / "hermes"
    if not hermes.is_dir():
        # flat tarball fallback
        if (tmp / "SOUL.md").is_file() or (tmp / "auth.json").is_file():
            hermes = tmp
        else:
            raise RuntimeError("Invalid bundle: expected hermes/ directory")

    return hermes


def import_bundle(
    bundle: Path,
    *,
    ophelia_home: Path = OPHELIA_HOME,
    dry_run: bool = False,
) -> str:
    hermes_src = extract_bundle(bundle)
    report = migrate_from_hermes(hermes_src, ophelia_home, dry_run=dry_run)

    if not dry_run:
        auth = hermes_src / "auth.json"
        if auth.is_file():
            import_hermes_auth_full(auth, ophelia_home / "hermes_auth.json")
            state = load_oauth_state(ophelia_home / "hermes_auth.json")
            if state:
                save_oauth_token(
                    ophelia_home / "xai_oauth.json",
                    state["access_token"],
                    state.get("refresh_token"),
                )

    # cleanup extract temp
    shutil.rmtree(hermes_src.parent, ignore_errors=True)
    return format_report(report)
