# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`gmcli` — a Gmail CLI published to PyPI as `gmcli`, installing a single command
named `gmail`. Python 3.10+, Typer + rich, built with hatchling, managed with uv.
Source lives under `src/gmcli/`.

It has two front ends over one core: the commands (`gmail ls`, `gmail send`, …)
and a full-screen interactive UI (`gmail ui`, in `src/gmcli/ui/`). Neither is
privileged. A change that gives one of them a new capability should be
considered for the other, and any capability either gains must still fit inside
the single `gmail.modify` scope.

## Commands

```bash
uv venv && uv pip install -e '.[dev]'   # first-time setup

uv run pytest                            # full suite (~350 tests, all offline)
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
ui/         the interactive mailbox — a second front end, same layers below
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

  One rule in `_walk_parts` is easy to get wrong twice: a part with an
  `attachmentId` **and** a filename is an attachment, `Content-Disposition:
  inline` or not. Inline parts used to be skipped as page furniture, which
  silently dropped every image attached through Gmail's own composer — it marks
  them inline with a `Content-ID`. The filename requirement is what still
  excludes tracking pixels.
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
- **Root-level commands** (`ls`, `search`, `read`, `archive`, `send`, `ui`, …)
  live in modules exposing `def register(app)` and are attached by calling it. This
  exists because those commands sit directly on the root app rather than under a
  group.

`commands/setup.py` is a third case: it exposes `register(auth_app)` and is
called at the *bottom* of `commands/auth.py` (`setup.py` imports nothing from
`auth.py`, so there is no cycle). It also reorders itself to the front of the
group's help, since it is where a new user starts.

### The `ui/` package

Five modules, and the split is load-bearing:

- **`ui/keys.py`** — cbreak-mode input decoded to key names (`"up"`,
  `"ctrl-d"`) or a `Mouse` event, plus the footer `LineEditor`. cbreak rather
  than raw, so Ctrl-C still raises `KeyboardInterrupt`. `ScriptedKeys` replaces
  the terminal in tests, which is what lets the real event loop run headless.

  Input is parsed off a *persistent buffer* by `parse()`, not one `read()` per
  key. A wheel spin delivers several complete SGR reports in one chunk; parsing
  one event and keeping the remainder is what stops the rest being dropped. Do
  not "simplify" this back to reading a byte at a time.

  Mouse reports use SGR (`\x1b[<b;x;yM`) because the original X10 encoding
  cannot express a column past 223. Motion reports (bit 5 of the button field)
  are *not* clicks — treating them as clicks fires actions on a drag.
  `pause()` turns mouse reporting off as well as restoring cooked mode; leaving
  it on would spray escapes into `$EDITOR`.
- **`ui/graphics.py`** — inline images. Returns the bytes to emit rather than
  writing them, so it stays testable and `app.py` keeps sole ownership of the
  screen. Kitty protocol takes PNG whole (`f=100`), which is why a PNG
  attachment displays with no Pillow; every other format needs the optional
  `gmcli[images]` extra. Anything over `DOWNSCALE_ABOVE_BYTES` is resampled to
  about the cell box it will occupy when Pillow is present — a 4 MB photo is
  5.5 MB of base64 in ~1300 protocol chunks otherwise — but a large PNG with no
  decoder available is still sent unshrunk rather than refused.

  Kitty images are not erased by drawing text over them: `app.py` sends
  `a=d,d=A` before redrawing, and removing that leaves an image stuck on
  screen.
- **`ui/state.py`** — `UIState` is everything on screen and nothing else.
- **`ui/render.py`** — pure `state → renderable`. No API calls, no mutation.
  Panes are lists of one-line `Text` objects rather than rich `Table`/`Layout`
  objects, because a pane must be *exactly* the height it was asked for or the
  two columns drift apart. It must not grow a dependency on `output.py`'s JSON
  path; `output.py` owns what commands print, and that contract has one caller.

  **Every line a frame emits is padded to exactly the terminal width**, by
  `exact()` in `frame()`. This is not cosmetic. A short line does not erase
  what was to the right of it, so an earlier frame — or another program's
  output, on a host that echoes the alt-screen sequence without honouring it —
  bleeds through the gaps. The reader was the worst case: a two-line message
  left twenty rows untouched. `test_the_reader_paints_every_cell` and its
  siblings are the guard; do not let a pane emit a naturally-sized line.
  `app.py` also calls `console.clear()` once when Live starts, for the same
  class of host.

  `THEME` is the whole palette, and two of its entries carry rules. `accent`
  (brass) is the only warm colour on screen and means exactly one thing —
  mail that wants something from you, or a key you can press; spending it on
  decoration is what makes the frame stop reading. `cursor`/`cursor_idle` set
  a **background and no foreground**, so the row under the cursor keeps its
  own colour coding instead of being flattened; the bar in the first column is
  what identifies the row on terminals where the tint is too subtle to see.

  Build a `Text` bare and style each `append`, never `Text(x, style=...)` as a
  container: a base style reaches everything appended after it. That is how
  the reader's message bodies once came out in hairline grey, and how a list
  snippet came out bold.

  The reader owns a three-cell gutter and is handed the *full* width — that
  gutter is where the thread spine is drawn, a hairline with a `◆` at each
  message, and it appears only when a thread actually has more than one
  message. `frame()` and `app.py`'s two `reader_lines()` calls must agree on
  that width or `n`/`p` jump to the wrong line.

  The footer is laid out once by `_hint_layout()`; `key_hints()` draws from it
  and `key_hint_spans()` clicks from it, so a click can never land on a hint
  the frame did not draw. Widening the gap between hints costs a hint at the
  right-hand end before it costs anything else.

  It also owns `hit_test()`, which turns a cell coordinate back into a pane and
  a row. That lives here on purpose: layout geometry has exactly one home, and
  a click landing a row off is the failure mode when it gets a second one.
  `frame()` and `hit_test()` must be edited together —
  `test_hit_test_matches_what_frame_draws` is the guard.
- **`ui/app.py`** — the event loop, key bindings, and actions.

Three things to preserve when editing it:

- Every listing the UI draws calls `cache.set_listing()`. That is what makes
  `#N` mean the same rows after quitting the UI, and it is a documented promise
  in the README.
- Network calls are synchronous, on the key that triggered them, wrapped in
  `MailApp.busy()` — which catches `GmcliError` *and* bare `Exception` and puts
  it on the status line. A UI that dies on one bad response is worse than a
  slow one. Do not let an action bypass `busy()`.
- Composing goes through `MailApp.suspended()` → `commands/send.py:
  compose_in_editor` → `api/compose.py`. That is deliberate: replies from the
  UI get the identical `In-Reply-To`/`References`/`threadId` handling as
  `gmail reply`, covered by `tests/test_compose.py`.

`refresh_counts()` runs at startup as well as on refresh: `labels.list` carries
no unread counts — only `labels.get` does — so the sidebar would come up blank
without it. It is one batched round trip.

`gmail ui` refuses to start under `--json` or without a tty, and says which.
`--no-mouse` and `--no-images` turn off the two things that depend on terminal
capabilities rather than on Gmail.

Note the one intentional divergence from the CLI: opening a conversation in the
UI marks it read, because that is what a mail client does and what the user
just signalled. `gmail read` still does not — `--mark-read` stays opt-in there.

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

`tests/test_ui.py` drives the real event loop with `ScriptedKeys` and asserts
on `UIState` and on the calls `FakeService` recorded — no terminal involved.
Mouse events go in as `Mouse` objects through the same queue. Image tests
assert on the escape bytes `graphics.py` returns, and Pillow is a dev dependency
so the decode and half-block paths are covered too.

`tests/test_documented_usage.py` executes every command line printed in the
README's Usage section. It is the guard on that section the way the golden key
sets guard the JSON: renaming a flag has to break a test first. Add a line to
the README's Usage and you add it there too.

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
