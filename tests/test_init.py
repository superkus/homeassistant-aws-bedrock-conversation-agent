"""Tests for setup, unload and migration of config entries."""

from __future__ import annotations

import json

from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import device_registry as dr, entity_registry as er, llm
from homeassistant.helpers.template import Template
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.bedrock_conversation import migrate_v1_to_v2
from custom_components.bedrock_conversation.client import (
    BedrockAccessDeniedError,
    BedrockAuthError,
    BedrockConnectionError,
)
from custom_components.bedrock_conversation.const import CONF_PROMPT, DOMAIN, HOME_LLM_API_ID

from .conftest import ENTRY_DATA, MODEL, FakeBedrock, text_events, tool_events


@pytest.fixture(autouse=True)
async def setup_core(hass: HomeAssistant) -> None:
    """Load HA core (exposed entities), which conversation needs."""
    assert await async_setup_component(hass, "homeassistant", {})


# The real 1.0.37 entry this upgrade was written for (credentials replaced).
PRODUCTION_V1_DATA = {
    "aws_region": "us-east-1",
    "aws_access_key_id": "AKIATEST",
    "aws_secret_access_key": "secret",
    "aws_session_token": "",
}
PRODUCTION_V1_OPTIONS = {
    "aws_region": "eu-west-3",
    "llm_hass_api": "bedrock_conversation_services",
    "max_tokens": 4096.0,
    "max_tool_call_iterations": 5.0,
    "model": "eu.anthropic.claude-sonnet-4-6",
    "prompt": (
        "<prompt>Hablas Español de España. Eres un asistente.\n<current_date>\n\n"
        "<devices>Tienes permiso para trabajar con persianas y estores, nada mas."
    ),
    "refresh_prompt_per_turn": True,
    "remember_conversation": True,
    "remember_num_interactions": 10.0,
    "temperature": 0.5,
    "top_k": 250.0,
    "top_p": 0.999,
}


def test_migrate_production_entry() -> None:
    """The entry from the user's Home Assistant migrates as expected."""
    data, options = migrate_v1_to_v2(PRODUCTION_V1_DATA, PRODUCTION_V1_OPTIONS)
    # The region in use was the options one (eu-west-3), not the setup one.
    assert data == {**PRODUCTION_V1_DATA, "aws_region": "eu-west-3"}
    assert options == {
        "llm_hass_api": ["bedrock_conversation_services"],
        "max_tokens": 4096,
        "max_tool_call_iterations": 5,
        "model": "eu.anthropic.claude-sonnet-4-6",
        "prompt": (
            "<prompt>Hablas Español de España. Eres un asistente.\n\n\n"
            "Tienes permiso para trabajar con persianas y estores, nada mas."
        ),
        "temperature": 0.5,
    }


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("Usa {{ esto }} tal cual\n<devices>", "{% raw %}Usa {{ esto }} tal cual{% endraw %}"),
        ("<persona>", None),  # nothing left: HA's default instructions
        ("<persona>\n\n<current_date>\n\n<devices>", None),
        ("Hola <devices>", "Hola"),
    ],
)
def test_migrate_prompts(prompt: str, expected: str | None) -> None:
    """Placeholders go; template syntax stays literal; empty prompts go."""
    _, options = migrate_v1_to_v2({}, {"prompt": prompt})
    assert options.get("prompt") == expected
    assert migrate_v1_to_v2({}, options)[1].get("prompt") == expected


@pytest.mark.parametrize(
    "prompt",
    [
        "{% raw %} unmatched raw marker",
        "literal {% endraw %} marker",
        "before {% raw %}{{ value }}{% endraw %} after",
    ],
)
def test_migrate_prompt_preserves_literal_raw_markers(hass: HomeAssistant, prompt: str) -> None:
    """Legacy raw-marker text remains literal and renders without an error."""
    _, options = migrate_v1_to_v2({}, {CONF_PROMPT: prompt})
    try:
        rendered = Template(options[CONF_PROMPT], hass).async_render(parse_result=False)
        error = None
    except TemplateError as err:
        rendered = None
        error = err
    assert error is None, f"migrated prompt does not compile: {error}"
    assert rendered == prompt


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (llm.LLM_API_HOME_ASSISTANT, []),
        ("third_party_unrestricted", []),
        (
            [
                llm.LLM_API_HOME_ASSISTANT,
                "third_party_unrestricted",
                llm.LLM_API_ASSIST,
                HOME_LLM_API_ID,
            ],
            [llm.LLM_API_ASSIST, HOME_LLM_API_ID],
        ),
    ],
)
def test_migration_keeps_only_exposure_bounded_apis(
    configured: str | list[str], expected: list[str]
) -> None:
    """Migration drops unrestricted core and third-party device APIs."""
    _, options = migrate_v1_to_v2({}, {"llm_hass_api": configured})
    assert options["llm_hass_api"] == expected


