"""What the owner thought of her images, from his reactions.

Every image she sends on Telegram is remembered by message id. When he
reacts to one (❤ 🔥 😍 … or 👎 🤮 …), the reaction is joined to that
image's ledger line, so each checkpoint and LoRA builds up a score. Auto
mode then picks the ones he likes more often (selection.py).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from ophelia.fileio import atomic_write_json
import structlog

log = structlog.get_logger()

_LIKE = {"❤", "❤️", "🔥", "🥰", "😍", "😘", "💋", "😈", "💯", "👍", "🤩", "😁", "🤤",
         "👌", "😻", "🫦", "❤‍🔥", "❤️‍🔥", "💘", "😏", "👏", "🍓", "🤯", "😮", "🥵", "🍆",
         "💦", "🫠"}
_DISLIKE = {"👎", "💩", "🤮", "🤢", "😡", "🥱", "😐", "💔", "🤡", "😒", "😕", "🙄"}
_SENT_CAP = 400
_cache: dict[str, Any] = {"key": None, "scores": None}


def emoji_score(emoji: str) -> float | None:
    """+1 liked, -1 disliked, None for reactions that say nothing about the image."""
    parts = [e.strip() for e in (emoji or "").split() if e.strip()]
    vals = [1.0 if e in _LIKE else -1.0 if e in _DISLIKE else None for e in parts]
    known = [v for v in vals if v is not None]
    return sum(known) / len(known) if known else None


def _sent_path(settings: Any) -> Path:
    return Path(settings.data_dir) / "civitai_sent_images.json"


def _feedback_path(settings: Any) -> Path:
    return Path(settings.data_dir) / "logs" / "image_feedback.jsonl"


def remember_sent(settings: Any, chat_id: Any, message_id: Any, filename: str) -> None:
    """She sent ``filename`` as message ``message_id``. Never raises."""
    if not filename or message_id is None:
        return
    try:
        path = _sent_path(settings)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = []
        data.append([str(chat_id), int(message_id), os.path.basename(filename)])
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, data[-_SENT_CAP:])
    except Exception as e:
        log.debug("image_feedback.remember_failed", error=str(e))


def sent_file(settings: Any, chat_id: Any, message_id: Any) -> str:
    try:
        data = json.loads(_sent_path(settings).read_text(encoding="utf-8"))
    except Exception:
        return ""
    for chat, mid, name in reversed(data):
        if chat == str(chat_id) and mid == int(message_id):
            return name
    return ""


def record_reaction(settings: Any, chat_id: Any, message_id: Any, emoji: str) -> str:
    """Log his reaction if it was a like/dislike on one of her images;
    returns the file name ("" when nothing was logged)."""
    name = sent_file(settings, chat_id, message_id)
    score = emoji_score(emoji)
    if not name or score is None:
        return ""
    try:
        path = _feedback_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": round(time.time(), 3), "file": name, "emoji": emoji,
                                "score": score}) + "\n")
        log.info("image_feedback.recorded", file=name, emoji=emoji, score=score)
    except Exception as e:
        log.debug("image_feedback.write_failed", error=str(e))
    return name


_JSONL_CACHE: dict[tuple[str, int], tuple[tuple[int, int], list[dict]]] = {}


def _read_jsonl(path: Path, limit: int) -> list[dict]:
    """The last ``limit`` JSON lines of a log, parsed once per change: one
    image used to re-read and re-parse the whole ledger five-plus times
    (scores, pairs, liked weights per LoRA, liked prompts)."""
    try:
        st = path.stat()
    except OSError:
        return []
    key, stamp = (str(path), limit), (st.st_mtime_ns, st.st_size)
    hit = _JSONL_CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except Exception:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    _JSONL_CACHE[key] = (stamp, out)
    return out


def scores(settings: Any) -> dict[str, tuple[float, int]]:
    """{checkpoint or LoRA AIR: (sum of scores, count)} from his reactions."""
    from ophelia.providers.civitai.pipeline import ledger_path

    try:
        ledger, fb = ledger_path(settings), _feedback_path(settings)
        key = (str(fb), fb.stat().st_mtime, ledger.stat().st_mtime)
    except (OSError, TypeError):
        return {}
    if _cache["key"] == key:
        return _cache["scores"]
    by_file: dict[str, dict] = {}
    for rec in _read_jsonl(ledger, 4000):
        for p in rec.get("paths") or ([rec["path"]] if rec.get("path") else []):
            by_file[os.path.basename(str(p))] = rec
    out: dict[str, list[float]] = {}
    for item in _read_jsonl(fb, 4000):
        rec = by_file.get(str(item.get("file", "")))
        if not rec or not isinstance(item.get("score"), (int, float)):
            continue
        for air in [rec.get("model")] + list((rec.get("loras") or {}).keys()):
            if air:
                s = out.setdefault(str(air), [0.0, 0])
                s[0] += float(item["score"])
                s[1] += 1
    result = {k: (v[0], int(v[1])) for k, v in out.items()}
    _cache.update(key=key, scores=result)
    return result


def multiplier(score: tuple[float, int] | None) -> float:
    """How much more (or less) often to pick something he reacted to.
    Two neutral pseudo-votes keep one reaction from deciding everything."""
    if not score:
        return 1.0
    total, n = score
    avg = total / (n + 2)
    return max(0.3, min(1.8, 1.0 + 0.8 * avg))


def liked_prompts(settings: Any, recipe_key: str, limit: int = 2) -> list[dict]:
    """His favourite images on this model, best first: the ledger lines
    (prompt, loras) of images he reacted to with a like."""
    from ophelia.providers.civitai.pipeline import ledger_path

    by_file: dict[str, dict] = {}
    for rec in _read_jsonl(ledger_path(settings), 4000):
        for p in rec.get("paths") or ([rec["path"]] if rec.get("path") else []):
            by_file[os.path.basename(str(p))] = rec
    best: dict[str, tuple[float, dict]] = {}
    for item in _read_jsonl(_feedback_path(settings), 4000):
        rec = by_file.get(str(item.get("file", "")))
        score = item.get("score")
        if not rec or not isinstance(score, (int, float)) or rec.get("recipe") != recipe_key:
            continue
        prompt = str(rec.get("prompt") or "")
        if not prompt:
            continue
        old = best.get(prompt, (0.0, rec))
        best[prompt] = (old[0] + float(score), rec)
    ranked = sorted((v for v in best.values() if v[0] > 0), key=lambda v: -v[0])
    return [rec for _, rec in ranked[:limit]]


def _scored_records(settings: Any) -> list[tuple[float, dict]]:
    """(his score, ledger line) for every image he reacted to."""
    from ophelia.providers.civitai.pipeline import ledger_path

    by_file: dict[str, dict] = {}
    for rec in _read_jsonl(ledger_path(settings), 4000):
        for p in rec.get("paths") or ([rec["path"]] if rec.get("path") else []):
            by_file[os.path.basename(str(p))] = rec
    out = []
    for item in _read_jsonl(_feedback_path(settings), 4000):
        rec = by_file.get(str(item.get("file", "")))
        if rec and isinstance(item.get("score"), (int, float)):
            out.append((float(item["score"]), rec))
    return out


def pair_scores(settings: Any) -> dict[tuple[str, str], tuple[float, int]]:
    """{(a, b) sorted: (sum, count)} for every checkpoint+LoRA and LoRA+LoRA
    pair in the images he reacted to: which combinations worked together."""
    out: dict[tuple[str, str], list[float]] = {}
    for score, rec in _scored_records(settings):
        parts = sorted({str(a) for a in [rec.get("model"), *(rec.get("loras") or {})] if a})
        for i, a in enumerate(parts):
            for b in parts[i + 1:]:
                s = out.setdefault((a, b), [0.0, 0])
                s[0] += score
                s[1] += 1
    return {k: (v[0], int(v[1])) for k, v in out.items()}


def pair_multiplier(pairs: dict[tuple[str, str], tuple[float, int]], air: str,
                    others: list[str]) -> float:
    """How well ``air`` went with ``others`` before (1.0 = no history)."""
    mult = 1.0
    for o in others:
        if not o or o == air:
            continue
        key = (air, o) if air < o else (o, air)
        if key in pairs:
            mult *= multiplier(pairs[key])
    return max(0.2, min(2.5, mult))


def liked_weight(settings: Any, air: str, model: str = "") -> float | None:
    """The weight ``air`` had in images he liked (same checkpoint first)."""
    same: list[float] = []
    any_: list[float] = []
    for score, rec in _scored_records(settings):
        if score <= 0:
            continue
        w = (rec.get("loras") or {}).get(air)
        if not isinstance(w, (int, float)):
            continue
        any_.append(float(w))
        if model and rec.get("model") == model:
            same.append(float(w))
    pick = same or any_
    return round(sum(pick) / len(pick), 2) if pick else None
