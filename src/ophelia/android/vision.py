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
Then suggest ONE concrete next action if useful (tap target with approximate x,y from ui-dump if known, or open-app package, or 'none').
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
        if include_ui_dump:
            ui_text = (await self.android.ui_dump())[:6000]

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

        annotated_path: Path | None = None
        coord_guide = ""
        if self.settings.vision_grid_overlay:
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
        if ui_text:
            content.insert(
                1,
                {
                    "type": "text",
                    "text": f"Accessibility tree (for tap coordinates):\n{ui_text[:5000]}",
                },
            )

        try:
            gate = get_model_gate()
            async with gate.session("vision", model, self.stack.name("vision")):
                resp = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": content}],
                    max_tokens=800,
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
