# Dead Ends — Do Not Re-Litigate These

> Things that were tried and do not work, each with the reason. This file exists so a future session does not
> spend an evening rediscovering one of them. Add to it rather than deleting from it.

**Scope.** Negative results only, across the whole domain — state reads, reconnect behaviour, discovery, and
battery-related brightness limiting. The positive protocol description lives in
[LINKIO-PROTOCOL.md](LINKIO-PROTOCOL.md) and its sub-files.

## Reading light state back does not work

**Settled on hardware 2026-08-03, both candidate commands tried.** This entry replaces two earlier wrong
versions: first "gateway mode refuses the query" (the frame was simply malformed, message type 2 =
`CMD_ACK`), then "the body is the wrong size" (it is not — see below). The header fix, and accepting the
`MSG_STATUS`/marker-147 reply form, were both real and are kept; they are what made the probe legible.

**`DEVICE_DATA_GET` (66) is rejected with error 18.** Not because our body was wrong: the app's
`requestModuleLightState` sends `[14, 66, 0]` + twelve `0xFF`, which is byte-for-byte what we sent. **Why the
firmware refuses it is unexplained.** Do not reshape the payload; that was tried and it is not the problem.

Two proposed explanations have already been ruled out, so do not re-propose them:

- **Not the module role.** The app's poll loops skip only modules whose `m_module_role === LEAF` (6), and this
  lamp reports `LMP_PARAM_ROLE = 0` (`NODE`) in the capture pinned at
  [`tests/test_protocol.py`](../../tests/test_protocol.py) — the app would have polled it.
- **Not the payload length**, per the byte-for-byte match above.

Note too that **`18` is not in the app's `lmp_error_codes_e` table at all** (it stops at 20 `ITEM_NOT_FOUND`,
with no 18); the name `INVALID_SIZE` is this integration's own invention in `protocol.py`, not the
manufacturer's, and it should not be read as the firmware telling us anything about size.

**`DEVICES_DATA_LIST_GET` (74) is the app's real state read, and the lamp accepts it.** Sent by
`requestLatestsModuleStatuses` as `CMD_WITH_ACK` + SHORT with body
`[12, 74, 255, 255, dev_index, 0,0,0,0, <local time, LE uint32>]`; the direct-connection form puts the short
address in bytes 2–3 instead. Both forms were verified on the H134: success ACK, followed by a `DEVICE_DATA`
push (`mt=4`, marker 147) — the whole path works end to end, and `parse_device_state` reads it correctly.

**But the record it returns is frozen, which is why nothing sends it.** Eight reads across ~5 minutes and
three on/off cycles came back byte-identical — `0a9300250000000010191900ffffff` → `is_on=False, ch1=25,
ch2=25` — *including* reads taken while the lamp was lit, and including the bytes at 3–6 that the app treats
as a timestamp. The channel values never track what we actually commanded either (adaptive lighting varies
them continuously; the record says 25/25 forever).

**The clock hypothesis was tested, and it is wrong.** Those timestamp bytes read `0x25` = **37**, where
`getLocalTime()` produces a Unix-scale seconds value, so the lamp's clock plainly never started — it is set by
`LMP_COMMAND_DATETIME_SET` (26), payload `[5, 26, <local time, LE uint32>]` as `CMD_WITH_NO_ACK` + SHORT +
PRIVATE, which this integration never sent. Sending 26 before 74 was tried on hardware. The record stayed
frozen. We still send 26, because the app does and the lamp keeps those records for it, but it buys us nothing.

An earlier hypothesis — that the record only follows an *acknowledged, addressed* `DEVICE_DATA_SET` where we
use `MSG_FIRE` — is **refuted**: the app's own `Module.sendCommand` sends `DEVICE_DATA_SET` as
`CMD_WITH_NO_ACK`, exactly as we do. ACK-vs-FIRE is not the difference.

**And the whole question is moot, settled by a decrypted capture of the app's own BLE traffic (2026-08-04):
the app never sends 74 at all.** `requestLatestsModuleStatuses` builds the command; nothing in the capture
transmits it. The app reads no lamp state, ever. It holds the BLE link open and consumes the pushes the lamp
volunteers — which is what this integration does. Do not revive either read command.

## The lamp emits no EVENT after a plain BLE reconnect

Only the post-`REGISTER_END` EVENT during first pairing arrives unsolicited. This is *why* the link is held
open rather than re-established on demand: there is no resync, so a link that was down during a button press
has lost that press permanently. See [STATE-MODEL.md](STATE-MODEL.md).

