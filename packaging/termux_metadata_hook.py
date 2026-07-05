"""Hatch metadata hook: Termux-safe dependency pins for plain `pip install -e .`."""

from __future__ import annotations

import os
from pathlib import Path

from hatchling.metadata.plugin.interface import MetadataHookInterface


def _is_termux() -> bool:
    if os.environ.get("TERMUX_VERSION"):
        return True
    return Path("/data/data/com.termux/files/usr").is_dir()


def _pin_termux_dependency(dep: str) -> str:
    """Replace deps that pull broken Rust builds on Termux Android."""
    if dep.startswith("openai>=") or dep.startswith("openai=="):
        return "openai>=1.35,<1.40"
    if dep.startswith("httpx>=") or dep.startswith("httpx=="):
        return "httpx>=0.27,<0.28"
    return dep


class CustomMetadataHook(MetadataHookInterface):
    """Cap openai (no jiter) and httpx (proxies kwarg) when installing on Termux."""

    def update(self, metadata: dict) -> None:
        if not _is_termux():
            return
        deps = metadata.get("dependencies")
        if not deps:
            return
        metadata["dependencies"] = [_pin_termux_dependency(str(d)) for d in deps]
