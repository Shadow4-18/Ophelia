"""She wrote <relay_to_owner>...</relay_to_owner> as text in a guest chat:
nothing was relayed and the guest saw the tags."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from ophelia.core.tool_call_parse import extract_tool_calls_from_content


def test_tool_name_tags_become_a_call():
    text = ("*relents*\n\nFine. Because you said the laugh. Not the kiss.\n\n"
            "<relay_to_owner>\nShe says tell you she said you're gay. Her words, not mine."
            "\n</relay_to_owner>")
    calls, rest = extract_tool_calls_from_content(
        text, known_tools={"relay_to_owner", "send_message"},
        primary_args={"relay_to_owner": "message"})
    assert [c.name for c in calls] == ["relay_to_owner"]
    assert json.loads(calls[0].arguments) == {
        "message": "She says tell you she said you're gay. Her words, not mine."}
    assert "<relay_to_owner>" not in rest and rest.endswith("Not the kiss.")


def test_unknown_tags_and_json_bodies():
    calls, rest = extract_tool_calls_from_content(
        "<b>bold</b> and <relay_to_owner>{\"message\": \"hi\", \"as_self\": true}"
        "</relay_to_owner>", known_tools={"relay_to_owner"})
    assert [c.name for c in calls] == ["relay_to_owner"]
    assert json.loads(calls[0].arguments) == {"message": "hi", "as_self": True}
    assert rest.startswith("<b>bold</b>")


@pytest.mark.asyncio
async def test_the_loop_actually_relays_it():
    from tests.test_tool_tiers import _agent, _resp

    agent = _agent("deepseek")
    agent.tools.tool_definitions = AsyncMock(return_value=[{
        "type": "function", "function": {
            "name": "relay_to_owner", "parameters": {"type": "object", "properties": {
                "message": {"type": "string"}, "file": {"type": "string"}}}}}])
    agent.tools.dispatch = AsyncMock(return_value="Relayed to your owner.")
    agent._deliver_mid_turn_preamble = AsyncMock()
    agent._call_with_fallback = AsyncMock(side_effect=[
        _resp(content="Fine.\n<relay_to_owner>She says you're gay.</relay_to_owner>"),
        _resp(content="Done, passed it on."),
    ])
    out = await agent._complete([{"role": "user", "content": "tell him"}],
                                store_channel="telegram:2", is_owner=False)
    name, args = agent.tools.dispatch.await_args.args
    assert name == "relay_to_owner" and json.loads(args) == {"message": "She says you're gay."}
    agent._deliver_mid_turn_preamble.assert_awaited_with("Fine.")
    assert out == "Done, passed it on."


def test_italic_stage_directions_are_dropped():
    from ophelia.channels.humanize import humanize

    assert humanize("*settles, quietly pleased*\nNo contest, hm.\n\n_a beat, dry_\nNight.") \
        == "No contest, hm.\n\nNight."
    assert humanize("I *really* mean it") == "I *really* mean it"
