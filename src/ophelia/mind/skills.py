"""Dynamic skills Ophelia can learn and reuse."""

from __future__ import annotations

import re
from pathlib import Path

from ophelia.config import OPHELIA_HOME

SKILLS_DIR = OPHELIA_HOME / "skills"


def _parse_skill(text: str, path: Path) -> dict | None:
    text = text.strip()
    if not text:
        return None
    name = path.stem
    description = ""
    body = text
    if text.startswith("---"):
        m = re.match(r"---\s*\n(.*?)\n---\s*\n(.*)", text, re.S)
        if m:
            front = m.group(1)
            body = m.group(2).strip()
            for line in front.splitlines():
                if line.lower().startswith("description:"):
                    description = line.split(":", 1)[1].strip()
                elif line.lower().startswith("name:"):
                    name = line.split(":", 1)[1].strip()
    if not description:
        description = body.split("\n", 1)[0][:120]
    return {"name": name, "description": description, "body": body, "path": str(path)}


def load_skills() -> list[dict]:
    skills: list[dict] = []
    if not SKILLS_DIR.is_dir():
        return skills
    for path in sorted(SKILLS_DIR.rglob("*.md")):
        try:
            item = _parse_skill(path.read_text(encoding="utf-8"), path)
        except OSError:
            continue
        if item:
            skills.append(item)
    return skills


def skills_context_block() -> str:
    skills = load_skills()
    if not skills:
        return ""
    lines = ["# Learned skills (follow when relevant):"]
    for s in skills[:20]:
        lines.append(f"## {s['name']}\n{s['description']}\n{s['body'][:800]}")
    return "\n\n".join(lines)


def save_skill(name: str, description: str, content: str) -> Path:
    safe = re.sub(r"[^\w\-]+", "-", name.lower()).strip("-") or "skill"
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    path = SKILLS_DIR / "learned" / f"{safe}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = f"---\ndescription: {description}\nname: {name}\n---\n\n{content.strip()}\n"
    path.write_text(doc, encoding="utf-8")
    return path
