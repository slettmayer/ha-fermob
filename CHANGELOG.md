# Changelog

## 0.10.2

- Bump dependency (Dependabot)

## 0.10.1

**HACS now downloads a single archive instead of file-by-file.** Nothing about
the lamp, pairing or any entity changed; no re-pairing needed, and there is
nothing to do on your side.

- **Releases now carry a `fermob.zip` asset, and HACS installs from it.**
  Previously HACS fetched each file of the integration individually through the
  GitHub API. One archive is faster, and it makes downloads countable — GitHub
  reports a download count per release asset, which is the only way this project
  can see whether anyone besides its author is running it.

  Downgrading to 0.10.0 or earlier still works: those releases have no archive,
  and HACS falls back to the file-by-file download for them.

## 0.10.0

**Your lamp's firmware version, and whether Fermob has published a newer one.**
Nothing about pairing or lamp control changed; no re-pairing needed.

- **The firmware and hardware version now appear on the device page.** Both come
  from the same reply that already tells us the model, so they cost no extra
  Bluetooth traffic — a lamp paired before this release reads them once, the next
  time it connects.

- **A new *Firmware* entity says whether a newer build exists.** It **cannot
  install** one: these lamps take a signed firmware image that only Fermob can
  produce and only their app can transfer, so the entity reports and points you
  at the app. Updating with the app means releasing the lamp from Home Assistant
  first (`fermob.unpair`) — which is why it is easier to update *before* pairing
  a new lamp. See the [README](README.md#firmware-updates).

  It reads **unknown** rather than "up to date" until a check has actually
  succeeded. For a lamp the manufacturer's server does not carry — it has no
  Hoopik entry at all — or an HA that cannot reach it, "up to date" would be a
  claim nobody ever verified.

  A lamp that has never been updated may well have one waiting — our own MOOON!
  H134 was on 2.3.21.0 against 3.0.27.0 on Fermob's server. Note the app names
  no version numbers while updating, so this entity may be the first place you
  see one. Firmware for these lamps is published rarely: the newest build for
  any model is from November 2023.

- **New option: *Check for firmware updates*, on by default.** This is the only
  thing the integration sends outside your network — one small HTTPS request per
  lamp per day (plus one when the entity is first added), to the manufacturer's
  release server, carrying nothing but the model name. Switching it off stops the
  request entirely; the entity stays and reports the installed version with the
  available one unknown, so nothing you renamed or built a dashboard on breaks.
  Hide it by disabling the entity itself if you would rather not see it.

- **The reference firmware is now 3.0.27.0**, the newest build Fermob publish for
  the MOOON! H134. That is what the lamp behind these releases runs and what
  brightness, colour temperature, pairing, reconnect and unpair are all tested
  against. Older firmware works too — the protocol was reverse-engineered on
  2.3.21.0 — so there is nothing here that asks you to update. If you do, and
  something misbehaves, please open an issue with the version, which as of this
  release you can read off the device page.

## 0.9.2

**Two responsiveness fixes and a documentation correction.** No protocol or
pairing changes; nothing here requires re-pairing a lamp.

- **A light command that cannot reach the lamp gives up roughly twice as fast.**
  Bluetooth allows 20 s per connect attempt and the retry count was the library
  default of four, so an out-of-range lamp left the UI unresponsive for over a
  minute — which reads as a hang. Light commands and `fermob.check_in` now take
  two attempts. This halves the ordinary out-of-range failure. It is not a hard
  ceiling: some Bluetooth errors retry on their own separate budget, and a
  command still waits for any check-in already in progress.

  Three paths deliberately keep all four attempts, because for them giving up
  early costs more than waiting does: the scheduled check-in (nothing is waiting
  on it), anything that pairs (including the automatic re-pair after a factory
  reset — reporting "pairing failed" on a lamp that would have paired is the
  worse outcome), and `fermob.unpair` (an unpair that gives up releases nothing,
  and the obvious next move is deleting the integration, which leaves the lamp
  needing a ten-second factory reset).

- **The battery entities populate one minute after a restart, not two**, with a
  second attempt at three minutes if the first found nothing. The first check-in
  is what fills them in and what opens the link that makes button presses
  visible; the retry covers a Bluetooth proxy that had not finished reconnecting,
  which previously cost a full check-in interval. Commands were never affected —
  the lamp is controllable as soon as setup finishes.

- **`fermob.unpair` really does release the lamp**, now confirmed on hardware: a
  lamp unpaired with the service re-adds and pairs again with **no factory
  reset**. Only *deleting* the integration leaves the lamp registered and needs
  the ten-second reset. The README and the pairing guide now say so explicitly,
  because the two paths were easy to confuse.

- **Documented honestly: neither service can be called on a light that is
  greyed out.** Home Assistant discards service calls to an unavailable entity
  and still reports success. You do not need `fermob.check_in` in that case —
  the *scheduled* check-in contacts the lamp regardless of entity state and
  restores it on its own, within 30 minutes on the default setting. Reloading
  the integration clears the greyed-out state at once if you would rather not
  wait — though note that a reload rebuilds the entity without contacting the
  lamp, so on a lamp still out of range it will simply fail again on the next
  command.

  This release briefly moved `check_in` off the entity platform to lift that
  limitation, and the change was **reverted before release**. Leaving the
  platform means reimplementing target expansion, concurrent dispatch,
  registration lifetime and per-entity permission checks — Home Assistant does
  all four for free — and the only thing gained was not waiting for a recovery
  that already happens by itself.

## 0.9.1

**Fixes a dead end in 0.9.0.** If you factory-reset your lamp and the scheduled
check-in noticed before you touched the light, the lamp became unrecoverable
without reloading the integration. Upgrade if you use 0.9.0.

- **A factory-reset lamp can be re-paired again.** 0.9.0 detected the reset
  correctly and then took the light *unavailable* — and Home Assistant silently
  discards every service call to an unavailable entity. So the one action that
  fixes it, turning the light on, never arrived; neither did `fermob.unpair`; and
  the check-in is not allowed to pair. The lamp sat greyed out with no way back
  except reloading the integration, which nothing told you to do.

  A reset lamp now stays **available**, because that is the truthful state: a
  command *will* work on it — it re-pairs. A lamp that is genuinely unreachable
  or deaf still goes unavailable, exactly as before.

- **`fermob.unpair` on a reset lamp says the right thing.** It reported *"Could
  not reach the Fermob lamp … to unpair it"* and then advised turning the light
  on to **re**-pair — the wrong diagnosis for a lamp that had just answered,
  followed by the opposite of what you asked for. It now tells you the lamp is
  already unpaired and that deleting the integration is the cleanup.

- **The check-in log now names the actual problem** instead of a fixed
  *"reachable but not answering"* summary, which discarded the only recovery
  instruction the integration ever gives — and described a lamp that had answered
  as not answering.

With thanks again to the hardware, which found all three.

## 0.9.0

**A lamp that stopped responding now recovers on its own.** If you are on 0.8.0
or 0.8.1 and your lamp shows as connected but ignores everything, with the
battery reading *unavailable*, this is the release for you. Upgrade; nothing to
re-pair, no settings to revisit.

- **A lamp that goes deaf is noticed and reconnected.** The link could be up, the
  entity available, and every command silently discarded by the lamp — with no
  error anywhere, because the frames that carry light commands are sent without
  asking for a reply and cannot fail. The battery request is the one thing the
  lamp does acknowledge, so an unanswered one is now treated as what it is: a
  dead session. The check-in reconnects, and the lamp comes back within one
  check-in interval (30 minutes by default) or immediately via `fermob.check_in`.

  This was a 0.8.0 regression, and an unlucky one. Until then the link was
  dropped 30 seconds after each command, which repaired this by accident, every
  time, before anyone could see it. Holding the link open — which is what makes
  a physical button press visible in Home Assistant — removed the accident
  without replacing it.

- **Pairing no longer leaves the lamp unresponsive until you reload.** The lamp
  stops honouring the link it was paired on the moment pairing completes, so
  pairing now reconnects before handing over. Previously the first commands
  after a fresh pairing went nowhere, and reloading the integration was the
  undocumented cure.

- **A lamp you factory-reset is detected and re-paired automatically.** Home
  Assistant kept its old keys and went on encrypting with them forever, against
  a lamp that could no longer read them — connected, available, and deaf, with
  no way out but deleting `.storage/fermob_*` by hand. A lamp that stops
  answering is now asked whether it still knows us, and re-paired if it says no.
  Usually it does not have to be asked at all: a reset lamp *says so*, and Home
  Assistant now understands the answer. A lamp that is answering normally is
  never asked, and one that is merely out of range is never mistaken for a reset
  one.

- **A lamp that cannot be woken is reported as unavailable instead of pretending.**
  A connection that comes up but gets no answer from the lamp is now a failed
  connection, so the light entity goes unavailable rather than accepting commands
  it cannot deliver — and the scheduled check-in updates that too, so a lamp that
  goes quiet is noticed without anyone pressing a switch. The lamp is always asked
  twice before any of this: one lost reply on a marginal link changes nothing.
  A lamp that simply cannot be reached — out of range, taken indoors for the
  winter — is left alone as before, and is not the same thing.

- **Deleting the integration now deletes its pairing keys**, so a lamp that is
  gone for good — dead battery, given away, already reset — leaves nothing behind.
  Note the consequence: deleting the integration and adding the same lamp back
  will not work, because the lamp is still registered to Home Assistant and the
  key it needs is gone. Hold the lamp's button for ten seconds first, or use
  `fermob.unpair` instead, which releases the lamp properly.

- **`fermob.unpair` no longer strands the lamp when it cannot be reached.** It
  used to delete the keys regardless, leaving the lamp registered to a Home
  Assistant that had forgotten it: the *"lamp is in PRIVATE mode but no stored
  keys"* dead end, which only a 10-second factory reset clears. It now checks
  that the lamp is listening first, and if it is not, it does not even send the
  unpair command — it reports an error and changes nothing. Bring the lamp in
  range and try again.

  Two cases where that error used to be simply wrong now say what is actually
  going on. A lamp you already factory-reset is not unreachable — it is answering,
  and what it is answering is that it is no longer yours; the service now tells
  you there is nothing left to release and to delete the integration. And an entry
  whose pairing never completed has nothing to release either, so it is removed
  without touching the radio, instead of failing with a message about range.

- **The background check-in can no longer pair a lamp.** It was only ever
  supposed to reconnect and read the battery, but the new re-pairing above could
  be reached from it, which meant a lamp you had reset to hand back to the Fermob
  app could be silently claimed again overnight. Pairing now only ever happens
  because you did something.

With thanks to Thomas Rehm, who hit all of this on real hardware and reported it
carefully enough to find.

## 0.8.1

**Correction.** This entry originally said the release fixed a bug where the
battery level and charging sensor could silently stop updating. **There was no
such bug**, and nothing was broken before this release. The claim rested on a
measurement mistake: Home Assistant's `last_reported` timestamp, read through its
API, is not refreshed for an update that repeats the previous value, so an entity
reporting an unchanged battery level looks frozen while working perfectly.
Reading the timestamp directly showed both entities updating on every report, 74
microseconds apart, exactly as they always had.

What the release actually contains:

- **An internal change to how the two battery entities receive their updates.**
  Each now holds its own subscription to the lamp's battery reports, released
  when the entity goes away, instead of the two sharing one that had to be handed
  along from whichever was set up first. The old arrangement worked; the new one
  is the standard Home Assistant pattern, is easier to reason about, and is
  covered by tests — which those two entities previously had none of at all.
- **Nothing about how the lamp behaves is different.** No new entities, no
  changed entity IDs, no settings to revisit, no reason to re-pair.

## 0.8.0

Home Assistant now notices when someone switches the lamp on at its own button.

- **Physical button presses show up in Home Assistant**, in about a second, and
  so do brightness and colour-temperature changes made at the lamp. The light
  entity finally follows the lamp rather than only remembering the last command
  Home Assistant sent. Taking the lamp on or off its charger is reported the same
  way.
- **This works by keeping the Bluetooth link open.** The lamp only reports
  anything while something is connected to it, and reports nothing when a
  connection is re-established — so the old behaviour of hanging up 30 seconds
  after each command meant nearly every press went unseen. The manufacturer's own
  app does exactly this: it never asks the lamp anything, it just stays connected
  and listens.
- **New option: Configure → Connection.** *Always connected* is the default and
  the one that makes presses visible. *On demand* restores the old behaviour and
  hands back a connection slot on your Bluetooth adapter or proxy — useful if it
  is running near its limit, since proxies typically allow three devices. Presses
  are not reported in that mode.
- **The battery cost was measured, not guessed:** about 0.1 % per hour with the
  link held open, roughly 2 % a day. A lamp left on its stand will not notice.
- **The check-in now also repairs a dropped connection.** It runs every 30
  minutes in *always connected* mode, because nothing else would notice if the
  link went away — a Bluetooth proxy rebooting, say. It still never turns the
  lamp on or off, and it still refreshes the battery level.
- **New `fermob.check_in` service** — do that now, rather than waiting for the
  timer.
- **The lamp is told the time**, when it pairs and on every connection, which is
  what the official app does and this integration never did.

**Reading state back on demand is now known to be impossible**, rather than
merely untried. Both candidate commands were tested on hardware and the official
app sends neither: one is refused outright, and the other returns a stored record
that never changes — on an H134 it reported the lamp off while it was lit.
Holding the link open is not a workaround, it is the only mechanism there is.

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

- **The percentage reads high while the lamp is charging.** It jumps as soon as
  the charger goes on — 24 % straight to 33 % when this was tested — which is
  quicker than a battery can actually take charge, so the lamp is very likely
  reading voltage rather than counting capacity. Treat the level as optimistic
  whenever the charging sensor is on, and take the real figure once the lamp has
  been off the charger for a while.

A lamp left off does keep reporting a falling level, so the check-in is not
looking at a stale number — that was measured over an afternoon with the lamp
dark. Whether it stays truthful over *weeks* of darkness is still unknown, and
the reading is unlikely to be evenly spaced across the whole range given the
above, so treat the middle of the scale as more meaningful than either end.

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
