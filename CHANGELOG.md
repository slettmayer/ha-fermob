# Changelog

## 0.4.0

Prepares the repository for submission to the
[HACS default store](https://github.com/hacs/default). No functional change to
lamp control — the protocol, entity and pairing behaviour are untouched.

- **Added a brand icon** at `custom_components/fermob/brand/`, so the
  integration shows a proper icon instead of a placeholder. Home Assistant
  2026.3+ reads brand images from the integration directory; on older cores the
  icon is ignored and nothing else changes.
- **The HACS validation job no longer ignores the `brands` check.** It now
  passes on its own merit, which is a precondition for default-store inclusion.
- The icon is an original work under this repository's MIT licence — it is
  deliberately not Fermob's logo, which is not ours to relicense. Reasoning and
  the regeneration script are documented in `docs/tech/BRANDING.md`.

## 0.3.1

- Bump dependency (Dependabot)

## 0.3.0

First release of this fork. Carries upstream
[PR #2](https://github.com/edouardrosset/ha-fermob/pull/2) by
[@fjcompiled](https://github.com/fjcompiled) — MOOON! tunable-white support —
plus a hardening pass.

- **MOOON! support.** Tunable-white lamps (every MOOON! / table lamp) send a
  `DEVICE_DATA_SET` body with separate cold/warm channels instead of the
  Hoopik's single level byte, and expose a colour-temperature slider over
  3000–6000 K. Dimmable-white (Hoopik) frames are byte-identical to 0.1.0.
- **Lamp type override** under Configure → Lamp type, because the encrypted
  Linkio advertisement cannot reveal the model.
- **Fixed a dependency that could fail on a stock install.** The integration
  imported pycryptodome without declaring it; Home Assistant does not ship it.
  Now uses `cryptography`, which core always ships.
- **The BLE connection is released when the entry unloads or reloads.**
  Previously an unload left the link open, and these lamps accept one client at
  a time.
- **The light now reports itself unavailable** when a command fails, instead of
  showing its last known state as if it were still true.
- Removed a hardcoded fallback MAC address, a lamp-type detection branch that
  could never fire, and error handlers that swallowed failures silently.
- Corrected README claims that state is re-synced on reconnect — it is not.
- Added 794 unit tests over the protocol layer, and CI (ruff, pytest, hassfest,
  HACS validation).

## 0.2.0

Version proposed in upstream PR #2; never released.

## 0.1.0

Initial upstream release by [@edouardrosset](https://github.com/edouardrosset) —
Hoopik GL1200 (dimmable white) support.
