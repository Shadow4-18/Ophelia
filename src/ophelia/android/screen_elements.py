"""What's on the phone screen, as a short numbered list she can act on.

The raw uiautomator XML for one Samsung screen is 30-150 KB; it used to be
cut to 6-15k characters (most of the screen gone) and handed to her to read
`bounds="[x1,y1][x2,y2]"` strings herself. Now the full dump is parsed into
elements with a label, a kind and a center point, numbered so she can say
"tap 7" and the tap lands on the exact center the system reports.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_NODE = re.compile(r"<node\b([^>]*?)/?>")
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')


@dataclass
class Element:
    n: int
    label: str
    kind: str  # button | field | toggle | list | text | image | item
    x1: int
    y1: int
    x2: int
    y2: int
    clickable: bool = False
    scrollable: bool = False
    editable: bool = False
    checked: bool | None = None
    resource_id: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def center(self) -> tuple[int, int]:
        return (self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2

    @property
    def area(self) -> int:
        return max(0, self.x2 - self.x1) * max(0, self.y2 - self.y1)

    def line(self) -> str:
        cx, cy = self.center
        state = ""
        if self.checked is not None:
            state = " (on)" if self.checked else " (off)"
        label = f' "{self.label}"' if self.label else ""
        rid = f" #{self.resource_id}" if self.resource_id and not self.label else ""
        return f"[{self.n}] {self.kind}{label}{rid}{state} @({cx},{cy})"


def _short_id(rid: str) -> str:
    return rid.split("/")[-1] if rid else ""


def _attrs_iter(xml: str):
    """(attrs, depth-ordered) for every node; ElementTree when the XML is
    whole, regex when it's truncated or has junk around it."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        root = None
    if root is not None:
        for node in root.iter("node"):
            yield node.attrib, node
        return
    for m in _NODE.finditer(xml):
        yield dict(_ATTR.findall(m.group(1))), None


def _descendant_text(node) -> str:
    if node is None:
        return ""
    for d in node.iter():
        t = (d.attrib.get("text") or d.attrib.get("content-desc") or "").strip()
        if t:
            return t
    return ""


def _kind(attrs: dict, editable: bool, scrollable: bool) -> str:
    cls = (attrs.get("class") or "").split(".")[-1].lower()
    if editable:
        return "field"
    if attrs.get("checkable") == "true" or "switch" in cls or "checkbox" in cls:
        return "toggle"
    if scrollable:
        return "list"
    if "button" in cls:
        return "button"
    if "image" in cls:
        return "image" if attrs.get("clickable") != "true" else "button"
    if attrs.get("clickable") == "true":
        return "item"
    return "text"


def parse(xml: str, *, screen: tuple[int, int] | None = None, limit: int = 60) -> list[Element]:
    """Interactive elements and visible text, numbered top-to-bottom."""
    out: list[Element] = []
    seen: set[tuple[int, int, int, int]] = set()
    for attrs, node in _attrs_iter(xml or ""):
        m = _BOUNDS.match(attrs.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = (int(v) for v in m.groups())
        if x2 - x1 < 8 or y2 - y1 < 8:
            continue
        if screen and (x2 <= 0 or y2 <= 0 or x1 >= screen[0] or y1 >= screen[1]):
            continue
        clickable = attrs.get("clickable") == "true" or attrs.get("long-clickable") == "true"
        scrollable = attrs.get("scrollable") == "true"
        cls = attrs.get("class") or ""
        editable = "EditText" in cls or attrs.get("editable") == "true"
        text = (attrs.get("text") or "").strip()
        desc = (attrs.get("content-desc") or "").strip()
        label = text or desc
        if not label and (clickable or editable):
            label = _descendant_text(node)
        interesting = clickable or scrollable or editable or attrs.get("checkable") == "true"
        if not interesting and not label:
            continue
        key = (x1, y1, x2, y2)
        if key in seen:
            continue
        seen.add(key)
        checked = None
        if attrs.get("checkable") == "true":
            checked = attrs.get("checked") == "true"
        out.append(Element(
            n=0, label=" ".join(label.split())[:60], kind=_kind(attrs, editable, scrollable),
            x1=x1, y1=y1, x2=x2, y2=y2, clickable=clickable, scrollable=scrollable,
            editable=editable, checked=checked, resource_id=_short_id(attrs.get("resource-id", "")),
            extra={"package": attrs.get("package", "")},
        ))
    # Plain text that sits inside a clickable element is redundant with it.
    clickables = [e for e in out if e.clickable or e.editable]
    kept = []
    for e in out:
        if e.kind == "text" and any(
            c.x1 <= e.x1 and c.y1 <= e.y1 and c.x2 >= e.x2 and c.y2 >= e.y2 and c.label == e.label
            for c in clickables
        ):
            continue
        kept.append(e)
    # Screen-filling containers (the whole page is "clickable") are noise
    # unless they scroll.
    if screen:
        full = screen[0] * screen[1] * 0.85
        kept = [e for e in kept if e.area < full or e.scrollable]
    kept.sort(key=lambda e: (e.y1 // 20, e.x1))
    kept = kept[:limit]
    for i, e in enumerate(kept, 1):
        e.n = i
    return kept


def foreground_package(elements: list[Element]) -> str:
    pkgs = [e.extra.get("package", "") for e in elements if e.extra.get("package")]
    return max(set(pkgs), key=pkgs.count) if pkgs else ""


def describe(elements: list[Element], *, max_lines: int = 60) -> str:
    if not elements:
        return ("No tappable elements on this screen (a game or drawn screen). Look with "
                "phone_see_screen: it's covered by lettered cells; tap with "
                "phone_tap(cell=\"G7\") or phone_tap(cell=\"G7\", target=\"PLAY button\").")
    pkg = foreground_package(elements)
    head = f"On screen{f' ({pkg})' if pkg else ''}: tap by number, e.g. phone_tap(element=3)."
    return head + "\n" + "\n".join(e.line() for e in elements[:max_lines])


def find(elements: list[Element], query: str) -> Element | None:
    """Best element for a text query: exact label, then contains, then id."""
    q = (query or "").strip().lower()
    if not q:
        return None
    def score(e: Element) -> int:
        lab = e.label.lower()
        s = 0
        if lab == q:
            s = 100
        elif lab.startswith(q):
            s = 80
        elif q in lab:
            s = 60
        elif q.replace(" ", "_") in e.resource_id.lower():
            s = 40
        if s and (e.clickable or e.editable):
            s += 10
        return s
    best = max(elements, key=score, default=None)
    return best if best is not None and score(best) > 0 else None
