"""Her own day (day log), body clock (energy + sleep), seeing her own web
pages, and the less-mechanical consciousness prompts."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ophelia.mind import body_clock, day_log


def _settings(tmp_path, **kw):
    base = dict(data_dir=tmp_path / "data", timezone="UTC", sleep_hours="23-7",
                quiet_hours="", body_clock_enabled=True)
    base.update(kw)
    return SimpleNamespace(**base)


# --- day log -----------------------------------------------------------------


def test_day_log_merges_repeats_and_reads_naturally(tmp_path):
    s = _settings(tmp_path)
    day_log.note_tool(s, "generate_image", {"prompt": "goth girl in a neon alley"},
                      "Image saved to /a.png\nImage saved to /b.png [civitai]",
                      autonomous=True, owner=True)
    day_log.note_tool(s, "generate_image", {"prompt": "rainy rooftop"},
                      "Image saved to /c.png", autonomous=True, owner=True)
    day_log.note_tool(s, "generate_image", {"prompt": "x"}, "Refused: no", autonomous=True,
                      owner=True)
    day_log.note_tool(s, "web_search", {"query": "x"}, "results", autonomous=True, owner=True)
    day_log.record_chat(s, "your owner", "Send me the last 4 images created please")
    day_log.record_chat(s, "your owner", "thanks")
    block = day_log.context_block(s)
    assert block.startswith("# Your day so far")
    assert "made 3 images on your own: goth girl in a neon alley; rainy rooftop" in block
    assert 'talked with your owner (started with "Send me the last 4 images created please")' in block
    assert "web_search" not in block and block.count("\n- ") == 2  # refusal + search skipped
    entries = day_log.load(s)
    assert entries[-1]["count"] == 2  # two chat messages, one stretch


def test_day_log_singular_and_owner_requests(tmp_path):
    s = _settings(tmp_path)
    day_log.note_tool(s, "generate_image", {"prompt": "cat"}, "Image saved to /a.png",
                      autonomous=False, owner=True)
    assert "made 1 image because your owner asked: cat" in day_log.context_block(s)


def test_day_log_never_breaks_without_settings():
    day_log.record(None, "x", "y")
    assert day_log.context_block(SimpleNamespace(data_dir=None)) == ""


# --- body clock ---------------------------------------------------------------


def _clock_at(tmp_path, hour, **kw):
    s = _settings(tmp_path, **kw)
    clock = body_clock.BodyClock(s, path=tmp_path / "bc.json")
    fixed = datetime(2026, 10, 20, hour, 30, tzinfo=body_clock.BodyClock._now(clock).tzinfo)
    clock._now = lambda: fixed  # type: ignore[method-assign]
    return clock


def test_sleep_window_follows_owner_and_she_stays_up_an_hour(tmp_path):
    assert _clock_at(tmp_path, 12).sleep_window() == (0, 7)
    assert _clock_at(tmp_path, 12, sleep_hours="").sleep_window() == (1, 8)


def test_energy_curve_evening_best_night_lowest(tmp_path):
    night = _clock_at(tmp_path, 3).target_energy()
    morning = _clock_at(tmp_path, 7).target_energy()
    evening = _clock_at(tmp_path, 21).target_energy()
    assert night < morning < evening


def test_falls_asleep_at_night_and_owner_wakes_her(tmp_path):
    clock = _clock_at(tmp_path, 3)
    clock.state.energy = 0.3
    clock.state.updated_at = time.time()
    assert clock.update() == ["fell asleep"]
    assert clock.asleep and clock.phase() == "asleep"
    assert clock.interval_multiplier() == 4.0 and clock.threshold_delta() == 1.0
    assert clock.on_owner_message() is True
    assert not clock.asleep
    line = clock.felt_line(in_chat=True)
    assert "woke you" in line and "still gets done" in line
    # Right after he talks she doesn't drop off mid-conversation.
    clock.state.energy = 0.2
    assert clock.update() == []


def test_wakes_in_the_morning(tmp_path):
    clock = _clock_at(tmp_path, 9)
    clock.state.asleep = True
    clock.state.updated_at = time.time()
    assert clock.update() == ["woke up"]
    assert "woke up a little while ago" in clock.felt_line(in_chat=False)


def test_tiredness_slows_her_and_raises_the_bar(tmp_path):
    clock = _clock_at(tmp_path, 15)
    clock.state.energy = 0.9
    fresh = (clock.interval_multiplier(), clock.threshold_delta(), clock.phase())
    for _ in range(12):
        clock.spend("act")
    tired = (clock.interval_multiplier(), clock.threshold_delta(), clock.phase())
    assert tired[0] > fresh[0] and tired[1] > fresh[1]
    assert fresh[2] == "wired" and tired[2] == "drowsy"
    assert "running on fumes" in clock.felt_line(in_chat=False)


def test_state_survives_restart(tmp_path):
    clock = _clock_at(tmp_path, 15)
    clock.state.energy = 0.42
    clock.save()
    again = body_clock.BodyClock(_settings(tmp_path), path=tmp_path / "bc.json")
    assert again.state.energy == 0.42


def test_body_clock_off_or_unconfigured(tmp_path):
    assert body_clock.get(_settings(tmp_path, body_clock_enabled=False)) is None
    assert body_clock.get(SimpleNamespace(data_dir=object())) is None


# --- seeing her own pages ----------------------------------------------------------


@pytest.mark.asyncio
async def test_look_at_web_page(monkeypatch, tmp_path):
    from ophelia.tools import web_view
    from ophelia.tools.registry import ToolRegistry

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.microlink.io":
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={"status": "success", "data": {
                "screenshot": {"url": "https://iad.microlink.io/x.png"}}})
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"x" * 100)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}))

    async def describe(settings, path, *, question, stack=None):
        seen["question"] = question
        assert Path(path).is_file()
        return "Header spans the full width; cards have wide side gutters."

    monkeypatch.setattr("ophelia.media.vision_input.describe_image_file", describe)
    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = _settings(tmp_path)
    reg.stack = None
    reg.artifacts_dir = tmp_path / "artifacts"
    out = await reg._look_at_web_page("www.ophelian.space/p/wiki", width="phone-50",
                                      question="is the header boxed?")
    assert seen["params"]["url"] == "https://www.ophelian.space/p/wiki"
    assert seen["params"]["viewport.width"] == "824"
    assert "phone at 50% zoom" in seen["question"] and "header boxed" in seen["question"]
    assert out.startswith("Header spans") and "screenshot file:" in out
    assert "saved to" not in out.lower()  # not auto-sent to chat
    assert web_view.resolve_width("desktop") == 1366 and web_view.resolve_width(1000) == 1000


@pytest.mark.asyncio
async def test_look_at_web_page_failure_is_explained(monkeypatch, tmp_path):
    from ophelia.tools.registry import ToolRegistry

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: real(*a, **{
        **k, "transport": httpx.MockTransport(
            lambda r: httpx.Response(429, json={"status": "fail", "message": "rate limited"}))}))
    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings, reg.stack, reg.artifacts_dir = _settings(tmp_path), None, tmp_path
    out = await reg._look_at_web_page("https://x.example")
    assert out.startswith("Couldn't render") and "rate limited" in out


@pytest.mark.asyncio
async def test_tool_calls_land_in_her_day(tmp_path):
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = _settings(tmp_path)
    reg._handlers = {"generate_image": lambda **kw: _ret("Image saved to /x.png")}
    reg.mcp = SimpleNamespace(dispatch=_none)
    reg.ensure_mcp = _none
    reg.set_owner(True)
    clock = body_clock.get(reg.settings)
    before = clock.state.energy
    assert await reg.dispatch("generate_image", json.dumps({"prompt": "moon"})) == "Image saved to /x.png"
    assert "made 1 image because your owner asked: moon" in day_log.context_block(reg.settings)
    assert clock.state.energy < before


async def _ret(v):
    return v


async def _none(*a, **k):
    return None


# --- less mechanical prompts --------------------------------------------------------


def test_prompts_carry_feelings_not_numbers():
    from ophelia.mind.drives import DriveState
    from ophelia.mind.psyche import Mood, PsycheState

    d = DriveState(social=0.9, boredom=0.6, curiosity=0.1, agency=0.2, expressiveness=0.1)
    block = d.to_context_block()
    assert "=" not in block and "0." not in block and block.startswith("What's pulling at you")
    calm = DriveState(social=0.1, boredom=0.1, curiosity=0.1, agency=0.1, expressiveness=0.1)
    assert "content to just be" in calm.to_context_block()
    p = PsycheState(mood=Mood(valence=-0.6, arousal=0.8, label="furious"))
    text = p.to_context_block()
    assert "valence" not in text and "furious, in a dark place and keyed up" in text


def test_short_reply_only_takes_the_edge_off():
    from ophelia.mind.drives import DriveState

    a = DriveState(social=0.9, boredom=0.8)
    a.on_user_message("k")
    b = DriveState(social=0.9, boredom=0.8)
    b.on_user_message("hey, sorry, long day at work. what have you been up to all afternoon?")
    assert b.social == pytest.approx(0.05) and a.social > 0.3


def test_moment_text_and_idle_nudges_vary(monkeypatch):
    from ophelia.mind import consciousness as c
    from ophelia.mind.drives import DriveState

    texts = {c._moment_text(4000, None) for _ in range(30)}
    assert len(texts) > 1 and all("hours since he last said" in t or "hour since" in t
                                  for t in texts)
    lonely = DriveState(social=0.95, boredom=0.05, curiosity=0.05, agency=0.05,
                        expressiveness=0.05)
    picks = [c._idle_nudge(lonely, None) for _ in range(60)]
    social_lines = set(c._IDLE_MODES["social"])
    assert sum(any(p.startswith(s) for s in social_lines) for p in picks) > 30


def test_trim_to_sentence():
    from ophelia.mind.consciousness import _trim_to_sentence

    assert _trim_to_sentence("Short.", 50) == "Short."
    assert _trim_to_sentence("Hello there. This keeps going and going forever", 30) == "Hello there."
    assert _trim_to_sentence("one two three four five six seven", 20).endswith("...")


def test_promises_come_back_at_sensible_times():
    from ophelia.mind.open_loops import promise_due_hours

    assert 16 <= promise_due_hours("I'll tell you tomorrow") <= 24
    assert promise_due_hours("give me a sec, I'll check in a bit") <= 1.5
    vals = {round(promise_due_hours("I'll look into it"), 2) for _ in range(20)}
    assert len(vals) > 5 and all(1.5 <= v <= 6 for v in vals)


def test_owner_up_late_is_not_asleep(tmp_path):
    from ophelia.mind.life_context import LifeContext

    s = SimpleNamespace(sleep_hours="23-7", quiet_hours="", work_days="", work_hours="",
                        timezone="UTC", sleep_mode_enabled=True, home_wifi_ssid="")
    life = LifeContext(s, SimpleNamespace(last_user_message_at=time.time() - 60))
    # 2am: inside sleep hours no matter when the suite runs.
    life.now = lambda: datetime(2026, 1, 15, 2, 0, tzinfo=life.tz())
    assert life.infer_owner_state() == "up_late" and not life.is_sleep_mode()
    assert "he's up, talking to you" in life.to_context_block()
    life.signals.last_user_message_at = time.time() - 3 * 3600
    assert life.infer_owner_state() == "asleep" and life.is_sleep_mode()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed, closed", [(True, True), (False, False)])
async def test_follow_up_only_closes_when_message_is_actually_sent(allowed, closed):
    from unittest.mock import AsyncMock, MagicMock

    from test_autonomous_turns import _wire

    from ophelia.mind.consciousness import ConsciousnessLoop

    loop = ConsciousnessLoop.__new__(ConsciousnessLoop)
    agent = MagicMock()
    agent.run_consciousness_tick = AsyncMock(return_value=json.dumps(
        {"action": "message", "internal_thought": "check on him",
         "outward_message": "Did you ever fix the sink? Be honest."}))
    store = MagicMock()
    store.save_psyche = AsyncMock()
    store.save_drives = AsyncMock()
    store.append_message = AsyncMock()
    store.recent_messages = AsyncMock(return_value=[])
    notify = AsyncMock()
    _wire(loop, agent, store, notify)
    loop.governor.allow_outreach.return_value = (allowed, "ok" if allowed else "rate")
    loops = MagicMock(close=AsyncMock(), mark_surfaced=AsyncMock())
    loop._due_loops = AsyncMock(return_value=[{"id": 5, "text": "ask about the sink",
                                                 "due_at": time.time(), "created_at": time.time()}])
    loop._loops = lambda: loops
    loop._outreach_delta = AsyncMock(return_value=0.0)

    await loop._tick(idle_seconds=4000)

    assert notify.await_count == (1 if allowed else 0)
    assert loops.close.await_count == (1 if closed else 0)
    assert loops.mark_surfaced.await_count == (0 if closed else 1)
