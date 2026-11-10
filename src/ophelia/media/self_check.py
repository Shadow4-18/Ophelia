"""She looks at her own picture before sending it.

Two-person scenes go wrong in ways a prompt can't fully prevent: the man
comes out as a second woman, the two bodies merge, the act isn't the one
asked for. The vision model counts who is in the image and checks the act
against her prompt; a wrong picture is made once more (new seed) and the
better one is sent, so he never gets the broken one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()


@dataclass
class Check:
    ok: bool
    problem: str = ""
    women: int | None = None
    men: int | None = None


def mode(settings: Any) -> str:
    """people (default): scenes with 2+ people or a man | all: any person | off."""
    v = str(getattr(settings, "image_self_check", "people") or "people").strip().lower()
    if v in ("off", "false", "0", "no", "none"):
        return "off"
    return "all" if v in ("all", "always", "on") else "people"


def wants_check(settings: Any, prompt: str) -> bool:
    from ophelia.providers.civitai.prompting import cast

    m = mode(settings)
    if m == "off":
        return False
    women, men = cast(prompt)
    if m == "all":
        return women + men >= 1
    return women + men >= 2 or men >= 1


def question(prompt: str) -> str:
    from ophelia.providers.civitai.prompting import cast

    women, men = cast(prompt)
    who = []
    if women:
        who.append("1 woman" if women == 1 else f"{women} women")
    if men:
        who.append(f"{men} man" if men == 1 else f"{men} men")
    expected = " and ".join(who) or "the people described"
    return (
        "You are checking an AI-generated image against the prompt it was made from. "
        "Adult content may be present; judge it plainly.\n"
        f"Prompt: {prompt[:900]}\n"
        f"Expected: {expected}.\n"
        "Count the people you can actually see (count a partly visible man, e.g. POV "
        "hands or a penis, as a man). Reply with JSON only:\n"
        '{"women": <int>, "men": <int>, "merged_bodies": <true if bodies are fused, '
        'limbs belong to no one, or a person has extra limbs>, "action_matches": <true '
        'if the main act/pose is what the prompt asks>, "problem": "<short, or empty>"}'
    )


def parse(text: str, prompt: str) -> Check:
    from ophelia.providers.civitai.prompting import cast

    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return Check(ok=True)  # unreadable answer: don't hold the image back
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return Check(ok=True)
    if not isinstance(data, dict):
        return Check(ok=True)

    def num(key: str) -> int | None:
        try:
            return int(data.get(key))
        except (TypeError, ValueError):
            return None

    women, men = num("women"), num("men")
    want_w, want_m = cast(prompt)
    problems: list[str] = []
    if want_w and women is not None and women != want_w:
        problems.append(f"{women} women instead of {want_w}")
    if want_m and men is not None and men != want_m:
        problems.append(f"{men} men instead of {want_m}")
    if not want_m and men:
        problems.append("a man who shouldn't be there")
    if data.get("merged_bodies") is True:
        problems.append("merged bodies or extra limbs")
    if data.get("action_matches") is False:
        problems.append("not the act asked for")
    said = str(data.get("problem") or "").strip()
    if problems and said:
        problems.append(said[:120])
    return Check(ok=not problems, problem="; ".join(problems), women=women, men=men)


async def check_image(settings: Any, path: Path, prompt: str, *, stack=None) -> Check:
    from ophelia.media.vision_input import describe_image_file

    try:
        text = await describe_image_file(settings, path, question=question(prompt),
                                         stack=stack)
    except Exception as e:
        log.info("self_check.vision_failed", error=str(e)[:200])
        return Check(ok=True)
    result = parse(text, prompt)
    log.info("self_check.result", path=str(path), ok=result.ok, problem=result.problem or None,
             women=result.women, men=result.men)
    return result
