# Firmware Update

> How the official app updates lamp firmware, what that would cost us, and why **this integration reports
> firmware updates but does not install them.** Written up so the analysis is not lost, and so the next session
> does not have to redo it.

**Status: reporting only, as of 0.10.0.** We read both versions the lamp reports and ask the vendor server
whether a newer build exists — `firmware.py` and the `update` platform, described in
[ENTITIES-AND-SERVICES.md](ENTITIES-AND-SERVICES.md#the-firmware-entity). **Nothing sends a DFU command, and
nothing downloads an image.** Everything below the server section is a design record, not shipped behaviour.

**Confidence.** The mechanism is *derived from the app* — `assets/www/build/main.js` plus jadx on
`classes.dex` (`net.linkio.plugin.DFUImpl`, `net.linkio.update.DFUUpdate`), Fermob Lighting 3.0.2, versionCode
1209, analysed 2026-08-07. The server behaviour and the payload shapes below are *verified live* against the
vendor server on 2026-08-07. **Nothing here has ever been executed against a lamp**, by us or by upstream.

## The lamp is an nRF52 running signed Nordic Secure DFU

*Verified from the downloaded image, 2026-08-07.* The release zip for the H134 contains exactly what Nordic's
tooling produces:

```
manifest.json                              189 B   {"manifest":{"application":{bin_file, dat_file}}}
NRF52_Fermob_MOOONH134_v3.0.27.0.bin    76 504 B   the application image
NRF52_Fermob_MOOONH134_v3.0.27.0.dat       143 B   init packet
```

The `.dat` is a protobuf init packet carrying a SHA-256 hash and a **64-byte ECDSA P-256 signature** — that is
**Secure DFU** (nRF5 SDK ≥ 12), not the legacy protocol. The project name (`NRF52_Fermob_MOOONH134`) confirms
the SoC family.

Two consequences, and they pull in opposite directions:

- **We would not need the signing key.** The `.dat` is forwarded to the bootloader verbatim; signing already
  happened at Fermob's build. Only the vendor can produce an image, which is also why we cannot build a test
  image of our own.
- **The bootloader refuses anything it cannot verify**, so a corrupt or foreign image cannot be written. The
  realistic failure is an *interrupted* transfer, not a bricked lamp — see [Risks](#risks).

## The vendor update server

Base URL `https://dfu1.smartandgreen.eu:443/api/dfu/v1`, with `dfu2.smartandgreen.eu` as a fallback carrying
the same content. Both are hardcoded app constants (`dfuServerUrl` / `dfuServerUrlFallback`). Note the domain:
this is **Linkio's server, not Fermob's** — the same protocol vendor whose name is all over the wire format.

Three calls, in this order:

| # | Call | Auth | Returns |
|---|---|---|---|
| 1 | `GET /release/{manufacturer}/{model}/latest` | none | `data.release` — `version`, `file_id`, `project`, `date`, `manufacturer`, `model` |
| 2 | `POST /token` with `{"date":"YYYY-MM-DDTHH:MM:SS"}` | none | `data.token` — a 40-hex-char bearer token |
| 3 | `GET /files/{file_id}` | `Authorization: Bearer <token>` | the DFU zip |

**Those three plus `POST /infos` are the entire public API — there is no release history.** The server is a
Django app whose 404 page lists its URL patterns; everything else it serves (`release/<id>`, `release/add`,
`manufacturers`, `models`) is behind `accounts/login`. So **you cannot ask it about any build but the newest**:
no list, no by-version lookup, and therefore **no publication date for a version a lamp is currently running**.
The `date` in call 1 is the publication date of the *current* release only.

`manufacturer` and `model` are the lamp's own strings — TLV `0xb2` and `0xb3` from `MODULE_INFO_GET`, which we
already parse ([PROTOCOL-COMMANDS.md](PROTOCOL-COMMANDS.md)) — with `-` and whitespace stripped:
`MOOON - H134` → `MOOONH134`. An unknown model is a `400` with `"This model doesn't exists"`, and the token is
required only for the download.

*Verified live 2026-08-07*, one query per model string we could guess:

| Model | Latest | Published |
|---|---|---|
| `MOOONH134` | 3.0.27.0 | 2023-11-07 (image itself built 2023-09-21, per the timestamps inside the zip) |
| `MOOONH63` | 3.0.27.0 | 2023-11-07 |
| `INOUI` | 3.0.27.0 | 2023-11-07 |
| `MOOON3D15`, `MOOOND25` | 3.0.24.0 | 2023-01-10 |
| `MOOOND15` | 3.0.24 | 2022-12-19 |
| `MOOON1D15`, `LUDO`, `APLOPREMIUM`, `MOLIARE` | *no such model* | — |
| Hoopik — `HOOPIKL1200`, `HOOPIKGL1200`, `HOOPIK1200`, `GL1200`, `HOOPIK`, `HOOPIKH24` | *no such model* | — |

Two things to take from that table. **Firmware moves rarely**: the newest build on the server is from November
2023. And **the Hoopik is not on the server under any name we tried** — its real `0xb3` string is unknown to us
(no Hoopik has ever been on hardware here, see [DEVICES.md](DEVICES.md#confidence)), so this is not evidence
that it has no updates.

## The version a lamp reports, and why nobody can see what the app installed

The H134 reported `0xb5 = 00 02 03 15` (*verified on hardware*, most recent capture 2026-08-03). The app reads
that TLV **reordered** as `[v3, v4, v5, v2]`, giving **2.3.21.0** — against **3.0.27.0** on the server, so its
own comparison (first three components) would offer the update.

**The reorder is not a guess about the app's intent — it is the same parse its comparison consumes.**
`parseModuleInfoData` walks the TLVs with `o` on the length byte, so `o+2` is the first value byte:
`m_type=(e[o+3]<<8)+e[o+2]` (little-endian, as we read it), `m_firmware_version=[e[o+3],e[o+4],e[o+5],e[o+2]]`,
`m_hardware_version=[e[o+2],e[o+3],e[o+4]]`. Still *derived from the app*, but from the code path that decides
whether a user is offered an update, not from a display string.

**The app never shows a version number while updating**, which is worth knowing before reading anything into
what a user saw. Its whole DFU vocabulary is version-free: *"Looking for update…"*, *"Firmware update
found…"*, *"The firmware of your lamp is updated."*, *"Update in progress, please wait…"*. The installed
version appears only on a separate technical-information screen (`PRODUCT_TECHNICAL_INFO_FIRMWARE_LABEL`,
"Software version"), which needs the app to own the lamp. So **"the app did not offer me version X" is not
evidence about X** — the app names no versions at all. The discriminator is which of two messages appeared:
*"Firmware update found…"* means it flashed something; *"The firmware of your lamp is updated."* means it
thought the lamp was already current.

**Our reference lamp was updated via the app on 2026-08-06 and what it now runs is unconfirmed.** The HA log
for that evening shows the fingerprint of another controller taking ownership — `CRYPT_MSG`, *"lamp no longer
holds our keys"*, then an automatic re-pair — and the last `MODULE_INFO_GET` dump predates it (2026-08-03,
still `00 02 03 15`). It is presumably on 3.0.27.0 now; the first connect under 0.10.0 will say. **Treat
2.3.21.0 as the build every hardware-verified claim in these docs was established on, not as what the lamp is
running today.** The lamp does still work after that update, which is the only evidence we have about 3.x on
the wire.

## How the app installs one

1. **Reboot the lamp into its bootloader** with `LMP_COMMAND_RESET` — command id **0**, sent as `MSG_CMD`
   (`CMD_WITH_ACK`) with payload `[2, 0, 1]` on the normal Linkio characteristic, private-encrypted like any
   other command. Being ACKed makes it one of the few commands whose delivery we could actually confirm; see
   [STATE-MODEL.md](STATE-MODEL.md#it-is-also-the-liveness-probe) for why that is unusual here.
2. **Hand the same BLE address to Nordic's DFU library.** `DfuServiceInitiator(address).setZip(path).start()`,
   every option left at its default. All transfer logic lives in `no.nordicsemi.android.dfu`, bundled in the
   APK — the app itself implements none of it, so the APK tells us nothing about the transfer that the public
   Nordic Secure DFU specification does not.

The app also carries a **second, unused-looking path**: `setModuleInDFUMode()` writes payload `[1, 1]` with no
ACK to service *and* characteristic `8E400001-F315-4F60-9FB8-838830DAEA50` — a Linkio-custom UUID sharing
Nordic's Secure DFU base. Why both exist is unexplained; the `LMP_COMMAND_RESET` path is the one the update
flow calls.

## What is built, and what installing would still cost

| Part | State |
|---|---|
| Firmware and hardware version in the device registry (`0xb5`, `0xb6`) | **Done in 0.10.0** — no extra Bluetooth traffic; they ride the `MODULE_INFO_GET` reply we already make |
| Release-server client and an `update` entity reporting availability **without** `UpdateEntityFeature.INSTALL` | **Done in 0.10.0** — `firmware.py` plus `update.py`, behind the *Check for firmware updates* option |
| `LMP_COMMAND_RESET` frame, plus reconnecting to the bootloader | Not built. Small — existing frame machinery; `CMD_RESET = 0` is not yet a constant |
| A Nordic Secure DFU client over `bleak` — object create/select, CRC32 verification, packet-receipt notifications, chunked writes, execute, resume | Not built. ~400–600 lines plus tests |

The split is deliberate: the first two rows carry no risk to the lamp; the last row is the one that writes
flash, and the risks below are why it is deferred rather than merely unbuilt.

**Three things the read-only half already settles, for whoever builds the rest.** The version the lamp reports
and the version the server publishes are directly comparable (`protocol.compare_versions`, over three
components — the server serves `3.0.24` for one model where a lamp reports `3.0.24.0`). The path segments are
the lamp's own `0xb2`/`0xb3` strings with `-` and whitespace stripped (`firmware.slugify_name`). And a `200`
carrying `code: 400` is an answer about a real lamp, not a transport failure — the fallback host holds the same
catalogue, so retrying it there buys nothing.

## Risks

Why the install half is deferred rather than merely unbuilt:

- **It is one-way.** The server offers only `latest`; there is no downgrade, and no way to get 2.3.21.0 back.
- **It would spend the only test target we have.** Exactly one lamp here has a pending update. Updating it —
  by any route, app included — leaves nothing to test a DFU implementation against.
- **The protocol basis is the lamp being updated.** Whatever changed between 2.x and 3.x, we would discover it
  on the reference hardware for every other claim in `docs/domain/`.
- **Recovery is retry-only.** An interrupted transfer leaves the lamp in its bootloader, advertising and
  retryable — but the 10-second button reset does not reach a bootloader, and the vendor app's own DFU flow
  starts from a *paired* lamp in application mode. The retry path would be ours alone.
- **Whether pairing survives is unknown.** If the update clears the lamp's registration, recovery needs
  physical access — and our keys stay on disk either way, which is the situation
  [PAIRING.md](PAIRING.md#when-the-lamp-says-nothing-at-all-is-this-still-our-lamp) exists to handle.
- **Bluetooth proxies are unlikely to carry it.** 76 KB of write-without-response at DFU throughput is a very
  different workload from 20-byte frames, and a proxy is the documented setup for HA hosts without an adapter.
- **It introduces a cloud dependency** into an integration whose stated shape is local BLE with no hub and no
  cloud. The read-only half already does, which is why the availability check is an option a user can refuse —
  and why it asks for release metadata only, never an image.

## There is no off-the-shelf Python client to reuse

*Verified 2026-08-07.* PyPI carries `nrfutil` and `pc-ble-driver-py`; both drive BLE through Nordic's own
connectivity firmware on an nRF5 dongle over serial, so neither can use HA's Bluetooth stack, a plain HCI
adapter or a proxy. Nothing packaged targets `bleak`.

The closest prior art is [`bringert/nrf5-ota-dfu-python`](https://github.com/bringert/nrf5-ota-dfu-python) —
Apache-2.0, `bleak`-based, with a secure-DFU controller alongside a legacy one. It is a small script
repository, not a library, and not published anywhere installable.

So the realistic options are **write it** or **vendor it with attribution**, decided when the work is actually
scheduled. Note the licence angle before vendoring: Apache-2.0 into an MIT repository is permitted but adds a
notice obligation this repository does not currently carry — see [BRANDING.md](../tech/BRANDING.md) for the
existing licence stance.

## Until then

Users who want a firmware update install it with the Fermob app, which means unpairing from Home Assistant
first — the app cannot reach a lamp Home Assistant owns
([PAIRING.md](PAIRING.md#the-ownership-model)). Doing it **before** pairing to Home Assistant avoids that
round trip entirely, which is what the README recommends and what the entity's `release_summary` says.
