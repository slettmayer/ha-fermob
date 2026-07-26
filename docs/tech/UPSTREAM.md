# Relationship to Upstream

> What this fork is, what it changed, and what would have to change before any of it could go back.

## The lineage

| | |
|---|---|
| Upstream | [edouardrosset/ha-fermob](https://github.com/edouardrosset/ha-fermob) — created 2026-05-18, two commits, last touched 2026-05-18 |
| The MOOON! fix | [upstream PR #2](https://github.com/edouardrosset/ha-fermob/pull/2) by [@fjcompiled](https://github.com/fjcompiled), opened 2026-07-11 against upstream issue #1 |
| This fork | Created 2026-07-26, carrying PR #2 plus a hardening pass |

We forked rather than waited because upstream has been silent since 2026-05-18 and had not responded to
either the issue or the PR. There is also no shortcut through the contributor's own fork: their `main` is
byte-identical to upstream, with the fix living only on a feature branch, and HACS custom repositories install
from a release or the default branch — never an arbitrary branch.

At the time of forking, upstream was the **only** Fermob integration on GitHub. There is no better-maintained
alternative to fall back to.

## What we took verbatim

PR #2's three commits were cherry-picked unchanged, with `@fjcompiled` preserved as co-author. That is the
tunable-white protocol work: the lamp-family split, the cold/warm payload, `ColorMode.COLOR_TEMP`, the options
flow, and the translations. It is sound, and it was tested by its author on a real MOOON! Moon2AD2.

## What we changed, and why

Upstream's protocol reverse-engineering is good work. Its Home Assistant plumbing was not, and PR #2 inherited
all of it.

| Fix | Why it mattered |
|---|---|
| `cryptography` instead of pycryptodome | The import could fail outright on a stock HA install — see [TECH-STACK.md](TECH-STACK.md#the-aes-dependency) |
| Release the BLE link on unload | PR #2's options-reload listener made a pre-existing leak reachable, and these lamps accept one client at a time |
| Module-level imports | In-coroutine imports trip core's blocking-call detection |
| Report unavailability; `should_poll = False` | The entity asserted a stale state as current, and HA was polling an entity with no update method |
| Removed the `ensure_connected()` return value | Every caller discarded a lamp state that could never have been useful |
| Removed the hardcoded fallback MAC | Upstream's own lamp address was the default in three call sites |
| Removed the `module_type` detection branch | It could never fire — nothing writes `entry.data["module_type"]`, and the advertisement cannot reveal the model |
| Logged the swallowed exceptions | `except Exception: pass` around all frame decoding |
| Corrected the README | It claimed three times that state re-syncs on reconnect, which the code contradicts |
| Extracted `protocol.py`; added 794 tests + CI | PR #2 claimed byte-for-byte verification but shipped nothing that re-checks it |
| Packaging | Unsorted manifest keys, an invalid `hacs.json` `description` key that failed the whole schema, a missing `issue_tracker` — all found by CI the moment it existed |

## Confidence in the protocol claims

**We have verified none of it against the official Fermob app.** PR #2 states its frames were checked
byte-for-byte against the app's `buildPayload`, but shipped no test vectors, so that check is not reproducible
from what we have.

One inconsistency is worth recording: upstream issue #1 reports a `GATT Protocol Error: Unlikely Error` on the
write, while PR #2 explains the same failure as the lamp silently dropping a malformed no-ACK frame. Those are
different failure modes. Both accounts come from the same author and neither is reproducible here, so treat the
precise failure mode as unsettled — the fix itself is confirmed working by its author.

Our tests pin *our* layout and intent. The single exception is
`test_dw_payload_matches_upstream_literal`, which independently re-expresses the dimmable-white body and so
genuinely enforces the "Hoopik unchanged" guarantee.

## If contributing back

Should upstream become active, the hardening is worth offering. Two things must change first:

1. **Flip the lamp-family default.** Our heuristic sends everything not named `hoop*` down the tunable-white path. That is right for a MOOON!-only fork, but upstream's users all have Hoopiks, and a renamed Hoopik would silently break for them. Default to dimmable-white-when-unknown there. This is most likely why a cautious maintainer left PR #2 alone.
2. **Split the diff.** Offer the correctness fixes (the AES dependency, the BLE unload leak, availability) separately from the restructuring (`protocol.py`, ruff formatting). A maintainer can accept the first without adopting our tooling.

The `ruff format` pass in particular is a fork-local choice: it drops upstream's column-aligned assignment
style, so any patch crossing that boundary will conflict on whitespace.

## Divergence to watch

Our `manifest.json` points `documentation` and `issue_tracker` at this fork, sets `codeowners` to
`@slettmayer`, and versions independently (upstream: 0.1.0; PR #2 proposed 0.2.0; this fork started at 0.3.0).
If you ever rebase onto a revived upstream, those four fields are the expected conflicts.
