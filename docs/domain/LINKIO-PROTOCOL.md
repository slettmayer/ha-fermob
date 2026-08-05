# The Linkio BLE Protocol

> How Fermob lamps are actually driven. Everything here maps to `custom_components/fermob/protocol.py`.

**Confidence:** the framing, crypto and handshake below were reverse-engineered from the official Fermob
Lighting app's protocol JS by [@edouardrosset](https://github.com/edouardrosset) (dimmable white) and
[@fjcompiled](https://github.com/fjcompiled) (tunable white), and confirmed working against real hardware by
each of them. **We have not independently verified any of it against the app.** Where a detail is inferred
rather than observed, it says so.

## Transport

| | |
|---|---|
| Advertisement service UUID | `41c13060-6def-11e5-bcde-0002a5d5c51b` (the `bluetooth:` matcher in `manifest.json`; advertised in an *incomplete list* of 128-bit service UUIDs, AD type `0x06`) |
| GATT service UUID | `41c15000-6def-11e5-bcde-0002a5d5c51b` — **not the advertisement UUID**; only reachable post-connection, so useless for discovery. **Confirmed** by enumerating a real H134's GATT table (2026-08-02) |
| Manufacturer data | company `0x04AA`, rotating/encrypted payload |
| Write/notify characteristic | `00005002-0000-1000-8000-00805f9b34fb` (app's `LINKIO_TXRX_CHARACTERISTIC`) |
| Connections | **one client at a time** — see [PAIRING.md](PAIRING.md) |

Because the advertisement payload is rotating and encrypted, **the lamp model cannot be identified before
pairing.** That is the whole reason lamp-family detection is a name heuristic plus a manual override rather
than a lookup.

## Frame layout

Every frame is exactly 20 bytes:

```
[0]      header  = (msg_type << 5) | (encryption << 3) | frame_type
[1]      cmd_id  — our rolling sequence number, echoed in the ACK
[2..3]   short address (b2/b3) for mesh frames, 0 otherwise
[4..19]  encrypt( [crc] + payload padded to 15 bytes )
```

- **`crc`** is a plain XOR fold over the 15 padded payload bytes (`protocol.crc`).
- **Padding** (`protocol.pad15`) appends one `0x00` terminator, then `0xFF` filler, to 15 bytes. A payload of exactly 15 bytes gets no terminator.
- **Payloads longer than 15 bytes** fragment into 15-byte chunks (`protocol.build_long`): frame type `3` for the first fragment and `6` for continuations, with `[2]` = fragment index and `[3]` = fragment count.

### Message types

| Name | Value | Direction | Meaning |
|---|---|---|---|
| `MSG_FIRE` | 0 | out | Command with **no ACK** — fire and forget |
| `MSG_CMD` | 1 | out | Command the lamp must acknowledge |
| `MSG_CMD_ACK` | 2 | in | The lamp's acknowledgement of one of our commands |
| `MSG_STATUS` | 3 | in | Lamp state, pushed in reply to a query |
| `MSG_EVENT` | 4 | in | Lamp state, pushed unsolicited |

**The message type does not determine the frame type.** They are independent: the message type says whether
the lamp must acknowledge, the frame type says how the frame is addressed — SHORT gives `lmp_short_frame`
(2), LOCAL gives `local_short_frame` (0). `MSG_CMD` legitimately appears with both, so `build_short` takes an
explicit `addressed` flag.

This is worth stating plainly because getting it wrong is a silent failure. Until 0.5.1 this module defined
`MSG_MESH_CMD = 2` for "command with ACK, addressed via the short address" — but 2 is `MSG_CMD_ACK`, the
lamp's *reply* type. Sending it told the lamp our request was an acknowledgement, so it never answered. An
acknowledged, SHORT-addressed command is `MSG_CMD` with frame type 2, i.e. header **`0x32`** under
`ENCRYPT_PRIVATE` — not `0x52`.

An ACK is an inbound frame with message type `2` whose `[1]` equals the sequence number we sent. Anything
else is ignored. `_send_frames()` waits 3 s.

**A matching ACK is not necessarily a success.** Byte `[2]` of an ACK TLV is an `LMP_ERRORS` code; non-zero
means the command was rejected. `ack_error()` checks it and `_send_frames()` reports failure, because
otherwise a NAK body gets parsed as if it were real data — which in the handshake meant storing an error
payload as the lamp's private key.

Both `MSG_STATUS` and `MSG_EVENT` carry lamp state, and both `LMP_EVENT_DEVICE_DATA` (146) and
`LMP_STATUS_DEVICE_DATA` (147) mark it, with identical bodies — so all four combinations must be *parsed*
(`STATE_PUSH_TYPES`, `DEVICE_DATA_MARKERS`).

**Identical bodies, opposite trust.** Only **146** may be applied to an entity: it is the lamp volunteering a
change as it happens. **147** is the reply to `DEVICES_DATA_LIST_GET` (74) and is a *stored* record — on an
H134 it reported the lamp off while it was lit. Nothing sends 74 any more, so a 147 should never arrive;
`_dispatch_event` refuses it explicitly anyway, because "the bodies look the same, accept both" is precisely
the mistake that pair of constants exists to prevent.

### Encryption

| Mode | Value | Key |
|---|---|---|
| `ENCRYPT_NONE` | 0 | — (plaintext) |
| `ENCRYPT_PUBLIC` | 1 | the lamp's public key |
| `ENCRYPT_PRIVATE` | 2 | the negotiated private key |

The scheme is a **keystream XOR, not a block cipher over the data**: the 16-byte nonce is AES-ECB encrypted
under the chosen key, and the result is XORed over the 16-byte body. It is therefore symmetric — the same
function encrypts and decrypts (`protocol.crypt`).

We use `cryptography` for this, not pycryptodome. See [TECH-STACK.md](../tech/TECH-STACK.md#the-aes-dependency).

## Commands

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
| `CMD_MODULE_INFO_GET` | 48 | TLV list; we read the short address and API version |
| `CMD_DEVICE_INFO_GET` | 50 | Optional info, response ignored |
| `CMD_DEVICE_DATA_SET` | 65 (`0x41`) | **Set the light** |
| `CMD_DEVICE_DATA_GET` | 66 | Read lamp state — refused by the H134 with error `18`, cause unknown; **not sent**, see Dead ends |
| `CMD_DATETIME_SET` | 26 (`0x1A`) | Set the lamp's own clock (JS `setModuleTime`). Sent at the end of pairing and on every connection, as the app does |
| `CMD_DEVICES_DATA_LIST_GET` | 74 (`0x4A`) | A state read the app *builds and never sends* — accepted by the H134, but returns a frozen record; **not sent**, see Dead ends |

The app's full command enum runs to 60-odd entries; the ones above are what this integration sends. The
complete table is in the APK analysis. Two worth knowing about because they are the only route to data we
cannot otherwise get:

| Constant | Value | Purpose |
|---|---|---|
| `LMP_COMMAND_MODULES_BATTERY_LEVEL_GET` | 44 (`0x2C`) | **Battery level and charging flag.** Payload `[3, 44, addr_lo, addr_hi]`; `255, 255` broadcasts to every module. The reply carries `LMP_PARAM_BATTERY_LEVEL` (192 / `0xC0`) — one byte, `percent = b & 0x7F`, `charging = b & 0x80`. The app polls it every 20 s and lists the H134 as battery-powered. **Implemented since 0.6.0** — it is an acknowledged mesh command, so it depends on the `0x32` header fix. The ACK carries no value (a bare `[2, 0x80, 0x00]` success); the reading arrives separately as a `STATUS` push with payload `[2, 0xC0, byte]`, confirmed on an H134 |
| `LMP_COMMAND_MODULE_BATTERY_STATUS_GET` | 45 | Defined in the app but never called; unknown whether the firmware implements it |

**Reading the battery needs a connection and nothing else — no light command, and no lit lamp.** Worth stating
plainly because it is what makes the scheduled check-in possible. In the app, `requestModuleBatteryState` sends
the one frame and reads the reply; the poll loop that drives it (`startPeriodicBatteryStatusRequest`) gates only
on an internal flag, a non-empty module list, loop bounds and `m_module_role !== LEAF` — **nothing about light
state**, and it runs on a timer with every lamp dark. The app's connect routine sends nothing at all: no wake,
no state read, and no keep-alive exists anywhere (`HEARTBEAT_REQ` 42 and `CONNECT_CHECK` 34 are defined and
never sent). Its cadence is `m_intervalStatusRequest` = 20 s between modules with a 30 s ACK timeout, alternating
a light pass and a battery pass, so a single-lamp install is polled roughly every 40 s while the app is open.

Two related commands are **dead** in the app — defined in `CODES` and never called: `MODULE_PROPERTY_GET` (54)
and `DEVICE_PROPERTY_GET` (68), alongside 45 above. `requestLatestsModulePeriodicStatuses` (a `LOCAL`-addressed
`[3, 44, 255, 255]` form) is reachable in principle but no UI path calls it. So there is no unexplored
battery or state route left in the app.

### MODULE_INFO_GET

`CMD_MODULE_INFO_GET` returns a TLV list — each entry `[length, type, ...value]`, where `length` counts the type
byte plus the value. `protocol.iter_tlv` walks it and `protocol.parse_module_info` picks out four fields into a
`ModuleInfo` named tuple.

The table below is the **complete** TLV set of a real MOOON! H134, captured 2026-08-02. It is pinned verbatim as
`H134_MODULE_INFO` in `tests/test_protocol.py` — the only hardware-derived expectation in that suite.

The names come from the app's own `parseModuleInfoData`, so they are the manufacturer's, not ours. Four rows
that were "unidentified" when this table was captured are named as of the APK analysis (2026-08-03).

| Type | Value on the H134 | What it is |
|---|---|---|
| `0x80` | `00` | `LMP_STATUS_ACK` — status; always zero here |
| `0xaf` | MAC, `07`, `000000`, `9401`, `04` | `LMP_PARAM_MODULE_REFERENCE` — composite: address, manufacturer_id **7**, module_type. The app defines it but never parses it, so the internal layout is ours to guess; don't rely on it |
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
[OVERVIEW.md](OVERVIEW.md#lamp-family-detection). The API version is parsed and returned but still not branched
on by anything.

**The lamp answers this command in GATEWAY mode**, promptly (~1.5 s including the connect), which is *not* true
of `DEVICE_DATA_GET`. That is why the family read can happen on a plain reconnect rather than only during
pairing.

**`DEVICE_INFO_GET` (50), by contrast, returns nothing usable** — the observed response is `02 80 00 00`, a
zero status TLV then the terminator. Do not expect device details from it.

## The light command

Both families use `DEVICE_DATA_SET` (`0x41`) as a `MSG_FIRE` frame under `ENCRYPT_PRIVATE`, addressed with
the short address, and both use `led_mode = LEDS_MODE_COLOR = 1`, so the on-byte is identical:

```
on_byte = (1 if on else 0) | (led_mode << 4)     # 0x11 on, 0x10 off
```

They differ **only** in the body:

```
Dimmable white (Hoopik L1200):  [6, 0x41, dev, on_byte, level,             fade_lo, fade_hi]
Tunable white  (every MOOON!):  [7, 0x41, dev, on_byte, cold_white, warm_white, fade_lo, fade_hi]
```

The leading byte is the body length, `dev` is the device index (always `0` for a single lamp), and `fade` is
`FADE = 50` ms little-endian (the app's `fade_timing_10.color_transition`).

**Sending the 6-byte dimmable-white body to a tunable-white lamp does nothing** — that was the entire MOOON!
bug. Because it is a no-ACK FIRE write, there is no error to observe; the lamp simply ignores it. (Upstream
issue #1 reported a `GATT Protocol Error: Unlikely Error` on the write instead, which does not match the
silent-drop explanation given in the PR. Both accounts come from the same author and we cannot reproduce
either, so treat the precise failure mode as unsettled — only the fix is confirmed.)

### Tunable-white mixing

Colour temperature is expressed as a ratio between two intensity channels whose **sum is the total output**:

```
warm_white = round(brightness% × warm_ratio)
cold_white = brightness% − warm_white
warm_white + cold_white == brightness%
```

`warm_ratio` spans the 3000 K – 6000 K envelope: `1.0` is 3000 K (all warm), `0.0` is 6000 K (all cold).
`protocol.kelvin_to_warm_ratio` and `warm_ratio_to_kelvin` are exact inverses at every integer Kelvin in the
envelope, and both clamp outside it.

**The mapping is linear in mired, not in Kelvin** — mired being 10⁶/K. Two fixed-CCT emitters mixed at some
ratio land at the ratio's position in *reciprocal* colour temperature, so an even mix of a 3000 K and a 6000 K
channel is **4000 K**, not the arithmetic mean 4500 K:

| Kelvin | 3000 | 3750 | 4000 | 4500 | 5000 | 6000 |
|---|---|---|---|---|---|---|
| `warm_ratio` | 1.0 | 0.6 | 0.5 | ⅓ | 0.2 | 0.0 |

Up to and including 0.5.0 this interpolated Kelvin directly, which overstated the temperature everywhere
strictly between the endpoints — worst at a 4727 K slider, where the lamp actually emitted about 4212 K, a
515 K error. `test_mix_is_linear_in_mired` pins round mired fractions specifically so a revert to Kelvin-linear
interpolation fails rather than merely looking slightly off.

One caveat on the physics: mired-linearity assumes the two channels put out **equal luminous flux at equal
drive percent**. Fermob publishes no per-channel flux figures, so if the warm and cold LEDs differ in
efficacy the true midpoint shifts toward the brighter channel. Mired is the correct model absent that data,
and it is a large improvement on Kelvin-linear either way, but it is not calibrated against a meter.

A consequence worth knowing: at very low brightness the split quantises hard, and **which way it skews
alternates**, because `warm` is computed with Python's `round()` — which is half-to-even, not half-up. At mid
colour temperature (`warm_ratio = 0.5`): `level = 1` gives `cold = 1, warm = 0` (skews **cold**, since
`round(0.5) == 0`), while `level = 3` gives `cold = 1, warm = 2` (skews warm). That is inherent to expressing
temperature as two integer percentages, not a bug in the conversion.

Exact splits *are* pinned in a few places — `test_tw_payload_layout` fixes `cold = 50, warm = 50` at 100 % /
4000 K, and `test_tw_extremes_are_single_channel` fixes both endpoints — so a gross rounding change (a switch
to `floor`, or an off-by-one) fails CI immediately. What is **not** covered is the **tie-breaking rule**: none
of the pinned cases lands on a `.5` boundary (50.000…, and the 0/80 extremes, are not ties — see below), so
swapping half-to-even for half-up would keep the suite green while changing behaviour at low brightness.
Verify ties by hand if you touch this.

A worked example of why the ties are so slippery, from the mired change itself: `kelvin_to_warm_ratio(4000)`
returns `0.5000000000000001`, not `0.5`, because 4000 K is not exactly representable as a ratio of the two
mired endpoints. That hair of float error is enough to *escape* the tie, and it flips the low-brightness skew
relative to a literal `0.5` — at `level = 1`, `DEFAULT_KELVIN` gives `warm = 1, cold = 0` while an exact
`warm_ratio = 0.5` gives `cold = 1, warm = 0`. Both are defensible at one percent of output; the point is that
the tie-break here is decided by floating-point representation rather than by any rule, so do not treat either
skew as a specified behaviour.

## Inbound state

`protocol.parse_device_record` reads a device-data push, identified by `payload[1]` being either marker in
`DEVICE_DATA_MARKERS` (146 or 147). `parse_device_state` is the same thing without the timestamp, for callers
that do not care. Whether a parsed record is *believed* is decided by the marker, in `_dispatch_event` — see
Message types above.

```
payload[1]        146 = LMP_EVENT_DEVICE_DATA, 147 = LMP_STATUS_DEVICE_DATA (identical bodies)
payload[2]        dev_index — routes to a sub-device; always 0 for a single lamp
payload[3..6]     update timestamp, little-endian uint32 (see below)
payload[7]        status — must be 0, else we reject the frame
payload[8] & 0x0F is_on  (the high nibble carries led_mode, so mask it)
payload[8] >> 4   led_mode — not read yet; tells us whether a timer or effect is running
payload[9]        level (dimmable white) / cold_white (tunable white)
payload[10]       warm_white (tunable white); defaults to 0 if the payload is only 10 bytes
payload[11..14]   nothing. Not filler from the lamp — this is our own pad15 output
```

The caller interprets the two channel bytes according to its configured family — `protocol.py` does not know
which lamp it is talking to.

**Bytes 11–14 are confirmed empty.** Every device parser in the app — dimmable white, tunable white, RGBW,
temperature and the generic fallback — stops at byte 10. Nothing is hiding past `warm_white`.

**The timestamp at 3–6 is logged and nothing more.** The app uses it as a stale-frame guard, dropping any
frame older than the last it saw. We do not, and should not: the trust decision is the marker, and a
timestamp comparison against a lamp clock we cannot verify risks silently freezing state updates forever —
far worse than the stale frame it would prevent. It is carried on `DeviceRecord` purely as diagnostics,
because it is the only outside evidence that `DATETIME_SET` reached the lamp. An H134 that had never been
sent one stamped every record `37`.

A date guard was tried, in the form of a `STATE_RECORD_MIN_TIME` floor below which a record was not believed.
It was removed: it was a proxy for the marker check, which is exact, and it would have silently discarded
legitimate pushes from a lamp whose clock had not been set.

## Dead ends — do not re-litigate these

- **Reading light state back does not work on an H134 — settled on hardware 2026-08-03, both candidate commands tried.** This entry replaces two earlier wrong versions: first "gateway mode refuses the query" (the frame was simply malformed, message type 2 = `CMD_ACK`), then "the body is the wrong size" (it is not — see below). The header fix, and accepting the `MSG_STATUS`/marker-147 reply form, were both real and are kept; they are what made the probe legible.

  **`DEVICE_DATA_GET` (66) is rejected with error 18.** Not because our body was wrong: the app's `requestModuleLightState` sends `[14, 66, 0]` + twelve `0xFF`, which is byte-for-byte what we sent. **Why the firmware refuses it is unexplained.** Do not reshape the payload; that was tried and it is not the problem.

  Two proposed explanations have already been ruled out, so do not re-propose them. It is *not* the module role: the app's poll loops skip only modules whose `m_module_role === LEAF` (6), and this lamp reports `LMP_PARAM_ROLE = 0` (`NODE`) in the capture pinned at [`tests/test_protocol.py`](../../tests/test_protocol.py) — the app would have polled it. It is also not the payload length, per the byte-for-byte match above. Note too that **`18` is not in the app's `lmp_error_codes_e` table at all** (it stops at 20 `ITEM_NOT_FOUND`, with no 18); the name `INVALID_SIZE` is this integration's own invention in `protocol.py`, not the manufacturer's, and it should not be read as the firmware telling us anything about size.

  **`DEVICES_DATA_LIST_GET` (74) is the app's real state read, and the lamp accepts it.** Sent by `requestLatestsModuleStatuses` as `CMD_WITH_ACK` + SHORT with body `[12, 74, 255, 255, dev_index, 0,0,0,0, <local time, LE uint32>]`; the direct-connection form puts the short address in bytes 2–3 instead. Both forms were verified on the H134: success ACK, followed by a `DEVICE_DATA` push (`mt=4`, marker 147) — the whole path works end to end, and `parse_device_state` reads it correctly.

  **But the record it returns is frozen, which is why nothing sends it.** Eight reads across ~5 minutes and three on/off cycles came back byte-identical — `0a9300250000000010191900ffffff` → `is_on=False, ch1=25, ch2=25` — *including* reads taken while the lamp was lit, and including the bytes at 3–6 that the app treats as a timestamp. The channel values never track what we actually commanded either (adaptive lighting varies them continuously; the record says 25/25 forever).

  **The clock hypothesis was tested, and it is wrong.** Those timestamp bytes read `0x25` = **37**, where `getLocalTime()` produces a Unix-scale seconds value, so the lamp's clock plainly never started — it is set by `LMP_COMMAND_DATETIME_SET` (26), payload `[5, 26, <local time, LE uint32>]` as `CMD_WITH_NO_ACK` + SHORT + PRIVATE, which this integration never sent. Sending 26 before 74 was tried on hardware. The record stayed frozen. We still send 26, because the app does and the lamp keeps those records for it, but it buys us nothing.

  An earlier hypothesis — that the record only follows an *acknowledged, addressed* `DEVICE_DATA_SET` where we use `MSG_FIRE` — is **refuted**: the app's own `Module.sendCommand` sends `DEVICE_DATA_SET` as `CMD_WITH_NO_ACK`, exactly as we do. ACK-vs-FIRE is not the difference.

  **And the whole question is moot, settled by a decrypted capture of the app's own BLE traffic (2026-08-04): the app never sends 74 at all.** `requestLatestsModuleStatuses` builds the command; nothing in the capture transmits it. The app reads no lamp state, ever. It holds the BLE link open and consumes the pushes the lamp volunteers — which is now what this integration does. Do not revive either read command.
- **The lamp emits no EVENT after a plain BLE reconnect** — only the post-`REGISTER_END` EVENT during first pairing arrives unsolicited. This is *why* the link is held open rather than re-established on demand: there is no resync, so a link that was down during a button press has lost that press permanently.

  **What the lamp does push, while connected, is everything we need** — confirmed in the same capture and then on hardware. Every physical button press produces an unsolicited `EVENT_DEVICE_DATA` (marker 146) carrying the correct on/off and both channels; every charger connect or disconnect produces a battery push (`0xC0`). A captured press decodes as `0a9200e868726a00110032` → on, cold 0, warm 50, and is pinned in [`tests/test_light.py`](../../tests/test_light.py) as the one piece of inbound evidence that is not a restatement of our own encoder.

  Held-link cost, measured on an H134 over 7.6 h: about **0.1 %/h** of battery (least-squares −0.078 %/h; band-shift 0.14 %/h), roughly 2 %/day, with 5 h 20 min of continuous uptime and no disconnects, no reconnects and no errors. The real cost is a connection slot on the adapter or BLE proxy, which is what the on-demand connection mode exists to hand back.
- **The post-`REGISTER_END` EVENT's state payload is useless to us.** Connections are only ever established *from* a command, so whatever state it reports is overwritten a few milliseconds later by the command that triggered the connection. The EVENT is still waited for — as a gateway-mode confirmation and settle gate — but its contents are only logged.
- **The model is not in the advertisement.** It rotates and is encrypted, so `module_type` (401 dimmable / 404 tunable) cannot be sniffed *before* pairing — a branch that attempted this was removed as dead code. It **is** readable after connecting, from `MODULE_INFO_GET`; that is a different question and it is now answered.
- **There is no battery level in the GATT table, and none in `DEVICE_INFO_GET`.** Both were checked on hardware — see the GATT table in [TECH-STACK.md](../tech/TECH-STACK.md#bluetooth). That part stands, and it was never the right place to look: **battery is a module-level command, `MODULES_BATTERY_LEVEL_GET` (44)**, documented under Commands above. Of the three candidates this entry used to list, "past byte 10" is now positively ruled out and "commands absent from our constant list" was the right one.
- **Brightness that feels capped or inert at the top of the slider is the lamp's own power management on battery, not a mapping bug.** Two separate observations, one mechanism. First, "brightness does nothing between 100 % and 20 %" — reported on an H134 at ~24 % charge: the top of the slider felt inert, with the whole perceptible range crammed into roughly 20 % down to 1 %. Re-tested at 33 % and on the charger, 3000 K and 6000 K both showed a clear 20 %-vs-100 % difference. The likely mechanism is the lamp current-limiting its LED driver as the cell sags, so every high setting clamps to whatever the battery can actually deliver. Check the state of charge before suspecting the code.

  Second, **output is capped by simply being off the charger, at a healthy state of charge.** Observed on an H134:
  switched on at 100 % while sitting on its stand, then lifted off it — output drops to roughly half and stays
  there. Same mechanism as above, no cell sag required. **There is nothing to send about it.** The official app
  has no setting for it and does not document it: its FAQ covers runtime ("Mooon! up to 6 hours at 100 %, 12
  hours at 50 %") and the ByPass that lets the lamp run while charging, but says nothing about reduced output
  on battery — and the app has no lamp-configuration surface at all, see
  [OVERVIEW.md](OVERVIEW.md#what-the-official-app-can-configure--and-what-it-cannot). The app is charger-blind
  by construction, too: `charging` appears only where it parses the battery byte and where it draws a
  lightning-bolt icon, `isH134()` only picks a CSS image container, and its light maths —
  `cold = ⌊brightness/100 × (100 − heat)⌋`, `warm = ⌊brightness/100 × heat⌋` — carries no cap and no charger
  term, exactly like ours. So parity with the app buys nothing here. Derived from the decompiled app
  (2026-08-04); the firmware mechanism itself is inferred, not observed.

  Nothing in the brightness path is temperature-asymmetric, which is the other reason to look at the battery first: `warm = level × warm_ratio`, `cold = level − warm`, so the **total drive is 100 units at full brightness for every colour temperature**. A corollary worth knowing before designing a test — comparing 4000 K against 6000 K at full brightness proves nothing, because our model sends the same total in both cases. That comparison was tried and it cannot discriminate anything.

  Related but *not* a defect: the app's dimmable path (`sendGroupDimmableLightCommand`) sends `cold = warm = n`, both strings at once, so the lamp can emit roughly twice what we ever ask for at neutral white. We spend a fixed budget across the two channels instead, which is why 4000 K at full is no brighter than 6000 K at full even though two strings are lit. That is a deliberate consequence of treating brightness as a budget, and changing it would alter the brightness-versus-temperature relationship users are accustomed to. Revisit only if more output at mid temperatures is actually wanted.

  A brief flicker when the colour temperature changes is expected: `FADE` is 50 ms, taken from the app's `fade_timing_10.color_transition`, and a temperature change moves both channels at once.
