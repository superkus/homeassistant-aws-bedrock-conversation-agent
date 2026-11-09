"""Tests for chat log -> Converse conversion and tool schemas."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components import conversation
from homeassistant.helpers import llm
import probatio

from custom_components.bedrock_conversation.llm_api import HassServiceTool
from custom_components.bedrock_conversation.messages import (
    CONVERSATION_STARTED_TEXT,
    ToolNames,
    bedrock_tool_name,
    build_system,
    build_tool_config,
    convert_content,
    format_tool,
)


def _names(*tools: str) -> ToolNames:
    names = ToolNames()
    for tool in tools:
        names.add(tool)
    return names


def _result(call_id: str, data: dict, *, error: bool = False) -> conversation.ToolResultContent:
    return conversation.ToolResultContent(
        agent_id="a",
        tool_call_id=call_id,
        tool_name="T",
        result=llm.ToolResult(data=data, error=error),
    )


def test_convert_alternates_and_pairs_tool_ids() -> None:
    """Tool results become user content and keep the tool call ids."""
    calls = [
        llm.ToolInput(id="A", tool_name="T", tool_args={"x": 1}),
        llm.ToolInput(id="B", tool_name="T", tool_args={"x": 2}),
    ]
    content = [
        conversation.SystemContent("sys"),
        conversation.UserContent("hola"),
        conversation.AssistantContent(agent_id="a", content="voy", tool_calls=calls),
        _result("A", {"ok": 1}),
        _result("B", {"e": 1}, error=True),
        conversation.AssistantContent(agent_id="a", content="hecho"),
        conversation.UserContent("gracias"),
    ]
    messages = convert_content(content, _names("T"))
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant", "user"]
    assert messages[1]["content"] == [
        {"text": "voy"},
        {"toolUse": {"toolUseId": "A", "name": "T", "input": {"x": 1}}},
        {"toolUse": {"toolUseId": "B", "name": "T", "input": {"x": 2}}},
    ]
    assert messages[2]["content"] == [
        {"toolResult": {"toolUseId": "A", "content": [{"json": {"ok": 1}}]}},
        {"toolResult": {"toolUseId": "B", "content": [{"json": {"e": 1}}], "status": "error"}},
    ]


def test_missing_and_stray_results() -> None:
    """A tool use without result gets an error result; stray results go."""
    content = [
        conversation.UserContent("a"),
        conversation.AssistantContent(
            agent_id="a", tool_calls=[llm.ToolInput(id="A", tool_name="T", tool_args={})]
        ),
        _result("ZZZ", {"stray": True}),
        conversation.UserContent("b"),
    ]
    messages = convert_content(content, _names("T"))
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    result, text = messages[2]["content"]
    assert result["toolResult"]["toolUseId"] == "A"
    assert result["toolResult"]["status"] == "error"
    assert text == {"text": "b"}


def test_unknown_tools_become_text() -> None:
    """Calls to tools not offered now (other agents) are sent as text."""
    call = llm.ToolInput(id="X", tool_name="HassTurnOn", tool_args={"name": "luz"}, external=True)
    content = [
        conversation.UserContent("enciende"),
        conversation.AssistantContent(agent_id="other", tool_calls=[call]),
        _result("X", {"ok": True}),
        conversation.AssistantContent(agent_id="other", content="Hecho"),
    ]
    messages = convert_content(content, ToolNames())
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[1]["content"] == [{"text": '[Called HassTurnOn: {"name": "luz"}]'}]
    assert messages[2]["content"] == [{"text": '[Result of T: {"ok": true}]'}]


def test_whitespace_and_leading_assistant() -> None:
    """Whitespace-only text is dropped; an opening announcement is kept."""
    content = [
        conversation.AssistantContent(agent_id="a", content="¿Abro la puerta?"),
        conversation.UserContent("  "),
        conversation.UserContent("no"),
        conversation.AssistantContent(agent_id="a", content="\n"),
        conversation.UserContent("vale"),
        conversation.AssistantContent(
            agent_id="x", tool_calls=[llm.ToolInput(id="1", tool_name="T", tool_args={})]
        ),
        _result("1", {"when": datetime(2026, 1, 1)}),
    ]
    messages = convert_content(content, _names("T"))
    assert messages[0] == {"role": "user", "content": [{"text": CONVERSATION_STARTED_TEXT}]}
    assert messages[1] == {"role": "assistant", "content": [{"text": "¿Abro la puerta?"}]}
    assert messages[2] == {"role": "user", "content": [{"text": "no"}, {"text": "vale"}]}
    assert messages[4]["content"][0]["toolResult"]["content"] == [
        {"json": {"when": "2026-01-01T00:00:00"}}
    ]


def test_tool_schema_from_parameters() -> None:
    """Schemas come from the tool's own parameters (1.x hard-coded one tool)."""

    class Other(llm.Tool):
        name = "intent__HassTurnOn"
        description = ""
        parameters = probatio.Schema({probatio.Required("name"): str})

        async def async_call(self, *args):  # pragma: no cover
            return llm.ToolResult(data={})

    spec = format_tool(Other(), llm.selector_serializer)["toolSpec"]
    assert spec["name"] == "intent__HassTurnOn"
    assert spec["description"] == "intent__HassTurnOn"  # Converse needs one
    schema = spec["inputSchema"]["json"]
    assert schema["type"] == "object"
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["required"] == ["name"]

    service = format_tool(HassServiceTool(), None)["toolSpec"]["inputSchema"]["json"]
    assert service["properties"]["rgb_color"]["type"] == "array"  # 1.x: string
    assert "brightness_pct" in service["properties"]
    assert "light.turn_on" in service["properties"]["service"]["enum"]


def test_tool_names_valid_and_unique() -> None:
    """Bedrock names allow [a-zA-Z0-9_-]{1,64}; collisions get a hash suffix."""
    assert bedrock_tool_name("script.open gate") == "script_open_gate"
    long_a, long_b = "x" * 70 + "a", "x" * 70 + "b"
    names = _names("a.b", "a_b", long_a, long_b)
    bedrock = list(names.to_bedrock.values())
    assert len(set(bedrock)) == 4
    assert all(len(n) <= 64 for n in bedrock)
    assert names.to_bedrock["a.b"] == "a_b"
    assert names.to_bedrock["a_b"] != "a_b"  # collided, suffixed
    assert {names.to_ha[n] for n in bedrock} == {"a.b", "a_b", long_a, long_b}


def test_cache_points() -> None:
    """Cache points follow the system prompt and the tool list."""
    assert build_system(conversation.SystemContent("s"), cache=True)[-1] == {
        "cachePoint": {"type": "default"}
    }
    assert build_system(conversation.SystemContent("s"), cache=False) == [{"text": "s"}]
    config, names = build_tool_config(None, cache=True)
    assert config is None
    assert names.to_bedrock == {}
