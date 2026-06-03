from __future__ import annotations

from pathlib import Path

from ophelia.config import OPHELIA_HOME
from ophelia.mind.prompter import load_prompter


def load_soul(path: Path | None = None) -> str:
    p = path or (OPHELIA_HOME / "SOUL.md")
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8").strip()


def parse_hermes_memory_file(text: str) -> list[str]:
    """Hermes/OpenClaw use § between memory entries."""
    if not text.strip():
        return []
    parts = text.split("§")
    return [p.strip() for p in parts if p.strip()]


def load_hermes_memories(hermes_home: Path) -> tuple[list[str], list[str]]:
    mem_dir = hermes_home / "memories"
    memory_entries: list[str] = []
    user_entries: list[str] = []
    mem_file = mem_dir / "MEMORY.md"
    user_file = mem_dir / "USER.md"
    if mem_file.is_file():
        memory_entries = parse_hermes_memory_file(mem_file.read_text(encoding="utf-8"))
    if user_file.is_file():
        user_entries = parse_hermes_memory_file(user_file.read_text(encoding="utf-8"))
    daily = mem_dir.glob("*.md")
    for f in daily:
        if f.name in ("MEMORY.md", "USER.md"):
            continue
        memory_entries.extend(parse_hermes_memory_file(f.read_text(encoding="utf-8")))
    return memory_entries, user_entries


def build_system_context(
    *,
    soul: str,
    memory_entries: list[str],
    user_entries: list[str],
    psyche_block: str,
    extra: str = "",
) -> str:
    blocks: list[str] = []
    if soul:
        blocks.append(f"# Persona (SOUL)\n{soul}")
    if psyche_block:
        blocks.append(f"# Internal state\n{psyche_block}")
    if user_entries:
        blocks.append("# User profile\n" + "\n---\n".join(user_entries[:40]))
    if memory_entries:
        blocks.append("# Long-term memory\n" + "\n---\n".join(memory_entries[:60]))
    prompter = load_prompter()
    if prompter:
        blocks.append(f"# Prompter policy (idle behavior)\n{prompter}")
    if extra:
        blocks.append(extra)
    return "\n\n".join(blocks)