def test_migrate_is_idempotent() -> None:
    """Running the migration twice gives the same result."""
    once = migrate_v1_to_v2(PRODUCTION_V1_DATA, PRODUCTION_V1_OPTIONS)
    assert migrate_v1_to_v2(*once) == once


def test_migrate_oldest_entries() -> None:
    """1.0.0-1.0.7 shapes: old API id, stray token in options, default prompt.

    1.x never read the session token from options, so a leftover one must not
    start being used: it would break setups that work with permanent keys.
    """
    data, options = migrate_v1_to_v2(
        {"aws_region": "us-west-2", "aws_access_key_id": "A", "aws_secret_access_key": "S"},
        {
            "aws_session_token": "TOKEN",
            "llm_hass_api": "bedrock-service-api",
            "refresh_system_prompt": False,
            "selected_language": "en",
            "request_timeout": 60,
            "prompt": "<persona>\n\n<current_date>\n\n<devices>",
            "extra_attributes_to_expose": [],
            "unknown_future_key": 1,
        },
    )
    assert data.get("aws_session_token", "") == ""  # v1 value (absent) kept
    assert "TOKEN" not in data.values()
    assert "aws_session_token" not in options
    assert data["aws_region"] == "us-west-2"
    assert options == {
        "llm_hass_api": [HOME_LLM_API_ID],
        "extra_attributes_to_expose": [],  # user's choice kept
        "unknown_future_key": 1,  # unknown keys kept
    }


ENTITY_ID = "conversation.bedrock_conversation_01kjxdtqr7pk8gjd7j2bsrh5bx"


