"""Settings screens built from a list of options.

Each option names an env key; its type, default, limits and help text come
from the matching field in ``config.Settings``, so a screen is just a list of
``Opt(key, label)``. Rows show the saved value (or the default); picking a
row edits it: on/off flips, choices open a picker, numbers and text are
typed and checked against the field's type before anything is written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ophelia.setup.env_io import read_env_key, write_env_updates
from ophelia.setup.tui import Header, menu, prompt_text, radiolist

_TRUE = ("1", "true", "yes", "on")


@dataclass
class Opt:
    key: str
    label: str
    choices: Sequence[tuple[str, str]] = ()  # (value, label)
    help: str = ""
    secret: bool = False
    validate: Callable[[str], str | None] | None = None
    show: Callable[[str], str] | None = None  # custom display of the value
    extra: dict = field(default_factory=dict)


def _fields() -> dict[str, tuple[str, Any]]:
    from ophelia.config import Settings

    out: dict[str, tuple[str, Any]] = {}
    for name, f in Settings.model_fields.items():
        keys: list[str] = []
        if f.alias:
            keys.append(f.alias)
        va = f.validation_alias
        if va is not None:
            keys += [c for c in getattr(va, "choices", [va]) if isinstance(c, str)]
        for k in keys:
            out.setdefault(k, (name, f))
    return out


_FIELD_CACHE: dict[str, tuple[str, Any]] | None = None


def field_for(key: str):
    global _FIELD_CACHE
    if _FIELD_CACHE is None:
        _FIELD_CACHE = _fields()
    got = _FIELD_CACHE.get(key)
    return got[1] if got else None


def _annotation_kind(f) -> str:
    ann = str(f.annotation) if f is not None else ""
    if "bool" in ann:
        return "tristate" if "None" in ann else "bool"
    if "float" in ann:
        return "float"
    if "int" in ann:
        return "int"
    return "text"


def default_text(key: str) -> str:
    f = field_for(key)
    if f is None:
        return ""
    d = f.default
    if d is None:
        return "auto" if _annotation_kind(f) == "tristate" else ""
    if isinstance(d, bool):
        return "on" if d else "off"
    return str(d)


def check_value(key: str, raw: str) -> str | None:
    """Error message if ``raw`` isn't valid for ``key``'s config field."""
    f = field_for(key)
    if f is None:
        return None
    from typing import Annotated

    from pydantic import TypeAdapter, ValidationError

    try:
        ann = Annotated[(f.annotation, *f.metadata)] if f.metadata else f.annotation
        TypeAdapter(ann).validate_python(raw)
    except ValidationError as e:
        msg = e.errors()[0].get("msg", "invalid value")
        return f"Not saved: {msg}."
    except Exception:
        return None
    return None


def current(opt: Opt) -> tuple[str, bool]:
    """(value, is_set_in_env)."""
    raw = read_env_key(opt.key)
    if raw:
        return raw, True
    return default_text(opt.key), False


def display(opt: Opt) -> str:
    val, is_set = current(opt)
    if opt.secret:
        return "set" if is_set else "not set"
    if opt.show:
        return opt.show(val)
    kind = _annotation_kind(field_for(opt.key))
    if kind in ("bool", "tristate") and not opt.choices:
        low = val.lower()
        val = "on" if low in _TRUE else ("auto" if low in ("", "auto") else "off")
    elif opt.choices:
        val = next((lab.split(" — ")[0] for v, lab in opt.choices if v == val), val)
    if not val:
        return "(not set)" if is_set else "default"
    return val if is_set else f"{val} (default)"


def edit(opt: Opt) -> bool:
    """Edit one option. True when something was written."""
    f = field_for(opt.key)
    kind = _annotation_kind(f)
    val, is_set = current(opt)
    help_text = opt.help or (f.description if f is not None and f.description else "")

    if kind == "bool" and not opt.choices:
        new = "false" if val.lower() in _TRUE else "true"
        write_env_updates({opt.key: new})
        return True

    choices = list(opt.choices)
    if kind == "tristate" and not choices:
        choices = [("auto", "auto — decide for this device"), ("true", "on"), ("false", "off")]
    if choices:
        labels = [lab for _, lab in choices]
        sel = next((i for i, (v, _) in enumerate(choices) if v.lower() == val.lower()), -1)
        dflt = default_text(opt.key)
        if is_set:
            labels.append(f"Reset to default ({dflt or 'none'})")
        pick = radiolist(opt.label, labels, selected=sel, description=help_text)
        if pick < 0:
            return False
        if pick >= len(choices):
            write_env_updates({opt.key: None})
            return True
        value = choices[pick][0]
        if kind == "tristate" and value == "auto":
            write_env_updates({opt.key: None})
        else:
            write_env_updates({opt.key: value})
        return True

    def _validate(raw: str) -> str | None:
        if opt.validate:
            err = opt.validate(raw)
            if err:
                return err
        return check_value(opt.key, raw)

    hint = help_text
    dflt = default_text(opt.key)
    if dflt and not opt.secret:
        hint = (hint + "\n" if hint else "") + f"  Default: {dflt}"
    typed = prompt_text(
        opt.label,
        secret=opt.secret,
        default=val if is_set else "",
        hint=hint,
        clearable=True,
        validate=_validate,
    )
    if typed is None:
        return False
    if typed == "":
        write_env_updates({opt.key: None})
        return True
    if typed == val and is_set:
        return False
    write_env_updates({opt.key: typed})
    return True


@dataclass
class Act:
    """A row that runs ``run()``; ``value`` (text or callable) shows beside it."""

    label: str
    run: Callable[[], Any]
    value: str | Callable[[], str] = ""


def _row_label(o: Opt | Act) -> str:
    return o.label


def _row_value(o: Opt | Act) -> str:
    if isinstance(o, Opt):
        return display(o)
    try:
        return o.value() if callable(o.value) else o.value
    except Exception as e:  # a status probe must never break the menu
        return f"? ({e.__class__.__name__})"


def settings_screen(
    title: str,
    rows: Sequence[Opt | Act | Header],
    *,
    description: str | Callable[[], str] = "",
) -> None:
    """Show rows with their current values; edit until Back."""
    cursor = 0
    while True:
        width = max((len(_row_label(r)) for r in rows if not isinstance(r, Header)), default=10) + 2
        labels: list[str] = []
        for r in rows:
            if isinstance(r, Header):
                labels.append(r)
                continue
            value = _row_value(r)
            labels.append(f"{r.label.ljust(width)}{value}" if value else r.label)
        labels.append("Back")
        desc = description() if callable(description) else description
        pick = menu(title, labels, selected=cursor, description=desc)
        if pick < 0 or pick == len(labels) - 1:
            return
        cursor = pick
        row = rows[pick]
        if isinstance(row, Opt):
            edit(row)
        elif isinstance(row, Act):
            row.run()


# --- validators ------------------------------------------------------------

_HOURS = re.compile(r"^\s*([01]?\d|2[0-3])\s*-\s*([01]?\d|2[0-4])\s*$")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def hours_range(raw: str) -> str | None:
    return None if _HOURS.match(raw) else "Use start-end hours like 1-8 or 23-7."


def day_list(raw: str) -> str | None:
    bad = [d for d in re.split(r"[,\s]+", raw.strip().lower()) if d and d[:3] not in _DAYS]
    return f"Unknown day(s): {', '.join(bad)}. Use mon,tue,..." if bad else None


def clock_list(raw: str) -> str | None:
    for t in [x.strip() for x in raw.split(",") if x.strip()]:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            return f"{t!r} isn't a time like 06:30."
    return None


def id_list(raw: str) -> str | None:
    bad = [x for x in re.split(r"[,\s]+", raw.strip()) if x and not x.lstrip("-").isdigit()]
    return f"IDs must be numbers: {', '.join(bad)}" if bad else None


def owner_ids(raw: str) -> str | None:
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        chan, _, uid = part.partition(":")
        if chan not in ("telegram", "discord") or not uid.strip().lstrip("-").isdigit():
            return f"{part!r} should look like telegram:12345 or discord:12345."
    return None


def timezone_name(raw: str) -> str | None:
    from ophelia.timeutil import validate_timezone_name

    ok, _, detail = validate_timezone_name(raw)
    return None if ok else detail
