# Documentation Contributing Guide

> Rules for maintaining the AI-agent documentation. Follow these when adding, updating, or restructuring any `.md` files in this system. They apply to both human developers and AI coding agents.

## Why This Architecture Exists

Coding agents read `AGENTS.md` on every session (Claude Code loads it via the `@AGENTS.md` import in
`CLAUDE.md`; Cursor/Codex/Copilot read `AGENTS.md` directly). If it is bloated with detail, it wastes the
context window on content that may not be relevant to the task. The architecture solves this:

- **`AGENTS.md`** is loaded every time — it must stay concise (index + critical one-liners). `CLAUDE.md` is a 3-line pointer that imports it; never edit `CLAUDE.md`.
- **`docs/tech/*.md`** — technical docs loaded on demand when a task touches that topic. `README.md` in that folder is the index.
- **`docs/domain/*.md`** — domain docs loaded on demand when a task touches that topic. `README.md` in that folder is the index.

Update these docs in the same pull request as the code change that affects them: stale docs mislead every
future session, and a wrong doc is worse than a missing one.

## File Locations and Scope

| Location | Purpose | Loaded when |
|----------|---------|-------------|
| `/AGENTS.md` | Concise index: tech stack, critical one-liners, links to detail | Every session |
| `/CLAUDE.md` | 3-line pointer importing `AGENTS.md` for Claude Code | Every Claude Code session (never edit) |
| `/docs/tech/README.md` | Index of technical topic guides | Agent reads it for a technical task |
| `/docs/tech/*.md` | Detailed technical guides (one topic per file) | Agent reads it for that topic |
| `/docs/domain/README.md` | Index of domain topic guides | Agent reads it for a domain task |
| `/docs/domain/*.md` | Detailed domain guides (one topic per file) | Agent reads it for that domain |

### What goes where

- **Rule in `AGENTS.md`**: a one-liner that fits a bullet, with a link to the detail.
- **Detail in `docs/tech`**: anything technical needing explanation, examples, tables, checklists, or code blocks.
- **Detail in `docs/domain`**: the lamps and the Linkio protocol — anything that would still be true if this were reimplemented in another language.

### Domain doc layout

Domain docs are **per-concept**: one entity, flow, lifecycle, or protocol layer per file. A new concept is a
**new file**, added to the table in `docs/domain/README.md`.

`docs/domain/OVERVIEW.md` is an **index, not a detail dump** — domain classification, a one-liner-per-concept
catalog with links, the cross-cutting decisions, and the glossary. `docs/domain/LINKIO-PROTOCOL.md` plays the
same role for the four `PROTOCOL-*.md` layers and carries the confidence statement that covers all of them.
If you find yourself adding a third top-level heading of detail to either index, that detail belongs in a
sub-file.

## Rules

1. **One topic per file.** If a file needs two unrelated headings at the top level, it is two files.
2. **Mark confidence explicitly.** This project is built on reverse engineering. Every protocol claim must say
   whether it is *verified on hardware*, *derived from the app's JS*, or *inferred*. Never present an inference
   as a fact — a confident wrong protocol note costs hours.
3. **No duplication.** State a fact in exactly one file and link to it. If two files need it, one of them is the wrong place.
4. **Link with relative paths** so the docs work on GitHub and in an editor.
5. **Keep `AGENTS.md` under 150 lines**, and every `docs/**` file under 300. If one grows past that, move
   detail into a sub-file and leave the one-liner plus a link.
6. **Record dead ends.** If something was tried and did not work (a command the lamp ignores, a state read that
   never answers), write it down in [domain/DEAD-ENDS.md](domain/DEAD-ENDS.md) with the reason. That is the
   knowledge that stops the next session repeating it.