async def test_migration_on_setup(hass: HomeAssistant, bedrock: FakeBedrock) -> None:
    """The production 1.x entry ends as v3 with one agent; the entity id stays."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="01KJXDTQR7PK8GJD7J2BSRH5BX",
        title="AWS Bedrock (us-east-1)",
        data=PRODUCTION_V1_DATA,
        options=PRODUCTION_V1_OPTIONS,
        version=1,
    )
    entry.add_to_hass(hass)
    # The entity and device as 1.x registered them (unique id = entry id).
    entity_registry, device_registry = er.async_get(hass), dr.async_get(hass)
    entity_registry.async_get_or_create(
        "conversation",
        DOMAIN,
        entry.entry_id,
        config_entry=entry,
        suggested_object_id="bedrock_conversation_01kjxdtqr7pk8gjd7j2bsrh5bx",
    )
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert (entry.version, entry.minor_version) == (3, 1)
    assert entry.title == "AWS Bedrock (eu-west-3)"
    assert entry.data["aws_region"] == "eu-west-3"
    assert entry.options == {}
    [agent] = entry.subentries.values()
    _, expected = migrate_v1_to_v2(PRODUCTION_V1_DATA, PRODUCTION_V1_OPTIONS)
    assert dict(agent.data) == expected

    entity = entity_registry.async_get(ENTITY_ID)
    assert entity is not None  # same entity id: voice assistants keep working
    assert entity.unique_id == agent.subentry_id
    assert entity.config_subentry_id == agent.subentry_id
    assert hass.states.get(ENTITY_ID) is not None
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, agent.subentry_id), entry.entry_id
    )
    assert device is not None
    assert device.config_entry_id == entry.entry_id
    assert device.config_subentry_id == agent.subentry_id


@pytest.fixture
async def migrated_empty_attributes_agent(hass: HomeAssistant, bedrock: FakeBedrock) -> str:
    """Set up a migrated v1 agent that explicitly exposes no extra attributes."""
    hass.states.async_set(
        "light.migrated",
        "on",
        {"friendly_name": "Migrated light", "brightness": 255, "rgb_color": [255, 0, 0]},
    )
    async_expose_entity(hass, "conversation", "light.migrated", True)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="AWS Bedrock (eu-west-3)",
        data=ENTRY_DATA,
        options={
            "llm_hass_api": "bedrock-service-api",
            "model": MODEL,
            "prompt": "Eres un asistente.",
            "extra_attributes_to_expose": [],
        },
        version=1,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    [agent] = entry.subentries.values()
    assert agent.data["extra_attributes_to_expose"] == []
    agent_id = er.async_get(hass).async_get_entity_id("conversation", DOMAIN, agent.subentry_id)
    assert agent_id is not None
    return agent_id


async def _ask_migrated(hass: HomeAssistant, agent_id: str, text: str):
    return await conversation.async_converse(
        hass,
        text,
        None,
        Context(),
        language="es",
        agent_id=agent_id,
        device_id="satellite-1",
    )


async def test_migrated_empty_attributes_suppresses_device_list_attributes(
    hass: HomeAssistant,
    bedrock: FakeBedrock,
    migrated_empty_attributes_agent: str,
) -> None:
    """A migrated empty attribute list leaves extra data out of device context."""
    bedrock.responses = [text_events("Hola")]
    await _ask_migrated(hass, migrated_empty_attributes_agent, "Hola")
    device_line = next(
        line
        for line in bedrock.calls[0]["system"][0]["text"].splitlines()
        if line.startswith("- light.migrated;")
    )
    assert "brightness" not in device_line
    assert "color" not in device_line


async def test_migrated_empty_attributes_suppresses_get_state_attributes(
    hass: HomeAssistant,
    bedrock: FakeBedrock,
    migrated_empty_attributes_agent: str,
) -> None:
    """HassGetState honors a migrated empty attribute list at call time."""
    bedrock.responses = [
        tool_events(
            (
                "state-1",
                "HassGetState",
                json.dumps({"entity_id": "light.migrated"}),
            )
        ),
        text_events("Está encendida."),
    ]
    await _ask_migrated(hass, migrated_empty_attributes_agent, "Estado de la luz")
    tool_results = [
        block["toolResult"]
        for message in bedrock.calls[1]["messages"]
        for block in message["content"]
        if "toolResult" in block
    ]
    assert tool_results[-1]["content"][0]["json"]["attributes"] == []


def _conversation_entities(hass: HomeAssistant, entry: MockConfigEntry) -> list[er.RegistryEntry]:
    return [
        e
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.domain == "conversation"
    ]


def _register_v1_entity(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """The entity exactly as 1.x left it: unique id = entry id, no device, no sub-entry."""
    entity = er.async_get(hass).async_get_or_create(
        "conversation",
        DOMAIN,
        entry.entry_id,
        config_entry=entry,
        suggested_object_id="bedrock_conversation_01kjxdtqr7pk8gjd7j2bsrh5bx",
    )
    assert entity.entity_id == ENTITY_ID
    assert entity.device_id is None
    assert entity.config_subentry_id is None


def _assert_single_repointed_entity(
    hass: HomeAssistant, entry: MockConfigEntry, subentry_id: str
) -> None:
    entities = _conversation_entities(hass, entry)
    assert [e.entity_id for e in entities] == [ENTITY_ID]  # no second entity was created
    [entity] = entities
    assert entity.unique_id == subentry_id
    assert entity.config_subentry_id == subentry_id
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, subentry_id), entry.entry_id
    )
    assert device is not None
    assert device.identifiers == {(DOMAIN, subentry_id)}
    assert entity.device_id == device.id
    assert hass.states.get(ENTITY_ID) is not None


async def test_migration_production_registry_without_device(
    hass: HomeAssistant, bedrock: FakeBedrock
) -> None:
    """The real 1.x registry: an entity keyed by entry id and no device.

    1.x never registered a device, so the migration must not depend on one.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="01KJXDTQR7PK8GJD7J2BSRH5BX",
        title="AWS Bedrock (us-east-1)",
        data=PRODUCTION_V1_DATA,
        options=PRODUCTION_V1_OPTIONS,
        version=1,
    )
    entry.add_to_hass(hass)
    _register_v1_entity(hass, entry)
    assert not dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert (entry.version, entry.minor_version) == (3, 1)
    [agent] = entry.subentries.values()
    _assert_single_repointed_entity(hass, entry, agent.subentry_id)


