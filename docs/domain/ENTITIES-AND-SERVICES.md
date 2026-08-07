# Entities and Services

> What one paired lamp becomes in Home Assistant: one light entity, two diagnostic battery entities, a firmware
> entity, two entity services and three options.

**Scope.** The user-facing surface. How fresh any of it is — and why — is in
[STATE-MODEL.md](STATE-MODEL.md). Which lamps get a colour-temperature slider at all is in
[DEVICES.md](DEVICES.md).

## The light entity

One `light` entity per lamp:

- **Brightness** — always. HA's 0–255 maps to the protocol's 0–100 %, with a floor of 1 % so "on at minimum"
  never turns the lamp off.
- **Colour temperature** — tunable white only, `ColorMode.COLOR_TEMP` over **3000 K – 6000 K**. Note that
  `COLOR_TEMP` implies brightness support in HA, so `supported_color_modes` is that mode alone, not both.
- **Availability** is explicit, not derived from the Bluetooth presence cache: optimistic at startup, `False`
  after a failed command, `True` after a successful one or an inbound EVENT.

Colour modes are fixed at construction from the resolved family, so changing the lamp type reloads the entry.

## Battery entities

Two diagnostic entities per lamp, both fed by the same battery push and both **unavailable until the lamp has
reported a level at least once**, so a lamp that has never answered is never mistaken for a flat one:

- **`sensor.<lamp>_battery`** — state of charge as the lamp reports it (`SensorDeviceClass.BATTERY`, %).
- **`binary_sensor.<lamp>_charging`** — on while the lamp sits on its charger
  (`BinarySensorDeviceClass.BATTERY_CHARGING`).

They exist on every lamp; a model with no battery simply never reports one and they stay unavailable. The
reading is best understood as *"as of last contact"* rather than live — a scheduled check-in keeps it recent
without turning the lamp on, the lamp pushes an update of its own accord whenever the charger goes on or off,
and the last known value is held rather than blanked when the lamp is out of range.

