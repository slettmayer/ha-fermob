# Changelog

## 0.7.0

The battery level now refreshes on its own, instead of only when something
happens to talk to the lamp.

- **A battery check-in runs every 6 hours**, and once shortly after Home
  Assistant starts. Until now the level only updated when a light command
  reached the lamp, so a lamp left switched off kept whatever reading it had
  when it was last used — for as long as that lasted.
- **It does not turn the lamp on, and cannot.** The check-in only asks for the
  battery; it sends no light command at all, and simply connecting cannot change
  what the lamp is doing. This is how the manufacturer's own app behaves too — it
  polls the same command on a timer with every lamp dark, roughly every 40
  seconds while its screen is open, so a check-in four times a day is a tiny
  fraction of the traffic the official app puts on the lamp.
- **A lamp that is out of range is not an error.** If the lamp cannot be reached
  — off-season, out of range, taken indoors — the last known level simply stays
  put and the entities keep working. The reading has always been "as of last
  contact" and still is.
- **An unpaired lamp is never contacted** by the check-in, so it can never set
  off the pairing sequence (which makes the lamp flash) unattended.

One thing is still unknown and worth knowing: whether the lamp *measures* the
level when asked, or reports a value it only refreshes while its LEDs are
running. If it is the latter, checking in on a lamp that has been dark for weeks
will keep reporting the level it had when it was last lit. Nothing in the
manufacturer's app answers this, so it needs observing over time on a real lamp.

## 0.6.1

No change to how the lamp behaves. This settles, on hardware, whether Home
Assistant can ever read the lamp's light state back — and removes the dead code
that implied it might.

The short answer is no, and 0.6.0 said so, but for the wrong reason twice over.

- **Reading state back is not possible on a MOOON! H134, and both candidate
  commands have now been tried on the lamp.** `DEVICE_DATA_GET` is refused; the
  command the official app actually uses is accepted, but the lamp answers it
  with a record that never changes — it reported the lamp off while it was lit,
  and returned byte-identical data across three on/off cycles.
- **The unused `get_state()` has been removed** rather than left as a method
  that looks usable and is not. Applying what that query returns would make
  things worse, not better: it would push Home Assistant to "off" while the lamp
  was on. The lamp's state in Home Assistant continues to come from the commands
  we send it.
- The reasoning, the frames and the traces are recorded in
  `docs/domain/LINKIO-PROTOCOL.md` so this is not investigated a fourth time,
  along with the two cases still worth testing: whether pressing the lamp's own
  button updates the record, and whether setting the lamp's clock unfreezes it.
- **A made-up error name is gone.** The error the lamp returns for that query,
  `18`, does not appear in the manufacturer's table at all — it was labelled
  `INVALID_SIZE` here, which is not their name, and reading it as the firmware
  complaining about payload size sent two separate investigations the wrong way.
  Unrecognised codes now log honestly as `UNKNOWN(18)`.

Also corrects the 0.6.0 documentation of the battery command, which was still
described as unimplemented after it shipped.

## 0.6.0

Battery-powered lamps now report their state of charge.

- **New: battery level and charging sensors.** A battery percentage and a
  charging on/off sensor appear as diagnostic entities on the lamp device. Both
  are read from the lamp itself — there is no GATT battery service, so this uses
  the same command the official app polls.
- The level refreshes **whenever a command reaches the lamp**, which for an
  adaptive-lighting-driven lamp means every adjustment. The lamp never speaks
  unprompted, so while it is switched off and nothing is talking to it, the
  reading holds its last known value rather than going blank — treat it as "as
  of last contact" rather than live.
- Until the lamp has reported a level at least once, both entities read as
  unavailable rather than 0 %, so a quiet lamp is never mistaken for a flat one.

Lamps that are not battery-powered (the Hoopik L1200 string light) simply never
report a level, and the entities stay unavailable.

The rest of this release is protocol fixes found by decoding the official Fermob
Lighting app. They are what made the battery reading possible: the request is an
acknowledged command and the answer is a pushed status, and both of those paths
were broken. Nothing here changes how the lamp behaves day to day — on/off,
brightness and colour temperature use a frame that was already correct.

- **Acknowledged commands used the wrong message type.** The constant for "command
  with acknowledgement" carried the value 2, which is the type the *lamp* uses
  for its replies; the app uses 1. The lamp therefore read our queries as
  acknowledgements and never answered them. Frame type is also independent of
  message type — it comes from the addressing mode — so the two are no longer
  conflated.
- **Solicited lamp state was discarded.** We listened only for unsolicited state
  pushes (message type 4, payload marker 146) and ignored the solicited forms
  (type 3, marker 147) that a reply to a query actually uses. Together with the
  message-type bug, this left no working path for reading state back at all.
- **A rejected command was indistinguishable from a successful one.** Only the
  sequence number was checked, never the error code the acknowledgement carries.
  In the pairing handshake that meant a rejected key exchange could have its
  error response stored as the lamp's private key, producing a paired-looking
  device that could never be reached, with nothing in the log to explain it.
  Rejections are now reported by name and treated as failures.

Note for anyone re-pairing: a handshake step the lamp rejects now fails
immediately and visibly, where it previously carried on and produced a broken
configuration entry.

Reading the lamp's *light* state back is still not possible. With the message
type corrected the lamp does now answer that query instead of ignoring it — but
what it answers is a rejection. The request is unchanged and still unused; the
rejection is followed up separately.

## 0.5.1

Fixes the colour-temperature scale on tunable-white lamps. The slider was
reporting a temperature the lamp was not emitting.

