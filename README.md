# gmcli

Manage Gmail from the terminal — read, search, send, and organize mail without
opening a browser.

Two ways to use it, and you can move between them freely:

- **`gmail ui`** — a full-screen interactive mailbox.
- **`gmail <command>`** — one-shot commands that pipe, script, and compose.

Neither is the "real" interface. They run on the same code, hold the same
single OAuth scope, and share the same `#N` numbering, so you can browse in the
UI, quit, and immediately run `gmail archive '#2'` on the row you were looking
at.

```console
$ gmail ls -n 5
  #    From                      Subject                                   Date
  1 ★📎 Dana Whitfield            Q3 numbers, revised (3)                  09:14
  2    GitHub                     [repo] Deploy failed on main             08:02
  3    Priya Raghunathan          Re: lunch thursday?                      Mar 04
  4 📎  billing@example.com       Invoice 4471                             Mar 03
  5    Marcus Oyelaran            Notes from the offsite                   Mar 01

$ gmail read '#1'
$ gmail label add '#1' --label finance && gmail archive '#1'
$ gmail search "from:dana has:attachment" --json | jq '.[].subject'
```

Built on the Gmail API with OAuth2 — no IMAP, no app passwords.

## Why the scope matters

gmcli requests exactly one OAuth scope:

```
https://www.googleapis.com/auth/gmail.modify
```

That covers reading, searching, sending, drafts, labels, archiving, and trash.
It **cannot permanently delete mail** — that would require the much broader
`https://mail.google.com/`, which gmcli never asks for.

So there is no `gmail delete`. `gmail trash` moves a conversation to Trash,
where Gmail keeps it for 30 days, and `gmail untrash` brings it straight back.
The guarantee is structural rather than a confirmation prompt: even a bug in
this tool cannot destroy your mail irreversibly.

## Install

```console
$ pipx install gmcli     # or: uv tool install gmcli
```

This installs a single command, `gmail`. Then run `gmail auth setup` once.

### Staying up to date

Once a day, in the background, gmcli asks PyPI whether there is a newer
release. It never delays a command to do it — the answer is cached, and the
*next* run mentions it, on stderr, in one dim line:

```
Update available: gmcli 0.1.0 → 0.2.0. Run `gmail --upgrade` to install it.
```

```console
$ gmail --upgrade                   # install the latest release
```

`--upgrade` uses whatever installed this copy — `pipx upgrade`, `uv tool
upgrade`, or `pip install -U` — rather than assuming pip and breaking a pipx
shim. From a git checkout it declines and tells you to `git pull` instead.

The notice never appears under `--json` or `--quiet`, and never when stderr is
not a terminal, so nothing in a pipeline sees it. Turn the check off entirely
with `GMCLI_NO_UPDATE_CHECK=1`, or in the config file:

```toml
[update]
check = false
```

## Setup

```console
$ gmail auth setup
```

That's it — the wizard walks you through it, opens each page in your browser at
the right moment, finds the downloaded credentials file on its own, and signs
you in at the end. About two minutes, once.

If you have the `gcloud` CLI installed it will offer to create the project and
enable the API for you, skipping two of the five steps.

<details>
<summary><b>Why do I have to create anything at all?</b></summary>

Because Gmail mailbox access uses a **restricted** OAuth scope. To ship a CLI
where everyone signs in through *one* shared client, Google requires OAuth
verification **plus a paid, annually-renewed third-party security assessment**
(CASA). Until that is done, such a client is capped at roughly 100 users total
and shows a "Google hasn't verified this app" screen to every one of them.

There is also a security argument for the per-user model: a client secret
shipped inside a PyPI package is extractable by anyone, so a shared client is
a ready-made phishing kit carrying the tool's branding. With your own client,
your mail is reachable only by credentials you own, on quota you own, and
revoking gmcli is a single click in your own project.

