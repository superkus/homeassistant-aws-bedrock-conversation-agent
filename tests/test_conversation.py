"""End-to-end tests of the conversation agent with a scripted Bedrock."""

from __future__ import annotations

import asyncio
from collections import Counter
import contextlib
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import chat_session, intent, llm
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_mock_service

from custom_components.bedrock_conversation.client import (
    BedrockAuthError,
    BedrockClient,
    BedrockError,
)
from custom_components.bedrock_conversation.const import (
    CONF_CONFIRM_SENSITIVE_ACTIONS,
    CONF_LLM_HASS_API,
    CONF_MAX_TOOL_CALL_ITERATIONS,
    CONF_MODEL_ID,
    CONF_PROMPT,
    HOME_LLM_API_ID,
    MAX_TOOL_CALLS_PER_RESPONSE,
    MAX_TOOL_OPERATIONS_PER_RESPONSE,
)
from custom_components.bedrock_conversation.conversation import BedrockConversationEntity
from custom_components.bedrock_conversation.llm_api import HassGetStateTool

from .conftest import MODEL, FakeBedrock, text_events, tool_events
from .test_client import TEXT_DELTA, FakeEventStream, mid_stream_errors

SATELLITE = "satellite-1"
# Captured at import, before the bedrock fixture patches it, to run the real
# retry logic against a fake boto3 runtime.
REAL_CONVERSE_STREAM = BedrockClient.async_converse_stream


async def _ask(
    hass: HomeAssistant,
    agent_id: str,
    text: str,
    conversation_id: str | None = None,
    device_id: str | None = SATELLITE,
):
    """Send one turn, by default from a voice satellite (like real voice use)."""
    return await conversation.async_converse(
        hass,
        text,
        conversation_id,
        Context(),
        language="es",
        agent_id=agent_id,
        device_id=device_id,
    )


def _tool_results(call: dict) -> list[dict]:
    return [
        block["toolResult"]
        for message in call["messages"]
        for block in message["content"]
        if "toolResult" in block
    ]


def _tool_uses(call: dict) -> list[dict]:
    return [
        block["toolUse"]
        for message in call["messages"]
        for block in message["content"]
        if "toolUse" in block
    ]


