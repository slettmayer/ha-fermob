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
- **Physical button presses show up in HA** — the lamp reports its own on/off, brightness and colour temperature the moment someone presses its button, so the light entity follows the lamp rather than only the last HA command
- **Automatic check-in** — reconnects a dropped link and refreshes the battery on a timer, without turning the lamp on, so a lamp left switched off does not keep a stale reading
- **Lamp family read from the lamp** — `MODULE_INFO_GET` reports what the lamp actually is, rather than guessing from its name, with a manual override if it is ever wrong
- **Two services** — `fermob.check_in` (contact the lamp now) and `fermob.unpair` (cleanly remove it, equivalent to "Forget" in the Fermob app)
- **Two options** — lamp type and connection mode, both under **Configure**; see [Configuration](#configuration)

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

The lamp reports its level when asked, and also **pushes an update of its own
accord whenever the charger goes on or off** — so the two entities react to being
docked or lifted within about a second. Between those events the figure is still
best read as **"as of last contact"**: the scheduled check-in keeps it recent, and
the last known value is held rather than blanked when the lamp is out of range.

> **The percentage moves faster than a battery can charge or drain.** It jumps as
> soon as the charger goes on — 24 % straight to 33 % in one test, 86 % to 98 % in
> another — and settles back over the following minutes once the charger comes
> off. That is the shape of a voltage reading, not a capacity count. Take the
> trustworthy figure once the lamp has been off the charger for a while; the
> reading is stable and slow-moving in between (about 0.1 %/h measured over an
> evening held connected).

## Supported devices

| Model | Type | Hardware-tested by |
|---|---|---|
| MOOON! H134 | Tunable white | This repository's maintainer — pairing, on/off, brightness, colour temperature, battery level and charging |
| Hoopik GL1200 | Dimmable white | Upstream's author |
| MOOON! (Moon2AD2) | Tunable white | The contributor of the tunable-white support |
| Other MOOON! / table lamps | Tunable white | ⚠️ nobody — same `module_type`, so expected to work |

See [docs/domain/DEVICES.md](docs/domain/DEVICES.md#confidence) for what each of
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
fallback for an unrecognised `module_type`. A manual override beats both — see
[Configuration](#configuration).

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
6. After that the BLE link is kept open by default, so subsequent commands go out immediately — and the lamp's own button presses show up in HA. See [Configuration](#configuration) if you would rather it released the link between commands

> **Nothing appearing?** Make sure your HA instance has a working Bluetooth adapter. You can check in **Settings → System → Hardware**. If your HA server has no Bluetooth, a [Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) on an ESP32 nearby works perfectly.

### Manual addition

If the automatic notification does not appear:

1. Power-cycle the lamp to make it advertise
2. Wait ~15 seconds for HA to pick up the advertisement
3. Go to **Settings → Devices & Services → Add Integration → Fermob**
4. The lamp should appear in the list — select it and confirm

> If you see *"No Fermob lamp found"*, the lamp has not been seen by the BLE scanner yet. Power-cycle it again and retry immediately.

## Configuration

Two options, both under **Settings → Devices & Services → Fermob → Configure**.
Changing either reloads the integration; neither requires re-pairing.

| Option | Default | What it decides |
|---|---|---|
| **Lamp type** | Auto-detect (by name) | Whether the lamp is treated as dimmable white or tunable white — i.e. whether it gets a colour-temperature slider |
| **Connection** | Always connected | Whether the BLE link is held open, which is what makes the lamp's own button presses visible in HA |

### Lamp type

Leave this alone unless the lamp is detected as the wrong family. Auto-detect
asks the lamp what it is (`MODULE_INFO_GET` reports `module_type` 401 dimmable /
404 tunable) and falls back to the name only for a lamp that has never connected
or reports something unrecognised — the name heuristic treats only the Hoopik as
dimmable white. An explicit choice here beats both.

### Connection

| | Always connected (default) | On demand |
|---|---|---|
| BLE link | held open | released 30 s after the last command |
| Button presses | reported in ~1 s | **not reported** |
| Charger on/off | reported in ~1 s | picked up at the next check-in |
| Check-in runs | every 30 min | every 6 hours |
| Connection slot | permanently occupied | free between commands |

**Always connected** is the default because it is the only way HA learns what the
lamp is doing. The lamp pushes its state when it changes, but only while
something is connected, and it pushes nothing when a connection is
re-established — so a link that was down during a press has lost that press for
good. There is no query that recovers it; see [Physical button](#physical-button).

**On demand** is the pre-0.8.0 behaviour, and the reason to pick it is
**connection slots**. An ESPHome Bluetooth proxy typically allows three
simultaneous connections, and a held link occupies one of them indefinitely. If
your proxy is near its limit, or several lamps share one, handing the slot back
between commands may be worth losing press detection for.

There is deliberately no middle setting. A link held for a few minutes would give
a light that is sometimes right with no way to tell which times those were, which
is worse than either end.

The two timings behind the modes are set together rather than exposed
separately, because they interact: a check-in re-establishes the link and so
re-arms the idle timer, meaning any check-in interval shorter than the timeout
would hold the link open regardless of what the timeout said.

### What holding the link costs the battery

**Preliminary, and an upper bound rather than a measurement of the link itself.**

Measured on an H134 left dark on its stand overnight with the link held open:
**86 % → 85 % over 7.6 hours**. Least-squares fit −0.078 %/h; the reading's own
band stepped down once in 7.1 h, which is 0.14 %/h. Call it **~0.1 %/h**, so
roughly **2 % per day** — which extrapolates to somewhere between about four
and eight weeks of standby from full, depending on which of those two estimates
you take. Link uptime over the same window was 5 h 20 min continuous, with no
disconnects, reconnects or errors.

Four caveats, because this is one night's data and it is easy to over-read:

- **It is total drain, not the cost of the connection.** The same lamp was never
  measured over a comparable period with *no* connection, so how much of that
  0.1 %/h the held link is responsible for is unknown. The lamp self-discharges
  and runs its radio either way. Treat ~0.1 %/h as the ceiling on what switching
  to *on demand* could possibly save you, not as the saving.
- **The gauge is a voltage proxy, not a capacity count** — see [Entities](#entities).
  So %/h is not uniform across the charge range, and a figure taken around 85 %
  need not hold near the top or bottom of it.
- **One lamp, one run, one night, one temperature.** Nothing here has been
  repeated, and a cold balcony in winter changes both battery chemistry and BLE
  behaviour.
- **The lamp being dark is doing a lot of work.** Lit, the LEDs dominate the
  draw so completely that the link is noise by comparison — Fermob quote 6 h at
  100 % brightness, which is about 16 %/h.

Practical reading: for a lamp that lives on or near its charger, the cost is not
worth thinking about. For one stored off-charger for a season, it is still
probably not the dominant term — but the honest answer is that we have not
measured the alternative, so switch modes for the connection slot, not for the
battery.

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
[docs/domain/APP-CAPABILITIES.md](docs/domain/APP-CAPABILITIES.md).

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
>
> Switching the connection mode does **not** change this. Pairing is what confers
> ownership, not whether a link happens to be open, so *on demand* does not let
> the phone app in — you still have to unpair from HA and pair with the app.

### Physical button

**Press the lamp's button and HA follows**, in about a second — on/off, brightness
and colour temperature. The lamp volunteers its new state as soon as it changes,
and it does the same when it is put on or taken off its charger.

This works because the BLE link is **held open**. The lamp pushes only while
something is connected, and pushes nothing when a connection is re-established,
so a link that has been dropped means a press that is never seen. That is why
holding it is the default.

If you switch **Configure → Connection** to *on demand*, presses stop being
reported and the entity goes back to showing whatever HA last commanded. There is
no middle setting, deliberately: a link held for a few minutes would give a light
that is sometimes right with no way to tell when.

There is no way to ask the lamp instead. Both candidate read commands were tried
on hardware, and the manufacturer's own app sends neither:

- `DEVICE_DATA_GET` (66) is refused with error `18`. Not a payload problem — the
  body sent is byte-for-byte the official app's. **Why the firmware refuses it is
  unexplained**; earlier claims blaming gateway mode, payload size and module
  role have each been disproved.
- `DEVICES_DATA_LIST_GET` (74) *is* accepted and does reply — but with a stored
  record that never changes. It reported the lamp off while it was lit, and
  returned byte-identical data across repeated on/off cycles.

A packet capture of the official app settled it: the app builds the second
command and never sends it. It reads nothing, holds the link, and listens — which
is what this integration now does. The full traces are in
[docs/domain/LINKIO-PROTOCOL.md](docs/domain/LINKIO-PROTOCOL.md).

### Check-in service

`fermob.check_in` contacts the lamp now — reconnecting if the link has dropped
and refreshing the battery — instead of waiting for the scheduled check-in. It
never turns the lamp on or off, and a lamp that is out of range is left alone.

```yaml
service: fermob.check_in
target:
  entity_id: light.fermob_moon
```

**It cannot be called on a light that is greyed out.** Home Assistant drops
service calls aimed at an unavailable entity and still reports success, so
nothing happens. You do not need it in that case: a greyed-out lamp is contacted
again by the *scheduled* check-in and comes back on its own, within 30 minutes on
the default setting. To force it sooner, reload the integration
(**Settings → Devices & Services → Fermob → ⋮ → Reload**).

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

It checks the lamp is listening before it releases anything, and **removes
nothing if it is not** — otherwise the lamp would stay registered to a Home
Assistant that had forgotten it, which only a factory reset clears. Three
outcomes:

| What the lamp says | What happens |
|---|---|
| It answers | Released and removed, keys and all |
| Nothing comes back | Error, nothing removed — bring it in range and try again |
| It rejects our keys | Error saying it is **already** unpaired (you factory-reset it). Nothing to release; delete the integration to clean up |

An entry whose pairing never completed has nothing to release either, so it is
removed without contacting the lamp at all.

## Factory reset

If the lamp has stale keys from a previous client and won't pair:

1. **Hold the lamp's button for 10 seconds** until it flashes — this clears all stored credentials
2. Delete `.storage/fermob_*` in your HA config directory
3. Restart HA
4. Power-cycle the lamp and retry setup

## Troubleshooting

| Symptom | Fix |
|---|---|
| Lamp not discovered | Move HA closer, or use a [Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) |
| **Lamp shows as available but ignores every command, battery *unavailable*** | A dead session behind a link that still looks up. On 0.9.0+ this repairs itself — wait for the next check-in or call `fermob.check_in`. **On 0.8.0/0.8.1 it is permanent: upgrade to 0.9.0.** As a stopgap there, reload the integration |
| Lamp unresponsive right after pairing | Fixed in 0.9.0, which reconnects after pairing. On earlier versions, reload the integration once |
| *"Lamp is in PRIVATE mode but no stored keys"* | The lamp has keys from a previous client. Factory-reset the lamp (hold its button 10 s), delete `.storage/fermob_*`, restart HA. On 0.9.0+ a lamp you factory-reset *while the integration is installed* is re-paired automatically, without any of that — but see the row below |
| Lamp you factory-reset is greyed out and nothing works | Fixed in 0.9.1. On 0.9.0 the check-in correctly spotted the reset, marked the light unavailable, and Home Assistant then discarded every service call to it — including the `light.turn_on` that would have re-paired it. Reload the integration (**Settings → Devices & Services → Fermob → ⋮ → Reload**), then turn the light on |
| Re-added the integration and the lamp no longer works | Expected — but only if you *deleted* it. Deleting the integration deletes its pairing keys while the lamp stays registered to Home Assistant, so the re-add has nothing to talk to it with. Factory-reset the lamp (hold its button 10 s) and set it up again. To avoid this, release the lamp with `fermob.unpair` **before** deleting the integration |
| Re-adding a lamp released with `fermob.unpair` | Just add it again — **no factory reset needed.** The unpair told the lamp, so it is back in `NONE` and free. Pairing happens on the first toggle, so switch the light on once after adding it (confirmed on hardware, 2026-08-06) |
| Lamp flashes 3× on toggle | The lamp is being unregistered. Use the `fermob.unpair` service instead of toggling, then re-pair |
| Pairing timeout | Ensure the official Fermob app is not connected to the lamp |
| Turning the lamp on takes ages before failing | Expected when it is out of range or asleep. Bluetooth allows 20 s per connect attempt and 0.9.2 halved the attempts a command makes, from four to two — but there is no fixed ceiling: some Bluetooth errors retry on their own separate budget, and a command also has to wait for any check-in already in progress. Bring the lamp into range rather than timing it |
| Battery entities unavailable for a while after restarting HA | The lamp reports its battery only when asked, and the first check-in runs one minute after startup (two minutes before 0.9.2) to let the Bluetooth stack come up. Commands are **not** affected — the lamp is controllable straight away |
| Light greyed out and `fermob.check_in` does nothing | Home Assistant discards service calls to an unavailable entity and still reports success. Wait for the next scheduled check-in, which contacts the lamp regardless and restores it (30 minutes on the default setting), or reload the integration to force it now |
| Physical button not reflected in HA | Check **Configure → Connection** is *Always connected*; on demand releases the BLE link, and the lamp reports presses only while connected. Otherwise the link has dropped — `fermob.check_in` restores it, and the scheduled check-in does so within 30 minutes |
| Battery reads `unavailable` | The lamp has not reported a level yet. It answers on connect, so this clears at the next check-in or the next lamp command |
| Battery percentage looks too high | Expected on and just off the charger — see [Entities](#entities). Read it once the lamp has been off the charger a while |
| Lamp holds a Bluetooth connection permanently | By design — it is what makes button presses visible. Switch **Configure → Connection** to *On demand* to free the slot, at the cost of press detection |
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
| [docs/domain/](docs/domain/README.md) | The lamps, the entities, the state model, the Linkio BLE protocol, pairing, and the dead ends |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development cycle, versioning policy, changelog format, protocol-change obligations |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

## Development

```bash
pip install -r requirements_test.txt
python -m pytest tests/ -q            # 987 tests, seconds to run
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