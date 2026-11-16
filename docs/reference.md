# apod reference

Every command, file, setting and variable. For how to set things up, see the
[guides](guides.md); for writing broker extensions, [EXTENSIONS.md](../EXTENSIONS.md).

- [Commands](#commands)
- [profile.toml](#profiletoml)
- [roles.toml](#rolestoml)
- [Scope kinds](#scope-kinds)
- [How requests are classified](#how-requests-are-classified)
- [Environment variables](#environment-variables)
- [Where apod keeps its files](#where-apod-keeps-its-files)

## Commands

```
apod <profile> [options] [dir] [-- agent args]   run an agent in a pod, in dir (default: the current folder)
apod new [profile]                               create a profile, step by step
apod migrate [profile | --all] [--yes]           old profile files to one profile.toml (the old files move to .old/)
apod resume [--list|--latest|--claude] [dir]     resume a stopped Claude session started in dir
apod history                                     folders and profiles launched, most recent first
apod ps                                          running pods: profile, session, status, folder
apod profiles                                    profiles: broker role, GitHub token, Google login
apod token <profile> [--rm]                      set, replace or remove a profile's GitHub token
apod google-auth <profile>                       sign a profile in to Google for google-docs-mcp
apod selinux [install|remove|status]             SELinux labelling for broker and ssh-agent pods
apod broker ...                                  the broker (apod gh ... is the same)
```

Options for `apod <profile>`:

| Option                          |                                                                                                                                                                                                      |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `-a claude\|opencode\|bash`     | The agent (default `claude`); `bash` is a shell in the pod.                                                                                                                                          |
| `-i`, `--image IMAGE`           | The image, over the profile's `[pod] image` and `APOD_IMAGE` (default `apod`).                                                                                                                       |
| `-c`, `--connectors LIST`       | claude.ai connector groups for this run, comma-separated, repeatable; `none` turns them off. Replaces the profile's `[pod] connectors`. Claude only: ignored, with a warning, for opencode and bash. |
| `--ro PATH` (`-m`), `--rw PATH` | Mount a file or folder for this run, read-only or read-write. Refused when it would expose apod's own files (see [Where apod keeps its files](#where-apod-keeps-its-files)).                         |
| `--role ROLE`                   | The broker role for this run, over `profile.toml`'s `role`.                                                                                                                                          |
| `--scope KIND:VALUE`            | Add to the broker scope for this run; repeatable.                                                                                                                                                    |
| `--gh-user ACCOUNT`             | The host `gh` account the broker acts as for this run, over `[gh] user`.                                                                                                                             |
| `--no-broker`                   | No broker for this run, even for a profile that uses it.                                                                                                                                             |
| `-- ARGS`                       | Anything after `--` goes to the agent (`-- --continue`).                                                                                                                                             |

`--role`, `--scope` and `--gh-user` use the broker even for a profile with `broker = false`.

`apod broker` subcommands:

| Command                        |                                                                                                                                                                                                                                                                               |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `apod broker` (`ui`)           | The approval TUI ([guide](guides.md#approving-requests)).                                                                                                                                                                                                                     |
| `log [-f] [-n N]`              | Recent requests with their decisions; `-f` follows.                                                                                                                                                                                                                           |
| `pending`                      | Requests waiting for approval.                                                                                                                                                                                                                                                |
| `allow ID [--session\|--role]` | Approve a request: once, for the session, or for its role. Once it's no longer waiting (refused or timed out), only `--session` or `--role`, so that the agent's retry goes through.                                                                                          |
| `deny ID`                      | Refuse a waiting request.                                                                                                                                                                                                                                                     |
| `sessions`                     | Running sessions: profile, role, scope, approvals.                                                                                                                                                                                                                            |
| `roles [NAME]`                 | What each role allows, by extension and command: what it adds to the roles it extends, approvals saved for it, its `deny`. With NAME, everything that role allows, inherited included. `--plain`: one line per role, tab-separated (name, description, actions), for scripts. |
| `grants [rm ROLE ACTION]`      | Approvals saved for roles; remove one.                                                                                                                                                                                                                                        |
| `scopes`                       | The scope kinds the loaded extensions provide, and which file each extension loaded from.                                                                                                                                                                                     |
| `extensions [NAME]`            | The loaded extensions, with a line each; with NAME, its guide, what each role allows of it (what a role adds to the roles it extends), what's never allowed, its scope kinds.                                                                                                 |
| `keys`                         | The keys profiles and running pods use; loads the missing ones into the broker's agent.                                                                                                                                                                                       |
| `restart`, `stop`              | Restart or stop the broker. Running pods' sessions come back on restart; reload keys with `keys`.                                                                                                                                                                             |

## profile.toml

A profile is a folder, `profiles/NAME/` in apod's [config folder](#where-apod-keeps-its-files)
(`~/.agent-pod/NAME/` in the legacy layout), and its settings are one file in it, `profile.toml`. Put the top-level keys first: a key written
after a `[table]` line belongs to that table (`scope` after `[gh]` would be `gh.scope`), so apod
refuses a `role`, `scope`, `allow`, `deny` or `broker` inside a table. Unknown keys in `[pod]`,
`[git]`, `[token]` and `[ssh-agent]` are errors too, and so are wrong types. `apod-broker profile
--profile NAME` prints what apod reads from a profile, as JSON, whichever format it is in.

```toml
broker = true                  # false: no broker, a GitHub token instead; default true
role   = "reader"
scope  = ["gh:fryorcraken/*", "gh-search:all"]
allow  = ["clipboard:image"]
deny   = []

[pod]
image = "apod"
network = "host"               # optional
ro = ["~/.agent-pod/worklog", "~/.agent-pod/history.jsonl"]
rw = ["~/reports/drafts"]
volumes = { "apod-myproj-nix" = "/nix" }
connectors = ["gdoc"]
[pod.env]
SOME_VAR = "value"

[git]                          # the identity the pod's commits have
name = "Agent"
email = "agent@example.com"

[gh]                           # an extension's table
user = "fryorcraken"
```

| Key                          |                                                                                                                                                                                                                                                                                                                                        |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `broker`                     | `false`: no broker for this profile's pods; they use a GitHub token (`[token]`) and can't use the broker's extensions. Default `true`. `--no-broker` does that for one run; `--role`, `--scope` or `--gh-user` turn the broker on for one run.                                                                                         |
| `role`                       | The default role (`--role` overrides; default `reader`).                                                                                                                                                                                                                                                                               |
| `allow`                      | Actions added to the role for this profile's pods, as `PROVIDER:ACTION` patterns: `["ssh:connect", "ssh:login"]`.                                                                                                                                                                                                                      |
| `deny`                       | Actions this profile's pods never get, whatever the role allows: `["gh:pr create"]`.                                                                                                                                                                                                                                                   |
| `scope`                      | Scope entries, `["gh:ORG/*", "ssh:devbox"]`, added to the project's remotes and `--scope`.                                                                                                                                                                                                                                             |
| `[pod] image`                | The image (`-i` overrides; else `APOD_IMAGE`, else `apod`).                                                                                                                                                                                                                                                                            |
| `[pod] network`              | `host` to share the host's network (Tailscale and DNS included, but also the host's localhost services); otherwise podman's default.                                                                                                                                                                                                   |
| `[pod] ro`, `[pod] rw`       | Extra mounts: files or folders, read-only or read-write, at the same path in the pod (`~` is your home). `--ro` and `--rw` override these for the same path. Paths that don't exist are skipped with a warning. Mounts that would expose apod's own files are refused (see [Where apod keeps its files](#where-apod-keeps-its-files)). |
| `[pod] volumes`              | Named podman volumes, `NAME = "/path in the pod"`, e.g. a persistent `/nix` store. Podman fills an empty volume from the image's content at that path.                                                                                                                                                                                 |
| `[pod] connectors`           | Default claude.ai connector groups (`-c` replaces them).                                                                                                                                                                                                                                                                               |
| `[pod.env]`                  | Environment variables for the pod, `NAME = "value"` (strings). Passed to podman as `-e` options, so the value is exactly what you wrote. Not the git identity: that's `[git]`.                                                                                                                                                         |
| `[git] name`, `[git] email`  | The git identity, e.g. for a bot; else your host's global git config (`user.name`, `user.email`). `committer_name` and `committer_email` default to them. apod sets `user.name` and `user.email` in the pod to match, over any `.gitconfig` there; a project's `.git/config` doesn't count, since pods can edit it.                    |
| `[token] profile`            | Without the broker: use another profile's GitHub token (the podman secret `gh-<that profile>`, `apod token`). Default: the profile's own.                                                                                                                                                                                              |
| `[ssh-agent] key`            | Path to a passphrase-protected private key, served to the pod from a per-run ssh-agent; the pod gets the agent's socket, never the key. Ignored when the broker's ssh extension is on for the pod (`[ssh]` and hosts in scope), which replaces it.                                                                                     |
| `[ssh-agent] confirm`        | `true`: confirm every use of that key on the host.                                                                                                                                                                                                                                                                                     |
| `[gh] user`                  | The host `gh` account the broker acts as. Required for `gh` and GitHub remotes.                                                                                                                                                                                                                                                        |
| `[git-sign] key`             | The private key pods sign commits and tags with (its `.pub` next to it).                                                                                                                                                                                                                                                               |
| `[git-sign] confirm`         | `true`: confirm each signature on the host (needs openssh-askpass).                                                                                                                                                                                                                                                                    |
| `[git-sign] auto`            | `false`: don't turn on `commit.gpgsign`/`tag.gpgsign`, even for roles that sign.                                                                                                                                                                                                                                                       |
| `[ssh] key`, `[ssh] keys`    | The private key (or keys) pods log in with. The ssh extension is on only for a pod with hosts in scope (`scope = ["ssh:NAME"]` or `--scope ssh:NAME`).                                                                                                                                                                                 |
| `[ssh] hosts`                | The old way to put hosts in scope, still read with a launch warning: write `scope = ["ssh:NAME"]` instead.                                                                                                                                                                                                                             |
| `[ssh] confirm`              | `true`: confirm each login on the host.                                                                                                                                                                                                                                                                                                |
| `[ssh] proxy`                | `false`: the pod connects to hosts itself instead of through the broker (default `true`).                                                                                                                                                                                                                                              |
| `[NAME] enabled`             | `false`: the extension NAME is off for the profile's pods, as if it weren't installed (`[rad] enabled = false`).                                                                                                                                                                                                                       |
| `[rad] autodetect`           | `true`: Radicle is on, and the project's `rad://` remotes join the scope. Off by default.                                                                                                                                                                                                                                              |
| `[rad] enabled`              | `true`: Radicle is on, for the `rad:RID` entries in the scope only. `false` wins over `autodetect`.                                                                                                                                                                                                                                    |
| `[rad] home`                 | `RAD_HOME` of the identity pushes land as (default `$RAD_HOME` or `~/.radicle`).                                                                                                                                                                                                                                                       |
| `[rad] key`                  | The Radicle signing key (default `home/keys/radicle`).                                                                                                                                                                                                                                                                                 |
| `[rad] confirm`              | `true`: confirm each use of the Radicle key.                                                                                                                                                                                                                                                                                           |
| `[rad] nid`, `[rad] storage` | The node id and storage folder, when `rad` can't tell (default: `rad self --nid`, `home/storage`).                                                                                                                                                                                                                                     |
| `[gh-api] enabled`           | `true`: `https://api.github.com` from the pod goes through the broker, checked and with the account's token ([guide](guides.md#github-api-from-the-pod)). Needs `[gh] user` and `openssl` on the host.                                                                                                                                 |
| `[clipboard] enabled`        | `true`: Ctrl+V in the pod pastes images from the host's clipboard, never its text ([guide](guides.md#pasting-images)). Needs `wl-clipboard` or `xclip` on the host.                                                                                                                                                                    |
| `[notify] enabled`           | `true`: pods may send desktop notifications (`apod-notify`).                                                                                                                                                                                                                                                                           |
| `[notify] command`           | The command, called with TITLE TEXT (default `["notify-send", "-a", "apod"]`).                                                                                                                                                                                                                                                         |
| `[notify] urgent`            | Arguments added for `apod-notify --urgent` (default `["-u", "critical"]`).                                                                                                                                                                                                                                                             |

Extensions read their own `[NAME]` table. The names `pod`, `git`, `token` and `ssh-agent` are the profile's
own, and `config.toml` in apod's config folder (`broker.toml` in the legacy layout) holds the extension tables shared by every profile (key by key within
each table; a profile's own value wins; it has no `role`, `scope`, `allow`, `deny` or `broker`).

Next to `profile.toml`, a profile folder has `google-credentials.json` (an OAuth client for google-docs-mcp,
instead of the checkout's). Its home, `profiles/NAME/home/` in the data folder (`home/` in the profile folder in
the legacy layout), is the pod's persistent `$HOME`: the agent's login, settings and history, and the Google
login; every pod of the profile shares it. Nothing of the profile folder is mounted in a pod (but
`google-credentials.json`, read-only, with google-docs-mcp), so a pod can't change its own `profile.toml`. A
folder with no settings file at all (a bare `mkdir ~/.config/apod/profiles/me`) launches without the broker,
as `broker = false` does.

### The old files

Profiles made before `profile.toml` have their settings in several files. They're still read, as
they always were, when there is no `profile.toml` (with a warning at launch); `apod migrate`
writes the `profile.toml` for them and moves the files to `.old/`. With both, `profile.toml` is
used and the old files are ignored, with a warning.

| File               | Now                                                                                                                 |
| ------------------ | ------------------------------------------------------------------------------------------------------------------- |
| `broker.toml`      | The top-level keys and the extension tables of `profile.toml`. Without it, `broker = false`.                        |
| `env`              | `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`, `GIT_COMMITTER_EMAIL`: `[git]`; the rest: `[pod.env]`. |
| `mounts`           | `[pod] ro` and `rw`: one path per line, read-only unless prefixed `rw`.                                             |
| `image`, `network` | `[pod] image`, `[pod] network` (first line).                                                                        |
| `volumes`          | `[pod] volumes`: `NAME /path` per line.                                                                             |
| `connectors`       | `[pod] connectors`.                                                                                                 |
| `github`           | `[token] profile`.                                                                                                  |
| `ssh-key`          | `[ssh-agent] key`.                                                                                                  |
| `ssh-confirm`      | `[ssh-agent] confirm = true`.                                                                                       |

## roles.toml

The built-in roles are in `roles.toml` in the apod checkout, and change when you update apod;
nothing is copied at install. `~/.agent-pod/roles.toml` adds to them:

- a role there with a built-in's name adds its lists to the built-in one (its `description`
  replaces it);
- `deny` removes actions, which is the only way to take one away from a built-in role;
- a new name is a new role.

```toml
[roles.NAME]
description = "one line, shown by apod broker roles"
extends = ["reader"]            # roles whose actions this one has too
gh = ["pr create", "api GET /repos/{repo}/*"]   # one list per provider: actions after "PROVIDER:"
github = ["push branch"]
allow = ["notify:*"]            # full PROVIDER:ACTION patterns
deny = ["gh:repo delete"]       # never allowed for this role, whatever else allows it
```

Entries are shell-style patterns (`*` matches anything, `/` included). A list named after a
provider that isn't loaded is ignored. The actions each shipped provider has are listed at the
top of [`roles.toml`](../roles.toml) and [below](#how-requests-are-classified).

## Scope kinds

| Entry                                           | Extension  | Covers                                                                                                                                                                    |
| ----------------------------------------------- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `gh:OWNER/REPO`, `gh:OWNER/*`                   | gh, github | A repository: gh commands, API calls and git pushes and fetches.                                                                                                          |
| `gh:OWNER`                                      | gh         | The owner itself: owner-level reads, its project list, creating repositories there.                                                                                       |
| `gh-project:OWNER/NUMBER`, `gh-project:OWNER/*` | gh         | A project board.                                                                                                                                                          |
| `gh-search:prs`, `gh-search:all`                | gh         | `gh search prs` (or issues, code, commits, repos) without `--repo` or `--owner`: across GitHub, as the account sees it, private repositories included. `all`: every kind. |
| `rad:RID`                                       | rad        | A Radicle repository.                                                                                                                                                     |
| `ssh:HOST`, `ssh:USER@HOST`                     | ssh        | A host as named in the host's ssh config: as the user that config gives it (its `User`, else your user name), or as USER.                                                 |
| `git-local:PATH`                                | git-sign   | Signing in a repository with no GitHub or Radicle remote, by its folder.                                                                                                  |

Values take shell-style patterns, case-insensitive. Automatically in scope, each when its
extension is on for the pod: the project's GitHub remotes (with `[gh] user`), its Radicle
remotes (with `[rad] autodetect`), the project folder, when it's a git repository, and the repositories under it (`git-local:`, with a `[git-sign]`
key), and forks and repositories the session creates. `apod broker scopes` lists every kind,
your extensions' included. Entries of a kind whose extension is off for the pod are dropped from its
scope.

## How requests are classified

The broker turns each request into actions (what it does) and targets (what it touches).

- **gh** (`gh:` actions): commands by their full name, aliases resolved (`pr ls` is `pr list`);
  risky flags as their own actions (`pr merge --admin`). `gh api` by method and path, with the
  repository as `{repo}` and owners as `{owner}` (`api GET /repos/{repo}/pulls/*`). GraphQL
  (`gh api graphql` only) by operation and top-level field (`graphql mutation addComment`), with
  `repository`, `organization`/`user` arguments and node ids (looked up on GitHub) as targets; a
  query or mutation that can't be scoped that way waits for approval. `gh search` targets its
  `--repo` and `--owner` values; without either, `gh-search:KIND` (`gh-search:prs`). Never allowed: `gh auth`,
  `config`, `extension`, `alias`, `ssh-key`, `gpg-key`, `codespace`, `browse`, `repo deploy-key
  add`, and GraphQL subscriptions; `repo set-default`, `run download`, `release download` and
  `gist clone` (they need the pod's files); `pr checkout` and `repo clone` run in the pod
  instead, through git. Links in titles and bodies aren't targets.
- **gh-api** (`gh:` actions, as for `gh api`): a request to `https://api.github.com` by method
  and path, or GraphQL by top-level field; shown as `METHOD https://api.github.com/... (direct)`.
  It never waits for approval: what the role and scope don't allow is refused at once.
- **github** and **rad** (git): `fetch`, `fetch public` (a public GitHub repository outside the
  scope), and pushes by ref: `push branch`, `push default-branch`, `push tag`, `push delete`,
  `push force` (a branch update that isn't a fast-forward, on top of the ref's own action),
  `push other-ref` (any other ref, such as `refs/notes/*`), and
  for Radicle `push patch` (`HEAD:refs/patches`, push options such as `-o patch.message=...`
  passed on). `--force-with-lease` and `--force` look the same to the broker: the lease is checked
  by git in the pod. For GitHub, the broker reads the commits in the push and asks GitHub about
  the ones it already has; a push it can't show to be a fast-forward counts as a force push.
- **git-sign**: `git-sign:commit`, `git-sign:tag`; `git-sign:as other` when the author, committer or tagger isn't
  the profile's identity; `git-sign:other` for anything that isn't a commit or tag. The target is the
  repository, by the remote the branch pushes to, else its folder (`git-local:`).
- **ssh**: `ssh:login` to the host whose host key the pod's ssh binds to the agent (OpenSSH
  8.9's `session-bind`, matched against the host's `known_hosts`), every hop of a forwarded agent
  included; `ssh:connect` for the connection through the broker, which follows the host's ssh
  config for the name (`HostName`, `Port`, and `User` unless the entry is `USER@NAME`);
  `ssh:connect port` for a port other than the host's ssh port.
- **notify**: `notify:send`, `notify:urgent`; no target.
- **clipboard**: `clipboard:image` to read an image from the host's clipboard; no target.
  Listing its image types isn't checked.

## Environment variables

On the host:

| Variable                                                                         |                                                                                                                 |
| -------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `APOD_CONFIG_DIR`, `APOD_DATA_DIR`, `APOD_STATE_DIR`                             | apod's folders, over the XDG variables ([where apod keeps its files](#where-apod-keeps-its-files)).             |
| `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`                             | Where apod's folders go when `APOD_*_DIR` isn't set (defaults `~/.config`, `~/.local/share`, `~/.local/state`). |
| `APOD_ROOT`                                                                      | The legacy layout, rooted at this folder (`~/.agent-pod` otherwise): everything in one folder.                  |
| `APOD_IMAGE`                                                                     | The default image (default `apod`).                                                                             |
| `APOD_SSH_TTL`                                                                   | How long keys stay in the broker's agent and per-run agents (default `4h`).                                     |
| `APOD_BROKER_ASK_TIMEOUT`                                                        | Seconds a request waits for approval (default `90`; `0` rejects at once).                                       |
| `APOD_BROKER_NOTIFY`                                                             | How the TUI notifies you: `osc9`, `osc99`, `osc777`, `bell`, `none`.                                            |
| `APOD_PANE_ENV`                                                                  | Variables that identify the terminal tab, for `apod resume` (default `HERDR_PANE_ID HC_SCOPE`).                 |
| `APOD_HERDR`                                                                     | `0`: don't show a herdr pane as blocked while a request of its pod waits for approval.                          |
| `APOD_GDOCS_MCP`                                                                 | The google-docs-mcp checkout (default `~/src/fryorcraken/google-docs-mcp`).                                     |
| `APOD_GDOCS_SCOPES`                                                              | Its Google scopes (default `docs,sheets,slides,drive`).                                                         |
| `APOD_GH_BIN`, `APOD_GH_GIT_UPSTREAM`, `APOD_GH_API_UPSTREAM`, `APOD_SSH_CONFIG` | For the tests: the `gh` binary, the git server, the API server (`gh-api`) and the ssh config the broker uses.   |

In the pod: `APOD_PROFILE` (the profile), `APOD_BROKER_SOCK` (the broker's socket), and the
variables from the profile's `[pod.env]`.

## Where apod keeps its files

apod's files are in three folders, after the XDG Base Directory specification (on macOS too):

| Folder                                                                  | What's in it                                                                                                                                                                                                                                                                                    |
| ----------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| config: `$APOD_CONFIG_DIR`, else `${XDG_CONFIG_HOME:-~/.config}/apod`   | What you write: `config.toml` (the extension tables shared by every profile), `roles.toml` (your roles), `extensions/` (your [broker extensions](../EXTENSIONS.md)), `profiles/NAME/` (a profile: [`profile.toml`](#profiletoml), `google-credentials.json`).                                   |
| data: `$APOD_DATA_DIR`, else `${XDG_DATA_HOME:-~/.local/share}/apod`    | What you'd back up: `profiles/NAME/home/` (each profile's home), `worklog/<id>/worklog.md` (each session's [work log](guides.md#work-logs)).                                                                                                                                                    |
| state: `$APOD_STATE_DIR`, else `${XDG_STATE_HOME:-~/.local/state}/apod` | What the tools write: `history.jsonl` (every launch, for `apod history` and `apod resume`), `broker/` (the broker's state: `log.jsonl`, every request; `broker.log`, its own log; `grants.json`, approvals for roles; `sessions/`; its sockets), `selinux` (written by `apod selinux install`). |

`apod-broker paths` prints where they are (`--json` for scripts).

**The legacy layout.** Installs from before this layout keep everything in one folder,
`~/.agent-pod`: profiles as folders in it (`<profile>/profile.toml`, `<profile>/home/`),
`broker.toml` (now `config.toml`), `roles.toml`, `broker-extensions/` (now `extensions/`),
`broker/`, `history.jsonl`, `worklog/`, `selinux`. apod uses it when none of the three folders
above exists and `~/.agent-pod` does, and says so at launch. `APOD_ROOT=DIR` means the legacy
layout rooted at `DIR`, whatever the other variables say.

apod refuses a mount (`[pod] ro`/`rw`, `--ro`, `--rw`) that is, contains or is inside one of
apod's folders (`~/.agent-pod` in the legacy layout) or the apod checkout, and a read-write mount
of your home folder: they hold the profiles' logins and settings and the broker's state. The
exceptions are read-only mounts of the work logs (or one of them), `history.jsonl` and the
broker's `log.jsonl`, for a pod that reviews them ([SECURITY.md](../SECURITY.md#the-container)).
The launch folder is not checked.
