# Fermob (ha-fermob)

> Home Assistant custom integration for Fermob Bluetooth lamps — MOOON! (tunable white) and Hoopik GL1200 (dimmable white) — over local BLE, no hub and no cloud.

> **Editing this guide:** `AGENTS.md` is the single source of truth for project context, read by all AI
> coding agents and humans. Keep it concise — put detail in `docs/` and link it. When you change code that
> alters documented behaviour, update the matching `docs/` file in the **same PR** — see
> [docs/README.md](docs/README.md) for the doc-structure contract.

## Quick Reference

- **Build**: none — pure Python custom component distributed via HACS
- **Run**: load into Home Assistant (HACS custom repository, or copy `custom_components/fermob/`)
- **Test**: `pip install -r requirements_test.txt && python -m pytest tests/ -q` (794 tests, no Home Assistant needed)
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
| Understand the lamps and what we control | [docs/domain/OVERVIEW.md](docs/domain/OVERVIEW.md) |
| Work on frames, crypto or payloads | [LINKIO-PROTOCOL.md](docs/domain/LINKIO-PROTOCOL.md) |
| Debug pairing, ownership or resets | [PAIRING.md](docs/domain/PAIRING.md) |

## Architecture Overview

Four modules in `custom_components/fermob/`. `protocol.py` is a **pure** layer — frame building, AES-ECB
keystream crypto, payload construction, inbound parsing — with **no `homeassistant` imports**, so it is unit
testable on its own. `light.py` holds `FermobBLEConnection` (BLE link, pairing handshake, key persistence,
frame send/ACK matching, idle disconnect) and `FermobLight` (the HA entity). `config_flow.py` handles
Bluetooth discovery, manual add, and the lamp-type options flow. `__init__.py` forwards the platform and
reloads the entry when options change. There is no coordinator and no polling: state is pushed by our own
commands and by EVENT notifications while the link is up. See
[ARCHITECTURE.md](docs/tech/ARCHITECTURE.md).

## Tech Stack

- Python 3.14; Home Assistant Core (min `2024.4.0`).
- `cryptography` for the AES-ECB keystream — **shipped by HA core, deliberately absent from `requirements`** so we never fight core's pin. Never reintroduce pycryptodome; core does not ship it.
- `bleak` + `bleak_retry_connector` via HA's Bluetooth stack; `dependencies: ["bluetooth_adapters"]`.
- Ruff (lint + format); pytest. No runtime PyPI requirements; HACS-distributed. See [TECH-STACK.md](docs/tech/TECH-STACK.md).

## Core Conventions

- Keep `protocol.py` free of `homeassistant` imports — that is what makes the frame layer testable without a `hass` instance.
- All protocol constants (commands, encryption modes, message types, light families, Kelvin envelope) live in `protocol.py` — never inline literals.
- Import at **module level**, never inside a coroutine: HA imports integration modules in an executor, and an in-loop import trips core's blocking-call detection.
- Every **light** command goes through `FermobLight._async_send_led()`, which owns the connect/send/failure/availability
  path — do not add a second copy. `async_unpair()` is the one deliberate exception: it tears the entry down, so it has
  no availability state to maintain.
- Lamp families are the strings `LIGHT_TYPE_DW` / `LIGHT_TYPE_TW`; anything family-dependent branches on those, not on model names.
- Ruff-enforced: 4 spaces, double quotes, line length 88, rule set `E,W,F,I,UP,B,SIM,C4,RUF`. See [CONVENTIONS.md](docs/tech/CONVENTIONS.md).

## Business Domain

Fermob's lamps speak the Linkio BLE protocol: an encrypted, rotating advertisement, a one-time pairing
handshake that exchanges keys and puts the lamp in "gateway" state, then AES-ECB-obscured 20-byte frames on
a single characteristic. Two LED families share everything except the light command body — dimmable white
(Hoopik) carries one `level` byte, tunable white (every MOOON!) carries separate `cold_white`/`warm_white`
channels whose sum is the total output, which is how colour temperature is expressed. See
[docs/domain/OVERVIEW.md](docs/domain/OVERVIEW.md).

## Structural Risks

- **Nothing is verified against the official Fermob app.** The protocol was reverse-engineered from its JS by others; our tests pin *our* layout and intent only. See [UPSTREAM.md](docs/tech/UPSTREAM.md).
- **Lamp-family detection is a name heuristic** (`"hoop"` in the name → dimmable white, everything else → tunable white). It is wrong for a renamed Hoopik; the options flow is the escape hatch. The model cannot be read from the advertisement — it is rotating and encrypted.
- **State drifts silently after the 30 s idle disconnect.** The lamp emits no EVENT on reconnect and stops answering `DEVICE_DATA_GET` in gateway mode, so a physical button press outside the connected window is unrecoverable. `FermobBLEConnection.get_state()` exists but is intentionally unused — see its docstring before wiring it in.
- **The lamp limits its own light output on battery** — roughly half off the charger, worse at a low state of charge. Firmware behaviour with **no setting anywhere**: the official app has no lamp-configuration surface at all and never sends the config commands its own enum defines. Do not go looking for a command to send, and do not treat a "capped brightness" report as a mapping bug. See [OVERVIEW.md](docs/domain/OVERVIEW.md#what-the-official-app-can-configure--and-what-it-cannot).
- **One controller at a time.** Pairing makes Home Assistant the owner; the Fermob app cannot connect while HA holds the link, and re-pairing from the app invalidates our stored keys. See [PAIRING.md](docs/domain/PAIRING.md).
- **`config_flow.py` keeps its own copy of the lamp-family strings** (`LIGHT_TYPE_AUTO/DW/TW`) instead of importing them
  from `protocol.py`, so the two must be kept in sync by hand. See
  [CONVENTIONS.md](docs/tech/CONVENTIONS.md#protocol-code).
- `hacs/action@main` and `home-assistant/actions/hassfest@master` are floating CI refs. The HACS action runs with
  **no ignored checks** — reintroducing an `ignore:` key would disqualify the repository from the HACS default
  store. See [BRANDING.md](docs/tech/BRANDING.md).
- Only the MOOON! Moon2AD2 has been confirmed by anyone; other MOOON! sizes are inferred from the same `module_type`.

## Development Workflow

- **Never commit to `main`** — it is protected by a ruleset (linear history, PR required, `gate` must pass, squash-only, no force-push or deletion). Branch, PR, squash-merge.
- **Update `CHANGELOG.md`** in the same PR when behaviour changes, under the version `manifest.json` will carry — `release.yml` reads that section as the release notes.
- **Update `docs/`** in the same PR as the code change that affects it. A wrong doc is worse than a missing one.
- **Run `ruff check . && ruff format . --check && pytest tests/ -q` before pushing** — the same three checks `gate` enforces (CI runs pytest with `-v`; the flag is the only difference).
- **Bump `manifest.json` + `CHANGELOG.md` to release**; documentation-only and CI-only changes need neither. Full cycle and versioning policy: [CONTRIBUTING.md](CONTRIBUTING.md).

## Detailed Guides

- [Technical Context](docs/tech/README.md) -- architecture, tech stack, conventions, testing, infrastructure, upstream
- [Domain Context](docs/domain/README.md) -- lamps, Linkio protocol, pairing
