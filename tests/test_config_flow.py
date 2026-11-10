"""Tests for the config, reauth, reconfigure and options flows."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
import probatio
import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.bedrock_conversation.client import (
    BedrockAccessDeniedError,
    BedrockAuthError,
    BedrockClient,
    BedrockConnectionError,
)
from custom_components.bedrock_conversation.config_flow import (
    BedrockConversationConfigFlow,
)
from custom_components.bedrock_conversation.const import (
    CONF_AWS_ACCESS_KEY_ID,
    CONF_AWS_REGION,
    CONF_AWS_SECRET_ACCESS_KEY,
    CONF_AWS_SESSION_TOKEN,
    CONF_CONFIRM_SENSITIVE_ACTIONS,
    CONF_COST_SENSOR_ENABLED,
    CONF_COST_TAG_KEY,
    CONF_COST_TAG_VALUE,
    CONF_LLM_HASS_API,
    CONF_MAX_TOKENS,
    CONF_MAX_TOOL_CALL_ITERATIONS,
    CONF_MODEL_ID,
    CONF_PROMPT,
    CONF_PROMPT_CACHING,
    CONF_TEMPERATURE,
    DEFAULT_COST_TAG_KEY,
    DEFAULT_COST_TAG_VALUE,
    DEFAULT_PROMPT,
    DOMAIN,
    HOME_LLM_API_ID,
    is_application_inference_profile_arn,
)

from .conftest import ENTRY_DATA, MODEL, FakeBedrock

CREDENTIALS = {
    CONF_AWS_REGION: "eu-west-3",
    CONF_AWS_ACCESS_KEY_ID: "AKIATEST",
    CONF_AWS_SECRET_ACCESS_KEY: "test-secret",
}
AIP_MODEL = (
    "arn:aws:bedrock:eu-west-3:123456789012:application-inference-profile/home-assistant-bedrock"
)


async def _set_application_profile(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    agent = next(iter(entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        entry, agent, data={**agent.data, CONF_MODEL_ID: AIP_MODEL}
    )
    await hass.async_block_till_done()


@pytest.fixture(autouse=True)
async def setup_llm(hass: HomeAssistant) -> None:
    """Register HA's LLM APIs so the API selector has options."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "llm", {})


