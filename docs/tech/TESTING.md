# Testing

```bash
pip install -r requirements_test.txt
python -m pytest tests/ -q          # 1121 tests, ~12 s (most of it importing Home Assistant)
```

CI runs the same suite with `-v` (`.github/workflows/validate.yml`), so a local `-q` run and the `Pytest` job
differ only in output verbosity.

Seven modules with deliberately different needs:

| Module | Needs | Covers |
|---|---|---|
| `tests/test_protocol.py` | nothing but `pytest` + `cryptography` | frames, crypto, payloads, TLV parsing |
| `tests/test_light.py` | a real `hass` (`pytest-homeassistant-custom-component`) | family resolution, module-info persistence, entity capabilities, the command path, which pushes are believed |
| `tests/test_connection_profile.py` | the `fermob` package (no radio) | how the connection-mode option maps to an idle timeout and a check-in interval |
| `tests/test_battery_entities.py` | a real `hass` | the two diagnostic entities: that both get every battery push, and that each subscription dies with its entity |
| `tests/test_firmware.py` | the `fermob` package (no radio, no network — the aiohttp session is a stub) | the release-server client: the URL shape, an error envelope inside a `200`, a non-JSON body, and which of those makes the fallback host fire |
| `tests/test_update_entity.py` | a real `hass` | the firmware entity: that it never advertises `INSTALL`, that a lamp on current firmware reads as up to date, and that the option being off adds no entity |
| `tests/test_session_recovery.py` | a real `hass` | the mechanisms that recover a link the lamp has stopped honouring — the reconnect after pairing, the retried battery ACK as liveness signal, the gated factory-reset probe, the check-in that never pairs and reports its verdict, the unpair that neither broadcasts nor removes when the lamp is silent, and which connect budget each caller gets |

`protocol.py` stays HA-free so the first of those keeps running on a bare install. `firmware.py` is HA-free
too, but its tests reach it through the package, so they need the harness like the rest -- what the injected
session buys there is a suite that never touches the network, not a second bare-install module. `requirements_test.txt`
installs everything for both, because CI runs them together.

## How the test module imports the code

`tests/test_protocol.py` loads `protocol.py` **by file path** via `importlib`, not as
`custom_components.fermob.protocol`:

```python
_PATH = (
    Path(__file__).resolve().parents[1] / "custom_components" / "fermob" / "protocol.py"
)
_SPEC = importlib.util.spec_from_file_location("fermob_protocol", _PATH)
```

Importing it as a package member would execute `custom_components/fermob/__init__.py`, which imports Home
Assistant — defeating the whole arrangement. The `# noqa: E402` on the subsequent import is required because
the loader must run first.

## What the tests actually establish

**They pin our layout and our intent. They are not verification against the official Fermob app** — nobody
here can run it. The test module docstring says this explicitly; keep it that way.

One test is stronger than the rest:

- **`test_dw_payload_matches_upstream_literal`** writes the dimmable-white body out longhand (`[6, 0x41, 0x00, on_byte, level, 50, 0]`) instead of deriving it from the implementation. That makes upstream PR #2's "the Hoopik path is byte-identical" guarantee *enforced* rather than asserted in prose. If you refactor payload building, this is the test that catches a drift.

The rest, by area:

| Area | Coverage |
|---|---|
| Tunable-white mixing | `warm + cold == level`, both within 0–100, across **every** level 0–100 at seven colour temperatures |
| Colour temperature | Endpoints (3000 K = all warm, 6000 K = all cold, **4000 K = even** — the mix is mired-linear, not Kelvin-linear), round mired fractions pinned so a Kelvin-linear regression fails, exact round-trips every 50 K across the envelope, clamping outside it |
| Payload shape | Both family bodies, the shared `0x11`/`0x10` on-byte, level clamping, little-endian fade |
| Crypto | `ENCRYPT_NONE` passthrough, symmetry (encrypt twice = original) in both keyed modes, public and private keys producing different output |
| Framing | 20-byte length, header bit packing, frame type per message type, short-address bytes, XOR CRC, `pad15` terminator-then-filler including the exactly-15 case, long-frame fragment indices and counts, decode round-trips in all three modes |
| Inbound parsing | The `& 0x0F` on-state rule (high nibble is `led_mode`), rejection of short and non-zero-status payloads, the 10-byte dimmable-white response with no warm byte, MODULE_INFO TLV walking and its defaults |

