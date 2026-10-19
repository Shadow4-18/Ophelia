"""Tool tiers: local chat gets core + relevant tool groups, with load_tools
to pull in the rest; cloud chat (auto) and guests are unchanged."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ophelia.channels.session import ChannelSession  # noqa: F401 — import order
from ophelia.tools.tiers import (
    STICKY_TURNS,
    ToolTierState,
    filter_definitions,
    group_of,
    tiers_active,
    triggered_groups,
)


def _def(name):
    return {"type": "function", "function": {"name": name, "parameters": {}}}


ALL = [
    _def(n)
    for n in (
        "send_message",
        "web_search",
        "generate_image",
        "edit_soul",
        "site_upsert_page",
        "phone_tap",
        "sqlite_exec",
    )
]


def test_groups_and_triggers():
    assert group_of("site_deploy") == "site"
    assert group_of("phone_tap") == "phone"
    assert group_of("generate_image") == "media"
    assert group_of("some_mcp_tool") == "core"
    assert triggered_groups("draw me a picture of a fox") == {"media"}
    assert triggered_groups("hey how was work") == set()


def test_filter_keeps_core_plus_groups():
    names = {t["function"]["name"] for t in filter_definitions(ALL, {"media"})}
    assert names == {"send_message", "web_search", "generate_image"}


def test_sticky_groups_decay():
    st = ToolTierState()
    assert st.begin_turn("c", "make an image") == {"media"}
    for _ in range(STICKY_TURNS - 1):
        assert "media" in st.begin_turn("c", "ok")
    assert st.begin_turn("c", "ok") == set()
    assert st.load("c", ["site", "bogus", "core"]) == ["site"]
    assert st.active("c") == {"site"}


def test_tiers_active_modes():
    s = SimpleNamespace(tool_tiers="auto")
    assert tiers_active(s, "ollama") is True
    assert tiers_active(s, "xai-oauth") is False  # cloud chat: unchanged
    assert tiers_active(SimpleNamespace(tool_tiers="off"), "ollama") is False
    assert tiers_active(SimpleNamespace(tool_tiers="on"), "deepseek") is True


def _resp(content=None, tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _tc(name, arguments="{}", call_id="c1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def _agent(provider="ollama"):
    from ophelia.core.agent_loop import AgentLoop
    from ophelia.tools.registry import ToolRegistry

    agent = AgentLoop.__new__(AgentLoop)
    agent.settings = SimpleNamespace(
        max_tool_rounds=6, tool_loop_resume=False, tool_tiers="auto"
    )
    agent.use_tools = True
    tools = ToolRegistry.__new__(ToolRegistry)
    tools.tool_definitions = AsyncMock(return_value=list(ALL))
    tools.dispatch = AsyncMock(return_value="Image saved to /tmp/x.png")
    tools.proactive_sender = None
    agent.tools = tools
    agent.stack = MagicMock()
    agent.stack.name.return_value = provider
    agent._pending_resume = {}
    agent._continuation_count = {}
    agent.honcho = None
    agent._client = AsyncMock(return_value=MagicMock())
    agent._model = MagicMock(return_value="m")
    agent._store = AsyncMock()
    return agent


def _sent_tool_names(call):
    return {t["function"]["name"] for t in call.kwargs["tools"]}


@pytest.mark.asyncio
async def test_local_chat_sends_core_and_load_tools_then_expands():
    agent = _agent("ollama")
    agent._call_with_fallback = AsyncMock(
        side_effect=[
            _resp(tool_calls=[_tc("load_tools", '{"groups": ["site"]}')]),
            _resp(tool_calls=[_tc("site_upsert_page", '{"slug": "x"}', "c2")]),
            _resp(content="page updated"),
        ]
    )
    out = await agent._complete(
        [{"role": "user", "content": "hello there, quick favour?"}],
        store_channel="telegram:1",
    )
    assert out == "page updated"
    calls = agent._call_with_fallback.await_args_list
    first = _sent_tool_names(calls[0])
    assert first == {"send_message", "web_search", "load_tools"}
    assert "site_upsert_page" in _sent_tool_names(calls[1])
    # load_tools is handled in the loop; only the real tool hit dispatch.
    agent.tools.dispatch.assert_awaited_once()
    assert agent.tools.dispatch.await_args.args[0] == "site_upsert_page"
    # …and the site group stays loaded for her next turns in this channel.
    assert "site" in agent.tools.tiers.active("telegram:1")


@pytest.mark.asyncio
async def test_keywords_preload_group():
    agent = _agent("ollama")
    agent._call_with_fallback = AsyncMock(side_effect=[_resp(content="sure")])
    await agent._complete(
        [{"role": "user", "content": "can you draw me a picture?"}],
        store_channel="telegram:1",
    )
    assert "generate_image" in _sent_tool_names(agent._call_with_fallback.await_args)


@pytest.mark.asyncio
async def test_cloud_chat_gets_every_tool():
    agent = _agent("xai-oauth")
    agent._call_with_fallback = AsyncMock(side_effect=[_resp(content="hi")])
    await agent._complete(
        [{"role": "user", "content": "hello"}], store_channel="telegram:1"
    )
    assert _sent_tool_names(agent._call_with_fallback.await_args) == {
        t["function"]["name"] for t in ALL
    }
