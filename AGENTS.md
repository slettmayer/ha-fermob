# Fermob (ha-fermob)

> Home Assistant custom integration for Fermob Bluetooth lamps — MOOON! (tunable white) and Hoopik GL1200
> (dimmable white) — over local BLE, no hub and no cloud. One exception, opt-out: a daily firmware-version
> check against the vendor's release server.

> **Editing this guide:** `AGENTS.md` is the single source of truth for project context, read by every AI
> coding agent and human. Keep it concise — put detail in `docs/` and link it, and update the matching `docs/`
> file in the **same PR** as the code change. See [docs/README.md](docs/README.md) for the contract.

## Quick Reference

- **Build**: none — pure Python custom component distributed via HACS
- **Run**: load into Home Assistant (HACS custom repository, or copy `custom_components/fermob/`)
- **Test**: `pip install -r requirements_test.txt && python -m pytest tests/ -q` (1122 tests, ~12 s —
  `test_protocol.py` needs no Home Assistant, the other six use its test harness)
- **Lint**: `pip install -r requirements_lint.txt && ruff check . --fix && ruff format .` — autofix; CI enforces the non-mutating form, see Development Workflow
- **Release**: merge to `main` with a bumped `manifest.json` and a matching `CHANGELOG.md` section — `release.yml` tags, archives and publishes it

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
| Answer a firmware-update question | [FIRMWARE-UPDATE.md](docs/domain/FIRMWARE-UPDATE.md) — reporting only, by design |
| Check whether something was already tried | [DEAD-ENDS.md](docs/domain/DEAD-ENDS.md) |

## Architecture Overview

Nine modules in `custom_components/fermob/`, and **two import no Home Assistant at all**: `protocol.py` (frame
building, AES-ECB keystream crypto, payload construction, inbound parsing) and `firmware.py` (the vendor
release-server client, session injected) — which is what makes both unit-testable without a `hass`. `light.py`
holds `FermobBLEConnection` (BLE link, pairing handshake, key persistence, send/ACK matching, idle disconnect)
and `FermobLight`; `entity.py`, `sensor.py`, `binary_sensor.py` and `update.py` add the two diagnostic battery
entities and the firmware entity on that same connection with no BLE logic of their own; `config_flow.py` owns
discovery, manual add and the options flow; `__init__.py` forwards the platforms and reloads on option change.
There is no coordinator and no light polling — state is pushed by our own commands and by EVENT notifications
while the link is up. See [ARCHITECTURE.md](docs/tech/ARCHITECTURE.md).

## Tech Stack

- Python 3.14; Home Assistant Core (min `2024.4.0`). No runtime PyPI requirements; HACS-distributed.
- `cryptography` for the AES-ECB keystream — **shipped by HA core, deliberately absent from `requirements`** so
  we never fight core's pin. Never reintroduce pycryptodome; core does not ship it.
- `bleak` + `bleak_retry_connector` via HA's Bluetooth stack; `dependencies: ["bluetooth_adapters"]`.
- Ruff (lint and format, pinned in `requirements_lint.txt`); pytest. See [TECH-STACK.md](docs/tech/TECH-STACK.md).

## Core Conventions

- Keep `protocol.py` and `firmware.py` free of `homeassistant` imports — that is what makes them testable
  without a `hass` instance.
- All protocol constants (commands, encryption modes, message types, light families, Kelvin envelope) live in
  `protocol.py` — never inline literals. Known exception: `config_flow.py` keeps its own `LIGHT_TYPE_*` copies,
  which have to be kept in sync by hand.
- Import at **module level**, never inside a coroutine: HA imports integration modules in an executor, and an
  in-loop import trips core's blocking-call detection. Two sanctioned exceptions — `__init__.py` importing
  `light` (circular), and the test module's file-path load.
- Every **light** command goes through `FermobLight._async_send_led()`, which owns the
  connect/send/failure/availability path — never write a second copy. `async_unpair()` is the one deliberate
  exception: it tears the entry down, so it has no availability state to maintain.
- Lamp families are the strings `LIGHT_TYPE_DW` / `LIGHT_TYPE_TW`; branch on those, never on model names.
- **Push subscriptions are lists with removal, never assignable callback slots.** Subscribe via
  `conn.add_battery_listener()` / `add_state_listener()` and hand the returned unsubscribe callable to
  `Entity.async_on_remove` — a single slot leaves the second subscriber no way to unchain.
- Ruff-enforced: 4 spaces, double quotes, line length 88, rule set `E,W,F,I,UP,B,SIM,C4,RUF` (`E501` ignored).
  See [CONVENTIONS.md](docs/tech/CONVENTIONS.md).

## Business Domain

Fermob's lamps speak the Linkio BLE protocol: an encrypted, rotating advertisement, a one-time pairing
handshake that exchanges keys and puts the lamp in "gateway" state, then AES-ECB-obscured 20-byte frames on a
single characteristic. The two LED families differ only in the light-command body — dimmable white (Hoopik)
carries one `level` byte, tunable white (every MOOON!) separate `cold_white`/`warm_white` channels whose sum is
the total output, which is how colour temperature is expressed. See [OVERVIEW.md](docs/domain/OVERVIEW.md).

## Structural Risks

- **Reverse-engineered, not vendor-supported.** Everything came from the official app's JS, a decrypted BLE
  capture and hardware tests; our tests pin *our* layout and intent only. Every domain doc marks which of the
  three a claim rests on — keep those markers. [UPSTREAM.md](docs/tech/UPSTREAM.md)
