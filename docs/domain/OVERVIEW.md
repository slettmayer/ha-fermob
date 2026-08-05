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
| **MOOON! H134 works on this build** (pairing, on/off, brightness, colour temperature) | **Confirmed on hardware**, 2026-08-02 |
| The H134 reports `module_type` 404 and model `MOOON - H134` | **Confirmed on hardware** — full TLV capture in [LINKIO-PROTOCOL.md](LINKIO-PROTOCOL.md#module_info_get) |
| Other MOOON! sizes (H63 / Ø15 / 3×Ø15 / Ø25) work | **Inferred** — same `module_type`, same protocol, untested by anyone |
| The dimmable-white path still works | **Inferred** — unchanged code and pinned by `test_dw_payload_matches_upstream_literal`, but no Hoopik has run *this* build |

Other Fermob lamps advertising `41c13060-6def-11e5-bcde-0002a5d5c51b` may work but are untested.

## What the integration exposes

One `light` entity per lamp:

- **Brightness** — always. HA's 0–255 maps to the protocol's 0–100 %, with a floor of 1 % so "on at minimum" never turns the lamp off.
- **Colour temperature** — tunable white only, `ColorMode.COLOR_TEMP` over **3000 K – 6000 K**. Note that `COLOR_TEMP` implies brightness support in HA, so `supported_color_modes` is that mode alone, not both.
- **`fermob.unpair`** — an entity service equivalent to "Forget" in the app. See [PAIRING.md](PAIRING.md#unpairing).
- **Lamp type** — an options-flow selector (Auto / Tunable white / Dimmable white). "Auto" now resolves from the `module_type` the lamp reports, so the selector is an override for when that is wrong or unavailable, not the primary mechanism.

Plus two diagnostic entities per lamp, both fed by the same battery push and both **unavailable until the lamp
has reported a level at least once**, so a lamp that has never answered is never mistaken for a flat one:

- **`sensor.<lamp>_battery`** — state of charge as the lamp reports it (`SensorDeviceClass.BATTERY`, %).
- **`binary_sensor.<lamp>_charging`** — on while the lamp sits on its charger (`BinarySensorDeviceClass.BATTERY_CHARGING`).

They exist on every lamp; a model with no battery simply never reports one and they stay unavailable. The
reading is best understood as *"as of last contact"* rather than live — a scheduled check-in keeps it recent
without turning the lamp on, the lamp pushes an update of its own accord whenever the charger goes on or off,
and the last known value is held rather than blanked when the lamp is out of range.

The percentage **moves faster than a cell can charge or drain** around a charger event — 24 % → 33 % in one
test, 86 % → 98 % in another, settling back over the following minutes once the charger comes off — so the lamp
is very likely reporting voltage rather than counting capacity. In between it is stable and slow: about
0.1 %/h measured over 7.6 h with the lamp dark and the BLE link held open.

There are no switches and no other platforms.

## What the official app can configure — and what it cannot

Worth knowing before chasing a missing feature: **the Fermob Lighting app has no lamp-configuration surface at
all.** Established by reading the decompiled app — Fermob Lighting 3.0.2, versionCode 1209, a Cordova/Ionic
hybrid whose entire logic is the JavaScript in `assets/www/build/main.js` — on 2026-08-04. Derived from the
app's JS, not verified against firmware.

Its **Settings** page (`page-settings`) holds exactly one control, a language selector; the only other item, a
notifications toggle, is commented out in the source. Everything the app can change about a lamp is:

| What | Command |
|---|---|
| Rename the lamp | `MODULE_NAME_SET` (49) |
| Brightness and colour temperature — an "ambience" | `DEVICE_DATA_SET` (65) |
| Timer, scheduling | `RULE_*` (97–111) |
| Group membership, LUDO switch assignment | `GROUP_*` (81–88) |
| Set the lamp's clock | `DATETIME_SET` (26) |
| Firmware update, delete / unpair | DFU, `UNREGISTER` (17) |

There is **no output limit, no power profile, no battery-behaviour setting and no persistent power-on default.**
The app's command enum does define `CONFIG_SET` (5), `MODULE_PROPERTY_SET` (53), `DEVICE_PROPERTY_SET` (67) and
`HOST_PARAM_SET` (71) — the plausible homes for something like that — but **it never calls a single one of
them**, so there is no payload to imitate and no evidence the firmware implements them.

The practical consequence: anything the lamp does that you might want to switch off — notably its
[output limiting on battery](LINKIO-PROTOCOL.md#dead-ends--do-not-re-litigate-these) — is firmware behaviour
with no exposed setting, in the app or here. Reaching feature parity with the app is therefore **not** a route
to changing it.

## Lamp-family detection

The family determines the byte layout of every light command, so getting it wrong means the lamp ignores
everything. Resolution order (`light._resolve_light_type`):

1. **Explicit override** in `entry.options["light_type"]` or `entry.data["light_type"]`.
2. **`module_type` as the lamp reported it** — `entry.data["module_type"]`, mapped by
   `protocol.module_type_to_light_type` (401 → dimmable, 404 → tunable). Exact, and the normal path.
3. **Name heuristic** — `"hoop"` in the lamp's name → dimmable white; **everything else → tunable white.**

**Step 2 is only available from the second setup onwards**, because learning it takes a connection. The sequence
is: first command → `MODULE_INFO_GET` → `module_type` and `model` persisted into `entry.data` (and into the key
store) → HA reloads the entry → the entity is rebuilt with the right family. So the name heuristic is the
first-run guess, not the steady state, and it is also the fallback if a lamp reports a `module_type` we do not
recognise — deliberately, since guessing a family from an unknown value would send the wrong payload layout.

Lamps paired before this existed never ran the handshake step that reads it, so `FermobBLEConnection`
re-requests `MODULE_INFO_GET` on reconnect until it has an answer. That is **one extra round trip per install**,
not per connect, and the lamp does answer it in GATEWAY mode.

The override still exists, and still wins over what the lamp says — the escape hatch for a lamp that reports
something wrong.

If this were ever offered upstream, the default should flip to dimmable-white-when-unknown, since upstream's
existing users all have Hoopiks. See [UPSTREAM.md](../tech/UPSTREAM.md).

## State model, and its limits

The integration is push-only — `iot_class: local_push`, `should_poll = False`. State changes reach HA from
exactly two places:

1. **Our own commands.** After a successful write we record what we commanded.
2. **EVENT notifications**, which the lamp volunteers the moment anything about it changes — a button press, a
   brightness or colour-temperature change made at the lamp, the charger going on or off. These arrive in about
   a second, and they are the reason the light entity can be trusted to follow the lamp rather than only the
   last command HA sent.

**The catch is that the lamp pushes only while something is connected to it, and pushes nothing when a
connection is re-established.** So a link that was down during a button press has lost that press permanently;
there is no query that recovers it (see the dead ends in
[LINKIO-PROTOCOL.md](LINKIO-PROTOCOL.md#dead-ends--do-not-re-litigate-these), where both candidate read
commands and the manufacturer's own app are accounted for). Holding the link open is therefore not an
optimisation, it is the entire mechanism, and it is the default.

The **connection mode** option trades it away: *on demand* releases the link 30 s after the last command,
freeing a connection slot on the adapter or BLE proxy, and the entity falls back to showing whatever HA last
commanded. There is deliberately no middle setting — a link held for a few minutes would produce a light that
is sometimes right with no way to tell when.

If a command fails, the entity goes *unavailable* rather than continuing to assert a state that may be false.

## Further reading

- [LINKIO-PROTOCOL.md](LINKIO-PROTOCOL.md) — frames, crypto, commands, payload layouts
- [PAIRING.md](PAIRING.md) — the ownership model, handshake, recovery
