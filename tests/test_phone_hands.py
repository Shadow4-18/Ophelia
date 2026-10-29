"""Phone body: the right coordinate space, a readable numbered screen, taps
by element / label / sight, scrolling by direction, typing."""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ophelia.android import screen_elements as se

# A trimmed Samsung-style dump: a full-screen container, a toolbar button
# whose label lives on a child, a text field, a toggle, a scrolling list
# with two rows, and plain text inside a clickable row.
DUMP = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node index="0" text="" class="android.widget.FrameLayout" package="com.whatsapp" clickable="true" bounds="[0,0][1080,2400]">
  <node index="0" text="" content-desc="Back" class="android.widget.ImageButton" package="com.whatsapp" clickable="true" bounds="[0,96][126,222]" />
  <node index="1" text="" class="android.widget.LinearLayout" package="com.whatsapp" clickable="true" bounds="[140,100][700,220]">
    <node index="0" text="Okia" class="android.widget.TextView" package="com.whatsapp" clickable="false" bounds="[160,110][400,170]" />
  </node>
  <node index="2" text="" class="androidx.recyclerview.widget.RecyclerView" package="com.whatsapp" scrollable="true" clickable="false" bounds="[0,240][1080,2000]">
    <node index="0" text="" class="android.widget.FrameLayout" package="com.whatsapp" clickable="true" bounds="[0,300][1080,420]">
      <node index="0" text="see you tonight" class="android.widget.TextView" package="com.whatsapp" bounds="[40,320][600,400]" />
    </node>
    <node index="1" text="Dark mode" class="android.widget.Switch" package="com.whatsapp" checkable="true" checked="true" clickable="true" bounds="[0,500][1080,620]" />
  </node>
  <node index="3" text="Message" class="android.widget.EditText" package="com.whatsapp" clickable="true" bounds="[30,2100][880,2220]" />
  <node index="4" text="" content-desc="Send" class="android.widget.ImageButton" package="com.whatsapp" clickable="true" bounds="[900,2100][1050,2250]" />
