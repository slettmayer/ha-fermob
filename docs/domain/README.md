# Domain Context

Detailed guides on the lamps and the protocol they speak. Read the one that matches your task.

| Guide | Covers |
|---|---|
| [OVERVIEW.md](OVERVIEW.md) | The two lamp families, what the integration exposes, what the official app can and cannot configure, family detection, the push-only state model and its limits, and a confidence table for every "this works" claim |
| [LINKIO-PROTOCOL.md](LINKIO-PROTOCOL.md) | Frame layout, AES-ECB keystream crypto, message types, the command table, both light payload bodies, tunable-white mixing, inbound state parsing — and the dead ends not to re-litigate |
| [PAIRING.md](PAIRING.md) | The one-client ownership model, the 10-step pairing handshake, key storage, reconnect behaviour, setup prerequisites, unpairing, and recovery from a locked-out lamp |

Everything here is reverse-engineered. Each document marks what is verified on hardware, what comes from the
official app's JS, and what is inferred — preserve those markers when you edit.
