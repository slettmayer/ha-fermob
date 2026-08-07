# Contributing

## Development Cycle

### Making Changes

1. Create a feature branch from `main` — `main` is protected and cannot be pushed to
2. Make your changes
3. Run linting locally: `ruff check . && ruff format . --check`
4. Run tests locally: `python -m pytest tests/ -q`
5. Bump `version` in `custom_components/fermob/manifest.json`
6. Add a new `## X.Y.Z` section at the top of `CHANGELOG.md` with your changes
7. Update the matching `docs/` file in the **same PR** if you changed documented behaviour
8. Create a PR — CI runs automatically (ruff, pytest, hassfest, HACS validation)
9. Merge PR (squash — it is the only permitted merge method)
10. Release is created automatically after validation passes on `main`

### Releasing

Releases are fully automated. When a PR that changes the version in `manifest.json` is merged to `main`:

1. The `Validate` workflow runs (ruff, pytest, hassfest, HACS validation)
2. On success, the `Auto Release` workflow creates a git tag and GitHub release
3. Release notes are extracted from `CHANGELOG.md`
4. HACS picks up the new release

No manual tagging or release creation needed. If you forget the `CHANGELOG.md` section, you get a release with
empty notes.

### Dependabot PRs

Dependabot PRs are auto-bumped: a workflow increments the patch version in `manifest.json` and prepends a
changelog entry. Reviewers only need to approve and merge.

That workflow needs `GH_ACTION_APP_CLIENT_ID` and `GH_ACTION_APP_PRIVATE_KEY` in **both** the Actions *and* Dependabot
secret stores — see [docs/tech/INFRASTRUCTURE.md](docs/tech/INFRASTRUCTURE.md#dependabot).

### Versioning

- **MAJOR** (1.0.0): Breaking changes — config-entry or options schema changes that require re-adding the lamp, removed entities, changed entity IDs or `unique_id`, or a change to the `.storage/fermob_*` key format that would force re-pairing
- **MINOR** (0.2.0): New features — support for a new lamp family or model, a new entity, a new service, a new config option
- **PATCH** (0.1.1): Bug fixes — a wrong payload byte, connection or reconnect handling, availability behaviour

**Documentation-only and CI-only changes need no version bump**, and therefore produce no release. Steps 5 and 6
of the cycle above do not apply to them: nothing about the installed integration changed, and bumping would
prompt every user to update for a README edit.

Anything that forces a user to **factory-reset and re-pair a lamp is a MAJOR change**, however small the diff.
Re-pairing is a physical trip to the lamp with a 10-second button hold, and it hands ownership back and forth
with the Fermob app.

### Changelog Format

```
## X.Y.Z

- Description of change
- Another change
```

- No `[Unreleased]` section — every changelog entry ships with a version bump
- Version headers: `## X.Y.Z` (no brackets, no dates)
- Flat bullet points (no subcategory headers like `### Fixed`)
- Prefix bullets with context if helpful: `- Fix: ...`, `- Add: ...`
- Write for residents, not maintainers: the release notes are what a user reads in HACS

### Testing

- Install test dependencies: `pip install -r requirements_test.txt`
- Run: `python -m pytest tests/ -q` — 1102 tests, about 12 s, most of it importing Home Assistant
- **`tests/test_protocol.py` needs neither Home Assistant nor a `hass` fixture**, which is the point of keeping
  `protocol.py` free of `homeassistant` imports. It is the bulk of the suite
- **`tests/test_light.py` needs a real `hass` fixture**, which is why `requirements_test.txt` installs
  `pytest-homeassistant-custom-component`
- **`tests/test_connection_profile.py` needs no fixture but does import the `fermob` package**, and that
  reaches Home Assistant through `__init__.py` — so it needs HA installed, just not a `hass` instance
- See [docs/tech/TESTING.md](docs/tech/TESTING.md) for what the suite does and does not establish, and for the
  surface that is still verified only against a real lamp — the pairing handshake, ACK matching, long-frame
  reassembly, the idle disconnect's real timing, and the options flow

### Code Style

- Enforced by [Ruff](https://docs.astral.sh/ruff/) — runs in CI
- Run locally: `pip install -r requirements_lint.txt && ruff check . --fix && ruff format .`
- See `pyproject.toml` for rule configuration, and [docs/tech/CONVENTIONS.md](docs/tech/CONVENTIONS.md) for the conventions ruff cannot enforce
- Note that `ruff format` also formats Python code blocks inside Markdown, so run it after editing docs that contain Python

## Protocol Changes

This integration is built entirely on reverse engineering, which puts two extra obligations on protocol work.

**State your confidence.** Every claim in code comments, commit messages and docs must make clear whether it is
**verified on hardware**, **derived from the official app's JS**, or **inferred**. Never present an inference as
a fact — a confident wrong protocol note costs the next person hours. See
[docs/domain/LINKIO-PROTOCOL.md](docs/domain/LINKIO-PROTOCOL.md).

**Say what you tested it on.** If you change frame or payload construction, test it against a real lamp and name
the model in the PR. If you cannot, say so explicitly rather than leaving it implied — an untested protocol
change is still worth submitting, but only if it is labelled as one.

**Check the dead ends first.** [DEAD-ENDS.md](docs/domain/DEAD-ENDS.md) records what these lamps do *not*
answer — state queries, notifications after a plain reconnect, the model in the advertisement. Those were each
established the hard way; re-implementing one costs a 3-second timeout on every command.

## Adding a Lamp Model

Open an issue with the model name and debug log output
(`logger: logs: custom_components.fermob: debug`). Useful details: what the lamp is called in the Fermob app,
whether brightness and colour temperature respond, and the `→FIRE` frames from the log.

If the lamp is tunable white and simply works, the only change needed may be a note in the supported-devices
table and the confidence table in [docs/domain/DEVICES.md](docs/domain/DEVICES.md#confidence).

## Relationship to Upstream

This is a fork of [edouardrosset/ha-fermob](https://github.com/edouardrosset/ha-fermob). Before contributing
anything here back upstream, read [docs/tech/UPSTREAM.md](docs/tech/UPSTREAM.md) — in particular, the
lamp-family default would have to flip, because upstream's users all have Hoopiks.
