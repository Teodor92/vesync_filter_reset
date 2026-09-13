"""Reset the filter life on VeSync purifiers.

pyvesync (pinned at 3.4.2 by Home Assistant 2026.9.x) implements
``VeSyncAirBypass.reset_filter()``, but the core ``vesync`` integration never
wires it to an entity or a service. This component closes that gap.

It does not log in or talk to the VeSync cloud itself. It reuses the manager
object already authenticated by the core integration, reached via
``config_entry.runtime_data.manager``.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "vesync_filter_reset"
VESYNC_DOMAIN = "vesync"
SERVICE_RESET_FILTER = "reset_filter"

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): cv.entity_ids,
        vol.Optional("device_id"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("area_id"): vol.All(cv.ensure_list, [cv.string]),
    }
)


def _collect_device_ids(hass: HomeAssistant, call: ServiceCall) -> set[str]:
    """Resolve whatever the caller targeted down to device registry IDs."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    device_ids: set[str] = set(call.data.get("device_id", []))

    for entity_id in call.data.get("entity_id", []):
        entity = ent_reg.async_get(entity_id)
        if entity is not None and entity.device_id:
            device_ids.add(entity.device_id)

    for area_id in call.data.get("area_id", []):
        for device in dr.async_entries_for_area(dev_reg, area_id):
            device_ids.add(device.id)
        for entity in er.async_entries_for_area(ent_reg, area_id):
            if entity.device_id:
                device_ids.add(entity.device_id)

    return device_ids


def _vesync_cid(device_entry: dr.DeviceEntry) -> str | None:
    """Return the VeSync identifier this device is registered under."""
    for domain, identifier in device_entry.identifiers:
        if domain == VESYNC_DOMAIN:
            return identifier
    return None


def _vesync_config_entry(
    hass: HomeAssistant, device_entry: dr.DeviceEntry
) -> ConfigEntry | None:
    """Find the vesync config entry backing a device (core supports several)."""
    for entry_id in device_entry.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None and entry.domain == VESYNC_DOMAIN:
            return entry
    return None


def _find_pyvesync_device(manager, cid: str):
    """Match a registry identifier back to a live pyvesync device object.

    VeSyncBaseEntity.base_unique_id is ``cid`` or ``cid + sub_device_no``,
    so both forms are accepted.
    """
    for device in manager.devices:
        if device.cid == cid:
            return device
        sub = device.sub_device_no
        if isinstance(sub, int) and f"{device.cid}{sub}" == cid:
            return device
    return None


async def _async_reset_filter(call: ServiceCall) -> None:
    """Reset filter life to 100% on every targeted VeSync purifier."""
    hass = call.hass
    dev_reg = dr.async_get(hass)

    device_ids = _collect_device_ids(hass, call)
    if not device_ids:
        raise HomeAssistantError(
            "No target supplied. Target a VeSync purifier entity, device or area."
        )

    targets = []
    for device_id in device_ids:
        device_entry = dev_reg.async_get(device_id)
        if device_entry is None:
            continue

        cid = _vesync_cid(device_entry)
        if cid is None:
            # Not a VeSync device. Skip quietly so area targets stay usable.
            continue

        name = device_entry.name_by_user or device_entry.name or device_id

        entry = _vesync_config_entry(hass, device_entry)
        if entry is None:
            raise HomeAssistantError(f"No VeSync config entry found for {name}.")
        if entry.state is not ConfigEntryState.LOADED:
            raise HomeAssistantError(
                f"The VeSync config entry for {name} is not loaded."
            )

        device = _find_pyvesync_device(entry.runtime_data.manager, cid)
        if device is None:
            raise HomeAssistantError(
                f"{name} is in the device registry but pyvesync has no device with "
                f"cid {cid}. Try reloading the VeSync integration."
            )
        if not hasattr(device, "reset_filter"):
            raise HomeAssistantError(
                f"{name} ({device.device_type}) has no filter to reset."
            )

        targets.append((name, entry, device))

    if not targets:
        raise HomeAssistantError("No VeSync devices matched the supplied target.")

    entries_to_refresh: dict[str, ConfigEntry] = {}
    for name, entry, device in targets:
        _LOGGER.debug("Resetting filter life for %s (%s)", name, device.device_type)
        try:
            reset_ok = await device.reset_filter()
        except Exception as err:
            raise HomeAssistantError(f"Filter reset failed for {name}: {err}") from err

        if not reset_ok:
            raise HomeAssistantError(
                f"VeSync rejected the filter reset for {name} ({device.device_type}). "
                "The device may not support the resetFilter command."
            )

        _LOGGER.info("Filter life reset to 100%% for %s", name)
        entries_to_refresh[entry.entry_id] = entry

    # Pull the new filter_life straight away instead of waiting for the next poll.
    for entry in entries_to_refresh.values():
        await entry.runtime_data.async_request_refresh()


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the reset_filter service."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_FILTER,
        _async_reset_filter,
        schema=SERVICE_SCHEMA,
    )
    return True