## Adding tests

- **Pure protocol logic → `tests/test_protocol.py`.** Prefer `@pytest.mark.parametrize` over loops; the level×temperature matrix is why the count is in the hundreds rather than ~40, and it costs nothing.
- **Anything needing a `hass` instance → `tests/test_light.py`.** The harness is already installed; see [TECH-STACK.md](TECH-STACK.md#test-dependencies) for why `asyncio_mode` must stay set alongside it.
- **Golden frame hex is discouraged** unless it comes from a real capture. A hex string generated by this code and pinned as "expected" only proves the code equals itself; the longhand-literal approach above is stronger.

## Known blind spot in the tunable-white coverage

Exact warm/cold splits are pinned at a handful of points, but **no pinned case lands on a `.5` rounding tie**,
so the half-to-even tie-breaking rule itself is unverified. Details and the reasoning are in
[PROTOCOL-LIGHT-COMMAND.md](../domain/PROTOCOL-LIGHT-COMMAND.md#rounding-ties-are-unspecified) — kept there
to avoid two copies.

## Verifying on a live lamp: `last_reported` lies

When checking by hand whether an entity is still being updated — the natural question for a push-only
integration — **do not read `last_reported` through the HA API.** It goes stale and makes a healthy entity look
frozen.

In `StateMachine.async_set_internal`, a write whose state *and* attributes repeat the previous value takes the
`same_state and same_attr` branch: it mutates `old_state.last_reported` in place, sets only
`_cache["last_reported_timestamp"]`, fires `EVENT_STATE_REPORTED`, and returns. `_as_dict`, `as_dict_json` and
`json_fragment` are `under_cached_property`, filled lazily on first serialization, and none of them is
invalidated. So once anything has serialized that State, every later report-only update is invisible to an API
reader — and a battery sensor sitting at a steady percentage reports nothing but its last *change*.

Read the live object through the template engine instead:

```jinja
{{ states.sensor.<lamp>_battery.last_reported }}
```

This is not hypothetical. On 2026-08-05 the API view was taken at face value, a non-existent bug was diagnosed
from it, and 0.8.1 shipped with release notes describing a failure that never happened; both were corrected. A
single unchanged-write "control" is not enough to trust the API value — the one that passed did so only because
it was the first serialization after a value change, the single case where the API *is* fresh.

Related trap: reading state in the same batch as the action that triggers the write races it. Sequence the read
after the write has landed.

## What is not tested

`tests/test_light.py` closed the worst of this gap, but not all of it. Still verified only by running against
a real lamp:

- **The pairing handshake** — all ten steps, in order, including the nonce/authkey exchange and `REGISTER_END`.
- **ACK matching and long-frame reassembly** in `_send_frames`: the message-type-2 + sequence-number rule, the EVENT-arrives-while-waiting case, fragment ordering, and the 3 s timeout.
- **The idle disconnect's real timing** — whether the timer actually fires after its delay, and how that interacts with the connection lock. The tests cover which mode arms a timer at all and that each command defers it; `test_commands_are_serialised_by_the_connection_lock` covers the lock itself, not the timer.
- **The options flow**, and `async_unpair`'s effect on the lamp.

All of those need a faked `BleakClient` driving the notification callback, which is a bigger job than what is
here. The tests that exist use `AsyncMock` on the connection's own methods instead — which is why they say
nothing about whether the bytes on the wire are right. `tests/test_protocol.py` is what covers the bytes.
