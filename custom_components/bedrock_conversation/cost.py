"""AWS Cost Explorer coordinator for actual Amazon Bedrock billing cost."""

from __future__ import annotations

import asyncio
from collections.abc import Hashable
from datetime import date, timedelta
from typing import Any, override

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .client import BedrockAuthError, BedrockClient, BedrockError, CostExplorerData
from .const import (
    CONF_AWS_ACCESS_KEY_ID,
    COST_EXPLORER_UPDATE_INTERVAL,
    DOMAIN,
    LOGGER,
)

type _CostCoordinatorIdentity = tuple[str, Hashable]
_COST_COORDINATORS: HassKey[dict[_CostCoordinatorIdentity, BedrockCostCoordinator]] = HassKey(
    f"{DOMAIN}_cost_coordinators"
)
_COST_EXPLORER_REQUEST_SLOT: HassKey[asyncio.Semaphore] = HassKey(
    f"{DOMAIN}_cost_explorer_request_slot"
)


class BedrockCostCoordinator(DataUpdateCoordinator[CostExplorerData]):
    """Fetch application-tag-filtered Bedrock month-to-date cost.

    Cost Explorer data can lag by hours and can be revised by AWS. A failed
    refresh only makes the cost sensor unavailable; it never prevents or stops
    a conversation agent.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        name_entry_id: str,
        tag_key: str,
        tag_value: str,
    ) -> None:
        """Initialise an application-tag-filtered coordinator without entry-owned scheduling."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=None,
            name=f"{DOMAIN}_{name_entry_id}_cost",
            update_interval=COST_EXPLORER_UPDATE_INTERVAL,
            always_update=False,
        )
        self._entries: dict[str, tuple[ConfigEntry, BedrockClient]] = {}
        self._request_slot = hass.data.setdefault(_COST_EXPLORER_REQUEST_SLOT, asyncio.Semaphore(1))
        self.tag_key = tag_key
        self.tag_value = tag_value
        self._initial_refresh_started = False
        self._authentication_failed = False
        self._initial_refresh_task: asyncio.Future[Any] | None = None
        self._active_refresh_tasks: set[asyncio.Task[Any]] = set()

    @property
    def has_entries(self) -> bool:
        """Return whether an enabled entry still uses this coordinator."""
        return bool(self._entries)

    @callback
    def async_add_entry(self, entry: ConfigEntry, client: BedrockClient) -> None:
        """Add or replace an entry and make its validated client current."""
        polling_was_enabled = self._polling_enabled
        self._entries[entry.entry_id] = (entry, client)
        if not polling_was_enabled and self._polling_enabled and self._listeners:
            self._schedule_refresh()

    @callback
    def async_remove_entry(self, entry_id: str) -> None:
        """Stop using an entry while retaining other sensors and clients."""
        polling_was_enabled = self._polling_enabled
        self._entries.pop(entry_id, None)
        if polling_was_enabled and not self._polling_enabled:
            self._unschedule_refresh()

    @property
    def _polling_enabled(self) -> bool:
        """Return whether at least one sharing entry permits polling."""
        return any(not entry.pref_disable_polling for entry, _client in self._entries.values())

    @callback
    @override
    def _schedule_refresh(self) -> None:
        """Schedule one shared refresh if any attached entry permits polling."""
        if self._polling_enabled:
            super()._schedule_refresh()

    @callback
    def async_start_initial_refresh(self) -> None:
        """Start at most one initial refresh for the shared coordinator."""
        if self._initial_refresh_task is not None:
            return
        if self._initial_refresh_started and not self._authentication_failed:
            return
        self._initial_refresh_started = True
        task = self.hass.async_create_background_task(
            self.async_refresh(),
            f"{self.name}_initial_refresh",
        )
        self._initial_refresh_task = task
        task.add_done_callback(self._async_clear_initial_refresh_task)

    @callback
    def _async_clear_initial_refresh_task(self, task: asyncio.Future[Any]) -> None:
        """Forget a completed initial refresh while retaining its started state."""
        if self._initial_refresh_task is task:
            self._initial_refresh_task = None

    async def _async_update_data(self) -> CostExplorerData:
        """Fetch the current UTC billing month through tomorrow (exclusive)."""
        task = asyncio.current_task()
        if task is not None:
            self._active_refresh_tasks.add(task)
        try:
            _entry, client = next(reversed(self._entries.values()))
            today = dt_util.utcnow().date()
            period_start = date(today.year, today.month, 1)
            period_end = today + timedelta(days=1)
            try:
                async with self._request_slot:
                    data = await client.async_get_month_to_date_cost(
                        period_start,
                        period_end,
                        self.tag_key,
                        self.tag_value,
                    )
                self._authentication_failed = False
                return data
            except BedrockAuthError as err:
                self._authentication_failed = True
                # Every attached entry uses the same access-key identity. Start
                # reauthentication for each one without exposing AWS's message.
                LOGGER.debug("Cost Explorer authentication failed: %s", err)
                for entry, _client in tuple(self._entries.values()):
                    entry.async_start_reauth_if_available(self.hass)
                raise ConfigEntryAuthFailed(err.code) from err
            except BedrockError as err:
                # AWS messages may contain account/principal details. Keep the full
                # message at debug level and expose only the error code to HA state.
                LOGGER.debug("Cost Explorer refresh failed: %s", err)
                raise UpdateFailed(f"AWS Cost Explorer error ({err.code})") from err
        finally:
            if task is not None:
                self._active_refresh_tasks.discard(task)

    @override
    async def async_shutdown(self) -> None:
        """Cancel shared initial or scheduled work after the final entry leaves."""
        await super().async_shutdown()
        current = asyncio.current_task()
        tasks = {task for task in self._active_refresh_tasks if task is not current}
        if (
            self._initial_refresh_task is not None
            and not self._initial_refresh_task.done()
            and self._initial_refresh_task is not current
        ):
            tasks.add(self._initial_refresh_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def async_acquire_cost_coordinator(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: BedrockClient,
    tag_key: str,
    tag_value: str,
) -> BedrockCostCoordinator:
    """Return the coordinator shared by entries with one key and tag filter."""
    access_key_id = entry.data.get(CONF_AWS_ACCESS_KEY_ID)
    identity: _CostCoordinatorIdentity = (
        (
            "access_key_and_tag",
            (access_key_id, tag_key, tag_value),
        )
        if isinstance(access_key_id, str) and access_key_id
        else ("entry_and_tag", (entry.entry_id, tag_key, tag_value))
    )
    coordinators = hass.data.setdefault(_COST_COORDINATORS, {})
    if (coordinator := coordinators.get(identity)) is None:
        coordinator = BedrockCostCoordinator(hass, entry.entry_id, tag_key, tag_value)
        coordinators[identity] = coordinator
        await coordinator.async_register_shutdown()
    coordinator.async_add_entry(entry, client)
    return coordinator


async def async_release_cost_coordinator(
    hass: HomeAssistant,
    entry_id: str,
    coordinator: BedrockCostCoordinator,
) -> None:
    """Release one entry and shut down the coordinator after the final user."""
    coordinator.async_remove_entry(entry_id)
    if coordinator.has_entries:
        return

    coordinators = hass.data.get(_COST_COORDINATORS)
    if coordinators is not None:
        for identity, shared in tuple(coordinators.items()):
            if shared is coordinator:
                coordinators.pop(identity)
                break
        if not coordinators:
            hass.data.pop(_COST_COORDINATORS)
    await coordinator.async_shutdown()
