# apod: agents in podman

`apod` runs coding agents (Claude Code, opencode, ...) in rootless podman containers. With the
host's [broker](docs/guides.md#the-broker), the pod holds no credentials, and three things
decide what it can do (see [Concepts](#concepts)):

- its **profile** is *who* acts: the git identity, the GitHub account and the keys;
- its **role** is *what* it may do: a preset list of actions, such as open a PR, push a branch,
  sign a commit or log in over ssh;
- its **scope** is *where*: the repositories, project boards and hosts those actions may touch.

Anything outside them waits for your approval. The agent runs with no permission prompts; the
container, not the agent, is the security boundary.

**Don't trust an agent to rein in your agent.**

## Why

I ran Claude Code in auto mode, where a classifier model judges each action before it runs, with
`permissions.blockReadsOutsideWorkingDirectories` on, so it can't read outside the folders I've
allowed. That works well until the agent starts scripting (pipes into loops, command
substitutions, variables, inline scripts): the checks can't tell whether a command is safe, so
they ask, and a session turns into death by a thousand approvals.

In auto mode, an agent decides what the session's agent may do. It can't always tell, so it
asks, and when it lets something through, it was a judgement call. Don't trust an agent to rein
in your agent.

apod moves the boundary instead. The agent runs in a rootless container that only has the
folder it works in and what the profile mounts, so it can run anything without asking, and what it
can't see it can't touch.

Then I wanted agents to do some GitHub work too: push branches, open PRs, comment on issues, move
cards on a project board. Handing a pod a token gives it everything that token can do, and
fine-grained tokens are tedious to make and still coarse (a repository, not "open PRs here but
don't merge"). So apod's [broker](docs/guides.md#the-broker) keeps credentials on the host and
acts for the pod: each pod gets a role (what it may do) and a scope (what it may touch), and
anything else waits for my approval in a TUI. The few prompts left are the ones worth reading.

The broker is software, not an agent: it checks rules, it doesn't make judgement calls.

The same pattern fits more than GitHub: Radicle pushes, signing commits with a key the pod never
sees, ssh to my hosts without a key per pod. So the broker is a small core (sessions, roles,
scope, approvals, the audit log) with each of these as an extension, and adding another is a
single Python file: see [EXTENSIONS.md](EXTENSIONS.md).

## Where it lives

- **Radicle (primary):** `rad clone rad:z2CjA38QDC7c5tgBkPV23UippSCjK`, published by `fryorgearo`
  (`did:key:z6MkowunyxpkcgCx2DdndJwebjcpk5pEnhaDE8JH3MbLnDBe`).
- **GitHub (mirror):** <https://github.com/fryorcraken/apod>

Both are kept in sync: every push goes to both (`git sync`, see [Contributing](#contributing)).
Patches are welcome on either.

## Install

On the host: rootless podman, bash, git, jq.

```sh
git clone https://github.com/fryorcraken/apod   # or: rad clone rad:z2CjA38QDC7c5tgBkPV23UippSCjK
podman build -t apod ./apod                     # the pods' image
ln -s "$PWD/apod/apod" ~/.local/bin/apod
```

The rest is needed only for what uses it:

| For                                                                              | On the host                                                                                                                                                          |
| -------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The [broker](docs/guides.md#the-broker) (GitHub, Radicle, signing, ssh for pods) | Python 3.11 or later, OpenSSH 8.9 or later (the pods' image has both)                                                                                                |
| GitHub through the broker (the `gh` and `github` extensions)                     | `gh`, logged in as the agent's GitHub account                                                                                                                        |
| Radicle through the broker (`rad`)                                               | `rad`, with an identity (`rad auth`)                                                                                                                                 |
| `apod google-auth` (Google Docs for Claude)                                      | `node`                                                                                                                                                               |
| zsh completion                                                                   | From the same folder: `ln -s "$PWD/apod/_apod" ~/.oh-my-zsh/custom/completions/_apod`, then `rm -f ~/.zcompdump*; exec zsh` (oh-my-zsh puts that folder on `$fpath`) |

## Quick start

A profile is a folder:

```sh
mkdir -p ~/.agent-pod/me
cd ~/src/some-repo
apod me                    # Claude Code in a pod; the first run asks you to log in to Claude once
```

The pod sees the folder you launch it in (read-write), the profile's home and the session's work
log, nothing else from the host, and holds no credentials: the agent commits as you (your
host's git `user.name` and `user.email`), but can't push or use GitHub. That's what the broker adds (next).

```sh
apod me -a opencode        # opencode instead
apod me -a bash            # a shell in the pod
apod me -- --resume        # anything after -- goes to the agent
apod me ~/src/other        # another folder than the current one
```

### Quick start with GitHub

The broker acts on GitHub for the pod, as a GitHub account for the agent (a bot, with write
access to the repositories it works on), logged in to the host's `gh`. Its token never enters
the pod.

```sh
gh auth login                         # as the bot; then gh auth switch back to your own account
mkdir -p ~/.agent-pod/bot
printf 'GIT_AUTHOR_NAME=my-bot\nGIT_AUTHOR_EMAIL=12345+my-bot@users.noreply.github.com\n' > ~/.agent-pod/bot/env
cat > ~/.agent-pod/bot/broker.toml <<'TOML'
role = "dev"                          # PRs, issues, branch pushes; anything else waits for you
[gh]
user = "my-bot"                       # the host gh login the broker acts as
TOML
cd ~/src/some-repo && apod bot        # gh and git push work in the pod, within the role and scope
```

```sh
apod broker                           # in another terminal: approve what's out of role or scope
```

The noreply email is under the bot's Settings → Emails. The folder's GitHub remote is in scope;
`apod bot --role pm` takes another role for one launch. The [GitHub guide](docs/guides.md#github)
has the details, and the other guides cover Radicle, signing, ssh and the rest.

### Permission prompts

`apod` passes some settings to the agents. By default, they run without permission prompts,
since the container is the boundary:

- **Claude Code** runs with `--permission-mode bypassPermissions`.
- **opencode** (2.x, `@opencode/cli`) runs with `--auto`, which approves every permission a
  config doesn't explicitly deny: a `"deny"` in the profile's `opencode.json` still holds. It also
  runs with `--standalone`, a private server per pod instead of opencode's background service,
  which pods of the same profile would otherwise share through their common home.

At startup, the agent is told what is mounted and what isn't, and, with the broker, its role
and scope.

## Concepts

A launch combines a **profile**, a **role**, a **scope** and a **path** into a **session**:
`apod <profile> [--role ROLE] [--scope SCOPE]... <path>`. The profile is *who*, the role is
*what*, the scope is *where*.

| Term          | What it is                                                                                                                                                                                                                                                                                                                             | Where it's set                                                                                                                                     |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Extension** | Something the broker can do for a pod: `gh`, git to GitHub or Radicle, commit signing, ssh. Each one names its **actions** (`gh:pr create`, `ssh:login`) and declares the **kinds** of target it handles (`gh:` for repositories, `gh-project:` for boards, `ssh:` for hosts). Roles list actions, scopes list targets of those kinds. | Shipped ones in `extensions/`; yours in `~/.agent-pod/broker-extensions/`. `apod broker extensions` lists them.                                    |
| **Profile**   | *Who* acts: the git identity (your host's, unless `env` sets another), the GitHub account (`[gh] user`), the keys it signs and logs in with, its home (agent login, history, settings), mounts, image and network. One per identity or kind of job.                                                                                    | `~/.agent-pod/<profile>/`                                                                                                                          |
| **Role**      | *What* a pod may do without asking: a preset list of actions, per extension (`gh` commands, git pushes, signing, ssh logins).                                                                                                                                                                                                          | Built in: `reader`, `dev`, `maintainer`, `pm`, `ops`; `~/.agent-pod/roles.toml` adds to them. A profile's `role =`, or `--role`; default `reader`. |
| **Scope**     | *Where* those actions may happen: targets written `KIND:VALUE`, such as `gh:OWNER/REPO`, `gh-project:OWNER/N`, `rad:RID`, `ssh:HOST`.                                                                                                                                                                                                  | The path's remotes and folder, automatically (when the extension for them is set up); the profile's `scope` and `[ssh] hosts`; `--scope`.          |
| **Path**      | The folder the agent works in: mounted read-write; its git remotes join the scope.                                                                                                                                                                                                                                                     | The folder argument, else the current folder.                                                                                                      |
| **Session**   | One running pod, registered with the broker, with what you approve "for the session". It ends with the pod.                                                                                                                                                                                                                            | Each launch.                                                                                                                                       |

The broker reads each request from a pod as **actions** (what it does) and **targets** (what it
touches). The role lists actions, the scope lists targets:

| In the pod                                 | Action                   | Target                                        |
| ------------------------------------------ | ------------------------ | --------------------------------------------- |
| `gh pr create` in a clone of `acme/widget` | `gh:pr create`           | `gh:acme/widget`                              |
| `git push rad HEAD:refs/patches`           | `rad:push patch`         | `rad:z2CjA…` (the repository)                 |
| `ssh devbox`                               | `ssh:login`              | `ssh:you@devbox` (in scope with `ssh:devbox`) |
| `gh api /rate_limit`                       | `gh:api GET /rate_limit` | none: it touches no repository                |

A target is written like a scope entry, `KIND:VALUE`: the kind says what sort of thing it is (a
GitHub repository, a project board, a host), the value which one. An action's prefix is its
extension's name; a target's kind is one its extension declared, and one extension can declare
several (`gh` declares `gh:` and `gh-project:`); `apod broker scopes` lists them. Roles
exist because there are too many actions to list one by one per profile: each built-in role
bundles the actions for a kind of job. The broker lets a request through when the role
allows every action and every target is in the scope (a request with no target needs only the
role); anything else waits for you. You can approve it once, for the session, or for the role
(every profile with that role, now and later), so prefer the session for one-offs.

## Built-in extensions

Everything the broker does for a pod is an extension, each with its own guide:

- **gh:** `gh` in the pod, run by the broker as a host `gh` account
  ([GitHub](docs/guides.md#github)).
- **github:** git fetch and push to github.com remotes, with that account's token.
- **rad:** git fetch and push to `rad://` remotes, as the host's Radicle identity
  ([Radicle](docs/guides.md#radicle)).
- **git-sign:** commit and tag signatures with a key the broker holds
  ([Signing commits](docs/guides.md#signing-commits)).
- **ssh:** logins to the hosts in scope with the broker's agent ([ssh](docs/guides.md#ssh)).
- **notify:** desktop notifications from the pod ([Notifications](docs/guides.md#notifications)).

The [overview](docs/guides.md#built-in-extensions) has what each needs to be set up, and how to
turn one off. On the host, `apod broker extensions NAME` shows an extension's guide and what
each role may do with it. Yours go in `~/.agent-pod/broker-extensions/`
([EXTENSIONS.md](EXTENSIONS.md)).

## Documentation

- **[Guides](docs/guides.md):** [the broker](docs/guides.md#the-broker),
  [GitHub](docs/guides.md#github), [Radicle](docs/guides.md#radicle),
  [signing commits](docs/guides.md#signing-commits), [ssh](docs/guides.md#ssh),
  [notifications](docs/guides.md#notifications),
  [roles and scope](docs/guides.md#roles-and-scope),
  [approving requests](docs/guides.md#approving-requests),
  [tokens instead of the broker](docs/guides.md#without-the-broker-tokens),
  [mounts, network and images](docs/guides.md#mounts-network-and-images),
  [SELinux](docs/guides.md#selinux),
  [claude.ai connectors and MCP servers](docs/guides.md#claudeai-connectors-and-mcp-servers),
  [work logs](docs/guides.md#work-logs),
  [history, resume and herdr](docs/guides.md#history-resume-and-herdr).
- **[Reference](docs/reference.md):** every command and option, profile file, `broker.toml`
  key, the `roles.toml` format, scope kinds, how requests are classified, environment variables.
- **[EXTENSIONS.md](EXTENSIONS.md):** writing a broker extension. Everything the broker does is
  one; yours go in `~/.agent-pod/broker-extensions/`, and can replace the shipped ones.
- **[SECURITY.md](SECURITY.md):** the threat model, what apod guarantees, and what it doesn't.

## Works with herdr

[herdr](https://herdr.dev) is a terminal multiplexer for coding agents, and apod is built for its
panes: herdr sees the agent inside the pod (working, blocked, idle), and after a herdr restart
`apod resume` in a restored pane goes back to that tab's session. See
[History, resume and herdr](docs/guides.md#history-resume-and-herdr).

## Security

[SECURITY.md](SECURITY.md) has the threat model, what apod guarantees and assumes, what it
doesn't protect against, a hardening checklist, and how to report a vulnerability. In short:

- The container is the boundary. The agent runs with no permission prompts and sees only the
  path (the folder it works in), the profile's home and the profile's mounts.
- With the broker, pods hold no token or key. Every GitHub, Radicle, signing and ssh request is
  checked against the role and scope, and anything else waits for your approval.
- Pods can still damage what's in scope, send what they can read over the network, and write
  files in that folder (`.git/hooks`, `.claude/settings.json`) that run when you use it on the
  host. Give pods their own worktrees, one per pod.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) has where things are, the checklist of docs to update with
each kind of change, and how to test. The repository is on Radicle and mirrored to GitHub. To
push to both from a clone with both remotes (`rad` and `origin`):

```sh
git config alias.sync '!b=$(git branch --show-current); git push rad "$b" && git push origin "$b"'
git sync
```

## Licence

Dual-licensed under MIT ([LICENSE-MIT](LICENSE-MIT)) or Apache-2.0
([LICENSE-APACHE-v2](LICENSE-APACHE-v2)), at your option.
