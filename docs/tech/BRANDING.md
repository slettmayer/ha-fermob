# Branding

> Where the integration's icon lives, why it looks the way it does, and why it is **not** Fermob's logo.

## Where the asset lives

| | |
|---|---|
| Location | `custom_components/fermob/brand/` |
| Files | `icon.png` (256×256), `icon@2x.png` (512×512) |
| Generator | `scripts/generate_brand_icon.py` (needs `Pillow`) |
| Format | PNG, RGBA, transparency outside the rounded square |

Home Assistant reads brand images from that directory directly, and local images take priority over the
brands CDN. This is the mechanism introduced in **HA 2026.3**; on older cores the directory is simply
ignored and the lamp falls back to a generic icon. The integration itself still supports 2024.4, so the
minimum in `hacs.json` is unchanged — only the icon is version-gated, not the functionality.

To regenerate after editing the script:

```bash
pip install Pillow
python scripts/generate_brand_icon.py
```

## Why the asset is in this repo and not in home-assistant/brands

[home-assistant/brands](https://github.com/home-assistant/brands) still has a `custom_integrations/`
directory, but it is **legacy and discouraged**: its README now points custom-component authors at the
in-repo `brand/` directory instead. Shipping the icon here is the current, recommended path, and it needs no
second pull request against another repository.

It also satisfies the HACS `brands` check. That check looks for `<content path>/brand/icon.png` in the
repository tree and only falls back to querying the brands CDN when the file is absent — so with the asset
present, the check passes without any `ignore:` entry. That matters because HACS default-store submission
requires the action to pass with **no** ignored checks. See
[INFRASTRUCTURE.md](INFRASTRUCTURE.md#hacs-default-store).

## Why it is not Fermob's logo

**This repository is MIT-licensed. Fermob's logo, wordmark and product designs are not ours to relicense.**
Dropping their mark into an MIT repository would assert a licence over it that we have no standing to grant,
and this project is explicitly [not affiliated with Fermob](../../README.md). The brands CDN sidesteps this
with an "identification purposes only" notice covering its own centrally-hosted index; that notice does not
travel with a file we vendor into our own tree under our own licence.

So the icon is an **original work authored in this repository**, and it deliberately avoids:

- Fermob's logo or wordmark, in any recognisable or stylised form.
- The MOOON! and Hoopik product silhouettes, which are distinctive industrial designs.
- Any typography that could read as a Fermob brand asset.

Anyone is free to reuse it under the repository's MIT licence, which is only true because we own it.

## What the design means

The icon is functional rather than brand-derived — it depicts what the integration *does*, not who makes the
hardware:

- **A disc carrying a horizontal warm-to-cold gradient.** This is the domain concept: these lamps express
  colour temperature as two intensity channels whose sum is the total output, warm at the 3000 K end and cold
  at 6000 K. Left-to-right in the icon matches low-to-high Kelvin. See
  [LINKIO-PROTOCOL.md](../domain/LINKIO-PROTOCOL.md#tunable-white-mixing).
- **Eight radiating rays**, so the disc reads as something emitting light rather than an abstract sphere.
- **A dark slate rounded square**, chosen to hold up against both light and dark Home Assistant themes.

If you change the design, keep it original and keep the two-channel warm/cold idea — that is the one thing
about these lamps a user needs to recognise, and it is the part no other integration's icon expresses.
