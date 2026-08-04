# Fermob — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Default-blue.svg)](https://github.com/hacs/default)

Control your **Fermob Bluetooth lamps** (Hoopik GL1200, MOOON! and compatible) directly from Home Assistant, without a hub or cloud dependency.

> **This is a fork.** Upstream is [edouardrosset/ha-fermob](https://github.com/edouardrosset/ha-fermob),
> which supports the Hoopik only. This fork carries
> [PR #2](https://github.com/edouardrosset/ha-fermob/pull/2) by
> [@fjcompiled](https://github.com/fjcompiled) — MOOON! tunable-white support —
> plus a hardening pass: the AES dependency is one Home Assistant actually ships,
> the BLE link is released on unload, the entity reports unavailability, and the
> protocol layer has unit tests and CI. See [docs/tech/UPSTREAM.md](docs/tech/UPSTREAM.md)
> for exactly what changed and why.

---

## Features

- **Local BLE control** — no internet, no Fermob cloud account required
- **Auto-discovery** — HA detects nearby Fermob lamps automatically
- **Brightness control** — full dimming support via the HA light slider
- **Colour temperature** — warm↔cold white for tunable-white lamps (MOOON!), 3000–6000 K, interpolated in mired so the requested Kelvin is the Kelvin you get
- **Battery level and charging state** — for battery-powered lamps, read from the lamp itself; there is no GATT battery service to read
- **Automatic battery check-in** — refreshes the level every 6 hours without turning the lamp on, so a lamp left switched off does not keep a stale reading
- **Lamp family read from the lamp** — `MODULE_INFO_GET` reports what the lamp actually is, rather than guessing from its name, with a manual override if it is ever wrong
- **Unpair service** — cleanly remove the lamp from HA (equivalent to "Forget" in the Fermob app)
- **On-demand connection** — BLE connects automatically on first command, stays open for 30 s, then disconnects to save resources

## Entities

One device per lamp, with these entities:

| Entity | Type | Category | Notes |
|---|---|---|---|
| `light.<lamp>` | Light | — | On/off and brightness. Colour temperature too on tunable-white lamps |
| `sensor.<lamp>_battery` | Sensor (`battery`, %) | Diagnostic | State of charge as the lamp reports it |
| `binary_sensor.<lamp>_charging` | Binary sensor (`battery_charging`) | Diagnostic | On while the lamp is on its charger |

The two battery entities read **unavailable** until the lamp has reported a level
at least once, so a lamp that has never answered is never mistaken for a flat
one. They exist on every lamp; a model with no battery simply never reports one
and they stay unavailable.

Because the lamp only speaks when spoken to, the level is best read as **"as of
last contact"** rather than live. The check-in keeps that recent, and it holds
the last known value rather than blanking when the lamp is out of range.

> **The percentage reads high while charging.** It jumps as soon as the charger
> goes on — 24 % straight to 33 % in testing — which is faster than a battery can
> actually take charge, so the lamp is very likely reading voltage rather than
> counting capacity. Take the trustworthy figure once it has been off the charger
> for a while.

## Supported devices

| Model | Type | Hardware-tested by |
|---|---|---|
| MOOON! H134 | Tunable white | This repository's maintainer — pairing, on/off, brightness, colour temperature, battery level and charging |
| Hoopik GL1200 | Dimmable white | Upstream's author |
| MOOON! (Moon2AD2) | Tunable white | The contributor of the tunable-white support |
| Other MOOON! / table lamps | Tunable white | ⚠️ nobody — same `module_type`, so expected to work |

See [docs/domain/OVERVIEW.md](docs/domain/OVERVIEW.md#confidence) for what each of
those claims actually rests on.

Fermob lamps fall into two LED families that share an identical BLE
handshake and command header, differing only in the light payload:

- **Dimmable white** — the Hoopik L1200 string light (brightness only).
- **Tunable white** — every MOOON! / table lamp (brightness **and** colour
  temperature via two warm/cold channels).

The integration asks the lamp which it is: `MODULE_INFO_GET` reports a
`module_type` (401 dimmable / 404 tunable) and a model string, both of which are
stored, so a renamed lamp is not misidentified. The name heuristic — only the
Hoopik is treated as dimmable-white — remains the first-run guess and the
fallback for an unrecognised `module_type`. A manual override under
**Configure → Lamp type** beats both.

Other Fermob lamps using the Linkio BLE protocol (advertisement UUID
`41C13060-6DEF-11E5-BCDE-0002A5D5C51B`) may work but have not all been tested.

## Requirements

- Home Assistant 2024.4 or later
- A Bluetooth adapter accessible to HA (built-in, USB dongle, or an **active**
  [Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) — a
  passive proxy can see the lamp advertise but cannot connect to it)
- The lamp must **not** be actively connected to the official Fermob app during setup

## Installation

### Via HACS (recommended)

This integration is in the HACS default store, so no custom repository is needed.

1. Open HACS → search for **Fermob**
2. Click **Download**
3. Restart Home Assistant

### Manual

1. Copy the `fermob/` folder into your `config/custom_components/` directory
2. Restart Home Assistant

## Setup

### Automatic discovery (recommended)

1. Make sure your lamp is powered on and **not connected to the Fermob app**
2. **Power-cycle the lamp** (switch off, wait 2 seconds, switch on) — this triggers a burst of BLE advertisements that HA picks up
3. Within a few seconds, a notification should appear in **Settings → Devices & Services**: *"Fermob lamp detected"*
4. Click **Configure** and confirm
5. The first toggle will perform the initial BLE pairing (~4 s)
6. Subsequent toggles use fast reconnect (~1 s)

> **Nothing appearing?** Make sure your HA instance has a working Bluetooth adapter. You can check in **Settings → System → Hardware**. If your HA server has no Bluetooth, a [Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) on an ESP32 nearby works perfectly.

### Manual addition

If the automatic notification does not appear:

1. Power-cycle the lamp to make it advertise
2. Wait ~15 seconds for HA to pick up the advertisement
3. Go to **Settings → Devices & Services → Add Integration → Fermob**
4. The lamp should appear in the list — select it and confirm

> If you see *"No Fermob lamp found"*, the lamp has not been seen by the BLE scanner yet. Power-cycle it again and retry immediately.

## Usage

### Brightness

The lamp appears as a dimmable light in HA. Use the brightness slider in the UI, or set it via automation:

```yaml
service: light.turn_on
target:
  entity_id: light.fermob_hoopik
data:
  brightness_pct: 75
```

> **The lamp is dimmer on battery than on its charger, and there is no setting for it.** Take an H134 off its
> stand and the output drops to roughly half, even at 100 % and a healthy state of charge; at a low charge the
> top of the slider stops doing anything at all and the whole usable range squeezes into the bottom fifth. That
> is the lamp's own power management, not this integration — see
> [Brightness on battery](#brightness-on-battery) below.

### Brightness on battery

Nothing can turn the limiting off. Not this integration, and **not the official Fermob app either** — its
entire Settings page is a language selector, and the only things it can change about a lamp are its name, its
brightness and colour temperature, timers and schedules, group and switch assignment, its clock, and firmware
updates. The Linkio protocol does define configuration commands (`CONFIG_SET`, `MODULE_PROPERTY_SET`,
`DEVICE_PROPERTY_SET`, `HOST_PARAM_SET`) but the app never sends any of them, so there is no known way to ask
the lamp for more output and no reason to think the firmware would answer.

The app does not document the behaviour anywhere either. Its FAQ covers battery runtime — *"Mooon! can be left
on for up to 6 hours at 100 %, and up to 12 hours at 50 % brightness"* — and confirms the lamps have a ByPass
so they can run while charging, but never mentions output being reduced off the charger.

If this matters for your setup, Fermob support (`support.lighting@fermob.com`) is the only route. Established by
reading the decompiled Fermob Lighting app (3.0.2, build 1209) on 2026-08-04; details and evidence in
[docs/domain/OVERVIEW.md](docs/domain/OVERVIEW.md#what-the-official-app-can-configure--and-what-it-cannot).

### Colour temperature (tunable-white lamps)

MOOON! lamps expose a colour-temperature slider (3000 K warm … 6000 K cold):

```yaml
service: light.turn_on
target:
  entity_id: light.fermob_moon
data:
  brightness_pct: 80
  color_temp_kelvin: 4000
```

Internally the lamp mixes two intensity channels
(`warm_white = brightness% × warm_ratio`, `cold_white = brightness% − warm_white`);
the integration converts to/from Kelvin automatically.

> **One controller at a time.** These lamps can be paired to a single client.
> If the Fermob app connects, it takes ownership and HA loses control until you
> **Forget** the lamp in the app, **power-cycle** it, and re-pair in HA. Pick
> HA *or* the app, and keep the other disconnected.

### Physical button

**HA does not learn about button presses.** The lamp's state in HA is the state HA
last commanded; it keeps showing that until the next command sets the lamp — and
the entity — to a known state again. If a command fails, the entity goes
*unavailable* rather than continuing to claim a stale state.

The integration does apply any state the lamp pushes to it, so a press *while the
BLE link is up* (30 seconds after the last command) may be picked up — but the
lamp is not known to push anything in that situation, and it has not been
observed doing so. Do not rely on it.

Reading the state back on reconnect does not work either, and both candidate
commands have been tried on hardware:

- `DEVICE_DATA_GET` (66) is refused with error `18`. Not a payload problem — the
  body sent is byte-for-byte the official app's. **Why the firmware refuses it is
  unexplained**; earlier claims blaming gateway mode, payload size and module
  role have each been disproved.
- `DEVICES_DATA_LIST_GET` (74), which is what the app actually uses, *is*
  accepted and does reply — but with a record that never changes. It reported the
  lamp off while it was lit, and returned byte-identical data across three on/off
  cycles.

Applying that reply would be worse than not reading at all, so nothing sends it.
The full traces are in
[docs/domain/LINKIO-PROTOCOL.md](docs/domain/LINKIO-PROTOCOL.md).

### Unpair service

To remove the lamp from HA and reset it for use with the Fermob app:

1. Go to **Developer Tools → Services**
2. Select `fermob.unpair`, target your lamp entity
3. Call the service — the lamp will flash 3× and the integration will be removed

Or via automation:

```yaml
service: fermob.unpair
target:
  entity_id: light.fermob_hoopik
```

## Factory reset

If the lamp has stale keys from a previous client and won't pair:

1. **Hold the lamp's physical reset button for 10 seconds** until it flashes — this clears all stored credentials
2. Delete `.storage/fermob_*` in your HA config directory
3. Restart HA
4. Power-cycle the lamp and retry setup

## Troubleshooting

| Symptom | Fix |
|---|---|
| Lamp not discovered | Move HA closer, or use a [Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) |
| *"Lamp is in PRIVATE mode but no stored keys"* | The lamp has keys from a previous client. Factory-reset the lamp (hold reset button 10 s), delete `.storage/fermob_*`, restart HA |
| Lamp flashes 3× on toggle | The lamp is being unregistered. Use the `fermob.unpair` service instead of toggling, then re-pair |
| Pairing timeout | Ensure the official Fermob app is not connected to the lamp |
| Physical button not reflected in HA | Expected — the lamp does not report presses and its state cannot be read back. The next HA command puts the lamp into a known state |
| Battery reads `unavailable` | The lamp has not reported a level yet. It answers on connect, so this clears at the next check-in or the next lamp command |
| Battery percentage looks too high | Expected while charging — see [Entities](#entities). Read it once the lamp has been off the charger a while |
| Lamp dims when lifted off the charger | Expected — the lamp limits its own output on battery. No setting exists, in HA or in the Fermob app — see [Brightness on battery](#brightness-on-battery) |
| Top of the brightness slider does nothing | Same cause, worse at a low state of charge. Charge the lamp before suspecting a bug |
| Integration not loading | Check logs for `custom_components.fermob` errors |

## Debug logging

```yaml
# configuration.yaml
logger:
  default: warning
  logs:
    custom_components.fermob: debug
```

## Documentation

| | |
|---|---|
| [AGENTS.md](AGENTS.md) | Project context for humans and AI coding agents — start here |
| [docs/tech/](docs/tech/README.md) | Architecture, tech stack, conventions, testing, CI/release, upstream relationship |
| [docs/domain/](docs/domain/README.md) | The lamps, the Linkio BLE protocol, pairing and recovery |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development cycle, versioning policy, changelog format, protocol-change obligations |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

## Development

```bash
pip install -r requirements_test.txt
python -m pytest tests/ -q            # 957 tests, seconds to run
ruff check . --fix && ruff format .   # lint + format
```

`protocol.py` deliberately imports nothing from Home Assistant, which is what lets
the protocol layer be tested standalone in under a second.

`main` is protected: branch, open a PR, and let CI (`ruff`, `pytest`, `hassfest`,
`HACS`) go green — merges are squash-only. Releases are automatic: merge a bumped
`manifest.json` version plus the matching `CHANGELOG.md` section and the release
workflow tags it. See
[docs/tech/INFRASTRUCTURE.md](docs/tech/INFRASTRUCTURE.md).

## Contributing

Pull requests welcome. If you have a different Fermob model and want to add
support, open an issue with the model name and debug log output — and please read
[docs/tech/CONVENTIONS.md](docs/tech/CONVENTIONS.md) first.

Protocol claims must state whether they are verified on hardware, taken from the
official app's JS, or inferred. This project rests entirely on reverse
engineering, and a confident wrong note costs the next person hours.

## License

MIT

---

*This integration is an independent community project, not affiliated with or endorsed by Fermob.*