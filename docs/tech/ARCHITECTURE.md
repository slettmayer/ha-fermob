# Architecture

> Module layering, the BLE connection lifecycle, and the concurrency model.

## Modules

Seven files in `custom_components/fermob/`:

| Module | Owns | Imports `homeassistant`? |
|---|---|---|
| `protocol.py` | Frame building, AES-ECB keystream crypto, payload construction, inbound parsing, TLV walking, all protocol constants | **No — keep it that way** |
| `light.py` | `FermobBLEConnection` (BLE link, handshake, key/module-info persistence, ACK matching, idle disconnect) and `FermobLight` (the entity) | Yes |
| `config_flow.py` | Bluetooth discovery, manual add, the lamp-type options flow | Yes |
| `entity.py` | `FermobBatteryEntityBase` — device info and the chained `on_battery` subscription shared by both diagnostic entities | Yes |
| `sensor.py` | `FermobBatterySensor` — state of charge | Yes |
| `binary_sensor.py` | `FermobChargingSensor` — charging flag | Yes |
| `__init__.py` | Platform forwarding, unload, the options-update listener | Yes |

The three battery files carry no BLE logic: they read `FermobBLEConnection.battery` and subscribe to its
callback. The subscription is **chained rather than assigned**, because the sensor and the binary sensor share
one connection and overwriting `on_battery` would silently disconnect the other.

### Why `protocol.py` has no HA imports

It makes the frame layer testable with `pip install pytest cryptography` — no Home Assistant, no `hass`
fixture, no async. That is what `tests/test_protocol.py` exercises, and it is the reason that
module runs in about two seconds while `tests/test_light.py` spends most of its time importing HA. The test module even loads `protocol.py` **by file path** rather than as
`custom_components.fermob.protocol`, because importing the package would pull in `__init__.py` and with it
Home Assistant.

If you need something from `homeassistant` in `protocol.py`, that is a signal the logic belongs in
`light.py`.

## The connection lifecycle

There is **no `DataUpdateCoordinator`**, and the light itself is never polled — its state is push-only.
`FermobBLEConnection` is created once per config entry and holds the BLE state machine:

```
first command  ─→ ensure_connected() ─→ BLE connect + start_notify ─→ _pairing_handshake()
                                                                       (keys saved, gateway mode)
later commands ─→ ensure_connected() ─→ already connected?  ─→ reset the idle timer, return
                                    └─→ BLE connect + start_notify (no handshake)
                                          └─→ MODULE_INFO_GET, but only until it answers once
battery timer  ─→ async_poll_battery() ─→ paired? ─→ take the lock ─→ ensure_connected()
                                                                     └─→ already up? request_battery()
30 s idle      ─→ disconnect()
entry unload   ─→ async_shutdown() ─→ cancel idle task, disconnect under the lock
```

The one scheduled thing is the **battery check-in** (`BATTERY_POLL_INTERVAL`, 6 h, plus one run
`BATTERY_POLL_STARTUP_DELAY` after setup). It exists because the lamp never reports unprompted: without it the
level only refreshes when a light command happens to reach the lamp, so a lamp left off keeps a stale reading
indefinitely. It reads battery **only** — it sends no light command and cannot change what the lamp is doing,
which is also how the vendor app behaves (it polls the same command on a timer with every lamp dark, roughly
every 40 s while its screen is open). Both timers are cancelled via `entry.async_on_unload`.

It deliberately **refuses to run on an unpaired lamp**: `ensure_connected()` would otherwise start the pairing
handshake, which makes the lamp flash, unattended and at an arbitrary hour. It also swallows every failure — an
out-of-range balcony lamp is the normal case, and a missed check-in must leave the last known level in place
rather than clearing it or marking the entities unavailable.

### Learning what the lamp is, without deadlocking

`_fetch_module_info_once()` runs inside `ensure_connected()`, which runs **while the connection lock is held**.
What it discovers has to reach `entry.data`, and writing that triggers the update listener, whose reload calls
`async_shutdown()` — which takes the same lock. So the callback path is deliberately non-awaiting:

```
_fetch_module_info_once()  →  _store_module_info()  →  on_module_info callback
                                                        └─ hass.config_entries.async_update_entry(...)
```

`async_update_entry` only *schedules* the listener, so the reload queues behind the in-flight command instead of
deadlocking against it. The callback must therefore stay synchronous and must not await a reload itself.

It is also self-limiting: once the value is in `entry.data`, `_resolve_light_type` agrees with the lamp, the
callback's diff is empty, and no further entry updates or reloads happen. Both halves of that matter — the
`changed` check in `_store_module_info` and the diff in the callback — or a lamp would reload on every connect.

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
- **All light commands funnel through `_async_send_led()`**, which owns connect → send → on failure disconnect, mark
  unavailable and write state. Attribute updates happen only after it reports success, so a failed command never leaves
  HA claiming a state the lamp does not have. `async_unpair()` deliberately does not use it — see
  [CONVENTIONS.md](CONVENTIONS.md#entity-and-connection-code).
- **Availability is tracked explicitly** — optimistic at startup, `False` after a failed command, `True` after a successful one or an inbound EVENT. It is not derived from the Bluetooth stack's presence cache, which would flap for a lamp that stops advertising while connected.
- **`unique_id`** is `fermob_<mac_with_underscores>`; the device identifier is `("fermob", address)`.
- **`fermob.unpair` is an entity service**, registered in `async_setup_entry` via
  `entity_platform.async_get_current_platform().async_register_entity_service("unpair", {}, "async_unpair")` — not a
  plain method and not a `hass.services` registration. It takes no schema. See
  [PAIRING.md](../domain/PAIRING.md#unpairing) for what it does to the lamp.

See [OVERVIEW.md](../domain/OVERVIEW.md#state-model-and-its-limits) for why there is no state resync.
