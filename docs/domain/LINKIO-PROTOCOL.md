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
| Advertisement service UUID | `41c13060-6def-11e5-bcde-0002a5d5c51b` (the `bluetooth:` matcher in `manifest.json`) |
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

`CMD_MODULE_INFO_GET` returns a TLV list walked by `protocol.parse_module_info`: each entry is
`[length, type, ...value]`, and we pick out `LMP_PARAM_SHORT_ADDRESS` (177 / `0xb1`, two address bytes) and
`LMP_PARAM_API_VERSION` (184 / `0xb8`).

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

`warm_ratio` maps linearly onto the 3000 K – 6000 K envelope: `1.0` is 3000 K (all warm), `0.0` is 6000 K
(all cold). `protocol.kelvin_to_warm_ratio` and `warm_ratio_to_kelvin` are exact inverses across the whole
envelope, and both clamp outside it.

A consequence worth knowing: at very low brightness the split quantises hard. At `level = 1` and mid
colour temperature, `warm = 1` and `cold = 0`, so the lamp skews warm. That is inherent to expressing
temperature as two integer percentages, not a bug in the conversion.

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
- **The model is not in the advertisement.** It rotates and is encrypted. Do not try again to sniff `module_type` (401 dimmable / 404 tunable) before pairing; a branch that attempted this was removed as dead code.