@pytest.fixture
def state_tool_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Record observable state-tool invocations without replacing their behavior."""
    calls: list[dict[str, object]] = []
    original = HassGetStateTool.async_call

    async def tracked_call(self, hass, tool_input, llm_context):
        calls.append(dict(tool_input.tool_args))
        return await original(self, hass, tool_input, llm_context)

    monkeypatch.setattr(HassGetStateTool, "async_call", tracked_call)
    return calls


async def test_answer_and_memory(hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str) -> None:
    """The answer is spoken, the real conversation id is returned and remembered."""
    bedrock.responses = [text_events("Hola, ¿en qué te ayudo?"), text_events("Son las diez.")]

    first = await _ask(hass, agent_id, "Hola")
    assert first.response.speech["plain"]["speech"] == "Hola, ¿en qué te ayudo?"
    assert first.conversation_id  # 1.x returned None here
    assert first.continue_conversation  # ends with "?" so satellites keep listening

    second = await _ask(hass, agent_id, "¿Qué hora es?", first.conversation_id)
    assert second.conversation_id == first.conversation_id
    assert second.response.speech["plain"]["speech"] == "Son las diez."

    # The second request carries the whole first exchange, user text once each.
    messages = bedrock.calls[1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == [{"text": "Hola"}]
    assert messages[1]["content"] == [{"text": "Hola, ¿en qué te ayudo?"}]
    assert messages[2]["content"] == [{"text": "¿Qué hora es?"}]


async def test_usage_is_traced(hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str) -> None:
    """Bedrock token counts are recorded in the standard conversation trace."""
    usage = {
        "inputTokens": 42,
        "outputTokens": 7,
        "totalTokens": 49,
        "cacheReadInputTokens": 11,
    }
    bedrock.responses = [[*text_events("Hola"), {"metadata": {"usage": usage}}]]

    with patch.object(conversation.ChatLog, "async_trace") as async_trace:
        await _ask(hass, agent_id, "Hola")

    async_trace.assert_any_call(
        {
            "stats": {"input_tokens": 42, "output_tokens": 7},
            "usage": usage,
        }
    )


async def test_request_shape(hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str) -> None:
    """System prompt, tools, cache points and inference config are sent."""
    bedrock.responses = [text_events("Vale.")]
    await _ask(hass, agent_id, "Hola")

    call = bedrock.calls[0]
    assert call["modelId"] == MODEL
    assert call["system"][0]["text"].startswith("Eres un asistente.")
    assert "<device_data>" in call["system"][0]["text"]
    assert call["system"][-1] == {"cachePoint": {"type": "default"}}
    tool_names = [t["toolSpec"]["name"] for t in call["toolConfig"]["tools"] if "toolSpec" in t]
    assert "HassCallService" in tool_names
    assert "toolChoice" not in call["toolConfig"]  # 1.x forced tools on Nova
    assert call["inferenceConfig"] == {"maxTokens": 1024, "temperature": 0.5}


async def test_service_tool_calls(hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str) -> None:
    """Two calls to the same tool are paired with their own results."""
    hass.states.async_set("light.lamp_a", "on", {"friendly_name": "Lamp A"})
    hass.states.async_set("light.lamp_b", "on", {"friendly_name": "Lamp B"})
    async_expose_entity(hass, "conversation", "light.lamp_a", True)
    async_expose_entity(hass, "conversation", "light.lamp_b", True)
    calls = async_mock_service(hass, "light", "turn_off")

    bedrock.responses = [
        tool_events(
            (
                "toolu_A",
                "HassCallService",
                json.dumps({"service": "light.turn_off", "target_device": "light.lamp_a"}),
            ),
            (
                "toolu_B",
                "HassCallService",
                json.dumps({"service": "light.turn_off", "target_device": "light.lamp_b"}),
            ),
        ),
        text_events("He apagado las dos lámparas."),
    ]
    result = await _ask(hass, agent_id, "Apaga las dos lámparas")

    assert result.response.speech["plain"]["speech"] == "He apagado las dos lámparas."
    assert len(calls) == 1
    assert calls[0].data["entity_id"] == ["light.lamp_a", "light.lamp_b"]
    second = bedrock.calls[1]
    assert [u["toolUseId"] for u in _tool_uses(second)] == ["toolu_A", "toolu_B"]
    results = _tool_results(second)
    assert [r["toolUseId"] for r in results] == ["toolu_A", "toolu_B"]
    assert all(r["content"][0]["json"]["result"] == "success" for r in results)
    # Bedrock requires alternating roles: user, assistant(toolUse), user(toolResult)
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]


@pytest.mark.parametrize(
    "entry_options",
    [
        {
            CONF_MODEL_ID: MODEL,
            CONF_PROMPT: "Eres un asistente.",
            CONF_LLM_HASS_API: [HOME_LLM_API_ID, llm.LLM_API_ASSIST],
        }
    ],
)
async def test_assist_tool_interleaved_with_grouped_service_calls(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str
) -> None:
    """Assist and grouped service calls each keep one result and streamed text."""
    hass.states.async_set("cover.shade_a", "open", {"device_class": "shade"})
    hass.states.async_set("cover.shade_b", "open", {"device_class": "shade"})
    for entity_id in ("cover.shade_a", "cover.shade_b"):
        async_expose_entity(hass, "conversation", entity_id, True)
    service_calls = async_mock_service(hass, "cover", "close_cover")
    original_ids = ["service-a", "assist-state", "service-b"]
    bedrock.responses = [
        tool_events(
            (
                original_ids[0],
                "HassCallService",
                json.dumps({"service": "cover.close_cover", "target_device": "cover.shade_a"}),
            ),
            (
                original_ids[1],
                "homeassistant__GetLiveContext",
                json.dumps({"domain": "cover"}),
            ),
            (
                original_ids[2],
                "HassCallService",
                json.dumps({"service": "cover.close_cover", "target_device": "cover.shade_b"}),
            ),
            text="Voy a comprobar y cerrar los estores.",
        ),
        text_events("Los dos estores están cerrados."),
    ]

    result = await _ask(hass, agent_id, "Comprueba y cierra los estores")

    assert result.response.speech["plain"]["speech"] == "Los dos estores están cerrados."
    assert len(service_calls) == 1
    assert service_calls[0].data == {"entity_id": ["cover.shade_a", "cover.shade_b"]}

    follow_up = bedrock.calls[1]
    assert [item["toolUseId"] for item in _tool_uses(follow_up)] == original_ids
    tool_results = _tool_results(follow_up)
    assert Counter(item["toolUseId"] for item in tool_results) == Counter(original_ids)
    by_id = {item["toolUseId"]: item for item in tool_results}
    assert by_id["service-a"]["content"] == by_id["service-b"]["content"]
    assert by_id["service-a"]["content"][0]["json"]["result"] == "success"
    assert "Live Context" in by_id["assist-state"]["content"][0]["json"]["result"]
    assert [message["role"] for message in follow_up["messages"]] == [
        "user",
        "assistant",
        "user",
    ]
    assistant_blocks = follow_up["messages"][1]["content"]
    assert assistant_blocks[0] == {"text": "Voy a comprobar y cerrar los estores."}

    with (
        chat_session.async_get_chat_session(
            hass, result.conversation_id, context=Context()
        ) as session,
        conversation.async_get_chat_log(hass, session) as chat_log,
    ):
        streamed = next(
            item
            for item in chat_log.content
            if isinstance(item, conversation.AssistantContent) and item.tool_calls
        )
        recorded_results = [
            item
            for item in chat_log.content
            if isinstance(item, conversation.ToolResultContent)
            and item.tool_call_id in original_ids
        ]
    assert streamed.content == "Voy a comprobar y cerrar los estores."
    assert [call.id for call in streamed.tool_calls or []] == original_ids
    assert Counter(item.tool_call_id for item in recorded_results) == Counter(original_ids)


async def test_grouped_service_failure_reaches_every_original_tool_id(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str
) -> None:
    """One canonical service failure is reported for every grouped tool use."""
    for entity_id in ("light.lamp_a", "light.lamp_b"):
        hass.states.async_set(entity_id, "off")
        async_expose_entity(hass, "conversation", entity_id, True)
    service_calls = async_mock_service(
        hass,
        "light",
        "turn_on",
        raise_exception=HomeAssistantError("service exploded"),
    )
    bedrock.responses = [
        tool_events(
            (
                "failure-a",
                "HassCallService",
                json.dumps({"service": "light.turn_on", "target_device": "light.lamp_a"}),
            ),
            (
                "failure-b",
                "HassCallService",
                json.dumps({"service": "light.turn_on", "target_device": "light.lamp_b"}),
            ),
        ),
        text_events("No pude encender las lámparas."),
    ]

    await _ask(hass, agent_id, "Enciende las lámparas")

    assert len(service_calls) == 1
    tool_results = _tool_results(bedrock.calls[1])
    assert [item["toolUseId"] for item in tool_results] == ["failure-a", "failure-b"]
    assert all(item["status"] == "error" for item in tool_results)
    errors = [item["content"][0]["json"] for item in tool_results]
    assert errors == [
        {"error": "light.turn_on failed: service exploded"},
        {"error": "light.turn_on failed: service exploded"},
    ]


async def test_unexposed_entity_rejected(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str
) -> None:
    """The tool cannot touch entities that are not exposed to Assist."""
    hass.states.async_set("lock.front_door", "locked")
    async_expose_entity(hass, "conversation", "lock.front_door", False)
    calls = async_mock_service(hass, "lock", "lock")

    bedrock.responses = [
        tool_events(
            (
                "t1",
                "HassCallService",
                json.dumps({"service": "lock.lock", "target_device": "lock.front_door"}),
            )
        ),
        text_events("No puedo."),
    ]
    await _ask(hass, agent_id, "Cierra la puerta")
    assert not calls
    result = _tool_results(bedrock.calls[1])[0]
    assert result["status"] == "error"
    assert "Unknown device" in result["content"][0]["json"]["error"]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            BedrockError("ValidationException", "bad model for account 123456789012"),
            "ValidationException",
        ),
        (BedrockError("ThrottlingException", "slow down"), "ThrottlingException"),
    ],
)
async def test_errors_are_spoken(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str, error: Exception, code: str
) -> None:
    """Errors give a short spoken message without AWS account details."""
    bedrock.responses = [error]
    result = await _ask(hass, agent_id, "Hola")
    assert result.response.response_type == intent.IntentResponseType.ERROR
    speech = result.response.speech["plain"]["speech"]
    assert speech.startswith("AWS Bedrock returned an error")
    assert f"({code})" in speech
    assert "123456789012" not in speech


async def test_auth_error_starts_reauth(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str, config_entry: MockConfigEntry
) -> None:
    """Rejected credentials start the reauth flow."""
    bedrock.responses = [BedrockAuthError("ExpiredTokenException", "expired")]
    result = await _ask(hass, agent_id, "Hola")
    assert result.response.response_type == intent.IntentResponseType.ERROR
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(config_entry.domain)
    assert [f["context"]["source"] for f in flows] == ["reauth"]


@pytest.mark.parametrize("entry_options", [{CONF_MODEL_ID: MODEL, CONF_PROMPT: "x"}])
async def test_no_llm_api_chat_only(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str
) -> None:
    """Without an API there are no tools (chat only)."""
    bedrock.responses = [text_events("Hola")]
    await _ask(hass, agent_id, "Hola")
    assert "toolConfig" not in bedrock.calls[0]


@pytest.mark.parametrize(
    "entry_options",
    [
        {
            CONF_MODEL_ID: MODEL,
            CONF_PROMPT: "x",
            CONF_LLM_HASS_API: [
                llm.LLM_API_HOME_ASSISTANT,
                "third_party_unrestricted",
                llm.LLM_API_ASSIST,
                HOME_LLM_API_ID,
            ],
        }
    ],
)
async def test_runtime_filters_unrestricted_apis_before_llm_data(
    config_entry: MockConfigEntry,
) -> None:
    """Persisted unsafe API IDs never reach HA's LLM data provider."""
    agent = next(iter(config_entry.subentries.values()))
    entity = BedrockConversationEntity(config_entry, agent)
    user_input = MagicMock(language="es", extra_system_prompt=None)
    user_input.as_llm_context.return_value = MagicMock()
    chat_log = MagicMock()
    chat_log.async_provide_llm_data = AsyncMock()

    with (
        patch.object(entity, "_async_resolve_pending", new=AsyncMock()),
        patch.object(entity, "_async_handle_chat_log", new=AsyncMock()),
        patch.object(conversation, "async_get_result_from_chat_log", return_value=MagicMock()),
    ):
        await entity._async_handle_message(user_input, chat_log)

    assert chat_log.async_provide_llm_data.await_args.args[1] == [
        llm.LLM_API_ASSIST,
        HOME_LLM_API_ID,
    ]


