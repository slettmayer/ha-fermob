# Inbound State

> The device-data record the lamp pushes, byte by byte — and the marker rule that decides whether a record is
> believed. Maps to `parse_device_record` / `parse_device_state` in `custom_components/fermob/protocol.py`.

**Scope.** Parsing and trusting what the lamp sends us. What we send is in
[PROTOCOL-COMMANDS.md](PROTOCOL-COMMANDS.md) and [PROTOCOL-LIGHT-COMMAND.md](PROTOCOL-LIGHT-COMMAND.md).
*When* pushes arrive at all — and why the link is held open — is in [STATE-MODEL.md](STATE-MODEL.md).

## Marker 146 versus 147

Both `MSG_STATUS` and `MSG_EVENT` carry lamp state, and both `LMP_EVENT_DEVICE_DATA` (146) and
`LMP_STATUS_DEVICE_DATA` (147) mark it, with identical bodies — so all four combinations must be *parsed*
(`STATE_PUSH_TYPES`, `DEVICE_DATA_MARKERS`).

**Identical bodies, opposite trust.** Only **146** may be applied to an entity: it is the lamp volunteering a
change as it happens — *unsolicited*. **147** is the reply to `DEVICES_DATA_LIST_GET` (74) and is a *stored*
record — *solicited*, and on an H134 it reported the lamp off while it was lit. Nothing sends 74 any more, so
a 147 should never arrive; `_dispatch_event` refuses it explicitly anyway, because "the bodies look the same,
accept both" is precisely the mistake that pair of constants exists to prevent. The evidence that 147 is
frozen is in [DEAD-ENDS.md](DEAD-ENDS.md#reading-light-state-back-does-not-work).

## The record layout

`protocol.parse_device_record` reads a device-data push, identified by `payload[1]` being either marker in
`DEVICE_DATA_MARKERS`. `parse_device_state` is the same thing without the timestamp, for callers that do not
care. Whether a parsed record is *believed* is decided by the marker, in `_dispatch_event`.

```
payload[1]        146 = LMP_EVENT_DEVICE_DATA, 147 = LMP_STATUS_DEVICE_DATA (identical bodies)
payload[2]        dev_index — routes to a sub-device; always 0 for a single lamp
payload[3..6]     update timestamp, little-endian uint32 (see below)
payload[7]        status — must be 0, else we reject the frame
payload[8] & 0x0F is_on  (the high nibble carries led_mode, so mask it)
payload[8] >> 4   led_mode — not read yet; tells us whether a timer or effect is running
payload[9]        level (dimmable white) / cold_white (tunable white)
payload[10]       warm_white (tunable white); defaults to 0 if the payload is only 10 bytes
payload[11..14]   nothing. Not filler from the lamp — this is our own pad15 output
```

The caller interprets the two channel bytes according to its configured family — `protocol.py` does not know
which lamp it is talking to.

**Bytes 11–14 are confirmed empty.** Every device parser in the app — dimmable white, tunable white, RGBW,
temperature and the generic fallback — stops at byte 10. Nothing is hiding past `warm_white`.

## The timestamp is diagnostics only

**The timestamp at 3–6 is logged and nothing more.** The app uses it as a stale-frame guard, dropping any
frame older than the last it saw. We do not, and should not: the trust decision is the marker, and a
timestamp comparison against a lamp clock we cannot verify risks silently freezing state updates forever —
far worse than the stale frame it would prevent. It is carried on `DeviceRecord` purely as diagnostics,
because it is the only outside evidence that `DATETIME_SET` reached the lamp. An H134 that had never been
sent one stamped every record `37`.

A date guard was tried, in the form of a `STATE_RECORD_MIN_TIME` floor below which a record was not believed.
It was removed: it was a proxy for the marker check, which is exact, and it would have silently discarded
legitimate pushes from a lamp whose clock had not been set.

## The battery push

The battery reading arrives on the same characteristic but is not a device-data record: it is a `STATUS` push
with payload `[2, 0xC0, byte]`, where `percent = byte & 0x7F` and `charging = byte & 0x80`. The lamp sends one
when asked, and volunteers one whenever the charger goes on or off. See
[PROTOCOL-COMMANDS.md](PROTOCOL-COMMANDS.md#the-battery-command) for the request side and
[ENTITIES-AND-SERVICES.md](ENTITIES-AND-SERVICES.md#battery-entities) for how the figure should be read.