</node></hierarchy>"""


def test_parse_numbers_elements_top_to_bottom_with_labels():
    els = se.parse(DUMP, screen=(1080, 2400))
    lines = [e.line() for e in els]
    assert lines[0].startswith('[1] button "Back"')
    assert any('item "Okia"' in ln for ln in lines)  # label pulled from child text
    assert any(ln.startswith("[") and 'toggle "Dark mode" (on)' in ln for ln in lines)
    assert any('field "Message"' in ln for ln in lines)
    assert any('list' in ln for ln in lines)
    # Full-screen container dropped; text duplicated by its row dropped.
    assert not any("FrameLayout" in ln for ln in lines)
    assert sum('"see you tonight"' in ln for ln in lines) == 1
    send = se.find(els, "send")
    assert send.center == (975, 2175) and send.kind == "button"
    assert se.find(els, "okia").label == "Okia"
    assert se.find(els, "nothing like this") is None
    assert "com.whatsapp" in se.describe(els)


def test_truncated_dump_still_parses():
    els = se.parse(DUMP[: len(DUMP) // 2], screen=(1080, 2400))
    assert any(e.label == "Back" for e in els)


def test_empty_screen_points_to_aiming():
    assert "target=" in se.describe([])


@pytest.mark.asyncio
async def test_display_size_uses_the_size_in_use_not_the_panel():
    from ophelia.android.shizuku import AndroidBody

    body = AndroidBody.__new__(AndroidBody)
    body._display_size = None
    body.shell = AsyncMock(return_value="Physical size: 1440x3200\nOverride size: 1080x2400")
    assert await body.display_size() == (1080, 2400)
    body.learn_display_size((720, 1600))
    assert await body.display_size() == (720, 1600)


@pytest.mark.asyncio
async def test_typing_escapes_for_the_shell():
    from ophelia.android.shizuku import AndroidBody

    body = AndroidBody.__new__(AndroidBody)
    body.shell = AsyncMock(return_value="(ok)")
    await body.type_text("hi babe, it's me!")
    sent = body.shell.await_args.args[0]
    assert sent == "input text hi%sbabe,%sit\\'s%sme\\!"
    assert "nothing typeable" in await body.type_text("💀")


def _registry(elements=None, aim=None):
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.settings = SimpleNamespace(data_dir=None)
    android = SimpleNamespace(
        mode="rish",
        tap=AsyncMock(return_value="(ok)"),
        swipe=AsyncMock(return_value="(ok)"),
        type_text=AsyncMock(return_value="(ok)"),
        shell=AsyncMock(return_value="(ok)"),
        display_size=AsyncMock(return_value=(1080, 2400)),
    )
    reg.android = android
    els = elements if elements is not None else se.parse(DUMP, screen=(1080, 2400))

    async def read_elements():
        vision.last_elements = els
        vision.last_elements_at = __import__("time").time()
        return els

    vision = SimpleNamespace(last_elements=[], last_elements_at=0.0,
                             read_elements=read_elements, aim=aim)
    reg.vision = vision
    return reg


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    from ophelia.tools import phone_tools

    async def instant(_s):
        return None

    monkeypatch.setattr(phone_tools.asyncio, "sleep", instant)


@pytest.mark.asyncio
async def test_tap_by_element_label_and_sight():
    reg = _registry(aim=AsyncMock(return_value=(512, 1300, "aimed by sight: cell D11, square 6")))
    await reg._phone_ui_dump()
    send = next(e for e in reg.vision.last_elements if e.label == "Send")
    out = await reg._phone_tap(element=send.n)
    reg.android.tap.assert_awaited_with(975, 2175)
    assert 'Tapped (975,2175) on [' in out and "Now on screen" in out
    await reg._phone_tap(text="Okia")
    reg.android.tap.assert_awaited_with(420, 160)
    out = await reg._phone_tap(target="the red play button")
    reg.android.tap.assert_awaited_with(512, 1300)
    assert "aimed by sight" in out
    assert "No element 99" in await reg._phone_tap(element=99)
    assert "Say what to tap" in await reg._phone_tap()


@pytest.mark.asyncio
async def test_long_press_and_scroll():
    reg = _registry()
    await reg._phone_tap(text="Okia", long_press=True)
    reg.android.swipe.assert_awaited_with(420, 160, 420, 160, 700)
    out = await reg._phone_swipe(scroll="down")
    x1, y1, x2, y2, ms = reg.android.swipe.await_args.args
    assert x1 == x2 == 540 and y1 > y2 and ms >= 350  # finger moves up = content down
    assert out.startswith("Scrolled down")
    lst = next(e for e in reg.vision.last_elements if e.kind == "list")
    await reg._phone_swipe(scroll="up", element=lst.n, amount=0.5)
    x1, y1, x2, y2, _ = reg.android.swipe.await_args.args
    assert y1 < y2 and 240 < y1 and y2 < 2000


@pytest.mark.asyncio
async def test_type_into_field_and_send():
    reg = _registry()
    await reg._phone_ui_dump()
    field = next(e for e in reg.vision.last_elements if e.kind == "field")
    out = await reg._phone_type("on my way", element=field.n, enter=True)
    reg.android.tap.assert_awaited_with(455, 2160)
    reg.android.type_text.assert_awaited_with("on my way")
    reg.android.shell.assert_awaited_with("input keyevent 66")
    assert "Typed 'on my way' and pressed enter" in out


@pytest.mark.asyncio
async def test_aim_two_steps(tmp_path):
    from PIL import Image

    from ophelia.android.vision import ScreenVision

    buf = io.BytesIO()
    Image.new("RGB", (1080, 2400), (20, 20, 20)).save(buf, format="PNG")
    shot = tmp_path / "s.png"
    shot.write_bytes(buf.getvalue())
    v = ScreenVision.__new__(ScreenVision)
    v.settings = SimpleNamespace(phone_fine_aim=False)  # the two-step path
    v.android = SimpleNamespace(learn_display_size=lambda size: None)
    v.capture = AsyncMock(return_value=shot)
    v._ask = AsyncMock(side_effect=["C7", "6"])
    x, y, note = await v.aim("the gem")
    # Portrait: 8x16 cells of 135x150 px. C7 = column 2, row 6 (x 270..405,
    # y 900..1050); the zoom adds half a cell around it (x 202..472, y
    # 825..1125); square 6 of 4x4 = col 1, row 1.
    assert (x, y) == (303, 937)
    assert "cell C7, square 6" in note
    v._ask = AsyncMock(side_effect=["NONE"])
    with pytest.raises(RuntimeError, match="couldn't find"):
        await v.aim("a unicorn")


def test_marks_draw_on_screenshot():
    from PIL import Image

    from ophelia.android.vision import _annotate_marks

    buf = io.BytesIO()
    Image.new("RGB", (540, 1200), (0, 0, 0)).save(buf, format="PNG")
    raw = buf.getvalue()
    out = _annotate_marks(raw, se.parse(DUMP, screen=(1080, 2400)), (1080, 2400))
    assert out != raw
    img = Image.open(io.BytesIO(out))
    assert img.size == (540, 1200)
    # The Send button box (900..1050 x 2100..2250) is drawn at half scale.
    assert img.getpixel((450, 1100)) != (0, 0, 0)


def test_landscape_games_get_a_landscape_grid():
    from ophelia.android.vision import ScreenVision, grid_shape

    assert grid_shape(2400, 1080) == (16, 8) and grid_shape(1080, 2400) == (8, 16)
    v = ScreenVision.__new__(ScreenVision)
    v.last_grid = (16, 8, 2400, 1080, __import__("time").time())
    # Balatro's PLAY sat around (900, 900) on the owner's grid screenshot.
    assert v.cell_box("g7") == (900, 810, 1050, 945)
    with pytest.raises(RuntimeError, match="off the grid"):
        v.cell_box("Z1")
    v.last_grid = None
    with pytest.raises(RuntimeError, match="look at the screen first"):
        v.cell_box("A1")


@pytest.mark.asyncio
async def test_tap_by_cell_and_cell_plus_target():
    from ophelia.android.vision import ScreenVision

    reg = _registry(elements=[], aim=AsyncMock(return_value=(960, 880, "aimed by sight: cell G7, square 6")))
    real = ScreenVision.__new__(ScreenVision)
    real.last_grid = (16, 8, 2400, 1080, __import__("time").time())
    reg.vision.cell_box = real.cell_box
    out = await reg._phone_tap(cell="G7")
    reg.android.tap.assert_awaited_with(975, 877)
    assert "center of cell G7" in out and "lettered cells" in out
    await reg._phone_tap(cell="G7", target="PLAY button")
    reg.vision.aim.assert_awaited_with("PLAY button", cell="G7")
    reg.android.tap.assert_awaited_with(960, 880)


@pytest.mark.asyncio
async def test_blank_vision_answer_is_retried():
    from ophelia.android.vision import ScreenVision

    calls = []

    class _Client:
        class chat:
            class completions:
                @staticmethod
                async def create(**kw):
                    calls.append(kw)
                    text = "" if len(calls) == 1 else "G7"
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    v = ScreenVision.__new__(ScreenVision)
    v.settings = SimpleNamespace(deepseek_thinking=False)
    v.stack = SimpleNamespace(name=lambda role: "xai")
    v._vision_client = AsyncMock(return_value=(_Client, "grok"))
    assert await v._ask(b"png", "which cell?") == "G7"
    assert len(calls) == 2 and calls[0]["max_tokens"] >= 300


@pytest.mark.asyncio
async def test_telegram_menu_replaces_every_scope():
    """Hermes's menu lived in a narrower scope than the default one Ophelia
    overwrote, so it kept showing. Every scope is cleared, then hers is set."""
    from ophelia.channels.telegram_bot import TelegramGateway

    gw = TelegramGateway.__new__(TelegramGateway)
    gw.settings = SimpleNamespace(allowed_telegram_users=lambda: {111})
    deleted, setted = [], []

    class _Bot:
        async def delete_my_commands(self, scope=None, language_code=None):
            deleted.append((type(scope).__name__, getattr(scope, "chat_id", None), language_code))
            return True

        async def set_my_commands(self, commands, scope=None, language_code=None):
            setted.append((type(scope).__name__, [c.command for c in commands]))
            return True

    await gw._register_commands(_Bot())
    kinds = {d[0] for d in deleted}
    assert {"BotCommandScopeDefault", "BotCommandScopeAllPrivateChats",
            "BotCommandScopeAllGroupChats", "BotCommandScopeChat"} <= kinds
    assert ("BotCommandScopeChat", 111, "en") in deleted
    assert [s[0] for s in setted] == ["BotCommandScopeDefault", "BotCommandScopeAllPrivateChats"]
    assert "sessions" not in setted[0][1] and "status" in setted[0][1]


@pytest.mark.asyncio
async def test_aim_targets_the_uncovered_part(tmp_path):
    """Fanned cards overlap: aiming at a card's middle hits the next card."""
    from PIL import Image

    from ophelia.android.vision import ScreenVision

    buf = io.BytesIO()
    Image.new("RGB", (2400, 1080), (20, 20, 20)).save(buf, format="PNG")
    shot = tmp_path / "s.png"
    shot.write_bytes(buf.getvalue())
    v = ScreenVision.__new__(ScreenVision)
    v.android = SimpleNamespace(learn_display_size=lambda size: None)
    v.capture = AsyncMock(return_value=shot)
    v._ask = AsyncMock(side_effect=["G6", "5", "7"])
    await v.aim("7 of clubs")
    assert v._ask.await_count == 3  # coarse, zoom, fine
    for call in v._ask.await_args_list:
        prompt = call.args[1]
        assert "uncovered" in prompt and "center of" not in prompt


