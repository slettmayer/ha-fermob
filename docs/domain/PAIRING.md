# Pairing, Ownership and Recovery

> The pairing model has a real consequence for households: **it decides whether Home Assistant or the Fermob app controls the lamp.** Read this before pairing anything.

## The ownership model

These lamps accept **one BLE client at a time**, and pairing is not a handshake you repeat — it is a
registration. The pairing sequence exchanges keys and ends with `REGISTER_END`, after which the lamp enters
"gateway" state and stays there permanently across power cycles and BLE disconnects.

What follows from that:

- **While Home Assistant holds the link, the Fermob app cannot connect.** By default the link is never
  released; under the *on demand* connection mode it is dropped 30 s after the last command. See
  [STATE-MODEL.md](STATE-MODEL.md#connection-modes).
- **Releasing the link does not release ownership.** Pairing is what confers it, so *on demand* does not let
  the phone app in — you still have to unpair from HA and pair from the app.
- **If someone re-pairs from the app, our stored keys stop working.** Recovery is a factory reset, not a retry.
- **In practice: pick one.** After pairing to HA, treat the app as a factory-reset tool only.

## First pairing

Pairing happens lazily, on the **first command** — not when the config entry is created. The handshake
(`FermobBLEConnection._pairing_handshake`) is 10 steps:

| # | Command | Encryption | Purpose |
|---|---|---|---|
| 1 | `REGISTER(0)` | none | Probe. If the lamp answers under `ENCRYPT_PRIVATE`, it is already registered to someone else — we abort with a factory-reset instruction |
| 2 | `AUTHKEY_GET(0)` | none | Read the lamp's public key |
| 3 | `NONCE_GENERATE` | none | Lamp generates and returns the nonce |
| 4 | `CRYPT_SET(PUBLIC)` | none | Switch to public encryption |
| 5 | `AUTHKEY_GEN(1)` | public | Generate the private key |
| 6 | `CRYPT_SET(PRIVATE)` | public | Switch to private encryption |
| 7 | `MODULE_INFO_GET` | private | Read the short address used by all later mesh frames |
| 8 | `DEVICE_INFO_GET` | private | Optional; response ignored |
| 9 | `REGISTER(1)` | private | `REGISTER_END` → lamp enters gateway mode |
| 9b | `DATETIME_SET` | private | Starts the lamp's clock, where the app does it — FIRE, no reply |
| 10 | *(wait for EVENT)* | private | Confirms gateway mode and acts as a settle gate |

Keys are **persisted before** step 9, deliberately: if the confirming EVENT never arrives, the keys the lamp
now holds are still on disk, so we are not locked out.

**`ensure_connected()` then drops the link and opens a fresh one.** The lamp stops honouring the link it was
paired on once `REGISTER_END` puts it in gateway mode: on an H134, every command after pairing was accepted by
Home Assistant and silently ignored by the lamp until the integration was reloaded. A reload is a fresh
connect, so pairing now does that itself. Do not remove it as a redundant round trip — it is the difference
between a working lamp and a dead one, and the failure is invisible from Home Assistant's side because
`send_led` is a write-without-response.

Storage is `.storage/fermob_<mac_with_underscores>` in the HA config directory — the MAC is **lowercased**, so
grep for it in lower case during recovery. It holds five keys: `pub`, `priv` and `nonce` as hex strings, plus
`addr_b2` and `addr_b3` (the short address) as plain integers, not hex.

### The short address is not key material, and must never be overwritten with zero

Step 7 is the only place the handshake learns it, and it is unacknowledged like everything else — a dropped
reply leaves the address at 0 while `_save_keys()` runs anyway. So three separate guards keep a zero out of the
record:

- **Step 7 only assigns a non-zero address.** A reply without the `0xb1` TLV, or no reply at all, leaves what
  was already there.
- **`_forget_keys_in_memory()` does not clear it.** It is derived from the lamp's MAC and a factory reset does
  not change it, so a re-pair has no reason to throw it away — and if it did, a single dropped step-7 reply
  would persist `0x0000` over a good record.
- **`_fetch_module_info_once()` re-reads until it gets one**, bounded by `_MODULE_INFO_MAX_READS` so a lamp
  whose address genuinely *is* `0x0000` does not re-read on every reconnect forever. `_forget_keys_in_memory()`
  clears that latch, because the re-pair is exactly when the recovery is needed.

The consequence of getting this wrong is total and permanent: every addressed frame — `send_led`,
`DATETIME_SET`, and the battery request that is the only liveness signal there is — goes to `0x0000`, the ACK
never comes back, and `ensure_connected()` correctly refuses a link that will never work.

## Reconnects

Reconnecting is **a BLE connect plus `start_notify`** — no `REGISTER_END`, no key exchange. The lamp keeps its
gateway state, so re-running the handshake would be wrong.

This is also why there is no state resync: see
[DEAD-ENDS.md](DEAD-ENDS.md#the-lamp-emits-no-event-after-a-plain-ble-reconnect).

It ends, as every connect does, with a battery request — and that request is also the check that the session
works at all, because its ACK is the only one the lamp ever sends
([STATE-MODEL.md](STATE-MODEL.md#it-is-also-the-liveness-probe)). A reconnect that gets an answer is done.

### When the lamp says our keys are wrong

The lamp usually tells us directly, and this is the fast path. A reset lamp answers an addressed `PRIVATE`
frame with **`CRYPT_MSG`** (error 7) — "I cannot decrypt you" — and `UNREGISTERED` (5) says the same thing.
Either one ends the guessing: the keys are dropped from memory and the handshake runs on a second pass. **No
`REGISTER(0)` probe is sent**, because the lamp has already stated the conclusion the probe would infer, and
more reliably.

Observed on an H134 on 2026-08-06, which is how it was found: a release that read every refusal as "the lamp is
listening" could not see a factory reset at all, and the light went on reporting success into a lamp that could
not read a word of what it was sent.

### When the lamp says nothing at all: is this still our lamp?

`ensure_connected()` runs at most **two passes**, and only an unanswered battery request starts the second.
`_lamp_still_paired()` re-sends step 1's unencrypted `REGISTER(0)` and reads **the encryption mode the lamp
answers in**, not the body. Anything other than `PRIVATE` means the lamp no longer holds our keys — it was
factory-reset behind our back — so the keys are discarded and the full handshake runs on a fresh pass.

This covers the **inverse** of step 1's check, and nothing else did. Step 1 catches *lamp registered, us with
no keys*. The reverse — *us with keys, lamp reset* — was a silent, permanent dead end: the reconnect path
skipped the handshake, every frame went out `PRIVATE`-encrypted to a lamp back in `NONE` mode, and the only
recovery was deleting `.storage/fermob_*` by hand. The BLE link looked perfect throughout.

Two deliberate restrictions, both of which stop this from being worse than the bug it fixes:

- **It is never sent to a lamp that is answering.** `REGISTER(0)` is the first frame of the *pairing* sequence.
  What it does to a lamp that is already registered is unknown beyond "it answers" — the protocol is
  reverse-engineered, Fermob document none of it, and the vendor app has never been observed sending it to a
  lamp it owns. Putting it on the happy path would mean sending a pairing frame to a working lamp on every
  connect, on a guess. Behind a failure, the lamp is useless anyway and a surprise is worth the diagnosis.
- **Silence is read as "still paired".** A probe that times out proves nothing — the lamp may be at the edge of
  range — and re-pairing on that evidence would flash the lamp unattended *and* throw away keys that were still
  good. Only a lamp that positively answers in a non-`PRIVATE` mode is treated as reset.

A *refusal* is an answer, though, and the probe goes through `_send_frames` rather than `_send` to keep the
two apart. `_send` drops the `answered` flag, so reading the payload would classify a reset lamp that NAKs the
probe as still-ours — and since the caller then raises on every subsequent connect, it would never be
re-paired.

The second pass always pairs and always stops. If the freshly-paired lamp *still* does not answer, the connect
fails with `LampNotAnswering` rather than looping round to pair again — so a lamp that is deaf for some third
reason is re-paired once, not repeatedly.

A freshly-paired lamp is held to the same standard, and this is the case that matters most: the handshake's ten
ACKs all landed on the pre-`REGISTER_END` link, which is exactly the link the lamp stops honouring. "We just
paired" is therefore not evidence the *new* link works, and exempting it would report the reproduced
post-pairing failure as success.

Its error message says so explicitly — *"paired, but the lamp did not acknowledge on the new link"*. By that
point the handshake has run to completion: keys saved, `REGISTER_END` sent, the lamp registered to Home
Assistant. A message reading like "pairing failed" would send the user to a factory reset they do not need,
when retrying the command takes the reconnect path and works if the link does.

## Setup prerequisites

- The lamp must be **powered on and not connected to the Fermob app**.
- **Power-cycle it** (off, 2 s, on) immediately before setup — that triggers the advertisement burst HA needs to discover it.
- HA needs a Bluetooth adapter or an **active** ESPHome Bluetooth proxy (`bluetooth_proxy: active: true`) within range. A passive-only proxy can see the lamp but cannot connect to it.
- Battery-powered/portable variants are frequently asleep or out of range. That is normal, and it is why the
  entity reports *unavailable* on a failed command rather than pretending its last state is current.

## Unpairing

`fermob.unpair` (an entity service, as `fermob.check_in` also is) broadcasts `UNREGISTER`, deletes the stored keys and removes the config
entry. The lamp flashes 3× and resets its crypto state to `NONE`, so it can be paired with the app again. It is
the only thing that deletes the keys **while telling the lamp** — removing the entry deletes them too, but
silently, which is why that is a one-way door; see below.

**It is both halves or neither, and the order matters.** `UNREGISTER` is a fire-and-forget broadcast, exactly
as the app sends it, so it can never be acknowledged. What *can* be checked is the session carrying it, with a
battery request one command earlier — retried once, because a single dropped ACK is a marginal link and not a
verdict. If both go unanswered, **the broadcast is not sent at all** and `async_unpair` raises
`HomeAssistantError`, removing nothing. When the connect just ran, its own battery request supplies that
verdict and no second round trip is made (`_connect_verdict`); the probe is repeated only when
`ensure_connected()` returned early on an already-open link, having asked nothing.

`unpair()` returns a `BatteryVerdict`, not a bool, because there are **three** outcomes and only one of them is
"could not reach it":

| Verdict | What it means | What the service does |
|---|---|---|
| `ANSWERED` | The session was alive one command ago | Broadcast, discard keys, remove the entry |
| `SILENT` | Nothing came back — out of range, asleep, or deaf | Raise; remove nothing; "bring it in range and try again" |
| `KEYS_REJECTED` | The lamp answered `CRYPT_MSG`/`UNREGISTERED`: it is **already free** | Raise, saying there is nothing to release and to delete the integration |

Flattening `KEYS_REJECTED` into `SILENT` — which a bare `request_battery()` does — told a user who had just
factory-reset their lamp to bring it in range and retry a service that could never succeed. The removal is
still not done for them: it is the one-way door, and the rejection could in principle be our key store rather
than the lamp.

**The verdict has to be read off the exception too, not only out of `unpair()`.** `ensure_connected()` refuses a
link it could not get an answer over, so on a reset lamp it raises `LampNotAnswering` *before* `unpair()` runs —
which made the `KEYS_REJECTED` row above unreachable from the service. What the user actually saw was *"Could
not reach the Fermob lamp … to unpair it: … turn the light on in Home Assistant to re-pair it"*: the wrong
diagnosis for a lamp that had just answered, followed by the opposite of what they asked for. Found on hardware
(2026-08-06); the unit tests missed it because they mock `ensure_connected` into succeeding.

An entry with **no stored keys at all** never had a pairing to release, so the service removes it without
touching the radio. Sending it down the connect path raised *"not paired, and pairing is not allowed here"*
wrapped in *"could not reach the lamp"* — blaming range for something unrelated to it, and leaving an entry
`fermob.unpair` could never clean up.

It also **never pairs**: `ensure_connected(allow_pairing=False)`. On a lamp the user reset behind Home
Assistant's back, the default would run the re-pair branch — flashing the lamp, re-registering it — and only
then broadcast `UNREGISTER`, which is the opposite of what the service is for. It fails cleanly instead, and
the user deletes the entry, which is the documented path for a lamp that is already free.

Sending it anyway would make the error message a coin toss: the lamp might well receive it and drop to `NONE`
while Home Assistant truthfully reported "nothing has been removed" and kept the keys. Lamp unregistered, HA
still holding keys and an entry — and the next connect would silently re-pair it, so a user trying to hand the
lamp back to the Fermob app could never succeed.

Be precise about what the check does establish: it rules out a broadcast fired into a link the lamp had already
stopped honouring. It does **not** prove the lamp received or acted on the broadcast — nothing can, at the time
of sending.

**Afterwards, though, there is one piece of evidence, and it is conclusive.** Confirmed on an H134
(2026-08-06): a lamp released with `fermob.unpair` was re-added and paired cleanly **with no factory reset**.
Had the broadcast not landed, the lamp would have stayed registered in `PRIVATE` with its keys deleted — the
one-way door below — and the re-add would have stopped at step 1's probe with *"Lamp is in PRIVATE mode but no
stored keys found"*. It did not, so the lamp really was back in `NONE`. The broadcast works; only its
*acknowledgement* is unavailable.

That is also the practical difference between the two ways of getting rid of a lamp, and worth stating plainly
because the reset requirement is easy to over-apply: **re-adding after `fermob.unpair` needs no factory reset.**
The lamp was told. Only entry removal leaves it registered, and only that path needs the ten seconds.

Deleting the keys while the lamp stays registered produces the one state nothing recovers from except a
paperclip: a lamp owned by a controller that has forgotten it, which reads as *"PRIVATE mode but no stored
keys"* forever.

### Removing the config entry deletes the keys — and is a one-way door

`async_remove_entry` deletes `.storage/fermob_<mac>`. That is the cleanup path for a lamp that is *gone* —
dead battery, given away, already reset — because the service above deliberately refuses on a lamp it cannot
reach, and the keys should not outlive the integration as an orphan.

⚠️ **It also means deleting and re-adding the same lamp does not work.** Removing an entry tells the lamp
nothing: it stays registered in `PRIVATE`, and once the keys are gone the re-add hits step 1's probe, finds a
lamp it cannot decrypt, and stops with *"Lamp is in PRIVATE mode but no stored keys found"*. The only way back
is holding the lamp's button for ten seconds. This is a deliberate trade, not an oversight.

Note this is **not** how a factory-reset lamp is recovered — `_lamp_still_paired()` handles that automatically
while the entry still exists, with no `.storage` surgery and no re-add.

So: use the **service** when you want the lamp released and Home Assistant can still reach it. **Delete the
entry** only when you are done with the lamp, or are prepared to factory-reset it.

## Recovery

**Symptom: the lamp is connected and available in HA, but does not react and the battery reads *unavailable*.**
The session is dead: the link is up, `is_connected` is True, and the lamp is discarding everything. Since 0.9.0
this repairs itself — the check-in treats an unacknowledged battery request as a dead session and reconnects
(see [STATE-MODEL.md](STATE-MODEL.md#the-check-in)), so wait one check-in interval or call `fermob.check_in`.
On 0.8.0–0.8.1 it was permanent; reload the integration.

**Symptom: "Lamp is in PRIVATE mode but no stored keys found."**
The lamp is registered to a client whose keys we do not have — the app, or an HA install whose `.storage`
entry was deleted. There is no way to talk to it in that state.

1. Hold the lamp's physical button for **10 seconds** until it flashes — this clears its credentials.
2. Delete `.storage/fermob_*` in the HA config directory.
3. Restart Home Assistant.
4. Power-cycle the lamp and set it up again.

Two ways to reach this since 0.9.0, one accidental and one not. A factory reset performed *while* the entry
exists is handled automatically by the reconnect probe above — no manual `.storage` surgery needed. But
**deleting the config entry and re-adding the lamp lands you here by design**, because entry removal takes the
keys with it; see the warning under [Unpairing](#unpairing). An unacknowledged `fermob.unpair` cannot cause it:
it removes nothing.

**Symptom: the lamp flashes 3× when toggled.**
Something sent `UNREGISTER`. Use the `fermob.unpair` service deliberately rather than toggling, then re-pair.

**Symptom: pairing times out.**
The Fermob app is almost certainly connected. Close it, or move the phone out of range, and retry.
