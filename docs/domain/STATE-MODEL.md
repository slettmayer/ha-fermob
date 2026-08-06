# State Model

> Where the light's state comes from, why holding the BLE link open is the entire mechanism rather than an
> optimisation, and the two timings that follow from the connection mode.

**Scope.** Freshness and the connection lifecycle as a *domain* concern. The code that implements it is in
[ARCHITECTURE.md](../tech/ARCHITECTURE.md#the-connection-lifecycle); the frames involved are in
[PROTOCOL-INBOUND-STATE.md](PROTOCOL-INBOUND-STATE.md).

## Push-only, by necessity

The integration is push-only — `iot_class: local_push`, `should_poll = False`. State changes reach HA from
exactly two places:

1. **Our own commands.** After a successful write we record what we commanded.
2. **EVENT notifications**, which the lamp volunteers the moment anything about it changes — a button press, a
   brightness or colour-temperature change made at the lamp, the charger going on or off. These arrive in about
   a second, and they are the reason the light entity can be trusted to follow the lamp rather than only the
   last command HA sent.

**The catch is that the lamp pushes only while something is connected to it, and pushes nothing when a
connection is re-established.** So a link that was down during a button press has lost that press permanently;
there is no query that recovers it. Both candidate read commands were tried on hardware and the
manufacturer's own app sends neither — see
[DEAD-ENDS.md](DEAD-ENDS.md#reading-light-state-back-does-not-work).

Holding the link open is therefore not an optimisation, it is the entire mechanism, and it is the default.

If a command fails, the entity goes *unavailable* rather than continuing to assert a state that may be false.

## Connection modes

The **connection mode** option trades the link away for a connection slot on the adapter or BLE proxy.
`resolve_connection_profile` in `__init__.py` maps the option onto a `ConnectionProfile` — an idle-disconnect
delay and a check-in interval:

| Mode | Idle disconnect | Check-in interval | Consequence |
|---|---|---|---|
| Always connected (default) | none — the link is never dropped | 30 min | Button presses and charger events reach HA in ~1 s |
| On demand | 30 s after the last command | 6 h | Presses are not reported; the entity shows what HA last commanded |

**The two timings must stay derived from one option.** A check-in calls `ensure_connected()`, which re-arms the
idle timer — so an interval shorter than the timeout would hold the link open no matter what the timeout said.
Exposing them as two independent numbers makes that misconfiguration possible by accident.

Under *always connected* the check-in is the **reconnect heartbeat**: nothing else notices a dropped link,
which is why it runs far more often than the battery alone would justify. Thirty minutes bounds how long the
entity can show confidently stale state after, say, a BLE proxy reboots. Under *on demand* nothing is
listening between commands, so a check-in cannot be a heartbeat at all — only a battery poll, and six hours
gives a same-day figure with four chances to catch the lamp in range.

There is deliberately **no middle setting**. A link held for a few minutes would produce a light that is
sometimes right with no way to tell which times those were, which is worse than either end.

Switching mode does not change who owns the lamp. Pairing confers ownership, not an open link, so *on demand*
does not let the phone app in — see [PAIRING.md](PAIRING.md).

## The check-in

The check-in is the one scheduled thing in the integration. It has two jobs: reconnect a dropped link, and
refresh the battery, which the lamp reports only when asked. It sends no light command and cannot change what
the lamp is doing — which is also how the vendor app behaves, polling the same battery command on a timer with
every lamp dark.

It also runs once `CHECK_IN_STARTUP_DELAY` (2 minutes) after setup, for two reasons: the interval timer
restarts from zero on every reload, so on a box that is restarted often the tick could otherwise be missed
repeatedly; and both battery entities read unavailable until the lamp has reported once, which would otherwise
last until something turned the light on. The delay lets the Bluetooth stack come up first.

Two deliberate refusals:

- **It never pairs, at all.** Not merely "will not run on an unpaired lamp" — it passes
  `ensure_connected(allow_pairing=False)`, which forbids the handshake outright. The older key-presence guard
  was not enough: a lamp someone factory-reset to hand back to the Fermob app leaves *our* keys on disk, so the
  guard passed, and the re-pair branch would have flashed it through a full handshake overnight and silently
  taken ownership again. Pairing belongs to something the user just did.
- **It swallows every failure.** An out-of-range balcony lamp is the normal case, and a missed check-in must
  leave the last known level in place rather than clearing it.

It does, however, **report one specific outcome**: `LampNotAnswering`, meaning the link came up and the lamp
ignored two requests on it. Availability is otherwise written only when a command is sent, so a lamp that has
gone deaf would read *available* and *on* in the UI indefinitely — exactly the appearance this release is about.

**Failing to reach the lamp at all is not that**, and deliberately changes nothing. Out of range, taken indoors,
no advertisement yet, adapter busy — that is the normal condition of a balcony lamp, and in *on demand* mode the
next check-in is six hours away. Reporting unavailable there would grey the entity out for the rest of the day
over one missed advertisement, for a lamp that would answer a command perfectly well.

**With one exception: once the session has been *proved* dead, that excuse expires.** If the check-in found an
open link the lamp was ignoring, tore it down, and then could not get the lamp back, "cannot reach it" is no
longer reassuring — the last thing known for certain is that the lamp was not answering. It reports unavailable
in that case too.

### It is also the liveness probe

**The battery ACK is the only acknowledgement this integration ever receives on a live link.** Every other
frame it sends — `send_led`, `DATETIME_SET`, `UNREGISTER` — is a write-without-response and cannot fail. So
when the check-in finds the link already up, an unacknowledged battery request is the *only* available evidence
that the lamp has stopped listening, and it acts on it: disconnect, reconnect, and let the connect path
re-establish the session.

Two things make that signal trustworthy enough to act on:

- **A refusal usually counts as an answer.** The lamp NAKs some commands outright (`DEVICE_DATA_GET` is
  refused with error 18), and such a NAK is proof it is listening. `request_battery()` therefore reports the
  *acknowledgement*, not the payload — both a NAK and a timeout come back with no body.
- **Except `CRYPT_MSG` and `UNREGISTERED`, which mean the opposite.** Those two are the lamp saying it cannot
  decrypt us, so a session that gets one is not alive at all: our keys are wrong. Verified on hardware
  (2026-08-06) — a factory-reset H134 answers `CRYPT_MSG` rather than going silent, and for one release counting
  that as "listening" made the reset undetectable. The three-way split lives in `BatteryVerdict`.

  It is also the one failure the user can fix, so it must **not** take the entity unavailable — an unavailable
  entity cannot be sent the `light.turn_on` that re-pairs it. See
  [ARCHITECTURE.md](../tech/ARCHITECTURE.md#and-one-exception-inside-the-exception-a-reset-lamp-stays-available).
- **One miss is not a diagnosis.** A `SILENT` request is retried once before anything acts on the failure —
  and the retry returns a `BatteryVerdict` too, so a rejection that only shows up on the second attempt is
  still read as a rejection. Anywhere that flattens the three verdicts into a bool loses exactly that case;
  `request_battery()` is the bool wrapper and is only for callers that genuinely mean "can I talk to this
  lamp".

This is what replaces the 30 s idle disconnect. Before 0.8.0 that timer dropped the link after every command,
so a session the lamp had stopped honouring was repaired within half a minute — invisibly, and by accident.
Holding the link open removed that and put nothing in its place: `is_connected` stays `True` on a session the
lamp is ignoring, `_async_send_led` marks the entity *available* after a write that cannot fail, and
`request_battery` logged its timeout and swallowed it. The result was a lamp that read as connected and
healthy in Home Assistant while ignoring every command, permanently. Fixed in 0.9.0.

The check-in interval is therefore also the **upper bound on how long that state can last**, which is a second
reason not to lengthen it.

`fermob.check_in` is the same routine on demand — see
[ENTITIES-AND-SERVICES.md](ENTITIES-AND-SERVICES.md#services).

## What holding the link costs

Measured on an H134 left dark on its stand overnight: **86 % → 85 % over 7.6 hours**, about **0.1 %/h**, so
roughly 2 % per day. Link uptime over the same window was 5 h 20 min continuous, with no disconnects,
reconnects or errors.

Read that as an **upper bound on the connection's cost, not a measurement of it**: the same lamp was never
measured over a comparable period with no connection, and it self-discharges and runs its radio either way.
One lamp, one night, one temperature, and the gauge is a voltage proxy — see
[ENTITIES-AND-SERVICES.md](ENTITIES-AND-SERVICES.md#battery-entities). Lit, the LEDs dominate so completely
that the link is noise: Fermob quote 6 h at 100 % brightness, which is about 16 %/h.

The real cost is the **connection slot**, which is what the on-demand mode exists to hand back.