If you are forking gmcli for an internal Workspace tool where you control the
org — or you have completed verification — you can bundle a client and skip
all of this. See [Bundling your own client](#bundling-your-own-client).

</details>

<details>
<summary><b>What the wizard does, if you would rather click through it yourself</b></summary>

1. **Create a project** — <https://console.cloud.google.com/projectcreate>.
   Any name; `gmcli` is fine.

2. **Enable the Gmail API** —
   <https://console.cloud.google.com/apis/library/gmail.googleapis.com>.

3. **Configure the consent screen** — the *Branding* page under Google Auth
   Platform. Exactly three fields are required; leave the rest blank:

   - **App name** — anything, `gmcli` is fine
   - **User support email** — your address
   - **Developer contact information** — your address again

   Skip the logo, app domain, privacy policy, and authorized domains. User type
   **External** (or **Internal** on a Workspace account, which skips the
   unverified-app warning entirely). Click **Save** — the next step's
   **Publish app** button stays greyed out until this page is complete.

4. **Publish the app.** ⚠️ This is the step everyone misses. While the app sits
   in **Testing**, Google expires refresh tokens after **7 days** — everything
   works for a week, then fails with an opaque `invalid_grant`. Click
   **Publish app** to move it to **In production**. It stays unverified, which
   is expected: you will see a "Google hasn't verified this app" screen at
   login, and click **Advanced → Go to (your app)**.

5. **Create the OAuth client** — *Credentials → Create credentials → OAuth
   client ID*, application type **Desktop app** (not "Web application" — only a
   desktop client can use the loopback redirect). Download the JSON.

Then hand it over:

```console
$ gmail auth login --credentials ~/Downloads/client_secret_1234.json
```

gmcli installs the file into its own data directory at mode `0600`, so later
logins need no flag.

A file that came out of `~/Downloads` or `~/Desktop` is **moved** rather than
copied — the browser wrote a working OAuth client into a directory that syncs,
backs up, and gets shared out of, and gmcli will not leave a second copy of it
there. The destination is written first and the original removed only once that
succeeded, so there is always at least one readable copy. Pass
`--keep-download` to leave the file where it is; a client anywhere else is
copied and never touched.

</details>

Check it worked:

```console
$ gmail auth status
$ gmail auth doctor     # if anything looks wrong
```

### Supplying the client without a file

Handy for containers, CI, and dotfile-managed setups:

```console
$ export GMCLI_CLIENT_ID=1234-abc.apps.googleusercontent.com
$ export GMCLI_CLIENT_SECRET=...
$ gmail auth login
```

Or in `~/.config/gmcli/config.toml`:

```toml
[oauth]
client_id = "1234-abc.apps.googleusercontent.com"
client_secret = "..."
```

gmcli resolves the client in this order, and `gmail auth status` always reports
which source won:

1. `--credentials PATH` on this run
2. `GMCLI_CLIENT_ID` / `GMCLI_CLIENT_SECRET`
3. the client installed by `gmail auth setup`
4. `[oauth]` in `config.toml`
5. a client bundled into the build (unset by default)

### Bundling your own client

If you are forking gmcli for an internal tool, or have completed OAuth
verification, fill in `BUNDLED_CLIENT_ID` and `BUNDLED_CLIENT_SECRET` in
`src/gmcli/auth/client_config.py` and rebuild. `gmail auth login` then works
with no setup at all.

Read the comment above those constants first — for a public release the
restricted-scope verification and CASA assessment requirements apply, and the
~100-user cap will bite before anything else does.

## Usage

Everything below has a keystroke in `gmail ui`, and everything in `gmail ui`
has a command below. Pick whichever suits the moment.

### The interactive mailbox

```console
$ gmail ui                          # open on the inbox
$ gmail ui --mailbox finance        # open on a label
$ gmail ui --search "is:unread has:attachment"
$ gmail ui -n 200                   # fetch 200 rows per page instead of 50
$ gmail ui --no-mouse               # leave text selection to the terminal
```

```
 ▎gmail  you@example.com                           Inbox  12 unread  · 14:07  ⟳ refresh
MAILBOXES             ▏    #     FROM         SUBJECT                               DATE
▎Inbox             12 ▏▎   1 ●   Dana Ortiz   Q3 numbers, final pass               14:02
 Unread            12 ▏    2 ●   GitHub       [gmcli] PR #12  sanket opened a …    13:11
 Starred              ▏ ✓  3  ★▣ Priya Raman  Re: contract redlines                11:40
 Sent                 ▏    4     AWS Billing  Your invoice is available           Mar 03
 Drafts             2 ▏    5     Sam Iyer     lunch thursday?  the thai place …   Mar 01
 All Mail             ▏
 Trash                ▏
 Spam                 ▏
 LABELS ───────────── ▏
 clients/acme         ▏
 finance            3 ▏
 · 5 conversations  ·  ] for the next page
 j/k move  ↵ open  / search  ]/[ page  x mark  a archive  s star  u unread  L label
```

`●` is unread, `★` starred, `▣` carries an attachment, and `✓` is a row you
marked for the next action. The bar down the first column is the cursor. Where
a subject leaves room, Gmail's own snippet fills the rest of the line.

The time in the header is when the list was last loaded, so you can see at a
glance how stale it is. **⟳ refresh** in the top right re-fetches the mailbox
and the unread counts; `Ctrl-R` and `.` do the same from the keyboard.

Keyboard-driven throughout, and the mouse works too — click, double-click,
right-click, scroll, and the key bar along the bottom is clickable.

| Key | Does |
|---|---|
| `j` `k` `↓` `↑` | move; `g`/`G` jump to the ends, `Ctrl-D`/`Ctrl-U` page |
| `Tab` | switch between the sidebar and the list |
| `Enter` | open the conversation (and mark it read, as a mail client does) |
| `x` | mark a row — actions then apply to every marked row; `v` clears |
| `a` / `A` | archive / move back to the inbox |
| `s` / `u` | toggle star / toggle read |
| `L` | add a label; prefix with `-` to remove one |
| `d` | move to Trash, after a confirmation |
| `w` | save attachments — one of them, `1,3` / `2-4`, or `a` for all |
| `c` / `r` / `R` / `f` | compose / reply / reply-all / forward, via `$EDITOR` |
| `/` | search with Gmail's own query syntax; `Esc` clears it again |
| `]` `[` | next / previous page — mail past the rows already fetched |
| `t` | switch between conversations and individual messages |
| `n` | change the page size — how many rows to fetch at a time |
| `Ctrl-R` or `.` | fetch the latest mail — or click **⟳ refresh** in the top right |
| `i` | view an image attachment inline |
| `M` | turn mouse reporting off |
| `?` | the full key reference |
| `q` `Esc` | back, or quit from the list |

Anything that sends mail opens `$EDITOR` and then asks for a `y` before it
goes. Nothing in the UI can delete mail — same scope, same guarantee.

#### Getting at more than the first screenful

`-n` is the size of one **page**, not a ceiling on what the UI can reach. The
inbox is fetched fifty conversations at a time; `]` asks Gmail for the next
fifty and `[` comes back, so you can walk the whole mailbox without loading it
all at once. The header shows which page you are on as soon as there is more
than one, and the status line says when there is a page after this one.

Three ways at the same mailbox, and they compose:

- the **sidebar** filters by mailbox or label — Inbox, Unread, Starred, Sent,
  Drafts, All Mail, Trash, Spam, then every label you have made;
- **`/`** filters by query, using Gmail's own syntax exactly as `gmail search`
  does — `from:dana`, `has:attachment`, `newer_than:7d`, `is:unread`,
  `larger:5M`, or any combination. Search aliases from your config work here
  too. `Esc` drops the search and puts the mailbox back;
- **`]`** walks further back through whatever the first two left you with.

Raising `-n` and paging do different things: `-n 200` makes each fetch bigger
(and slower); `]` keeps fetches small and moves the window.

#### Saving attachments

`w` saves attachments from the open conversation — or from the row under the
cursor, without opening it. Anything Gmail lists as an attachment can be
saved: PDFs, spreadsheets, archives, images, including images the sender
pasted into Gmail's own composer.

With one attachment it asks only where to put it. With several it asks which
first — a single number, `1,3`, `2-4`, or `a` for all of them, the same shapes
`#N` references take. The folder is created if it does not exist, is
remembered for the rest of the session, and an existing file is never
overwritten: a second `invoice.pdf` lands as `invoice (1).pdf`.

`gmail attachments download` does the same thing from a script.

#### The mouse

- **Click** a row to select it, or a sidebar entry to switch mailbox.
- **Double-click** a row to open the conversation.
- **Right-click** a row to mark it.
- **Scroll** with the wheel — the list, the reader, and the key reference.
- **Click the bar along the bottom**: those key hints are buttons.

Mouse reporting takes click-drag away from your terminal's own text selection.
`M` toggles it, and `gmail ui --no-mouse` starts without it. (In Ghostty and
most others, holding Shift while dragging bypasses it either way.)

#### Images

On a terminal that can draw them, `i` shows an image attachment inline —
attachments that are images are marked `▨` in the reader.

This includes images attached through Gmail's own composer, which arrive as
`Content-Disposition: inline` with a `Content-ID`. They are ordinary
attachments and are listed, downloaded, and viewed like any other; parts with
no filename at all — tracking pixels — are still skipped. `--json` reports an
`inline` flag on each attachment if you need to tell them apart.

Large images are resampled down to roughly the cells they will occupy before
being transmitted, when Pillow is available: a 4 MB photo goes over the wire as
about 500 KB rather than 5.5 MB of base64. Without Pillow a large PNG is still
sent, just unshrunk.

| Terminal | How |
|---|---|
| Ghostty, Kitty, WezTerm, Konsole | Kitty graphics protocol |
| iTerm2 | iTerm2 inline images |
| anything truecolor | half-block `▀` cells, with Pillow installed |

PNG attachments display with no extra dependency at all — the Kitty protocol
takes PNG bytes as they are. Other formats (JPEG, GIF, WebP) need decoding
first:

```console
$ pipx install 'gmcli[images]'      # or: pip install 'gmcli[images]'
```

Detection is by environment variable, which a multiplexer can hide. Override it
with `GMCLI_IMAGE_PROTOCOL=kitty|iterm2|blocks|none`, or turn the whole thing
off with `gmail ui --no-images`.

#### Links

URLs in a message body are emitted as OSC 8 hyperlinks, so Ghostty, Kitty,
WezTerm, iTerm2 and modern VTE terminals make them clickable — no need to
select and copy the address out of the pane. `gmail read` does the same, and a
`[1]` standing in for a long address is a link to it, so a footnoted URL costs
you nothing: click the marker where you found it, or the address at the bottom.
A URL folded across two lines stays clickable on both halves.

### Reading

```console
$ gmail ls                          # inbox conversations
$ gmail ls -n 50 --unread           # 50 unread
$ gmail ls --label finance          # by label
$ gmail ls --messages               # individual messages, not conversations
$ gmail read '#3'                   # a whole conversation
$ gmail read '#3' --latest          # just the newest message
$ gmail read '#3' --show-quoted     # expand the quoted history
$ gmail read '#3' --raw             # original RFC 822 source
```

#### Mail that was written for a browser

Most bulk mail — a receipt, an account notification, a newsletter — is authored
as HTML, and the plain-text half a sender attaches beside it is usually machine
output rather than prose: Outlook conditional-comment markers left in the text,
every paragraph repeated once per branch of them, and each link's address glued
onto the front of its own label.

gmcli cleans that up before showing it. The markers go, the repeated blocks
collapse to one, lines the sender hard-wrapped are rejoined so the text wraps to
*your* width, and a URL too long to sit inside a sentence becomes a numbered
reference with the addresses listed at the end of the message. The marker is
itself a hyperlink, so nothing becomes harder to open by being footnoted. When the plain part is a broken flattening of
the HTML, gmcli flattens the HTML itself instead, which keeps the sentences
whole. Mail that arrives as HTML only is rendered the same way.

`gmail read --html` shows the HTML source when you want to see it, and
`--raw` the original message exactly as it arrived.

### Searching

The query goes straight to Gmail, so every operator the web UI supports works
here unchanged:

```console
$ gmail search "from:dana after:2026/01/01 has:attachment"
$ gmail search "subject:invoice larger:5M"
$ gmail search '"exact phrase" -label:promotions'
$ gmail search "is:starred" --limit 100
```

Convenience flags compose with a raw query:

```console
$ gmail search "from:github" --unread --attachments
```

### The `#N` shorthand

Gmail ids are 16 hex characters. Every listing prints an index column, and the
next command accepts those references:

```console
$ gmail ls
$ gmail archive '#1'
$ gmail label add '#1-5' --label triage       # a range
$ gmail mark read '#1,3,7'                    # a selection
```

Quote them so your shell does not treat `#` as a comment. Full ids always work
too, so scripts never depend on this state.

`gmail ui` writes the rows it shows to the same place, so the numbering carries
across the two interfaces in both directions:

```console
$ gmail ui                                    # browse, quit on the finance label
$ gmail label add '#1-3' --label triage       # act on what was just on screen
```

### Sending

```console
$ gmail send --to dana@example.com --subject "Q3" --body "Numbers attached." -a q3.pdf
$ gmail send --to a@x.com --to "b@x.com, c@x.com" --subject "Hi"    # repeatable or comma-separated
$ echo "deployed" | gmail send --to ops@example.com --subject "Deploy OK"
$ gmail send --to dana@example.com --subject "Notes" --body-file notes.md
$ gmail send --to dana@example.com --subject "Long one"    # opens $EDITOR
```

Always check first with `--dry-run`, which prints the exact MIME that would be
sent and calls nothing:

```console
$ gmail send --to dana@example.com --subject "Test" --body hi -a report.pdf --dry-run
```

Replies stay in the conversation (`In-Reply-To`, `References`, and `threadId`
are all set):

```console
$ gmail reply '#2' --body "Sounds good."
$ gmail reply '#2' --all --body "Looping everyone in."
$ gmail forward '#2' --to legal@example.com --body "FYI"
```

Drafts:

```console
$ gmail draft create --to dana@example.com --subject "Later"
$ gmail draft list
$ gmail draft send <draft-id>
```

### Organizing

```console
$ gmail archive '#1'                          # remove from inbox
$ gmail unarchive '#1'
$ gmail mark read '#1-5'
$ gmail mark star '#2'
$ gmail trash '#4'                            # recoverable for 30 days
$ gmail untrash '#4'

$ gmail labels list
$ gmail labels list --counts                  # with unread counts
$ gmail labels create "clients/acme"          # '/' nests
$ gmail label add '#1' --label clients/acme
$ gmail label add '#1' --label newthing --create
$ gmail label remove '#1' --label triage
```

All of these act on whole conversations by default; add `--messages` to act on
individual messages instead.

### Attachments

```console
$ gmail attachments list '#1'
$ gmail attachments download '#1' --all -o ~/Downloads
$ gmail attachments download '#1' --index 2
$ gmail attachments download '#1' --name '*.pdf'
```

Filenames from incoming mail are sanitized before anything is written, and
collisions get a numeric suffix rather than overwriting.

## Scripting

Add `--json` to any command. (`gmail ui` is the one exception — it is
interactive and has no JSON form; it will tell you so and point at
`gmail ls --json`.) The JSON document goes to stdout; every human
message, warning, and progress line goes to stderr, so pipes stay clean:

```console
$ gmail search "is:unread from:alerts" --json | jq -r '.[].subject'
$ gmail ls --json | jq -r '.[] | select(.has_attachments) | .id'
$ gmail labels list --json | jq -r '.[] | select(.type=="user") | .name'
```

Exit codes:

| Code | Meaning |
|-----:|---------|
| 0 | success |
| 1 | general failure |
| 2 | usage error |
| 3 | authentication failure |
| 4 | not found |
| 5 | API or network failure |

## Multiple accounts

```console
$ gmail auth setup                                      # once, first account
$ gmail auth login                                      # repeat per account
$ gmail auth list
$ gmail auth switch work@example.com                    # set the default
$ gmail --account personal@gmail.com ls                 # override per command
```

## Configuration

`~/.config/gmcli/config.toml` (XDG-aware):

```toml
default_account = "you@gmail.com"

[output]
color = true
max_results = 20

[send]
signature = "— sent from the terminal"

[update]
# Ask PyPI once a day whether a newer gmcli exists. Default: true.
check = true

# Optional: supply the OAuth client here instead of a downloaded JSON file.
# [oauth]
# client_id = "1234-abc.apps.googleusercontent.com"
# client_secret = "..."

[aliases]
# `gmail search unread-bills` expands to the query on the right
unread-bills = "is:unread (subject:invoice OR subject:receipt)"
```

## Where things are stored

| What | Where |
|------|-------|
| Config | `~/.config/gmcli/config.toml` |
| OAuth client | `~/.local/share/gmcli/client_secret.json` (mode 0600) |
| Tokens | OS keyring, or `~/.local/share/gmcli/accounts/*.json` (mode 0600) |
| Cache | `~/.cache/gmcli/` — safe to delete at any time |

Refresh tokens go to the OS keyring (GNOME Keyring, KWallet, macOS Keychain,
Windows Credential Locker) when one is available, and fall back to an
owner-only file when it is not — which is the normal case on headless servers
and in containers. `gmail auth status` tells you which is in use, and
`GMCLI_TOKEN_STORE=file` forces the file backend.

## Headless and remote machines

The OAuth flow needs a browser to reach a loopback redirect on the machine
running gmcli. Google removed the copy-paste (out-of-band) flow in October
2022, so there are two working options:

**Forward the port over SSH** (recommended):

```console
$ ssh -L 8899:127.0.0.1:8899 you@server
$ gmail auth login --port 8899          # on the server
```

Open the printed URL in your local browser. Add `http://localhost:8899/` to the
OAuth client's authorized redirect URIs if Google objects.

**Or authenticate locally and copy the token:**

```console
$ scp ~/.local/share/gmcli/accounts/you@gmail.com.json server:~/.local/share/gmcli/accounts/
```

Set `GMCLI_TOKEN_STORE=file` on the desktop first so the token lands in a file
rather than the keyring.

The OAuth *client* needs no file on the remote host either — set
`GMCLI_CLIENT_ID` and `GMCLI_CLIENT_SECRET` in the environment instead.

## Troubleshooting

```console
$ gmail auth doctor
```

Checks the OAuth client, stored accounts, token store backend, token file
permissions, token age, whether a refresh actually works right now, granted
scopes, and system clock skew — and explains each failure rather than printing
a traceback.

**"It worked for a week and then stopped."** Your consent screen is in
Testing. Publish the app — see step 4 in [Setup](#setup). `gmail auth doctor`
detects this and says so.

**"No OAuth client configured."** Run `gmail auth setup`.

**"Publish app" is greyed out**, with "Your app's OAuth configuration is
incomplete." The Branding page is unfinished. Fill in the app name, user
support email, and developer contact email there, click **Save**, then return
to *Audience* and publish.

**"Google hasn't verified this app."** Expected for your own unverified client.
Click **Advanced → Go to (your app)**.

**"Access blocked: this app's request is invalid."** You created a
*Web application* client instead of a *Desktop app* client.

## Development

```console
$ git clone https://github.com/snehangshu2002/gmcli && cd gmcli
$ uv venv && uv pip install -e '.[dev]'
$ uv run pytest
```

The test suite runs entirely offline against a fake Gmail service — no test
touches the network or a real mailbox.

Releases are cut by pushing a `v*` tag: `.github/workflows/release.yml` runs the
suite, builds the sdist and wheel, refuses a tag that disagrees with
`__version__`, and uploads to PyPI over Trusted Publishing, with no API token
stored anywhere.

## License

MIT