- **Lamp-family detection has three tiers, and only the last is a name heuristic.** Explicit override, then the
  reported `module_type`, then `"hoop"` in the name as a first-run guess — the model is not in the
  advertisement, so tier 2 needs one connection first. [DEVICES.md](docs/domain/DEVICES.md#lamp-family-detection)
- **There is no way to read the lamp's state; holding the BLE link open is the whole mechanism.** Only marker
  **146** may reach an entity (147 is a frozen record), and the check-in is the only thing that reconnects after
  a drop. [STATE-MODEL.md](docs/domain/STATE-MODEL.md), [DEAD-ENDS.md](docs/domain/DEAD-ENDS.md)
- **Almost nothing we send is acknowledged, and the one ACK has three meanings.** `is_connected` stays true on
  a link the lamp has abandoned; the battery ACK is the only signal, and a refusal proves the lamp is listening
  *except* `CRYPT_MSG` (7) and `UNREGISTERED` (5). Branch on `BatteryVerdict`, never on a bare bool.
- **`ensure_connected()` is a two-pass loop, and neither extra step is a redundant round trip.** Pairing
  reconnects afterwards; a battery request unanswered twice probes with a **pairing** frame, so that probe must
  stay behind the failure. [PAIRING.md](docs/domain/PAIRING.md#when-the-lamp-says-nothing-at-all-is-this-still-our-lamp)
- **The connect budget is asymmetric, and `ensure_connected` defaults to the slow one.** Interactive callers
  pass `CONNECT_ATTEMPTS_INTERACTIVE` (2), everything else keeps 4; any pass that pairs and `fermob.unpair`
  override the caller. **Do not quote `attempts x 20 s` as a worst case** — see the comment in `light.py`.
- **Only a user action may pair, and pairing takes ownership.** `async_check_in` passes `allow_pairing=False`;
  a key-presence guard is not enough, since a factory-reset lamp leaves our keys on disk. One controller at a
  time — the vendor app cannot connect while HA holds the link. [PAIRING.md](docs/domain/PAIRING.md)
- **Entry removal deletes the pairing keys, so "delete and re-add" is a one-way door** — the lamp stays
  registered while its key is gone, and the re-add needs a 10-second factory reset first. Deliberate: it is the
  cleanup path for a lamp that is gone. [PAIRING.md](docs/domain/PAIRING.md#unpairing)
- **An entity service cannot be called on an unavailable entity, and the call still reports success.** Core
  filters on `entity.available` before the handler runs. 0.9.1 fixed this at the source by keeping a
  `KEYS_REJECTED` lamp available; the scheduled check-in bypasses the platform entirely.
  [ENTITIES-AND-SERVICES.md](docs/domain/ENTITIES-AND-SERVICES.md#neither-can-be-called-on-an-unavailable-entity-and-that-is-accepted)
- **The idle timeout and the check-in interval are coupled, and must stay derived from one option.** A check-in
  re-arms the idle timer, so a shorter interval holds the link open whatever the timeout says.
  `resolve_connection_profile` sets both from `connection_mode`; never expose them as two numbers.
- **The lamp limits its own light output on battery** — roughly half off the charger, worse at a low state of
  charge. Firmware behaviour with **no setting anywhere**: do not hunt for a command to send, and do not read a
  capped brightness as a mapping bug. [APP-CAPABILITIES.md](docs/domain/APP-CAPABILITIES.md)
- **The firmware entity reports and must never install.** These lamps take a *signed* Nordic Secure DFU image
  only Fermob can produce, so `UpdateEntityFeature.INSTALL` is a button nothing implements — a test pins its
  absence. The daily check is the only non-local traffic here. [FIRMWARE-UPDATE.md](docs/domain/FIRMWARE-UPDATE.md)
- **Only two lamps have ever been confirmed on hardware, and "confirmed" means firmware `3.0.27.0`.** Every
  other MOOON! size is inferred from the same `module_type` — state that confidence honestly rather than
  widening the claim. [DEVICES.md](docs/domain/DEVICES.md#confidence)
- `hacs/action@main` and `home-assistant/actions/hassfest@master` are floating CI refs, and the HACS action runs
  with **no ignored checks** — an `ignore:` key would disqualify the repository from the HACS default store.
  [BRANDING.md](docs/tech/BRANDING.md)

## Development Workflow

- **Never commit to `main`** — a ruleset protects it (linear history, PR required, `gate` must pass,
  squash-only, no force-push or deletion). Branch, PR, squash-merge.
- **Update `CHANGELOG.md`** in the same PR when behaviour changes, under the version `manifest.json` will
  carry — `release.yml` reads that section as the release notes.
- **Update `docs/`** in the same PR as the code change that affects it. A wrong doc is worse than a missing one.
- **Run `ruff check . && ruff format . --check && pytest tests/ -q` before pushing** — the same three checks
  `gate` enforces (CI runs pytest with `-v`; the flag is the only difference).
- **Bump `manifest.json` + `CHANGELOG.md` to release**; documentation-only and CI-only changes need neither.
  Full cycle and versioning policy: [CONTRIBUTING.md](CONTRIBUTING.md).

## Detailed Guides

- [Technical Context](docs/tech/README.md) — architecture, tech stack, conventions, testing, infrastructure, upstream
- [Domain Context](docs/domain/README.md) — lamps, entities, state model, Linkio protocol, pairing, dead ends