@pytest.mark.asyncio
async def test_look_closer_zooms_with_the_same_cell_names(tmp_path):
    from PIL import Image

    from ophelia.android.vision import ScreenVision

    buf = io.BytesIO()
    Image.new("RGB", (2400, 1080), (20, 20, 20)).save(buf, format="PNG")
    shot = tmp_path / "s.png"
    shot.write_bytes(buf.getvalue())
    v = ScreenVision.__new__(ScreenVision)
    v.capture = AsyncMock(return_value=shot)
    seen = {}

    async def look(png, prompt):
        seen["size"] = Image.open(io.BytesIO(png)).size
        seen["prompt"] = prompt
        return "J♠ J♣ 7♣(raised) 5♣ 4♠"

    v._look = look
    out = await v.look_closer("F5:L7", "which cards are raised?")
    assert out.endswith("(raised) 5♣ 4♠")
    # Landscape 16x8 cells of 150x135: F5:L7 is 7x3 cells = 1050x405 px, upscaled.
    assert seen["size"][0] > 1050 and abs(seen["size"][0] / seen["size"][1] - 1050 / 405) < 0.01
    assert "cells F5:L7" in seen["prompt"] and "raised" in seen["prompt"]
    assert v.last_grid[:2] == (16, 8)  # phone_tap(cell=...) works off the zoom
    assert "isn't a cell" in await v.look_closer("hand area")
    assert "off the grid" in await v.look_closer("F5:Z9")


