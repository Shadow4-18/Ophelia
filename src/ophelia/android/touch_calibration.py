"""Touch calibration: how far off her aim-by-sight is, and a correction.

`input tap` lands exactly where it's told. Her misses come from *choosing*
the point: she looks at a screenshot, picks a cell and a zoomed square, and
that pick is off by some amount, often the same way in the same part of
the screen. This measures it.

While it runs, a tiny local web server shows a calibration page in the
phone's browser: a dark screen with one red target. Two exact reference
taps (known pixels) map the page's coordinates onto screen pixels, then for
each round the target moves and she aims at it by sight, the same way she
aims in games. Her error per screen region is saved per screen size and
orientation, and later aim-by-sight taps are shifted by it.

No app to install: the page is plain HTML served from Ophelia itself.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

PORT = 8791
_REGIONS = 3  # 3x3 screen regions
_SHRINK = 2.0  # pseudo-samples pulling a region toward the global offset
_MIN_SHIFT = 6  # px: smaller corrections are noise
_MIN_GAIN = 0.9  # apply only if it cuts her error by 10%+ (fairly measured)
_TARGET_R = 30  # CSS px radius of the target circle
TARGET_DESC = "the red target circle (the calibration dot)"

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Ophelia touch calibration</title>
<style>
html,body{margin:0;height:100%;background:#15161c;overflow:hidden;touch-action:none;
  font-family:sans-serif;color:#8a8fa3;user-select:none;-webkit-user-select:none}
#t{position:fixed;border-radius:50%;background:#e8203a;box-shadow:0 0 0 6px #fff;display:none}
#t:after{content:"";position:absolute;left:50%;top:50%;width:6px;height:6px;margin:-3px;
  border-radius:50%;background:#fff}
#m{position:fixed;left:0;right:0;top:40%;text-align:center;font-size:18px}
#d{position:fixed;width:14px;height:14px;margin:-7px;border-radius:50%;background:#3ad17a;
  display:none;pointer-events:none}
</style></head><body>
<div id="m">Ophelia touch calibration: starting</div><div id="t"></div><div id="d"></div>
<script>
let full=false;
function vp(){return "vw="+innerWidth+"&vh="+innerHeight+"&dpr="+devicePixelRatio}
document.addEventListener("pointerdown",e=>{
  if(!full&&document.documentElement.requestFullscreen){
    document.documentElement.requestFullscreen().catch(()=>{});full=true}
  const d=document.getElementById("d");d.style.left=e.clientX+"px";d.style.top=e.clientY+"px";
  d.style.display="block";
  fetch("/tap",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({x:e.clientX,y:e.clientY,vw:innerWidth,vh:innerHeight,
      dpr:devicePixelRatio,t:Date.now()})}).catch(()=>{});
});
async function poll(){
  try{
    const s=await (await fetch("/state?"+vp())).json();
    const t=document.getElementById("t"),m=document.getElementById("m");
    m.textContent=s.message||"";
    if(s.target){const r=s.target.r;t.style.width=t.style.height=(2*r)+"px";
      t.style.left=(s.target.x-r)+"px";t.style.top=(s.target.y-r)+"px";t.style.display="block"}
    else t.style.display="none";
    if(s.target&&s.fresh)document.getElementById("d").style.display="none";
  }catch(e){}
  setTimeout(poll,250);
}
poll();
</script></body></html>
"""


# --- the page's server ---------------------------------------------------------


@dataclass
class PageState:
    target: dict | None = None  # {"x","y","r"} in CSS px
    message: str = "Ophelia touch calibration: starting"
    viewport: dict = field(default_factory=dict)  # vw, vh, dpr as last reported
    seen: asyncio.Event = field(default_factory=asyncio.Event)
    taps: asyncio.Queue = field(default_factory=asyncio.Queue)
    shown: int = 0  # target version the page last fetched
    version: int = 0


