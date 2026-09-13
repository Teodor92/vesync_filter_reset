# VeSync Filter Reset

Adds a `vesync_filter_reset.reset_filter` action to Home Assistant, so you can
reset the filter life on a Levoit / VeSync air purifier **without opening the
VeSync app**.

## Why this exists

The `pyvesync` library — the one Home Assistant already ships and
authenticates — has implemented filter reset for a long time:

```python
# pyvesync/devices/vesyncpurifier.py  (class VeSyncAirBypass)
async def reset_filter(self) -> bool:
    """Reset filter to 100%."""
    r_dict = await self.call_bypassv2_api('resetFilter')
```

Home Assistant's core `vesync` integration never wires it to anything. As of
Home Assistant 2026.9 (`pyvesync==3.4.2`) the integration ships no `button`
platform at all, and registers exactly one action — `vesync.update_devices`.

So the filter-life sensor tells you the filter is dead, and then you have to go
find your phone. This closes that gap by reusing the manager object the core
integration has **already logged in**, reached via
`config_entry.runtime_data.manager`. It does not log in, store credentials, or
talk to the VeSync cloud on its own.

## Requirements

- The core [VeSync integration](https://www.home-assistant.io/integrations/vesync/)
  set up and loaded
- Home Assistant 2026.9 or newer

## Installation

### HACS (custom repository)

1. HACS → three-dot menu → **Custom repositories**
2. Add `https://github.com/Teodor92/vesync_filter_reset`, category **Integration**
3. Install **VeSync Filter Reset**
4. Add the line below to `configuration.yaml`
5. Restart Home Assistant

### Manual

Copy `custom_components/vesync_filter_reset/` into your `config/custom_components/`
directory, then do steps 4 and 5 above.

### Required configuration

This integration has no config flow, so it needs one line in
`configuration.yaml`:

```yaml
vesync_filter_reset:
```

A **full restart** is required — a YAML reload will not pick up a new custom
component.

## Usage

```yaml
action: vesync_filter_reset.reset_filter
target:
  entity_id: fan.office_air_purifier
```

Target any entity, device, area, floor or label belonging to the purifier — the
sensor, the fan, the child-lock switch all work, because targets are resolved to
the underlying device using Home Assistant's own target resolver. Non-VeSync
devices caught by a broad target are skipped silently; a VeSync device with no
resettable filter raises an error naming the model.

When several purifiers are targeted at once, every one is attempted. If some
fail, the action still resets and refreshes the rest, then reports both lists in
a single error — it does not stop at the first failure and leave the remainder
untouched.

### Dashboard button

```yaml
type: button
name: Reset office filter
icon: mdi:restart
tap_action:
  action: perform-action
  perform_action: vesync_filter_reset.reset_filter
  target:
    entity_id: fan.office_air_purifier
  confirmation:
    text: Reset the office filter to 100%?
```

> **Use a confirmation.** The reset is irreversible. Firing it on a purifier
> whose filter has *not* been replaced sets that unit to 100% and destroys the
> real filter reading, with no way to put it back.

## Supported devices

Anything `pyvesync` maps to `VeSyncAirBypass` or its subclass
`VeSyncAirBaseV2` — which is every Core / Vital / LAP-series purifier.
Verified working on:

| Model | pyvesync class |
| --- | --- |
| Core300S | `VeSyncAirBypass` |
| LAP-C601S-WEU (Core600S) | `VeSyncAirBypass` |
| LAP-V102S-WEU (Vital 100S) | `VeSyncAirBaseV2` |

**Not supported:** `VeSyncAir131` (LV-PUR131S) and `VeSyncAirRH131`. Both are
purifiers and both have a `reset_filter` attribute, but they inherit the base
class no-op, which returns `False` without ever calling the API. This
integration detects that the method is not overridden and refuses up front,
rather than reporting a cloud rejection that never happened.

Note that `pyvesync`'s `device_map.py` only declares the
`PurifierFeatures.RESET_FILTER` flag for `Core200S` and `CS137-AF`. That flag is
**not** checked by `reset_filter()`, which is why the models above work anyway —
the metadata is simply incomplete upstream. This integration deliberately does
not gate on that flag either.

If a model genuinely does not support the command, the VeSync API returns a
failure and the action raises a `HomeAssistantError` naming the device.

## Errors

All failures raise `HomeAssistantError`, so they surface in the UI rather than
only in the log:

| Message | Cause |
| --- | --- |
| `No target supplied` | Action called with no target |
| `No VeSync devices matched the supplied target` | Target contained no VeSync devices |
| `The VeSync config entry for ... is not loaded` | No loaded vesync entry for that device |
| `... does not support filter reset` | Device isn't a purifier, or is one whose pyvesync class doesn't override the stub |
| `Incompatible VeSync integration version` | `runtime_data` no longer exposes the pyvesync manager |
| `no loaded VeSync entry has a device with cid ...` | Registry and library are out of sync — reload the VeSync integration |
| `Filter reset succeeded for: ... Failed: ...` | One or more targets failed; the listed successes did complete and were refreshed |

Failures against the cloud quote VeSync's own reason where the API supplies one,
rather than a generic message.

After a successful reset the integration requests a coordinator refresh, so the
filter-life sensor updates immediately instead of at the next poll.
`reset_filter()` does not update local state, so this step is required, not
cosmetic.

## Icon

`custom_components/vesync_filter_reset/brand/` carries the icon HACS displays.
It reuses VeSync's brand green (`#17B68B` → `#0AA67F`, sampled from the asset in
[home-assistant/brands](https://github.com/home-assistant/brands/tree/master/core_integrations/vesync))
and their corner radius, with a reset glyph in place of the wordmark — related
by colour, without presenting itself as an official VeSync integration. The
wordmark was dropped because it is unreadable at the 32–48px these are actually
rendered at.

## Licence

Apache-2.0