- **Colour temperature now interpolates in mired rather than in Kelvin.**
  Mixing two fixed-CCT channels is linear in mired (10⁶/K), so an even mix of
  the lamp's 3000 K and 6000 K channels is 4000 K — not the arithmetic mean of
  4500 K that the old mapping assumed. The previous scale overstated the
  temperature everywhere strictly between the endpoints, worst at a 4727 K
  slider where the lamp actually emitted about 4212 K, a 515 K error. The
  endpoints were always correct, so the effect was a slider that felt cooler
  than it read rather than a wrong colour at either extreme.
- **Ask for the temperature you want.** Requesting 4000 K now gives an even
  channel split; previously 4000 K asked for two-thirds warm and produced
  about 3600 K. If you have automations or scenes with a Fermob colour
  temperature tuned by eye against the old scale, they will shift warmer and
  are worth re-checking.
- `test_mix_is_linear_in_mired` pins round mired fractions so a revert to
  Kelvin-linear interpolation fails CI rather than merely looking slightly off.
  `docs/domain/LINKIO-PROTOCOL.md` records the model, the worked float
  tie-breaking example, and the one physical assumption behind it: mired
  linearity holds only if both channels emit equal luminous flux at equal drive
  percent, which Fermob does not publish. This is not calibrated against a
  meter.

## 0.5.0

**First release verified on hardware by this repository's own maintainer**, a
Fermob MOOON! H134: pairing, on/off, brightness and colour temperature.

- **The lamp family is now read from the lamp instead of guessed from its name.**
  `MODULE_INFO_GET` reports `module_type` (401 dimmable / 404 tunable) and a
  model string, both of which are persisted into the config entry. A renamed
  Hoopik — or a lamp whose name says nothing — is no longer misidentified. The
  name heuristic remains as the first-run guess and as the fallback for an
  unrecognised `module_type`; the manual override still wins over both.
- **Lamps paired with an earlier version pick this up automatically.** The
  connection re-requests `MODULE_INFO_GET` on reconnect until it answers once,
  then persists it — one extra round trip per install, not per connect.
- **The device page shows the real model** (e.g. `MOOON - H134`) instead of a
  family label, once the lamp has reported it.
- **`FermobBLEConnection` and `FermobLight` have tests for the first time**
  (`tests/test_light.py`, 31 of them): family resolution order, module-info
  persistence and its no-op cases, entity capabilities per family, and the
  command path's availability handling. The suite is now 840 tests. What remains
  untested is listed explicitly in `docs/tech/TESTING.md` — the pairing
  handshake, ACK matching and long-frame reassembly among them.
- **A real hardware capture is pinned as a test vector.** The H134's complete
  `MODULE_INFO_GET` response is in `tests/test_protocol.py` as
  `H134_MODULE_INFO`, the one expectation in that suite derived from a lamp
  rather than from our reading of the app.
- Recorded two dead ends confirmed on hardware, so nobody repeats them: the GATT
  table has **no Battery Service** (`0x180F`/`0x2A19` do not exist on this lamp)
  and **`DEVICE_INFO_GET` returns nothing usable**. The GATT service UUID
  `41c15000-…`, previously only inferred from a code comment, is now confirmed.

## 0.4.0

Prepares the repository for submission to the
[HACS default store](https://github.com/hacs/default). No functional change to
lamp control — the protocol, entity and pairing behaviour are untouched.

- **Added a brand icon** at `custom_components/fermob/brand/`, so the
  integration shows a proper icon instead of a placeholder. Home Assistant
  2026.3+ reads brand images from the integration directory; on older cores the
  icon is ignored and nothing else changes.
- **The HACS validation job no longer ignores the `brands` check.** It now
  passes on its own merit, which is a precondition for default-store inclusion.
- The icon is an original work under this repository's MIT licence — it is
  deliberately not Fermob's logo, which is not ours to relicense. Reasoning and
  the regeneration script are documented in `docs/tech/BRANDING.md`.

## 0.3.1

- Bump dependency (Dependabot)

## 0.3.0

First release of this fork. Carries upstream
[PR #2](https://github.com/edouardrosset/ha-fermob/pull/2) by
[@fjcompiled](https://github.com/fjcompiled) — MOOON! tunable-white support —
plus a hardening pass.

- **MOOON! support.** Tunable-white lamps (every MOOON! / table lamp) send a
  `DEVICE_DATA_SET` body with separate cold/warm channels instead of the
  Hoopik's single level byte, and expose a colour-temperature slider over
  3000–6000 K. Dimmable-white (Hoopik) frames are byte-identical to 0.1.0.
- **Lamp type override** under Configure → Lamp type, because the encrypted
  Linkio advertisement cannot reveal the model.
- **Fixed a dependency that could fail on a stock install.** The integration
  imported pycryptodome without declaring it; Home Assistant does not ship it.
  Now uses `cryptography`, which core always ships.
- **The BLE connection is released when the entry unloads or reloads.**
  Previously an unload left the link open, and these lamps accept one client at
  a time.
- **The light now reports itself unavailable** when a command fails, instead of
  showing its last known state as if it were still true.
- Removed a hardcoded fallback MAC address, a lamp-type detection branch that
  could never fire, and error handlers that swallowed failures silently.
- Corrected README claims that state is re-synced on reconnect — it is not.
- Added 794 unit tests over the protocol layer, and CI (ruff, pytest, hassfest,
  HACS validation).

## 0.2.0

Version proposed in upstream PR #2; never released.

## 0.1.0

Initial upstream release by [@edouardrosset](https://github.com/edouardrosset) —
Hoopik GL1200 (dimmable white) support.
