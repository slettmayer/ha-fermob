# What the Official App Can Configure

> The Fermob Lighting app's full lamp-configuration surface, established by reading its code. Worth knowing
> before chasing a missing feature: there is far less here than people assume.

**Scope.** The vendor app as a source of truth about what the firmware will accept. Commands this integration
actually sends are in [PROTOCOL-COMMANDS.md](PROTOCOL-COMMANDS.md); the specific behaviour people most often go
looking for a setting for is in
[DEAD-ENDS.md](DEAD-ENDS.md#brightness-that-feels-capped-is-the-lamps-own-power-management).

**Confidence: derived from the app's JS, not verified against firmware.** Established by reading the
decompiled app — Fermob Lighting 3.0.2, versionCode 1209, a Cordova/Ionic hybrid whose entire logic is the
JavaScript in `assets/www/build/main.js` — on 2026-08-04.

## There is no lamp-configuration surface

The app's **Settings** page (`page-settings`) holds exactly one control, a language selector; the only other
item, a notifications toggle, is commented out in the source.

Everything the app can change about a lamp is:

| What | Command |
|---|---|
| Rename the lamp | `MODULE_NAME_SET` (49) |
| Brightness and colour temperature — an "ambience" | `DEVICE_DATA_SET` (65) |
| Timer, scheduling | `RULE_*` (97–111) |
| Group membership, LUDO switch assignment | `GROUP_*` (81–88) |
| Set the lamp's clock | `DATETIME_SET` (26) |
| Firmware update, delete / unpair | DFU, `UNREGISTER` (17) |

There is **no output limit, no power profile, no battery-behaviour setting and no persistent power-on
default.** The app's command enum does define `CONFIG_SET` (5), `MODULE_PROPERTY_SET` (53),
`DEVICE_PROPERTY_SET` (67) and `HOST_PARAM_SET` (71) — the plausible homes for something like that — but **it
never calls a single one of them**, so there is no payload to imitate and no evidence the firmware implements
them.

## What follows from that

Anything the lamp does that you might want to switch off is firmware behaviour with no exposed setting, in the
app or here. **Reaching feature parity with the app is therefore not a route to changing it.** If it matters
for a particular setup, Fermob support (`support.lighting@fermob.com`) is the only route.

The app does not document the behaviour anywhere either. Its FAQ covers battery runtime — *"Mooon! can be left
on for up to 6 hours at 100 %, and up to 12 hours at 50 % brightness"* — and confirms the lamps have a ByPass
so they can run while charging, but never mentions output being reduced off the charger.

The app also reads no lamp state at all: it holds the BLE link open and consumes what the lamp volunteers,
which is what this integration does. See [STATE-MODEL.md](STATE-MODEL.md) and
[DEAD-ENDS.md](DEAD-ENDS.md#reading-light-state-back-does-not-work).
