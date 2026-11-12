# apod reference

Every command, file, setting and variable. For how to set things up, see the
[guides](guides.md); for writing broker extensions, [EXTENSIONS.md](../EXTENSIONS.md).

- [Commands](#commands)
- [Profile files](#profile-files)
- [broker.toml](#brokertoml)
- [roles.toml](#rolestoml)
- [Scope kinds](#scope-kinds)
- [How requests are classified](#how-requests-are-classified)
- [Environment variables](#environment-variables)
- [~/.agent-pod](#agent-pod)

## Commands

```
apod <profile> [options] [dir] [-- agent args]   run an agent in a pod, in dir (default: the current folder)
apod resume [--list|--latest|--claude] [dir]     resume a stopped Claude session started in dir
apod history                                     folders and profiles launched, most recent first
apod ps                                          running pods: profile, session, status, folder
apod profiles                                    profiles: broker role, GitHub token, Google login
apod token <profile> [--rm]                      set, replace or remove a profile's GitHub token
apod google-auth <profile>                       sign a profile in to Google for google-docs-mcp
apod selinux [install|remove|status]             SELinux labelling for broker and ssh-key pods
apod broker ...                                  the broker (apod gh ... is the same)
```

Options for `apod <profile>`:

| Option | |
|---|---|
| `-a claude\|opencode\|bash` | The agent (default `claude`); `bash` is a shell in the pod. |
| `-i`, `--image IMAGE` | The image, over the profile's `image` file and `APOD_IMAGE` (default `apod`). |
| `-c`, `--connectors LIST` | claude.ai connector groups for this run, comma-separated, repeatable; `none` turns them off. Replaces the profile's `connectors` file. |
| `--ro PATH` (`-m`), `--rw PATH` | Mount a file or folder for this run, read-only or read-write. |
| `--role ROLE` | The broker role for this run, over `broker.toml`'s `role`. |
| `--scope KIND:VALUE` | Add to the broker scope for this run; repeatable. |
| `--gh-user ACCOUNT` | The host `gh` account the broker acts as for this run, over `[gh] user`. |
| `--no-broker` | No broker for this run, even with a `broker.toml`. |
| `-- ARGS` | Anything after `--` goes to the agent (`-- --continue`). |

`--role`, `--scope` and `--gh-user` use the broker even without a `broker.toml`.

`apod broker` subcommands:

| Command | |
|---|---|
| `apod broker` (`ui`) | The approval TUI ([guide](guides.md#approving-requests)). |
| `log [-f] [-n N]` | Recent requests with their decisions; `-f` follows. |
| `pending` | Requests waiting for approval. |
| `allow ID [--session\|--role]` | Approve a request: once, for the session, or for its role. |
| `deny ID` | Refuse a waiting request. |
| `sessions` | Running sessions: profile, role, scope, approvals. |
| `roles` | Each role's allowed and denied actions. |
| `grants [rm ROLE ACTION]` | Approvals saved for roles; remove one. |
| `scopes` | The scope kinds the loaded extensions provide, and which file each extension loaded from. |
| `extensions [NAME]` | The loaded extensions, with a line each; with NAME, its guide, what each role allows of it (what a role adds to the roles it extends), what's never allowed, its scope kinds. |
| `keys` | The keys profiles and running pods use; loads the missing ones into the broker's agent. |
| `restart`, `stop` | Restart or stop the broker. Running pods' sessions come back on restart; reload keys with `keys`. |

## Profile files

Everything lives in `~/.agent-pod/<profile>/` (`APOD_ROOT` overrides `~/.agent-pod`):

| File | |
|---|---|
| `env` | Required. The git identity, and the only place it's set: `GIT_AUTHOR_NAME` and `GIT_AUTHOR_EMAIL` (`GIT_COMMITTER_*` default to them). apod sets `user.name` and `user.email` to match, over any `.gitconfig` in the pod. Other variables here go into the pod's environment. |
| `home/` | The pod's persistent `$HOME`: the agent's login, settings and history; Google login. Every pod of the profile shares it. |
| `broker.toml` | Use the broker: role, scope, GitHub account, keys ([below](#brokertoml)). |
| `mounts` | Extra mounts, one path per line, read-only unless prefixed `rw`. |
| `image` | The image (first line). |
| `network` | `host` to share the host's network. |
| `volumes` | Named podman volumes, `NAME /path` per line, e.g. `apod-myproj-nix /nix`. |
| `connectors` | Default claude.ai connector groups (`-c` replaces them). |
| `google-credentials.json` | OAuth client for google-docs-mcp, instead of the checkout's. |
| `github` | Without the broker: use another profile's GitHub token (`gh-<that profile>`). |
| `ssh-key` | Without the broker: path to a passphrase-protected private key, served from a per-run ssh-agent. |
| `ssh-confirm` | If present, confirm every use of that key on the host. |

## broker.toml

A profile's `~/.agent-pod/<profile>/broker.toml`, over `~/.agent-pod/broker.toml` (shared by
every profile; key by key within each table; `role` and `scope` are ignored there).

| Key | |
|---|---|
| `role` | The default role (`--role` overrides; default `reader`). |
| `scope` | Scope entries, `["gh:ORG/*", "ssh:devbox"]`, added to the project's remotes and `--scope`. |
| `[gh] user` | The host `gh` account the broker acts as. Required for `gh` and GitHub remotes. |
| `[git-sign] key` | The private key pods sign commits and tags with (its `.pub` next to it). |
| `[git-sign] confirm` | `true`: confirm each signature on the host (needs openssh-askpass). |
| `[git-sign] auto` | `false`: don't turn on `commit.gpgsign`/`tag.gpgsign`, even for roles that sign. |
| `[ssh] key`, `[ssh] keys` | The private key (or keys) pods log in with. |
| `[ssh] hosts` | Hosts in scope, as named in the host's ssh config: `NAME` (any user) or `USER@NAME`. |
| `[ssh] confirm` | `true`: confirm each login on the host. |
| `[ssh] proxy` | `false`: the pod connects to hosts itself instead of through the broker (default `true`). |
| `[NAME] enabled` | `false`: the extension NAME is off for the profile's pods, as if it weren't installed (`[rad] enabled = false`). |
| `[rad] home` | `RAD_HOME` of the identity pushes land as (default `$RAD_HOME` or `~/.radicle`). |
| `[rad] key` | The Radicle signing key (default `home/keys/radicle`). |
| `[rad] confirm` | `true`: confirm each use of the Radicle key. |
| `[rad] nid`, `[rad] storage` | The node id and storage folder, when `rad` can't tell (default: `rad self --nid`, `home/storage`). |
| `[notify] enabled` | `true`: pods may send desktop notifications (`apod-notify`). |
| `[notify] command` | The command, called with TITLE TEXT (default `["notify-send", "-a", "apod"]`). |
| `[notify] urgent` | Arguments added for `apod-notify --urgent` (default `["-u", "critical"]`). |

Extensions read their own `[NAME]` table.

## roles.toml

The built-in roles are in `roles.toml` in the apod checkout; `~/.agent-pod/roles.toml` adds to
them. A role there with a built-in's name adds its lists to the built-in one (its `description`
replaces it); a new name is a new role.

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

| Entry | Extension | Covers |
|---|---|---|
| `gh:OWNER/REPO`, `gh:OWNER/*` | gh, github | A repository: gh commands, API calls and git pushes and fetches. |
| `gh:OWNER` | gh | The owner itself: owner-level reads, its project list, creating repositories there. |
| `gh-project:OWNER/NUMBER`, `gh-project:OWNER/*` | gh | A project board. |
| `rad:RID` | rad | A Radicle repository. |
| `ssh:HOST`, `ssh:USER@HOST` | ssh | A host as named in the host's ssh config, for any user or that user. |
| `git-local:PATH` | sign | Signing in a repository with no GitHub or Radicle remote, by its folder. |

Values take shell-style patterns, case-insensitive. Automatically in scope: the project's GitHub
and Radicle remotes, the project folder and the repositories under it (`git-local:`), `[ssh]
hosts`, and forks and repositories the session creates. `apod broker scopes` lists every kind,
your extensions' included.

## How requests are classified

The broker turns each request into actions (what it does) and targets (what it touches).

- **gh** (`gh:` actions): commands by their full name, aliases resolved (`pr ls` is `pr list`);
  risky flags as their own actions (`pr merge --admin`). `gh api` by method and path, with the
  repository as `{repo}` and owners as `{owner}` (`api GET /repos/{repo}/pulls/*`). GraphQL
  (`gh api graphql` only) by operation and top-level field (`graphql mutation addComment`), with
  `repository`, `organization`/`user` arguments and node ids (looked up on GitHub) as targets; a
  query or mutation that can't be scoped that way waits for approval. Never allowed: `gh auth`,
  `config`, `extension`, `alias`, `ssh-key`, `gpg-key`, `codespace`, `browse`, `repo deploy-key
  add`, and GraphQL subscriptions; `repo set-default`, `run download`, `release download` and
  `gist clone` (they need the pod's files); `pr checkout` and `repo clone` run in the pod
  instead, through git. Links in titles and bodies aren't targets.
- **github** and **rad** (git): `fetch`, `fetch public` (a public GitHub repository outside the
  scope), and pushes by ref: `push branch`, `push default-branch`, `push tag`, `push delete`,
  `push force` (a branch update that isn't a fast-forward, on top of the ref's own action), and
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

## Environment variables

On the host:

| Variable | |
|---|---|
| `APOD_ROOT` | Where profiles and the broker's state live (default `~/.agent-pod`). |
| `APOD_IMAGE` | The default image (default `apod`). |
| `APOD_SSH_TTL` | How long keys stay in the broker's agent and per-run agents (default `4h`). |
| `APOD_BROKER_ASK_TIMEOUT` | Seconds a request waits for approval (default `90`; `0` rejects at once). |
| `APOD_BROKER_NOTIFY` | How the TUI notifies you: `osc9`, `osc99`, `osc777`, `bell`, `none`. |
| `APOD_PANE_ENV` | Variables that identify the terminal tab, for `apod resume` (default `HERDR_PANE_ID HC_SCOPE`). |
| `APOD_GDOCS_MCP` | The google-docs-mcp checkout (default `~/src/fryorcraken/google-docs-mcp`). |
| `APOD_GDOCS_SCOPES` | Its Google scopes (default `docs,sheets,slides,drive`). |
| `APOD_GH_BIN`, `APOD_GH_GIT_UPSTREAM`, `APOD_SSH_CONFIG` | For the tests: the `gh` binary, the git server and the ssh config the broker uses. |

In the pod: `APOD_PROFILE` (the profile), `APOD_BROKER_SOCK` (the broker's socket), and the
variables from the profile's `env`.

## ~/.agent-pod

| Path | |
|---|---|
| `<profile>/` | A profile ([files](#profile-files)). |
| `broker.toml` | Broker settings for every profile. |
| `roles.toml` | Your roles. |
| `broker-extensions/` | Your broker extensions ([EXTENSIONS.md](../EXTENSIONS.md)). |
| `broker/` | The broker's state: `log.jsonl` (every request), `broker.log` (its own log), `grants.json` (approvals for roles), `sessions/`, its sockets. |
| `history.jsonl` | Every launch, for `apod history` and `apod resume`; its `worklog` field names the launch's work log. |
| `worklog/<id>/worklog.md` | Each session's [work log](guides.md#work-logs), written by the agent. |
| `selinux` | Written by `apod selinux install`. |

Keep `~/.agent-pod` out of every pod's mounts: it holds the profiles' logins and the broker's state.
Its `worklog/` folder alone is fine, read-only, for a pod that reviews the logs.