async def test_agent_change_reloads(
    hass: HomeAssistant, bedrock: FakeBedrock, config_entry, agent_id: str
) -> None:
    """Changing an agent's sub-entry applies without a restart."""
    agent = next(iter(config_entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        config_entry, agent, data={**agent.data, CONF_MODEL_ID: "amazon.nova-lite-v1:0"}
    )
    await hass.async_block_till_done()
    bedrock.responses = [text_events("Hola")]
    await _ask(hass, agent_id, "Hola")
    assert bedrock.calls[0]["modelId"] == "amazon.nova-lite-v1:0"


# --- Spoken confirmation ------------------------------------------------------


@pytest.fixture
def garage(hass: HomeAssistant) -> list:
    """An exposed garage door and a recorder for open_cover calls."""
    hass.states.async_set(
        "cover.garage", "closed", {"friendly_name": "Garaje", "device_class": "garage"}
    )
    async_expose_entity(hass, "conversation", "cover.garage", True)
    return async_mock_service(hass, "cover", "open_cover")


OPEN_GARAGE = json.dumps({"service": "cover.open_cover", "target_device": "cover.garage"})
QUESTION = "Necesito tu confirmación para abrir Garaje. ¿Lo hago?"


async def test_sensitive_action_asks_then_runs_on_yes(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str, garage: list
) -> None:
    """The agent itself asks a fixed question; only a yes runs the action."""
    bedrock.responses = [tool_events(("t1", "HassCallService", OPEN_GARAGE))]
    first = await _ask(hass, agent_id, "Abre el garaje")
    assert not garage  # not opened yet
    assert first.response.speech["plain"]["speech"] == QUESTION
    assert first.continue_conversation  # satellites keep listening
    assert len(bedrock.calls) == 1  # the model is not asked to phrase it

    bedrock.responses = [text_events("Abriendo el garaje.")]
    second = await _ask(hass, agent_id, "Sí", first.conversation_id)
    assert [c.data["entity_id"] for c in garage] == ["cover.garage"]
    assert second.response.speech["plain"]["speech"] == "Abriendo el garaje."
    results = _tool_results(bedrock.calls[1])
    assert results[0]["content"][0]["json"]["status"] == "confirmation_required"
    assert results[-1]["content"][0]["json"]["result"] == "success"
    # Bedrock sees a valid sequence: ... assistant(question), user(yes),
    # assistant(toolUse), user(toolResult)
    assert [m["role"] for m in bedrock.calls[1]["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]


@pytest.mark.parametrize("answer", ["No, déjalo", "¿Sí?", "ok"])
async def test_sensitive_action_cancelled(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str, garage: list, answer: str
) -> None:
    """Anything but a clear yes cancels, and the model is told so."""
    bedrock.responses = [tool_events(("t1", "HassCallService", OPEN_GARAGE))]
    first = await _ask(hass, agent_id, "Abre el garaje")
    bedrock.responses = [text_events("De acuerdo, no lo abro.")]
    await _ask(hass, agent_id, answer, first.conversation_id)
    cancelled = _tool_results(bedrock.calls[1])[-1]
    assert cancelled["status"] == "error"
    assert cancelled["content"][0]["json"]["performed"] is False
    bedrock.responses = [text_events("Hola")]
    await _ask(hass, agent_id, "Sí", first.conversation_id)  # a late yes does nothing
    assert not garage


async def test_confirmation_bound_to_device(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str, garage: list
) -> None:
    """A yes from another satellite in the same conversation does nothing."""
    bedrock.responses = [tool_events(("t1", "HassCallService", OPEN_GARAGE))]
    first = await conversation.async_converse(
        hass, "Abre el garaje", None, Context(), language="es", agent_id=agent_id, device_id="sat-1"
    )
    bedrock.responses = [text_events("No.")]
    await conversation.async_converse(
        hass,
        "Sí",
        first.conversation_id,
        Context(),
        language="es",
        agent_id=agent_id,
        device_id="sat-2",
    )
    assert not garage


async def test_confirmation_expires(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str, garage: list
) -> None:
    """A yes after the confirmation window does nothing."""
    bedrock.responses = [tool_events(("t1", "HassCallService", OPEN_GARAGE))]
    first = await _ask(hass, agent_id, "Abre el garaje")
    with patch(
        "custom_components.bedrock_conversation.confirmation.time.monotonic",
        return_value=time.monotonic() + 3600,
    ):
        bedrock.responses = [text_events("Ha caducado.")]
        await _ask(hass, agent_id, "Sí", first.conversation_id)
    assert not garage
    assert "expired" in _tool_results(bedrock.calls[1])[-1]["content"][0]["json"]["reason"]


async def test_no_identity_is_refused(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str, garage: list
) -> None:
    """With neither user nor device (automations), nobody could confirm: refuse."""
    bedrock.responses = [
        tool_events(("t1", "HassCallService", OPEN_GARAGE)),
        text_events("No puedo abrirlo sin confirmación."),
    ]
    result = await _ask(hass, agent_id, "Abre el garaje", device_id=None)
    assert result.response.speech["plain"]["speech"] == "No puedo abrirlo sin confirmación."
    refused = _tool_results(bedrock.calls[1])[0]
    assert refused["status"] == "error"
    assert refused["content"][0]["json"]["status"] == "refused"
    bedrock.responses = [text_events("Nada.")]
    await _ask(hass, agent_id, "Sí", result.conversation_id, device_id=None)
    assert not garage


async def test_confirmed_operations_start_concurrently_and_keep_result_order(
    config_entry,
) -> None:
    """Confirmed canonical operations start together and retain pending order."""
    agent = next(iter(config_entry.subentries.values()))
    entity = BedrockConversationEntity(config_entry, agent)
    entity.entity_id = "conversation.test"
    values = [30, 10, 20]
    pending = MagicMock(
        calls=[
            llm.ToolInput(tool_name=f"tool_{value}", tool_args={"value": value}) for value in values
        ],
        expired=False,
    )
    pending.same_requester.return_value = True
    entity._pending["conversation"] = pending
    chat_log = MagicMock(conversation_id="conversation", llm_api=MagicMock())
    user_input = MagicMock(text="yes")
    started = {value: asyncio.Event() for value in values}
    release = {value: asyncio.Event() for value in values}
    active: set[int] = set()

    async def delayed_call(_llm_api, call) -> llm.ToolResult:
        value = call.tool_args["value"]
        active.add(value)
        started[value].set()
        try:
            await release[value].wait()
        finally:
            active.remove(value)
        return llm.ToolResult(data={"value": value})

    resolve_task = asyncio.create_task(entity._async_resolve_pending(user_input, chat_log))
    start_error: str | None = None
    try:
        with patch.object(entity, "_async_call_tool", side_effect=delayed_call):
            try:
                async with asyncio.timeout(0.2):
                    await asyncio.gather(*(event.wait() for event in started.values()))
            except TimeoutError:
                missing = [value for value, event in started.items() if not event.is_set()]
                start_error = f"confirmed operations did not start concurrently; missing {missing}"

            if start_error is None:
                for value in reversed(values):
                    release[value].set()
                await asyncio.wait_for(resolve_task, timeout=1)
                tool_results = [
                    call.args[0]
                    for call in chat_log.async_add_assistant_content_without_tools.call_args_list
                    if isinstance(call.args[0], conversation.ToolResultContent)
                ]
                assert [result.result.data["value"] for result in tool_results] == values
    finally:
        if not resolve_task.done():
            resolve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await resolve_task
        assert not active

    if start_error is not None:
        pytest.fail(start_error)


async def test_duplicate_held_calls_run_once(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str, garage: list
) -> None:
    """The same sensitive call requested twice runs once after one yes."""
    bedrock.responses = [
        tool_events(("t1", "HassCallService", OPEN_GARAGE), ("t2", "HassCallService", OPEN_GARAGE))
    ]
    first = await _ask(hass, agent_id, "Abre el garaje")
    bedrock.responses = [text_events("Abierto.")]
    await _ask(hass, agent_id, "Sí, hazlo", first.conversation_id)
    assert len(garage) == 1


async def test_non_sensitive_calls_in_same_message_run(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str, garage: list
) -> None:
    """Only the sensitive call is held; the rest runs immediately."""
    hass.states.async_set("light.lamp", "off")
    async_expose_entity(hass, "conversation", "light.lamp", True)
    lights = async_mock_service(hass, "light", "turn_on")
    bedrock.responses = [
        tool_events(
            (
                "t1",
                "HassCallService",
                json.dumps({"service": "light.turn_on", "target_device": "light.lamp"}),
            ),
            ("t2", "HassCallService", OPEN_GARAGE),
        )
    ]
    result = await _ask(hass, agent_id, "Enciende la luz y abre el garaje")
    assert len(lights) == 1
    assert not garage
    assert result.response.speech["plain"]["speech"] == QUESTION


@pytest.mark.parametrize(
    "entry_options",
    [
        {
            CONF_MODEL_ID: MODEL,
            CONF_LLM_HASS_API: [HOME_LLM_API_ID],
            CONF_CONFIRM_SENSITIVE_ACTIONS: False,
        }
    ],
)
async def test_confirmation_can_be_disabled(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str, garage: list
) -> None:
    """With the option off, sensitive actions run directly."""
    bedrock.responses = [
        tool_events(("t1", "HassCallService", OPEN_GARAGE)),
        text_events("Abierto."),
    ]
    await _ask(hass, agent_id, "Abre el garaje")
    assert len(garage) == 1


@pytest.mark.parametrize(
    "entry_options", [{CONF_MODEL_ID: MODEL, CONF_LLM_HASS_API: [llm.LLM_API_ASSIST]}]
)
async def test_assist_api_unlock_all_needs_confirmation(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str
) -> None:
    """HA's Device control API: HassTurnOff name=all on locks is held."""
    hass.states.async_set("lock.front_door", "locked", {"friendly_name": "Puerta principal"})
    async_expose_entity(hass, "conversation", "lock.front_door", True)
    unlocks = async_mock_service(hass, "lock", "unlock")

    bedrock.responses = [text_events("x")]
    await _ask(hass, agent_id, "Hola")
    names = [
        t["toolSpec"]["name"] for t in bedrock.calls[0]["toolConfig"]["tools"] if "toolSpec" in t
    ]
    turn_off = next(n for n in names if n.endswith("HassTurnOff"))

    bedrock.responses = [
        tool_events(("t1", turn_off, json.dumps({"name": "all", "domain": ["lock"]}))),
    ]
    first = await _ask(hass, agent_id, "Abre todas las cerraduras")
    assert not unlocks
    assert first.response.speech["plain"]["speech"] == (
        "Necesito tu confirmación para abrir la cerradura Puerta principal. ¿Lo hago?"
    )

    bedrock.responses = [text_events("Hecho.")]
    await _ask(hass, agent_id, "sí, por favor", first.conversation_id)
    assert [c.data["entity_id"] for c in unlocks] == ["lock.front_door"]


async def test_other_agents_cannot_unlock_with_our_api(hass: HomeAssistant, config_entry) -> None:
    """Agents that cannot ask for confirmation get an error for sensitive calls."""
    hass.states.async_set("lock.front_door", "locked")
    async_expose_entity(hass, "conversation", "lock.front_door", True)
    unlocks = async_mock_service(hass, "lock", "unlock")
    context = llm.LLMContext(
        platform="openai_conversation",
        context=Context(),
        language="es",
        assistant="conversation",
        device_id=None,
    )
    api = await llm.async_get_api(hass, HOME_LLM_API_ID, context)
    result = await api.async_call_tool(
        llm.ToolInput(
            tool_name="HassCallService",
            tool_args={"service": "lock.unlock", "target_device": "lock.front_door"},
        )
    )
    assert result.error
    assert not unlocks


async def test_other_agents_cannot_unlock_bulk_targets_with_our_api(
    hass: HomeAssistant, config_entry
) -> None:
    """Bulk sensitive targets cannot bypass another agent's confirmation refusal."""
    targets = ["lock.front_door", "lock.back_door"]
    for entity_id in targets:
        hass.states.async_set(entity_id, "locked")
        async_expose_entity(hass, "conversation", entity_id, True)
    unlocks = async_mock_service(hass, "lock", "unlock")
    context = llm.LLMContext(
        platform="openai_conversation",
        context=Context(),
        language="es",
        assistant="conversation",
        device_id=None,
    )
    api = await llm.async_get_api(hass, HOME_LLM_API_ID, context)

    result = await api.async_call_tool(
        llm.ToolInput(
            tool_name="HassCallService",
            tool_args={"service": "lock.unlock", "target_devices": targets},
        )
    )

    assert result.error
    assert result.data == {
        "error": "lock.unlock needs confirmation, which this assistant cannot ask for"
    }
    assert not unlocks


# --- Robustness ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("stop_reason", "message"),
    [
        ("guardrail_intervened", "The answer was blocked by a content filter"),
        ("content_filtered", "The answer was blocked by a content filter"),
        (
            "model_context_window_exceeded",
            "The conversation is too long for this model. Start a new conversation",
        ),
    ],
)
async def test_unusable_stop_reason_is_spoken(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str, stop_reason: str, message: str
) -> None:
    """A blocked or overflowing answer gives an error, not the partial text.

    HA drops the final period of spoken error messages.
    """
    bedrock.responses = [text_events("Texto a medias", stop=stop_reason)]
    result = await _ask(hass, agent_id, "Hola")
    assert result.response.response_type == intent.IntentResponseType.ERROR
    assert result.response.speech["plain"]["speech"] == message
    assert len(bedrock.calls) == 1


@pytest.mark.parametrize(
    "entry_options",
    [
        {
            CONF_MODEL_ID: MODEL,
            CONF_LLM_HASS_API: [HOME_LLM_API_ID],
            CONF_MAX_TOOL_CALL_ITERATIONS: 2,
        }
    ],
)
async def test_tool_loop_stops_at_max_iterations(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str
) -> None:
    """A model that keeps calling tools is stopped after the configured rounds."""
    hass.states.async_set("light.lamp", "off")
    async_expose_entity(hass, "conversation", "light.lamp", True)
    lights = async_mock_service(hass, "light", "turn_on")
    turn_on = json.dumps({"service": "light.turn_on", "target_device": "light.lamp"})
    bedrock.responses = [
        tool_events(("t1", "HassCallService", turn_on)),
        tool_events(("t2", "HassCallService", turn_on)),
        tool_events(("t3", "HassCallService", turn_on)),  # must never be requested
    ]
    result = await _ask(hass, agent_id, "Enciende la luz")
    assert result.response.response_type == intent.IntentResponseType.ERROR
    assert result.response.speech["plain"]["speech"] == "I could not complete that request"
    assert len(bedrock.calls) == 2
    assert len(lights) == 2


@pytest.mark.parametrize("error", mid_stream_errors())
async def test_error_after_text_is_spoken_without_retry(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str, error: Exception
) -> None:
    """An error after text was streamed is not retried and gives the api_error message."""
    entry = hass.config_entries.async_entries("bedrock_conversation")[0]
    runtime = MagicMock()
    runtime.converse_stream.return_value = {
        "stream": FakeEventStream([{"messageStart": {"role": "assistant"}}, TEXT_DELTA], error)
    }
    entry.runtime_data.client._runtime = runtime
    with patch.object(BedrockClient, "async_converse_stream", REAL_CONVERSE_STREAM):
        result = await _ask(hass, agent_id, "Hola")
    assert runtime.converse_stream.call_count == 1
    assert result.response.response_type == intent.IntentResponseType.ERROR
    assert result.response.speech["plain"]["speech"] == (
        "AWS Bedrock returned an error (ValidationException)"
    )


async def test_truncated_tool_call(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str
) -> None:
    """A tool call cut off by the token limit gives a clear error."""
    events = tool_events(("t1", "HassCallService", '{"service": "light.tu'))
    events[-1] = {"messageStop": {"stopReason": "max_tokens"}}
    bedrock.responses = [events]
    result = await _ask(hass, agent_id, "Enciende la luz")
    assert result.response.response_type == intent.IntentResponseType.ERROR
    assert "token limit" in result.response.speech["plain"]["speech"]


async def test_tool_call_from_another_agent_sent_as_text(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str
) -> None:
    """Tool calls made by HA's local agent in the same chat do not break Converse."""
    bedrock.responses = [text_events("Hola")]
    first = await _ask(hass, agent_id, "Hola")
    with (
        chat_session.async_get_chat_session(
            hass, first.conversation_id, context=Context()
        ) as session,
        conversation.async_get_chat_log(hass, session) as chat_log,
    ):
        chat_log.async_add_user_content(conversation.UserContent("enciende la luz"))
        call = llm.ToolInput(tool_name="HassTurnOn", tool_args={"name": "luz"}, external=True)
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id="conversation.home_assistant", tool_calls=[call])
        )
        chat_log.async_add_assistant_content_without_tools(
            conversation.ToolResultContent(
                agent_id="conversation.home_assistant",
                tool_call_id=call.id,
                tool_name="HassTurnOn",
                result=llm.ToolResult(data={"ok": True}),
            )
        )
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id="conversation.home_assistant", content="Hecho")
        )
    bedrock.responses = [text_events("De nada")]
    await _ask(hass, agent_id, "Gracias", first.conversation_id)
    messages = bedrock.calls[1]["messages"]
    assert not _tool_uses(bedrock.calls[1])
    assert any("[Called HassTurnOn" in b.get("text", "") for m in messages for b in m["content"])