**What the lamp does push, while connected, is everything we need** — confirmed in the same capture and then
on hardware. Every physical button press produces an unsolicited `EVENT_DEVICE_DATA` (marker 146) carrying the
correct on/off and both channels; every charger connect or disconnect produces a battery push (`0xC0`). A
captured press decodes as `0a9200e868726a00110032` → on, cold 0, warm 50, and is pinned in
[`tests/test_light.py`](../../tests/test_light.py) as the one piece of inbound evidence that is not a
restatement of our own encoder.

The cost of holding the link is small and mostly *not* battery — the real price is a connection slot on the
adapter or BLE proxy, which is what the on-demand connection mode exists to hand back. Measurements and their
caveats are in [STATE-MODEL.md](STATE-MODEL.md#what-holding-the-link-costs).

## The post-REGISTER_END EVENT's state payload is useless to us

Connections are only ever established *from* a command, so whatever state it reports is overwritten a few
milliseconds later by the command that triggered the connection. The EVENT is still waited for — as a
gateway-mode confirmation and settle gate — but its contents are only logged.

## The model is not in the advertisement

It rotates and is encrypted, so `module_type` (401 dimmable / 404 tunable) cannot be sniffed *before*
connecting — a branch that attempted this was removed as dead code. It **is** readable after connecting, from
`MODULE_INFO_GET`; that is a different question and it is now answered. See
[DEVICES.md](DEVICES.md#lamp-family-detection).

## There is no battery level in the GATT table, and none in DEVICE_INFO_GET

Both were checked on hardware — see the GATT table in [TECH-STACK.md](../tech/TECH-STACK.md#bluetooth). That
part stands, and it was never the right place to look: **battery is a module-level command,
`MODULES_BATTERY_LEVEL_GET` (44)**, documented in
[PROTOCOL-COMMANDS.md](PROTOCOL-COMMANDS.md#the-battery-command). Of the three candidates this entry used to
list, "past byte 10" is now positively ruled out and "commands absent from our constant list" was the right
one.

## Brightness that feels capped is the lamp's own power management

Brightness that feels capped or inert at the top of the slider is the lamp limiting its own output on battery,
**not a mapping bug.** Two separate observations, one mechanism.

First, "brightness does nothing between 100 % and 20 %" — reported on an H134 at ~24 % charge: the top of the
slider felt inert, with the whole perceptible range crammed into roughly 20 % down to 1 %. Re-tested at 33 %
and on the charger, 3000 K and 6000 K both showed a clear 20 %-vs-100 % difference. The likely mechanism is
the lamp current-limiting its LED driver as the cell sags, so every high setting clamps to whatever the
battery can actually deliver. Check the state of charge before suspecting the code.

Second, **output is capped by simply being off the charger, at a healthy state of charge.** Observed on an
H134: switched on at 100 % while sitting on its stand, then lifted off it — output drops to roughly half and
stays there. Same mechanism as above, no cell sag required.

**There is nothing to send about it.** The official app has no setting for it and does not document it: its
FAQ covers runtime ("Mooon! up to 6 hours at 100 %, 12 hours at 50 %") and the ByPass that lets the lamp run
while charging, but says nothing about reduced output on battery — and the app has no lamp-configuration
surface at all, see [APP-CAPABILITIES.md](APP-CAPABILITIES.md). The app is charger-blind by construction, too:
`charging` appears only where it parses the battery byte and where it draws a lightning-bolt icon, `isH134()`
only picks a CSS image container, and its light maths — `cold = ⌊brightness/100 × (100 − heat)⌋`,
`warm = ⌊brightness/100 × heat⌋` — carries no cap and no charger term, exactly like ours. So parity with the
app buys nothing here. Derived from the decompiled app (2026-08-04); the firmware mechanism itself is
inferred, not observed.

Nothing in the brightness path is temperature-asymmetric, which is the other reason to look at the battery
first: `warm = level × warm_ratio`, `cold = level − warm`, so the **total drive is 100 units at full
brightness for every colour temperature**. A corollary worth knowing before designing a test — comparing
4000 K against 6000 K at full brightness proves nothing, because our model sends the same total in both cases.
That comparison was tried and it cannot discriminate anything.

Related but *not* a defect: the app's dimmable path (`sendGroupDimmableLightCommand`) sends `cold = warm = n`,
both strings at once, so the lamp can emit roughly twice what we ever ask for at neutral white. We spend a
fixed budget across the two channels instead, which is why 4000 K at full is no brighter than 6000 K at full
even though two strings are lit. That is a deliberate consequence of treating brightness as a budget, and
changing it would alter the brightness-versus-temperature relationship users are accustomed to. Revisit only
if more output at mid temperatures is actually wanted.

A brief flicker when the colour temperature changes is expected: `FADE` is 50 ms, taken from the app's
`fade_timing_10.color_transition`, and a temperature change moves both channels at once.
