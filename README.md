# Fermob — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

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
- **Colour temperature** — warm↔cold white for tunable-white lamps (MOOON!), 3000–6000 K
- **Physical button sync** — HA state updates when the lamp's button is pressed, while the BLE connection is active
- **Unpair service** — cleanly remove the lamp from HA (equivalent to "Forget" in the Fermob app)
- **On-demand connection** — BLE connects automatically on first command, stays open for 30 s, then disconnects to save resources

## Supported devices

| Model | Type | Hardware-tested by |
|---|---|---|
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

The integration auto-detects the family by name (only the Hoopik is treated
as dimmable-white; everything else as tunable-white). If it ever guesses
wrong, override it under **Configure → Lamp type**.

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

1. Open HACS → **Integrations** → ⋮ menu → **Custom repositories**
2. Add `https://github.com/slettmayer/ha-fermob` with category **Integration**
3. Click **Download** on the Fermob integration
4. Restart Home Assistant

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

When the lamp's physical button is pressed **while the BLE connection is active**, HA detects the state change and updates the entity automatically.

The BLE connection is active for 30 seconds after the last HA command.

**A button press after that window is not seen by HA.** The lamp emits no state
notification when a link is re-established, and it stops answering
`DEVICE_DATA_GET` once it is in gateway mode, so there is nothing to read back on
reconnect. HA keeps showing the state it last commanded until the next HA command
sets the lamp (and the entity) to a known state again. If a command fails, the
entity goes *unavailable* rather than continuing to claim a stale state.

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
| Physical button not reflected in HA | Expected if the BLE connection was idle — the press cannot be recovered. The next HA command puts the lamp back into a known state |
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
| [CHANGELOG.md](CHANGELOG.md) | Release history |

## Development

```bash
pip install -r requirements_test.txt
python -m pytest tests/ -q            # 794 tests, no Home Assistant needed
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