async def test_tool_calls_per_response_are_bounded(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str
) -> None:
    """An oversized model tool batch fails before Home Assistant runs any call."""
    hass.states.async_set("light.lamp", "off")
    async_expose_entity(hass, "conversation", "light.lamp", True)
    lights = async_mock_service(hass, "light", "turn_on")
    turn_on = json.dumps({"service": "light.turn_on", "target_device": "light.lamp"})
    bedrock.responses = [
        tool_events(
            *[
                (f"t{index}", "HassCallService", turn_on)
                for index in range(MAX_TOOL_CALLS_PER_RESPONSE + 1)
            ]
        )
    ]

    result = await _ask(hass, agent_id, "Enciende la luz")

    assert result.response.response_type == intent.IntentResponseType.ERROR
    assert str(MAX_TOOL_CALLS_PER_RESPONSE) in result.response.speech["plain"]["speech"]
    assert "No tools were run" in result.response.speech["plain"]["speech"]
    assert not lights
    assert len(bedrock.calls) == 1


async def test_many_per_device_service_calls_are_collapsed(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str
) -> None:
    """A model's 75 per-cover calls run as one bounded HA service operation."""
    targets = [f"cover.shade_{index}" for index in range(75)]
    for entity_id in targets:
        hass.states.async_set(entity_id, "open", {"device_class": "shade"})
        async_expose_entity(hass, "conversation", entity_id, True)
    calls = async_mock_service(hass, "cover", "close_cover")
    tool_calls = [
        (
            f"tool_{index}",
            "HassCallService",
            json.dumps({"service": "cover.close_cover", "target_device": entity_id}),
        )
        for index, entity_id in enumerate(targets)
    ]
    bedrock.responses = [
        tool_events(*tool_calls),
        text_events("He cerrado los 75 estores."),
    ]

    result = await _ask(hass, agent_id, "Cierra todos los estores")

    assert result.response.speech["plain"]["speech"] == "He cerrado los 75 estores."
    assert len(calls) == 1
    assert calls[0].data == {"entity_id": targets}
    results = _tool_results(bedrock.calls[1])
    assert [item["toolUseId"] for item in results] == [f"tool_{index}" for index in range(75)]
    assert all(item["content"][0]["json"]["result"] == "success" for item in results)
    assert all("targets" not in item["content"][0]["json"] for item in results)


