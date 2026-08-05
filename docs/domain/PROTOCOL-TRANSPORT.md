# Transport, Framing and Crypto

> How a byte gets to the lamp: the BLE transport, the 20-byte frame, the message types, and the keystream
> cipher. Maps to `build_short` / `build_long` / `crypt` in `custom_components/fermob/protocol.py`.

**Scope.** The envelope only. What goes *inside* a frame is in
[PROTOCOL-COMMANDS.md](PROTOCOL-COMMANDS.md) and [PROTOCOL-LIGHT-COMMAND.md](PROTOCOL-LIGHT-COMMAND.md);
what comes *back* is in [PROTOCOL-INBOUND-STATE.md](PROTOCOL-INBOUND-STATE.md). Confidence markers and the
overall protocol framing are in [LINKIO-PROTOCOL.md](LINKIO-PROTOCOL.md).

## Transport

| | |
|---|---|
| Advertisement service UUID | `41c13060-6def-11e5-bcde-0002a5d5c51b` — the `bluetooth:` matcher in `manifest.json`, advertised in an *incomplete list* of 128-bit service UUIDs (AD type `0x06`) |
| GATT service UUID | `41c15000-6def-11e5-bcde-0002a5d5c51b` — **not the advertisement UUID**; reachable only post-connection, so useless for discovery. **Confirmed** on an H134 (2026-08-02) |
| Manufacturer data | company `0x04AA`, rotating/encrypted payload |
| Write/notify characteristic | `00005002-0000-1000-8000-00805f9b34fb` (the app's `LINKIO_TXRX_CHARACTERISTIC`) |
| Connections | **one client at a time** — see [PAIRING.md](PAIRING.md) |

Because the advertisement payload is rotating and encrypted, **the lamp model cannot be identified before
connecting.** That is why the family is read from the lamp after connecting rather than sniffed — see
[DEVICES.md](DEVICES.md#lamp-family-detection).

## Frame layout

Every frame is exactly 20 bytes:

```
[0]      header  = (msg_type << 5) | (encryption << 3) | frame_type
[1]      cmd_id  — our rolling sequence number, echoed in the ACK
[2..3]   short address (b2/b3) for mesh frames, 0 otherwise
[4..19]  encrypt( [crc] + payload padded to 15 bytes )
```

- **`crc`** is a plain XOR fold over the 15 padded payload bytes (`protocol.crc`).
- **Padding** (`protocol.pad15`) appends one `0x00` terminator, then `0xFF` filler, to 15 bytes.
  A payload of exactly 15 bytes gets no terminator.
- **Payloads longer than 15 bytes** fragment into 15-byte chunks (`protocol.build_long`): frame type `3` for
  the first fragment and `6` for continuations, with `[2]` = fragment index and `[3]` = fragment count.

## Message types

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

## ACKs

An ACK is an inbound frame with message type `2` whose `[1]` equals the sequence number we sent. Anything
else is ignored. `_send_frames()` waits 3 s.

**A matching ACK is not necessarily a success.** Byte `[2]` of an ACK TLV is an `LMP_ERRORS` code; non-zero
means the command was rejected. `ack_error()` checks it and `_send_frames()` reports failure, because
otherwise a NAK body gets parsed as if it were real data — which in the handshake meant storing an error
payload as the lamp's private key.

## Encryption

| Mode | Value | Key |
|---|---|---|
| `ENCRYPT_NONE` | 0 | — (plaintext) |
| `ENCRYPT_PUBLIC` | 1 | the lamp's public key |
| `ENCRYPT_PRIVATE` | 2 | the negotiated private key |

The scheme is a **keystream XOR, not a block cipher over the data**: the 16-byte nonce is AES-ECB encrypted
under the chosen key, and the result is XORed over the 16-byte body. It is therefore symmetric — the same
function encrypts and decrypts (`protocol.crypt`).

We use `cryptography` for this, not pycryptodome. See
[TECH-STACK.md](../tech/TECH-STACK.md#the-aes-dependency).
