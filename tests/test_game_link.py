"""Game link (O.M.G.I.): phone_game_state / phone_game_act / phone_game_query."""

from __future__ import annotations

import socket
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ophelia.android.games import GameStore, game_tool_definitions

omgi_testing = pytest.importorskip("omgi.testing")


ACTIONS = [{
    "name": "play_hand", "description": "Play 1-5 cards",
    "params": {"type": "object", "properties": {"cards": {"type": "array"}}, "required": ["cards"]},
}]
HAND = [{"index": i, "label": lbl} for i, lbl in enumerate(["A♥", "A♠", "7♦"], 1)]


def _store(tmp_path, port: int, omgi: bool = True) -> GameStore:
    yaml = tmp_path / "games.yaml"
    yaml.write_text(textwrap.dedent(f"""
        games:
          - id: balatro
            name: Balatro
            package: com.playstack.balatro.android
            omgi: {str(omgi).lower()}
            omgi_port: {port}
    """))
    return GameStore.load(yaml)


def _registry(store: GameStore):
    from ophelia.tools.registry import ToolRegistry

    reg = ToolRegistry.__new__(ToolRegistry)
    reg.games = store
    reg.android = SimpleNamespace(mode="rish")  # on-phone: no adb forward
    reg.vision = None
    return reg


def _fake_balatro(**kw):
    state = {"phase": "selecting_hand", "stable": True,
             "run": {"ante": 1, "round": 1, "dollars": 4, "hands_left": 4, "discards_left": 3,
                     "deck_remaining": 44, "deck_total": 52, "joker_slots": 5, "consumable_slots": 2,
                     "max_selected": 5},
             "hand": HAND, "jokers": [], "consumables": [],
             "blind": {"name": "Small Blind", "chips_needed": 300}, "score": 0}
    return omgi_testing.FakeGame(game_id="balatro", state=state, actions=ACTIONS, **kw)


def test_profile_reads_omgi_fields(tmp_path):
    p = _store(tmp_path, 47801).get("balatro")
    assert p.omgi is True and p.omgi_port == 47801
    names = {t["function"]["name"] for t in game_tool_definitions()}
    assert {"phone_game_state", "phone_game_act", "phone_game_query"} <= names


async def test_state_and_act_through_the_link(tmp_path):
    def on_act(name, args):
        new = dict(game.state)
        new["score"] = 220
        new["hand"] = HAND[2:]
        return {"ok": True, "played": ["A♥", "A♠"], "hand": "Pair"}, new

    with _fake_balatro(on_act=on_act) as game:
        reg = _registry(_store(tmp_path, game.port))
        text = await reg._phone_game_state("balatro")
        assert "Balatro — selecting hand" in text
        assert "1) A♥" in text and "play_hand(cards)" in text

        out = await reg._phone_game_act("play_hand", '{"cards": [1, 2]}', "balatro")
        assert out.startswith('Did play_hand: {"played": ["A♥", "A♠"], "hand": "Pair"}')
        assert "score 220 / 300" in out
        assert game.calls[-2][0] == "state"
        act = next(params for method, params in game.calls if method == "act")
        assert act == {"action": "play_hand", "args": {"cards": [1, 2]}}

        assert "args must be an object" in await reg._phone_game_act("play_hand", "[1, 2]", "balatro")
        assert "said no (invalid_action)" in await reg._phone_game_act("fly", None, "balatro")


async def test_query(tmp_path):
    with _fake_balatro(on_query=lambda name, args: {"hand": "Pair", "cards": args["cards"]}) as game:
        reg = _registry(_store(tmp_path, game.port))
        out = await reg._phone_game_query("hand_info", {"cards": [1, 2]}, "balatro")
        assert out == '{"hand": "Pair", "cards": [1, 2]}'


async def test_link_down_falls_back_to_sight(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # nothing listens here once closed
    reg = _registry(_store(tmp_path, port))
    text = await reg._phone_game_state("balatro")
    assert "No game link" in text and "phone_game_look" in text


async def test_game_without_link(tmp_path):
    reg = _registry(_store(tmp_path, 47801, omgi=False))
    text = await reg._phone_game_state("balatro")
    assert "has no game link" in text


async def test_adb_body_forwards_the_port(tmp_path, monkeypatch):
    from ophelia.android import adb as adb_util
    from ophelia.android.game_link import GameLink

    calls = []

    async def fake_run(args, timeout=45.0):
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(adb_util, "adb_run", fake_run)
    android = SimpleNamespace(mode="adb", _adb_path="/usr/bin/adb", adb_device="10.0.0.5:5555",
                              ensure_ready=AsyncMock())
    link = GameLink(_store(tmp_path, 47801).get("balatro"), android)
    await link._ensure_forward()
    await link._ensure_forward()
    assert calls == [["/usr/bin/adb", "-s", "10.0.0.5:5555", "forward", "tcp:47801", "tcp:47801"]]
