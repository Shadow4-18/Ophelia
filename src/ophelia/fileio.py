"""Writing state files so a kill mid-write can't corrupt them.

Termux gets killed without warning. path.write_text() truncates the file
first, so a kill during the write left a half-written JSON file that then
failed to load and silently reset to empty (his image reactions, LoRA
caches, the OAuth token). Writing to a temp file in the same folder and
renaming it over the old one is atomic: readers see the old file or the
new one, never half of either.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path | str, text: str, *, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)  # keep 0600 on token files
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path | str, data: Any, **dumps: Any) -> None:
    atomic_write_text(path, json.dumps(data, **dumps))
