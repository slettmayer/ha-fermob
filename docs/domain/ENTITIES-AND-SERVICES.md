# Entities and Services

> What one paired lamp becomes in Home Assistant: one light entity, two diagnostic battery entities, two
> entity services and two options.

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

There are no switches and no other platforms.

## Services

Both are **entity services**, registered on the light platform — not `hass.services` registrations. Neither
takes a schema.

| Service | What it does |
|---|---|
| `fermob.check_in` | Contacts the lamp now — reconnecting a dropped link and refreshing the battery — rather than waiting for the scheduled check-in. Never touches the light |
| `fermob.unpair` | Broadcasts `UNREGISTER` and removes the config entry, which deletes the stored keys with it. The lamp flashes 3× and resets its crypto state, so the Fermob app can claim it again. **Raises if the lamp did not answer**, removing nothing |

`fermob.check_in` is the scheduled check-in routine on demand — see
[STATE-MODEL.md](STATE-MODEL.md#the-check-in). `fermob.unpair` is the one light-path exception in the codebase,
for the reason given in [CONVENTIONS.md](../tech/CONVENTIONS.md#entity-and-connection-code); what it does to
the lamp is in [PAIRING.md](PAIRING.md#unpairing).

## Options

Two, both under **Configure**. Changing either reloads the entry; neither requires re-pairing.

| Option | Default | Decides |
|---|---|---|
| **Lamp type** | Auto | Dimmable or tunable white — whether the lamp gets a colour-temperature slider. Overrides what the lamp reports; see [DEVICES.md](DEVICES.md#lamp-family-detection) |
| **Connection** | Always connected | Whether the BLE link is held open, which is what makes the lamp's own button presses visible; see [STATE-MODEL.md](STATE-MODEL.md#connection-modes) |