class CalibrationServer:
    """Minimal HTTP/1.1 server (stdlib asyncio): the page, its state, taps."""

    def __init__(self, port: int = PORT) -> None:
        self.port = port
        self.state = PageState()
        self._server: asyncio.AbstractServer | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        if not self.port:  # 0 = any free port (tests)
            self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 2)
            except Exception:
                pass
            self._server = None

    def show(self, target: dict | None, message: str = "") -> None:
        self.state.target = target
        self.state.message = message
        self.state.version += 1

    async def wait_shown(self, timeout: float = 4.0) -> bool:
        """Until the page has fetched the latest target (it polls 4x a second)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.state.shown >= self.state.version:
                await asyncio.sleep(0.35)  # let it paint
                return True
            await asyncio.sleep(0.1)
        return False

    async def next_tap(self, timeout: float = 4.0) -> dict | None:
        try:
            return await asyncio.wait_for(self.state.taps.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def drain_taps(self) -> None:
        while not self.state.taps.empty():
            self.state.taps.get_nowait()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = (lines[0].split(" ") + ["", "", ""])[:3]
            headers = {k.strip().lower(): v.strip() for k, _, v in
                       (ln.partition(":") for ln in lines[1:] if ":" in ln)}
            body = b""
            n = int(headers.get("content-length") or 0)
            if n:
                body = await asyncio.wait_for(reader.readexactly(min(n, 65536)), 5)
            status, ctype, payload = self.route(method, target, body)
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\n"
                f"Content-Length: {len(payload)}\r\nCache-Control: no-store\r\n"
                "Connection: close\r\n\r\n".encode() + payload)
            await writer.drain()
        except Exception as e:
            log.debug("touch_calibration.http_error", error=str(e))
        finally:
            writer.close()

    def route(self, method: str, target: str, body: bytes) -> tuple[str, str, bytes]:
        from urllib.parse import parse_qs, urlsplit

        url = urlsplit(target)
        if method == "GET" and url.path == "/":
            return "200 OK", "text/html; charset=utf-8", PAGE.encode()
        if method == "GET" and url.path == "/state":
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                self.state.viewport = {"vw": float(q["vw"]), "vh": float(q["vh"]),
                                       "dpr": float(q["dpr"])}
            except (KeyError, ValueError):
                pass
            self.state.seen.set()
            fresh = self.state.shown < self.state.version
            self.state.shown = self.state.version
            return "200 OK", "application/json", json.dumps(
                {"target": self.state.target, "message": self.state.message,
                 "fresh": fresh}).encode()
        if method == "POST" and url.path == "/tap":
            try:
                tap = json.loads(body or b"{}")
                self.state.taps.put_nowait({"x": float(tap["x"]), "y": float(tap["y"]),
                                            "dpr": float(tap.get("dpr") or 1)})
            except Exception:
                return "400 Bad Request", "text/plain", b"bad tap"
            return "200 OK", "application/json", b"{}"
        return "404 Not Found", "text/plain", b"not found"


# --- mapping page coordinates onto screen pixels --------------------------------


@dataclass
class Mapping:
    """screen_px = css * scale + offset, per axis (page sits under bars)."""

    sx: float
    ox: float
    sy: float
    oy: float

    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        return x * self.sx + self.ox, y * self.sy + self.oy

    def to_css(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.ox) / self.sx, (y - self.oy) / self.sy


def fit_mapping(pairs: list[tuple[tuple[float, float], tuple[float, float]]],
                dpr: float) -> Mapping:
    """From (screen px tapped, css px reported) pairs at two or more distinct
    points. Raises if they don't agree with the page's pixel ratio."""
    (s1, c1), (s2, c2) = pairs[0], pairs[-1]
    if abs(c2[0] - c1[0]) < 10 or abs(c2[1] - c1[1]) < 10:
        raise RuntimeError("reference taps too close together to map the page")
    sx = (s2[0] - s1[0]) / (c2[0] - c1[0])
    sy = (s2[1] - s1[1]) / (c2[1] - c1[1])
    for s in (sx, sy):
        if dpr and not (0.75 * dpr <= s <= 1.25 * dpr):
            raise RuntimeError(f"page scale {s:.2f} doesn't match the browser's ({dpr:.2f})")
    return Mapping(sx, s1[0] - c1[0] * sx, sy, s1[1] - c1[1] * sy)


# --- the correction ----------------------------------------------------------------


def _region(x: float, y: float, w: int, h: int) -> str:
    c = min(_REGIONS - 1, max(0, int(x / w * _REGIONS)))
    r = min(_REGIONS - 1, max(0, int(y / h * _REGIONS)))
    return f"{r},{c}"


def _fit(samples: list[dict], w: int, h: int) -> tuple[list[float], dict]:
    """(global offset, per-region offsets): median errors, a region pulled
    toward the global one when it has few samples."""
    errs = [(s["ax"] - s["tx"], s["ay"] - s["ty"]) for s in samples]
    gx = statistics.median(e[0] for e in errs)
    gy = statistics.median(e[1] for e in errs)
    by_region: dict[str, list[tuple[float, float]]] = {}
    for s, e in zip(samples, errs):
        by_region.setdefault(_region(s["tx"], s["ty"], w, h), []).append(e)
    regions = {}
    for key, es in by_region.items():
        n = len(es)
        rx = statistics.median(e[0] for e in es)
        ry = statistics.median(e[1] for e in es)
        regions[key] = [round((n * rx + _SHRINK * gx) / (n + _SHRINK), 1),
                        round((n * ry + _SHRINK * gy) / (n + _SHRINK), 1), n]
    return [round(gx, 1), round(gy, 1)], regions


def fit_profile(samples: list[dict], w: int, h: int) -> dict:
    """samples: {"tx","ty"} where the target was, {"ax","ay"} where she aimed
    (screen px); error = aimed - target.

    The "after" error is measured honestly: each tap is corrected by a fit
    made without it (leave-one-out). Scored on the taps it was fitted to, a
    correction always looks good; on her first real run it claimed 34->24 px
    where a fair test said 34->35. The correction only applies when the fair
    test says it helps."""
    glob, regions = _fit(samples, w, h)
    before, after = [], []
    for i, s in enumerate(samples):
        ex, ey = s["ax"] - s["tx"], s["ay"] - s["ty"]
        before.append(math.hypot(ex, ey))
        rest = samples[:i] + samples[i + 1:]
        g, r = _fit(rest, w, h) if rest else ([0, 0], {})
        ox, oy = _offset(r, g, s["tx"], s["ty"], w, h)
        after.append(math.hypot(ex - ox, ey - oy))
    mean_before = statistics.mean(before)
    mean_after = statistics.mean(after)
    return {
        "size": [w, h], "ts": round(time.time()), "n": len(samples),
        "global": glob, "regions": regions,
        "mean_error_px": round(mean_before, 1),
        "mean_error_after_px": round(mean_after, 1),
        "applies": mean_after <= mean_before * _MIN_GAIN,
        "hits": sum(1 for s in samples if s.get("hit")),
        "samples": [{k: round(float(v), 1) if k != "hit" else bool(v) for k, v in s.items()}
                    for s in samples],
    }


def _offset(regions: dict, glob: list, x: float, y: float, w: int, h: int) -> tuple[float, float]:
    r = regions.get(_region(x, y, w, h))
    return (r[0], r[1]) if r else (glob[0], glob[1])


def _path(data_dir: Path | str) -> Path:
    return Path(data_dir) / "touch_calibration.json"


def load_profiles(data_dir: Path | str) -> dict:
    try:
        data = json.loads(_path(data_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_profile(data_dir: Path | str, profile: dict) -> None:
    data = load_profiles(data_dir)
    w, h = profile["size"]
    data[f"{w}x{h}"] = profile
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def correct(data_dir: Path | str, x: int, y: int, w: int, h: int) -> tuple[int, int, str]:
    """Shift an aimed-by-sight point by her measured error for this screen
    size (orientation included). (x, y, note); unchanged when uncalibrated."""
    prof = load_profiles(data_dir).get(f"{w}x{h}")
    # Older profiles were scored on their own taps (no fair test): unused
    # until calibration is run again.
    if not prof or prof.get("applies") is not True:
        return x, y, ""
    dx, dy = _offset(prof.get("regions") or {}, prof.get("global") or [0, 0], x, y, w, h)
    if math.hypot(dx, dy) < _MIN_SHIFT:
        return x, y, ""
    nx = int(round(min(w - 1, max(0, x - dx))))
    ny = int(round(min(h - 1, max(0, y - dy))))
    return nx, ny, f" calibrated: moved {_describe(-dx, -dy)}."


def _describe(dx: float, dy: float) -> str:
    parts = []
    if abs(dx) >= 1:
        parts.append(f"{abs(dx):.0f}px {'right' if dx > 0 else 'left'}")
    if abs(dy) >= 1:
        parts.append(f"{abs(dy):.0f}px {'down' if dy > 0 else 'up'}")
    return " and ".join(parts) or "slightly"


def target_points(vw: float, vh: float, rounds: int, rng: random.Random) -> list[tuple[float, float]]:
    """Target centres in CSS px, spread over all 3x3 regions in turn."""
    margin_x, margin_y = max(_TARGET_R * 1.6, vw * 0.06), max(_TARGET_R * 1.6, vh * 0.06)
    cells = [(r, c) for r in range(_REGIONS) for c in range(_REGIONS)]
    rng.shuffle(cells)
    out = []
    for i in range(rounds):
        r, c = cells[i % len(cells)]
        x0 = max(margin_x, vw * c / _REGIONS)
        x1 = min(vw - margin_x, vw * (c + 1) / _REGIONS)
        y0 = max(margin_y, vh * r / _REGIONS)
        y1 = min(vh - margin_y, vh * (r + 1) / _REGIONS)
        out.append((rng.uniform(x0, max(x0, x1)), rng.uniform(y0, max(y0, y1))))
    return out


# --- running it on the phone ----------------------------------------------------------


def _stopped(msg: str) -> str:
    log.info("touch_calibration.stopped", reason=msg[:200])
    return msg


async def set_show_taps(android: Any, on: bool) -> str:
    """Android's "Show taps" developer option: a dot wherever a touch lands,
    visible in screenshots."""
    return await android.shell(f"settings put system show_touches {1 if on else 0}")


async def _setting(android: Any, name: str) -> str:
    out = (await android.shell(f"settings get system {name}") or "").strip()
    return out if out.lstrip("-").isdigit() else ""


async def run(android: Any, vision: Any, data_dir: Path | str, *, rounds: int = 18,
              orientation: str = "portrait", port: int = PORT,
              rng: random.Random | None = None, settle: float = 1.2) -> str:
    """The whole calibration. Returns a short report for her."""
    rng = rng or random.Random()
    rounds = max(6, min(36, int(rounds)))
    state = await android.screen_state() if hasattr(android, "screen_state") else "unknown"
    if state in ("off", "locked"):
        return _stopped("The screen is off or locked. Calibration needs it on and unlocked "
                "(ask him to unlock the phone) — then run it again.")
    server = CalibrationServer(port)
    try:
        await server.start()
    except OSError as e:
        return _stopped(f"Couldn't start the calibration page on port {port}: {e}")
    landscape = orientation.lower().startswith("land")
    restore: dict[str, str] = {}
    try:
        restore["show_touches"] = await _setting(android, "show_touches")
        await set_show_taps(android, True)
        if landscape:
            restore["accelerometer_rotation"] = await _setting(android, "accelerometer_rotation")
            restore["user_rotation"] = await _setting(android, "user_rotation")
            await android.shell("settings put system accelerometer_rotation 0")
            await android.shell("settings put system user_rotation 1")
        await android.shell(f"am start -a android.intent.action.VIEW -d {server.url}")
        try:
            await asyncio.wait_for(server.state.seen.wait(), 25)
        except asyncio.TimeoutError:
            return _stopped(("The calibration page didn't open (no browser answered "
                    f"{server.url}). Open it in the phone's browser and try again."))
        await asyncio.sleep(settle)
        shot = await vision.capture()
        from ophelia.android.vision import png_size

        size = png_size(shot.read_bytes()) if shot else None
        if not size:
            return _stopped("Couldn't take a screenshot to size the screen.")
        w, h = size
        if hasattr(android, "learn_display_size"):
            android.learn_display_size(size)

        # 1) A first tap puts the page in full screen; 2) two exact taps at
        # known pixels map page coordinates onto the screen.
        server.show(None, "Calibrating: hold on")
        await android.tap(w // 2, h // 2)
        await asyncio.sleep(settle + 0.6)
        server.drain_taps()
        pairs = []
        for px, py in ((w * 0.3, h * 0.3), (w * 0.7, h * 0.7)):
            await android.tap(int(px), int(py))
            tap = await server.next_tap()
            if tap is None:
                return _stopped(("The page didn't register the reference taps; is something "
                        "covering the browser? Calibration stopped."))
            pairs.append(((int(px), int(py)), (tap["x"], tap["y"])))
        try:
            mapping = fit_mapping(pairs, server.state.viewport.get("dpr") or 0.0)
        except RuntimeError as e:
            return _stopped(f"Couldn't line the page up with the screen ({e}). "
                            "Nothing was changed.")
        vw = server.state.viewport.get("vw") or w / mapping.sx
        vh = server.state.viewport.get("vh") or h / mapping.sy

        samples = []
        for i, (cx, cy) in enumerate(target_points(vw, vh, rounds, rng)):
            server.show({"x": round(cx, 1), "y": round(cy, 1), "r": _TARGET_R},
                        f"{i + 1}/{rounds}")
            await server.wait_shown()
            server.drain_taps()
            tx, ty = mapping.to_screen(cx, cy)
            try:
                ax, ay, _ = await vision.aim(TARGET_DESC)
            except Exception as e:
                log.info("touch_calibration.miss", round=i + 1, error=str(e)[:120])
                continue
            await android.tap(int(ax), int(ay))
            tap = await server.next_tap(3.0)
            hit = False
            if tap is not None:
                hit = math.hypot(tap["x"] - cx, tap["y"] - cy) <= _TARGET_R
            samples.append({"tx": round(tx, 1), "ty": round(ty, 1), "ax": ax, "ay": ay,
                            "hit": hit})
            log.info("touch_calibration.round", round=i + 1, err_x=round(ax - tx),
                     err_y=round(ay - ty), hit=hit)
        if len(samples) < 4:
            return _stopped((f"Only {len(samples)} usable rounds (vision couldn't find the target); "
                    "not enough to calibrate. Nothing was changed."))
        profile = fit_profile(samples, w, h)
        save_profile(data_dir, profile)
        server.show(None, f"Done: off by {profile['mean_error_px']:.0f}px on average")
        await asyncio.sleep(1.0)
        log.info("touch_calibration.saved", size=f"{w}x{h}", n=profile["n"],
                 mean=profile["mean_error_px"], after=profile["mean_error_after_px"])
        gx, gy = profile["global"]
        head = (f"Calibrated {'landscape' if landscape else 'portrait'} ({w}x{h}): "
                f"{profile['hits']}/{profile['n']} taps landed on the target; you were off "
                f"by {profile['mean_error_px']:.0f}px on average, leaning "
                f"{_describe(gx, gy)}. ")
        if profile["applies"]:
            return head + (
                f"Aim-by-sight taps are now corrected for that lean (fair estimate: "
                f"{profile['mean_error_after_px']:.0f}px). Run it again any time, or in "
                "the other orientation for games.")
        return head + (
            "That's mostly random scatter, not a steady lean, so a fixed correction "
            f"wouldn't help (fair estimate {profile['mean_error_after_px']:.0f}px) and "
            "none is applied. The misses come from how precisely you pick the spot.")
    finally:
        try:
            await android.shell("input keyevent 4")  # leave full screen
            await android.shell("input keyevent 3")  # home
            if restore.get("show_touches") in ("0", ""):
                await set_show_taps(android, False)
            if landscape:
                if restore.get("user_rotation"):
                    await android.shell(
                        f"settings put system user_rotation {restore['user_rotation']}")
                if restore.get("accelerometer_rotation"):
                    await android.shell("settings put system accelerometer_rotation "
                                        f"{restore['accelerometer_rotation']}")
        except Exception as e:
            log.debug("touch_calibration.restore_failed", error=str(e))
        await server.stop()
