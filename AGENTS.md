# Fermob (ha-fermob)

> Home Assistant custom integration for Fermob Bluetooth lamps — MOOON! (tunable white) and Hoopik GL1200 (dimmable white) — over local BLE, no hub and no cloud. One exception, opt-out: a daily firmware-version check against the vendor's release server.

> **Editing this guide:** `AGENTS.md` is the single source of truth for project context, read by all AI
> coding agents and humans. Keep it concise — put detail in `docs/` and link it. When you change code that
> alters documented behaviour, update the matching `docs/` file in the **same PR** — see
> [docs/README.md](docs/README.md) for the doc-structure contract.

## Quick Reference

- **Build**: none — pure Python custom component distributed via HACS
- **Run**: load into Home Assistant (HACS custom repository, or copy `custom_components/fermob/`)
- **Test**: `pip install -r requirements_test.txt && python -m pytest tests/ -q` (1103 tests, ~12 s — `test_protocol.py` needs no Home Assistant, the other six use its test harness)
- **Lint**: `ruff check . --fix && ruff format .`
- **Release**: merge to `main` with a bumped `manifest.json` version and a matching `CHANGELOG.md` section — `release.yml` tags and releases it automatically

## Where to Find Things

| I need to... | Read |
|--------------|------|
| Understand the architecture | [ARCHITECTURE.md](docs/tech/ARCHITECTURE.md) |
| Write code that fits conventions | [CONVENTIONS.md](docs/tech/CONVENTIONS.md) |
| Know the tech stack | [TECH-STACK.md](docs/tech/TECH-STACK.md) |
| Write or run tests | [TESTING.md](docs/tech/TESTING.md) |
| Understand CI / ruleset / release | [INFRASTRUCTURE.md](docs/tech/INFRASTRUCTURE.md) |
| Know how this fork relates to upstream | [UPSTREAM.md](docs/tech/UPSTREAM.md) |
| Change the icon, or understand the licence stance | [BRANDING.md](docs/tech/BRANDING.md) |
| Get oriented in the domain | [docs/domain/OVERVIEW.md](docs/domain/OVERVIEW.md) — index, concept catalog, glossary |
| Know which lamp is which, or add a model | [DEVICES.md](docs/domain/DEVICES.md) |
| Work on frames, crypto or payloads | [LINKIO-PROTOCOL.md](docs/domain/LINKIO-PROTOCOL.md) — protocol index |
| Understand state freshness or connection modes | [STATE-MODEL.md](docs/domain/STATE-MODEL.md) |
| Change an entity, service or option | [ENTITIES-AND-SERVICES.md](docs/domain/ENTITIES-AND-SERVICES.md) |
| Debug pairing, ownership or resets | [PAIRING.md](docs/domain/PAIRING.md) |
| Answer a firmware-update question | [FIRMWARE-UPDATE.md](docs/domain/FIRMWARE-UPDATE.md) — analysed, deliberately not implemented |
| Check whether something was already tried | [DEAD-ENDS.md](docs/domain/DEAD-ENDS.md) |

## Architecture Overview

Nine modules in `custom_components/fermob/`. `protocol.py` is a **pure** layer — frame building, AES-ECB
keystream crypto, payload construction, inbound parsing — with **no `homeassistant` imports**, so it is unit
testable on its own. `light.py` holds `FermobBLEConnection` (BLE link, pairing handshake, key persistence,
frame send/ACK matching, idle disconnect) and `FermobLight` (the HA entity). `config_flow.py` handles
Bluetooth discovery, manual add, and the lamp-type options flow. `entity.py`, `sensor.py` and
`binary_sensor.py` add the two diagnostic battery entities on top of the same connection, with no BLE logic of
their own. `firmware.py` is the second HA-free module — the vendor release-server client, session injected —
and `update.py` is the firmware entity built on it. `__init__.py` forwards the platforms and reloads the entry
when options change. There is no coordinator and no light polling: state is pushed by our own commands and by
EVENT notifications while the link is up (the battery is the one scheduled read; the firmware check is the one
daily poll). See [ARCHITECTURE.md](docs/tech/ARCHITECTURE.md).

## Tech Stack

