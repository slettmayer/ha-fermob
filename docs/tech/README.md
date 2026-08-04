# Technical Context

Detailed technical guides. Read the one that matches your task.

| Guide | Covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Module layering and why `protocol.py` stays HA-free, the BLE connection lifecycle, frame routing, the locking model, and how the entity is kept thin |
| [TECH-STACK.md](TECH-STACK.md) | Python and HA versions, the AES dependency story (never reintroduce pycryptodome), Bluetooth plumbing, test dependencies |
| [CONVENTIONS.md](CONVENTIONS.md) | Ruff config and its consequences, the module-level import rule, protocol-code rules, the single command path, naming, commit and PR expectations |
| [TESTING.md](TESTING.md) | How to run the suite, why the test module loads `protocol.py` by file path, what the tests do and do not establish, and the untested surface |
| [INFRASTRUCTURE.md](INFRASTRUCTURE.md) | CI jobs and the `gate` aggregator, the branch ruleset, automatic releases, Dependabot (including the two-secret-store trap), HACS installation |
| [UPSTREAM.md](UPSTREAM.md) | What this fork is, what we changed and why, how much of the protocol is actually verified, and what to fix before contributing back |
| [BRANDING.md](BRANDING.md) | Where the icon lives and how to regenerate it, why it ships in-repo rather than via home-assistant/brands, why it is not Fermob's logo, and what the design encodes |

For the lamps and the protocol itself, see [domain context](../domain/README.md).
