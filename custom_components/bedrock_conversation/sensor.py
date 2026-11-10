"""Sensor platform for actual Amazon Bedrock account cost."""

from __future__ import annotations

from datetime import UTC, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING, Any, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    COST_EXPLORER_METRIC,
    COST_EXPLORER_SERVICE,
    COST_SENSOR_KEY,
    DOMAIN,
)
from .cost import BedrockCostCoordinator

if TYPE_CHECKING:
    from . import BedrockConfigEntry

_ATTR_ESTIMATED = "estimated"
_ATTR_PERIOD_START = "billing_period_start"
_ATTR_PERIOD_END = "billing_period_end_exclusive"
_ATTR_LAST_UPDATED = "last_updated"
_ATTR_METRIC = "metric"
_ATTR_SCOPE = "scope"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BedrockConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the optional account-cost sensor."""
    coordinator = entry.runtime_data.cost_coordinator
    if coordinator is None:
        return
    async_add_entities([BedrockMonthToDateCostSensor(entry, coordinator)])
    # Billing access is optional. Refresh in the background so a billing
    # outage or missing ce:GetCostAndUsage never blocks conversation setup.
    coordinator.async_start_initial_refresh()


class BedrockMonthToDateCostSensor(CoordinatorEntity[BedrockCostCoordinator], SensorEntity):
    """Actual account-wide Amazon Bedrock cost for the current UTC month."""

    _attr_has_entity_name = True
    _attr_translation_key = "month_to_date_account_cost"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 6
    _attr_icon = "mdi:cash"

    def __init__(
        self,
        entry: BedrockConfigEntry,
        coordinator: BedrockCostCoordinator,
    ) -> None:
        """Initialise the sensor on an entry-level service device."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{COST_SENSOR_KEY}"
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Amazon Web Services",
            model="AWS Cost Explorer",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    @override
    def available(self) -> bool:
        """The sensor is unavailable until a cost refresh succeeds."""
        return self.coordinator.last_update_success and self.coordinator.data is not None

    @property
    @override
    def native_value(self) -> Decimal | None:
        """Return the month-to-date amount without losing decimal precision."""
        return self.coordinator.data.amount if self.coordinator.data else None

    @property
    @override
    def native_unit_of_measurement(self) -> str | None:
        """Return the ISO 4217 currency supplied by Cost Explorer."""
        return self.coordinator.data.currency if self.coordinator.data else None

    @property
    @override
    def last_reset(self) -> datetime | None:
        """Return the start of AWS's current UTC billing month."""
        if self.coordinator.data is None:
            return None
        return datetime.combine(
            self.coordinator.data.period_start,
            time.min,
            tzinfo=UTC,
        )

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return billing period and freshness details."""
        data = self.coordinator.data
        if data is None:
            return {}
        return {
            _ATTR_ESTIMATED: data.estimated,
            _ATTR_PERIOD_START: data.period_start.isoformat(),
            _ATTR_PERIOD_END: data.period_end.isoformat(),
            _ATTR_LAST_UPDATED: data.retrieved_at.isoformat(),
            _ATTR_METRIC: COST_EXPLORER_METRIC,
            _ATTR_SCOPE: f"Account-wide {COST_EXPLORER_SERVICE}, all regions",
        }
