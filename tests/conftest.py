"""Fixtures for the AWS Bedrock Conversation tests.

Bedrock itself is never called: ``FakeBedrock`` replaces the network methods
of ``BedrockClient`` and replays scripted ConverseStream events.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import copy
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.bedrock_conversation.client import (
    BedrockClient,
    CostExplorerData,
    ModelOption,
)
from custom_components.bedrock_conversation.const import (
    CONF_AWS_ACCESS_KEY_ID,
    CONF_AWS_REGION,
    CONF_AWS_SECRET_ACCESS_KEY,
    CONF_AWS_SESSION_TOKEN,
    CONF_LLM_HASS_API,
    CONF_MODEL_ID,
    CONF_PROMPT,
    DOMAIN,
    HOME_LLM_API_ID,
    SUBENTRY_CONVERSATION,
)

MODEL = "eu.anthropic.claude-sonnet-4-6"
MODELS = [
    ModelOption(MODEL, f"EU Claude Sonnet 4.6 ({MODEL})"),
    ModelOption("amazon.nova-lite-v1:0", "Amazon Nova Lite (amazon.nova-lite-v1:0)"),
]
ENTRY_DATA = {
    CONF_AWS_REGION: "eu-west-3",
    CONF_AWS_ACCESS_KEY_ID: "AKIATEST",
    CONF_AWS_SECRET_ACCESS_KEY: "test-secret",
    CONF_AWS_SESSION_TOKEN: "",
}


def text_events(text: str, stop: str = "end_turn") -> list[dict[str, Any]]:
    """ConverseStream events for a plain text answer."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": stop}},
    ]


def tool_events(*calls: tuple[str, str, str], text: str | None = None) -> list[dict]:
    """ConverseStream events for tool calls given as (id, name, json_args)."""
    events: list[dict[str, Any]] = [{"messageStart": {"role": "assistant"}}]
    index = 0
    if text:
        events += [
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
        ]
        index = 1
    for tool_id, name, args in calls:
        events += [
            {
                "contentBlockStart": {
                    "contentBlockIndex": index,
                    "start": {"toolUse": {"toolUseId": tool_id, "name": name}},
                }
            },
            # Arguments arrive in pieces, like the real stream.
            {
                "contentBlockDelta": {
                    "contentBlockIndex": index,
                    "delta": {"toolUse": {"input": args[:5]}},
                }
            },
            {
                "contentBlockDelta": {
                    "contentBlockIndex": index,
                    "delta": {"toolUse": {"input": args[5:]}},
                }
            },
            {"contentBlockStop": {"contentBlockIndex": index}},
        ]
        index += 1
    events.append({"messageStop": {"stopReason": "tool_use"}})
    return events


class FakeBedrock:
    """Scripted stand-in for the Bedrock network calls."""

    def __init__(self) -> None:
        """Start with no scripted responses."""
        self.responses: list[list[dict[str, Any]] | Exception] = []
        self.calls: list[dict[str, Any]] = []
        self.models = list(MODELS)
        self.validate_error: Exception | None = None
        self.cost_error: Exception | None = None
        self.cost_calls: list[tuple[date, date, str, str]] = []
        self.cost_data = CostExplorerData(
            amount=Decimal("1.234567"),
            currency="USD",
            period_start=date(2026, 11, 1),
            period_end=date(2026, 11, 11),
            estimated=True,
            retrieved_at=datetime(2026, 11, 10, 12, 0, tzinfo=UTC),
        )

    async def converse_stream(self, kwargs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(copy.deepcopy(kwargs))
        if not self.responses:
            raise AssertionError("Bedrock called more often than scripted")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        for event in response:
            yield event

    async def validate(self) -> None:
        if self.validate_error:
            raise self.validate_error

    async def list_models(self) -> list[ModelOption]:
        return self.models

    async def get_cost(
        self,
        period_start: date,
        period_end: date,
        tag_key: str,
        tag_value: str,
    ) -> CostExplorerData:
        self.cost_calls.append((period_start, period_end, tag_key, tag_value))
        if self.cost_error:
            raise self.cost_error
        return self.cost_data


@pytest.fixture
def bedrock() -> FakeBedrock:
    """Patch BedrockClient's network methods."""
    fake = FakeBedrock()

    async def noop(_client: BedrockClient) -> None:
        return None

    with (
        patch.object(BedrockClient, "async_setup", noop),
        patch.object(BedrockClient, "async_validate", lambda _c: fake.validate()),
        patch.object(BedrockClient, "async_list_models", lambda _c: fake.list_models()),
        patch.object(
            BedrockClient,
            "async_get_month_to_date_cost",
            lambda _c, start, end, tag_key, tag_value: fake.get_cost(
                start, end, tag_key, tag_value
            ),
        ),
        patch.object(
            BedrockClient, "async_converse_stream", lambda _c, kw: fake.converse_stream(kw)
        ),
    ):
        yield fake


@pytest.fixture
def entry_options() -> dict[str, Any]:
    """Options of the test config entry; override per test."""
    return {
        CONF_MODEL_ID: MODEL,
        CONF_PROMPT: "Eres un asistente.",
        CONF_LLM_HASS_API: [HOME_LLM_API_ID],
    }


@pytest.fixture
async def config_entry(
    hass: HomeAssistant, bedrock: FakeBedrock, entry_options: dict[str, Any]
) -> MockConfigEntry:
    """Set up a v2 config entry with HA's conversation stack."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    assert await async_setup_component(hass, "intent", {})
    assert await async_setup_component(hass, "llm", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="AWS Bedrock (eu-west-3)",
        data=ENTRY_DATA,
        version=3,
        minor_version=1,
        subentries_data=[
            ConfigSubentryData(
                data=entry_options,
                subentry_type=SUBENTRY_CONVERSATION,
                title="AWS Bedrock",
                unique_id=None,
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
def agent_id(config_entry: MockConfigEntry, hass: HomeAssistant) -> str:
    """Entity id of the conversation agent."""
    subentry_id = next(iter(config_entry.subentries))
    entity_id = er.async_get(hass).async_get_entity_id("conversation", DOMAIN, subentry_id)
    assert entity_id
    return entity_id
