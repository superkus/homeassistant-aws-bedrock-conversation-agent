"""Convert Home Assistant chat log content and tools to the Bedrock Converse API.

Pure functions, so they can be tested without Home Assistant running.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any

from homeassistant.components import conversation
from homeassistant.helpers import llm
from homeassistant.helpers.json import json_dumps
import probatio

from .const import BEDROCK_TOOL_NAME_MAX_LEN

_CACHE_POINT = {"cachePoint": {"type": "default"}}
_INVALID_TOOL_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
# Converse rejects these JSON-schema keys at the top level of a tool schema.
_UNSUPPORTED_TOP_LEVEL_KEYS = {"oneOf", "anyOf", "allOf", "$schema", "title"}
# Converse requires the first message to come from the user. A conversation
# opened by an announcement (assist_satellite.start_conversation) starts with
# the assistant, so this neutral user turn is put in front of it.
CONVERSATION_STARTED_TEXT = "(Home Assistant started this conversation.)"
_HASH_SUFFIX_LEN = 8
_MISSING_RESULT = {"error": "No result was recorded for this tool call."}


@dataclass(slots=True)
class ToolNames:
    """Bijective map between HA tool names and valid Bedrock tool names."""

    to_bedrock: dict[str, str] = field(default_factory=dict)
    to_ha: dict[str, str] = field(default_factory=dict)

    def add(self, ha_name: str) -> str:
        """Register a tool and return its unique Bedrock name."""
        if ha_name in self.to_bedrock:
            return self.to_bedrock[ha_name]
        name = _INVALID_TOOL_NAME_CHARS.sub("_", ha_name)
        if len(name) > BEDROCK_TOOL_NAME_MAX_LEN or name in self.to_ha:
            digest = hashlib.sha1(ha_name.encode()).hexdigest()[:_HASH_SUFFIX_LEN]
            name = f"{name[: BEDROCK_TOOL_NAME_MAX_LEN - _HASH_SUFFIX_LEN - 1]}_{digest}"
        self.to_bedrock[ha_name] = name
        self.to_ha[name] = ha_name
        return name


def bedrock_tool_name(name: str) -> str:
    """Return ``name`` made valid for Bedrock (charset and length)."""
    return ToolNames().add(name)


def format_tool(
    tool: llm.Tool, custom_serializer: Callable[[Any], Any] | None, name: str | None = None
) -> dict[str, Any]:
    """Format a Home Assistant LLM tool as a Converse ``toolSpec``."""
    schema = probatio.to_openapi(
        tool.parameters, custom_serializer=custom_serializer, openapi_version="3.1.0"
    )
    schema = {k: v for k, v in schema.items() if k not in _UNSUPPORTED_TOP_LEVEL_KEYS}
    schema["type"] = "object"
    schema.setdefault("properties", {})
    return {
        "toolSpec": {
            "name": name or bedrock_tool_name(tool.name),
            # Converse requires a non-empty description.
            "description": tool.description or tool.name,
            "inputSchema": {"json": schema},
        }
    }


def build_tool_config(
    llm_api: llm.APIInstance | None, *, cache: bool
) -> tuple[dict[str, Any] | None, ToolNames]:
    """Return the Converse ``toolConfig`` and the tool name mapping."""
    names = ToolNames()
    if llm_api is None or not llm_api.tools:
        return None, names
    tools: list[dict[str, Any]] = [
        format_tool(tool, llm_api.custom_serializer, names.add(tool.name)) for tool in llm_api.tools
    ]
    if cache:
        tools.append(_CACHE_POINT)
    return {"tools": tools}, names


def build_system(system: conversation.SystemContent, *, cache: bool) -> list[dict]:
    """Return the Converse ``system`` blocks, with a cache point if wanted."""
    blocks: list[dict[str, Any]] = [{"text": system.content or " "}]
    if cache:
        blocks.append(_CACHE_POINT)
    return blocks


def _json_safe(data: Any) -> dict[str, Any]:
    """Round-trip through HA's encoder so datetimes and sets become JSON."""
    value = json.loads(json_dumps(data))
    return value if isinstance(value, dict) else {"result": value}


