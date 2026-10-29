"""Touch calibration: a page in the phone's browser shows targets, she aims at
them by sight, and her measured error corrects later aimed taps."""

from __future__ import annotations

import asyncio
import random
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ophelia.android import touch_calibration as tc

W, H = 1080, 2400
DPR, BAR = 3.0, 110  # browser pixel ratio, status bar + toolbar height in px
BIAS = (30, -12)  # her aim: 30 px right, 12 px high


def _png(w: int, h: int) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IEND", b""))


class FakePhone:
    """A phone with a browser: taps at screen pixels reach the page as CSS
    pixels under a status bar."""

    def __init__(self, tmp: Path) -> None:
        self.shells: list[str] = []
        self.server_url = ""
        self.browser: asyncio.Task | None = None
        self.shot = tmp / "shot.png"
        self.shot.write_bytes(_png(W, H))
        self.settings = {"show_touches": "0", "accelerometer_rotation": "1",
                         "user_rotation": "0"}

    async def screen_state(self):
        return "on"

    async def shell(self, cmd: str) -> str:
        self.shells.append(cmd)
        if cmd.startswith("settings get system "):
            return self.settings.get(cmd.split()[-1], "null")
        if cmd.startswith("settings put system "):
            _, _, _, name, value = cmd.split()
            self.settings[name] = value
        if "android.intent.action.VIEW" in cmd:
            self.server_url = cmd.split("-d ")[-1].strip()
            self.browser = asyncio.create_task(self._browse())
        return ""

    async def _browse(self):
        q = f"vw={W / DPR}&vh={(H - BAR) / DPR}&dpr={DPR}"
        async with httpx.AsyncClient(trust_env=False) as http:
            while True:
                try:
                    await http.get(self.server_url + "state?" + q)
                except Exception:
                    return
                await asyncio.sleep(0.03)

    async def tap(self, x: int, y: int) -> str:
        css = {"x": x / DPR, "y": (y - BAR) / DPR, "dpr": DPR}
        async with httpx.AsyncClient(trust_env=False) as http:
            await http.post(self.server_url + "tap", json=css)
        return ""

    def learn_display_size(self, size):
        pass


class FakeVision:
    """Aims at the page's target with a fixed bias, like a model would."""

    def __init__(self, phone: FakePhone, server_ref: dict) -> None:
        self.phone = phone
        self.server_ref = server_ref
        self.last_grid = None

    async def capture(self):
        return self.phone.shot

    async def aim(self, target: str, cell=None):
        t = self.server_ref["server"].state.target
        x, y = t["x"] * DPR + BIAS[0], t["y"] * DPR + BAR + BIAS[1]
        self.last_grid = (9, 20, W, H, 0)
        return int(x), int(y), "aimed by sight"


@pytest.mark.asyncio
async def test_calibration_measures_her_aim_and_corrects_it(tmp_path, monkeypatch):
    phone = FakePhone(tmp_path)
    ref: dict = {}
    real = tc.CalibrationServer

    def server(port):
        ref["server"] = real(0)
        return ref["server"]

    monkeypatch.setattr(tc, "CalibrationServer", server)
    report = await tc.run(phone, FakeVision(phone, ref), tmp_path, rounds=9,
                          rng=random.Random(4), settle=0.05)
    phone.browser.cancel()
    assert "Calibrated portrait (1080x2400)" in report
    assert "30px right and 12px up" in report
    assert "0/9 taps landed" not in report

    prof = tc.load_profiles(tmp_path)["1080x2400"]
    assert prof["global"] == pytest.approx([30, -12], abs=1) and prof["n"] == 9
    assert prof["mean_error_after_px"] < 1.5
    # Later aimed taps are shifted back onto the target.
    x, y, note = tc.correct(tmp_path, 530, 1188, W, H)
    assert abs(x - 500) <= 1 and abs(y - 1200) <= 1 and "calibrated" in note
    assert tc.correct(tmp_path, 530, 1188, H, W)[:2] == (530, 1188)  # landscape: not calibrated
    # The phone is left as it was: show taps back off, home screen.
    assert phone.settings["show_touches"] == "0"
    assert "input keyevent 3" in phone.shells