def test_shell_reports_stderr_when_stdout_is_empty():
    from ophelia.android.shizuku import _no_output

    assert _no_output("Error: no such file") == "Error: no such file"
    assert _no_output("") == "(finished, no output)"


@pytest.mark.asyncio
async def test_game_look_sends_a_clean_copy_to_read_from(tmp_path):
    """Cell labels sat on card corners; she now also gets the unmarked screen."""
    import base64

    from PIL import Image

    from ophelia.android.vision import _VISION_TEMPERATURE, ScreenVision

    buf = io.BytesIO()
    Image.new("RGB", (2400, 1080), (20, 20, 20)).save(buf, format="PNG")
    shot = tmp_path / "s.png"
    shot.write_bytes(buf.getvalue())
    v = ScreenVision.__new__(ScreenVision)
    v.android = SimpleNamespace(mode="shizuku", display_size=AsyncMock(return_value=(2400, 1080)),
                                learn_display_size=lambda size: None)
    v.settings = SimpleNamespace(vision_grid_overlay=True, deepseek_thinking=False)
    v.stack = SimpleNamespace(supports_vision=lambda: True, name=lambda role: "deepseek")
    v.capture = AsyncMock(return_value=shot)
    v.read_elements = AsyncMock(return_value=[])
    sent = {}

    async def create(**kw):
        sent.update(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="hand: A♥ K♥"))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    v._vision_client = AsyncMock(return_value=(client, "deepseek-flash"))
    assert await v.see(question="what's in my hand?") == "hand: A♥ K♥"
    images = [p["image_url"]["url"] for p in sent["messages"][0]["content"] if p["type"] == "image_url"]
    assert len(images) == 2
    assert base64.b64decode(images[0].split(",", 1)[1]) == shot.read_bytes()  # clean first
    assert images[1] != images[0]  # then the gridded one
    assert sent["temperature"] == _VISION_TEMPERATURE
