"""The AWS Bedrock Conversation integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er, llm
from homeassistant.util.hass_dict import HassKey

from .client import (
    BedrockAccessDeniedError,
    BedrockAuthError,
    BedrockClient,
    BedrockError,
)
from .const import (
    CONF_AWS_REGION,
    CONF_AWS_SESSION_TOKEN,
    CONF_LLM_HASS_API,
    CONF_MAX_TOKENS,
    CONF_MAX_TOOL_CALL_ITERATIONS,
    CONF_PROMPT,
    CONF_TEMPERATURE,
    DEFAULT_AWS_REGION,
    DOMAIN,
    HOME_LLM_API_ID,
    LOGGER,
    SUBENTRY_CONVERSATION,
    default_title,
    exposure_bounded_llm_api_ids,
    is_default_title,
)
from .llm_api import BedrockServicesAPI

PLATFORMS = [Platform.CONVERSATION]

# Unregisters the shared "AWS Bedrock Services" API when the last entry unloads.
_API_UNREGISTER: HassKey[Callable[[], None]] = HassKey(f"{DOMAIN}_api_unregister")
# Entry ids with a reload already scheduled by the update listener.
_RELOAD_PENDING: HassKey[set[str]] = HassKey(f"{DOMAIN}_reload_pending")


@dataclass
class BedrockRuntimeData:
    """Runtime data for a config entry."""

    client: BedrockClient


type BedrockConfigEntry = ConfigEntry[BedrockRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: BedrockConfigEntry) -> None:
    """Set up AWS Bedrock Conversation from a config entry."""
    # Before any entity is created: repair registries an unclean stop left
    # behind after the v3 migration.
    _repoint_legacy_registry_entries(hass, entry)
    client = BedrockClient(hass, entry.data)
    # Only the AWS error code is shown in the UI: the message can contain the
    # account id and IAM principal. The full error is logged at debug level.
    try:
        await client.async_setup()
        await client.async_validate()
    except BedrockAuthError as err:
        LOGGER.debug("Credential check failed: %s", err)
        raise ConfigEntryAuthFailed(err.code) from err
    except BedrockAccessDeniedError as err:
        LOGGER.error("AWS denied listing Bedrock models: %s", err)
        raise ConfigEntryError(err.code) from err
    except BedrockError as err:
        LOGGER.debug("Bedrock not reachable: %s", err)
        raise ConfigEntryNotReady(err.code) from err

    entry.runtime_data = BedrockRuntimeData(client=client)

    if _API_UNREGISTER not in hass.data:
        hass.data[_API_UNREGISTER] = llm.async_register_api(hass, BedrockServicesAPI(hass))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Adding, changing or removing an agent (sub-entry) reloads the entry.
    entry.async_on_unload(entry.add_update_listener(_async_reload))


async def _async_reload(hass: HomeAssistant, entry: BedrockConfigEntry) -> None:
    """Reload the entry once for a batch of updates.

    A flow step can update several sub-entries and the entry at once; each
    update calls this listener. The first call waits one loop turn so the
    others (started in the same step) find it pending and skip. Updates made
    after the wait start a new reload, so none is lost.
    """
    pending = hass.data.setdefault(_RELOAD_PENDING, set())
    if entry.entry_id in pending:
        return
    pending.add(entry.entry_id)
    try:
        await asyncio.sleep(0)
    finally:
        pending.discard(entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: BedrockConfigEntry) -> None:
    """Unload a config entry."""
    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)
    others_loaded = any(
        other.entry_id != entry.entry_id
        for other in hass.config_entries.async_loaded_entries(DOMAIN)
    )
    if not others_loaded and (unregister := hass.data.pop(_API_UNREGISTER, None)):
        unregister()


# --- Migration ---------------------------------------------------------------

# Options from 1.x that no longer exist: HA's chat log now handles memory and
# the system prompt, and top_p/top_k were never sent to Claude models.
_REMOVED_OPTIONS = (
    "top_p",
    "top_k",
    "refresh_prompt_per_turn",
    "refresh_system_prompt",
    "remember_conversation",
    "remember_num_interactions",
    "selected_language",
    "request_timeout",
)
_LEGACY_API_IDS = {"bedrock-service-api": HOME_LLM_API_ID}
# 1.x expanded these placeholders itself. HA now adds the date and the
# selected API adds the device list, so they are removed from the prompt.
_LEGACY_PLACEHOLDERS = ("<current_date>", "<devices>")
_LEGACY_PERSONA = "<persona>"
_TEMPLATE_SYNTAX = re.compile(r"\{\{|\{%|\{#")
_RAW_START, _RAW_END = "{% raw %}", "{% endraw %}"
_LITERAL_TEMPLATE_PREFIX, _LITERAL_TEMPLATE_SUFFIX = "{{ ", " }}"


def _literal_prompt_template(prompt: str) -> str:
    """Return a template expression that renders ``prompt`` literally."""
    return f"{_LITERAL_TEMPLATE_PREFIX}{json.dumps(prompt, ensure_ascii=False)}{_LITERAL_TEMPLATE_SUFFIX}"


def _is_literal_prompt_template(prompt: str) -> bool:
    """Return whether ``prompt`` is the canonical literal expression above."""
    if not (
        prompt.startswith(_LITERAL_TEMPLATE_PREFIX) and prompt.endswith(_LITERAL_TEMPLATE_SUFFIX)
    ):
        return False
    try:
        literal = json.loads(prompt[len(_LITERAL_TEMPLATE_PREFIX) : -len(_LITERAL_TEMPLATE_SUFFIX)])
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(literal, str) and prompt == _literal_prompt_template(literal)


def _sanitize_agent_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Remove LLM APIs that do not enforce per-entity exposure."""
    sanitized = dict(options)
    if CONF_LLM_HASS_API in sanitized:
        sanitized[CONF_LLM_HASS_API] = exposure_bounded_llm_api_ids(sanitized[CONF_LLM_HASS_API])
    return sanitized


