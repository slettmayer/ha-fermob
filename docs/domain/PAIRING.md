# Pairing, Ownership and Recovery

> The pairing model has a real consequence for households: **it decides whether Home Assistant or the Fermob app controls the lamp.** Read this before pairing anything.

## The ownership model

These lamps accept **one BLE client at a time**, and pairing is not a handshake you repeat — it is a
registration. The pairing sequence exchanges keys and ends with `REGISTER_END`, after which the lamp enters
"gateway" state and stays there permanently across power cycles and BLE disconnects.

What follows from that:

- **While Home Assistant holds the link, the Fermob app cannot connect.** We hold it for 30 s after each command, then disconnect.
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
| 10 | *(wait for EVENT)* | private | Confirms gateway mode and acts as a settle gate |

Keys are **persisted before** step 9, deliberately: if the confirming EVENT never arrives, the keys the lamp
now holds are still on disk, so we are not locked out.

Storage is `.storage/fermob_<mac_with_underscores>` in the HA config directory, holding the public key,
private key, nonce and short address as hex.

## Reconnects

Reconnecting is **just a BLE connect plus `start_notify`** — no `REGISTER`, no key exchange. The lamp keeps
its gateway state, so re-running the handshake would be wrong.

This is also why there is no state resync: see the dead ends in
[LINKIO-PROTOCOL.md](LINKIO-PROTOCOL.md#dead-ends--do-not-re-litigate-these).

## Setup prerequisites

- The lamp must be **powered on and not connected to the Fermob app**.
- **Power-cycle it** (off, 2 s, on) immediately before setup — that triggers the advertisement burst HA needs to discover it.
- HA needs a Bluetooth adapter or an **active** ESPHome Bluetooth proxy (`bluetooth_proxy: active: true`) within range. A passive-only proxy can see the lamp but cannot connect to it.
- Battery-powered/portable variants are frequently asleep or out of range. That is normal, and it is why the entity reports *unavailable* on a failed command rather than pretending its last state is current.

## Unpairing

`fermob.unpair` (an entity service) broadcasts `UNREGISTER`, then deletes the stored keys and removes the
config entry. The lamp flashes 3× and resets its crypto state to `NONE`, so it can be paired with the app
again.

## Recovery

**Symptom: "Lamp is in PRIVATE mode but no stored keys found."**
The lamp is registered to a client whose keys we do not have — the app, or an HA install whose `.storage`
entry was deleted. There is no way to talk to it in that state.

1. Hold the lamp's physical button for **10 seconds** until it flashes — this clears its credentials.
2. Delete `.storage/fermob_*` in the HA config directory.
3. Restart Home Assistant.
4. Power-cycle the lamp and set it up again.

**Symptom: the lamp flashes 3× when toggled.**
Something sent `UNREGISTER`. Use the `fermob.unpair` service deliberately rather than toggling, then re-pair.

**Symptom: pairing times out.**
The Fermob app is almost certainly connected. Close it, or move the phone out of range, and retry.
