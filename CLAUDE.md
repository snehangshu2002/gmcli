# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`gmcli` — a Gmail CLI published to PyPI as `gmcli`, installing a single command
named `gmail`. Python 3.10+, Typer + rich, built with hatchling, managed with uv.
Source lives under `src/gmcli/`. Not currently a git repository.

## Commands

```bash
uv venv && uv pip install -e '.[dev]'   # first-time setup

uv run pytest                            # full suite (~200 tests, all offline)
uv run pytest tests/test_compose.py      # one file
uv run pytest tests/test_compose.py::test_reply_subject_gets_one_re_prefix   # one test
uv run pytest -k threading -q            # by keyword

uvx ruff@latest check --select F,E9 src/ tests/    # lint (unused imports, syntax)
uv build                                 # sdist + wheel into dist/

uv run gmail --help                      # run the CLI from source
```

The test suite never touches the network or a real mailbox — see Testing below.
There is no separate typecheck or format step configured.

## Two invariants that must not be broken

**1. The scope is `gmail.modify`, and only that.** Defined once in
`auth/flow.py:SCOPES`. It covers read, search, send, drafts, labels, archive,
trash, and untrash, and it structurally *cannot* permanently delete mail —
that would require `https://mail.google.com/`. This is why there is no
`gmail delete` command, only `trash`/`untrash`. Do not add a permanent-delete
path or widen the scope; the safety property is the absence of the capability,
not a confirmation prompt. `tests/test_commands.py::test_there_is_no_permanent_delete_command`
guards this.

**2. JSON key names are public API.** Scripts pipe `--json` into `jq`. The
golden key sets at the top of `tests/test_output.py` lock them — adding a key is
fine, renaming or removing one must break a test first.

## Architecture

Four layers, each with a single job. Read `models.py` and `output.py` first;
they explain most of the rest.

```
commands/   argument parsing, orchestration, user interaction
    ↓
api/        typed wrappers over Gmail REST (messages, threads, labels,
            attachments, compose)
    ↓
api/client.py   the authenticated service + retry, batching, error mapping
```

- **`models.py`** is the only module that knows the shape of a Gmail API
  payload. Everything downstream works with `Message`, `Thread`, `Label`,
  `Attachment`. If you find yourself indexing `payload["payload"]["headers"]`
  outside this file, it belongs in here instead.
- **`output.py`** owns rendering. Under `--json` it writes one JSON document to
  stdout and moves *all* human messaging to stderr — that is what keeps pipes
  clean. Deliberate exceptions write raw content straight to stdout because the
  content *is* the output: `read --raw`, `send --dry-run`, `cache path`,
  `--version`.
- **`api/client.py`** wraps every call in retry-with-backoff (429/5xx, honoring
  `Retry-After`) and maps Google exceptions onto the `errors.py` hierarchy.
  Never call `.execute()` directly — go through `client.execute()`,
  `client.batch_get()`, or `client.paginate()`.
- **`context.py:AppContext`** carries global flags and builds collaborators
  lazily. `client`, `cache`, `labels`, and `account` are all properties that do
  work on first access, so commands that need no network (`--help`,
  `cache clear`, `send --dry-run`) never trigger a token refresh. Keep it that
  way when adding commands.

### Exit codes are enforced by wrapping, not by `main()`

`errors.py` defines the hierarchy and each class carries its `exit_code`
(0 ok, 1 general, 2 usage, 3 auth, 4 not found, 5 API/network).

`cli.py:_install_error_handling(app)` runs at import time *after* all
registration and wraps every command callback so a raised `GmcliError` becomes
its exit code. This is why the contract holds identically for the console
script, `python -m gmcli`, and `CliRunner` in tests.

Consequences when editing `cli.py`:

- **Register every new sub-Typer above the `_install_error_handling(app)` call**
  near the bottom, or its commands will not be guarded.
- To signal a failure, `raise` the appropriate `GmcliError` subclass with a
  `hint=` — do not call `sys.exit` or print-and-return.

### Two command registration styles

Both are in use, deliberately:

- **Command groups** (`auth`, `labels`, `draft`, `attachments`, `cache`) define
  a module-level `app = typer.Typer(...)` and are attached in `cli.py` via
  `add_typer`.