async def test_model_bulk_service_call_keeps_complete_target_list(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str
) -> None:
    """A single model-originated bulk call may return its complete target list."""
    targets = [f"light.lamp_{index}" for index in range(3)]
    for entity_id in targets:
        hass.states.async_set(entity_id, "off")
        async_expose_entity(hass, "conversation", entity_id, True)
    calls = async_mock_service(hass, "light", "turn_on")
    bedrock.responses = [
        tool_events(
            (
                "bulk-call",
                "HassCallService",
                json.dumps({"service": "light.turn_on", "target_devices": targets}),
            )
        ),
        text_events("He encendido las luces."),
    ]

    await _ask(hass, agent_id, "Enciende las luces")

    assert len(calls) == 1
    result = _tool_results(bedrock.calls[1])[0]["content"][0]["json"]
    assert result == {"result": "success", "service": "light.turn_on", "targets": targets}


async def test_distinct_tool_operations_remain_bounded(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str
) -> None:
    """Grouping does not remove the cap on genuinely different operations."""
    hass.states.async_set("light.lamp", "off")
    async_expose_entity(hass, "conversation", "light.lamp", True)
    calls = async_mock_service(hass, "light", "turn_on")
    bedrock.responses = [
        tool_events(
            *[
                (
                    f"tool_{index}",
                    "HassCallService",
                    json.dumps(
                        {
                            "service": "light.turn_on",
                            "target_device": "light.lamp",
                            "brightness_pct": index,
                        }
                    ),
                )
                for index in range(MAX_TOOL_OPERATIONS_PER_RESPONSE + 1)
            ]
        )
    ]

    result = await _ask(hass, agent_id, "Cambia muchas veces la luz")

    assert result.response.response_type == intent.IntentResponseType.ERROR
    speech = result.response.speech["plain"]["speech"]
    assert str(MAX_TOOL_OPERATIONS_PER_RESPONSE) in speech
    assert "No tools were run" in speech
    assert not calls


