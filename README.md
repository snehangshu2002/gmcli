# gmcli

Manage Gmail from the terminal — read, search, send, and organize mail without
opening a browser.

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

gmcli copies the file into its own data directory at mode `0600`, so later
logins need no flag.

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

Add `--json` to any command. The JSON document goes to stdout; every human
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
$ git clone https://github.com/snehangshu/gmcli && cd gmcli
$ uv venv && uv pip install -e '.[dev]'
$ uv run pytest
```

The test suite runs entirely offline against a fake Gmail service — no test
touches the network or a real mailbox.

## License

MIT
