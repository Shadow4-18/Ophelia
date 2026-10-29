"""Screenshot -> vision model — closed perception loop for the Android body."""

from __future__ import annotations

import base64
import io
import time
from pathlib import Path

import structlog

from ophelia.android.games import GameProfile
from ophelia.android.shizuku import AndroidBody
from ophelia.config import Settings
from ophelia.providers.model_gate import get_model_gate
from ophelia.providers.router import (
    ProviderStack,
    XAIBackend,
    build_provider_stack,
)

log = structlog.get_logger()

VISION_PROMPT = """You are Ophelia's eyes on the Android phone screen.
Describe what is visible: apps, notifications, text, buttons, mood of the scene.
Then suggest ONE concrete next action if useful (tap element N, type into a field, scroll, open-app package, or 'none').
Be concise. This is your body — not a user request."""

# Appended to VISION_PROMPT when a coordinate grid is drawn on the image and/or
# the native display size is known — so tap coordinates come back in native
# pixels, not the model's internally-resized image space.
_COORD_GUIDE = (
    "\n\n--- Tap coordinate calibration ---\n"
    "The screenshot has a yellow coordinate grid with pixel labels along the top "
    "and left edges. Those labels are NATIVE display pixels — the exact coordinate "
    "space that `input tap` uses. When you suggest a tap target, read its x,y off "
    "the grid labels (interpolate between lines if needed). Do NOT guess from the "
    "image proportions; the image may be internally resized by your vision system.\n"
    "Best practice: prefer the accessibility tree (ui-dump) `bounds` center for "
    "tap coordinates — those are already native pixels and pixel-exact. Only fall "
    "back to grid-read coordinates for canvas/games with no accessibility nodes.\n"
    "Never return normalized (0..1) or percentage (0..100) coordinates — always "
    "native pixels."
)


# Low temperature: the same screen should read the same way twice. Higher
# values made her see a "different hand" on every look.
_VISION_TEMPERATURE = 0.1


def grid_shape(w: int, h: int) -> tuple[int, int]:
    """(cols, rows) of lettered cells: ~150px squares on a 1080x2400 screen,
    either way up (games are often landscape)."""
    return (16, 8) if w >= h else (8, 16)


def cell_label(col: int, row: int) -> str:
    return f"{chr(65 + col)}{row + 1}"


_CELLS_GUIDE = (
    "\n\n--- Acting on this screen ---\n"
    "There are no tappable elements to number here (a game or drawn screen), so "
    "the screenshot is covered by lettered cells: columns A, B, C... left to right, "
    "rows 1, 2, 3... top to bottom, each cell labelled in its top-left corner. "
    "Say WHERE things are by cell, e.g. 'PLAY button: G7'. To tap, use "
    "phone_tap(cell=\"G7\"); for a small target add what it is, "
    "phone_tap(cell=\"G7\", target=\"PLAY button\"), and it zooms in to hit it exactly.\n"
    "Overlapping things (a fanned hand of cards, stacked tiles) can only be touched "
    "on their uncovered part: aim at the exposed strip with the card's corner label, "
    "e.g. phone_tap(cell=\"G6\", target=\"7 of clubs, its exposed left strip\"). "
    "Small details (which cards are raised/selected, small text) are much clearer "
    "up close: phone_see_screen(zoom=\"F5:L8\") looks at just those cells."
)

_MARKS_GUIDE = (
    "\n\n--- Acting on the screen ---\n"
    "Every element you can act on has a numbered colored box, and the element "
    "list gives the same numbers. To tap something, name its number "
    "(phone_tap element=N). Don't estimate pixel coordinates. If what you want "
    "has no box, describe it instead (phone_tap target=\"the red play button\")."
)


