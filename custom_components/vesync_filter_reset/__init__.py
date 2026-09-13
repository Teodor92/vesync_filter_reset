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
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import target as target_helpers
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

DOMAIN = "vesync_filter_reset"
VESYNC_DOMAIN = "vesync"
SERVICE_RESET_FILTER = "reset_filter"

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

# Home Assistant merges the whole target dict into the call data before
# validating, so the schema has to accept every target key -- including
# floor_id and label_id, which the UI target picker offers.
SERVICE_SCHEMA = vol.Schema(cv.TARGET_SERVICE_FIELDS)


def _collect_device_ids(hass: HomeAssistant, call: ServiceCall) -> set[str]:
    """Resolve whatever the caller targeted down to device registry IDs.

    Delegated to Home Assistant's own resolver rather than walking the
    registries by hand: that gets floor and label targets, entity-registry
    UUIDs, the `none` sentinel and composite devices for free.
    """
    ent_reg = er.async_get(hass)

    selection = target_helpers.TargetSelection(call.data)
    referenced = target_helpers.async_extract_referenced_entity_ids(hass, selection)

    device_ids: set[str] = set(referenced.referenced_devices)
    for entity_id in referenced.referenced | referenced.indirectly_referenced:
        entity = ent_reg.async_get(entity_id)
        if entity is not None and entity.device_id:
            device_ids.add(entity.device_id)

    return device_ids


def _vesync_cid(device_entry: dr.DeviceEntry) -> str | None:
    """Return the VeSync identifier this device is registered under."""
    for domain, identifier in device_entry.identifiers:
        if domain == VESYNC_DOMAIN:
            return identifier
    return None


def _vesync_config_entries(
    hass: HomeAssistant, device_entry: dr.DeviceEntry
) -> list[ConfigEntry]:
    """Every vesync config entry this device is linked to.

    ``device_entry.config_entries`` is a set, and core supports several vesync
    entries, so callers must not assume there is exactly one.
    """
    entries = []
    for entry_id in device_entry.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None and entry.domain == VESYNC_DOMAIN:
            entries.append(entry)
    return entries


def _manager_for(entry: ConfigEntry) -> Any | None:
    """Reach the pyvesync manager without assuming runtime_data's shape.

    vesync is quality_scale bronze, so this layout is not a public contract.
    Returning None lets the caller raise something readable instead of an
    AttributeError traceback.
    """
    return getattr(entry.runtime_data, "manager", None)


def _find_pyvesync_device(manager: Any, cid: str) -> Any | None:
    """Match a registry identifier back to a live pyvesync device object.

    VeSyncBaseEntity.base_unique_id is ``cid`` or ``cid + sub_device_no``;
    reconstruct the same key rather than matching either form loosely.
    """
    for device in manager.devices:
        sub = device.sub_device_no
        key = f"{device.cid}{sub}" if isinstance(sub, int) else device.cid
        if key == cid:
            return device
    return None


def _supports_filter_reset(device: Any) -> bool:
    """True only when the device class actually overrides pyvesync's stub.

    Every VeSyncPurifier carries a ``reset_filter`` attribute, but the base
    class implementation is a no-op returning False without calling the API --
    VeSyncAir131 (LV-PUR131S) and VeSyncAirRH131 inherit it. hasattr() cannot
    tell those apart from a real implementation, so check for the override.
    """
    try:
        from pyvesync.base_devices.purifier_base import VeSyncPurifier
    except ImportError:
        # pyvesync moved this path between 2.x and 3.x. Fall back to the
        # loose check rather than failing outright.
        return hasattr(device, "reset_filter")

    if not isinstance(device, VeSyncPurifier):
        return False
    return type(device).reset_filter is not VeSyncPurifier.reset_filter


def _rejection_reason(device: Any) -> str:
    """Pull VeSync's own reason out of the last response, when there is one."""
    response = getattr(device, "last_response", None)
    message = getattr(response, "message", None)
    return message or "VeSync rejected the request"


