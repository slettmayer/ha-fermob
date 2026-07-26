# Battery Probe — Debug Session Notes

> **Scratch branch `scratch/battery-probe-diagnostics`. Not for merge.** This patch exists to answer one
> question: *does a Fermob lamp hand us a state of charge anywhere?* It ships no entity and changes no lamp
> behaviour — it only logs. Delete it, or turn it into a real sensor, once the answer is in.

## The question

MOOON! lamps are rechargeable and portable, so a charge level exists inside the device. Whether it is
*reachable over the interfaces we speak* is unknown — nothing in the reverse-engineered protocol we inherited
mentions battery, but that JS was only ever mined for the light path, so its silence is not evidence.

**Confidence: everything below is unverified.** No claim here has been seen on hardware. This document
records what to look at, not what is true.

## The four places it could be hiding

| # | Candidate | Why it is plausible | What the probe does |
|---|---|---|---|
| 1 | SIG Battery Service `0x180F`, char `0x2A19` | The standard place. We would never have noticed it: the integration writes straight to the Linkio characteristic and has never enumerated the GATT table | Dumps every service/characteristic, then reads `0x2A19` if present |
| 2 | `CMD_DEVICE_INFO_GET` (50) response | Sent during pairing, response discarded entirely — a whole message we have never looked at | Logs it during pairing *and* re-sends it on every connect |
| 3 | Unparsed `CMD_MODULE_INFO_GET` (48) TLVs | We consume exactly two TLV types (`0xb1` short address, `0xb8` API version) and skip the rest of the list without logging | Logs the full payload plus every TLV type we do not consume |
| 4 | Non-`DEVICE_DATA` EVENT frames | `_dispatch_event` returns early on any `pl[1] != 146`, silently. An unsolicited low-battery event would vanish here | Logs every other event type, with payload |

Also read, since the GATT table is open anyway: Device Information Service strings (model, serial, firmware,
hardware, manufacturer). Those are a free side-answer to a *different* known problem — lamp-family detection
is currently a name heuristic because the advertisement is encrypted, but a model string read over GATT after
connect would not be. Out of scope here; note it if it shows up.

## Running the session

1. Copy `custom_components/fermob/protocol.py` and `custom_components/fermob/light.py` from this branch over
   your HA install, then restart HA (or reload the integration).
2. No logger configuration needed. The probe logs at `WARNING` under
   `custom_components.fermob.light.battery_probe`, so it lands in the default log. Grep for `PROBE`.
3. Toggle the lamp on and off. Each connect triggers one full probe pass. The 30 s idle disconnect means the
   next toggle probes again.
4. Repeat when the lamp is visibly low on charge — ideally near-empty. **One reading proves nothing**; the
   signal is a byte that *moved down* while its neighbours held still.

`byte_table()` renders payloads as fixed-width `index=decimal` pairs precisely so two dumps from different
charge levels can be diffed by eye or with `diff`.

### Expected costs and how to cut them

- `_PROBE_LMP_COMMANDS` re-sends `MODULE_INFO_GET` and `DEVICE_INFO_GET` on every connect. Whether the lamp
  answers these in gateway mode is itself unknown — `DEVICE_DATA_GET` famously does not. If it declines, each
  costs a 3 s ACK timeout, logged as `ACK timeout`, which delays every light command by ~6 s. **That timeout
  is itself a finding — record it, then set `_PROBE_LMP_COMMANDS = False` and continue with the GATT half.**
- `_PROBE_ENABLED = False` turns the whole thing off without reverting the files.

## Reading the results

- **A byte in 0..100 that tracks discharge** → that is the sensor. Confirm across at least three charge levels
  before believing it.
- **A byte in the 3000..4200 range** (or a little-endian pair) → millivolts, not percent. Usable, but it needs
  a discharge curve to become a percentage, and a raw-voltage sensor is the honest first ship.
- **Nothing moves** → the lamp does not tell us. Record that in `docs/domain/OVERVIEW.md` as a closed dead end
  (the LINKIO-PROTOCOL "dead ends not to re-litigate" section exists for exactly this) and delete the branch.

## If it works: what the sensor looks like

- New `sensor` platform, one `SensorEntity` per entry, `device_class: BATTERY`,
  `state_class: MEASUREMENT`, `native_unit_of_measurement: %`, sharing the existing `FermobBLEConnection` and
  `DeviceInfo`.
- Value refreshed on each connect — i.e. after each light command — and restored across restarts with
  `RestoreEntity`. Unlike on/off state, a slowly-changing charge level survives the push-only model honestly.
- **Do not poll.** Waking the lamp on a timer locks the Fermob app out and drains the very battery being
  measured. See [PAIRING.md](PAIRING.md) on one-client ownership.

## What this patch touches

| File | Change |
|---|---|
| `custom_components/fermob/protocol.py` | New diagnostics section at the end: `iter_tlv`, `unknown_module_info_tlvs`, `byte_table`, `KNOWN_MODULE_INFO_PARAMS`, `BATTERY_LEVEL_UUID`, `DEVICE_INFO_UUIDS`. Pure, no HA imports, as the rest of the module. `parse_module_info` is deliberately **not** refactored onto `iter_tlv` — it is the shipping path and its exact indexing is pinned by tests |
| `custom_components/fermob/light.py` | `_probe_battery_sources` / `_probe_gatt_services` / `_probe_lmp_info` called at the end of `ensure_connected`; the pairing `DEVICE_INFO_GET` response is logged instead of discarded; non-`DEVICE_DATA` EVENT frames are logged instead of dropped. All probe failures are swallowed — a diagnostic must never break the light |
| `tests/test_diagnostics.py` | 12 tests over the pure helpers |

Nothing in the light command path changed. The probe is additive and guarded.
