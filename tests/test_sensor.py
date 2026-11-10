"""Tests for the optional AWS Cost Explorer monetary sensor."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from custom_components.bedrock_conversation.client import (
    BedrockAccessDeniedError,
    BedrockAuthError,
)
from custom_components.bedrock_conversation.const import (
    CONF_AWS_REGION,
    CONF_COST_SENSOR_ENABLED,
    CONF_COST_TAG_KEY,
    CONF_COST_TAG_VALUE,
    COST_EXPLORER_METRIC,
    COST_EXPLORER_UPDATE_INTERVAL,
    COST_SENSOR_KEY,
    DEFAULT_COST_TAG_KEY,
    DEFAULT_COST_TAG_VALUE,
    DOMAIN,
    SUBENTRY_CONVERSATION,
)

from .conftest import ENTRY_DATA, MODEL, FakeBedrock

AIP_MODEL = (
    "arn:aws:bedrock:eu-west-3:123456789012:application-inference-profile/home-assistant-bedrock"
)


async def _setup_entry(
    hass: HomeAssistant,
    bedrock: FakeBedrock,
    *,
    enabled: bool,
    region: str = "eu-west-3",
    tag_key: object = DEFAULT_COST_TAG_KEY,
    tag_value: object = DEFAULT_COST_TAG_VALUE,
    model: str = AIP_MODEL,
) -> MockConfigEntry:
    for component in ("homeassistant", "conversation", "intent", "llm"):
        assert await async_setup_component(hass, component, {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"AWS Bedrock ({region})",
        data={
            **ENTRY_DATA,
            CONF_AWS_REGION: region,
            CONF_COST_SENSOR_ENABLED: enabled,
            CONF_COST_TAG_KEY: tag_key,
            CONF_COST_TAG_VALUE: tag_value,
        },
        version=3,
        minor_version=1,
        subentries_data=[
            ConfigSubentryData(
                data={"model": model},
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


def _sensor_id(hass: HomeAssistant, entry: MockConfigEntry) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{COST_SENSOR_KEY}"
    )


async def test_cost_sensor_state_and_metadata(hass: HomeAssistant, bedrock: FakeBedrock) -> None:
    """The sensor uses HA's monetary total contract and AWS billing metadata."""
    entry = await _setup_entry(hass, bedrock, enabled=True)
    coordinator = entry.runtime_data.cost_coordinator
    assert coordinator is not None
    assert coordinator.update_interval == COST_EXPLORER_UPDATE_INTERVAL
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    entity_id = _sensor_id(hass, entry)
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "1.234567"
    assert state.attributes["device_class"] == SensorDeviceClass.MONETARY
    assert state.attributes["state_class"] == SensorStateClass.TOTAL
    assert state.attributes["unit_of_measurement"] == "USD"
    assert state.attributes["last_reset"] == "2026-11-01T00:00:00+00:00"
    assert state.attributes["estimated"] is True
    assert state.attributes["billing_period_start"] == "2026-11-01"
    assert state.attributes["billing_period_end_exclusive"] == "2026-11-11"
    assert state.attributes["last_updated"] == "2026-11-10T12:00:00+00:00"
    assert state.attributes["metric"] == COST_EXPLORER_METRIC
    assert state.attributes["cost_allocation_tag_key"] == DEFAULT_COST_TAG_KEY
    assert state.attributes["cost_allocation_tag_value"] == DEFAULT_COST_TAG_VALUE
    assert state.attributes["scope"] == "application_tag_all_regions"

    registry_entry = er.async_get(hass).async_get(entity_id)
    assert registry_entry is not None
    device = dr.async_get(hass).async_get(registry_entry.device_id)
    assert device is not None
    assert device.identifiers == {(DOMAIN, entry.entry_id)}
    assert device.config_entry_id == entry.entry_id
    assert device.config_subentry_id is None

    today = datetime.now(UTC).date()
    assert bedrock.cost_calls[-1] == (
        date(today.year, today.month, 1),
        today + timedelta(days=1),
        DEFAULT_COST_TAG_KEY,
        DEFAULT_COST_TAG_VALUE,
    )