def _resolve_target(
    hass: HomeAssistant, device_entry: dr.DeviceEntry, cid: str, name: str
) -> tuple[ConfigEntry, Any]:
    """Find the loaded vesync entry that actually knows this device."""
    entries = _vesync_config_entries(hass, device_entry)
    if not entries:
        raise HomeAssistantError(f"No VeSync config entry found for {name}.")

    for entry in entries:
        if entry.state is not ConfigEntryState.LOADED:
            continue
        manager = _manager_for(entry)
        if manager is None:
            raise HomeAssistantError(
                f"Incompatible VeSync integration version: cannot reach the "
                f"pyvesync manager for {name}."
            )
        device = _find_pyvesync_device(manager, cid)
        if device is not None:
            return entry, device

    if not any(entry.state is ConfigEntryState.LOADED for entry in entries):
        raise HomeAssistantError(f"The VeSync config entry for {name} is not loaded.")

    raise HomeAssistantError(
        f"{name} is in the device registry but no loaded VeSync entry has a device "
        f"with cid {cid}. Try reloading the VeSync integration."
    )


async def _async_reset_filter(call: ServiceCall) -> None:
    """Reset filter life to 100% on every targeted VeSync purifier."""
    hass = call.hass
    dev_reg = dr.async_get(hass)

    device_ids = _collect_device_ids(hass, call)
    if not device_ids:
        raise HomeAssistantError(
            "No target supplied. Target a VeSync purifier entity, device, area, "
            "floor or label."
        )

    targets: list[tuple[str, ConfigEntry, Any]] = []
    for device_id in device_ids:
        device_entry = dev_reg.async_get(device_id)
        if device_entry is None:
            continue

        cid = _vesync_cid(device_entry)
        if cid is None:
            # Not a VeSync device. Skip quietly so broad targets stay usable.
            continue

        name = device_entry.name_by_user or device_entry.name or device_id
        entry, device = _resolve_target(hass, device_entry, cid, name)

        if not _supports_filter_reset(device):
            raise HomeAssistantError(
                f"{name} ({device.device_type}) does not support filter reset."
            )

        targets.append((name, entry, device))

    if not targets:
        raise HomeAssistantError("No VeSync devices matched the supplied target.")

    succeeded: list[str] = []
    failed: list[str] = []
    entries_to_refresh: dict[str, ConfigEntry] = {}

    try:
        # Sequential on purpose: pyvesync raises VeSyncRateLimitError, so firing
        # these concurrently trades one problem for another.
        for name, entry, device in targets:
            _LOGGER.debug("Resetting filter life for %s (%s)", name, device.device_type)
            try:
                reset_ok = await device.reset_filter()
            except Exception as err:
                # Broad by design: every pyvesync and aiohttp failure should
                # reach the user as a readable error, not a log-only traceback.
                # CancelledError is a BaseException, so it still propagates.
                failed.append(f"{name}: {err}")
                continue

            if not reset_ok:
                failed.append(f"{name}: {_rejection_reason(device)}")
                continue

            _LOGGER.info("Filter life reset to 100%% for %s", name)
            succeeded.append(name)
            entries_to_refresh[entry.entry_id] = entry
    finally:
        # Always refresh whatever succeeded, even if a later target failed --
        # reset_filter does not update state locally, so without this the
        # filter-life sensor keeps reporting the old value until the next poll.
        for entry in entries_to_refresh.values():
            coordinator = entry.runtime_data
            if isinstance(coordinator, DataUpdateCoordinator):
                await coordinator.async_request_refresh()

    if failed:
        raise HomeAssistantError(
            f"Filter reset succeeded for: {', '.join(succeeded) or 'none'}. "
            f"Failed: {'; '.join(failed)}"
        )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the reset_filter service."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_FILTER,
        _async_reset_filter,
        schema=SERVICE_SCHEMA,
    )
    return True
