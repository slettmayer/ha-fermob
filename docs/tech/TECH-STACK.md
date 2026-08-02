# Tech Stack

| Layer | Choice |
|---|---|
| Language | Python 3.14 (`.python-version`) |
| Platform | Home Assistant Core, minimum `2024.4.0` |
| Crypto | `cryptography` (AES-ECB single block) |
| BLE | `bleak` + `bleak_retry_connector`, through HA's Bluetooth stack |
| Lint / format | Ruff |
| Tests | pytest (no Home Assistant needed) |
| Distribution | HACS custom repository |
| Runtime PyPI requirements | **none** |

## The AES dependency

`manifest.json` declares `"requirements": []` and that is now **correct** — but it was not always.

Upstream imported `Crypto.Cipher.AES` (pycryptodome) while declaring no requirements. pycryptodome appears
**nowhere** in Home Assistant core's `requirements_all.txt`; it exists only as a version floor
(`pycryptodome>=3.6.6`) in `package_constraints.txt`, which constrains it *if* something else pulls it in but
never installs it. On an install where nothing else drags it in, every lamp command failed at the first frame.

We use `cryptography` instead, which core pins in `package_constraints.txt` (`cryptography==48.0.1` at the
time of writing) and therefore always ships. It is **deliberately absent from `requirements`** so pip is never
asked to resolve it against core's pin.

The two produce identical output — verified over 2000 random key/nonce pairs before the swap, since AES-ECB
over a single 16-byte block has no padding or mode ambiguity to differ on.

**Never reintroduce pycryptodome.** If you need another primitive, take it from `cryptography`.

## Home Assistant minimum

`2024.4.0`, set by `ConfigFlowResult` (added 2024.4) in `config_flow.py`. Claimed in both `hacs.json` and the
README. Upstream claimed 2024.1 while already using that API.

If you use a newer HA API, raise the minimum in `hacs.json` **and** the README in the same PR.

## Bluetooth

`async_ble_device_from_address(..., connectable=True)` plus `bleak_retry_connector.establish_connection`, which
is what makes ESPHome Bluetooth proxies work transparently — the proxy must be **active** (`bluetooth_proxy:
active: true`), since a passive proxy can see advertisements but cannot open a connection.

`manifest.json` declares `"dependencies": ["bluetooth_adapters"]` so HA guarantees the Bluetooth stack is set
up before our platform loads, and a `bluetooth:` matcher on the advertisement service UUID for passive
discovery.

**Observed GATT table** of a MOOON! H134 (2026-08-02, enumerated over a proxy) — worth knowing because it rules
things out:

| Service | Contents |
|---|---|
| `0x1800` GAP | Preferred Connection Parameters, Central Address Resolution |
| `0x1801` GATT | Service Changed |
| `41c15000-6def-11e5-bcde-0002a5d5c51b` | the `00005002-…` write/notify characteristic we use |
| `8e400001-f315-4f60-9fb8-838830daea50` | Nordic *Experimental Buttonless DFU* |

There is **no Battery Service (`0x180F`) and no Device Information Service (`0x180A`)**. Do not reach for
`0x2A19` for a charge level; it does not exist on this lamp. The DFU service means the lamp is an nRF part with
over-the-air firmware update reachable over BLE — leave it alone.

## Test dependencies

`requirements_test.txt` installs `pytest`, `cryptography`, `pytest-homeassistant-custom-component`, and a short
list of transitive Bluetooth requirements. `protocol.py` still imports nothing from Home Assistant, so
`tests/test_protocol.py` alone would run on `pytest` + `cryptography`; `tests/test_light.py` needs a real
`hass`, which is what pulled the harness in.

**`asyncio_mode = "auto"` in `[tool.pytest.ini_options]` and `pytest-homeassistant-custom-component` move
together.** The harness registers autouse *async* event-loop fixtures, so without that key **every** test in the
suite errors at setup — including the pure ones, which is a confusing way to discover the linkage. Conversely,
dropping the harness means dropping the key, or pytest warns about an unknown ini option on every run.

**The Bluetooth transitive pins (`aiousbwatcher`, `serialx`, `bleak-esphome`, `bleak-retry-connector`,
`habluetooth`) are a test-only artefact.** `homeassistant.components.bluetooth` imports
`homeassistant.components.usb`, whose own requirements `pip install homeassistant` does not resolve — a real HA
install always has them. Without these, `tests/test_light.py` dies at import with a bare
`ModuleNotFoundError: No module named 'aiousbwatcher'` that gives no hint it is about Bluetooth. They are
**not** runtime dependencies: `manifest.json` still declares `"requirements": []`.

## Lint dependencies

`requirements_lint.txt` pins `ruff` with `==`, in its own file so the Ruff job does not install the test
suite to run a linter. The `==` is the point: with a `>=` floor pip resolves whatever is newest, so the pin
describes the version rather than choosing it — which is how CI ran ruff 0.16.0, a release that expanded the
default rule set from 59 rules to 413, for a week before anyone chose it. Dependabot's `pip` ecosystem bumps
the pin, and the bump PR is where a breaking release now surfaces.

**When you do, add `asyncio_mode = "auto"` back to `[tool.pytest.ini_options]` in `pyproject.toml`.**
`ha-geosphere-next` sets it; we omit it *because* of the lean dependencies — without `pytest-asyncio` (which
arrives with `pytest-homeassistant-custom-component`) that key is an unknown ini option and pytest warns on
every run. The two settings move together.
