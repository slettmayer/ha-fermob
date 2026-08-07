# The Devices

> What these lamps are, which of the two LED families each belongs to, how the integration works out which it
> is talking to, and how confident we are about each claim.

**Scope.** The device model and its detection. What the integration *exposes* per lamp is in
[ENTITIES-AND-SERVICES.md](ENTITIES-AND-SERVICES.md); the byte layouts the families differ in are in
[PROTOCOL-LIGHT-COMMAND.md](PROTOCOL-LIGHT-COMMAND.md).

## The two families

Fermob sells Bluetooth LED lamps that speak the Linkio protocol. They split into two LED families, which the
app's device-class table (`manufacturer_id 7`) keys off `module_type`:

| Family | `module_type` | Models | Controls |
|---|---|---|---|
| Dimmable white (`LIGHT_TYPE_DW`) | 401 | Hoopik GL1200 string light (`model_id` 3) | Brightness |
| Tunable white (`LIGHT_TYPE_TW`) | 404 | Every MOOON! and table lamp | Brightness **and** colour temperature |

Per that table, **the Hoopik L1200 is the only dimmable-white model**; everything else Fermob makes in this
line is tunable white.

**"Hoopik L1200" and "Hoopik GL1200" are the same lamp.** The codebase is inconsistent about it — `protocol.py`
and the options-flow label say `L1200`, the device-registry model string in `light.py` says `GL1200` — so
neither spelling is wrong when you meet it. There is only one dimmable-white model.

The family determines the byte layout of every light command, so getting it wrong means the lamp silently
ignores everything — it is a `MSG_FIRE` write with no ACK to observe.

## Confidence

| Claim | Status |
|---|---|
| Hoopik GL1200 works | Confirmed on hardware by upstream's author |
| MOOON! Moon2AD2 works (on/off, brightness, 3000↔6000 K, reconnect) | Confirmed on hardware by the PR author |
| **MOOON! H134 works on this build** (pairing, on/off, brightness, colour temperature, reconnect, unpair) | **Confirmed on hardware** on **lamp firmware 3.0.27.0**, the reference build — see below |
| The H134 reports `module_type` 404 and model `MOOON - H134` | **Confirmed on hardware** — full TLV capture in [PROTOCOL-COMMANDS.md](PROTOCOL-COMMANDS.md#module_info_get) |
| Other MOOON! sizes (H63 / Ø15 / 3×Ø15 / Ø25) work | **Inferred** — same `module_type`, same protocol, untested by anyone |
| The dimmable-white path still works | **Inferred** — unchanged code and pinned by `test_dw_payload_matches_upstream_literal`, but no Hoopik has run *this* build |

Other Fermob lamps advertising `41c13060-6def-11e5-bcde-0002a5d5c51b` may work but are untested.

### The reference firmware is 3.0.27.0

**When a doc here says "confirmed on hardware", assume lamp firmware `3.0.27.0` unless it says otherwise** —
that is what the reference H134 runs, what the vendor's release server publishes as the newest build for it, and
what everything from brightness and colour temperature to pairing, reconnect and `fermob.unpair` has been
exercised against. It is read straight off the lamp and shown on the device page (0.10.0+), so a bug report can
say which build it came from; see [FIRMWARE-UPDATE.md](FIRMWARE-UPDATE.md).

One detail worth keeping straight, because a fixture in the test suite depends on it: the lamp was on
**2.3.21.0** until the owner updated it with the vendor app in early August 2026, so the verbatim
`MODULE_INFO_GET` capture pinned in `tests/test_protocol.py` — and the TLV table it feeds — carries that older
version in `0xb5`. Everything *else* is 3.0.27.0. That the two renderings are 2.3.21.0 and 3.0.27.0, from one
formatter, is also the cross-check on the byte order.

## Lamp-family detection

Resolution order, in `light.resolve_light_type`:

1. **Explicit override** in `entry.options["light_type"]` or `entry.data["light_type"]`.
2. **`module_type` as the lamp reported it** — `entry.data["module_type"]`, mapped by
   `protocol.module_type_to_light_type` (401 → dimmable, 404 → tunable). Exact, and the normal path.
3. **Name heuristic** — `"hoop"` in the lamp's name → dimmable white; **everything else → tunable white.**

**Step 2 is only available from the second setup onwards**, because learning it takes a connection. The
sequence is: first command → `MODULE_INFO_GET` → `module_type` and `model` persisted into `entry.data` (and
into the key store) → HA reloads the entry → the entity is rebuilt with the right family. So the name
heuristic is the first-run guess, not the steady state, and it is also the fallback if a lamp reports a
`module_type` we do not recognise — deliberately, since guessing a family from an unknown value would send the
wrong payload layout.

The model cannot be read before connecting: the advertisement is rotating and encrypted. See
[DEAD-ENDS.md](DEAD-ENDS.md#the-model-is-not-in-the-advertisement).

Lamps paired before this existed never ran the handshake step that reads it, so `FermobBLEConnection`
re-requests `MODULE_INFO_GET` on reconnect until it has an answer. That is **one extra round trip per
install**, not per connect, and the lamp does answer it in GATEWAY mode.

The override still exists, and still wins over what the lamp says — the escape hatch for a lamp that reports
something wrong.

**The options-flow wording lags this behaviour.** The dropdown label is still `"Auto-detect (by name)"` and
the step description still says auto-detect "picks tunable white for every lamp except the Hoopik L1200
string light" — both describe step 3 only and omit step 2, which is the normal path. That is a user-facing
string to fix in `config_flow.py`, `strings.json` and `translations/en.json`, not a docs inaccuracy; it is
recorded here so the mismatch is not mistaken for one.

If this were ever offered upstream, the default should flip to dimmable-white-when-unknown, since upstream's
existing users all have Hoopiks. See [UPSTREAM.md](../tech/UPSTREAM.md).