- Python 3.14; Home Assistant Core (min `2024.4.0`).
- `cryptography` for the AES-ECB keystream — **shipped by HA core, deliberately absent from `requirements`** so we never fight core's pin. Never reintroduce pycryptodome; core does not ship it.
- `bleak` + `bleak_retry_connector` via HA's Bluetooth stack; `dependencies: ["bluetooth_adapters"]`.
- Ruff (lint + format); pytest. No runtime PyPI requirements; HACS-distributed. See [TECH-STACK.md](docs/tech/TECH-STACK.md).

## Core Conventions

- Keep `protocol.py` free of `homeassistant` imports — that is what makes the frame layer testable without a
  `hass` instance.
- All protocol constants (commands, encryption modes, message types, light families, Kelvin envelope) live in
  `protocol.py` — never inline literals.
- Import at **module level**, never inside a coroutine: HA imports integration modules in an executor, and an
  in-loop import trips core's blocking-call detection. Two sanctioned exceptions only — `__init__.py` importing
  `light` (circular), and the test module's file-path load.
- Every **light** command goes through `FermobLight._async_send_led()`, which owns the connect/send/failure/availability
  path — do not add a second copy. `async_unpair()` is the one deliberate exception: it tears the entry down, so it has
  no availability state to maintain.
- Lamp families are the strings `LIGHT_TYPE_DW` / `LIGHT_TYPE_TW`; anything family-dependent branches on those,
  not on model names.
