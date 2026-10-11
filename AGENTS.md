# AGENTS.md

Guidance for coding agents working in this repository.

## What this is

`igctl` is a Python 3.12+ CLI (Typer + Rich) that publishes, schedules, analyzes and automates Instagram.
It has two interchangeable backends behind one contract:

- `graph`: the official Instagram Graph API over httpx. Business/Creator accounts only.
- `private`: instagrapi, Instagram's private mobile API. Can do everything, but it violates Instagram's Terms and the account can be restricted.

**To use igctl for a task**, rather than develop it, follow [`skills/igctl/SKILL.md`](skills/igctl/SKILL.md). It covers how to call the CLI, the safety rules (approval and `--dry-run` before any writes, never handle the user's password) and the full reference.

## Commands

```bash
uv sync                                   # install deps (incl. dev group)
uv run igctl --help                       # run the CLI from source
uv run pytest -q                          # full test suite; offline, must stay green
uv run ruff check . && uv run ruff format --check .
uv run python scripts/gen_skill_reference.py   # regenerate skills/igctl/references/*.md
```

Run all of these before you finish a change. Tests must never touch the network or a real account. Mock HTTP with `respx` and instagrapi with fakes or `MagicMock`. Point `IGCTL_HOME` at a temp dir and stub `keyring`.

## Layout

```
src/igctl/
  backends/   base.py (Backend ABC + errors), graph.py, private.py
  core/       models.py (pydantic, backend-neutral), config.py (~/.igctl, keyring secrets),
              storage.py (sqlite: actions, scheduled_posts, snapshots, seen), context.py,
              ratelimit.py (Guard), media_host.py, validation.py, refs.py
  scheduler/  timeparse.py, queue.py, runner.py
  analytics/  metrics.py, report.py, followers.py, export.py
  automation/ rules.py (pydantic schema), registry.py, handlers.py, engine.py, state.py
  cli/        main.py (root + global options), _common.py (get_ctx, emit, ok, warn), one module per group
skills/igctl/ Agent Skill (SKILL.md + generated references/)
examples/     rules.yaml (annotated automation example; embedded into the skill reference)
```

## Conventions that matter

- **The backend contract lives in `backends/base.py`.** A backend raises `NotSupported(self.name, "op")` for anything its transport can't do; `cli/main.py:run()` turns that into a hint to try the other backend. Every new capability goes into the ABC first (with a `NotSupported` default), then into the backends that can do it.
- **Errors meant for the user** are `IgctlError` subclasses (`AuthError`, `RateLimited`, `NotSupported`). Map transport exceptions to these inside the backend. Never let raw httpx or instagrapi errors reach the CLI.
- **Every automated or bulk write goes through `Context.guard.act(action, target, fn)`.** The guard handles human-like delays, rolling 24-hour caps, the action log and `--dry-run`. In dry-run mode nothing may write to Instagram *or* to the store (no `mark_seen`, snapshots or enqueues).
- **CLI output** goes through `emit(ctx, data, columns=...)`, so `--json` keeps working everywhere. Status messages go to stderr via `ok()`/`warn()`. Global options (`--json`, `-a`, `-b`, `--dry-run`) belong to the root callback and come before the group name.
- **Secrets** (Graph tokens, passwords) live only in the OS keyring, or in env `IGCTL_<ALIAS>_<KIND>`, never in `config.yaml`, logs or test fixtures.
- **Models** in `core/models.py` are the only types that cross module boundaries. Convert transport payloads at the backend edge.
- **Adding an automation trigger**: write a handler in `automation/handlers.py`, register it in `registry.py`, add an example to `examples/rules.yaml`, then regenerate the skill references.
- **Changing CLI commands or options**: regenerate the skill references (`scripts/gen_skill_reference.py`). `tests/test_skill_refs.py` fails if they drift. Update the "Common tasks" table in `SKILL.md` by hand if it's affected.
- Style: ruff (line length 100, rules E/F/I/B/UP), full type hints, short comments only where the code isn't obvious.

## Safety

This tool acts on real Instagram accounts. When you test by hand, prefer read-only commands and `--dry-run`. Never run write commands (post, like, follow, DM, delete, `automate run`) against a real account unless the user explicitly asks you to. Never raise the default limits in `core/config.py` without the user's sign-off.