@pytest.mark.asyncio
async def test_landscape_rotates_and_restores(tmp_path, monkeypatch):
    phone = FakePhone(tmp_path)
    ref: dict = {}
    real = tc.CalibrationServer
    monkeypatch.setattr(tc, "CalibrationServer", lambda port: ref.setdefault("server", real(0)))
    await tc.run(phone, FakeVision(phone, ref), tmp_path, rounds=6, orientation="landscape",
                 rng=random.Random(1), settle=0.05)
    phone.browser.cancel()
    assert "settings put system user_rotation 1" in phone.shells
    assert phone.settings["user_rotation"] == "0" and phone.settings["accelerometer_rotation"] == "1"


@pytest.mark.asyncio
async def test_locked_screen_is_refused(tmp_path):
    phone = SimpleNamespace(screen_state=lambda: asyncio.sleep(0, "locked"))
    out = await tc.run(phone, None, tmp_path)
    assert "unlocked" in out


def test_fit_profile_regions_and_shrinkage():
    rng = random.Random(0)
    samples = []
    for _ in range(18):
        tx, ty = rng.uniform(0, W), rng.uniform(0, H)
        dx = 40 if tx < W / 3 else 0  # only drifts on the left third
        samples.append({"tx": tx, "ty": ty, "ax": tx + dx, "ay": ty})
    prof = tc.fit_profile(samples, W, H)
    left = [v for k, v in prof["regions"].items() if k.endswith(",0")]
    right = [v for k, v in prof["regions"].items() if k.endswith(",2")]
    assert all(v[0] > 15 for v in left) and all(abs(v[0]) < 15 for v in right)
    assert prof["mean_error_after_px"] < prof["mean_error_px"]


def test_mapping_rejects_nonsense():
    with pytest.raises(RuntimeError):
        tc.fit_mapping([((100, 100), (10, 10)), ((900, 900), (12, 12))], 3.0)
    m = tc.fit_mapping([((324, 830), (108, 240)), ((756, 1790), (252, 560))], 3.0)
    assert m.to_screen(108, 240) == pytest.approx((324, 830))


def test_small_errors_are_left_alone(tmp_path):
    tc.save_profile(tmp_path, {"size": [W, H], "global": [3, -2], "regions": {}, "applies": True})
    assert tc.correct(tmp_path, 500, 500, W, H) == (500, 500, "")


@pytest.mark.asyncio
async def test_phone_tap_target_uses_the_calibration(tmp_path):
    from ophelia.tools.registry import ToolRegistry

    tc.save_profile(tmp_path, {"size": [W, H], "global": [30, -12], "regions": {},
                               "applies": True})
    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = SimpleNamespace(data_dir=tmp_path)
    taps = []

    async def tap(x, y):
        taps.append((x, y))
        return "ok"

    async def aim(target, cell=None):
        return 530, 1188, "aimed by sight"

    reg.android = SimpleNamespace(mode="shizuku", tap=tap, display_size=lambda: None)
    reg.vision = SimpleNamespace(aim=aim, last_grid=(9, 20, W, H, 0))
    reg._phone_unavailable_reason = lambda: None

    async def after():
        return ""

    reg._after_action = after

    async def resolve(android, x, y):
        return int(x), int(y), ""

    import ophelia.tools.phone_tools as pt

    orig = pt._resolve_tap_coords
    pt._resolve_tap_coords = resolve
    try:
        out = await reg._phone_tap(target="PLAY button")
    finally:
        pt._resolve_tap_coords = orig
    assert taps == [(500, 1200)]
    assert "calibrated: moved 30px left and 12px down" in out


# Her first real run (2026-10-29), errors as logged: portrait leans right
# and down; landscape is mostly scatter.
PORTRAIT = [(0, 16), (19, 8), (20, 21), (43, 23), (11, -5), (11, 31), (54, 36), (16, 49),
            (35, 16), (60, 16), (40, -10), (21, 40), (-34, 57), (-13, 10), (69, 10),
            (-13, 35), (-27, 10), (-7, 40)]
