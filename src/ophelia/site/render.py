"""Safe Markdown → HTML for published pages (no external deps)."""

from __future__ import annotations

import html
import re

_FENCE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
_HR = re.compile(r"^---+\s*$")
_TOKEN = re.compile(
    r"(!\[([^\]]*)\]\(([^)]+)\)|"
    r"\[([^\]]+)\]\(([^)]+)\)|"
    r"`([^`]+)`|"
    r"\*\*(.+?)\*\*|"
    r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*))"
)


def _inline_full(text: str) -> str:
    tokens: list[tuple[str, str]] = []
    pos = 0
    for m in _TOKEN.finditer(text):
        if m.start() > pos:
            tokens.append(("text", text[pos : m.start()]))
        if m.group(1) and m.group(1).startswith("!["):
            tokens.append(("img", f"{m.group(2)}\0{m.group(3)}"))
        elif m.group(4) is not None:
            tokens.append(("link", f"{m.group(4)}\0{m.group(5)}"))
        elif m.group(6) is not None:
            tokens.append(("code", m.group(6)))
        elif m.group(7) is not None:
            tokens.append(("bold", m.group(7)))
        else:
            tokens.append(("italic", m.group(8) or ""))
        pos = m.end()
    if pos < len(text):
        tokens.append(("text", text[pos:]))

    out: list[str] = []
    for kind, payload in tokens:
        if kind == "text":
            out.append(html.escape(payload))
        elif kind == "code":
            out.append(f"<code>{html.escape(payload)}</code>")
        elif kind == "bold":
            out.append(f"<strong>{_inline_full(payload)}</strong>")
        elif kind == "italic":
            out.append(f"<em>{_inline_full(payload)}</em>")
        elif kind == "link":
            label, href = payload.split("\0", 1)
            href = href.strip()
            if href.startswith(("http://", "https://", "/", "#", "mailto:")):
                out.append(
                    f'<a href="{html.escape(href, quote=True)}">'
                    f"{html.escape(label)}</a>"
                )
            else:
                out.append(html.escape(label))
        elif kind == "img":
            alt, src = payload.split("\0", 1)
            src = src.strip()
            if src.startswith(("http://", "https://", "/")):
                out.append(
                    f'<img src="{html.escape(src, quote=True)}" '
                    f'alt="{html.escape(alt, quote=True)}" loading="lazy">'
                )
            else:
                out.append(html.escape(alt))
    return "".join(out)


def markdown_to_html(source: str) -> str:
    """Convert a Markdown subset to HTML. Unknown constructs escape as text."""
    if not source:
        return ""

    fences: list[str] = []

    def _stash_fence(m: re.Match[str]) -> str:
        lang = html.escape(m.group(1) or "")
        code = html.escape(m.group(2).rstrip("\n"))
        fences.append(
            f'<pre class="code"><code class="language-{lang}">{code}</code></pre>'
        )
        return f"\n\n@@FENCE{len(fences) - 1}@@\n\n"

    text = _FENCE.sub(_stash_fence, source.replace("\r\n", "\n"))
    blocks = re.split(r"\n{2,}", text.strip())
    html_blocks: list[str] = []

    for block in blocks:
        block = block.strip()
        if not block:
            continue
        fence_m = re.fullmatch(r"@@FENCE(\d+)@@", block)
        if fence_m:
            html_blocks.append(fences[int(fence_m.group(1))])
            continue
        if _HR.match(block):
            html_blocks.append("<hr>")
            continue

        lines = block.split("\n")
        if len(lines) == 1:
            heading = re.match(r"^(#{1,6})\s+(.+)$", lines[0])
            if heading:
                level = len(heading.group(1))
                html_blocks.append(
                    f"<h{level}>{_inline_full(heading.group(2).strip())}</h{level}>"
                )
                continue
            if re.match(r"^>\s?", lines[0]):
                quote = re.sub(r"^>\s?", "", lines[0])
                html_blocks.append(
                    f"<blockquote><p>{_inline_full(quote)}</p></blockquote>"
                )
                continue

        nonempty = [ln for ln in lines if ln.strip()]
        if nonempty and all(re.match(r"^[-*]\s+", ln) for ln in nonempty):
            items = []
            for ln in nonempty:
                item = re.sub(r"^[-*]\s+", "", ln)
                items.append(f"<li>{_inline_full(item)}</li>")
            html_blocks.append("<ul>" + "".join(items) + "</ul>")
            continue

        if nonempty and all(re.match(r"^\d+\.\s+", ln) for ln in nonempty):
            items = []
            for ln in nonempty:
                item = re.sub(r"^\d+\.\s+", "", ln)
                items.append(f"<li>{_inline_full(item)}</li>")
            html_blocks.append("<ol>" + "".join(items) + "</ol>")
            continue

        # Heading-only first line then body, or plain paragraph with soft breaks
        if len(lines) > 1:
            heading = re.match(r"^(#{1,6})\s+(.+)$", lines[0])
            if heading:
                level = len(heading.group(1))
                html_blocks.append(
                    f"<h{level}>{_inline_full(heading.group(2).strip())}</h{level}>"
                )
                rest = "\n".join(lines[1:]).strip()
                if rest:
                    html_blocks.append(
                        "<p>"
                        + "<br>\n".join(_inline_full(ln) for ln in rest.split("\n"))
                        + "</p>"
                    )
                continue

        html_blocks.append(
            "<p>" + "<br>\n".join(_inline_full(ln) for ln in lines) + "</p>"
        )

    return "\n".join(html_blocks)