async def test_many_sensitive_service_calls_collapse_before_confirmation(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str
) -> None:
    """A grouped garage operation asks once and runs one service call after yes."""
    targets = [f"cover.garage_{index}" for index in range(6)]
    for index, entity_id in enumerate(targets):
        hass.states.async_set(
            entity_id,
            "closed",
            {"friendly_name": f"Garaje {index}", "device_class": "garage"},
        )
        async_expose_entity(hass, "conversation", entity_id, True)
    calls = async_mock_service(hass, "cover", "open_cover")
    bedrock.responses = [
        tool_events(
            *[
                (
                    f"tool_{index}",
                    "HassCallService",
                    json.dumps({"service": "cover.open_cover", "target_device": entity_id}),
                )
                for index, entity_id in enumerate(targets)
            ]
        )
    ]

    first = await _ask(hass, agent_id, "Abre todos los garajes")

    assert not calls
    question = first.response.speech["plain"]["speech"]
    assert question.startswith("Necesito tu confirmación para abrir Garaje 0")
    assert "y 1 más" in question
    assert question.endswith("¿Lo hago?")

    bedrock.responses = [text_events("He abierto los garajes.")]
    await _ask(hass, agent_id, "Sí, hazlo", first.conversation_id)

    assert len(calls) == 1
    assert calls[0].data == {"entity_id": targets}