def migrate_v1_to_v2(
    data: Mapping[str, Any], options: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return v2 data and options for a v1 entry.

    Pure and idempotent. Unknown keys are kept; nothing the user chose is
    replaced by a default.
    """
    data, options = dict(data), dict(options)

    # Region: the options flow used to store it in options; it now lives in
    # data next to the credentials it was validated with.
    data[CONF_AWS_REGION] = (
        options.pop(CONF_AWS_REGION, None) or data.get(CONF_AWS_REGION) or DEFAULT_AWS_REGION
    )
    # 1.x never read a session token from options, so one stored there is
    # stale; copying it into data would pair it with permanent keys and AWS
    # would reject the request. Discard it and keep data's own token.
    options.pop(CONF_AWS_SESSION_TOKEN, None)

    for key in _REMOVED_OPTIONS:
        options.pop(key, None)

    if CONF_LLM_HASS_API in options:
        api = options[CONF_LLM_HASS_API]
        apis = [api] if isinstance(api, str) else api if isinstance(api, (list, tuple)) else []
        options[CONF_LLM_HASS_API] = exposure_bounded_llm_api_ids(
            [_LEGACY_API_IDS.get(a, a) for a in apis if isinstance(a, str)]
        )

    # Number selectors stored floats; slices and API fields need ints.
    for key in (CONF_MAX_TOKENS, CONF_MAX_TOOL_CALL_ITERATIONS):
        if options.get(key) is not None:
            options[key] = int(float(options[key]))
    if options.get(CONF_TEMPERATURE) is not None:
        options[CONF_TEMPERATURE] = float(options[CONF_TEMPERATURE])

    if isinstance(prompt := options.get(CONF_PROMPT), str):
        options[CONF_PROMPT] = _migrate_prompt(prompt)
        if not options[CONF_PROMPT]:
            options.pop(CONF_PROMPT)  # fall back to HA's default instructions

    return data, options


def _migrate_prompt(prompt: str) -> str:
    """Remove 1.x placeholders and keep the text literal.

    1.x sent the prompt as plain text; HA now renders it as a template. Text
    with template syntax is wrapped in a raw block so it stays literal. Raw
    block markers themselves need a quoted expression because they would
    otherwise change or break the surrounding template.
    """
    for placeholder in (*_LEGACY_PLACEHOLDERS, _LEGACY_PERSONA):
        prompt = prompt.replace(placeholder, "")
    prompt = prompt.strip()
    if _is_literal_prompt_template(prompt):
        return prompt
    if _RAW_START in prompt or _RAW_END in prompt:
        # Keep the raw wrapper emitted by older migration runs idempotent.
        if (
            prompt.startswith(_RAW_START)
            and prompt.endswith(_RAW_END)
            and prompt.count(_RAW_START) == prompt.count(_RAW_END) == 1
        ):
            return prompt
        return _literal_prompt_template(prompt)
    if _TEMPLATE_SYNTAX.search(prompt):
        prompt = f"{_RAW_START}{prompt}{_RAW_END}"
    return prompt


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Migrate old config entries."""
    if (entry.version, entry.minor_version) > (3, 1):
        # Downgrade from a future version: refuse rather than corrupt it.
        raise ConfigEntryError(
            f"Entry version {entry.version}.{entry.minor_version} is newer than this release"
        )
    if entry.version == 1:
        data, options = migrate_v1_to_v2(entry.data, entry.options)
        # Keep a title the user chose; refresh the automatic one (its region
        # was the setup region, not the one in use).
        title = entry.title
        if is_default_title(title):
            title = default_title(data[CONF_AWS_REGION])
        hass.config_entries.async_update_entry(
            entry, data=data, options=options, title=title, version=2, minor_version=1
        )
        LOGGER.info("Migrated %s to version 2.1", entry.entry_id)
    if entry.version == 2:
        _migrate_v2_to_v3(hass, entry)
        LOGGER.info("Migrated %s to version 3.1 (agent moved to a sub-entry)", entry.entry_id)


def _migrate_v2_to_v3(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Move the agent settings from the entry options into a sub-entry.

    The conversation entity and its device are re-pointed to the sub-entry and
    keep their entity id, so voice assistants using the agent keep working.
    Safe to re-run if interrupted: an existing sub-entry is reused, and an
    entity already re-pointed no longer has the entry id as unique id, so it
    is left alone. Setup repeats the re-point in case the registries were not
    saved (see `_repoint_legacy_registry_entries`).
    """
    subentry = next(
        (s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_CONVERSATION),
        None,
    )
    if subentry is None:
        subentry = ConfigSubentry(
            data=_sanitize_agent_options(entry.options),
            subentry_type=SUBENTRY_CONVERSATION,
            title=entry.title,
            unique_id=None,
        )
        hass.config_entries.async_add_subentry(entry, subentry)
    else:
        sanitized = _sanitize_agent_options(subentry.data)
        if sanitized != dict(subentry.data):
            hass.config_entries.async_update_subentry(entry, subentry, data=sanitized)

    _repoint_legacy_registry_entries(hass, entry)
    hass.config_entries.async_update_entry(entry, options={}, version=3, minor_version=1)


def _repoint_legacy_registry_entries(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Move the 1.x/2.x entity and device (keyed by entry id) to the agent sub-entry.

    Called by the v2→v3 migration and again on every setup: HA saves config
    entries within a second but the entity and device registries only after
    minutes during startup, so an unclean stop can persist the v3 entry while
    the registries still hold the old unique id. Without this, setup would
    create a second entity and voice assistants would lose their agent.

    No-op on a clean entry, when the entry has more than one agent (the target
    is ambiguous), or when the sub-entry already has an entity/device.
    """
    agents = [s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_CONVERSATION]
    if len(agents) != 1:
        return
    subentry_id = agents[0].subentry_id

    entity_registry = er.async_get(hass)
    if entity_registry.async_get_entity_id("conversation", DOMAIN, subentry_id) is None:
        for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
            if (
                entity.domain == "conversation"
                and entity.unique_id == entry.entry_id
                and entity.config_subentry_id not in entry.subentries
            ):
                entity_registry.async_update_entity(
                    entity.entity_id,
                    new_unique_id=subentry_id,
                    config_subentry_id=subentry_id,
                )
                break  # unique ids are unique: at most one legacy entity

    device_registry = dr.async_get(hass)
    if device_registry.async_get_device_by_identifier(
        (DOMAIN, subentry_id), entry.entry_id
    ) is None and (
        device := device_registry.async_get_device_by_identifier(
            (DOMAIN, entry.entry_id), entry.entry_id
        )
    ):
        device_registry.async_update_device(
            device.id,
            new_identifiers={(DOMAIN, subentry_id)},
            new_config_entry_id=entry.entry_id,
            new_config_subentry_id=subentry_id,
        )
