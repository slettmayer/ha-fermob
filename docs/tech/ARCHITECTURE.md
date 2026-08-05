# Architecture

> Module layering, the BLE connection lifecycle, and the concurrency model.

## Modules

Seven files in `custom_components/fermob/`:

| Module | Owns | Imports `homeassistant`? |
|---|---|---|
| `protocol.py` | Frame building, AES-ECB keystream crypto, payload construction, inbound parsing, TLV walking, all protocol constants | **No — keep it that way** |
| `light.py` | `FermobBLEConnection` (BLE link, handshake, key/module-info persistence, ACK matching, idle disconnect) and `FermobLight` (the entity) | Yes |
| `config_flow.py` | Bluetooth discovery, manual add, the options flow (lamp type, connection mode) | Yes |
| `entity.py` | `FermobBatteryEntityBase` — device info and the battery-push subscription shared by both diagnostic entities | Yes |
| `sensor.py` | `FermobBatterySensor` — state of charge | Yes |
| `binary_sensor.py` | `FermobChargingSensor` — charging flag | Yes |
| `__init__.py` | Platform forwarding, unload, the check-in timers, the connection profile, the options-update listener | Yes |

The three battery files carry no BLE logic: they read `FermobBLEConnection.battery` and subscribe to its
pushes.

### Push subscriptions are lists with removal, not assignable slots

`add_battery_listener()` / `add_state_listener()` append to a list and return an unsubscribe callable, which
entities register through `Entity.async_on_remove`. Follow that pattern for any future push.

`on_battery` and `on_state_change` used to be single assignable attributes, which is the pattern this replaced.
Two entities want the battery push, so whichever was added second had to *chain* onto whatever it found in the
slot, and nothing ever unchained because the entities had no removal hook. That is hard to reason about — you
have to know the platform setup order to know who is subscribed — and it is not the Home Assistant idiom. The
list makes subscribers independent of each other and ties each subscription to its entity's lifetime.

**No user-visible failure was ever demonstrated from the old pattern**, and 0.8.1's release notes were
corrected to say so. This is a defensive change, not a bug fix. Do not cite it as evidence that the old shape
broke in production.

`on_module_info` is still a single slot, and that is correct — the config entry is its only writer, and it is
set before the platforms are forwarded.

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
                                                                       (keys saved, gateway mode,
                                                                        DATETIME_SET)
later commands ─→ ensure_connected() ─→ already connected?  ─→ re-arm the idle timer, return
                                    └─→ BLE connect + start_notify (no handshake)
                                          └─→ MODULE_INFO_GET, but only until it answers once
                                          └─→ set_module_time → request_battery
check-in timer ─→ async_check_in() ─→ paired? ─→ take the lock ─→ ensure_connected()
                                                                  └─→ already up? ask both again
