# Conventions

## Ruff

Config lives in `pyproject.toml` and is shared with `ha-geosphere-next`:

- `target-version = "py314"`, `line-length = 88`
- Rules: `E, W, F, I, UP, B, SIM, C4, RUF`
- Ignored: `E501` (long lines in URLs and translation strings; the formatter handles the rest)
- isort: `known-first-party = ["custom_components.fermob"]`, `combine-as-imports = true`

`ruff check . && ruff format . --check` is what CI enforces. Run `ruff check . --fix && ruff format .` before
pushing.

Consequences worth knowing:

- **The formatter does not preserve column-aligned assignments.** Upstream's style (`self.hass     = hass`) is gone and cannot be kept; don't reintroduce it.
- **`# noqa` for a rule that isn't selected is an error** (`RUF100`). `BLE001` is not in our rule set, so `# noqa: BLE001` gets flagged as unused. Where a broad `except Exception` needs justifying, write a plain comment on the `except` line instead.
- **`zip()` needs an explicit `strict=`** (`B905`). In `crypt()` this is `strict=True` on purpose: both sides are always 16 bytes, and silent truncation would produce a mis-parsed payload instead of a visible failure.
- **`ruff format` also formats Python code blocks inside Markdown.** A ```` ```python ```` snippet in `docs/` that is not format-clean fails the `Ruff` job just like real source would. Run `ruff format .` after editing docs that contain Python, not only after editing code.

## Imports

**Import at module level. Never inside a coroutine.** Home Assistant imports integration modules in an
executor thread, which is what makes module-scope imports safe; an import executed inside the event loop trips
core's blocking-call detection. This applies to `bleak`, `bleak_retry_connector`,
`homeassistant.components.bluetooth`, `DeviceInfo` — all of which upstream imported lazily inside functions.

The one exception is the test module, which loads `protocol.py` by file path deliberately (see
[TESTING.md](TESTING.md)).

## Protocol code

- **No `homeassistant` imports in `protocol.py`.** See [ARCHITECTURE.md](ARCHITECTURE.md#why-protocolpy-has-no-ha-imports).
- **Every protocol constant lives in `protocol.py`** — commands, encryption modes, message types, TLV parameter IDs, the light families, `FADE`, the Kelvin envelope. Never inline a literal like `0x41` or `146` at a call site.
- **Known exception: `config_flow.py` redeclares the lamp-family strings.** It defines its own
  `LIGHT_TYPE_AUTO`/`LIGHT_TYPE_DW`/`LIGHT_TYPE_TW` rather than importing `LIGHT_TYPE_DW`/`LIGHT_TYPE_TW` from
  `protocol.py`, so the two copies must be kept in sync by hand — the comment above them says so. `light.py` imports
  them properly. If you change a family string, change it in **both** files; better, delete the duplicates and import.
- `config_flow.py` also owns `FERMOB_ADV_UUID`, the discovery-side advertisement UUID. That one is genuinely a
  config-flow concern (it is also declared in `manifest.json`) and is not the same value as the GATT service UUID — see
  [LINKIO-PROTOCOL.md](../domain/LINKIO-PROTOCOL.md#transport).
- **Functions there are pure**: bytes in, bytes out, no I/O, no logging, no clock.
- Public names (no leading underscore) since they cross a module boundary.

## Entity and connection code

- **One light-command path.** `FermobLight._async_send_led()` owns connect → send → failure handling → availability. Do
  not write a second copy of that block; `async_turn_on`/`async_turn_off` only compute parameters and record attributes
  after it succeeds.
- **`async_unpair()` is the one sanctioned exception.** It runs its own lock → `ensure_connected()` → `unpair()` →
  `disconnect()` block and reaches into the connection's private key state, because it is removing the config entry:
  there is no availability to maintain and no entity left to update afterwards. It is not a precedent — any *new*
  command belongs in `_async_send_led()`.
- **Update attributes only after a confirmed send**, so a failure never leaves HA asserting a state the lamp does not have.
- **Branch on `LIGHT_TYPE_DW` / `LIGHT_TYPE_TW`**, never on model names or `module_type`.
- **Hold the connection lock for the whole connect-and-send.** `ensure_connected()` and `send_led()` assume the caller holds it.
- **Log lamp identity as the MAC** (`self._entry.data[CONF_ADDRESS]` in the entity, `self._address` in the connection) so log lines are greppable per device.

## Naming

- Classes are `Fermob*` (`FermobLight`, `FermobBLEConnection`, `FermobConfigFlow`, `FermobOptionsFlow`).
- Private helpers are `_`-prefixed; `protocol.py` is the exception.
- Frame builders are `build_*`, parsers are `parse_*`, conversions are `<from>_to_<to>`.

## Commits and PRs

- **Conventional Commits** with a scope: `fix(ble):`, `chore(ci):`, `refactor:`, `test:`, `docs:`, `style:`.
- **Branch, PR, squash-merge.** `main` is protected — see [INFRASTRUCTURE.md](INFRASTRUCTURE.md#branch-ruleset).
- **Explain *why* in the commit body**, especially when removing something. A commit that says what the diff already shows is a wasted commit message.
- **`CHANGELOG.md` in the same PR** as any behaviour change, under the version `manifest.json` will carry.
- **`docs/` in the same PR** as the code change that affects it.
- **State confidence honestly.** This codebase rests on reverse engineering; a claim of "verified" that nobody can reproduce is worse than "inferred".