async def test_user_flow_region_then_models(hass: HomeAssistant, bedrock: FakeBedrock) -> None:
    """Region + credentials, then the models of that region, then an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    regions = result["data_schema"].schema[CONF_AWS_REGION].config["options"]
    assert {"value": "eu-west-3", "label": "Europe (Paris) (eu-west-3)"} in regions

    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "model"
    assert result["errors"] == {}
    assert result["description_placeholders"] == {"region": "eu-west-3", "count": "2"}
    models = result["data_schema"].schema[CONF_MODEL_ID].config["options"]
    assert [m["value"] for m in models] == [MODEL, "amazon.nova-lite-v1:0"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODEL_ID: MODEL, CONF_LLM_HASS_API: [llm.LLM_API_ASSIST]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "AWS Bedrock (eu-west-3)"
    assert result["data"] == {**CREDENTIALS, CONF_AWS_SESSION_TOKEN: ""}
    assert result["options"] == {}
    assert result["version"] == 3
    entry = result["result"]
    [agent] = entry.subentries.values()
    assert agent.subentry_type == "conversation"
    assert agent.title == "AWS Bedrock"
    assert dict(agent.data) == {
        CONF_MODEL_ID: MODEL,
        CONF_LLM_HASS_API: [llm.LLM_API_ASSIST],
        CONF_PROMPT: DEFAULT_PROMPT,
    }


async def test_first_install_offers_custom_exposure_bounded_api(
    hass: HomeAssistant, bedrock: FakeBedrock
) -> None:
    """The custom API is selectable before any Bedrock entry has loaded."""
    assert not hass.config_entries.async_entries(DOMAIN)
    assert HOME_LLM_API_ID not in {api.id for api in llm.async_get_apis(hass)}

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    options = result["data_schema"].schema[CONF_LLM_HASS_API].config["options"]
    assert {option["value"] for option in options} == {
        llm.LLM_API_ASSIST,
        HOME_LLM_API_ID,
    }


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (BedrockAuthError("UnrecognizedClientException", "bad"), "invalid_auth"),
        (BedrockAccessDeniedError("AccessDeniedException", "no"), "access_denied"),
        (BedrockConnectionError("EndpointConnectionError", "offline"), "cannot_connect"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant, bedrock: FakeBedrock, error: Exception, key: str
) -> None:
    """Credential problems are shown on the form, which can be retried."""
    bedrock.validate_error = error
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": key}

    bedrock.validate_error = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["step_id"] == "model"


async def test_no_models_allows_typed_id(hass: HomeAssistant, bedrock: FakeBedrock) -> None:
    """If no model can be listed, a model id can still be typed."""
    bedrock.models = []
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["errors"] == {"base": "no_models"}
    assert result["data_schema"].schema[CONF_MODEL_ID].config["custom_value"] is True
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_MODEL_ID: "arn:aws:bedrock:eu-west-3:1:application-inference-profile/x"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_duplicate_aborts(hass: HomeAssistant, bedrock: FakeBedrock) -> None:
    """The same key in the same region cannot be added twice."""
    MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, version=3).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, bedrock: FakeBedrock
) -> None:
    """Reauth replaces the credentials and keeps the region."""
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    bedrock.validate_error = BedrockAuthError("InvalidSignatureException", "bad")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AWS_ACCESS_KEY_ID: "AKIANEW", CONF_AWS_SECRET_ACCESS_KEY: "x"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    bedrock.validate_error = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AWS_ACCESS_KEY_ID: "AKIANEW", CONF_AWS_SECRET_ACCESS_KEY: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_AWS_ACCESS_KEY_ID] == "AKIANEW"
    assert config_entry.data[CONF_AWS_SECRET_ACCESS_KEY] == "new"
    assert config_entry.data[CONF_AWS_REGION] == "eu-west-3"


async def test_reconfigure_region_and_model(
    hass: HomeAssistant, config_entry: MockConfigEntry, bedrock: FakeBedrock
) -> None:
    """Changing region re-validates and asks for a model of the new region."""
    result = await config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**CREDENTIALS, CONF_AWS_REGION: "us-east-1"}
    )
    assert result["step_id"] == "reconfigure_model"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODEL_ID: "amazon.nova-lite-v1:0"}
    )
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_AWS_REGION] == "us-east-1"
    assert config_entry.title == "AWS Bedrock (us-east-1)"
    [agent] = config_entry.subentries.values()
    assert agent.data[CONF_MODEL_ID] == "amazon.nova-lite-v1:0"  # set for every agent
    assert agent.data[CONF_LLM_HASS_API] == [HOME_LLM_API_ID]  # kept


AGENT_FORM = {
    CONF_MODEL_ID: "amazon.nova-lite-v1:0",
    CONF_PROMPT: "Habla español.",
    CONF_LLM_HASS_API: [],
    CONF_CONFIRM_SENSITIVE_ACTIONS: True,
    CONF_MAX_TOKENS: 512.0,
    CONF_TEMPERATURE: 0.2,
    CONF_MAX_TOOL_CALL_ITERATIONS: 4.0,
    CONF_PROMPT_CACHING: False,
}


async def test_reconfigure_agent(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Agent settings are merged (keys not on the form survive) and typed."""
    agent = next(iter(config_entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        config_entry, agent, data={**agent.data, "extra_attributes_to_expose": []}
    )
    await hass.async_block_till_done()
    result = await config_entry.start_subentry_reconfigure_flow(hass, agent.subentry_id)
    assert result["step_id"] == "set_options"
    assert "name" not in result["data_schema"].schema
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], AGENT_FORM)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    data = config_entry.subentries[agent.subentry_id].data
    assert data[CONF_MODEL_ID] == "amazon.nova-lite-v1:0"
    assert CONF_LLM_HASS_API not in data  # empty selection = chat only
    assert data[CONF_MAX_TOKENS] == 512
    assert isinstance(data[CONF_MAX_TOKENS], int)
    assert data[CONF_MAX_TOOL_CALL_ITERATIONS] == 4
    assert data["extra_attributes_to_expose"] == []  # not on the form, kept


