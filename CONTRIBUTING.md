# Contributing to apod

Patches are welcome on Radicle (`rad:z2CjA38QDC7c5tgBkPV23UippSCjK`) or GitHub
(<https://github.com/fryorcraken/apod>). This file is for anyone changing apod, people and
agents alike.

## Where things are

| Path                     | What it is                                                                                                                                                                    |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `apod`                   | The launcher (bash): profiles, mounts, the pod's command line, `apod history`, `apod resume`.                                                                                 |
| `_apod`                  | zsh completion for `apod`.                                                                                                                                                    |
| `apod-broker`, `broker/` | The broker: `core.py` (sessions, roles, scope, approvals, admin commands), `api.py` (what extensions may import), `gitwire.py`, `tui.py`, `pod-shim` (the pod-side commands). |
| `broker/paths.py`        | Where apod's files are (the XDG layout, or the legacy `~/.agent-pod`): everything asks it, `apod` through `apod-broker paths --json`.                                         |
| `broker/profiles.py`     | Reads a profile (`apod-broker profile`), the mount guard, `apod migrate`.                                                                                                     |
| `extensions/`            | The shipped broker extensions, one file each.                                                                                                                                 |
| `roles.toml`             | The built-in roles.                                                                                                                                                           |
| `tests/test_broker.py`   | The broker's tests: a real broker, throwaway apod folders, stub programs; `paths.py`, the profile loader and `gitwire.py`'s pack reader.                                      |
| `tests/test_apod.py`     | The launcher's tests: a stub `podman` that records the command line, compared with `tests/golden/`, in both layouts.                                                          |
| `tools/mdtables.py`      | Aligns the Markdown tables.                                                                                                                                                   |
| `README.md`              | The pitch, install, quick start, concepts, and links to the rest.                                                                                                             |
| `docs/guides.md`         | How to set each thing up, one section per extension or feature.                                                                                                               |
| `docs/reference.md`      | Every command and option, `profile.toml` and `config.toml` key, where apod keeps its files, environment variable, and how requests are classified.                            |
| `EXTENSIONS.md`          | Writing a broker extension: tutorial, API reference, security rules.                                                                                                          |
| `SECURITY.md`            | The threat model, what apod guarantees, what it doesn't.                                                                                                                      |

## Checklist

Go through the rows that match your change before you commit. The docs are part of the change:
a feature that the guides describe wrongly is a bug.

| You changed                                                                       | Also update                                                                                                                                                                                                                                                                 |
| --------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| An `apod` option or subcommand                                                    | `usage` in `apod`, `_apod`, the commands table in `docs/reference.md`, and the README if it's in the quick start or concepts.                                                                                                                                               |
| A profile file (`env`, `mounts`, `image`, ...)                                    | The profile files table in `docs/reference.md`, and the guide that covers it.                                                                                                                                                                                               |
| An `apod broker` command                                                          | `USAGE` in `broker/core.py`, the broker commands in `_apod` and `docs/reference.md`.                                                                                                                                                                                        |
| An extension's behaviour: when it's on, what the pod gets, its actions or targets | Its section in `docs/guides.md` (check with `apod broker extensions NAME`, which prints it), "How requests are classified" in `docs/reference.md`, the table in `docs/guides.md#built-in-extensions`, its module docstring, and `SECURITY.md` if what a pod can do changed. |
| An extension's `profile.toml` (or `config.toml`) key                              | The `profile.toml` table in `docs/reference.md`, and its guide's example.                                                                                                                                                                                                   |
| A new extension                                                                   | `description` and `guide` on its provider, a section in `docs/guides.md`, a row in its built-in table, a line in the README's "Built-in extensions", and tests.                                                                                                             |
| A role, or an action's name                                                       | `roles.toml` (its header comment lists every action), the role table in its guide, and `docs/reference.md`. Renaming an action or extension: keep the old name working (the provider's `renamed_from`, or a kind's `old=`) and say so in the guide.                         |
| The extension API (`broker/api.py`, `Provider`, what `configure` gets)            | `EXTENSIONS.md`'s reference; bump `API_VERSION` if an existing extension could break.                                                                                                                                                                                       |
| Anything a pod can reach or do                                                    | `SECURITY.md`.                                                                                                                                                                                                                                                              |
| An environment variable (`APOD_*`)                                                | Its table in `docs/reference.md`.                                                                                                                                                                                                                                           |

Then:

- Search the docs for the old wording (`grep -rn OLD README.md SECURITY.md EXTENSIONS.md docs`):
  the same fact is often stated in more than one place.
- Check that links to headings (`#anchor`) still point somewhere.
- Align the tables, so the files read well without a Markdown renderer:
  `git ls-files '*.md' | xargs python3 tools/mdtables.py`.

## Testing

```sh
python3 tests/test_broker.py            # the broker and its extensions, about three minutes
python3 tests/test_apod.py              # the launcher: podman command lines, migrations, the wizard
KEEP=1 python3 tests/test_broker.py     # keep the temporary folders to look at the broker's logs
bash -n apod && shellcheck apod && zsh -n _apod
```

In an apod pod, run the suites with `env -u GIT_CONFIG_COUNT`: the pod's own git config would
make the tests' commits ask the broker to sign them. The legacy layout's tests put their sockets
under `tmp/`: if the checkout's path is too long for that, `APOD_TEST_TMP=/tmp/x` puts the
temporary folders there (the new layout's sockets are in a short runtime folder anyway).

Don't test with your real apod folders (`~/.config/apod`, `~/.local/share/apod`,
`~/.local/state/apod`, `$XDG_RUNTIME_DIR/apod`, or `~/.agent-pod`): they hold real tokens and
keys, and the runtime folder is your running broker's. Use throwaway ones under `tmp/`, set with
`APOD_CONFIG_DIR`, `APOD_DATA_DIR`, `APOD_STATE_DIR` and `APOD_RUNTIME_DIR` (a short one: unix
socket paths are limited to 108 bytes), or `APOD_ROOT` for the legacy layout, and, for `apod`
itself, a stub `podman` first on `PATH` that records its arguments. The tests do this: they set
their own, and drop yours (`APOD_ROOT`, `APOD_*_DIR`, `XDG_*_HOME`, `XDG_RUNTIME_DIR`). Changes to mounts, labels, ssh-agent, networking or images also need a check in a
real pod; say which commands you ran.

## Publishing

The repository is on Radicle and mirrored to GitHub. To push to both from a clone with both
remotes (`rad` and `origin`):

```sh
git config alias.sync '!b=$(git branch --show-current); git push rad "$b" && git push origin "$b"'
git sync
```