LANDSCAPE = [(24, -15), (1, 21), (45, 38), (-11, 16), (19, 13), (4, 16), (6, 23), (70, -18),
             (-6, -35), (23, 23), (25, 15), (-30, 42), (-37, 13), (25, -10), (69, -1),
             (17, 3), (-17, -18), (1, -24)]


def _samples(errs, w, h):
    rng = random.Random(2)
    out = []
    for ex, ey in errs:
        tx, ty = rng.uniform(0, w), rng.uniform(0, h)
        out.append({"tx": tx, "ty": ty, "ax": tx + ex, "ay": ty + ey, "hit": False})
    return out


def test_a_lean_gets_corrected_but_scatter_does_not():
    p = tc.fit_profile(_samples(PORTRAIT, 1080, 2400), 1080, 2400)
    assert p["applies"] and p["global"] == [17.5, 18.5]
    assert p["mean_error_after_px"] < p["mean_error_px"] * 0.9
    land = tc.fit_profile(_samples(LANDSCAPE, 2400, 1080), 2400, 1080)
    assert not land["applies"]  # a fixed shift would make her worse there
    assert land["mean_error_after_px"] >= land["mean_error_px"] * 0.9
    assert len(land["samples"]) == 18


def test_scatter_profile_and_old_profiles_are_not_applied(tmp_path):
    tc.save_profile(tmp_path, tc.fit_profile(_samples(LANDSCAPE, 2400, 1080), 2400, 1080))
    assert tc.correct(tmp_path, 1200, 500, 2400, 1080) == (1200, 500, "")
    # Saved before the fair test existed: unused until she runs it again.
    tc.save_profile(tmp_path, {"size": [1080, 2400], "global": [17.5, 18.5], "regions": {}})
    assert tc.correct(tmp_path, 500, 500, 1080, 2400) == (500, 500, "")


# --- finer aim ---------------------------------------------------------------------


def _vision(answers, *, fine=True):
    from ophelia.android.vision import ScreenVision as VisionLoop

    v = VisionLoop.__new__(VisionLoop)
    v.settings = SimpleNamespace(phone_fine_aim=fine)
    asked = []

    async def ask(png, prompt):
        asked.append(prompt)
        return answers.pop(0)

    v._ask = ask
    return v, asked


def _screen_png():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (W, H), (20, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_fine_aim_takes_a_third_finer_step():
    png = _screen_png()
    box = (270, 600, 405, 750)  # a 135x150 cell
    v, asked = _vision(["6", "11"])
    x, y, note = await v._zoom_tap(png, box, W, H, "the 7 of clubs", "C5", 4)
    assert len(asked) == 2  # zoom + fine (the coarse cell was given)
    assert "fine square 11" in note
    # Step two alone: square 6 of a 270x300 window from (202, 525) = (303, 637).
    v2, _ = _vision(["6"], fine=False)
    cx, cy, _ = await v2._zoom_tap(png, box, W, H, "the 7 of clubs", "C5", 4)
    assert (cx, cy) == (303, 637)
    # The fine step lands inside that square, within a quarter-square grid.
    assert abs(x - cx) <= 67 and abs(y - cy) <= 75
    assert (x, y) != (cx, cy)


@pytest.mark.asyncio
async def test_fine_step_unclear_keeps_the_step_two_point():
    png = _screen_png()
    v, _ = _vision(["6", "no idea"])
    x, y, note = await v._zoom_tap(png, (270, 600, 405, 750), W, H, "x", "C5", 4)
    assert (x, y) == (303, 637) and "fine step unclear" in note


def test_correction_only_for_the_aim_it_was_measured_with(tmp_path):
    tc.save_profile(tmp_path, {"size": [W, H], "global": [30, -12], "regions": {},
                               "applies": True, "aim": "coarse"})
    assert tc.correct(tmp_path, 530, 1188, W, H, aim="fine") == (530, 1188, "")
    assert tc.correct(tmp_path, 530, 1188, W, H, aim="coarse")[:2] == (500, 1200)