async def test_add_second_agent(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """A second agent shares the credentials and gets its own entity."""
    result = await hass.config_entries.subentries.async_init(
        (config_entry.entry_id, "conversation"), context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "set_options"
    models = result["data_schema"].schema[CONF_MODEL_ID].config["options"]
    assert [m["value"] for m in models] == [MODEL, "amazon.nova-lite-v1:0"]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Rápido", **AGENT_FORM}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert sorted(s.title for s in config_entry.subentries.values()) == ["AWS Bedrock", "Rápido"]
    assert len(hass.states.async_entity_ids("conversation")) == 3  # 2 agents + HA's own


async def test_add_agent_needs_loaded_entry(hass: HomeAssistant, bedrock: FakeBedrock) -> None:
    """Agents cannot be added while the credentials do not work."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, version=3)
    entry.add_to_hass(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "conversation"), context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"


# --- Reload after reauth / reconfigure (HA 2026.11 update-listener rule) -----

UPDATE_LISTENER_WARNING = "has an update listener"


@pytest.fixture
def setup_calls(bedrock: FakeBedrock) -> Iterator[list[str]]:
    """Record each setup of an entry (BedrockClient.async_setup runs once per setup)."""
    calls: list[str] = []

    async def record(_client: BedrockClient) -> None:
        calls.append("setup")

    with patch.object(BedrockClient, "async_setup", record):
        yield calls


async def _add_second_agent(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    hass.config_entries.async_add_subentry(
        entry,
        config_entries.ConfigSubentry(
            data={CONF_MODEL_ID: MODEL, CONF_LLM_HASS_API: [HOME_LLM_API_ID]},
            subentry_type="conversation",
            title="Rápido",
            unique_id=None,
        ),
    )
    await hass.async_block_till_done()
    assert _agent_count(entry) == 2


def _agent_count(entry: MockConfigEntry) -> int:
    return sum(s.subentry_type == "conversation" for s in entry.subentries.values())


async def _reconfigure(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**CREDENTIALS, CONF_AWS_REGION: "us-east-1"}
    )
    assert result["step_id"] == "reconfigure_model"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODEL_ID: "amazon.nova-lite-v1:0"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()


async def test_reauth_reloads_once_without_update_listener_warning(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    setup_calls: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reauth on a loaded entry reloads it exactly once, via the update listener.

    HA 2026.11 warns (and 2026.12 breaks) when a flow schedules its own reload
    for an entry that has an update listener.
    """
    assert config_entry.state is ConfigEntryState.LOADED
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_AWS_ACCESS_KEY_ID: "AKIANEW", CONF_AWS_SECRET_ACCESS_KEY: "new"}
    )
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()

    assert UPDATE_LISTENER_WARNING not in caplog.text
    assert setup_calls == ["setup"]
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.data[CONF_AWS_ACCESS_KEY_ID] == "AKIANEW"


