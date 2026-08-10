# Domain Context

Detailed guides on the lamps and the protocol they speak. Read the one that matches your task; start at
[OVERVIEW.md](OVERVIEW.md) if you do not know which that is.

| Guide | Covers |
|---|---|
| [OVERVIEW.md](OVERVIEW.md) | The index: domain classification, the concept catalog, the cross-cutting decisions, and the glossary |
| [DEVICES.md](DEVICES.md) | The two lamp families, the models, lamp-family detection, and a confidence table for every "this works" claim |
| [ENTITIES-AND-SERVICES.md](ENTITIES-AND-SERVICES.md) | The light entity, the two diagnostic battery entities, the firmware entity, `check_in` and `unpair`, and the three options |
| [STATE-MODEL.md](STATE-MODEL.md) | Push-only state, why the held link is the whole mechanism, the connection modes and their coupled timings, the check-in |
| [APP-CAPABILITIES.md](APP-CAPABILITIES.md) | What the official Fermob app can and cannot configure, and why feature parity is not a route to changing lamp behaviour |
| [FIRMWARE-UPDATE.md](FIRMWARE-UPDATE.md) | How the app updates firmware, the vendor DFU server, and why we do not install it |
| [LINKIO-PROTOCOL.md](LINKIO-PROTOCOL.md) | Protocol index and the confidence statement covering every protocol page |
| [PROTOCOL-TRANSPORT.md](PROTOCOL-TRANSPORT.md) | Transport and UUIDs, the 20-byte frame, message types, ACK matching, AES-ECB keystream crypto |
| [PROTOCOL-COMMANDS.md](PROTOCOL-COMMANDS.md) | Every command we send, the battery command, and the `MODULE_INFO_GET` TLV table |
| [PROTOCOL-LIGHT-COMMAND.md](PROTOCOL-LIGHT-COMMAND.md) | Both light payload bodies and mired-linear tunable-white mixing |
| [PROTOCOL-INBOUND-STATE.md](PROTOCOL-INBOUND-STATE.md) | The device-data record byte by byte, and the marker 146/147 trust rule |
| [PAIRING.md](PAIRING.md) | The one-client ownership model, the 10-step handshake, key storage, reconnects, unpairing, recovery |
| [DEAD-ENDS.md](DEAD-ENDS.md) | What was tried and does not work — read before adding a command or chasing stale state |

Everything here is reverse-engineered. Each document marks what is verified on hardware, what comes from the
official app's JS, and what is inferred — preserve those markers when you edit.
