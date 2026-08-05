# Commands

> Every command this integration sends, the battery command that makes the check-in possible, and the
> `MODULE_INFO_GET` TLV table captured from real hardware.

**Scope.** Command identifiers and their payloads, except the light command — that has its own file,
[PROTOCOL-LIGHT-COMMAND.md](PROTOCOL-LIGHT-COMMAND.md). The commands the lamp *refuses* or answers
uselessly are in [DEAD-ENDS.md](DEAD-ENDS.md). Framing and encryption are in
[PROTOCOL-TRANSPORT.md](PROTOCOL-TRANSPORT.md).

## What we send

| Constant | Value | Purpose |
|---|---|---|
| `CMD_REGISTER` | 16 | `0` = pairing probe, `1` = REGISTER_END (enter gateway mode) |
| `CMD_UNREGISTER` | 17 | Broadcast "forget me"; lamp flashes 3× and resets its crypto state |
| `CMD_CRYPT_NONCE_GENERATE` | 19 | Lamp generates and returns the nonce |
| `CMD_CRYPT_NONCE_SET` | 21 | (defined, unused) |
| `CMD_CRYPT_AUTHKEY_GEN` | 22 | Generate the private key |
| `CMD_CRYPT_AUTHKEY_GET` | 23 | Read the lamp's public key |
| `CMD_CRYPT_AUTHKEY_SET` | 24 | (defined, unused) |
| `CMD_CRYPT_SET` | 25 | Switch the active encryption mode |
| `CMD_DATETIME_SET` | 26 (`0x1A`) | Set the lamp's own clock (JS `setModuleTime`). Sent at the end of pairing and on every connection, as the app does |
| `CMD_MODULE_INFO_GET` | 48 | TLV list; we read the short address, API version, `module_type` and model |
| `CMD_DEVICE_INFO_GET` | 50 | Optional info, response ignored |
| `CMD_DEVICE_DATA_SET` | 65 (`0x41`) | **Set the light** — see [PROTOCOL-LIGHT-COMMAND.md](PROTOCOL-LIGHT-COMMAND.md) |
| `LMP_COMMAND_MODULES_BATTERY_LEVEL_GET` | 44 (`0x2C`) | **Battery level and charging flag** — see below |