**The gauge is a voltage proxy, not a capacity count.** The percentage moves faster than a cell can charge or
drain around a charger event — 24 % → 33 % in one test, 86 % → 98 % in another, settling back over the
following minutes once the charger comes off. In between it is stable and slow-moving (see
[STATE-MODEL.md](STATE-MODEL.md#what-holding-the-link-costs) for the measured rate). Take the trustworthy
figure once the lamp has been off the charger for a while.

## The firmware entity

One `update` entity per lamp — `update.<lamp>_firmware` — since 0.10.0. It exists whatever the *Check for
firmware updates* option says; the option decides only whether it may ask the network.

- **`installed_version`** is local: the `0xb5` TLV of the `MODULE_INFO_GET` reply, rendered by
  `protocol.format_sw_version`. It is read from the connection first and the config entry second, so a fresh
  reading shows up before the reload that persists it and a restart has an answer before the first connect.
- **`latest_version`** comes from the vendor release server (`firmware.py`), asked once when the entity is added
  and daily after that. When nothing newer exists it reports the *installed* version, which is how an update
  entity says "up to date" — and what stops the server's three-component `3.0.24` reading as an update against
  a lamp's `3.0.24.0`.
- **Until a check has actually succeeded it is `None`, and the entity reads *unknown*.** Falling back to the
  installed version there would assert currency that was never checked — permanently for a model the server does
  not carry, which is every Hoopik slug, and for any install that cannot reach the server. Unknown is the honest
  answer, and it is why the entity asks once on being added rather than only on its daily tick: the state would
  otherwise sit at unknown for a day after every restart.
- **It does not support `UpdateEntityFeature.INSTALL`, deliberately.** Nothing here can write a signed Nordic
  Secure DFU image; the `release_summary` says so and points at the Fermob app. Adding the feature would put a
  button in the UI that nothing implements. See [FIRMWARE-UPDATE.md](FIRMWARE-UPDATE.md).
- **It stays available when the lamp is out of range**, holding the last answer. Neither version is state the
  lamp has to be present to have.
- **`_attr_name` is deliberately absent, not `None`.** Setting it to `None` declares the entity to be the
  device's main feature, which names it after the lamp and registers it as `update.<lamp>`. Left unset,
  `UpdateEntity` names it from its device class — which is where `_firmware` and "Firmware" come from.

### Switching the check off leaves the entity in place, unpolled

**The option governs the request, not the entity's existence** — `should_poll` goes False and no one-shot is
scheduled, so nothing leaves the network, and `latest_version` stays None so the entity reads *unknown*: exactly
true, since we were told not to find out. Three tidier-looking alternatives were each tried and reverted, and
the reasons are worth keeping because they are not obvious:

| Alternative | Why not |
|---|---|
| Don't add the entity | HA keeps the registry row and renders it *unavailable* — and the `unavailable` state written during unload is never cleaned up (`cleanup_restored_states_filter` fires on removal and rename only), so a dashboard or template reads `unavailable` until the next restart. That is "looks broken", which is what this was meant to avoid |
| Delete the registry row | Throws away the user's rename, area, icon and hidden flag, orphans the recorder history, and silently breaks anything referencing the entity_id |
| Disable the registry row | Clearing `disabled_by` again makes core's `EntityRegistryDisabledHandler` schedule a **second** config-entry reload 30 s later, which tears down the BLE link for an unrelated options change |

A user who wants the entity out of sight disables it in the UI, which HA already honours by never adding it.
That keeps one writer per fact: the option owns the traffic, the registry owns the visibility.

The firmware and hardware versions also appear on the **device page**, from the same TLVs — those are
unconditional, since they need no network.

There are no switches and no other platforms.

## Services

Both are **entity services**, registered on the light platform — not `hass.services` registrations. Neither
takes a schema.

| Service | What it does |
|---|---|
| `fermob.check_in` | Contacts the lamp now — reconnecting a dropped link and refreshing the battery — rather than waiting for the scheduled check-in. Never touches the light. Cannot be called on an *unavailable* entity; see below |
| `fermob.unpair` | Checks the session is alive, then broadcasts `UNREGISTER` (best-effort — the broadcast itself is never acknowledged) and removes the config entry, which deletes the stored keys with it. The lamp flashes 3× and resets its crypto state, so the Fermob app can claim it again. **Raises if the lamp was not answering** — the broadcast is not sent and nothing is removed. Also raises, with a different message, if the lamp *answered* that it no longer holds our keys: it is already free, so there is nothing to release. An entry with no stored keys is removed without touching the radio. For a lamp that is gone for good, delete the integration instead |

`fermob.check_in` is the scheduled check-in routine on demand — see
[STATE-MODEL.md](STATE-MODEL.md#the-check-in). `fermob.unpair` is the one light-path exception in the codebase,
for the reason given in [CONVENTIONS.md](../tech/CONVENTIONS.md#entity-and-connection-code); what it does to
the lamp is in [PAIRING.md](PAIRING.md#unpairing).

### Neither can be called on an unavailable entity, and that is accepted

Home Assistant filters an entity service's targets by availability **before the handler runs**. The path is
`entity_service_call` → `_resolve_entity_service_call_entities` in `homeassistant/helpers/service.py` (read
from HA 2026.8.0):

```python
entity_candidates = [e for e in entity_candidates if e.available]
missing = referenced.referenced.copy()
for entity in entity_candidates:
    missing.discard(entity.entity_id)
referenced.log_missing(missing, _LOGGER)
```

So a call aimed at an unavailable entity does nothing and still **reports success**. It is not completely
silent — HA logs *"Referenced entities … are missing or not currently available"* under
`homeassistant.helpers.service`, not under this integration, which is why it is easy to miss when grepping a
log for `fermob`.

That is the mechanism behind the 0.9.0 dead end: the check-in marked a factory-reset lamp unavailable, and Home
Assistant then discarded the `light.turn_on` that would have re-paired it. **0.9.1 fixed the dead end at the
source** by keeping a `KEYS_REJECTED` lamp *available*, because a command genuinely does work on one.

It still means `fermob.check_in` cannot be used on a greyed-out lamp, and that is a real limitation rather than
a bug to route around:

- **The scheduled check-in is unaffected**, because the timer in `__init__.py` calls `conn.async_check_in()` on
  the connection directly and never goes through the service layer. A lamp whose entity went unavailable is
  therefore recovered **with no user action at all**, within one check-in interval. Verified on hardware
  (2026-08-06): an entity unavailable for 26 minutes was restored by the scheduled check-in alone, and the same
  run confirmed the service being dropped.
- **Reloading the entry clears the grey at once** — Settings → Devices & Services → Fermob → ⋮ → Reload. Be
  precise about what that does, because it is easy to overstate: it rebuilds the entity, and a fresh
  `FermobLight` starts out `available`. It does **not** contact the lamp — `async_setup_entry` opens no link.
  First contact is the startup check-in a minute later, or the next command. So on a lamp that is still out of
  range, reloading makes the entity look healthy and it will fail again on the next command.

0.9.2 briefly moved `check_in` to a domain service to lift the limitation. It was reverted: leaving the entity
platform means reimplementing target expansion, concurrent dispatch, registration lifetime **and** per-entity
permission checks, all of which HA does for free, and two review rounds found defects in each. The whole
benefit was not waiting up to one check-in interval for something that already recovers by itself. **If you are
tempted to try again, that is the trade to beat** — and note that a domain service also lets a non-admin user
who is denied the light entities contact every lamp over BLE.

## Options

Three, all under **Configure**. Changing any of them reloads the entry; none requires re-pairing.

| Option | Default | Decides |
|---|---|---|
| **Lamp type** | Auto | Dimmable or tunable white — whether the lamp gets a colour-temperature slider. Overrides what the lamp reports; see [DEVICES.md](DEVICES.md#lamp-family-detection) |
| **Connection** | Always connected | Whether the BLE link is held open, which is what makes the lamp's own button presses visible; see [STATE-MODEL.md](STATE-MODEL.md#connection-modes) |
| **Check for firmware updates** | On | Whether the firmware entity may ask the vendor server whether a newer build is published. **The integration's only non-local traffic**, which is the whole reason it is switchable. Off leaves the entity in place, unpolled and reading *unknown* — [and why that beats removing, deleting or disabling it](#switching-the-check-off-leaves-the-entity-in-place-unpolled) |
