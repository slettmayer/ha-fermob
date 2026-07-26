# Domain Overview

> What these lamps are, what the integration exposes, and how confident we are about each part.

## The devices

Fermob sells Bluetooth LED lamps that speak the Linkio protocol. They split into two LED families, which the
app's device-class table (`manufacturer_id 7`) keys off `module_type`:

| Family | `module_type` | Models | Controls |
|---|---|---|---|
| Dimmable white (`LIGHT_TYPE_DW`) | 401 | Hoopik GL1200 string light (`model_id` 3) | Brightness |
| Tunable white (`LIGHT_TYPE_TW`) | 404 | Every MOOON! and table lamp | Brightness **and** colour temperature |

Per that table, **the Hoopik L1200 is the only dimmable-white model**; everything else Fermob makes in this
line is tunable white.

### Confidence

| Claim | Status |
|---|---|
| Hoopik GL1200 works | Confirmed on hardware by upstream's author |
| MOOON! Moon2AD2 works (on/off, brightness, 3000↔6000 K, reconnect) | Confirmed on hardware by the PR author |
| Other MOOON! sizes (H134 / H63 / Ø15 / 3×Ø15 / Ø25) work | **Inferred** — same `module_type`, same protocol, untested by anyone |
| Any of it works on *this* build | **Not yet tested on hardware** — update this table when it is |

Other Fermob lamps advertising `41c13060-6def-11e5-bcde-0002a5d5c51b` may work but are untested.

## What the integration exposes

One `light` entity per lamp:

- **Brightness** — always. HA's 0–255 maps to the protocol's 0–100 %, with a floor of 1 % so "on at minimum" never turns the lamp off.
- **Colour temperature** — tunable white only, `ColorMode.COLOR_TEMP` over **3000 K – 6000 K**. Note that `COLOR_TEMP` implies brightness support in HA, so `supported_color_modes` is that mode alone, not both.
- **`fermob.unpair`** — an entity service equivalent to "Forget" in the app. See [PAIRING.md](PAIRING.md#unpairing).
- **Lamp type** — an options-flow selector (Auto / Tunable white / Dimmable white), because the family cannot be detected reliably.

There are no sensors, no switches, and no diagnostics.

## Lamp-family detection

The family determines the byte layout of every light command, so getting it wrong means the lamp ignores
everything. Resolution order (`light._resolve_light_type`):

1. **Explicit override** in `entry.options["light_type"]` or `entry.data["light_type"]`.
2. **Name heuristic** — `"hoop"` in the lamp's name → dimmable white; **everything else → tunable white.**

The heuristic is a guess, and it is wrong for a Hoopik that has been renamed. It defaults to tunable white
because that covers every model except one. The model genuinely cannot be read before pairing — the
advertisement is rotating and encrypted — so there is no third, better source; the override exists precisely
because of that.

If this were ever offered upstream, the default should flip to dimmable-white-when-unknown, since upstream's
existing users all have Hoopiks. See [UPSTREAM.md](../tech/UPSTREAM.md).

## State model, and its limits

The integration is push-only — `iot_class: local_push`, `should_poll = False`. State changes reach HA from
exactly two places:

1. **Our own commands.** After a successful write we record what we commanded.
2. **EVENT notifications**, but **only while the BLE link is up** — 30 s after the last command.

A physical button press outside that window is **never seen**, and cannot be recovered later: the lamp sends
no EVENT on reconnect and stops answering state queries in gateway mode. The next HA command puts the lamp
back into a known state. If a command fails, the entity goes *unavailable* rather than continuing to assert a
state that may be false.

This is a genuine limitation of the device, not a shortcut — see the dead ends in
[LINKIO-PROTOCOL.md](LINKIO-PROTOCOL.md#dead-ends--do-not-re-litigate-these).

## Further reading

- [LINKIO-PROTOCOL.md](LINKIO-PROTOCOL.md) — frames, crypto, commands, payload layouts
- [PAIRING.md](PAIRING.md) — the ownership model, handshake, recovery