async def test_grouped_service_operations_start_concurrently_and_keep_result_order(
    hass: HomeAssistant, bedrock: FakeBedrock, agent_id: str
) -> None:
    """Delayed canonical operations start together and report in stable group order."""
    targets = [f"light.lamp_{index}" for index in range(6)]
    for entity_id in targets:
        hass.states.async_set(entity_id, "off")
        async_expose_entity(hass, "conversation", entity_id, True)

    operation_values = [30.0, 10.0, 20.0]
    started = {value: asyncio.Event() for value in operation_values}
    release = {value: asyncio.Event() for value in operation_values}
    active: set[float] = set()
    service_order: list[float] = []

    async def delayed_turn_on(call) -> None:
        value = call.data["brightness_pct"]
        service_order.append(value)
        active.add(value)
        started[value].set()
        try:
            await release[value].wait()
        finally:
            active.remove(value)

    hass.services.async_register("light", "turn_on", delayed_turn_on)
    tool_calls = [
        (
            "operation-a-1",
            "HassCallService",
            json.dumps(
                {
                    "service": "light.turn_on",
                    "target_device": targets[0],
                    "brightness_pct": 30,
                }
            ),
        ),
        (
            "operation-b-1",
            "HassCallService",
            json.dumps(
                {
                    "service": "light.turn_on",
                    "target_device": targets[2],
                    "brightness_pct": 10,
                }
            ),
        ),
        (
            "operation-a-2",
            "HassCallService",
            json.dumps(
                {
                    "service": "light.turn_on",
                    "target_device": targets[1],
                    "brightness_pct": 30,
                }
            ),
        ),
        (
            "operation-c-1",
            "HassCallService",
            json.dumps(
                {
                    "service": "light.turn_on",
                    "target_device": targets[4],
                    "brightness_pct": 20,
                }
            ),
        ),
        (
            "operation-b-2",
            "HassCallService",
            json.dumps(
                {
                    "service": "light.turn_on",
                    "target_device": targets[3],
                    "brightness_pct": 10,
                }
            ),
        ),
        (
            "operation-c-2",
            "HassCallService",
            json.dumps(
                {
                    "service": "light.turn_on",
                    "target_device": targets[5],
                    "brightness_pct": 20,
                }
            ),
        ),
    ]
    expected_result_ids = [
        "operation-a-1",
        "operation-a-2",
        "operation-b-1",
        "operation-b-2",
        "operation-c-1",
        "operation-c-2",
    ]
    bedrock.responses = [tool_events(*tool_calls), text_events("Luces ajustadas.")]
    ask_task = asyncio.create_task(_ask(hass, agent_id, "Ajusta las luces"))
    start_error: str | None = None

    try:
        try:
            async with asyncio.timeout(0.2):
                await asyncio.gather(*(event.wait() for event in started.values()))
        except TimeoutError:
            missing = [value for value, event in started.items() if not event.is_set()]
            start_error = f"canonical operations did not start concurrently; missing {missing}"

        if start_error is None:
            # Complete out of order. User-visible tool results must still follow
            # canonical operation order and each group's original ID order.
            for value in reversed(operation_values):
                release[value].set()
            result = await asyncio.wait_for(ask_task, timeout=1)
            assert result.response.speech["plain"]["speech"] == "Luces ajustadas."
            assert Counter(service_order) == Counter(operation_values)
            assert [item["toolUseId"] for item in _tool_results(bedrock.calls[1])] == (
                expected_result_ids
            )
    finally:
        if not ask_task.done():
            ask_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ask_task
        assert not active

    if start_error is not None:
        pytest.fail(start_error)


async def test_many_per_device_state_calls_are_collapsed(
    hass: HomeAssistant,
    bedrock: FakeBedrock,
    agent_id: str,
    state_tool_calls: list[dict[str, object]],
) -> None:
    """A model's 75 state calls become one read with compact per-ID results."""
    targets = [f"cover.shade_{index}" for index in range(75)]
    for index, entity_id in enumerate(targets):
        hass.states.async_set(
            entity_id,
            "open" if index % 2 else "closed",
            {"friendly_name": f"Estor {index}", "device_class": "shade"},
        )
        async_expose_entity(hass, "conversation", entity_id, True)
    bedrock.responses = [
        tool_events(
            *[
                (
                    f"state_{index}",
                    "HassGetState",
                    json.dumps({"entity_id": entity_id}),
                )
                for index, entity_id in enumerate(targets)
            ]
        ),
        text_events("Hay 38 estores cerrados y 37 abiertos."),
    ]

    result = await _ask(hass, agent_id, "Dime el estado de las persianas")

    assert result.response.speech["plain"]["speech"] == ("Hay 38 estores cerrados y 37 abiertos.")
    results = _tool_results(bedrock.calls[1])
    assert [item["toolUseId"] for item in results] == [f"state_{index}" for index in range(75)]
    assert state_tool_calls == [{"entity_ids": targets}]
    assert len(results) == 75
    for index, item in enumerate(results):
        data = item["content"][0]["json"]
        assert data["entity_id"] == targets[index]
        assert data["state"] == ("open" if index % 2 else "closed")
        # Each original ID gets only its own state, not the 75-state bulk payload.
        assert "entities" not in data