def _text(value: str | None) -> list[dict[str, Any]]:
    # Bedrock rejects text blocks that are empty or only whitespace.
    return [{"text": value}] if value and value.strip() else []


@dataclass(slots=True)
class _ContentConversionState:
    """Track message ordering and tool results during content conversion."""

    names: ToolNames
    messages: list[dict[str, Any]] = field(default_factory=list)
    open_calls: dict[str, None] = field(default_factory=dict)
    text_calls: dict[str, str] = field(default_factory=dict)

    def add(self, content: conversation.Content) -> None:
        """Convert one chat log item."""
        if isinstance(content, conversation.UserContent):
            self._close_open_calls()
            self._append("user", _text(content.content))
        elif isinstance(content, conversation.ToolResultContent):
            self._add_tool_result(content)
        elif isinstance(content, conversation.AssistantContent):
            self._add_assistant(content)

    def finish(self) -> list[dict[str, Any]]:
        """Close unanswered calls and ensure the conversation starts with a user."""
        self._close_open_calls()
        if self.messages and self.messages[0]["role"] != "user":
            self.messages.insert(
                0, {"role": "user", "content": [{"text": CONVERSATION_STARTED_TEXT}]}
            )
        return self.messages

    def _append(self, role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if self.messages and self.messages[-1]["role"] == role:
            self.messages[-1]["content"].extend(blocks)
        else:
            self.messages.append({"role": role, "content": blocks})

    def _close_open_calls(self) -> None:
        """Give every unanswered tool use an error result."""
        self._append(
            "user",
            [
                {
                    "toolResult": {
                        "toolUseId": tool_use_id,
                        "content": [{"json": _MISSING_RESULT}],
                        "status": "error",
                    }
                }
                for tool_use_id in self.open_calls
            ],
        )
        self.open_calls.clear()

    def _add_tool_result(self, content: conversation.ToolResultContent) -> None:
        data = _json_safe(content.result.data)
        if content.tool_call_id in self.open_calls:
            del self.open_calls[content.tool_call_id]
            block: dict[str, Any] = {
                "toolUseId": content.tool_call_id,
                "content": [{"json": data}],
            }
            if content.result.error:
                block["status"] = "error"
            self._append("user", [{"toolResult": block}])
        elif content.tool_call_id in self.text_calls:
            self._append("user", _text(f"[Result of {content.tool_name}: {json.dumps(data)}]"))

    def _add_assistant(self, content: conversation.AssistantContent) -> None:
        self._close_open_calls()
        blocks = _text(content.content)
        for call in content.tool_calls or []:
            if call.tool_name in self.names.to_bedrock:
                blocks.append(
                    {
                        "toolUse": {
                            "toolUseId": call.id,
                            "name": self.names.to_bedrock[call.tool_name],
                            "input": _json_safe(call.tool_args),
                        }
                    }
                )
                self.open_calls[call.id] = None
            else:
                self.text_calls[call.id] = call.tool_name
                blocks.append(
                    {"text": f"[Called {call.tool_name}: {json.dumps(_json_safe(call.tool_args))}]"}
                )
        self._append("assistant", blocks)


def convert_content(
    chat_content: Iterable[conversation.Content], names: ToolNames
) -> list[dict[str, Any]]:
    """Convert chat log content (without the system prompt) to Converse messages.

    - Roles strictly alternate, starting with ``user``; consecutive messages
      of one role are merged (tool results are user content in Converse).
    - Tool calls to tools not offered now (made by another agent, such as
      HA's local intent handling, or by an earlier configuration) are sent as
      text, because Converse rejects tool use for tools it was not given.
    - Every tool use gets exactly one result in the next user message; a
      missing one is filled in as an error and stray results are dropped.
    """
    state = _ContentConversionState(names)
    for content in chat_content:
        state.add(content)
    return state.finish()