async def test_reconfigure_reloads_once_without_update_listener_warning(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    setup_calls: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Entry-level reconfigure on a loaded entry reloads it exactly once."""
    await _reconfigure(hass, config_entry)

    assert UPDATE_LISTENER_WARNING not in caplog.text
    assert setup_calls == ["setup"]
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.data[CONF_AWS_REGION] == "us-east-1"


async def test_reconfigure_two_agents_reloads_once(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    setup_calls: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With two agents, updating both sub-entries still reloads the entry once."""
    await _add_second_agent(hass, config_entry)
    setup_calls.clear()  # adding the agent reloads the entry; not part of the flow
    caplog.clear()

    await _reconfigure(hass, config_entry)

    assert UPDATE_LISTENER_WARNING not in caplog.text
    assert setup_calls == ["setup"]
    assert config_entry.state is ConfigEntryState.LOADED
    assert {s.data[CONF_MODEL_ID] for s in config_entry.subentries.values()} == {
        "amazon.nova-lite-v1:0"
    }
    assert len(hass.states.async_entity_ids("conversation")) == 3  # 2 agents + HA's own


async def test_reauth_after_failed_setup_reloads_entry(
    hass: HomeAssistant,
    bedrock: FakeBedrock,
    setup_calls: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reauth on an entry whose setup failed reloads it, so it ends up loaded.

    A failed setup leaves no update listener, so the flow must schedule the
    reload itself; saving the credentials alone would leave it in SETUP_ERROR.
    """
    assert await async_setup_component(hass, "conversation", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="AWS Bedrock (eu-west-3)",
        data=ENTRY_DATA,
        version=3,
        minor_version=1,
        subentries_data=[
            config_entries.ConfigSubentryData(
                data={CONF_MODEL_ID: MODEL, CONF_LLM_HASS_API: [HOME_LLM_API_ID]},
                subentry_type="conversation",
                title="AWS Bedrock",
                unique_id=None,
            )
        ],
    )
    entry.add_to_hass(hass)
    bedrock.validate_error = BedrockAuthError("UnrecognizedClientException", "expired")
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert not entry.update_listeners

    # The failed setup started a reauth flow; finish it with valid credentials.
    [flow] = entry.async_get_active_flows(hass, {config_entries.SOURCE_REAUTH})
    bedrock.validate_error = None
    setup_calls.clear()
    caplog.clear()
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_AWS_ACCESS_KEY_ID: "AKIANEW", CONF_AWS_SECRET_ACCESS_KEY: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()

    assert UPDATE_LISTENER_WARNING not in caplog.text
    assert setup_calls == ["setup"]
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_AWS_ACCESS_KEY_ID] == "AKIANEW"


async def test_reconfigure_same_region_keeps_agent_models(
    hass: HomeAssistant, config_entry: MockConfigEntry, bedrock: FakeBedrock
) -> None:
    """Rotating keys in the same region does not touch any agent's model."""
    agent = next(iter(config_entry.subentries.values()))
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**CREDENTIALS, CONF_AWS_ACCESS_KEY_ID: "AKIAROTATED"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert config_entry.data[CONF_AWS_ACCESS_KEY_ID] == "AKIAROTATED"
    assert config_entry.subentries[agent.subentry_id].data[CONF_MODEL_ID] == MODEL


async def test_only_exposure_bounded_apis_are_selectable(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Every selectable device-control API respects HA's Expose list."""
    result = await hass.config_entries.subentries.async_init(
        (config_entry.entry_id, "conversation"),
        context={"source": config_entries.SOURCE_USER},
    )
    selector = result["data_schema"].schema[CONF_LLM_HASS_API]
    assert {option["value"] for option in selector.config["options"]} == {
        llm.LLM_API_ASSIST,
        HOME_LLM_API_ID,
    }
    assert llm.LLM_API_HOME_ASSISTANT not in {
        option["value"] for option in selector.config["options"]
    }


async def test_reconfigure_enables_entry_level_cost_sensor(
    hass: HomeAssistant, config_entry: MockConfigEntry, bedrock: FakeBedrock
) -> None:
    """Cost Explorer is an opt-in credential-entry setting, not an agent option."""
    await _set_application_profile(hass, config_entry)
    result = await config_entry.start_reconfigure_flow(hass)
    assert CONF_COST_SENSOR_ENABLED in result["data_schema"].schema
    marker = next(
        field for field in result["data_schema"].schema if field == CONF_COST_SENSOR_ENABLED
    )
    assert marker.default() is False

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**CREDENTIALS, CONF_COST_SENSOR_ENABLED: True},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    assert config_entry.data[CONF_COST_SENSOR_ENABLED] is True
    assert config_entry.data[CONF_COST_TAG_KEY] == DEFAULT_COST_TAG_KEY
    assert config_entry.data[CONF_COST_TAG_VALUE] == DEFAULT_COST_TAG_VALUE
    assert config_entry.runtime_data.cost_coordinator is not None
    assert all(
        CONF_COST_SENSOR_ENABLED not in agent.data for agent in config_entry.subentries.values()
    )


async def test_new_entry_does_not_enable_cost_sensor_implicitly(
    hass: HomeAssistant, bedrock: FakeBedrock
) -> None:
    """The setup flow requires no billing permission and stores no opt-in."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert CONF_COST_SENSOR_ENABLED not in result["data_schema"].schema
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODEL_ID: MODEL}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_COST_SENSOR_ENABLED not in result["data"]


@pytest.mark.parametrize(
    ("initially_enabled", "submitted_enabled"),
    [(False, True), (True, False)],
)
async def test_failed_reconfigure_preserves_submitted_cost_sensor_choice(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    bedrock: FakeBedrock,
    initially_enabled: bool,
    submitted_enabled: bool,
) -> None:
    """A credential error keeps the submitted billing opt-in on the retry form."""
    await _set_application_profile(hass, config_entry)
    hass.config_entries.async_update_entry(
        config_entry,
        data={**config_entry.data, CONF_COST_SENSOR_ENABLED: initially_enabled},
    )
    await hass.async_block_till_done()
    bedrock.validate_error = BedrockAuthError("InvalidSignatureException", "bad")

    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**CREDENTIALS, CONF_COST_SENSOR_ENABLED: submitted_enabled},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    schema = result["data_schema"].schema
    cost_marker = next(field for field in schema if field == CONF_COST_SENSOR_ENABLED)
    assert cost_marker.default() is submitted_enabled
    for secret_name in (CONF_AWS_SECRET_ACCESS_KEY, CONF_AWS_SESSION_TOKEN):
        secret_marker = next(field for field in schema if field == secret_name)
        assert secret_marker.default is probatio.UNDEFINED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (CONF_COST_TAG_KEY, ""),
        (CONF_COST_TAG_KEY, "aws:Reserved"),
        (CONF_COST_TAG_KEY, "k" * 129),
        (CONF_COST_TAG_VALUE, ""),
        (CONF_COST_TAG_VALUE, "v" * 257),
        (CONF_COST_TAG_VALUE, "line\nbreak"),
    ],
)
async def test_cost_sensor_requires_valid_application_tag(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    bedrock: FakeBedrock,
    field: str,
    value: str,
) -> None:
    """An enabled sensor always requires an application tag filter."""
    await _set_application_profile(hass, config_entry)
    result = await config_entry.start_reconfigure_flow(hass)
    user_input = {
        **CREDENTIALS,
        CONF_COST_SENSOR_ENABLED: True,
        CONF_COST_TAG_KEY: DEFAULT_COST_TAG_KEY,
        CONF_COST_TAG_VALUE: DEFAULT_COST_TAG_VALUE,
        field: value,
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {field: "invalid_cost_tag"}
    # Failed validation keeps the entered toggle and the other tag field.
    enabled_marker = next(
        marker for marker in result["data_schema"].schema if marker == CONF_COST_SENSOR_ENABLED
    )
    assert enabled_marker.default() is True


async def test_cost_sensor_requires_all_agents_to_use_application_profiles(
    hass: HomeAssistant, config_entry: MockConfigEntry, bedrock: FakeBedrock
) -> None:
    """The app-filtered sensor cannot be enabled while an agent is untagged."""
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **CREDENTIALS,
            CONF_COST_SENSOR_ENABLED: True,
            CONF_COST_TAG_KEY: DEFAULT_COST_TAG_KEY,
            CONF_COST_TAG_VALUE: DEFAULT_COST_TAG_VALUE,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cost_requires_application_profiles"}
    assert CONF_COST_SENSOR_ENABLED not in config_entry.data


async def test_agent_model_must_remain_application_profile_while_cost_enabled(
    hass: HomeAssistant, config_entry: MockConfigEntry, bedrock: FakeBedrock
) -> None:
    """Adding or changing an agent cannot create untagged Bedrock spend."""
    await _set_application_profile(hass, config_entry)
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **CREDENTIALS,
            CONF_COST_SENSOR_ENABLED: True,
            CONF_COST_TAG_KEY: DEFAULT_COST_TAG_KEY,
            CONF_COST_TAG_VALUE: DEFAULT_COST_TAG_VALUE,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()

    agent = next(iter(config_entry.subentries.values()))
    result = await config_entry.start_subentry_reconfigure_flow(hass, agent.subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**AGENT_FORM, CONF_MODEL_ID: MODEL}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MODEL_ID: "application_profile_required"}

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**AGENT_FORM, CONF_MODEL_ID: AIP_MODEL}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"


@pytest.mark.parametrize(
    "model_id",
    [
        "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/Profile-1:prod.v2",
        "arn:aws-us-gov:bedrock:us-gov-west-1:123456789012:application-inference-profile/Profile-1",
        "arn:aws-cn:bedrock:cn-north-1:123456789012:application-inference-profile/Profile-1",
        "arn:aws-iso:bedrock:us-iso-east-1:123456789012:application-inference-profile/Profile-1",
        "arn:aws-iso-b:bedrock:us-isob-east-1:123456789012:application-inference-profile/Profile-1",
    ],
)
def test_accepts_canonical_application_inference_profile_arns(model_id: str) -> None:
    """Every Bedrock partition accepts its canonical account-owned profile ARN."""
    assert is_application_inference_profile_arn(model_id)


@pytest.mark.parametrize(
    "model_id",
    [
        "eu.anthropic.claude-opus-5-5",
        "arn:aws:bedrock:eu-west-3:123456789012:inference-profile/eu.claude",
        "arn:aws-invalid:bedrock:eu-west-3:123456789012:inference-profile/x",
        "arn:aws:bedrock::123456789012:inference-profile/x",
        "arn:aws:bedrock:EU_WEST_3:123456789012:inference-profile/x",
        "arn:aws-invalid:bedrock:eu-west-3:123456789012:application-inference-profile/x",
        "arn:aws:bedrock::123456789012:application-inference-profile/x",
        "arn:aws:bedrock:EU_WEST_3:123456789012:application-inference-profile/x",
        "arn:aws:bedrock:-:123456789012:application-inference-profile/x",
        "arn:aws:bedrock:123:123456789012:application-inference-profile/x",
        "arn:aws:bedrock:us--east-1:123456789012:application-inference-profile/x",
        "arn:aws:bedrock:cn-north-1:123456789012:application-inference-profile/x",
        "arn:aws-cn:bedrock:us-east-1:123456789012:application-inference-profile/x",
        "arn:aws-us-gov:bedrock:us-east-1:123456789012:application-inference-profile/x",
        "arn:aws-iso:bedrock:us-isob-east-1:123456789012:application-inference-profile/x",
        "arn:aws:bedrock:eu-west-3:123:application-inference-profile/x",
        "arn:aws:bedrock:eu-west-3:123456789012:foundation-model/x",
        "arn:aws:bedrock:eu-west-3:123456789012:application-inference-profile/",
        "arn:aws:bedrock:eu-west-3:123456789012:application-inference-profile/bad_id",
        None,
    ],
)
def test_rejects_noncanonical_application_inference_profile_arns(model_id: object) -> None:
    """Malformed or non-account-owned model identifiers never qualify for cost tracking."""
    assert not is_application_inference_profile_arn(model_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param(CONF_COST_TAG_KEY, 123, id="non-string-key"),
        pytest.param(CONF_COST_TAG_VALUE, ["value"], id="non-string-value"),
        pytest.param(CONF_COST_TAG_KEY, "key\x7f", id="del-in-key"),
        pytest.param(CONF_COST_TAG_VALUE, "value\x7f", id="del-in-value"),
        pytest.param(CONF_COST_TAG_KEY, "key\x80", id="c1-start-in-key"),
        pytest.param(CONF_COST_TAG_VALUE, "value\x80", id="c1-start-in-value"),
        pytest.param(CONF_COST_TAG_KEY, "key\x9f", id="c1-end-in-key"),
        pytest.param(CONF_COST_TAG_VALUE, "value\x9f", id="c1-end-in-value"),
    ],
)
async def test_reconfigure_rejects_malformed_cost_tags_without_coercion(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    bedrock: FakeBedrock,
    field: str,
    value: object,
) -> None:
    """Form tags must be strings without DEL or C1 control characters."""
    flow = BedrockConversationConfigFlow()
    flow.hass = hass
    flow.context = {
        "entry_id": config_entry.entry_id,
        "source": config_entries.SOURCE_RECONFIGURE,
    }
    user_input = {
        **CREDENTIALS,
        CONF_COST_SENSOR_ENABLED: True,
        CONF_COST_TAG_KEY: DEFAULT_COST_TAG_KEY,
        CONF_COST_TAG_VALUE: DEFAULT_COST_TAG_VALUE,
        field: value,
    }
    with patch(
        "custom_components.bedrock_conversation.config_flow.is_application_inference_profile_arn",
        return_value=True,
    ):
        result = await flow.async_step_reconfigure(user_input)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {field: "invalid_cost_tag"}
    assert CONF_COST_SENSOR_ENABLED not in config_entry.data


async def test_invalid_tag_retry_retains_non_secret_fields_only(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    bedrock: FakeBedrock,
) -> None:
    """A tag error preserves retryable fields but clears submitted secrets."""
    result = await config_entry.start_reconfigure_flow(hass)
    submitted = {
        **CREDENTIALS,
        CONF_AWS_REGION: "us-east-1",
        CONF_AWS_ACCESS_KEY_ID: "AKIARETRY",
        CONF_AWS_SECRET_ACCESS_KEY: "new-secret",
        CONF_AWS_SESSION_TOKEN: "temporary-token",
        CONF_COST_SENSOR_ENABLED: True,
        CONF_COST_TAG_KEY: "",
        CONF_COST_TAG_VALUE: DEFAULT_COST_TAG_VALUE,
    }
    with patch(
        "custom_components.bedrock_conversation.config_flow.is_application_inference_profile_arn",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], submitted)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_COST_TAG_KEY: "invalid_cost_tag"}
    schema = result["data_schema"].schema
    region_marker = next(marker for marker in schema if marker == CONF_AWS_REGION)
    access_key_marker = next(marker for marker in schema if marker == CONF_AWS_ACCESS_KEY_ID)
    assert region_marker.default() == "us-east-1"
    assert access_key_marker.default() == "AKIARETRY"
    for secret_name in (CONF_AWS_SECRET_ACCESS_KEY, CONF_AWS_SESSION_TOKEN):
        secret_marker = next(marker for marker in schema if marker == secret_name)
        assert secret_marker.default is probatio.UNDEFINED


async def test_region_change_enabling_cost_rejects_base_model(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    bedrock: FakeBedrock,
) -> None:
    """Enabling cost tracking during a region change requires the replacement profile."""
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **CREDENTIALS,
            CONF_AWS_REGION: "us-east-1",
            CONF_COST_SENSOR_ENABLED: True,
            CONF_COST_TAG_KEY: DEFAULT_COST_TAG_KEY,
            CONF_COST_TAG_VALUE: DEFAULT_COST_TAG_VALUE,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure_model"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODEL_ID: "amazon.nova-lite-v1:0"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MODEL_ID: "application_profile_required"}


async def test_region_change_enabling_cost_accepts_application_profile(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    bedrock: FakeBedrock,
) -> None:
    """Enabling cost tracking accepts a replacement application profile."""
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **CREDENTIALS,
            CONF_AWS_REGION: "us-east-1",
            CONF_COST_SENSOR_ENABLED: True,
            CONF_COST_TAG_KEY: DEFAULT_COST_TAG_KEY,
            CONF_COST_TAG_VALUE: DEFAULT_COST_TAG_VALUE,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure_model"

    replacement = (
        "arn:aws:bedrock:us-east-1:123456789012:"
        "application-inference-profile/home-assistant-bedrock"
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODEL_ID: replacement}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_COST_SENSOR_ENABLED] is True
    assert next(iter(config_entry.subentries.values())).data[CONF_MODEL_ID] == replacement


async def test_region_change_disabling_cost_accepts_base_model(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    bedrock: FakeBedrock,
) -> None:
    """Disabling cost tracking permits a replacement base model in the new region."""
    await _set_application_profile(hass, config_entry)
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            CONF_COST_SENSOR_ENABLED: True,
            CONF_COST_TAG_KEY: DEFAULT_COST_TAG_KEY,
            CONF_COST_TAG_VALUE: DEFAULT_COST_TAG_VALUE,
        },
    )
    await hass.async_block_till_done()

    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **CREDENTIALS,
            CONF_AWS_REGION: "us-east-1",
            CONF_COST_SENSOR_ENABLED: False,
            CONF_COST_TAG_KEY: DEFAULT_COST_TAG_KEY,
            CONF_COST_TAG_VALUE: DEFAULT_COST_TAG_VALUE,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure_model"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODEL_ID: "amazon.nova-lite-v1:0"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_COST_SENSOR_ENABLED] is False
    assert (
        next(iter(config_entry.subentries.values())).data[CONF_MODEL_ID] == "amazon.nova-lite-v1:0"
    )
