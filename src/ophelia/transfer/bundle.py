"""Create Hermes transfer bundles."""

from __future__ import annotations

import tarfile
import tempfile
from pathlib import Path

HERMES_ITEMS = (
    "SOUL.md",
    "auth.json",
    "config.yaml",
    ".env",
    "memories",
    "skills",
    "state.db",
    "honcho.json",
    "memory_store.db",
)


def create_hermes_bundle(
    hermes_home: Path,
    dest: Path | None = None,
) -> Path:
    """Pack ~/.hermes into ophelia-hermes-bundle.tar.gz layout."""
    hermes_home = hermes_home.expanduser().resolve()
    if not hermes_home.is_dir():
        raise FileNotFoundError(f"Hermes home not found: {hermes_home}")

    if dest is None:
        out = Path(tempfile.gettempdir()) / "ophelia-hermes-bundle.tar.gz"
    else:
        out = dest.expanduser().resolve()

    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "hermes"
        staging.mkdir()
        copied = 0
        for name in HERMES_ITEMS:
            src = hermes_home / name
            if not src.exists():
                continue
            dst = staging / name
            if src.is_dir():
                import shutil

                shutil.copytree(src, dst)
            else:
                import shutil

                shutil.copy2(src, dst)
            copied += 1

        if copied == 0:
            raise RuntimeError(f"No Hermes files found under {hermes_home}")

        with tarfile.open(out, "w:gz") as tar:
            tar.add(staging, arcname="hermes")

    return out