- **Root-level commands** (`ls`, `search`, `read`, `archive`, `send`, …) live in
  modules exposing `def register(app)` and are attached by calling it. This
  exists because those commands sit directly on the root app rather than under a
  group.

`commands/setup.py` is a third case: it exposes `register(auth_app)` and is
called at the *bottom* of `commands/auth.py` (`setup.py` imports nothing from
`auth.py`, so there is no cycle). It also reorders itself to the front of the
group's help, since it is where a new user starts.

### The `#N` reference system

Gmail ids are 16 hex characters, so every listing prints an index column and
records the displayed ids to the cache. `idref.py:resolve()` then expands `#3`,
`#1-5`, `#1,3,7`, and bare full ids into concrete ids.

Any command taking a message or thread must route its arguments through
`resolve()` (or `resolve_one()`), never use them directly. Listings must call
`cache.set_listing(kind, ids)` — `commands/listing.py` is the reference.

### OAuth client resolution

`auth/client_config.py` resolves the OAuth client from five sources in a fixed
order (explicit `--credentials` → `GMCLI_CLIENT_ID`/`GMCLI_CLIENT_SECRET` env →
installed file → `[oauth]` in config → build-time bundled constants) and always
reports which one won via `auth status`.

`BUNDLED_CLIENT_ID`/`BUNDLED_CLIENT_SECRET` are `None` and a test asserts they
stay that way. Shipping credentials would require Google OAuth verification plus
a paid annual CASA security assessment for the restricted scope, and caps the
package at ~100 users lifetime. Read the comment above those constants before
touching them.

`auth/store.py` prefers the OS keyring and falls back to a `0600` file, deciding
by actually round-tripping a probe value rather than inspecting the backend
class — an importable backend can still fail at runtime on a headless host.

### The 7-day refresh-token trap

If a user's Google Cloud consent screen is left in "Testing", refresh tokens
expire after 7 days and surface much later as an opaque `invalid_grant`. This is
the single most common support issue. It is handled in three places that must
stay consistent: `auth/flow.py:PUBLISHING_STATUS_HINT`, the `token_age` and
`refresh` checks in `gmail auth doctor`, and step 4 of the setup wizard.

## Paths and configuration

XDG via `platformdirs`, all constructed in `config.py` — never hardcode a path.
Config at `~/.config/gmcli/config.toml`, OAuth client and tokens under
`~/.local/share/gmcli/` at mode `0600`, disposable cache in `~/.cache/gmcli/`.

Anything holding a secret must be written with `config.py:write_secret_file`,
which creates the file at `0600` via `os.open` — `open()` then `chmod()` leaves
a window where the file is world-readable.

## Testing

`tests/conftest.py` provides a `FakeService` that mimics a googleapiclient
resource chain, so real code paths run unmodified — including batching. Register
responses by dotted path:

```python
service.handlers["users.threads.list"] = {"threads": [{"id": "abc"}]}
service.handlers["users.messages.get"] = lambda kwargs: make_message(kwargs["id"])
service.calls   # every (path, kwargs) actually issued, for assertions
```

Key fixtures: `isolated_dirs` redirects all XDG paths to a tmpdir and forces
`GMCLI_TOKEN_STORE=file` so no test can reach the developer's real keyring;
`env` (in `test_commands.py`) additionally logs in a fake account and patches
`GmailClient.for_account`. `make_message()` builds realistic payloads — note it
produces a bare `text/plain` payload for single-part messages and only adds
`parts` for multipart, matching what Gmail actually returns.

Command tests invoke the real Typer app through `CliRunner`, so they cover
argument parsing, context building, and exit codes end-to-end.

## API efficiency

`messages.list`/`threads.list` return bare ids, so a naive 20-row listing costs
21 round trips. Listings must use `client.batch_get()` with
`format="metadata"` and an explicit `metadataHeaders` list — never fetch full
bodies to render a table. Label changes across many messages use
`messages.batchModify` (see `api/messages.py:modify`).

## Other agent configs

A `~/.codex/config.toml` and a `~/.gemini/` directory exist on this machine.
If you want their MCP servers, commands, or instructions available in Claude
Code, reply `/import` to see what is importable, then `/import --yes=<digest>`
to apply it.