async def test_setup_repoints_entity_left_by_unclean_stop(
    hass: HomeAssistant, bedrock: FakeBedrock
) -> None:
    """Entry saved as v3.1 but the entity registry still has the 1.x entity.

    An unclean stop after the migration can persist the entry but not the
    entity registry. Setup must re-point the old entity instead of creating
    a second agent with a new entity id.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="01KJXDTQR7PK8GJD7J2BSRH5BX",
        title="AWS Bedrock (eu-west-3)",
        data={**PRODUCTION_V1_DATA, "aws_region": "eu-west-3"},
        options={},
        version=3,
        minor_version=1,
        subentries_data=[
            ConfigSubentryData(
                data=migrate_v1_to_v2(PRODUCTION_V1_DATA, PRODUCTION_V1_OPTIONS)[1],
                subentry_type="conversation",
                title="AWS Bedrock (eu-west-3)",
                unique_id=None,
            )
        ],
    )
    entry.add_to_hass(hass)
    _register_v1_entity(hass, entry)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    [agent] = entry.subentries.values()
    _assert_single_repointed_entity(hass, entry, agent.subentry_id)


async def test_v2_migration_rerun_reuses_subentry(
    hass: HomeAssistant, bedrock: FakeBedrock
) -> None:
    """If interrupted after the sub-entry was added, a re-run does not add another."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        options={"model": "x"},
        version=2,
        subentries_data=[
            ConfigSubentryData(
                data={"model": "x"}, subentry_type="conversation", title="A", unique_id=None
            )
        ],
    )
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "conversation", DOMAIN, entry.entry_id, config_entry=entry
    )
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.version == 3
    [agent] = entry.subentries.values()
    [entity] = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert entity.unique_id == agent.subentry_id


async def test_migration_keeps_custom_title(hass: HomeAssistant, bedrock: FakeBedrock) -> None:
    """A title the user chose is not replaced."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Asistente casa",
        data=PRODUCTION_V1_DATA,
        options=PRODUCTION_V1_OPTIONS,
        version=1,
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.title == "Asistente casa"
    assert entry.version == 3
    assert next(iter(entry.subentries.values())).title == "Asistente casa"


async def test_future_version_is_refused(hass: HomeAssistant, bedrock: FakeBedrock) -> None:
    """Entries from a newer release are not touched after a downgrade."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, version=4)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    assert entry.version == 4  # untouched


async def test_future_minor_version_is_refused(hass: HomeAssistant, bedrock: FakeBedrock) -> None:
    """A newer minor schema is not loaded or rewritten after a downgrade."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, version=3, minor_version=2)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    assert (entry.version, entry.minor_version) == (3, 2)


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (BedrockAuthError("ExpiredTokenException", "x"), ConfigEntryState.SETUP_ERROR),
        (BedrockAccessDeniedError("AccessDeniedException", "x"), ConfigEntryState.SETUP_ERROR),
        (BedrockConnectionError("EndpointConnectionError", "x"), ConfigEntryState.SETUP_RETRY),
    ],
)
async def test_setup_errors(
    hass: HomeAssistant, bedrock: FakeBedrock, error: Exception, state: ConfigEntryState
) -> None:
    """Bad credentials ask for reauth; network problems retry."""
    bedrock.validate_error = error
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, version=3)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is state
    reauth = [
        f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == "reauth"
    ]
    assert bool(reauth) == isinstance(error, BedrockAuthError)


async def test_api_registered_once_and_unregistered(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """The custom API has requires_admin (HA 2026.11) and is removed on unload."""
    api = next(a for a in llm.async_get_apis(hass) if a.id == HOME_LLM_API_ID)
    assert api.requires_admin is False  # 1.0.37 crashed here on HA 2026.11

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert HOME_LLM_API_ID not in {a.id for a in llm.async_get_apis(hass)}

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    assert HOME_LLM_API_ID in {a.id for a in llm.async_get_apis(hass)}
