"""Conversation agent backed by the AWS Bedrock Converse API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
import json
from typing import TYPE_CHECKING, Any, Literal, override

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, llm
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client import (
    BedrockAccessDeniedError,
    BedrockAuthError,
    BedrockConnectionError,
    BedrockError,
)
from .confirmation import (
    CONFIRMATION_IMPOSSIBLE_RESULT,
    CONFIRMATION_REQUIRED_RESULT,
    PendingConfirmation,
    SensitiveAction,
    assess,
    confirmation_question,
    is_affirmative,
)
from .const import (
    CONF_CONFIRM_SENSITIVE_ACTIONS,
    CONF_EXTRA_ATTRIBUTES_TO_EXPOSE,
    CONF_LLM_HASS_API,
    CONF_MAX_TOKENS,
    CONF_MAX_TOOL_CALL_ITERATIONS,
    CONF_MODEL_ID,
    CONF_PROMPT,
    CONF_PROMPT_CACHING,
    CONF_TEMPERATURE,
    DEFAULT_CONFIRM_SENSITIVE_ACTIONS,
    DEFAULT_EXTRA_ATTRIBUTES,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MAX_TOOL_CALL_ITERATIONS,
    DEFAULT_PROMPT,
    DEFAULT_PROMPT_CACHING,
    DEFAULT_TEMPERATURE,
    DOMAIN,
    LOGGER,
    MAX_TOOL_CALLS_PER_RESPONSE,
    MAX_TOOL_OPERATIONS_PER_RESPONSE,
    SERVICE_TOOL_NAME,
    SUBENTRY_CONVERSATION,
    TURN_TIMEOUT,
    exposure_bounded_llm_api_ids,
)
from .llm_api import use_extra_attributes
from .messages import ToolNames, build_system, build_tool_config, convert_content

if TYPE_CHECKING:
    from . import BedrockConfigEntry

# Home Assistant also bounds each credentials client below; entity-level
# serialization would unnecessarily prevent its configured stream concurrency.
PARALLEL_UPDATES = 0

# Stop reasons after which the answer is unusable.
_FAILED_STOP_REASONS = {
    "guardrail_intervened": "response_blocked",
    "content_filtered": "response_blocked",
    "model_context_window_exceeded": "context_window_exceeded",
}


@dataclass(slots=True)
class _ToolOperation:
    """One operation after collapsing homogeneous service calls."""

    call: llm.ToolInput
    originals: list[llm.ToolInput]
    execute_here: bool = False


def _tool_operations(calls: list[llm.ToolInput]) -> list[_ToolOperation]:
    """Collapse per-device HassCallService calls into bounded bulk operations.

    Original calls remain in the assistant message so every Bedrock tool-use ID
    receives a result. Collapsible calls are marked external and executed once
    here; all other tools retain Home Assistant's normal execution path.
    """
    operations: list[_ToolOperation] = []
    service_groups: dict[str, _ToolOperation] = {}

    for call in calls:
        if call.tool_name.rsplit("__", 1)[-1] != SERVICE_TOOL_NAME:
            operations.append(_ToolOperation(call, [call]))
            continue

        args = dict(call.tool_args)
        target_device = args.pop("target_device", None)
        target_devices = args.pop("target_devices", None)
        if (target_device is None) == (target_devices is None):
            operations.append(_ToolOperation(call, [call]))
            continue
        targets = [target_device] if target_device is not None else target_devices
        if not isinstance(targets, list) or not all(isinstance(target, str) for target in targets):
            operations.append(_ToolOperation(call, [call]))
            continue

        key = json.dumps([call.tool_name, args], sort_keys=True, default=str)
        if (operation := service_groups.get(key)) is None:
            canonical_args = {**args, "target_devices": []}
            operation = _ToolOperation(
                llm.ToolInput(tool_name=call.tool_name, tool_args=canonical_args),
                [],
                execute_here=True,
            )
            service_groups[key] = operation
            operations.append(operation)
        operation.originals.append(call)
        operation.call.tool_args["target_devices"].extend(targets)
        call.external = True

    for operation in service_groups.values():
        operation.call.tool_args["target_devices"] = list(
            dict.fromkeys(operation.call.tool_args["target_devices"])
        )
    return operations


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BedrockConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one conversation entity per agent sub-entry."""
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_CONVERSATION:
            continue
        async_add_entities(
            [BedrockConversationEntity(entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class BedrockConversationEntity(
    conversation.ConversationEntity, conversation.AbstractConversationAgent
):
    """Bedrock conversation agent."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supports_streaming = True

    def __init__(self, entry: BedrockConfigEntry, subentry: ConfigSubentry) -> None:
        """Initialise the agent from its sub-entry."""
        self.entry = entry
        self.subentry = subentry
        model_id = subentry.data.get(CONF_MODEL_ID, "")
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="Amazon Web Services",
            model=model_id or None,
            entry_type=dr.DeviceEntryType.SERVICE,
        )
        if subentry.data.get(CONF_LLM_HASS_API):
            self._attr_supported_features = conversation.ConversationEntityFeature.CONTROL
        # conversation_id -> tool calls waiting for the user's "yes".
        self._pending: dict[str, PendingConfirmation] = {}

    @property
    @override
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return the supported languages (the model handles any)."""
        return MATCH_ALL

    @override
    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Answer one user turn."""
        options = self.subentry.data
        llm_api_ids = exposure_bounded_llm_api_ids(options.get(CONF_LLM_HASS_API))
        try:
            with use_extra_attributes(
                options.get(CONF_EXTRA_ATTRIBUTES_TO_EXPOSE, DEFAULT_EXTRA_ATTRIBUTES)
            ):
                await chat_log.async_provide_llm_data(
                    user_input.as_llm_context(DOMAIN),
                    llm_api_ids or None,
                    options.get(CONF_PROMPT, DEFAULT_PROMPT),
                    user_input.extra_system_prompt,
                )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        try:
            async with asyncio.timeout(TURN_TIMEOUT):
                await self._async_resolve_pending(user_input, chat_log)
                await self._async_handle_chat_log(chat_log, user_input.language)
        except TimeoutError as err:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="timeout") from err

        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    async def _async_resolve_pending(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> None:
        """Run held calls if the requester just said yes; otherwise cancel them.

        Either way the outcome is recorded in the chat log as the result of
        the held calls, so the model cannot claim they ran when they did not.
        """
        # Drop other conversations' stale holds. The current one is kept so
        # its expiry can be reported to the model below.
        for conversation_id in [c for c, p in self._pending.items() if p.expired]:
            if conversation_id != chat_log.conversation_id:
                del self._pending[conversation_id]
        pending = self._pending.pop(chat_log.conversation_id, None)
        if pending is None or chat_log.llm_api is None:
            return

        if pending.expired:
            cancel_reason: str | None = "The confirmation expired."
        elif not pending.same_requester(chat_log.llm_api.llm_context):
            cancel_reason = "The answer came from a different user or device."
        elif not is_affirmative(user_input.text):
            cancel_reason = "The user did not confirm."
        else:
            cancel_reason = None

        # Recorded as external calls so the chat log does not run them again.
        calls = [
            llm.ToolInput(tool_name=c.tool_name, tool_args=c.tool_args, external=True)
            for c in pending.calls
        ]
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=self.entity_id, tool_calls=calls)
        )
        if cancel_reason is not None:
            results = [
                llm.ToolResult(
                    data={"status": "cancelled", "performed": False, "reason": cancel_reason},
                    error=True,
                )
                for _ in calls
            ]
        else:
            results = await self._async_call_tools(chat_log.llm_api, calls)
        for call, result in zip(calls, results, strict=True):
            chat_log.async_add_assistant_content_without_tools(
                conversation.ToolResultContent(
                    agent_id=self.entity_id,
                    tool_call_id=call.id,
                    tool_name=call.tool_name,
                    result=result,
                )
            )

    async def _async_call_tools(
        self, llm_api: llm.APIInstance, calls: list[llm.ToolInput]
    ) -> list[llm.ToolResult]:
        """Run a bounded call batch concurrently and retain input order."""
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(self._async_call_tool(llm_api, call)) for call in calls]
        return [task.result() for task in tasks]

    async def _async_call_tool(
        self, llm_api: llm.APIInstance, call: llm.ToolInput
    ) -> llm.ToolResult:
        """Run a tool and always return a result for the chat log."""
        try:
            return await llm_api.async_call_tool(
                llm.ToolInput(tool_name=call.tool_name, tool_args=call.tool_args, id=call.id)
            )
        except Exception as err:
            LOGGER.warning("Tool %s failed: %s", call.tool_name, err)
            return llm.ToolResult(
                data={"error": type(err).__name__, "error_text": str(err)}, error=True
            )

    async def _async_handle_chat_log(
        self, chat_log: conversation.ChatLog, language: str | None
    ) -> None:
        """Call Bedrock until it answers without asking for more tools."""
        options = self.subentry.data
        client = self.entry.runtime_data.client
        cache = options.get(CONF_PROMPT_CACHING, DEFAULT_PROMPT_CACHING)

        system = chat_log.content[0]
        if not isinstance(system, conversation.SystemContent):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="system_message_not_found"
            )

        tool_config, names = build_tool_config(chat_log.llm_api, cache=cache)
        kwargs: dict[str, Any] = {
            "modelId": options[CONF_MODEL_ID],
            "system": build_system(system, cache=cache),
            "inferenceConfig": {
                "maxTokens": int(options.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS)),
                "temperature": float(options.get(CONF_TEMPERATURE, DEFAULT_TEMPERATURE)),
            },
        }
        if tool_config:
            kwargs["toolConfig"] = tool_config

        max_iterations = int(
            options.get(CONF_MAX_TOOL_CALL_ITERATIONS, DEFAULT_MAX_TOOL_CALL_ITERATIONS)
        )
        for _ in range(max(max_iterations, 1)):
            kwargs["messages"] = convert_content(chat_log.content[1:], names)
            stream = _DeltaStream(self, chat_log, names, language)
            try:
                # Draining the stream is what writes the deltas into the chat log.
                async for _content in chat_log.async_add_delta_content_stream(
                    self.entity_id, stream.deltas(client.async_converse_stream(kwargs))
                ):
                    pass
            except BedrockError as err:
                raise self._user_facing_error(err) from err

            if (key := _FAILED_STOP_REASONS.get(stream.stop_reason or "")) is not None:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key=key)
            if stream.asked_confirmation or not chat_log.unresponded_tool_results:
                return

        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="too_many_tool_calls")

    def _user_facing_error(self, err: BedrockError) -> HomeAssistantError:
        """Turn a Bedrock error into a short message; start reauth if needed.

        The AWS message can contain the account id and IAM principal, so it is
        logged but not spoken.
        """
        if isinstance(err, BedrockAuthError):
            LOGGER.error("AWS rejected the credentials: %s", err)
            self.entry.async_start_reauth(self.hass)
            key = "auth_error"
        elif isinstance(err, BedrockAccessDeniedError):
            LOGGER.error("Access denied by AWS Bedrock: %s", err)
            key = "access_denied"
        elif isinstance(err, BedrockConnectionError):
            LOGGER.warning("Could not reach AWS Bedrock: %s", err)
            key = "cannot_connect"
        else:
            LOGGER.error("AWS Bedrock error: %s", err)
            key = "api_error"
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders={"code": err.code},
        )

    def hold_for_confirmation(
        self, chat_log: conversation.ChatLog, calls: list[llm.ToolInput]
    ) -> bool:
        """Remember held calls until the requester confirms them.

        Returns False if the request has neither a user nor a device, since
        then anyone continuing the conversation could confirm it. Identical
        calls are kept once, so one "yes" never runs a script twice.
        """
        llm_context = chat_log.llm_api.llm_context if chat_log.llm_api else None
        context = llm_context.context if llm_context else None
        user_id = context.user_id if context else None
        device_id = llm_context.device_id if llm_context else None
        if user_id is None and device_id is None:
            return False
        unique: dict[str, llm.ToolInput] = {}
        for call in calls:
            key = json.dumps([call.tool_name, call.tool_args], sort_keys=True, default=str)
            unique.setdefault(
                key, llm.ToolInput(tool_name=call.tool_name, tool_args=call.tool_args)
            )
        self._pending[chat_log.conversation_id] = PendingConfirmation(
            calls=list(unique.values()), user_id=user_id, device_id=device_id
        )
        return True


class _DeltaStream:
    """Translate ConverseStream events into chat log deltas."""

    def __init__(
        self,
        entity: BedrockConversationEntity,
        chat_log: conversation.ChatLog,
        names: ToolNames,
        language: str | None,
    ) -> None:
        self._entity = entity
        self._chat_log = chat_log
        self._names = names
        self._language = language
        self._confirm = entity.subentry.data.get(
            CONF_CONFIRM_SENSITIVE_ACTIONS, DEFAULT_CONFIRM_SENSITIVE_ACTIONS
        )
        self._tools = {t.name: t for t in chat_log.llm_api.tools} if chat_log.llm_api else {}
        self.stop_reason: str | None = None
        self.asked_confirmation = False

    async def deltas(
        self, events: AsyncIterator[dict[str, Any]]
    ) -> AsyncIterator[
        conversation.AssistantContentDeltaDict | conversation.ToolResultContentDeltaDict
    ]:
        """Yield deltas for the chat log, holding back sensitive tool calls."""
        blocks: list[dict[str, str]] = []
        current: dict[str, str] | None = None
        tool_call_count = 0

        async for event in events:
            if "messageStart" in event:
                yield {"role": "assistant"}
            elif "contentBlockStart" in event:
                start = event["contentBlockStart"].get("start", {})
                if "toolUse" in start:
                    tool_call_count += 1
                    if tool_call_count > MAX_TOOL_CALLS_PER_RESPONSE:
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="too_many_tool_calls_in_response",
                            translation_placeholders={"maximum": str(MAX_TOOL_CALLS_PER_RESPONSE)},
                        )
                    current = {
                        "id": start["toolUse"]["toolUseId"],
                        "name": start["toolUse"]["name"],
                        "input": "",
                    }
            elif "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                if "text" in delta:
                    yield {"content": delta["text"]}
                elif "toolUse" in delta and current is not None:
                    current["input"] += delta["toolUse"].get("input", "")
            elif "contentBlockStop" in event and current is not None:
                blocks.append(current)
                current = None
            elif "messageStop" in event:
                self.stop_reason = event["messageStop"].get("stopReason")
            elif "metadata" in event:
                usage = event["metadata"].get("usage")
                LOGGER.debug("Bedrock usage: %s", usage)
                if usage is not None:
                    self._chat_log.async_trace(
                        {
                            "stats": {
                                "input_tokens": usage.get("inputTokens"),
                                "output_tokens": usage.get("outputTokens"),
                            },
                            "usage": usage,
                        }
                    )

        # Tool arguments are parsed once the stop reason is known, so a reply
        # cut off by the token limit gets a clear error.
        calls = [self._tool_input(block) for block in blocks]
        async for delta in self._post_stream_deltas(calls):
            yield delta

    def _classify_tool_operations(
        self, calls: list[llm.ToolInput]
    ) -> tuple[list[_ToolOperation], list[tuple[_ToolOperation, SensitiveAction]]]:
        """Collapse and split calls into immediate and confirmation-held operations."""
        operations = _tool_operations(calls)
        if len(operations) > MAX_TOOL_OPERATIONS_PER_RESPONSE:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="too_many_tool_operations_in_response",
                translation_placeholders={"maximum": str(MAX_TOOL_OPERATIONS_PER_RESPONSE)},
            )

        held: list[tuple[_ToolOperation, SensitiveAction]] = []
        immediate: list[_ToolOperation] = []
        if self._confirm:
            for operation in operations:
                if action := assess(
                    self._entity.hass,
                    operation.call,
                    self._tools.get(operation.call.tool_name),
                ):
                    for original in operation.originals:
                        original.external = True
                    held.append((operation, action))
                elif operation.execute_here:
                    immediate.append(operation)
        else:
            immediate = [operation for operation in operations if operation.execute_here]
        return immediate, held

    async def _post_stream_deltas(
        self, calls: list[llm.ToolInput]
    ) -> AsyncIterator[
        conversation.AssistantContentDeltaDict | conversation.ToolResultContentDeltaDict
    ]:
        """Emit calls, execute immediate operations, and hold sensitive operations."""
        immediate, held = self._classify_tool_operations(calls)
        if calls:
            yield {"tool_calls": calls}

        # Valid HassCallService operations are executed here after collapsing,
        # rather than letting HA create one task per original model call.
        if immediate:
            llm_api = self._chat_log.llm_api
            if llm_api is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="tool_api_missing"
                )
            results = await self._entity._async_call_tools(
                llm_api, [operation.call for operation in immediate]
            )
            for operation, result in zip(immediate, results, strict=True):
                if len(operation.originals) > 1 and not result.error:
                    result = _compact_grouped_result(result)
                for original in operation.originals:
                    yield _tool_result_from_result(original, result)

        if not held:
            return

        canonical_calls = [operation.call for operation, _ in held]
        if not self._entity.hold_for_confirmation(self._chat_log, canonical_calls):
            # Nobody identifiable could confirm (e.g. an automation calling
            # conversation.process): refuse, and let the model answer.
            for operation, _ in held:
                for original in operation.originals:
                    yield _tool_result(original, CONFIRMATION_IMPOSSIBLE_RESULT, error=True)
            return
        self.asked_confirmation = True
        for operation, _ in held:
            for original in operation.originals:
                yield _tool_result(original, CONFIRMATION_REQUIRED_RESULT)
        # One action per collapsed operation lets wording summarize large groups.
        yield {
            "role": "assistant",
            "content": confirmation_question(self._language, [action for _, action in held]),
        }

    def _tool_input(self, block: dict[str, str]) -> llm.ToolInput:
        """Build a tool call from a finished toolUse block."""
        try:
            args = json.loads(block["input"]) if block["input"] else {}
        except json.JSONDecodeError as err:
            key = "response_too_long" if self.stop_reason == "max_tokens" else "tool_args_error"
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key=key) from err
        return llm.ToolInput(
            id=block["id"],
            tool_name=self._names.to_ha.get(block["name"], block["name"]),
            tool_args=args if isinstance(args, dict) else {},
        )


def _compact_grouped_result(result: llm.ToolResult) -> llm.ToolResult:
    """Remove the repeated bulk target list from grouped success results."""
    data = dict(result.data)
    data.pop("targets", None)
    return llm.ToolResult(data=data, error=result.error)


def _tool_result_from_result(
    call: llm.ToolInput, result: llm.ToolResult
) -> conversation.ToolResultContentDeltaDict:
    """Return a delta preserving the original Bedrock tool-use ID."""
    return {
        "role": "tool_result",
        "tool_call_id": call.id,
        "tool_name": call.tool_name,
        "result": result,
    }


def _tool_result(
    call: llm.ToolInput, data: dict[str, Any], *, error: bool = False
) -> conversation.ToolResultContentDeltaDict:
    return {
        "role": "tool_result",
        "tool_call_id": call.id,
        "tool_name": call.tool_name,
        "result": llm.ToolResult(data=dict(data), error=error),
    }