lamp changes   ─→ EVENT push (marker 146) ─→ _dispatch_event() ─→ the light entity
idle timeout   ─→ disconnect()          (on-demand mode only; there is no timer otherwise)
entry unload   ─→ async_shutdown() ─→ cancel idle task, disconnect under the lock
```

**The link is held open by default, and that is the whole state mechanism.** The lamp pushes an unsolicited
`EVENT_DEVICE_DATA` the moment it changes — a button press, a brightness change at the lamp — and a battery
push whenever the charger goes on or off. It only does so while something is connected, and it pushes nothing
when a connection is re-established. There is no query that returns usable state (see
[LINKIO-PROTOCOL.md](../domain/LINKIO-PROTOCOL.md)), so a dropped link means a missed press, full stop.
Measured cost of holding it: about 0.1 %/h of battery, and one connection slot on the adapter or BLE proxy.

The **connection mode** option trades that slot back. `resolve_connection_profile` in `__init__.py` maps it to
an idle timeout and a check-in interval — always-connected is `(None, 30 min)`, on-demand is `(30 s, 6 h)`.
The two are derived from one option rather than exposed separately because they interact: a check-in calls
`ensure_connected()`, which re-arms the idle timer, so an interval shorter than the timeout would hold the link
open regardless of the timeout.

The one scheduled thing is the **check-in** (plus one run `CHECK_IN_STARTUP_DELAY` after setup). It has two
jobs. It reconnects — nothing else notices an unexpected disconnect, so its interval is the upper bound on how
long the entity can show confidently stale state after, say, a BLE proxy reboots. And it refreshes the battery,
which the lamp reports only when asked. It sends no light command and cannot change what the lamp is doing,
which is also how the vendor app behaves (it polls the same command on a timer with every lamp dark, roughly
every 40 s while its screen is open). `fermob.check_in` is the same routine on demand. Both timers are
cancelled via `entry.async_on_unload`.

It **never pairs**, and the guard for that is `ensure_connected(allow_pairing=False)`, not the key-presence
check at the top. That check alone was not enough: a lamp someone factory-reset leaves our keys on disk, so it
passes, and the re-pair branch would then flash the lamp through a full handshake at an arbitrary hour and
silently re-register a lamp its owner had deliberately freed. It also swallows every failure — an out-of-range
balcony lamp is the normal case, and a missed check-in must leave the last known level in place rather than
clearing it.

Swallowing is not the same as hiding, though: the check-in reports **`LampNotAnswering`** through
`add_availability_listener`, and `FermobLight.on_check_in_result` is the subscriber. Without that, availability
is written only on the command path (`_async_send_led`), so a lamp that has gone deaf reads *available* and *on*
indefinitely until somebody presses the switch — which is the failure this release exists to fix.

Only that exception. Every other connect failure — out of range, adapter busy, no advertisement — leaves
availability untouched, because it is routine and because the next check-in may be six hours away. That is what
the dedicated exception type is for: `RuntimeError` alone would have conflated "cannot reach it" with "it is
ignoring me".

Its third job is **liveness**. `request_battery()` returns whether the lamp acknowledged, and that ACK is the
only one the integration ever gets on a live link — every other frame is a write-without-response. When the
check-in finds the link already up and the request goes unanswered, it disconnects and reconnects. That is what
replaces the 30 s idle disconnect, which used to repair a dead session by accident after every command. See
[STATE-MODEL.md](../domain/STATE-MODEL.md#it-is-also-the-liveness-probe).

### Connect is a two-pass loop, not a straight line

`ensure_connected()` runs **at most two passes**. Each opens a raw link with `_open_link()` (device lookup,
`establish_connection`, `start_notify`), establishes the session, and ends with `set_module_time()` and a
battery request. What happens next depends only on whether that request was answered:

| Pass outcome | Next |
|---|---|
| Battery answered (first try **or the retry**) | done |
| Both unanswered, freshly paired (`not have_keys`) | **disconnect and raise** — no probe, no third pass |
| Both unanswered, `allow_pairing=False` | **disconnect and raise** |
| Both unanswered, `_lamp_still_paired()` says yes or stays silent | **disconnect and raise** |
| Both unanswered, lamp answers in a non-`PRIVATE` mode | `_forget_keys_in_memory()`, second pass pairs |

That `raise` is the point of the whole exercise. Returning a link nobody could get an answer over hands
`_async_send_led` something it will write into and mark *available*, because `send_led` cannot fail — so
`ensure_connected` has to be the layer that refuses. The entity goes unavailable, which is true, and the
check-in retries on its own schedule.

Three guards keep that from over-firing:

- **The battery request is retried once** before anything expensive or destructive happens. One dropped ACK on a
  marginal link is not evidence of anything, and the cost of treating it as evidence is a pairing frame or a
  light that goes unavailable while working.
- **`request_battery()` reports the ACK, not the payload.** `_send_frames` returns `(payload, enc, answered)`; a
  NAK has `payload=None` but `answered=True`, and a lamp that *refuses* the command has still proved it is
  listening. Conflating the two would take a working lamp unavailable for declining one diagnostic read.
- **`allow_pairing=False` forbids the handshake**, and `async_check_in` passes it. See below.

Three things here are load-bearing, and none is a redundant round trip:

- **The reconnect after pairing.** `REGISTER_END` puts the lamp in gateway mode and it stops honouring the link
  it was paired on — reproduced on an H134, where every post-pairing command was accepted by HA and ignored by
  the lamp until a reload. A reload is a fresh connect, so the pairing branch does one itself.
- **`_lamp_still_paired()`** is the inverse of the handshake's step-1 probe: *us with keys, lamp factory-reset*.
  It is **gated behind the unanswered battery request on purpose** — `REGISTER(0)` is a pairing frame, and what
  it does to a lamp that is already registered is not known. Never send it on a healthy connect.
- **`_ready`/`_connected` are cleared at the top of each pass**, because a link that dropped without going
  through `disconnect()` would otherwise leave `_ready` set and the handshake's state pushes would be
  dispatched to entities that cannot decode them.

All three are detailed in [PAIRING.md](../domain/PAIRING.md#reconnects).

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

It is also self-limiting: once the value is in `entry.data`, `resolve_light_type` agrees with the lamp, the
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
- **Both services are entity services**, registered in the platform's `async_setup_entry` via
  `entity_platform.async_get_current_platform().async_register_entity_service(...)` — not plain methods and not
  `hass.services` registrations. Neither takes a schema: `"unpair"` → `async_unpair`, `"check_in"` →
  `async_check_in`. See [ENTITIES-AND-SERVICES.md](../domain/ENTITIES-AND-SERVICES.md#services) for what each
  is for, and [PAIRING.md](../domain/PAIRING.md#unpairing) for what unpairing does to the lamp.

See [STATE-MODEL.md](../domain/STATE-MODEL.md) for why there is no state resync.
