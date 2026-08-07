# Domain Overview

> The index for this folder: what the domain is, the concepts it contains, the vocabulary, and the decisions
> that cut across every page. Detail lives in the sub-files — this page links, it does not restate.

## Domain classification

A **local device-control integration**. There is no business domain in the commercial sense: no users, no
tenants, no persistence beyond one key store per lamp. The domain is a **physical device and a
reverse-engineered wire protocol** — Fermob's Bluetooth LED lamps speaking Linkio, driven over local BLE with
no hub and no cloud.

Everything here is reverse-engineered. Each document marks what is verified on hardware, what comes from the
official app's JS, and what is inferred — **preserve those markers when you edit.**

## Concept catalog

| Concept | One-liner | Detail |
|---|---|---|
| **Lamp families** | Two LED families — dimmable white (Hoopik) and tunable white (every MOOON!) — sharing everything but the light-command body | [DEVICES.md](DEVICES.md) |
| **Family detection** | Override, then the `module_type` the lamp reports, then a name heuristic as first-run fallback | [DEVICES.md](DEVICES.md#lamp-family-detection) |
| **Entities and services** | One light, two diagnostic battery entities, `check_in` and `unpair`, two options | [ENTITIES-AND-SERVICES.md](ENTITIES-AND-SERVICES.md) |
| **State model** | Push-only; the held-open BLE link is the entire mechanism, and the check-in is the only reconnect | [STATE-MODEL.md](STATE-MODEL.md) |
| **Connection modes** | One option deriving two coupled timings: idle disconnect and check-in interval | [STATE-MODEL.md](STATE-MODEL.md#connection-modes) |
| **Pairing and ownership** | A one-time registration making one client the owner; one client at a time, permanently | [PAIRING.md](PAIRING.md) |
| **The protocol** | 20-byte frames, an AES-ECB keystream, and the commands built on top | [LINKIO-PROTOCOL.md](LINKIO-PROTOCOL.md) |
| **App capabilities** | The vendor app has no lamp-configuration surface at all — parity buys nothing | [APP-CAPABILITIES.md](APP-CAPABILITIES.md) |
| **Firmware update** | Signed Nordic Secure DFU from a vendor server; the app installs it, we do not | [FIRMWARE-UPDATE.md](FIRMWARE-UPDATE.md) |
| **Dead ends** | State reads, reconnect resync, the model in the advertisement, brightness limiting | [DEAD-ENDS.md](DEAD-ENDS.md) |

## Cross-cutting decisions

Four decisions shape every page, and each is easy to undo by accident:

- **Hold the BLE link open.** There is no state read that works, so a dropped link loses a button press for
  good. The link is the mechanism, not a cache. [STATE-MODEL.md](STATE-MODEL.md)
- **Only marker 146 may reach an entity.** 147 carries an identical body and a frozen record. Accepting both
  is the exact mistake that pair of constants exists to prevent.
  [PROTOCOL-INBOUND-STATE.md](PROTOCOL-INBOUND-STATE.md#marker-146-versus-147)
- **Trust the lamp over its name, and the user over both.** Family resolution is override → reported
  `module_type` → name. [DEVICES.md](DEVICES.md#lamp-family-detection)
- **Brightness is a fixed budget split across two channels.** The total drive is 100 units at full brightness
  at every colour temperature, which is deliberately unlike the vendor app.
  [PROTOCOL-LIGHT-COMMAND.md](PROTOCOL-LIGHT-COMMAND.md#tunable-white-mixing)

## Glossary

| Term | Meaning |
|---|---|
| **Linkio** | The BLE protocol these lamps speak — framing, crypto and command set. Not Fermob's own; Fermob is one vendor using it |
| **LMP** | The prefix on the manufacturer's own protocol identifiers (`LMP_PARAM_*`, `LMP_ERRORS`). Taken verbatim from the app's JS, which never expands the acronym — so neither can we |
| **Module** | The lamp's controller as the protocol sees it. A `module_type` identifies the family; `MODULE_INFO_GET` reads its properties |
| **Device** | A sub-unit of a module, addressed by `dev_index`. Always `0` for a single lamp — the distinction exists because the protocol supports multi-device modules |
| **Gateway state** | The registered state a lamp enters after `REGISTER_END`, accepting private-encrypted commands from its owner. Survives power cycles; only a reset leaves it |
| **Short address** | The two-byte address (`addr_b2`/`addr_b3`) read during pairing and used to address every later mesh frame |
| **Solicited / unsolicited** | Whether a push answers a request. Unsolicited (marker 146) is the lamp volunteering a real change, and is believed; solicited (147) is a stored record, and is refused |
| **Check-in** | The scheduled contact with the lamp that reconnects a dropped link and refreshes the battery, without touching the light |
| **Connection profile** | The idle-disconnect delay and check-in interval pair derived from the connection-mode option; never configured independently |
| **Warm ratio** | The share of the brightness budget given to the warm channel — `1.0` at 3000 K, `0.0` at 6000 K, interpolated in mired |
| **Family** | `LIGHT_TYPE_DW` or `LIGHT_TYPE_TW`. Everything family-dependent branches on these strings, never on model names or `module_type` |

## The sub-files

| Guide | Covers |
|---|---|
| [DEVICES.md](DEVICES.md) | The two families, the models, family detection, and the confidence table for every "this works" claim |
| [ENTITIES-AND-SERVICES.md](ENTITIES-AND-SERVICES.md) | The light entity, both battery entities, both services, both options |
| [STATE-MODEL.md](STATE-MODEL.md) | Push-only state, connection modes and their timings, the check-in, what holding the link costs |
| [APP-CAPABILITIES.md](APP-CAPABILITIES.md) | What the official app can and cannot configure, and why parity is not a route to anything |
| [FIRMWARE-UPDATE.md](FIRMWARE-UPDATE.md) | The DFU server, the reset-into-bootloader command, the cost and risks of installing one |
| [LINKIO-PROTOCOL.md](LINKIO-PROTOCOL.md) | Protocol index — transport, commands, the light command, inbound state |
| [PAIRING.md](PAIRING.md) | Ownership, the handshake, key storage, reconnects, unpairing, recovery |
| [DEAD-ENDS.md](DEAD-ENDS.md) | What was tried and does not work |
