# Pairing, Ownership and Recovery

> The pairing model has a real consequence for households: **it decides whether Home Assistant or the Fermob app controls the lamp.** Read this before pairing anything.

## The ownership model

These lamps accept **one BLE client at a time**, and pairing is not a handshake you repeat — it is a
registration. The pairing sequence exchanges keys and ends with `REGISTER_END`, after which the lamp enters
"gateway" state and stays there permanently across power cycles and BLE disconnects.

What follows from that:

- **While Home Assistant holds the link, the Fermob app cannot connect.** By default the link is never
  released; under the *on demand* connection mode it is dropped 30 s after the last command. See
  [STATE-MODEL.md](STATE-MODEL.md#connection-modes).
- **Releasing the link does not release ownership.** Pairing is what confers it, so *on demand* does not let
  the phone app in — you still have to unpair from HA and pair from the app.
- **If someone re-pairs from the app, our stored keys stop working.** Recovery is a factory reset, not a retry.
- **In practice: pick one.** After pairing to HA, treat the app as a factory-reset tool only.

## First pairing

Pairing happens lazily, on the **first command** — not when the config entry is created. The handshake
(`FermobBLEConnection._pairing_handshake`) is 10 steps:

| # | Command | Encryption | Purpose |
|---|---|---|---|
| 1 | `REGISTER(0)` | none | Probe. If the lamp answers under `ENCRYPT_PRIVATE`, it is already registered to someone else — we abort with a factory-reset instruction |
| 2 | `AUTHKEY_GET(0)` | none | Read the lamp's public key |
| 3 | `NONCE_GENERATE` | none | Lamp generates and returns the nonce |
| 4 | `CRYPT_SET(PUBLIC)` | none | Switch to public encryption |
| 5 | `AUTHKEY_GEN(1)` | public | Generate the private key |
| 6 | `CRYPT_SET(PRIVATE)` | public | Switch to private encryption |
| 7 | `MODULE_INFO_GET` | private | Read the short address used by all later mesh frames |
| 8 | `DEVICE_INFO_GET` | private | Optional; response ignored |
| 9 | `REGISTER(1)` | private | `REGISTER_END` → lamp enters gateway mode |
| 9b | `DATETIME_SET` | private | Starts the lamp's clock, where the app does it — FIRE, no reply |
| 10 | *(wait for EVENT)* | private | Confirms gateway mode and acts as a settle gate |

Keys are **persisted before** step 9, deliberately: if the confirming EVENT never arrives, the keys the lamp
now holds are still on disk, so we are not locked out.

**`ensure_connected()` then drops the link and opens a fresh one.** The lamp stops honouring the link it was
paired on once `REGISTER_END` puts it in gateway mode: on an H134, every command after pairing was accepted by
Home Assistant and silently ignored by the lamp until the integration was reloaded. A reload is a fresh
connect, so pairing now does that itself. Do not remove it as a redundant round trip — it is the difference
between a working lamp and a dead one, and the failure is invisible from Home Assistant's side because
`send_led` is a write-without-response.

Storage is `.storage/fermob_<mac_with_underscores>` in the HA config directory — the MAC is **lowercased**, so
grep for it in lower case during recovery. It holds five keys: `pub`, `priv` and `nonce` as hex strings, plus
`addr_b2` and `addr_b3` (the short address) as plain integers, not hex.

## Reconnects

Reconnecting is **a BLE connect, `start_notify` and one probe** — no `REGISTER_END`, no key exchange. The lamp
keeps its gateway state, so re-running the handshake would be wrong.

This is also why there is no state resync: see
[DEAD-ENDS.md](DEAD-ENDS.md#the-lamp-emits-no-event-after-a-plain-ble-reconnect).

### The probe: is this still our lamp?

`_lamp_still_paired()` re-sends step 1's unencrypted `REGISTER(0)` on every reconnect and reads **the
encryption mode the lamp answers in**, not the body. Anything other than `PRIVATE` means the lamp no longer
holds our keys — it was factory-reset behind our back — so the stored keys are discarded and the full
handshake runs.

This covers the **inverse** of step 1's check, and nothing else did. Step 1 catches *lamp registered, us with
no keys*. The reverse — *us with keys, lamp reset* — was a silent, permanent dead end: the reconnect path
skipped the handshake, every frame went out `PRIVATE`-encrypted to a lamp back in `NONE` mode, and the only
recovery was deleting `.storage/fermob_*` by hand. The BLE link looked perfect throughout.

**Silence is read as "still paired", deliberately.** A probe that times out proves nothing — the lamp may be
at the edge of range — and re-pairing on that evidence would flash the lamp unattended *and* throw away keys
that were still good. Only a lamp that positively answers in a non-`PRIVATE` mode is treated as reset.

## Setup prerequisites

- The lamp must be **powered on and not connected to the Fermob app**.
- **Power-cycle it** (off, 2 s, on) immediately before setup — that triggers the advertisement burst HA needs to discover it.
- HA needs a Bluetooth adapter or an **active** ESPHome Bluetooth proxy (`bluetooth_proxy: active: true`) within range. A passive-only proxy can see the lamp but cannot connect to it.
- Battery-powered/portable variants are frequently asleep or out of range. That is normal, and it is why the
  entity reports *unavailable* on a failed command rather than pretending its last state is current.

## Unpairing

`fermob.unpair` (an entity service) broadcasts `UNREGISTER`, then removes the config entry — which deletes the
stored keys with it, via `async_remove_entry`. The lamp flashes 3× and resets its crypto state to `NONE`, so
it can be paired with the app again.

**It is both halves or neither.** `UNREGISTER` is a fire-and-forget broadcast, exactly as the app sends it, so
it can never be acknowledged — but the session carrying it can be, with a battery request one command earlier.
If the lamp does not answer that, `async_unpair` raises `HomeAssistantError` and removes nothing. Deleting the
keys while the lamp stays registered produces the one state nothing recovers from except a paperclip: the lamp
owned by a controller that has forgotten it, which then reads as *"PRIVATE mode but no stored keys"* forever.

Removing the config entry by hand is equally safe now — `async_remove_entry` deletes
`.storage/fermob_<mac>` — but it does **not** tell the lamp anything, so the lamp stays registered. Use the
service when you want the lamp released; delete the entry when you want Home Assistant to forget it.

## Recovery

**Symptom: the lamp is connected and available in HA, but does not react and the battery reads *unavailable*.**
The session is dead: the link is up, `is_connected` is True, and the lamp is discarding everything. Since 0.9.0
this repairs itself — the check-in treats an unacknowledged battery request as a dead session and reconnects
(see [STATE-MODEL.md](STATE-MODEL.md#the-check-in)), so wait one check-in interval or call `fermob.check_in`.
On 0.8.0–0.8.1 it was permanent; reload the integration.

**Symptom: "Lamp is in PRIVATE mode but no stored keys found."**
The lamp is registered to a client whose keys we do not have — the app, or an HA install whose `.storage`
entry was deleted. There is no way to talk to it in that state.

1. Hold the lamp's physical button for **10 seconds** until it flashes — this clears its credentials.
2. Delete `.storage/fermob_*` in the HA config directory.
3. Restart Home Assistant.
4. Power-cycle the lamp and set it up again.

Since 0.9.0 the integration should not put you here on its own: an unacknowledged unpair keeps the keys, and
removing the entry deletes them. A factory reset performed *while* the entry exists is handled automatically by
the reconnect probe above — no manual `.storage` surgery needed.

**Symptom: the lamp flashes 3× when toggled.**
Something sent `UNREGISTER`. Use the `fermob.unpair` service deliberately rather than toggling, then re-pair.

**Symptom: pairing times out.**
The Fermob app is almost certainly connected. Close it, or move the phone out of range, and retry.
