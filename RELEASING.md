# Releasing gmcli

Publishing is `git tag` and nothing else. `.github/workflows/release.yml` runs
the suite on the oldest and newest supported Python, builds the sdist and
wheel, refuses to continue if the tag disagrees with `__version__`, and uploads
to PyPI over Trusted Publishing — so there is no API token anywhere in the
repository, in the CI settings, or on your laptop.

The one-time setup is below. After that, a release is four commands.

---

## One-time setup

### 1. A PyPI account with 2FA

Register at <https://pypi.org/account/register/> and enable two-factor
authentication (Account settings → Add 2FA). PyPI requires 2FA to upload, and
Trusted Publishing will not work without an account that can own a project.

Check the name is free: <https://pypi.org/project/gmcli/>. If it is taken, pick
another and change `name` in `pyproject.toml`, `PACKAGE` in
`src/gmcli/update.py`, and the URL in the workflow's `environment:` block. The
command it installs (`gmail`) is independent of the package name.

### 2. Tell PyPI to trust this repository

gmcli has never been published, so use a **pending publisher** — it authorises
the first upload to create the project:

<https://pypi.org/manage/account/publishing/> → *Add a new pending publisher*

| Field | Value |
| --- | --- |
| PyPI Project Name | `gmcli` |
| Owner | `snehangshu2002` |
| Repository name | `gmcli` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

(For an already-published project the same form lives under
*Your projects → gmcli → Publishing*.)

Nothing is secret here. The claim GitHub signs — this repository, this
workflow, this environment — is what PyPI checks, and it cannot be replayed by
anyone else's repository or by a fork.

### 3. Create the `pypi` environment on GitHub

*Settings → Environments → New environment* → name it **`pypi`**.

It costs a minute and buys two things: the environment name is part of what
PyPI verifies, and you can add *Required reviewers* so a release waits for a
human click before it uploads.

### 4. Check Actions can run

*Settings → Actions → General* → Allow all actions. The workflow requests
`id-token: write` for itself; no repository-wide permission change is needed.

---

## Cutting a release

1. **Bump the version.** One place — `src/gmcli/__init__.py`:

   ```python
   __version__ = "0.2.0"
   ```

   Semantic versioning: patch for fixes, minor for features, major for a
   breaking change to a command, a flag, a JSON key, or an exit code.

2. **Check it locally.**

   ```console
   $ uv run pytest -q
   $ uvx ruff@latest check --select F,E9 src/ tests/
   $ uv build && uvx twine check dist/*
   ```

3. **Commit and tag.** The tag must be `v` + the version in `__init__.py`, or
   the workflow stops before uploading.

   ```console
   $ git commit -am "Release 0.2.0"
   $ git tag -a v0.2.0 -m "gmcli 0.2.0"
   $ git push && git push origin v0.2.0
   ```

4. **Watch it land.** The *Release* workflow appears under the Actions tab.
   When it is green: <https://pypi.org/project/gmcli/>.

   ```console
   $ pipx install gmcli    # or: uv tool install gmcli
   $ gmail --version
   ```

Within a day, every installed copy learns about the release the next time it
runs and says so; `gmail --upgrade` installs it.

---

## When something goes wrong

**The publish step failed but the tag is pushed.** Fix the cause, then re-run
the workflow from the Actions tab (*Re-run failed jobs*), or run it manually
via *Run workflow* — the tag does not need to move.

**The tag was wrong.** If nothing was uploaded, delete and recreate it:

```console
$ git tag -d v0.2.0 && git push origin :refs/tags/v0.2.0
```

**A bad release is already on PyPI.** It cannot be replaced — a version number
is used exactly once, forever. Yank it (*Manage → Releases → Yank*, which hides
it from new installs without breaking pinned ones) and publish a patch release.

**Rehearsing on TestPyPI.** Add a second pending publisher on
<https://test.pypi.org/> and one line to the publish step:

```yaml
      - uses: pypa/gh-action-pypi-publish@release/v1
        with:
          repository-url: https://test.pypi.org/legacy/
```

**Publishing by hand**, if you ever need to bypass CI:

```console
$ uv build
$ uvx twine upload dist/*      # username: __token__, password: a pypi-… token
```

A token created at <https://pypi.org/manage/account/token/> is only needed for
this path. The workflow never uses one.