Two commands are defined in `protocol.py` and deliberately **never sent**, because hardware settled that they
do not work: `CMD_DEVICE_DATA_GET` (66) and `CMD_DEVICES_DATA_LIST_GET` (74). See
[DEAD-ENDS.md](DEAD-ENDS.md#reading-light-state-back-does-not-work).

The app's full command enum runs to 60-odd entries; the table above is what this integration sends. The
complete table is in the APK analysis.

## The battery command

`LMP_COMMAND_MODULES_BATTERY_LEVEL_GET` (44) is the only route to data we cannot otherwise get.

- Payload `[3, 44, addr_lo, addr_hi]`; `255, 255` broadcasts to every module.
- The reply carries `LMP_PARAM_BATTERY_LEVEL` (192 / `0xC0`) — one byte, `percent = b & 0x7F`,
  `charging = b & 0x80`.
- It is an **acknowledged mesh command**, so it depends on the `0x32` header fix described in
  [PROTOCOL-TRANSPORT.md](PROTOCOL-TRANSPORT.md#message-types). **Implemented since 0.6.0.**
- The ACK carries no value (a bare `[2, 0x80, 0x00]` success); the reading arrives separately as a `STATUS`
  push with payload `[2, 0xC0, byte]`. **Confirmed on an H134.**
- The app polls it every 20 s and lists the H134 as battery-powered.

**Reading the battery needs a connection and nothing else — no light command, and no lit lamp.** Worth
stating plainly because it is what makes the scheduled check-in possible. In the app,
`requestModuleBatteryState` sends the one frame and reads the reply; the poll loop that drives it
(`startPeriodicBatteryStatusRequest`) gates only on an internal flag, a non-empty module list, loop bounds
and `m_module_role !== LEAF` — **nothing about light state** — and it runs on a timer with every lamp dark.
The app's connect routine sends nothing at all: no wake, no state read, and no keep-alive exists anywhere
(`HEARTBEAT_REQ` 42 and `CONNECT_CHECK` 34 are defined and never sent). Its cadence is
`m_intervalStatusRequest` = 20 s between modules with a 30 s ACK timeout, alternating a light pass and a
battery pass, so a single-lamp install is polled roughly every 40 s while the app is open.

### Battery routes that do not exist

`LMP_COMMAND_MODULE_BATTERY_STATUS_GET` (45) is defined in the app but never called; whether the firmware
implements it is unknown. Two related commands are likewise **dead** in the app — defined in `CODES` and
never called: `MODULE_PROPERTY_GET` (54) and `DEVICE_PROPERTY_GET` (68).
`requestLatestsModulePeriodicStatuses` (a `LOCAL`-addressed `[3, 44, 255, 255]` form) is reachable in
principle but no UI path calls it. So there is no unexplored battery or state route left in the app.

## MODULE_INFO_GET

`CMD_MODULE_INFO_GET` returns a TLV list — each entry `[length, type, ...value]`, where `length` counts the
type byte plus the value. `protocol.iter_tlv` walks it and `protocol.parse_module_info` picks out four fields
into a `ModuleInfo` named tuple.

The table below is the **complete** TLV set of a real MOOON! H134, captured 2026-08-02. It is pinned verbatim
as `H134_MODULE_INFO` in `tests/test_protocol.py` — the only hardware-derived expectation in that suite.

The names come from the app's own `parseModuleInfoData`, so they are the manufacturer's, not ours. Four rows
that were "unidentified" when this table was captured are named as of the APK analysis (2026-08-03).

| Type | Value on the H134 | What it is |
|---|---|---|
| `0x80` | `00` | `LMP_STATUS_ACK` — status; always zero here |
| `0xaf` | MAC, `07`, `000000`, `9401`, `04` | `LMP_PARAM_MODULE_REFERENCE` — composite: address, manufacturer_id **7**, module_type. The app never parses it; the layout is a guess |
| `0xb4` | `9401` | **`LMP_PARAM_MODULE_TYPE`** — little-endian uint16, `404` |
| `0xb5` | `00 02 03 15` | `LMP_PARAM_MODULE_SW_VERSION`. The app reads it **reordered** as `[v3, v4, v5, v2]`, i.e. `02 03 15 00` here — so the leading `00` is the last component, not the first |
| `0xb6` | `01 00 00` | `LMP_PARAM_MODULE_HW_VERSION` — read in order as `[v2, v3, v4]` |
| `0xb8` | `02` | `LMP_PARAM_MODULE_API_VERSION` |
| `0xc1` | `00` | `LMP_PARAM_ROLE` — `0` is not `LEAF` (6), which is what the app requires before polling a module |
| `0xb9` | `00` | `LMP_PARAM_DEV_LIST_CHANGEABLE` |
| `0xb0` | MAC | `LMP_PARAM_MAC_ADDRESS` — full address, little-endian |
| `0xb1` | `757e` | **`LMP_PARAM_SHORT_ADDRESS`** |
| `0xb2` | `Fermob` | `LMP_PARAM_MANUFACTURER_NAME`, NUL-padded to 16 |
| `0xb3` | `MOOON - H134` | **`LMP_PARAM_MODEL_NAME`** — NUL-padded to 16 |
| `0xb7` | `Moon7E75` | `LMP_PARAM_MODULE_NAME` — the lamp's own device name |

Both `0xb5` and `0xb6` are already in a response we make on every reconnect, so surfacing firmware and
hardware version in the HA device registry costs no extra round-trip. Not done yet.

`module_type` (`0xb4`) and the model string (`0xb3`) are what make lamp-family detection exact — see
[DEVICES.md](DEVICES.md#lamp-family-detection). The API version is parsed and returned but still not branched
on by anything.

**The lamp answers this command in GATEWAY mode**, promptly (~1.5 s including the connect), which is *not*
true of `DEVICE_DATA_GET`. That is why the family read can happen on a plain reconnect rather than only during
pairing.

**`DEVICE_INFO_GET` (50), by contrast, returns nothing usable** — the observed response is `02 80 00 00`, a
zero status TLV then the terminator. Do not expect device details from it.
