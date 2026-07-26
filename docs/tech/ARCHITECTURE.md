# Architecture

> Module layering, the BLE connection lifecycle, and the concurrency model.

## Modules

Four files in `custom_components/fermob/`:

| Module | Owns | Imports `homeassistant`? |
|---|---|---|
| `protocol.py` | Frame building, AES-ECB keystream crypto, payload construction, inbound parsing, all protocol constants | **No — keep it that way** |
| `light.py` | `FermobBLEConnection` (BLE link, handshake, key persistence, ACK matching, idle disconnect) and `FermobLight` (the entity) | Yes |
| `config_flow.py` | Bluetooth discovery, manual add, the lamp-type options flow | Yes |
| `__init__.py` | Platform forwarding, unload, the options-update listener | Yes |

### Why `protocol.py` has no HA imports

It makes the frame layer testable with `pip install pytest cryptography` — no Home Assistant, no `hass`
fixture, no async. That is what the 794 tests in `tests/test_protocol.py` exercise, and it is the reason they
run in seconds. The test module even loads `protocol.py` **by file path** rather than as
`custom_components.fermob.protocol`, because importing the package would pull in `__init__.py` and with it
Home Assistant.

If you need something from `homeassistant` in `protocol.py`, that is a signal the logic belongs in
`light.py`.

## The connection lifecycle

There is **no `DataUpdateCoordinator`** and no polling. `FermobBLEConnection` is created once per config entry
and holds the BLE state machine:

```
first command  ─→ ensure_connected() ─→ BLE connect + start_notify ─→ _pairing_handshake()
                                                                       (keys saved, gateway mode)
later commands ─→ ensure_connected() ─→ already connected?  ─→ reset the idle timer, return
                                    └─→ BLE connect + start_notify only (no handshake)
30 s idle      ─→ disconnect()
entry unload   ─→ async_shutdown() ─→ cancel idle task, disconnect under the lock
```

Two flags distinguish the states: `_connected` (the BLE link is up) and `_ready` (post-connect setup is done,
so commands and EVENT dispatch are allowed). `_ready` gates the notification handler — during the handshake,
EVENT frames must go to the queue for `_wait_for_event()` instead of being dispatched to the entity.

**`async_shutdown()` is registered via `entry.async_on_unload`**, which HA runs *after* the platform
teardown. That ordering matters: the entity is gone before we close the link. Without this, an entry reload —
which the options flow triggers on every lamp-type change — would build a second connection while the first
still held the lamp, and these lamps accept one client at a time.

## Frame routing

One characteristic carries everything, so inbound frames are demultiplexed by message type in
`_notif_handler`:

- **`MSG_EVENT` (4)** → if `_ready`, decode and push to the entity via `_dispatch_event()`; otherwise queue it for the handshake's `_wait_for_event()`.
- **Everything else** → `_ack_queue`, where `_send_frames()` is waiting.

`_send_frames()` also has to handle an EVENT arriving *while* it waits for an ACK, and routes it the same way
rather than mistaking it for a response. It matches an ACK on message type `2` and `frame[1] == our sequence
number`, reassembles long-frame fragments in index order, and gives up after 3 s.

## Concurrency

A single `asyncio.Lock` on the connection serialises everything: each command takes it for the whole
connect-and-send, and the idle-disconnect task takes it before closing the link, so it can never disconnect
mid-command. `async_shutdown()` takes it too, which is why an unload waits for an in-flight command instead of
yanking the socket.

The lock is **not** re-entrant and nothing takes it twice — `_async_send_led()` is the only entry point that
acquires it, and `ensure_connected()`/`send_led()` assume it is already held.

## The entity

`FermobLight` is deliberately thin:

- **Colour modes are fixed at construction** from the resolved family: `ColorMode.COLOR_TEMP` with the 3000–6000 K envelope for tunable white, `ColorMode.BRIGHTNESS` for dimmable white.
- **All commands funnel through `_async_send_led()`**, which owns connect → send → on failure disconnect, mark unavailable and write state. Attribute updates happen only after it reports success, so a failed command never leaves HA claiming a state the lamp does not have.
- **Availability is tracked explicitly** — optimistic at startup, `False` after a failed command, `True` after a successful one or an inbound EVENT. It is not derived from the Bluetooth stack's presence cache, which would flap for a lamp that stops advertising while connected.
- **`unique_id`** is `fermob_<mac_with_underscores>`; the device identifier is `("fermob", address)`.

See [OVERVIEW.md](../domain/OVERVIEW.md#state-model-and-its-limits) for why there is no state resync.
