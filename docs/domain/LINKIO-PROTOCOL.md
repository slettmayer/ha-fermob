# The Linkio BLE Protocol

> How Fermob lamps are actually driven. Everything under here maps to
> `custom_components/fermob/protocol.py`. This page is the index; each layer has its own file.

**Confidence, and it applies to every page below.** The framing, crypto and handshake were reverse-engineered
from the official Fermob Lighting app's protocol JS by
[@edouardrosset](https://github.com/edouardrosset) (dimmable white) and
[@fjcompiled](https://github.com/fjcompiled) (tunable white), and confirmed working against real hardware by
each of them. **We have not independently verified any of it against the app.** Where a detail is inferred
rather than observed, it says so — preserve those markers when you edit.

## The layers

| Guide | Covers |
|---|---|
| [PROTOCOL-TRANSPORT.md](PROTOCOL-TRANSPORT.md) | BLE transport and UUIDs, the 20-byte frame, CRC and padding, fragmentation, message types, ACK matching, the AES-ECB keystream cipher |
| [PROTOCOL-COMMANDS.md](PROTOCOL-COMMANDS.md) | Every command we send, the battery command that makes the check-in possible, and the `MODULE_INFO_GET` TLV table captured from an H134 |
| [PROTOCOL-LIGHT-COMMAND.md](PROTOCOL-LIGHT-COMMAND.md) | `DEVICE_DATA_SET`, both family bodies, and the mired-linear warm/cold mixing that expresses colour temperature |
| [PROTOCOL-INBOUND-STATE.md](PROTOCOL-INBOUND-STATE.md) | The device-data record byte by byte, and the marker 146/147 trust rule |
| [DEAD-ENDS.md](DEAD-ENDS.md) | What was tried and does not work — state reads, reconnect resync, the model in the advertisement, brightness limiting |
| [PAIRING.md](PAIRING.md) | The one-client ownership model and the 10-step registration handshake |

## Reading order

Working on frames or crypto — start at [PROTOCOL-TRANSPORT.md](PROTOCOL-TRANSPORT.md). Adding or changing a
command — [PROTOCOL-COMMANDS.md](PROTOCOL-COMMANDS.md), then check
[DEAD-ENDS.md](DEAD-ENDS.md) before assuming the lamp will answer it. Chasing wrong brightness or colour —
[PROTOCOL-LIGHT-COMMAND.md](PROTOCOL-LIGHT-COMMAND.md). Chasing state that looks stale —
[PROTOCOL-INBOUND-STATE.md](PROTOCOL-INBOUND-STATE.md) and [STATE-MODEL.md](STATE-MODEL.md).

**Two rules that outrank everything else here**, both because getting them wrong is a silent failure rather
than an error:

- **Only marker 146 may reach an entity.** 147 is a stored record that does not track reality. See
  [PROTOCOL-INBOUND-STATE.md](PROTOCOL-INBOUND-STATE.md#marker-146-versus-147).
- **Never inline a protocol literal.** Every command number, marker, parameter ID and envelope bound lives in
  `protocol.py`. See [CONVENTIONS.md](../tech/CONVENTIONS.md#protocol-code).
