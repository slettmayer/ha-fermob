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

| Name | Value | Frame type | Meaning |
|---|---|---|---|
| `MSG_FIRE` | 0 | 2 | Command with **no ACK** — fire and forget (`lmp_short_frame`) |
| `MSG_CMD` | 1 | 0 | Command with ACK (`local_short_frame`) |
| `MSG_MESH_CMD` | 2 | 2 | Command with ACK, addressed via the short address |
| `MSG_EVENT` | 4 | — | Inbound only: unsolicited notification from the lamp |

An ACK is an inbound frame with message type `2` whose `[1]` equals the sequence number we sent. Anything
else is ignored. `_send_frames()` waits 3 s.

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
| `CMD_DEVICE_DATA_GET` | 66 | Read lamp state — **not answered in gateway mode**, see below |

### MODULE_INFO_GET

`CMD_MODULE_INFO_GET` returns a TLV list — each entry `[length, type, ...value]`, where `length` counts the type
byte plus the value. `protocol.iter_tlv` walks it and `protocol.parse_module_info` picks out four fields into a
`ModuleInfo` named tuple.

The table below is the **complete** TLV set of a real MOOON! H134, captured 2026-08-02. It is pinned verbatim as
`H134_MODULE_INFO` in `tests/test_protocol.py` — the only hardware-derived expectation in that suite.

| Type | Value on the H134 | What it is |
|---|---|---|
| `0x80` | `00` | status/ack; always zero here |
| `0xaf` | MAC, `07`, `000000`, `9401`, `04` | composite block: address, manufacturer_id **7**, module_type, unknown |
| `0xb4` | `9401` | **`LMP_PARAM_MODULE_TYPE`** — little-endian uint16, `404` |
| `0xb5` | `00 02 03 15` | unidentified; plausibly a version |
| `0xb6` | `01 00 00` | unidentified |
| `0xb8` | `02` | `LMP_PARAM_API_VERSION` |
| `0xc1` | `00` | unidentified single byte |
| `0xb9` | `00` | unidentified single byte |
| `0xb0` | MAC | full address, little-endian |
| `0xb1` | `757e` | **`LMP_PARAM_SHORT_ADDRESS`** |
| `0xb2` | `Fermob` | manufacturer name, NUL-padded to 16 |
| `0xb3` | `MOOON - H134` | **`LMP_PARAM_MODEL`** — NUL-padded to 16 |
| `0xb7` | `Moon7E75` | the lamp's own device name |

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

`protocol.parse_device_state` handles both `DEVICE_DATA_GET` responses and `EVENT_DEVICE_DATA`
notifications (identified by `payload[1] == 146`):

```
payload[7]        status — must be 0, else we reject the frame
payload[8] & 0x0F is_on  (the high nibble carries led_mode, so mask it)
payload[9]        level (dimmable white) / cold_white (tunable white)
payload[10]       warm_white (tunable white); defaults to 0 if the payload is only 10 bytes
```

The caller interprets the two channel bytes according to its configured family — `protocol.py` does not know
which lamp it is talking to.

## Dead ends — do not re-litigate these

- **`DEVICE_DATA_GET` is not answered once the lamp is in gateway mode.** `FermobBLEConnection.get_state()` builds the frame correctly and is deliberately never called: every invocation would just burn the 3 s ACK timeout before each command. It is kept because it documents the command and other lamp families may answer it.
- **The lamp emits no EVENT after a plain BLE reconnect.** Only the post-`REGISTER_END` EVENT during first pairing arrives unsolicited. So there is no way to resync state on reconnect, and a button press outside the connected window is simply lost.
- **The post-`REGISTER_END` EVENT's state payload is useless to us.** Connections are only ever established *from* a command, so whatever state it reports is overwritten a few milliseconds later by the command that triggered the connection. The EVENT is still waited for — as a gateway-mode confirmation and settle gate — but its contents are only logged.
- **The model is not in the advertisement.** It rotates and is encrypted, so `module_type` (401 dimmable / 404 tunable) cannot be sniffed *before* pairing — a branch that attempted this was removed as dead code. It **is** readable after connecting, from `MODULE_INFO_GET`; that is a different question and it is now answered.
- **There is no battery level in the GATT table, and none in `DEVICE_INFO_GET`.** Both were checked on hardware — see the GATT table in [TECH-STACK.md](../tech/TECH-STACK.md#bluetooth). A charge level does exist (the official app displays one), so it is carried somewhere we have not looked: the untested candidates are the `DEVICE_DATA` EVENT payload past byte 10, ACK bodies, and commands absent from our constant list.
