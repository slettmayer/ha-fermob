# Infrastructure

> CI, branch protection, releases, and how the integration reaches Home Assistant. Mirrors `slettmayer/ha-geosphere-next`.

## CI — `.github/workflows/validate.yml`

Runs on push to `main`, every PR, weekly (Mondays 04:00 UTC), and on demand.

| Job | Does |
|---|---|
| `Ruff (lint + format)` | `ruff check .` then `ruff format . --check` |
| `Pytest` | `pip install -r requirements_test.txt && python -m pytest tests/ -v` |
| `Hassfest` | `home-assistant/actions/hassfest@master` — validates `manifest.json` |
| `HACS Validation` | `hacs/action@main`, `category: integration`, **no ignored checks** |
| `gate` | Aggregates the four; `if: always()` and fails unless all four succeeded |

**`gate` exists because the branch ruleset needs a single required check.** Adding a job means adding it to
`gate`'s `needs` *and* its `if` condition, otherwise the new job can fail without blocking a merge.

The weekly schedule is deliberate: `hacs/action@main` and `hassfest@master` are floating refs, so a new HACS or
HA release can break validation without any commit of ours.

### HACS default store

The action runs with **every check enabled** — do not reintroduce an `ignore:` key. Default-store submission
requires a run with no ignored checks, and a link to that run is part of the submission PR.

The `brands` check used to be ignored, because it demanded the `fermob` domain be registered in
[home-assistant/brands](https://github.com/home-assistant/brands). It now passes on its own: the check looks
for `custom_components/fermob/brand/icon.png` first and only falls back to the brands CDN if that file is
missing. See [BRANDING.md](BRANDING.md).

Standing requirements for the default store, all currently satisfied — breaking any of them un-lists the
repository:

| Requirement | Where it lives |
|---|---|
| Public, unarchived, issues enabled, description set, topics defined | GitHub repository settings |
| At least one published **release** (a tag alone is not enough) | `release.yml` handles this |
| `hacs.json` present with at least a `name` | repository root |
| Valid `manifest.json` | `custom_components/fermob/` |
| HACS action passing with no ignores, plus hassfest | `validate.yml` |
| A brand `icon.png` | `custom_components/fermob/brand/` |

### An unexplained flake

An early run reported `hacsjson` and `integration_manifest` failures quoting file content that **did not exist
on the branch being tested** (the old `description` key, after it had been removed). I initially attributed this
to `hacs/action` validating the default branch; that explanation is wrong — the check later passed while `main`
still carried both defects. The cause is unknown and it has not recurred. If you see HACS complain about
content you cannot find, suspect this before rewriting your config.

## Branch ruleset

`main` is protected by a repository ruleset (not legacy branch protection), identical to
`ha-geosphere-next`'s:

- No deletion, no force-push (`non_fast_forward`), **linear history required**
- Pull request required: 0 approvals, but **conversation resolution required**, and **squash is the only allowed merge method**
- Required status check: **`gate`**, with strict up-to-date-branch enforcement
- No bypass actors — this applies to the repository owner too

Practical consequence: you cannot push to `main`, and a PR cannot merge until `gate` is green and the branch is
current with `main`.

## Releases — `.github/workflows/release.yml`

Fully automatic. On a **successful `Validate` run on `main`**:

1. Read `version` from `custom_components/fermob/manifest.json`.
2. If a release tagged `v<version>` already exists, stop.
3. Extract the matching `## <version>` section from `CHANGELOG.md` as the release notes.
4. Create the tag and release at that commit.

So **releasing means merging a PR that bumps `manifest.json` and adds the matching `CHANGELOG.md` section.**
There is nothing to run by hand. Forget the CHANGELOG section and you get a release with empty notes.

## Dependabot

`.github/dependabot.yml` — weekly, grouped: all `github-actions` updates in one PR, all `pip` updates in
another.

`.github/workflows/dependabot-version-bump.yml` then bumps the patch version in `manifest.json` and prepends a
`CHANGELOG.md` entry on Dependabot's PR, so the merge produces a release.

> **It needs `GH_ACTION_APP_CLIENT_ID` and `GH_ACTION_APP_PRIVATE_KEY` in BOTH secret stores.**
> The client ID (`Iv23li…`) is not the numeric App ID — `create-github-app-token` deprecated `app-id`.
> Dependabot-triggered `pull_request` runs read the **Dependabot** secret store, not the Actions one —
> Actions-only secrets arrive as empty strings and `actions/create-github-app-token` fails. Set them under
> Settings → Secrets and variables → **Actions** *and* → **Dependabot**.
>
> A GitHub App token is used rather than `GITHUB_TOKEN` because a push made with `GITHUB_TOKEN` does not
> re-trigger workflows, so `gate` would never re-run on the bumped commit.

## Installation into Home Assistant

**HACS custom repository** (this repo is not in the default store):

1. HACS → ⋮ → Custom repositories
2. Add `https://github.com/slettmayer/ha-fermob`, category **Integration**
3. Download, then restart Home Assistant

HACS installs from the newest **release**, which is why `release.yml` matters — without a release there is
nothing for HACS to offer.

Manual install: copy `custom_components/fermob/` into the HA config directory and restart.

## Repository settings

Mirrored from `ha-geosphere-next` and verified equal via the API: squash-only merges with
`COMMIT_OR_PR_TITLE` / `COMMIT_MESSAGES`, delete-branch-on-merge, auto-merge and update-branch enabled, issues
on, projects and wiki off, and the same 12 issue labels.