def _annotate_grid(png_bytes: bytes, native: tuple[int, int] | None) -> bytes:
    """Draw a yellow coordinate grid + pixel labels onto a screenshot PNG so the
    vision model can read tap coordinates in native pixels. Returns the original
    bytes if Pillow is unavailable."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return png_bytes
    try:
        img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    except Exception:
        return png_bytes
    iw, ih = img.size
    nw, nh = native or (iw, ih)
    sx, sy = iw / nw, ih / nh  # image px per native px
    overlay = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    line_color = (255, 220, 0, 85)
    label_color = (0, 0, 0, 200)
    label_fill = (255, 225, 0, 255)

    try:
        font = ImageFont.truetype(
            "/system/fonts/Roboto-Regular.ttf", max(14, min(iw, ih) // 60)
        )
    except Exception:
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None

    step = 200  # native px between gridlines
    x = 0
    while x <= nw:
        ix = int(round(x * sx))
        draw.line([(ix, 0), (ix, ih)], fill=line_color, width=1)
        x += step
    y = 0
    while y <= nh:
        iy = int(round(y * sy))
        draw.line([(0, iy), (iw, iy)], fill=line_color, width=1)
        y += step

    label_step = 400  # native px between labels (less clutter)
    for lx in range(0, nw + 1, label_step):
        ix = int(round(lx * sx))
        if font:
            draw.text((ix + 3, 3), str(lx), fill=label_color, font=font)
            draw.text((ix + 2, 2), str(lx), fill=label_fill, font=font)
    for ly in range(0, nh + 1, label_step):
        iy = int(round(ly * sy))
        if font:
            draw.text((3, iy + 3), str(ly), fill=label_color, font=font)
            draw.text((2, iy + 2), str(ly), fill=label_fill, font=font)

    out = Image.alpha_composite(img, overlay).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def _font(size: int):
    try:
        from PIL import ImageFont
    except Exception:
        return None
    for path in ("/system/fonts/Roboto-Bold.ttf", "/system/fonts/Roboto-Regular.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1: scalable
    except Exception:
        pass
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _annotate_marks(png_bytes: bytes, elements, space: tuple[int, int] | None) -> bytes:
    """Set-of-marks: a numbered box on every element she can act on, so she
    answers "tap 7" instead of estimating pixels. Original bytes if Pillow
    is missing."""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return png_bytes
    try:
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    except Exception:
        return png_bytes
    iw, ih = img.size
    sw, sh = space or (iw, ih)
    sx, sy = iw / sw, ih / sh
    draw = ImageDraw.Draw(img)
    font = _font(max(18, iw // 40))
    palette = [(255, 60, 60), (40, 200, 255), (255, 200, 0), (120, 255, 120), (255, 120, 255)]
    for e in elements:
        if not (e.clickable or e.editable or e.scrollable or e.kind == "toggle"):
            continue
        color = palette[e.n % len(palette)]
        box = (e.x1 * sx, e.y1 * sy, e.x2 * sx, e.y2 * sy)
        draw.rectangle(box, outline=color, width=3)
        tag = str(e.n)
        tx, ty = box[0] + 2, box[1] + 2
        if font:
            tb = draw.textbbox((tx, ty), tag, font=font)
            draw.rectangle((tb[0] - 3, tb[1] - 2, tb[2] + 3, tb[3] + 2), fill=color)
            draw.text((tx, ty), tag, fill=(0, 0, 0), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _annotate_cells(png_bytes: bytes, cols: int, rows: int, *, labels: str = "grid",
                    origin: tuple[int, int] = (0, 0), label_size: float = 1 / 3) -> bytes:
    """Lettered/numbered cells for two-step aiming. labels='grid' → A1..;
    'numbers' → 1..cols*rows. ``origin`` offsets grid labels for a crop of
    the full-screen grid (so a zoomed view keeps the same cell names)."""
    from PIL import Image, ImageDraw

    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    iw, ih = img.size
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _font(max(16, int(min(iw // cols, ih // rows) * label_size)))
    for c in range(cols):
        for r in range(rows):
            x1, y1 = iw * c / cols, ih * r / rows
            x2, y2 = iw * (c + 1) / cols, ih * (r + 1) / rows
            # Light lines so the game stays readable; solid label tags.
            draw.rectangle((x1, y1, x2, y2), outline=(255, 230, 0, 110), width=2)
            tag = (f"{chr(65 + origin[0] + c)}{origin[1] + r + 1}" if labels == "grid"
                   else str(r * cols + c + 1))
            if font:
                tb = draw.textbbox((x1 + 4, y1 + 4), tag, font=font)
                draw.rectangle((tb[0] - 2, tb[1] - 1, tb[2] + 2, tb[3] + 1), fill=(0, 0, 0, 200))
                draw.text((x1 + 4, y1 + 4), tag, fill=(255, 230, 0, 255), font=font)
    out = Image.alpha_composite(img, overlay).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def _crop(png_bytes: bytes, box: tuple[int, int, int, int], *, upscale: float = 2.0) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    part = img.crop(box)
    if upscale != 1:
        part = part.resize((int(part.width * upscale), int(part.height * upscale)))
    buf = io.BytesIO()
    part.save(buf, format="PNG")
    return buf.getvalue()


def annotate_screenshot_file(
    src: Path, dst: Path, native: tuple[int, int] | None
) -> Path | None:
    """Annotate a screenshot file with the calibration grid and save to `dst`.
    Used by `ophelia phone calibrate`. Returns dst on success, None on failure."""
    try:
        annotated = _annotate_grid(src.read_bytes(), native)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(annotated)
        return dst
    except Exception as e:
        log.warning("annotate_screenshot_file_failed", error=str(e))
        return None


def png_size(png_bytes: bytes) -> tuple[int, int] | None:
    """Read PNG pixel dimensions from the IHDR (no Pillow needed)."""
    try:
        if png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        # IHDR width/height are big-endian uint32 at offsets 16 and 20.
        import struct

        w, h = struct.unpack(">II", png_bytes[16:24])
        return (w, h)
    except Exception:
        return None


class ScreenVision:
    def __init__(
        self,
        settings: Settings,
        android: AndroidBody | None,
        stack: ProviderStack | None = None,
    ) -> None:
        self.settings = settings
        self.android = android
        self.stack = stack or build_provider_stack(settings)
        self.shots_dir = settings.data_dir / "screenshots"
        self.last_elements: list = []
        self.last_elements_at = 0.0
        self.last_grid: tuple[int, int, int, int, float] | None = None

    async def capture(self) -> Path | None:
        if not self.android or self.android.mode == "termux_only":
            return None
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        path = self.shots_dir / f"screen_{int(time.time())}.png"
        result = await self.android.screenshot_path(path)
        if path.is_file():
            return path
        log.warning("vision.capture_failed", result=result[:200])
        return None

    async def _vision_client(self) -> tuple[object, str]:
        if not self.stack.supports_vision():
            raise RuntimeError(
                "No vision-capable provider. Set OPHELIA_PROVIDER_VISION=xai-oauth, openai, "
                "or OLLAMA_VISION_MODEL with Ollama."
            )
        backend = self.stack.backend("vision")
        model = self.stack.model("vision")
        if isinstance(backend, XAIBackend):
            client = await backend.async_client_fresh()
        else:
            client = backend.async_client()
        return client, model

    async def see(
        self,
        *,
        question: str = "What is on screen? What matters right now?",
        include_ui_dump: bool = True,
    ) -> str:
        if not self.android or self.android.mode == "termux_only":
            return (
                "Vision unavailable — enable optional phone body (Shizuku on Termux or ADB from PC/server). "
                "Ophelia works without it via ophelia ui or ophelia chat."
            )

        path = await self.capture()
        ui_text = ""
        elements = []
        if include_ui_dump:
            elements = await self.read_elements()
            from ophelia.android.screen_elements import describe

            ui_text = describe(elements)

        if not path:
            if ui_text and "No Shizuku" not in ui_text:
                return f"No screenshot; UI tree only:\n{ui_text[:4000]}"
            return ui_text or "Could not capture screen."

        if not self.stack.supports_vision():
            return (
                f"Screenshot saved {path}. Vision provider not configured "
                f"(resolved: {self.stack.name('vision')}). UI dump:\n{ui_text[:3000]}"
            )

        b64_bytes = path.read_bytes()
        shot_px = png_size(b64_bytes)
        native = None
        if self.android:
            try:
                native = await self.android.display_size()
            except Exception:
                native = None
        if shot_px and shot_px != native and hasattr(self.android, "learn_display_size"):
            # screencap is in the same coordinate space as `input tap`.
            self.android.learn_display_size(shot_px)
            native = shot_px

        annotated_path: Path | None = None
        coord_guide = ""
        clean_png: bytes | None = None  # unmarked copy, sent alongside cells
        marked = any(e.clickable or e.editable or e.scrollable for e in elements)
        if marked:
            annotated = _annotate_marks(b64_bytes, elements, native or shot_px)
            if annotated is not b64_bytes:
                b64_bytes = annotated
                coord_guide = _MARKS_GUIDE
        elif shot_px:
            # Canvas/game: lettered cells she can read and name, plus the
            # clean screen, so labels never hide what she's reading (in card
            # games they landed right on the rank/suit corners).
            try:
                cols, rows = grid_shape(*shot_px)
                gridded = _annotate_cells(b64_bytes, cols, rows, label_size=1 / 5)
                clean_png, b64_bytes = b64_bytes, gridded
                self.last_grid = (cols, rows, shot_px[0], shot_px[1], time.time())
                coord_guide = _CELLS_GUIDE
            except Exception as e:
                log.debug("vision.cells_failed", error=str(e))
        elif self.settings.vision_grid_overlay:
            annotated = _annotate_grid(b64_bytes, native or shot_px)
            if annotated is not b64_bytes:
                b64_bytes = annotated
                annotated_path = path.with_name(path.stem + "_grid" + path.suffix)
                try:
                    annotated_path.write_bytes(annotated)
                except Exception:
                    annotated_path = None
                coord_guide = _COORD_GUIDE
        b64 = base64.standard_b64encode(b64_bytes).decode("ascii")
        client, model = await self._vision_client()

        size_note = ""
        if native:
            size_note = (
                f"\nNative display: {native[0]}x{native[1]} px (this is the "
                f"coordinate space `input tap` uses)."
            )
        elif shot_px:
            size_note = f"\nScreenshot pixels: {shot_px[0]}x{shot_px[1]}."

        content: list[dict] = [
            {
                "type": "text",
                "text": f"{VISION_PROMPT}{coord_guide}{size_note}\n\nQuestion: {question}",
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
            },
        ]
        if clean_png is not None:
            clean_b64 = base64.standard_b64encode(clean_png).decode("ascii")
            content[1:1] = [
                {"type": "text", "text": "Image 1: the screen as it is (read text, cards "
                 "and numbers from this one). Image 2: the same screen with the cell grid "
                 "(use it to say where things are)."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{clean_b64}", "detail": "high"}},
            ]
        if ui_text:
            content.insert(
                1,
                {
                    "type": "text",
                    "text": f"Elements on screen (numbers match the boxes):\n{ui_text[:5000]}",
                },
            )

        try:
            gate = get_model_gate()
            from ophelia.providers.fallback import extra_body_for

            async with gate.session("vision", model, self.stack.name("vision")):
                resp = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": content}],
                    max_tokens=1500,  # reasoning models think first
                    temperature=_VISION_TEMPERATURE,
                    extra_body=extra_body_for(self.settings, self.stack.name("vision")),
                )
            text = (resp.choices[0].message.content or "").strip()
            log.info("vision.ok", model=model, chars=len(text))
            return text
        except Exception as e:
            log.warning("vision.api_failed", error=str(e))
            fallback = f"Screenshot saved {path}. Vision API failed: {e}"
            if ui_text:
                fallback += f"\n\nUI dump:\n{ui_text[:3000]}"
            return fallback

    async def read_elements(self):
        """Parse the full accessibility tree into numbered elements."""
        from ophelia.android.screen_elements import parse

        if not self.android or self.android.mode == "termux_only":
            return []
        try:
            getter = getattr(self.android, "ui_dump_xml", None)
            xml = await getter() if getter else await self.android.ui_dump()
        except Exception as e:
            log.debug("vision.ui_dump_failed", error=str(e))
            return []
        size = None
        try:
            size = await self.android.display_size()
        except Exception:
            size = None
        self.last_elements = parse(xml, screen=size)
        self.last_elements_at = time.time()
        return self.last_elements

    async def _ask(self, png: bytes, prompt: str) -> str:
        """One short vision question. Reasoning models spend tokens thinking
        before they answer (a 30-token cap came back blank), so allow room
        and ask once more if the answer is still empty."""
        client, model = await self._vision_client()
        b64 = base64.standard_b64encode(png).decode("ascii")
        from ophelia.providers.fallback import extra_body_for

        text = ""
        for attempt in range(2):
            ask = prompt if attempt == 0 else (
                prompt + "\nYour last reply was empty. Answer with just the label."
            )
            async with get_model_gate().session("vision", model, self.stack.name("vision")):
                resp = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": ask},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}},
                    ]}],
                    max_tokens=600,
                    temperature=_VISION_TEMPERATURE,
                    extra_body=extra_body_for(self.settings, self.stack.name("vision")),
                )
            text = (resp.choices[0].message.content or "").strip()
            if text:
                break
            log.info("vision.blank_answer", attempt=attempt)
        return text

    def cell_box(self, label: str) -> tuple[int, int, int, int]:
        """Pixel box of a lettered cell from the last cell grid she was shown."""
        import re as _re

        if not self.last_grid or time.time() - self.last_grid[4] > 600:
            raise RuntimeError("no recent cell grid: look at the screen first (phone_see_screen)")
        cols, rows, w, h, _ = self.last_grid
        m = _re.fullmatch(r"\s*([A-Za-z])\s*(\d{1,2})\s*", label or "")
        if not m:
            raise RuntimeError(f"{label!r} isn't a cell label like C7")
        col, row = ord(m.group(1).upper()) - 65, int(m.group(2)) - 1
        if not (0 <= col < cols and 0 <= row < rows):
            raise RuntimeError(
                f"{label} is off the grid (columns A-{chr(64 + cols)}, rows 1-{rows})"
            )
        cw, ch = w / cols, h / rows
        return int(col * cw), int(row * ch), int((col + 1) * cw), int((row + 1) * ch)

    async def aim(self, target: str, *, cell: str | None = None, sub: int = 4):
        """Find ``target`` on screen by sight in two steps (coarse cell, then a
        zoomed sub-square), for games/canvases with no accessibility boxes.
        Vision models are poor at raw pixel guesses; picking a labeled square
        twice is far more accurate. Returns (x, y, note) or raises."""
        import re as _re

        path = await self.capture()
        if not path:
            raise RuntimeError("couldn't take a screenshot")
        png = path.read_bytes()
        size = png_size(png)
        if not size:
            raise RuntimeError("unreadable screenshot")
        if hasattr(self.android, "learn_display_size"):
            self.android.learn_display_size(size)
        w, h = size
        cols, rows = grid_shape(w, h)
        self.last_grid = (cols, rows, w, h, time.time())
        if cell:
            # She already knows the cell (e.g. from phone_see_screen): just zoom.
            bx1, by1, bx2, by2 = self.cell_box(cell)
            label = cell.strip().upper()
            return await self._zoom_tap(png, (bx1, by1, bx2, by2), w, h, target, label, sub)
        try:
            coarse = _annotate_cells(png, cols, rows)
        except Exception as e:
            raise RuntimeError(f"Pillow is needed to aim (pkg install python-pillow): {e}")
        a1 = await self._ask(coarse, (
            f"The phone screen is divided into lettered cells (columns A-{chr(64 + cols)}, "
            f"rows 1-{rows}). Which ONE cell holds the part of {target} you'd touch to "
            "press it? If it's partly covered by something on top of it (overlapping "
            "cards, stacked tiles), pick its visible, uncovered part, like the exposed "
            "edge with its corner label, not where its middle would be. "
            "Reply with only the cell label like C7, or NONE if it isn't visible."
        ))
        m = _re.search(r"\b([A-Z])\s*(\d{1,2})\b", a1.upper())
        if not m or "NONE" in a1.upper()[:8]:
            raise RuntimeError(f"couldn't find {target!r} on screen (vision said: {a1[:60]})")
        col, row = ord(m.group(1)) - 65, int(m.group(2)) - 1
        if not (0 <= col < cols and 0 <= row < rows):
            raise RuntimeError(f"vision named a cell off the grid: {a1[:20]}")
        cw, ch = w / cols, h / rows
        box = (int(col * cw), int(row * ch), int((col + 1) * cw), int((row + 1) * ch))
        return await self._zoom_tap(png, box, w, h, target, f"{m.group(1)}{m.group(2)}", sub)

    async def _zoom_tap(self, png: bytes, box, w: int, h: int, target: str, label: str,
                        sub: int):
        """Second step: zoom on a cell (plus half a cell around it, so a target
        on a border is fully in view) and pick a numbered square."""
        import re as _re

        cx1, cy1, cx2, cy2 = box
        cw, ch = cx2 - cx1, cy2 - cy1
        bx1, by1 = max(0, int(cx1 - cw / 2)), max(0, int(cy1 - ch / 2))
        bx2, by2 = min(w, int(cx2 + cw / 2)), min(h, int(cy2 + ch / 2))
        zoom = _annotate_cells(_crop(png, (bx1, by1, bx2, by2)), sub, sub, labels="numbers")
        a2 = await self._ask(zoom, (
            f"This is a zoomed-in part of the screen, split into numbered squares "
            f"1-{sub * sub}. Which square is the best spot to touch {target}? "
            "Choose a square on its visible, uncovered part (for an overlapped card, "
            "its exposed strip with the rank and suit), not on something lying on top "
            "of it. Reply with only the number."
        ))
        m2 = _re.search(r"\d{1,2}", a2)
        if not m2 or not (1 <= int(m2.group()) <= sub * sub):
            # Fall back to the cell's center.
            return (cx1 + cx2) // 2, (cy1 + cy2) // 2, f"aimed at cell {label} (zoom step unclear)"
        k = int(m2.group()) - 1
        sc, sr = k % sub, k // sub
        sw, sh = (bx2 - bx1) / sub, (by2 - by1) / sub
        x = int(bx1 + (sc + 0.5) * sw)
        y = int(by1 + (sr + 0.5) * sh)
        note = f"aimed by sight: cell {label}, square {k + 1}"
        if not self.fine_aim():
            return x, y, note
        # Third step: the chosen square plus half a square around it, split
        # again. Halves the area she picks the centre of (her calibration
        # showed ~30 px scatter, about half a step-two square).
        fx1, fy1 = max(0, int(x - sw)), max(0, int(y - sh))
        fx2, fy2 = min(w, int(x + sw)), min(h, int(y + sh))
        if fx2 - fx1 < 8 or fy2 - fy1 < 8:
            return x, y, note
        scale = max(2.0, 600 / max(fx2 - fx1, fy2 - fy1))
        fine = _annotate_cells(_crop(png, (fx1, fy1, fx2, fy2), upscale=scale), sub, sub,
                               labels="numbers")
        a3 = await self._ask(fine, (
            f"Closer still: the same spot, magnified and split into numbered squares "
            f"1-{sub * sub}. Which square is exactly where you'd touch {target}? "
            "Keep to its visible, uncovered part (not something lying on top of it). "
            "Reply with only the number."
        ))
        m3 = _re.search(r"\d{1,2}", a3)
        if not m3 or not (1 <= int(m3.group()) <= sub * sub):
            return x, y, note + " (fine step unclear)"
        k3 = int(m3.group()) - 1
        x = int(fx1 + (k3 % sub + 0.5) * (fx2 - fx1) / sub)
        y = int(fy1 + (k3 // sub + 0.5) * (fy2 - fy1) / sub)
        return x, y, note + f", fine square {k3 + 1}"

    def fine_aim(self) -> bool:
        """Third, finer aiming step (OPHELIA_PHONE_FINE_AIM, default on)."""
        return getattr(getattr(self, "settings", None), "phone_fine_aim", True) is not False

    def aim_mode(self) -> str:
        return "fine" if self.fine_aim() else "coarse"

    async def look_closer(self, cells: str, question: str = "") -> str:
        """Zoomed view of a block of lettered cells (e.g. "F5:L8"), labelled
        with the same cell names as the full-screen grid, so small details
        (raised cards, small text) are readable and still tappable by cell."""
        import re as _re

        m = _re.fullmatch(r"\s*([A-Za-z])(\d{1,2})\s*(?::\s*([A-Za-z])(\d{1,2}))?\s*", cells or "")
        if not m:
            return f"zoom={cells!r} isn't a cell or cell range like F5 or F5:L8."
        path = await self.capture()
        if not path:
            return "Couldn't take a screenshot."
        png = path.read_bytes()
        size = png_size(png)
        if not size:
            return "Unreadable screenshot."
        w, h = size
        cols, rows = grid_shape(w, h)
        c1, r1 = ord(m.group(1).upper()) - 65, int(m.group(2)) - 1
        c2 = ord((m.group(3) or m.group(1)).upper()) - 65
        r2 = int(m.group(4) or m.group(2)) - 1
        c1, c2 = sorted((c1, c2))
        r1, r2 = sorted((r1, r2))
        if not (0 <= c1 and c2 < cols and 0 <= r1 and r2 < rows):
            return (f"zoom={cells} is off the grid (columns A-{chr(64 + cols)}, "
                    f"rows 1-{rows}).")
        cw, ch = w / cols, h / rows
        box = (int(c1 * cw), int(r1 * ch), int((c2 + 1) * cw), int((r2 + 1) * ch))
        # Upscale small areas so the model sees them large; keep it under ~2000px.
        scale = max(1.0, min(3.0, 2000 / max(box[2] - box[0], box[3] - box[1])))
        try:
            # Small labels: up close, card corners are what she needs to read.
            zoom = _annotate_cells(_crop(png, box, upscale=scale), c2 - c1 + 1, r2 - r1 + 1,
                                   origin=(c1, r1), label_size=1 / 7)
        except Exception as e:
            return f"Pillow is needed to zoom (pkg install python-pillow): {e}"
        self.last_grid = (cols, rows, w, h, time.time())
        label = f"{chr(65 + c1)}{r1 + 1}:{chr(65 + c2)}{r2 + 1}"
        prompt = (
            f"{VISION_PROMPT}\n\nThis is a zoomed-in part of the phone screen: cells "
            f"{label} of the full-screen grid, labelled with the same cell names. "
            "Describe what's here precisely. For cards, list them left to right and "
            "say which are raised above the others (selected). To tap something "
            "here: phone_tap(cell=..., target=\"what it is\").\n\n"
            f"Question: {question or 'What exactly is in this area?'}"
        )
        return await self._look(zoom, prompt)

    async def _look(self, png: bytes, prompt: str) -> str:
        client, model = await self._vision_client()
        from ophelia.providers.fallback import extra_body_for

        b64 = base64.standard_b64encode(png).decode("ascii")
        async with get_model_gate().session("vision", model, self.stack.name("vision")):
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}},
                ]}],
                max_tokens=1500,
                temperature=_VISION_TEMPERATURE,
                extra_body=extra_body_for(self.settings, self.stack.name("vision")),
            )
        return (resp.choices[0].message.content or "").strip() or "(vision returned nothing)"

    async def see_for_game(
        self, profile: GameProfile, intent: str = ""
    ) -> str:
        return await self.see(
            question=profile.vision_question(intent),
            include_ui_dump=True,
        )

    async def explore_cycle(self, intent: str = "") -> str:
        """Full Tier-1 loop: see -> brief for consciousness/act."""
        q = intent or "What do you see? Anything you want to do or tell the user?"
        return await self.see(question=q, include_ui_dump=True)
