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

## Test dependencies

`requirements_test.txt` pins `pytest`, `cryptography` and `ruff` — deliberately **not**
`pytest-homeassistant-custom-component`, which the sibling `ha-geosphere-next` repo uses. `protocol.py` imports
nothing from Home Assistant, so no current test needs a `hass` instance, and installing all of HA to run
pure-function tests would be waste.

Swap it in the moment a test needs real HA machinery (an entity, a config flow, a coordinator). That is a
normal evolution, not a violation.

**When you do, add `asyncio_mode = "auto"` back to `[tool.pytest.ini_options]` in `pyproject.toml`.**
`ha-geosphere-next` sets it; we omit it *because* of the lean dependencies — without `pytest-asyncio` (which
arrives with `pytest-homeassistant-custom-component`) that key is an unknown ini option and pytest warns on
every run. The two settings move together.