async def test_cost_error_only_makes_sensor_unavailable(
    hass: HomeAssistant, bedrock: FakeBedrock
) -> None:
    """Missing ce:GetCostAndUsage never takes down conversation agents."""
    entry = await _setup_entry(hass, bedrock, enabled=True)
    coordinator = entry.runtime_data.cost_coordinator
    assert coordinator is not None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    sensor_id = _sensor_id(hass, entry)
    assert sensor_id is not None

    bedrock.cost_error = BedrockAccessDeniedError(
        "AccessDeniedException", "principal 123456789012 is denied"
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get(sensor_id)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
    [conversation_entity] = [
        state
        for state in hass.states.async_all("conversation")
        if state.entity_id != "conversation.home_assistant"
    ]
    assert conversation_entity.state != STATE_UNAVAILABLE

    bedrock.cost_error = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(sensor_id)
    assert state is not None
    assert state.state == "1.234567"


async def test_cost_sensor_disabled_by_default(hass: HomeAssistant, bedrock: FakeBedrock) -> None:
    """Existing entries create no sensor and make no billing request."""
    entry = await _setup_entry(hass, bedrock, enabled=False)
    assert entry.runtime_data.cost_coordinator is None
    assert _sensor_id(hass, entry) is None
    assert bedrock.cost_calls == []


async def test_manual_entity_update_refreshes_cost(
    hass: HomeAssistant, bedrock: FakeBedrock
) -> None:
    """HA's update_entity service requests a coordinator refresh."""
    entry = await _setup_entry(hass, bedrock, enabled=True)
    coordinator = entry.runtime_data.cost_coordinator
    assert coordinator is not None
    await coordinator.async_refresh()
    sensor_id = _sensor_id(hass, entry)
    assert sensor_id is not None
    previous_calls = len(bedrock.cost_calls)

    await hass.services.async_call(
        "homeassistant",
        "update_entity",
        {"entity_id": sensor_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert len(bedrock.cost_calls) == previous_calls + 1


async def test_initial_cost_failure_does_not_block_entry(
    hass: HomeAssistant, bedrock: FakeBedrock
) -> None:
    """A billing permission error at startup leaves conversation setup loaded."""
    bedrock.cost_error = BedrockAccessDeniedError(
        "AccessDeniedException", "principal 123456789012 is denied"
    )
    entry = await _setup_entry(hass, bedrock, enabled=True)
    coordinator = entry.runtime_data.cost_coordinator
    assert coordinator is not None
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert entry.state.name == "LOADED"
    sensor_id = _sensor_id(hass, entry)
    assert sensor_id is not None
    state = hass.states.get(sensor_id)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
    assert any(
        state.entity_id.startswith("conversation.")
        and state.entity_id != "conversation.home_assistant"
        and state.state != STATE_UNAVAILABLE
        for state in hass.states.async_all("conversation")
    )


@pytest.mark.parametrize(
    ("cost_error", "expected_state"),
    [
        pytest.param(None, "1.234567", id="success"),
        pytest.param(
            BedrockAccessDeniedError("AccessDeniedException", "denied"),
            STATE_UNAVAILABLE,
            id="failure",
        ),
    ],
)
async def test_initial_cost_refresh_runs_in_background_and_publishes_result(
    hass: HomeAssistant,
    bedrock: FakeBedrock,
    cost_error: Exception | None,
    expected_state: str,
) -> None:
    """The real startup refresh runs while setup stays loaded, then publishes its outcome."""
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def gated_cost(
        period_start: date,
        period_end: date,
        tag_key: str,
        tag_value: str,
    ):
        bedrock.cost_calls.append((period_start, period_end, tag_key, tag_value))
        started.set()
        try:
            await release.wait()
            if cost_error is not None:
                raise cost_error
            return bedrock.cost_data
        finally:
            asyncio.get_running_loop().call_soon(completed.set)

    bedrock.get_cost = gated_cost
    entry = await _setup_entry(hass, bedrock, enabled=True)
    async with asyncio.timeout(1):
        await started.wait()

    assert entry.state.name == "LOADED"
    sensor_id = _sensor_id(hass, entry)
    assert sensor_id is not None
    state = hass.states.get(sensor_id)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE

    release.set()
    async with asyncio.timeout(1):
        await completed.wait()
    await hass.async_block_till_done()

    state = hass.states.get(sensor_id)
    assert state is not None
    assert state.state == expected_state
    assert entry.state.name == "LOADED"


async def test_unload_cancels_blocked_initial_cost_refresh(
    hass: HomeAssistant, bedrock: FakeBedrock
) -> None:
    """An entry-owned initial billing refresh cannot survive entry unload."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_cost(
        period_start: date,
        period_end: date,
        tag_key: str,
        tag_value: str,
    ):
        bedrock.cost_calls.append((period_start, period_end, tag_key, tag_value))
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    bedrock.get_cost = blocked_cost
    entry = await _setup_entry(hass, bedrock, enabled=True)
    async with asyncio.timeout(1):
        await started.wait()
    assert entry.state.name == "LOADED"

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert cancelled.is_set()
    assert entry.state.name == "NOT_LOADED"


async def test_billing_auth_failure_starts_reauth_without_unloading_conversation(
    hass: HomeAssistant, bedrock: FakeBedrock
) -> None:
    """Expired billing credentials start reauth while chat remains loaded."""
    entry = await _setup_entry(hass, bedrock, enabled=True)
    coordinator = entry.runtime_data.cost_coordinator
    assert coordinator is not None
    await coordinator.async_refresh()
    sensor_id = _sensor_id(hass, entry)
    assert sensor_id is not None

    bedrock.cost_error = BedrockAuthError("ExpiredTokenException", "expired")
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert entry.state.name == "LOADED"
    state = hass.states.get(sensor_id)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE
    [reauth] = entry.async_get_active_flows(hass, {config_entries.SOURCE_REAUTH})
    assert reauth["context"]["source"] == config_entries.SOURCE_REAUTH
    assert any(
        state.entity_id.startswith("conversation.")
        and state.entity_id != "conversation.home_assistant"
        and state.state != STATE_UNAVAILABLE
        for state in hass.states.async_all("conversation")
    )


async def test_same_key_and_tag_across_regions_share_cost_request(
    hass: HomeAssistant, bedrock: FakeBedrock
) -> None:
    """One application tag is fetched once for entries sharing an access key."""
    started = asyncio.Event()
    release = asyncio.Event()
    all_completed = asyncio.Event()
    completed_count = 0

    async def gated_cost(
        period_start: date,
        period_end: date,
        tag_key: str,
        tag_value: str,
    ):
        nonlocal completed_count
        bedrock.cost_calls.append((period_start, period_end, tag_key, tag_value))
        started.set()
        try:
            await release.wait()
            return bedrock.cost_data
        finally:
            completed_count += 1
            if completed_count == len(bedrock.cost_calls):
                asyncio.get_running_loop().call_soon(all_completed.set)

    bedrock.get_cost = gated_cost
    first = await _setup_entry(hass, bedrock, enabled=True, region="eu-west-3")
    async with asyncio.timeout(1):
        await started.wait()
    second = await _setup_entry(hass, bedrock, enabled=True, region="us-east-1")
    startup_calls = len(bedrock.cost_calls)

    release.set()
    async with asyncio.timeout(1):
        await all_completed.wait()
    await hass.async_block_till_done()
    for entry in (first, second):
        sensor_id = _sensor_id(hass, entry)
        assert sensor_id is not None
        state = hass.states.get(sensor_id)
        assert state is not None
        assert state.state == "1.234567"

    bedrock.cost_calls.clear()
    async_fire_time_changed(
        hass,
        datetime.now(UTC) + COST_EXPLORER_UPDATE_INTERVAL + timedelta(seconds=1),
    )
    await hass.async_block_till_done()

    assert (startup_calls, len(bedrock.cost_calls)) == (1, 1)


async def test_different_application_tags_do_not_share_cost_request(
    hass: HomeAssistant, bedrock: FakeBedrock
) -> None:
    """Different application filters stay isolated even with one access key."""
    first = await _setup_entry(
        hass, bedrock, enabled=True, tag_value="HomeAssistantBedrockConversation"
    )
    second = await _setup_entry(hass, bedrock, enabled=True, tag_value="AnotherApplication")
    first_coordinator = first.runtime_data.cost_coordinator
    second_coordinator = second.runtime_data.cost_coordinator
    assert first_coordinator is not None
    assert second_coordinator is not None
    assert first_coordinator is not second_coordinator

    await first_coordinator.async_refresh()
    await second_coordinator.async_refresh()

    assert bedrock.cost_calls[-2][2:] == (
        DEFAULT_COST_TAG_KEY,
        "HomeAssistantBedrockConversation",
    )
    assert bedrock.cost_calls[-1][2:] == (
        DEFAULT_COST_TAG_KEY,
        "AnotherApplication",
    )


async def test_different_cost_coordinators_serialize_provider_requests(
    hass: HomeAssistant, bedrock: FakeBedrock
) -> None:
    """Cost Explorer requests share one slot across isolated coordinators."""
    first = await _setup_entry(
        hass, bedrock, enabled=True, tag_value="HomeAssistantBedrockConversation"
    )
    second = await _setup_entry(hass, bedrock, enabled=True, tag_value="AnotherApplication")
    first_coordinator = first.runtime_data.cost_coordinator
    second_coordinator = second.runtime_data.cost_coordinator
    assert first_coordinator is not None
    assert second_coordinator is not None
    assert first_coordinator is not second_coordinator

    first_started = asyncio.Event()
    release = asyncio.Event()
    active_requests = 0
    peak_active_requests = 0

    async def gated_cost(
        period_start: date,
        period_end: date,
        tag_key: str,
        tag_value: str,
    ):
        nonlocal active_requests, peak_active_requests
        bedrock.cost_calls.append((period_start, period_end, tag_key, tag_value))
        active_requests += 1
        peak_active_requests = max(peak_active_requests, active_requests)
        first_started.set()
        try:
            await release.wait()
            return bedrock.cost_data
        finally:
            active_requests -= 1

    bedrock.get_cost = gated_cost
    bedrock.cost_calls.clear()
    first_refresh = asyncio.create_task(first_coordinator.async_refresh())
    async with asyncio.timeout(1):
        await first_started.wait()
    second_refresh = asyncio.create_task(second_coordinator.async_refresh())
    await asyncio.sleep(0)

    assert len(bedrock.cost_calls) == 1
    release.set()
    await asyncio.gather(first_refresh, second_refresh)

    assert len(bedrock.cost_calls) == 2
    assert peak_active_requests == 1


async def test_enabled_sensor_requires_application_profiles_at_runtime(
    hass: HomeAssistant, bedrock: FakeBedrock, caplog: pytest.LogCaptureFixture
) -> None:
    """Legacy configurations without tags cannot silently omit HA usage."""
    entry = await _setup_entry(hass, bedrock, enabled=True, model=MODEL)
    assert entry.runtime_data.cost_coordinator is None
    assert _sensor_id(hass, entry) is None
    assert "every agent must use an application inference profile ARN" in caplog.text
    assert bedrock.cost_calls == []


@pytest.mark.parametrize(
    ("tag_key", "tag_value"),
    [
        pytest.param(["Application"], DEFAULT_COST_TAG_VALUE, id="unhashable-key"),
        pytest.param(DEFAULT_COST_TAG_KEY, ["HomeAssistant"], id="unhashable-value"),
        pytest.param("", DEFAULT_COST_TAG_VALUE, id="empty-key"),
        pytest.param("aws:Reserved", DEFAULT_COST_TAG_VALUE, id="reserved-key"),
        pytest.param("Application\x7f", DEFAULT_COST_TAG_VALUE, id="del-in-key"),
        pytest.param(DEFAULT_COST_TAG_KEY, "HomeAssistant\x80", id="c1-in-value"),
    ],
)
async def test_malformed_stored_cost_tags_omit_sensor_without_billing_query(
    hass: HomeAssistant,
    bedrock: FakeBedrock,
    tag_key: object,
    tag_value: object,
) -> None:
    """Malformed manually stored tags cannot crash setup or broaden the cost query."""
    with patch(
        "custom_components.bedrock_conversation.is_application_inference_profile_arn",
        return_value=True,
    ):
        entry = await _setup_entry(
            hass,
            bedrock,
            enabled=True,
            tag_key=tag_key,
            tag_value=tag_value,
        )

    assert entry.state.name == "LOADED"
    assert entry.runtime_data.cost_coordinator is None
    assert _sensor_id(hass, entry) is None
    assert bedrock.cost_calls == []
