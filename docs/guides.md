# apod guides

How to set up each part of apod. The [README](../README.md) has the quick start and the
concepts (profile, role, scope); the [reference](reference.md) lists every command, file, key and
variable.

- [The broker](#the-broker)
- [GitHub](#github)
- [Radicle](#radicle)
- [Signing commits](#signing-commits)
- [SSH](#ssh)
- [Notifications](#notifications)
- [Roles and scope](#roles-and-scope)
- [Approving requests](#approving-requests)
- [Without the broker: tokens](#without-the-broker-tokens)
- [Mounts, network and images](#mounts-network-and-images)
- [SELinux](#selinux)
- [claude.ai connectors and MCP servers](#claudeai-connectors-and-mcp-servers)
- [Work logs](#work-logs)
- [History, resume and herdr](#history-resume-and-herdr)
- [Checking a setup in a real pod](#checking-a-setup-in-a-real-pod)

## The broker

The broker is the recommended way to give a pod GitHub, Radicle, signing and ssh. The pod holds
no credentials. A broker on the host, `apod-broker`, acts for it through a socket directory
mounted at `/run/apod-broker`, and checks every request against the pod's role (what it may do)
and scope (where). Anything else waits for your approval.

A pod uses the broker when its profile has a `broker.toml`, or when the launch passes `--role`,
`--scope` or `--gh-user`. `--no-broker` turns it off for a run. The broker starts with the first
pod that uses it and keeps running; its state is in `~/.agent-pod/broker/`.

### Built-in extensions

Everything the broker does for a pod is an extension. These ship with apod:

| Extension  | In the pod                                                                           | Set up with                                           |
| ---------- | ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| `gh`       | `gh` is a shim; the broker runs the real `gh` as the host account the profile names. | `[gh] user`: [GitHub](#github)                        |
| `github`   | git's github.com remotes go through the broker, with that account's token.           | `[gh] user`                                           |
| `rad`      | `rad://` remotes go through the broker, which pushes as the host's Radicle identity. | `[rad] autodetect` or `enabled`: [Radicle](#radicle)  |
| `git-sign` | git signs commits and tags with a key the broker holds.                              | `[git-sign] key`: [Signing commits](#signing-commits) |
| `ssh`      | `ssh HOST` logs in with the broker's agent, to the hosts in scope, through the host. | an `[ssh]` key, and hosts in scope: [SSH](#ssh)       |
| `notify`   | `apod-notify TEXT` sends you a desktop notification.                                 | `[notify] enabled`: [Notifications](#notifications)   |

**Loaded, then on per pod.** The broker loads every extension when it starts: each one
registers its actions, its kinds of target and its routes, and that's all. Whether it does
anything for a pod is decided at each launch, by the same rules for every extension:

1. It isn't turned off: `[NAME] enabled = false` in the profile's (or the global) `broker.toml`
   turns it off for that profile's pods.
2. It's set up: what its table needs is there (`[gh] user`, `[git-sign] key`, an `[ssh]` key,
   `[rad] autodetect`, `[notify] enabled`). Nothing is on just because a folder looks like it
   could use it.
3. It has something to act on: the pod's scope has a target of one of its kinds (a host for
   `ssh`, a Radicle repository for `rad`, a git repository as the folder for `git-sign`). `gh`
   is the exception, since it acts on what isn't known up front (`gh repo create`, `gh search`)
   and asks for anything out of scope.
4. Its keys are asked for at launch only when the role allows something that uses them: a
   `reader` isn't asked for the signing key or the Radicle key, nor a `dev` for the ssh key.
   If you approve such a request anyway, `apod broker keys` loads the key, then retry it.

At startup, the agent is told which extensions are off for its pod, and why.
What each one counts as an action and a target is under
[How requests are classified](reference.md#how-requests-are-classified). On the host, `apod
broker extensions` lists the extensions the broker loaded, yours included, and `apod broker
extensions NAME` shows one's guide (its section here), what each role allows of it, what's never
allowed, and its scope kinds. To write your own, or replace one, see
[EXTENSIONS.md](../EXTENSIONS.md).

A full profile `broker.toml` ([reference](reference.md#brokertoml) for every key):

```toml
# ~/.agent-pod/work/broker.toml
role = "dev"
scope = ["gh:fryorcraken/*", "gh-project:fryorcraken/1"]   # plus the project's GitHub and Radicle remotes

[gh]
user = "work-bot"                    # a host gh account (gh auth login); required for gh and GitHub remotes

[git-sign]
key = "~/.ssh/id_apod_broker_sign"

[ssh]
key = "~/.ssh/id_apod_broker"
hosts = ["devbox", "root@nas"]
```

```sh
apod work ~/src/org/repo                                        # as broker.toml says
apod work --role maintainer --scope 'gh:fryorcraken/*' ~/src/org/repo
apod broker                                                     # approve requests in a terminal UI
```

**Settings for every profile.** `~/.agent-pod/broker.toml` takes the same tables (`[ssh]`,
`[git-sign]`, `[rad]`, `[notify]`, your extensions'), for every profile that uses the broker. A
profile's `broker.toml` overrides them key by key within each table. `role` and `scope` are
ignored there, with a warning: what a pod may do comes only from its profile and its launch. The
global file alone doesn't switch a token profile to the broker.

```toml
# ~/.agent-pod/broker.toml
[ssh]
key = "~/.ssh/id_apod_broker"
[git-sign]
key = "~/.ssh/id_apod_broker_sign"
```

**Keys** (ssh, signing, and Radicle's) live in the broker's own ssh-agent, never in the pod. At
launch, apod loads the ones it doesn't hold yet, of the extensions on for the pod and only when
the role allows something that uses them, with `ssh-add`, which asks for their passphrases
(an empty line skips a key: the pod starts, and the requests that need it fail); they expire after `APOD_SSH_TTL` (default `4h`). After a broker restart, or before launching,
`apod broker keys` lists the keys named in the profiles' `broker.toml` tables (not those of
extensions turned off with `enabled = false`) and those running pods use, and loads the missing
ones. A Radicle key is listed only once a pod uses it, unless a `[rad]` table names its home.
One key can serve many hosts: the destination is checked per login.

## GitHub

The broker runs `gh` and git's github.com traffic as a host `gh` login. Use a separate GitHub
account for pods (a bot), so that what a pod does is attributed to it and limited to what that
account can do.

1. **The account.** Create it, and give it access to the repositories it works on (collaborator
   with write access for `dev`; `dev` can't fork, `maintainer` can). Its noreply email is under
   Settings → Emails, as `ID+NAME@users.noreply.github.com`.
2. **Log it in to the host's `gh`:**

   ```sh
   gh auth login --hostname github.com    # as the bot; this makes it gh's active account
   gh auth switch                          # back to your own account for your own gh use
   gh auth status                          # lists both
   ```

   The broker uses the account the profile names, whichever is active.
3. **The profile:** the account in `broker.toml`:

   ```toml
   # ~/.agent-pod/bot/broker.toml
   role = "dev"
   [gh]
   user = "my-bot"
   ```

   `--gh-user ACCOUNT` names another account for one launch. A profile that names none gets no
   `gh` and no GitHub remotes: the broker never falls back to your active login.
4. **Optional: commit as the bot.** Pods commit with your host's git identity (`git config
   --global user.name`, `user.email`). To have commits by the bot, override it in the profile's
   `env`, which passes any variables you put there to the pod:

   ```sh
   # ~/.agent-pod/bot/env
   GIT_AUTHOR_NAME=my-bot
   GIT_AUTHOR_EMAIL=12345+my-bot@users.noreply.github.com
   ```

5. **The scope.** The project's GitHub remotes are in scope automatically. Add more with
   `scope = ["gh:ORG/*"]` or `--scope gh:ORG/REPO` ([Roles and scope](#roles-and-scope)).
6. **Check it** in a pod (`apod bot -a bash ~/src/org/repo`): `gh auth status` shows the account,
   role, scope and what's allowed; then `gh pr list` and a push of a branch.

**In the pod.** GitHub ssh remotes, `github-*` host aliases from the host's `~/.ssh/config`
included, are rewritten to HTTPS, so they go through the broker. The broker runs `gh` with an
empty config and home of its own and no access to the pod's files: the shim sends the current
repository and branch and the contents of files a command names (`--body-file`, `-F key=@file`,
`--input`, release assets). Arguments that name host paths are refused. `gh repo clone` and
`gh pr checkout` run locally through git; `gh pr create --fill` isn't available (pass `--title`
and `--body`).

## Radicle

**Radicle is off until you turn it on.** In the profile's `broker.toml` (or
`~/.agent-pod/broker.toml`, for every profile):

```toml
[rad]
autodetect = true   # pods launched in a folder with a rad:// remote get that repository
# enabled = true    # instead: only the repositories you name in the scope (rad:RID)
```

With `autodetect`, launch a pod in a `rad clone` (or after `rad init`), and its `git fetch` and
`git push` to that remote go through the broker, with the repository in scope. With `enabled`,
only `rad:RID` entries (the profile's `scope`, or `--scope`) are in scope. A launch in a folder
with a `rad://` remote that Radicle isn't on for says so.

Pods fetch and push `rad://` remotes through the broker, which pushes as the host's Radicle
identity. The pod never holds the identity's key: it's loaded in the broker's own ssh-agent on
the host, whose socket isn't mounted in any pod (the ssh extension's agent proxy lists and signs
only the session's ssh keys). Nothing from the pod's repository runs on the host either: a push
lands in a staging repository the broker owns, its ref updates are checked against the role and
scope, then the broker pushes them on with the host's `git-remote-rad`.

There's no `rad` command in the pod, unlike `gh`: only `git fetch` and `git push` to `rad://`
remotes, and patches through `git push rad HEAD:refs/patches -o patch.message="..."`. `rad
patch`, `rad issue` and `rad sync` aren't available.

**When it's on.** For a pod whose profile has `[rad] autodetect` or `enabled`, when a Radicle
repository is in its scope (the folder's `rad://` remote with `autodetect`, or a `rad:RID`
entry), and the host has `rad` with an identity (`rad auth`) or `[rad] nid` is set. Otherwise
it's off, and the pod is told why.

**The key.** When it's on and the role may push (`dev`, `maintainer`), the launch asks for the
identity's key passphrase (the one from `rad auth`), unless the broker's agent already holds
it. Roles that only fetch aren't asked: fetching doesn't use the key. An empty line skips it:
the pod starts, and Radicle pushes fail. A loaded key stays for `APOD_SSH_TTL` (default `4h`).

**What the role allows:**

| Role                  | Radicle                                        |
| --------------------- | ---------------------------------------------- |
| `reader`, `pm`, `ops` | `fetch`                                        |
| `dev`                 | also `push branch`, `push force`, `push patch` |
| `maintainer`          | also `push tag`, `push delete`                 |

A push to the default branch (`push default-branch`) waits for you whatever the role.

**Which key.** Pushes are signed with the host's Radicle identity, `~/.radicle` (or
`$RAD_HOME`): your own key, so a pod's pushes and patches look like yours, and as a delegate of
the repository its pushes can be canonical (which is why the default branch waits). A dedicated
identity for pods should work but hasn't been tested:

```toml
[rad]
home = "~/.radicle-pods"    # created with RAD_HOME=~/.radicle-pods rad auth
# key = "..."               # the signing key (default: home/keys/radicle)
# confirm = true            # confirm each use of the key on the host (needs openssh-askpass)
# nid, storage              # the node id and storage folder, if rad can't tell
```

That identity has its own storage, so the repositories must be there too (`rad clone` or `rad
seed` with that `RAD_HOME`), and its own node, running, to publish its pushes. It isn't a
delegate, so its branches and patches show as another peer's.

<a id="turning-radicle-off"></a>**Turning it off.** Leave `autodetect` and `enabled` out. To turn
it off for one profile when the global `broker.toml` turns it on, in that profile's
`broker.toml`:

```toml
[rad]
enabled = false
```

Then pods of that profile get nothing from Radicle, even in a Radicle project: no passphrase at
launch, no Radicle repository in scope, and `git fetch` or `git push` to a `rad://` remote fail
in the pod (it has no `git-remote-rad`). Everything else is unchanged: a `dev` pod still pushes
branches and opens PRs on the project's GitHub remote. `enabled = false` works the same for any
extension's table (`[ssh]`, `[git-sign]`, ...).

**Check it** in a pod: `git fetch rad`, then `git push rad HEAD:refs/patches -o
patch.message="test"` opens a patch (`dev` may).

## Signing commits

With this set up, git in the pod signs commits and tags (`git commit -S`, or every commit for
roles that sign), and GitHub shows them as "Verified". The pod never holds the signing key: the
broker signs with it on the host, and only for the repositories in the pod's scope. This is the
`git-sign` extension (it was called `sign`; a `[sign]` table and `sign = [...]` role lists still
work, with a warning).

The profile's `broker.toml` says **which key signs**, in its `[git-sign]` table. **Whose commits
they are** is the pod's git identity: your host's (`git config --global user.name`,
`user.email`), unless the profile's `env` overrides it (`GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`).

A signature vouches for that email: the broker only signs commits whose author and committer
are the profile's (anything else waits for you), and GitHub only shows "Verified" when the email
belongs to the GitHub account that has the key.

### Prerequisites

1. **A GitHub account whose verified emails include the commits' email** (its Settings →
   Emails; the `…@users.noreply.github.com` address counts). It's the
   account the profile already uses with the broker for GitHub; see [GitHub](#github).
2. **A dedicated SSH key for pods' signatures.** Not your own key: a separate one means a
   "Verified" badge tells your commits apart from a pod's, and you can revoke it alone.

   ```sh
   ssh-keygen -t ed25519 -f ~/.ssh/id_apod_broker_sign -C "apod pods"
   ```

3. **Its public half on that GitHub account, as a signing key** (not an authentication key).
   With `gh` logged in as the bot (`gh auth switch` to it, then back):

   ```sh
   gh ssh-key add ~/.ssh/id_apod_broker_sign.pub --type signing --title "apod pods"
   ```

### Setup

1. **Optional: the identity.** Commits are by your host's git identity unless the profile's
   `env` says otherwise, as for a bot:

   ```sh
   # ~/.agent-pod/work/env
   GIT_AUTHOR_NAME=work-bot
   GIT_AUTHOR_EMAIL=12345+work-bot@users.noreply.github.com
   ```

2. **The key**, in the profile's `broker.toml` (or in `~/.agent-pod/broker.toml`, for every
   profile):

   ```toml
   [git-sign]
   key = "~/.ssh/id_apod_broker_sign"   # its .pub next to it
   confirm = true                       # optional: confirm each signature on the host (needs openssh-askpass)
   auto = false                         # optional: don't turn on commit.gpgsign, even for roles that sign
   ```

3. **Nothing else, usually.** The role and scope you already have decide the rest:
   - **Role:** `dev`, `maintainer` and `ops` sign every commit and tag. With `reader` or `pm`,
     `git commit -S` waits for your approval, and the launch doesn't ask for the key's
     passphrase: after approving, `apod broker keys` loads it. Your own roles sign with `git-sign = ["commit",
     "tag"]`.
   - **Scope:** a signature is for the repository the branch pushes to (`gh:OWNER/REPO` or
     `rad:RID`), so the scope you give for pushes covers signing too. The project folder and the
     repositories under it are in scope as `git-local:PATH`, for repositories with no remote,
     when the folder is a git repository. Launched elsewhere, a pod gets no signing.
   - **Waits for you:** another repository, or a commit whose author or committer isn't the
     profile's identity (`git-sign:as other`).

### Check it

The next launch with a role that signs loads the key into the broker's agent and asks for its
passphrase. Then, in a
pod:

```sh
git commit --allow-empty -m "signing test" && git log --show-signature -1   # Good "git" signature for the bot's email
```

`git log --show-signature` and `git verify-commit` work in the pod because the broker gives it an
allowed-signers file with the profile's key and emails. Push the commit and GitHub shows it as
"Verified".

The pod tells the broker which repository it's signing in, so the scope keeps an agent to its
work rather than stopping a hostile one; pushing the commit is checked separately
([SECURITY.md](../SECURITY.md)).

## SSH

> **Read this first.** Once a pod can log in to a host, it can run any command there, as that
> account: the broker decides *whether* it logs in, not what it does once logged in. On those
> hosts, roles and approvals no longer limit the damage a pod can do: only that account's own
> rights do. What the broker does limit is *where*: a pod logs in only to the hosts you
> configured (`[ssh] hosts`, or `--scope ssh:HOST`), as the users you allow, and any other host
> waits for your approval. Give pods an unprivileged account on each host, never root unless
> the job needs it, and only the hosts the job needs.

**Through the broker** (recommended): the pod's `SSH_AUTH_SOCK` is a filtering agent that signs
logins only to the hosts in scope, and connections go through the host, so the pod needs no
route to them.

1. **A key** for pods' logins, its public half in `~/.ssh/authorized_keys` on each host (a
   dedicated account there is best):

   ```sh
   ssh-keygen -t ed25519 -f ~/.ssh/id_apod_broker -C "apod pods"
   ssh-copy-id -i ~/.ssh/id_apod_broker.pub deploy@nas
   ```

2. **The hosts in the host's ssh config and `known_hosts`.** Pods name hosts as your `~/.ssh/config`
   does (`HostName`, `Port` and `User` come from there), and the broker checks the host key the
   login is bound to against your `known_hosts`. Connect once from the host so the key is known.
3. **The profile:**

   ```toml
   [ssh]
   key = "~/.ssh/id_apod_broker"   # or keys = [...]
   hosts = ["devbox", "deploy@nas"] # NAME: as your ssh config's User for it; USER@NAME: as USER
   # confirm = true                 # confirm each login on the host (needs openssh-askpass)
   # proxy = false                  # the pod connects itself (it needs a route to the hosts)
   ```

   **Which user.** `devbox` allows logging in only as the user your ssh config gives `devbox`:
   its `User`, or your user name on the host if it has none. With `Host devbox` / `User alice`,
   `ssh devbox` logs in as alice and goes through; `ssh root@devbox` waits for you. To allow
   another user, name it: `root@devbox` allows root (and only root). In the pod, `ssh devbox`
   logs in as the same user your ssh would.

   `--scope ssh:HOST` adds a host for one launch. Only `ops` logs in to the hosts in scope
   without asking, since a login is a shell there; with other roles, every login waits for you
   (or add `ssh = ["connect", "login"]` to a role in your `roles.toml`). Another host waits
   for you whatever the role.

   **No hosts, no ssh.** A pod with no hosts in scope (no `hosts` here, no `--scope ssh:`) gets
   no ssh from the broker, so an `[ssh]` key in `~/.agent-pod/broker.toml` costs nothing to the
   profiles that don't name hosts. The launch asks for the key's passphrase only when the role
   logs in without asking (`ops`).
4. **Check it** in a pod: `ssh devbox true` logs in; `ssh some-other-host true` waits.

Only port 22 or the host's configured ssh port goes through without asking; another port is a
tunnel (`ssh:connect port`). Once logged in, the agent can do anything that account can.

**Without the broker's ssh extension** (no broker, or no `[ssh]` key): a profile's `ssh-key` file names a passphrase-protected private key kept
outside the profile's home. apod starts an ssh-agent on the host for the run, asks for the
passphrase, and passes only the agent socket into the pod; the key expires after `APOD_SSH_TTL`
and the agent is killed when the pod exits. `ssh-confirm` (an empty file) confirms every use.
Host aliases go in the profile's `home/.ssh/config`. The pod can log in anywhere the key works.

## Notifications

`apod-notify [--urgent] TEXT` in a pod sends you a desktop notification on the host, titled
with the agent session's name.

```toml
# ~/.agent-pod/broker.toml (every profile) or a profile's broker.toml
[notify]
enabled = true
# command = ["notify-send", "-a", "apod"]   # the default; called with TITLE TEXT
# urgent = ["-u", "critical"]                # added for --urgent
```

No built-in role allows `notify:send`, so each notification waits for your approval until you
add it to a role:

```toml
# ~/.agent-pod/roles.toml: adds to the built-in dev role
[roles.dev]
notify = ["send", "urgent"]
```

Or approve the first one "for the role" in the TUI (`r`).

## Roles and scope

A role is a list of **actions**: what a pod may do without asking (`gh:pr create`,
`github:push branch`, `ssh:login`). There are far too many to pick one by one for each
profile, `gh` alone has dozens, so apod ships roles that bundle them by job, and a profile
names one. Pick the closest, then add or remove actions in your own `roles.toml` (below).

**Choosing a role.** `apod broker roles` lists what each allows, and `apod broker extensions
NAME` what each role allows of one extension.

| Role         | For                            | May                                                                                                                                                     |
| ------------ | ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reader`     | Reviewing, answering questions | View PRs, issues, CI and repositories; read the APIs for what's in scope; fetch.                                                                        |
| `dev`        | Coding                         | reader, plus push branches (force pushes too, not the default branch), open Radicle patches, create and edit PRs, comment, create issues; sign commits. |
| `maintainer` | Landing work                   | dev, plus merge and review PRs, edit and close issues, re-run CI, fork and create repositories, push tags, delete branches.                             |
| `pm`         | Project boards                 | reader, plus project boards (items, fields, templates) and creating, editing and commenting on issues.                                                  |
| `ops`        | Hosts                          | ssh to the hosts in scope, sign commits; fetch only.                                                                                                    |

A pod's role is `--role`, else its profile's `role =`, else `reader`. One profile can take
different roles per launch: `apod bot --role pm ~/src/org/planning`.

**Your own roles**, in `~/.agent-pod/roles.toml`. The built-in roles aren't copied there: they're
read from `roles.toml` in the apod checkout, so they change when you update apod. Don't edit that
file, add to it from yours ([format](reference.md#rolestoml)):

- a role with a built-in's name adds actions to it;
- `deny = [...]` takes actions away, from a built-in role or your own;
- a new name is a new role.


```toml
[roles.dev]                 # adds to the built-in dev
gh = ["pr merge"]           # dev pods may merge too

[roles.triage]              # a new role
description = "reader + label and close issues"
extends = ["reader"]
gh = ["issue edit", "issue close"]
deny = ["gh:issue delete"]
```

**Scope** is where the actions may happen: a list of **targets**, the things requests touch,
each written `KIND:VALUE`. The kind is what sort of thing it is (a GitHub repository, a Radicle
repository, a host), the value which one. Each kind belongs to an extension, and one extension
can declare several: `gh` declares `gh:` (repositories and owners) and `gh-project:` (project
boards). Values take shell-style patterns (`*`). The scope is built from the project's GitHub and Radicle remotes (and its folder, for signing),
the profile's `scope` and `[ssh] hosts`, and `--scope`. Forks and repositories the session
creates join it. The kinds:

| Entry                                           | Covers                                                                              |
| ----------------------------------------------- | ----------------------------------------------------------------------------------- |
| `gh:OWNER/REPO`, `gh:OWNER/*`                   | A repository: gh commands, API calls, git fetches and pushes.                       |
| `gh:OWNER`                                      | The owner itself: owner-level reads, its project list, creating repositories there. |
| `gh-project:OWNER/NUMBER`, `gh-project:OWNER/*` | A project board.                                                                    |
| `rad:RID`                                       | A Radicle repository.                                                               |
| `ssh:HOST`, `ssh:USER@HOST`                     | A host as named in your ssh config: as the user that config gives it, or as USER.   |
| `git-local:PATH`                                | Signing in a repository with no GitHub or Radicle remote, by its folder.            |

Extensions add kinds (yours too); `apod broker scopes` lists them all, with the extension each
belongs to. An entry of a kind no extension
provides is ignored, with a warning at launch. The older forms (`OWNER/REPO`, `OWNER`,
`project:OWNER/N`) still work, with a warning naming the new form.

Keep scope narrow: `gh:fryorcraken/*` covers every repository of that account.

## Approving requests

A request outside the role or the scope waits (`APOD_BROKER_ASK_TIMEOUT`, default 90 seconds; 0
rejects at once). It shows up in `apod broker` (a terminal UI), in `apod broker pending`, and as
a desktop notification. Allow it:

- **once**: this request;
- **for the session**: its actions and targets, until the pod exits;
- **for the role**: its actions, for every profile with that role, now and later (saved in
  `~/.agent-pod/broker/grants.json`; `apod broker grants` lists them, `grants rm ROLE ACTION`
  removes one). Prefer the session for one-offs.

A rejected request can be allowed after the fact, for the session or the role, and the agent
retries. From a shell: `apod broker allow ID [--session|--role]` (only `--session` or `--role`
once a request isn't waiting any more), `apod broker deny ID`. `apod broker log -f` follows
every request with its decision.

**The TUI.**

- **View 1** lists waiting requests (with the seconds left) above recent ones, and the selected
  request's actions, targets and reason below. `a` allows once, `s` for the session, `r` for the
  role, `d` denies.
- **View 2** lists sessions with their scope and grants; `x` removes one. **View 3** lists the
  role grants; `x` removes one.
- Tab or `1`–`3` switch views, `q` quits. The mouse works: click a tab, a row, or a footer button
  (each shows its key); the wheel scrolls.
- To select text, hold Shift while you drag, or press `m` to hand the mouse to the terminal (`m`
  again takes it back). `y` copies the selected request's details (OSC 52: kitty, foot, Ghostty,
  WezTerm, iTerm2, Alacritty, tmux with `set-clipboard on`; not VTE terminals such as GNOME
  Terminal).
- On a new waiting request it jumps to it, rings the bell and asks the terminal for a desktop
  notification: `OSC 9` for Ghostty, iTerm2 and WezTerm, `OSC 99` for kitty, `OSC 777` for foot,
  rxvt and VTE terminals, else just the bell. `APOD_BROKER_NOTIFY` (`osc9`, `osc99`, `osc777`,
  `bell`, `none`) overrides the guess, e.g. inside a multiplexer. The broker also calls
  `notify-send` itself when it's installed.

Requests and sessions show the agent session's name when it has one (Claude Code's `/rename`),
else the profile. Approve by the actions and targets listed, not by the one-line summary, which
the agent's arguments partly shape.

## Without the broker: tokens

A profile without a `broker.toml` can use a GitHub token instead: `apod token bot` stores a
fine-grained one as the podman secret `gh-bot` (prompted without echo), and `gh` and `git push` in
the pod have whatever it allows. A `github` file naming another profile uses that profile's token.
Without either, `gh` and HTTPS pushes are unauthenticated.

**Moving a profile from a token to the broker.** `apod profiles` shows each profile's token: `set`
(its own), `gh-OTHER` (another profile's), or `unused` once a `broker.toml` replaces it, and lists
tokens no profile launches with. For each profile:

1. Log the token's GitHub account in to the host's `gh`, once per account ([GitHub](#github)).
2. Add a `broker.toml` with a role, `[gh] user` and any extra scope. For `ssh-key` profiles, an
   `[ssh]` table with the key and `hosts` replaces `ssh-key`, `ssh-confirm` and, if that was only
   for ssh, `network`.
3. Relaunch, check `gh auth status` and a push in the pod, then remove what the broker made
   redundant: `apod token PROFILE --rm`, the `github` file, and the token itself on GitHub
   (Settings → Developer settings → Personal access tokens).

## Mounts, network and images

**Mounts.** Only the project folder is mounted (read-write), plus the profile's home and
`mounts` file, your `~/.claude/CLAUDE.md` and `~/.claude/skills` read-only, the session's
[work log](#work-logs) (`/run/apod-worklog`, read-write) and, with the broker, its folder
(`/run/apod-broker`) and the extensions' commands (`gh`, `apod-sign`, ...).

```
# ~/.agent-pod/work/mounts
~/src/shared-libs
rw ~/src/work/tmp
```

For one run, `--ro PATH` (`-m`) and `--rw PATH`. A command-line entry overrides the file's mode
for the same path; a missing command-line path is an error. The project folder stays writable
even inside a read-only entry. The first launch relabels mounted trees for SELinux (`:z`), which
can take a while on big trees. A running pod can't gain mounts: exit and relaunch with
`-- --continue`, or `apod resume`.

**Network.** Pods use podman's default network (pasta). On some hosts (seen on Wi-Fi) pasta
gives pods IPv6 only, so IPv4-only destinations such as Tailscale hosts are unreachable; a
`network` file containing `host` shares the host's network instead, its localhost services
included.

**Images.** `Containerfile` builds the base `apod` image (Fedora, Claude Code, opencode, gh, git,
Node, Python). A profile's `image` file, `-i IMAGE` or `APOD_IMAGE` picks another. Two examples
build on it:

- `Containerfile.logos-toolchain`: Rust 1.98.1 and RISC Zero (cargo-risczero/r0vm 3.0.5), for
  [logos-execution-zone](https://github.com/logos-blockchain/logos-execution-zone).
- `Containerfile.dialectica`: logos-toolchain plus Qt 6.8.3, Node 24, yq, Nix 2.35.2 and
  logos-scaffold 0.3.1. Pair it with a `volumes` entry for a persistent `/nix`.

```sh
podman build -t logos-toolchain -f ~/src/rad/apod/Containerfile.logos-toolchain ~/src/rad/apod
echo logos-toolchain > ~/.agent-pod/work/image
```

Tools live under `/opt` so the mounted home can't hide them. Rebuild derived images after
rebuilding `apod`.

## SELinux

Pods normally run with SELinux labelling (podman's `container_t`), and their mounts are relabelled
for it (`:z`). Pods that use a socket on the host, the broker's or a per-run ssh-agent's, are the
exception: SELinux stops a container from connecting to a socket of one of your own processes, so
by default these pods run with labelling off (`label=disable`, and their mounts aren't
relabelled).

`apod selinux install` lets them keep it. It loads a one-rule policy module,
[`selinux/apod.cil`](../selinux/apod.cil), with sudo: containers (`container_t`) may connect to
unix sockets of unconfined processes. These pods then run as ordinary containers with relabelled
mounts, and the broker and per-run ssh-agents run at the plain `s0` level. That's what limits the
rule to apod's sockets: a container can only reach a socket file mounted into it and relabelled
(apod does that for its own sockets only), served by a process at a level its categories
dominate, which `s0` is and your login session's processes (`s0-s0:c0.c1023`) aren't.

```sh
apod selinux install       # sudo semodule -i ...; records it in ~/.agent-pod/selinux
apod broker restart        # the broker now runs at s0
apod selinux status        # or remove, to go back to label=disable
```

If a pod can't reach the broker after this, SELinux has denied something the module doesn't
allow yet: `sudo ausearch -m avc -ts recent` shows what, and `apod selinux remove` goes back to
running these pods with labelling off.

## claude.ai connectors and MCP servers

claude.ai connectors (Notion, Google Docs, …) come with the Claude account and act as you, so apod
turns them off unless enabled for the run with `-c` (comma-separated, repeatable) or the profile's
`connectors` file; `-c none` turns them off. This, and the local MCP servers below, apply to
Claude only: opencode and bash ignore `-c`, with a warning.

Groups: `notion`, `gdoc` (Docs, Drive, Sheets), `todoist`, `canva`, `lovable`, `utsjitsu`,
`claudedocs`. Other known connectors are denied, and a PreToolUse hook blocks any claude.ai
connector tool that isn't enabled, including ones added to the account later.

`-c google-docs-mcp` runs a local
[google-docs-mcp](https://github.com/fryorcraken/google-docs-mcp) checkout
(`APOD_GDOCS_MCP`, default `~/src/fryorcraken/google-docs-mcp`) in the pod from a read-only mount,
with scopes `docs,sheets,slides,drive` (`APOD_GDOCS_SCOPES`) and lazy tools. Each profile signs in
once on the host with `apod google-auth <profile>`. It can't be combined with `gdoc`.

## Work logs

Each session keeps a work log on the host, `~/.agent-pod/worklog/<id>/worklog.md`, mounted in
the pod at `/run/apod-worklog/worklog.md`. Claude is told to add to it after each meaningful
step: a dated heading (`## 2026-11-11`), then what it did, decided or found, what's blocked and
what's next, with links to PRs, issues and commits. The logs outlive their pods, so an agent can
review a week's work across every session, for a status update for example.

The id is the Claude session id, so `apod resume` carries on with the same log; other launches
(`--continue`, opencode, bash) start a new one. The `worklog` field of each launch in
`history.jsonl` names its log, with the profile, folder and time it was started.

Treat the logs as the agent's account, not as a record: the pod can write anything in them. The
broker's `log.jsonl` is the record of what it actually did on GitHub, Radicle and ssh. And
since a pod writes them, be careful about handing them to an agent on the host that has more
access than the pods did: run the review in a pod, with the logs mounted read-only
(`apod reviewer --ro ~/.agent-pod/worklog ~/reports`), rather than with your own `claude`.
opencode isn't told about its log yet (only Claude gets apod's notes at startup).

## History, resume and herdr

Every launch is recorded in `~/.agent-pod/history.jsonl`: profile, folder, options, Claude session
id and terminal tab. Each new Claude run gets its own session id, and pods are labelled with
profile, folder and session. `apod history` lists folders and profiles, most recent first.

`apod resume` offers only sessions that aren't running and have a saved conversation, and
relaunches with the same profile and options. If the current tab launched one of them, it resumes
that one; otherwise it lists them and asks. The tab id is the first set variable among
`$APOD_PANE_ENV` (default `HERDR_PANE_ID HC_SCOPE`). `--list` always asks, `--latest` takes the
newest, and `--claude` opens Claude's own picker.

[herdr](https://herdr.dev) is a terminal multiplexer for coding agents, and apod is built to run
in its panes:

- **Agent detection.** In a pane herdr sees `podman`, not the agent inside the container. apod sets
  `HERDR_AGENT=claude` (or `opencode`) on the podman process, so herdr reads the pane as that agent
  and shows its working, blocked and idle states. The variable stays on the host.
- **Recovery after a herdr restart or crash.** herdr restores container panes as plain shells in
  the right folder. Run `apod resume` there: apod recorded the tab each session was launched from
  (`HERDR_PANE_ID`, or `HC_SCOPE`, which survives a herdr server restart), so it goes straight back
  to that tab's session.
- **Many agents side by side.** Claude's agent view and background sessions are off inside pods (a
  pod lives only as long as its main process). Run several pods in herdr panes instead.

## Checking a setup in a real pod

```sh
apod work -a bash ~/src/org/repo
gh auth status; gh pr list; git fetch; git push origin HEAD:refs/heads/apod-broker-test
git commit --allow-empty -m test && git log --show-signature -1
ssh devbox true; ssh some-other-host true          # the second one waits: apod broker
git push rad HEAD:refs/patches -o patch.message="broker test"
gh pr merge 1                                      # waits: approve or deny in apod broker
```

The broker's own tests (`python3 tests/test_broker.py`) use a fake `gh`, a local git server, a
stand-in for Radicle storage and real ssh-agents.