async def test_oversized_original_bulk_state_call_is_rejected_without_collapse(
    hass: HomeAssistant,
    bedrock: FakeBedrock,
    agent_id: str,
    state_tool_calls: list[dict[str, object]],
) -> None:
    """An oversized original remains invalid even when all IDs are duplicates."""
    entity_id = "light.lamp"
    hass.states.async_set(entity_id, "on")
    async_expose_entity(hass, "conversation", entity_id, True)
    requested = [entity_id] * 101
    bedrock.responses = [
        tool_events(
            (
                "oversized-state",
                "HassGetState",
                json.dumps({"entity_ids": requested}),
            )
        ),
        text_events("La solicitud era demasiado grande."),
    ]

    await _ask(hass, agent_id, "Consulta demasiados estados")

    tool_result = _tool_results(bedrock.calls[1])[0]
    assert tool_result.get("status") == "error"
    assert "Invalid arguments" in tool_result["content"][0]["json"]["error"]
    assert state_tool_calls == [{"entity_ids": requested}]


async def test_null_singular_with_bulk_state_target_keeps_validation_error(
    hass: HomeAssistant,
    bedrock: FakeBedrock,
    agent_id: str,
    state_tool_calls: list[dict[str, object]],
) -> None:
    """A null singular field cannot be normalized into a valid bulk call."""
    entity_id = "light.lamp"
    hass.states.async_set(entity_id, "on")
    async_expose_entity(hass, "conversation", entity_id, True)
    original_args = {"entity_id": None, "entity_ids": [entity_id]}
    bedrock.responses = [
        tool_events(
            (
                "malformed-state",
                "HassGetState",
                json.dumps(original_args),
            )
        ),
        text_events("La solicitud no era válida."),
    ]

    await _ask(hass, agent_id, "Consulta el estado")

    tool_result = _tool_results(bedrock.calls[1])[0]
    assert tool_result.get("status") == "error"
    assert "Invalid arguments" in tool_result["content"][0]["json"]["error"]
    assert state_tool_calls == [original_args]


async def test_single_bulk_state_result_is_deduplicated_in_first_seen_order(
    hass: HomeAssistant,
    bedrock: FakeBedrock,
    agent_id: str,
    state_tool_calls: list[dict[str, object]],
) -> None:
    """One valid bulk original receives one compact result per unique entity."""
    targets = ["sensor.second", "sensor.first", "sensor.third"]
    for index, entity_id in enumerate(targets):
        hass.states.async_set(entity_id, str(index))
        async_expose_entity(hass, "conversation", entity_id, True)
    requested = [targets[0], targets[1], targets[0], targets[2], targets[1]]
    bedrock.responses = [
        tool_events(
            (
                "bulk-state",
                "HassGetState",
                json.dumps({"entity_ids": requested}),
            )
        ),
        text_events("Estados consultados."),
    ]

    await _ask(hass, agent_id, "Consulta varios estados")

    tool_results = _tool_results(bedrock.calls[1])
    assert [item["toolUseId"] for item in tool_results] == ["bulk-state"]
    data = tool_results[0]["content"][0]["json"]
    assert [item["entity_id"] for item in data["entities"]] == targets
    assert [item["state"] for item in data["entities"]] == ["0", "1", "2"]
    assert len(state_tool_calls) == 1


async def test_mixed_singular_and_bulk_state_results_preserve_original_order(
    hass: HomeAssistant,
    bedrock: FakeBedrock,
    agent_id: str,
    state_tool_calls: list[dict[str, object]],
) -> None:
    """Mixed originals receive compact data in their Bedrock tool-use order."""
    targets = ["cover.alpha", "cover.beta", "cover.gamma", "cover.delta"]
    for index, entity_id in enumerate(targets):
        hass.states.async_set(entity_id, f"state-{index}")
        async_expose_entity(hass, "conversation", entity_id, True)
    original_ids = ["single-alpha", "bulk-beta-gamma", "single-delta"]
    bedrock.responses = [
        tool_events(
            (
                original_ids[0],
                "HassGetState",
                json.dumps({"entity_id": targets[0]}),
            ),
            (
                original_ids[1],
                "HassGetState",
                json.dumps({"entity_ids": targets[1:3]}),
            ),
            (
                original_ids[2],
                "HassGetState",
                json.dumps({"entity_id": targets[3]}),
            ),
        ),
        text_events("Estados consultados."),
    ]

    result = await _ask(hass, agent_id, "Consulta los estores")

    follow_up = bedrock.calls[1]
    assert [message["role"] for message in follow_up["messages"]] == [
        "user",
        "assistant",
        "user",
    ]
    tool_results = _tool_results(follow_up)
    assert [item["toolUseId"] for item in tool_results] == original_ids
    assert tool_results[0]["content"][0]["json"]["entity_id"] == targets[0]
    assert [
        item["entity_id"] for item in tool_results[1]["content"][0]["json"]["entities"]
    ] == targets[1:3]
    assert tool_results[2]["content"][0]["json"]["entity_id"] == targets[3]
    assert state_tool_calls == [{"entity_ids": targets}]

    with (
        chat_session.async_get_chat_session(
            hass, result.conversation_id, context=Context()
        ) as session,
        conversation.async_get_chat_log(hass, session) as chat_log,
    ):
        recorded_ids = [
            item.tool_call_id
            for item in chat_log.content
            if isinstance(item, conversation.ToolResultContent)
            and item.tool_call_id in original_ids
        ]
    assert recorded_ids == original_ids


async def test_collapsed_state_error_reaches_every_original_tool_id(
    hass: HomeAssistant,
    bedrock: FakeBedrock,
    agent_id: str,
    state_tool_calls: list[dict[str, object]],
) -> None:
    """One atomic state failure is reported for every grouped tool use."""
    known = "light.known"
    missing = "light.missing"
    hass.states.async_set(known, "on")
    async_expose_entity(hass, "conversation", known, True)
    bedrock.responses = [
        tool_events(
            (
                "known-state",
                "HassGetState",
                json.dumps({"entity_id": known}),
            ),
            (
                "missing-state",
                "HassGetState",
                json.dumps({"entity_id": missing}),
            ),
        ),
        text_events("No pude consultar los estados."),
    ]

    await _ask(hass, agent_id, "Consulta dos estados")

    tool_results = _tool_results(bedrock.calls[1])
    assert [item["toolUseId"] for item in tool_results] == [
        "known-state",
        "missing-state",
    ]
    assert all(item.get("status") == "error" for item in tool_results)
    assert [item["content"][0]["json"] for item in tool_results] == [
        {"error": f"Unknown device {missing}"},
        {"error": f"Unknown device {missing}"},
    ]
    assert state_tool_calls == [{"entity_ids": [known, missing]}]