- **Push subscriptions are lists with removal, never assignable callback slots.** Subscribe via
  `conn.add_battery_listener()` / `add_state_listener()` and hand the returned unsubscribe callable to
  `Entity.async_on_remove`. A single assignable slot forces the second subscriber to chain onto the first and
  offers no way to unchain — see
  [ARCHITECTURE.md](docs/tech/ARCHITECTURE.md#push-subscriptions-are-lists-with-removal-not-assignable-slots).
- Ruff-enforced: 4 spaces, double quotes, line length 88, rule set `E,W,F,I,UP,B,SIM,C4,RUF`. See
  [CONVENTIONS.md](docs/tech/CONVENTIONS.md).

## Business Domain

Fermob's lamps speak the Linkio BLE protocol: an encrypted, rotating advertisement, a one-time pairing
handshake that exchanges keys and puts the lamp in "gateway" state, then AES-ECB-obscured 20-byte frames on
a single characteristic. Two LED families share everything except the light command body — dimmable white
(Hoopik) carries one `level` byte, tunable white (every MOOON!) carries separate `cold_white`/`warm_white`
channels whose sum is the total output, which is how colour temperature is expressed. See
[docs/domain/OVERVIEW.md](docs/domain/OVERVIEW.md).

## Structural Risks

- **Reverse-engineered, not vendor-supported.** The protocol came from the official app's JS, a decrypted
  capture of its BLE traffic, and hardware tests — never from Fermob, who document none of it. The framing,
  crypto and handshake in particular have never been checked against the running app, and our tests pin *our*
  layout and intent only. Every domain doc marks which of the three a claim rests on; keep it that way. See
  [UPSTREAM.md](docs/tech/UPSTREAM.md).
- **Lamp-family detection resolves in three tiers, and only the last is a name heuristic.** Explicit override,
  then the `module_type` the lamp reported via `MODULE_INFO_GET`, then `"hoop"` in the name → dimmable white.
  The name is the **first-run guess only**: the model cannot be read from the advertisement (rotating and
  encrypted), so tier 2 needs one connection first. Do not describe this as name-based — the user-facing
  options label still does, which is a known string bug. See
  [DEVICES.md](docs/domain/DEVICES.md#lamp-family-detection).
- **There is no way to read the lamp's state, and holding the BLE link open is the only mechanism there is.**
  Settled by a vendor-app packet capture (2026-08-04) plus hardware tests: `DEVICE_DATA_GET` (66) is refused
  with error 18; `DEVICES_DATA_LIST_GET` (74) returns a stored record that never changes; and the app sends
  neither — it holds the link and listens to what the lamp volunteers. Two things to know before touching this:
  only marker **146** may reach an entity (147 is the stale record), and the check-in is the **only** thing that
  reconnects after an unexpected drop, so lengthening its interval directly lengthens how long the entity can
  show confidently stale state. See [STATE-MODEL.md](docs/domain/STATE-MODEL.md) and
  [DEAD-ENDS.md](docs/domain/DEAD-ENDS.md).
- **The battery ACK has three meanings, and `CRYPT_MSG` is the counter-intuitive one.** A refusal normally
  proves the lamp is listening — except `CRYPT_MSG` (7) and `UNREGISTERED` (5), which are the lamp saying it
  cannot decrypt us. Confirmed on hardware: a factory-reset lamp answers `CRYPT_MSG` rather than going silent,
  and a release that read every refusal as "listening" could not detect a reset at all. Branch on
  `BatteryVerdict`, never on a bare bool.
- **Almost nothing we send is acknowledged, so a dead session is invisible.** `send_led`, `DATETIME_SET` and
  `UNREGISTER` are all writes-without-response and cannot fail; `is_connected` stays true on a link the lamp
  has stopped honouring. The **battery ACK is the only acknowledgement there is**, which is why
  `request_battery()` returns a bool and the check-in reconnects when it comes back false. Three separate 0.8.x
  failures traced to this one blind spot — see
  [STATE-MODEL.md](docs/domain/STATE-MODEL.md#it-is-also-the-liveness-probe). Do not add a command path that
  assumes a successful write means the lamp heard it.
- **`ensure_connected()` is a two-pass loop, and neither extra step is a redundant round trip.** Pairing
  reconnects afterwards, because the lamp stops honouring the link it was paired on once `REGISTER_END` lands.
  And a battery request unanswered **twice** triggers `_lamp_still_paired()`, because a lamp factory-reset behind
  our back is otherwise a silent, permanent dead end. That probe is `REGISTER(0)` — a **pairing** frame whose
  effect on an already-registered lamp is unknown — so it must stay behind the failure and never move onto the
  happy path. See [PAIRING.md](docs/domain/PAIRING.md#when-the-lamp-does-not-answer-is-this-still-our-lamp).
- **An entity service cannot be called on an unavailable entity, and the call still reports success.**
  `entity_service_call` -> `_resolve_entity_service_call_entities` filters on `entity.available` before the
  handler runs (it does log *"Referenced entities ... are missing or not currently available"*, but under
  `homeassistant.helpers.service`, so a log grepped for `fermob` shows nothing). That is what made the 0.9.0 bug
  unrecoverable, and **0.9.1 fixed it at the source** by keeping a `KEYS_REJECTED` lamp *available*. Both
  services still carry the limitation, deliberately: 0.9.2 moved `check_in` to a domain service to lift it and
  **reverted**, because leaving the platform means reimplementing target expansion, concurrent dispatch,
  registration lifetime and per-entity permissions, and the only gain was not waiting for a recovery that
  already happens by itself. The scheduled check-in calls the connection directly and is unaffected. See
  [ENTITIES-AND-SERVICES.md](docs/domain/ENTITIES-AND-SERVICES.md#neither-can-be-called-on-an-unavailable-entity-and-that-is-accepted).
- **The connect budget is deliberately asymmetric, and `ensure_connected` defaults to the slow one.**
  `bleak_retry_connector` hardcodes 20 s per attempt, so `max_attempts` is the only lever: a light command and
  the `fermob.check_in` service pass `CONNECT_ATTEMPTS_INTERACTIVE` (2) because a human is watching, everything
  else keeps 4. The default is the background value so forgetting the argument costs latency, never a lamp
  given up on too early. **Two paths override the caller and must keep doing so** — *any* connect on a pass
  that pairs (both of them: `have_keys` is the marker, false on a first pairing and cleared again before the
  re-pair `continue`, and the only caller that ever pairs is a light command asking for 2) and `fermob.unpair`
  (giving up removes nothing, and the user's fallback is deleting the entry, which is the one-way door). **Do not quote `attempts x 20 s` as a worst case** -- transient errors retry on a separate
  9-attempt budget, each attempt also sits under a 60 s safety timeout, and a command waits on the connection
  lock first. It bounds the ordinary out-of-range failure and nothing more.
- **Only a user action may pair.** `async_check_in` passes `ensure_connected(allow_pairing=False)`; a
  key-presence guard is *not* sufficient, because a factory-reset lamp leaves our keys on disk. Pairing flashes
  the lamp and takes ownership of it, and the owner may have reset it on purpose to free it for the vendor app.
- **Entry removal deletes the pairing keys, and that makes "delete and re-add" a one-way door** — the lamp stays
  registered while its key is gone, so the re-add needs a 10-second factory reset first. Deliberate: it is the
  cleanup path for a lamp that is gone, since `fermob.unpair` refuses on an unreachable one. Documented in
  `async_remove_entry`, [PAIRING.md](docs/domain/PAIRING.md#unpairing) and the README troubleshooting table.
- **The idle timeout and the check-in interval are coupled, and must stay derived from one option.** A check-in
  calls `ensure_connected()`, which re-arms the idle timer — so an interval shorter than the timeout holds the
  link open no matter what the timeout says. `resolve_connection_profile` sets both from `connection_mode` for
  exactly that reason; do not expose them as two independent numbers.
- **The lamp limits its own light output on battery** — roughly half off the charger, worse at a low state of
  charge. Firmware behaviour with **no setting anywhere**: the official app has no lamp-configuration surface at
  all and never sends the config commands its own enum defines. Do not go looking for a command to send, and do
  not treat a "capped brightness" report as a mapping bug. See
  [APP-CAPABILITIES.md](docs/domain/APP-CAPABILITIES.md).
- **One controller at a time.** Pairing makes Home Assistant the owner; the Fermob app cannot connect while HA
  holds the link, and re-pairing from the app invalidates our stored keys. See
  [PAIRING.md](docs/domain/PAIRING.md).
- **The firmware entity reports and must never install.** The lamps take a *signed* Nordic Secure DFU image
  only Fermob can produce, so `UpdateEntityFeature.INSTALL` would be a button nothing implements — a test pins
  its absence. The daily release-server check is also the **only non-local traffic in the integration**, which
  is why it is an option and why it asks for metadata, never an image. See
  [FIRMWARE-UPDATE.md](docs/domain/FIRMWARE-UPDATE.md).
- **`config_flow.py` keeps its own copy of the lamp-family strings** (`LIGHT_TYPE_AUTO/DW/TW`) instead of importing them
  from `protocol.py`, so the two must be kept in sync by hand. See
  [CONVENTIONS.md](docs/tech/CONVENTIONS.md#protocol-code).
- `hacs/action@main` and `home-assistant/actions/hassfest@master` are floating CI refs. The HACS action runs with
  **no ignored checks** — reintroducing an `ignore:` key would disqualify the repository from the HACS default
  store. See [BRANDING.md](docs/tech/BRANDING.md).
- **Only two lamps have ever been confirmed on hardware** — the MOOON! H134 on this build, and the Moon2AD2 by
  the contributor; the Hoopik only by upstream, on an older build. Every other MOOON! size is inferred from the
  same `module_type`. See [DEVICES.md](docs/domain/DEVICES.md#confidence).
- **"Confirmed on hardware" means lamp firmware `3.0.27.0`** — the reference build, and the newest the vendor
  publishes for the H134. The one exception is the `MODULE_INFO_GET` fixture in `tests/test_protocol.py`, whose
  `0xb5` predates the update. See [DEVICES.md](docs/domain/DEVICES.md#the-reference-firmware-is-30270).

## Development Workflow

- **Never commit to `main`** — it is protected by a ruleset (linear history, PR required, `gate` must pass, squash-only, no force-push or deletion). Branch, PR, squash-merge.
- **Update `CHANGELOG.md`** in the same PR when behaviour changes, under the version `manifest.json` will carry — `release.yml` reads that section as the release notes.
- **Update `docs/`** in the same PR as the code change that affects it. A wrong doc is worse than a missing one.
- **Run `ruff check . && ruff format . --check && pytest tests/ -q` before pushing** — the same three checks `gate` enforces (CI runs pytest with `-v`; the flag is the only difference).
- **Bump `manifest.json` + `CHANGELOG.md` to release**; documentation-only and CI-only changes need neither. Full cycle and versioning policy: [CONTRIBUTING.md](CONTRIBUTING.md).

## Detailed Guides

- [Technical Context](docs/tech/README.md) -- architecture, tech stack, conventions, testing, infrastructure, upstream
- [Domain Context](docs/domain/README.md) -- lamps, entities, state model, Linkio protocol, pairing, dead ends
