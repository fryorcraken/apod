# apod guides

How to set up each part of apod. The [README](../README.md) has the quick start and the
concepts (profile, role, scope); the [reference](reference.md) lists every command, file, key and
variable.

- [The broker](#the-broker)
- [GitHub](#github)
- [GitHub API from the pod](#github-api-from-the-pod)
- [Radicle](#radicle)
- [Signing commits](#signing-commits)
- [SSH](#ssh)
- [Notifications](#notifications)
- [Pasting images](#pasting-images)
- [Roles and scope](#roles-and-scope)
- [Approving requests](#approving-requests)
- [Without the broker: tokens](#without-the-broker-tokens)
- [Migrating to profile.toml](#migrating-to-profiletoml)
- [Migrating to the new layout](#migrating-to-the-new-layout)
- [Mounts, network and images](#mounts-network-and-images)
- [A clone for worktree flows](#a-clone-for-worktree-flows)
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

A pod uses the broker when its profile says so (`broker = true` in `profile.toml`, the default there; `broker = false` for a profile that uses a GitHub token instead), or when the launch passes `--role`,
`--scope` or `--gh-user`. `--no-broker` turns it off for a run. The broker starts with the first
pod that uses it and keeps running; its state is in `~/.local/state/apod/broker/`, its sockets in
`$XDG_RUNTIME_DIR/apod/` ([where](reference.md#where-apod-keeps-its-files)). A broker started
before you update apod runs the old code until `apod broker restart`.

`apod new` writes a profile for you: it asks for the role first, then the GitHub account, the
git identity (offering the account's noreply address), a signing key (or creates one), ssh
hosts, Radicle, notifications, pasting images, more scope, extra mounts and an image, offering to
add to the role what it lacks for what you set up (`allow`, for that profile only), shows the
`profile.toml` it would write, and writes nothing until you confirm. The guides below say what each part does;
edit the file afterwards for the rest.

### Built-in extensions

Everything the broker does for a pod is an extension. These ship with apod:

| Extension   | In the pod                                                                           | Set up with                                                             |
| ----------- | ------------------------------------------------------------------------------------ | ----------------------------------------------------------------------- |
| `gh`        | `gh` is a shim; the broker runs the real `gh` as the host account the profile names. | `[gh] user`: [GitHub](#github)                                          |
| `github`    | git's github.com remotes go through the broker, with that account's token.           | `[gh] user`                                                             |
| `rad`       | `rad://` remotes go through the broker, which pushes as the host's Radicle identity. | `[rad] autodetect` or `enabled`: [Radicle](#radicle)                    |
| `git-sign`  | git signs commits and tags with a key the broker holds.                              | `[git-sign] key`: [Signing commits](#signing-commits)                   |
| `ssh`       | `ssh HOST` logs in with the broker's agent, to the hosts in scope, through the host. | an `[ssh]` key, and hosts in scope: [SSH](#ssh)                         |
| `notify`    | `apod-notify TEXT` sends you a desktop notification.                                 | `[notify] enabled`: [Notifications](#notifications)                     |
| `gh-api`    | `https://api.github.com` goes through the broker (Claude Code's PR status).          | `[gh-api] enabled`: [GitHub API from the pod](#github-api-from-the-pod) |
| `clipboard` | Ctrl+V pastes images from the host's clipboard (never its text).                     | `[clipboard] enabled`: [Pasting images](#pasting-images)                |

**Loaded, then on per pod.** The broker loads every extension when it starts: each one
registers its actions, its kinds of target and its routes, and that's all. Whether it does
anything for a pod is decided at each launch, by the same rules for every extension:

1. It isn't turned off: `[NAME] enabled = false` in the profile's `profile.toml` (or the global `config.toml`)
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

A full `profile.toml` ([reference](reference.md#profiletoml) for every key):

```toml
# ~/.config/apod/profiles/work.toml
role = "dev"
scope = ["gh:fryorcraken/*", "gh-project:fryorcraken/1"]   # plus the project's GitHub and Radicle remotes

[pod]
image = "apod"
ro = ["~/src/shared-libs"]

[git]                                # the identity of its commits (default: yours)
name = "work-bot"
email = "12345+work-bot@users.noreply.github.com"

[gh]
user = "work-bot"                    # a host gh account (gh auth login); required for gh and GitHub remotes

[git-sign]
key = "~/.ssh/id_apod_broker_sign"

[ssh]
key = "~/.ssh/id_apod_broker"
```

with the hosts in its `scope`, next to the rest: `scope = ["gh:fryorcraken/*", "ssh:devbox", "ssh:root@nas"]`
(before the first table).

```sh
apod work ~/src/org/repo                                        # as profile.toml says
apod work --role maintainer --scope 'gh:fryorcraken/*' ~/src/org/repo
apod broker                                                     # approve requests in a terminal UI
```

**Settings for every profile.** `~/.config/apod/config.toml` takes the same tables (`[ssh]`,
`[git-sign]`, `[rad]`, `[notify]`, your extensions'), for every profile that uses the broker. A
profile's `profile.toml` overrides them key by key within each table. `role` and `scope` are
ignored there, with a warning: what a pod may do comes only from its profile and its launch. The
global file alone doesn't switch a token profile to the broker.

```toml
# ~/.config/apod/config.toml
[ssh]
key = "~/.ssh/id_apod_broker"
[git-sign]
key = "~/.ssh/id_apod_broker_sign"
```

**Keys** (ssh, signing, and Radicle's) live in the broker's own ssh-agent, never in the pod. At
launch, apod loads the ones it doesn't hold yet, of the extensions on for the pod and only when
the role allows something that uses them, with `ssh-add`, which asks for their passphrases
(an empty line skips a key: the pod starts, and the requests that need it fail); they expire after `APOD_SSH_TTL` (default `4h`). After a broker restart, or before launching,
`apod broker keys` lists the keys named in the profiles' `profile.toml` tables (not those of
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
3. **The profile:** the account in `profile.toml`:

   ```toml
   # ~/.config/apod/profiles/bot.toml
   role = "dev"
   [gh]
   user = "my-bot"
   ```

   `--gh-user ACCOUNT` names another account for one launch. A profile that names none gets no
   `gh` and no GitHub remotes: the broker never falls back to your active login.
4. **Optional: commit as the bot.** Pods commit with your host's git identity (`git config
   --global user.name`, `user.email`). To have commits by the bot, set `[git]` in the profile's
   `profile.toml`, after the top-level keys:

   ```toml
   # ~/.config/apod/profiles/bot.toml
   [git]
   name = "my-bot"
   email = "12345+my-bot@users.noreply.github.com"
   ```

5. **The scope.** The project's GitHub remotes are in scope automatically. Add more with
   `scope = ["gh:ORG/*"]` or `--scope gh:ORG/REPO` ([Roles and scope](#roles-and-scope)).
   Outside the scope, a fetch of a public repository goes through (`github:fetch public`), one
   of a private repository the account can see waits for you, and one GitHub answers 404 for
   (a wrong name, or a repository the account can't see) fails at once.
6. **Check it** in a pod (`apod bot -a bash ~/src/org/repo`): `gh auth status` shows the account,
   role, scope and what's allowed; then `gh pr list` and a push of a branch.

**In the pod.** GitHub ssh remotes, `github-*` host aliases from the host's `~/.ssh/config`
included, are rewritten to HTTPS, so they go through the broker. The broker runs `gh` with an
empty config and home of its own and no access to the pod's files: the shim sends the current
repository and branch and the contents of files a command names (`--body-file`, `-F key=@file`,
`--input`, release assets). Arguments that name host paths are refused. `gh repo clone` and
`gh pr checkout` run locally through git; `gh pr create --fill` isn't available (pass `--title`
and `--body`).

## GitHub API from the pod

Some tools call GitHub's API themselves, with the token `gh auth token` gives them, rather than
through `gh`. Claude Code's PR status (the PR number, link, checks and review state under the
prompt) is one: in a pod, where there's no token, it stays empty. The `gh-api` extension sends
those calls through the broker, which checks them and adds the token. It's off unless you turn it
on, and needs [GitHub](#github) set up:

```toml
# ~/.config/apod/profiles/bot.toml
[gh]
user = "my-bot"
[gh-api]
enabled = true
```

**How it works.**

- In the pod, `api.github.com` resolves to `127.0.0.1`, where a relay (`apod-gh-api-relay`,
  started before the agent) passes each connection to the broker.
- The broker ends TLS with a certificate for `api.github.com` from a CA of its own. The CA is
  made once, with `openssl`, in `~/.local/state/apod/broker/gh-api/`. Its key never leaves the host,
  and it can only vouch for `api.github.com` (a name constraint). The pod trusts it through
  `NODE_EXTRA_CA_CERTS`, which Claude Code, Node and Bun read; `curl` needs
  `--cacert /run/apod-gh-api/ca.pem`.
- Each request is checked as `gh api` would be: REST by method and path
  (`gh:api GET /repos/{repo}/pulls`), GraphQL by top-level field, against the role and the scope.
  What isn't allowed outright is refused at once, with a 403: these requests come from tools on
  their own, with nobody there to approve them, so they never wait.
- What's allowed goes to GitHub with the profile's account's token. `gh auth token` in the pod
  prints a placeholder that works only through the broker, so the token never enters the pod.
- They're logged like any request, marked `(direct)`: `apod broker log` shows them. Claude
  Code's PR status polls, so expect a few lines a minute while a branch has a PR.

**Limits.** It's off with `[pod] network = "host"`: the pod then shares the host's network, where
`api.github.com` can't point at the pod's relay. Only `api.github.com` is covered; `github.com`
itself (git, web pages) goes the usual ways. The `reader` role's REST and GraphQL reads cover
Claude Code's PR status, for the repositories in scope.

**Check it** after a launch: `apod broker log` shows `GET https://api.github.com/repos/...
(direct)` lines once Claude Code is on a branch with a PR. In the pod (`apod bot -a bash`):

```sh
gh auth token                                    # apod-gh-api-placeholder-token
curl -s --cacert /run/apod-gh-api/ca.pem https://api.github.com/repos/OWNER/REPO | head
```

## Radicle

**Radicle is off until you turn it on.** In the profile's `profile.toml` (or
`~/.config/apod/config.toml`, for every profile):

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
it off for one profile when the global `config.toml` turns it on, in that profile's
`profile.toml`:

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

The profile's `profile.toml` says **which key signs**, in its `[git-sign]` table. **Whose commits
they are** is the pod's git identity: your host's (`git config --global user.name`,
`user.email`), unless the profile's `[git]` overrides it (`name`, `email`).

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
   `[git]` says otherwise, as for a bot:

   ```toml
   # ~/.config/apod/profiles/work.toml
   [git]
   name = "work-bot"
   email = "12345+work-bot@users.noreply.github.com"
   ```

2. **The key**, in the profile's `profile.toml` (or in `~/.config/apod/config.toml`, for every
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
     passphrase: after approving, `apod broker keys` loads it. Your own roles sign with
     `git-sign = ["commit", "tag"]`.
   - **Scope:** a signature is for the repository the branch pushes to (`gh:OWNER/REPO` or
     `rad:RID`), so the scope you give for pushes covers signing too. The project folder and the
     repositories under it are in scope as `git-local:PATH`, for repositories with no remote,
     when the folder is a git repository. Launched elsewhere, a pod gets no signing.
   - **Waits for you:** another repository, or a commit whose author or committer isn't the
     profile's identity (`git-sign:as other`).

**Example.** A `dev` profile with a `[git-sign] key`, launched in a clone of
`github.com/acme/widget`:

```sh
cd ~/src/widget && apod work   # scope: gh:acme/widget (its remote), git-local:~/src/widget and below
```

| In the pod, `git commit` in                               | The broker checks                             | Result                                         |
| --------------------------------------------------------- | --------------------------------------------- | ---------------------------------------------- |
| `~/src/widget`                                            | the target `gh:acme/widget` (where it pushes) | signed                                         |
| `~/src/widget/scratch`, a `git init` with no remote       | the target `git-local:~/src/widget/scratch`   | signed                                         |
| `~/src/widget/other`, a clone of `acme/other`             | the target `gh:acme/other`, out of scope      | waits (or launch with `--scope gh:acme/other`) |
| `~/src/widget`, with `--author "Someone <s@example.com>"` | the action `git-sign:as other`, not in `dev`  | waits                                          |

### Check it

The next launch with a role that signs loads the key into the broker's agent and asks for its
passphrase. Then, in a pod:

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
> put in scope (`scope = ["ssh:HOST"]`, or `--scope ssh:HOST`), as the users you allow, and any other host
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
   scope = ["ssh:devbox", "ssh:deploy@nas"]  # NAME: as your ssh config's User for it; USER@NAME: as USER

   [ssh]
   key = "~/.ssh/id_apod_broker"   # or keys = [...]
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

   **No hosts, no ssh.** A pod with no hosts in scope (no `ssh:` entry in its `scope`, no `--scope ssh:`) gets
   no ssh from the broker, so an `[ssh]` key in `~/.config/apod/config.toml` costs nothing to the
   profiles that don't name hosts. The launch asks for the key's passphrase only when the role
   logs in without asking (`ops`).
4. **Check it** in a pod: `ssh devbox true` logs in; `ssh some-other-host true` waits.

Only port 22 or the host's configured ssh port goes through without asking; another port is a
tunnel (`ssh:connect port`). Once logged in, the agent can do anything that account can.

**Without the broker's ssh extension** (no broker, or no `[ssh]` key): a profile's `[ssh-agent]` `key` names a passphrase-protected private key kept
outside the profile's home. apod starts an ssh-agent on the host for the run, asks for the
passphrase, and passes only the agent socket into the pod; the key expires after `APOD_SSH_TTL`
and the agent is killed when the pod exits. `confirm = true` confirms every use.
Host aliases go in the profile's `home/.ssh/config`. The pod can log in anywhere the key works.

## Notifications

`apod-notify [--urgent] TEXT` in a pod sends you a desktop notification on the host, titled
with the agent session's name.

```toml
# ~/.config/apod/config.toml (every profile) or a profile's profile.toml
[notify]
enabled = true
# command = ["notify-send", "-a", "apod"]   # the default; called with TITLE TEXT
# urgent = ["-u", "critical"]                # added for --urgent
```

No built-in role allows `notify:send`, so each notification waits for your approval until you
add it to a role:

```toml
# ~/.config/apod/roles.toml: adds to the built-in dev role
[roles.dev]
notify = ["send", "urgent"]
```

Or approve the first one "for the role" in the TUI (`r`).

## Pasting images

The pod can't see your desktop, so Claude Code's Ctrl+V finds no image there. The `clipboard`
extension lets it paste images from your clipboard, and only images: copied text, passwords
included, never reaches the pod. It's off unless you turn it on:

```toml
# a profile's profile.toml
allow = ["clipboard:image"]   # paste without asking; without it each paste waits for you
[clipboard]
enabled = true
```

**How it works.** On Linux, Claude Code pastes an image by running `wl-paste -l`, then
`wl-paste --type image/png`. In the pod, `wl-paste` is `apod-clipboard`, which asks the broker.
The broker answers `-l` with the image types on the clipboard only (`image/png`, `image/jpeg`,
`image/gif`, `image/webp`, `image/bmp`), without asking. Reading the image is the
`clipboard:image` action: allowed by the role or the profile's `allow`, or it waits for your
approval while Claude Code waits for the paste. The broker then runs the host's `wl-paste`
(Wayland) or `xclip` (X11) with a fixed command, and passes the image back (up to 20 MB).

**On the host** it needs `wl-clipboard` (Wayland) or `xclip` (X11), and a broker started in
your desktop session: it reads the clipboard with the environment it was started in
(`WAYLAND_DISPLAY` or `DISPLAY`), so start it from a terminal on your desktop, and run
`apod broker restart` after logging out and back in. Without either, the extension stays off,
with a warning at launch.

**Without it,** paste the image's path instead: Claude Code attaches an image when you paste
an absolute path to a `.png`, `.jpg`, `.gif` or `.webp` file (drag the file onto the terminal),
if the pod can see the file (a mount, such as your screenshots folder).

**Check it:** copy an image, then in the pod (`apod NAME -a bash`):

```sh
wl-paste -l                                        # image/png
wl-paste --type image/png > /tmp/p.png && file /tmp/p.png
```

## Roles and scope

A role is a list of **actions**: what a pod may do without asking (`gh:pr create`,
`github:push branch`, `ssh:login`). There are far too many to pick one by one for each
profile, `gh` alone has dozens, so apod ships roles that bundle them by job, and a profile
names one. Pick the closest, then add or remove actions in your own `roles.toml` (below).

**Choosing a role.** `apod broker roles` lists what each allows, by extension and command (what
a role adds to the ones it extends; `apod broker roles NAME` for all of one role's), and `apod
broker extensions NAME` what each role allows of one extension.

| Role         | For                            | May                                                                                                                                                     |
| ------------ | ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reader`     | Reviewing, answering questions | View PRs, issues, CI and repositories; read the APIs for what's in scope; fetch.                                                                        |
| `dev`        | Coding                         | reader, plus push branches (force pushes too, not the default branch), open Radicle patches, create and edit PRs, comment, create issues; sign commits. |
| `maintainer` | Landing work                   | dev, plus merge and review PRs, edit and close issues, re-run CI, fork and create repositories, push tags, delete branches.                             |
| `pm`         | Project boards                 | reader, plus project boards (items, fields, templates) and creating, editing and commenting on issues.                                                  |
| `ops`        | Hosts                          | ssh to the hosts in scope, sign commits; fetch only.                                                                                                    |

A pod's role is `--role`, else its [preset](#mounts-network-and-images)'s `role =`, else its
profile's, else `reader`. One profile can take different roles per launch:
`apod bot --role pm ~/src/org/planning`, or `apod bot -p triage` with the role in a preset.

**Adding to the role, for one profile.** `allow` and `deny` in the profile's `profile.toml` add
actions to its role or take them away, for that profile's pods only, whatever role they launch
with. For a `dev` profile that also logs in to hosts:

```toml
# ~/.config/apod/profiles/work.toml
role = "dev"
allow = ["ssh:connect", "ssh:login"]   # on top of dev: logins to the hosts in scope
deny = ["github:push force"]           # never, for this profile
scope = ["ssh:nuc"]

[ssh]
key = "~/.ssh/id_apod_broker"
```

The scope says *where* and the role *what*: an `ssh:` entry alone doesn't let a pod log in. A
launch warns when a scope entry you wrote can't be used because the role allows nothing of its
extension (`ssh:nuc in scope, but role dev allows nothing of ssh`), with what to add. `apod new`
asks for the role first, and offers these additions as you set up ssh, signing, Radicle or
notifications. `allow` and `deny` are ignored in `~/.config/apod/config.toml`, like `role` and
`scope`.

**Your own roles**, in `~/.config/apod/roles.toml`. The built-in roles aren't copied there: they're
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
can declare several: `gh` declares `gh:` (repositories and owners), `gh-project:` (project
boards) and `gh-search:` (searches across GitHub). Values take shell-style patterns (`*`). The scope is built from the project's GitHub and Radicle remotes (and its folder, for signing),
the profile's `scope`, and `--scope`. Forks and repositories the session
creates join it. The kinds:

| Entry                                           | Covers                                                                                                                                                                       |
| ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `gh:OWNER/REPO`, `gh:OWNER/*`                   | A repository: gh commands, API calls, git fetches and pushes. `gh:OWNER/*` also covers `gh search --owner OWNER`.                                                            |
| `gh:OWNER`                                      | The owner itself: owner-level reads, its project list, creating repositories there.                                                                                          |
| `gh-project:OWNER/NUMBER`, `gh-project:OWNER/*` | A project board.                                                                                                                                                             |
| `gh-search:prs`, `gh-search:all`                | `gh search prs` (or issues, code, commits, repos): across GitHub, private repositories the account can read included, and with any `--repo` or `--owner`. `all`: every kind. |
| `rad:RID`                                       | A Radicle repository.                                                                                                                                                        |
| `ssh:HOST`, `ssh:USER@HOST`                     | A host as named in your ssh config: as the user that config gives it, or as USER.                                                                                            |
| `git-local:PATH`                                | Signing in a repository with no GitHub or Radicle remote, by its folder.                                                                                                     |

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
  `~/.local/state/apod/broker/grants.json`; `apod broker grants` lists them, `grants rm ROLE ACTION`
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
  rxvt and VTE terminals, else just the bell. Inside a multiplexer (herdr, tmux, screen, zellij)
  it rings only the bell: they pass the bell on, but keep notification sequences to themselves
  (herdr reads `OSC 9` as a progress report). `APOD_BROKER_NOTIFY` (`osc9`, `osc99`, `osc777`,
  `bell`, `none`) overrides the guess. Clicking the notification takes you to the TUI's tab or
  split, in terminals that do that (Ghostty does).
- The broker calls `notify-send` itself when it's installed, except while a TUI that sends
  desktop notifications is open (`osc9`, `osc99` or `osc777`; not `bell` or `none`): then you get
  the TUI's notification only. A click on the broker's own notification doesn't take you anywhere.
  If no notification shows while the TUI is open, the terminal ignored the sequence it guessed:
  set `APOD_BROKER_NOTIFY`.
- In a Herdr app terminal (`HC_TERMINAL_ID` set), the TUI also shows that terminal as "needs
  you" (its OSC 7501 status, `app=apod-broker`) while any request waits, with the newest as the
  message, and with no status otherwise (not idle, which the app would take for done); the
  app's alert takes you to the TUI. While it's open, the broker doesn't mark the agent's
  terminal (see [History, resume and herdr](#history-resume-and-herdr)). `APOD_HERDR=0` turns it
  off.

Requests and sessions show the agent session's name when it has one (Claude Code's `/rename`),
else the profile. Approve by the actions and targets listed, not by the one-line summary, which
the agent's arguments partly shape.

## Without the broker: tokens

A profile with `broker = false` in its `profile.toml` (an old one without a `broker.toml`) can use a GitHub token instead: `apod token bot` stores a
fine-grained one as the podman secret `gh-bot` (prompted without echo), and `gh` and `git push` in
the pod have whatever it allows. `[token] profile = "other"` uses another profile's token.
Without either, `gh` and HTTPS pushes are unauthenticated.

**Moving a profile from a token to the broker.** `apod profiles` shows each profile's token: `set`
(its own), `gh-OTHER` (another profile's), or `unused` once the broker replaces it, and lists
tokens no profile launches with. For each profile:

1. Log the token's GitHub account in to the host's `gh`, once per account ([GitHub](#github)).
2. In its `profile.toml`, remove `broker = false` and add a `role`, `[gh] user` and any extra scope
   (top-level keys first). For a profile with an `[ssh-agent]` key, an `[ssh]` table with the key,
   and the hosts as `scope = ["ssh:HOST"]` entries, replaces `[ssh-agent]` and, if that was only
   for ssh, `[pod] network`. A profile still in the old files: `apod migrate PROFILE` first.
3. Relaunch, check `gh auth status` and a push in the pod, then remove what the broker made
   redundant: `apod token PROFILE --rm`, `[token]`, and the token itself on GitHub
   (Settings → Developer settings → Personal access tokens).

## Migrating to profile.toml

Profiles made before `profile.toml` have their settings in up to eleven files (`broker.toml`,
`env`, `mounts`, `image`, `network`, `volumes`, `connectors`, `github`, `ssh-key`, `ssh-confirm`).
They still launch, as they always did, with a warning that names the fix. To convert one:

```sh
apod migrate work          # one profile
apod migrate --all         # every profile
```

For each profile, `apod migrate` shows the `profile.toml` it would write, and the old files it
would move, and asks. On yes it writes the file and moves the old files into
`~/.agent-pod/<profile>/.old/`; nothing is deleted, and nothing in `.old/` is read or mounted.

- A profile with a `broker.toml` keeps that file's text, comments included, as the start of its
  `profile.toml`; a profile without one gets `broker = false`.
- `env` becomes `[git]` (the four `GIT_AUTHOR_*` and `GIT_COMMITTER_*` names) and `[pod.env]` (the
  rest); `mounts` becomes `[pod] ro` and `rw`, read-only ones first; `github` becomes
  `[token] profile`; `ssh-key` and `ssh-confirm` become `[ssh-agent]`.
- Before it writes, it reads the new file back and checks that it means what the old files did. If it
  doesn't (a `scope` that sits under `[gh]` in the old `broker.toml` was `gh.scope` all along),
  it says so and changes nothing.
- It's safe to run again: a migrated profile says so. With both a `profile.toml` and old files
  (the old files are ignored, with a warning at every launch), it offers to move the old files.
- `--yes` doesn't ask.

One behaviour changes when a profile is read, migrated or not: the `env` file is no longer passed to
podman as `--env-file`. Its variables, and `[pod.env]`, are `-e` options, with the quotes stripped
from an old file's values as the broker always did. And mounts that would expose apod's own files
are refused (see [SECURITY.md](../SECURITY.md#the-container)).

## Migrating to the new layout

apod used to keep everything in `~/.agent-pod`: your settings, the profiles' logins, the broker's
state and its sockets. It now keeps them in four folders ([where](reference.md#where-apod-keeps-its-files)):
what you write in `~/.config/apod` (which can go in a dotfiles repository: no logins or logs
there), the profiles' homes and the work logs in `~/.local/share/apod`, what the tools write in
`~/.local/state/apod`, and the sockets in `$XDG_RUNTIME_DIR/apod`. An install that has only
`~/.agent-pod` keeps working as it did, and says `apod: legacy layout: apod migrate --layout` at
launch. The legacy layout will go in a later release. To move:

```sh
apod broker stop                 # and exit the pods first: it refuses while they run
apod migrate --layout            # shows what moves where, and asks; --yes doesn't ask
apod broker paths                # where things are now
```

The broker starts again with the next pod that uses it, from the new folders, with its saved
approvals; `apod broker keys` loads its keys.

- Each profile becomes one file, `profiles/NAME.toml`: its `profile.toml`, or its old files
  converted on the way (with the same check as `apod migrate`; mode 0600), or `broker = false` for
  a folder with no settings (which launched without the broker). Its `home/` goes to
  `~/.local/share/apod/profiles/NAME/home/`, its `google-credentials.json` beside its file, as
  `NAME.google-credentials.json`.
- `broker.toml` becomes `config.toml`; `roles.toml` and `broker-extensions/` (now `extensions/`)
  go to the config folder; `worklog/` to the data folder; `history.jsonl`, `selinux` and the
  broker's state (`log.jsonl`, `grants.json`, the saved sessions, the gh-api CA, its home) to the
  state folder.
- Nothing is deleted. What isn't moved (the old profile files, `.old/`, stale sockets, files apod
  doesn't know) stays in the old folder, which becomes `~/.agent-pod.migrated`: delete it once your
  profiles work.
- It stops before moving anything when a file it would write is there already with something
  else in it, and says which. Run again, it finishes what a first run left; once done, there's
  nothing to migrate.
- It says which mounts of a profile the new folders' [mount guard](reference.md#where-apod-keeps-its-files)
  would refuse (a mount of `~/.config` or `~/.local` contains them now), and which lines of a
  profile have paths in `~/.agent-pod` (a key kept in a profile's folder, say): what they point to
  stays in `~/.agent-pod.migrated`, so move it and fix the paths.
- With `APOD_ROOT=DIR`, it moves `DIR` to the folders the `APOD_*_DIR` and `XDG_*` variables say;
  then unset `APOD_ROOT`.

### One profile at a time

To keep working while you move, run this version beside the one you have, under another name, and
move one profile at a time, starting with one you aren't using:

```sh
ln -s ~/src/apod-new/apod ~/.local/bin/apod-new   # a checkout of this version
apod-new migrate --layout weekly                  # one profile; it asks first
apod-new weekly                                   # launch it with the new apod
```

`apod migrate --layout PROFILE` moves that profile's file, home and Google client as above, copies
its launches from the history, and copies what the profiles share (`config.toml`, `roles.toml`,
`extensions/`, the SELinux marker, the broker's approvals) when the new layout doesn't have them
yet. The rest of its folder goes to `~/.agent-pod.migrated/PROFILE`, where the old apod doesn't
see it. Only a pod of that profile stops it: the old broker and pods of the other profiles can go
on running.

While both run:

- The old apod launches the profiles still in `~/.agent-pod`; the new one, the migrated ones (and
  says, at each launch, which are left). Each has its broker, with its own sockets, approvals and
  keys (`apod broker keys`, and `apod-new broker keys`).
- What the profiles share is in both places: a change to your roles or `config.toml` goes in
  both, `~/.agent-pod` for the old apod and `~/.config/apod` for the new one.
- The new apod's [mount guard](reference.md#where-apod-keeps-its-files) refuses `~/.agent-pod`
  too, but for read-only mounts of its `worklog/`, `history.jsonl`, `broker/log.jsonl` and its
  profiles' transcripts (`<profile>/home/.claude/projects`): a profile that reviews them can
  mount both those and the new ones (`~/.local/share/apod/worklog`,
  `~/.local/state/apod/history.jsonl`, `~/.local/state/apod/broker/log.jsonl`,
  `~/.local/share/apod/profiles/<profile>/home/.claude/projects`). The old one doesn't know the new folders: don't give its pods `~/.config`, `~/.local`
  or `$XDG_RUNTIME_DIR`.

Once the last pods of the old apod are done, stop its broker (`apod broker stop`) and run
`apod-new migrate --layout` for the rest. It takes the new layout as it finds it: what's the same
there stays behind, the history is merged, and the old broker's log, approvals and CA stay where
the new broker has its own. `~/.agent-pod` then becomes `~/.agent-pod.migrated.1` (beside the
profiles' leftovers in `~/.agent-pod.migrated`). Point `apod` at the new version.

## Mounts, network and images

**Mounts.** Only the project folder is mounted (read-write), plus the profile's home and
`[pod] ro` and `rw` paths, your `~/.claude/CLAUDE.md` and `~/.claude/skills` read-only, the session's
[work log](#work-logs) (`/run/apod-worklog`, read-write) and, with the broker, its folder
(`/run/apod-broker`) and the extensions' commands (`gh`, `apod-sign`, ...).

When the project is a linked worktree (`git worktree add`, or herdr's worktrees), its `.git` is
a file pointing into the main repository's `.git`, which holds the worktree's HEAD and index and
the objects, refs, config and hooks they all share. apod mounts that `.git` read-write too, at
the same path, once the main repository's record of the worktree points back at the folder (the
pod can rewrite its `.git` file). A `[pod]` `ro`/`rw` entry or `--ro`/`--rw` naming that `.git` sets its
mode instead (read-only, git in the pod can't commit). The main checkout's files stay out of the
pod.

Git in the pod lists the repository's worktrees that aren't mounted as prunable, and
`git worktree prune` or `git worktree remove` there would delete their records (`.git/worktrees/<name>`:
their HEAD and index), which breaks those worktrees on the host. So `.git/worktrees` is mounted
read-only, but for the records of the worktrees in the pod (the project's own, and any mounted
read-write), and those commands fail, for worktrees the host adds while the pod runs too. A main
checkout's pod gets the same when the repository has worktrees outside the pod at launch. A
`[pod]` `ro`/`rw` entry or `--ro`/`--rw` naming `.git/worktrees` sets its mode instead. The agent
is told to leave them alone, and the pod's git config has `gc.worktreePruneExpire=never`. What
this costs in the pod:

- `git worktree add` fails. For flows that add worktrees, launch the pod in
  [a clone of its own](#a-clone-for-worktree-flows).
- `git gc` doesn't expire reflogs (`gc.reflogExpire` and `gc.reflogExpireUnreachable` are
  `never`: it would fail on the other worktrees'). `git gc` on the host still does.
- Renaming a branch that another worktree has checked out renames it but leaves that worktree's
  HEAD on the old name: rename it back on the host (`git branch -m NEW OLD`).

```toml
# ~/.config/apod/profiles/work.toml
[pod]
ro = ["~/src/shared-libs"]
rw = ["~/src/work/tmp"]
```

For one run, `--ro PATH` (`-m`) and `--rw PATH`. A command-line entry overrides the profile's mode
for the same path; a missing command-line path is an error, a missing profile path a warning.
apod refuses mounts that would expose its own files: its config, data, state and runtime folders
([where they are](reference.md#where-apod-keeps-its-files); `~/.agent-pod` in the legacy layout),
the apod checkout, anything that contains them, and a read-write mount of your home folder. A
read-only `~/.local/share/apod/worklog` (or a folder in it), `~/.local/state/apod/history.jsonl`,
`~/.local/state/apod/broker/log.jsonl` and a profile's transcripts,
`~/.local/share/apod/profiles/<profile>/home/.claude/projects` (or a folder in it), are fine, for
a pod that reviews them ([work logs](#work-logs)), but not by a path through a symlink in apod's
folders, which a pod may have made. The project folder can't be one of
apod's folders or contain one either (apod won't launch in `~` or `~/.config`); it stays writable
even inside a read-only entry. The first launch relabels mounted trees for SELinux (`:z`), which
can take a while on big trees. A running pod can't gain mounts: exit and relaunch with
`-- --continue`, or `apod resume --rw PATH` ([resume](#history-resume-and-herdr)).

**Environment.** `[pod.env]` sets variables in the pod, with their values in the profile's file.
For a secret (an API key), list its name in `[pod] env_from` instead, and keep the value in your
own environment (a password manager, your shell's startup): the file then holds no secret, and can
go in a dotfiles repository with the rest of `~/.config/apod`.

```toml
# ~/.config/apod/profiles/work.toml
[pod]
env_from = ["OPENAI_API_KEY"]   # passed as -e OPENAI_API_KEY: podman takes the value from apod's environment
```

The value isn't in podman's command line (`ps` doesn't show it), nor in `history.jsonl`. A name
that isn't set when you launch is left out, with a warning. Once passed, the value is in the pod,
like any variable: the agent can read it, and use it as the account it belongs to can.

**Network.** Pods use podman's default network (pasta). On some hosts (seen on Wi-Fi) pasta
gives pods IPv6 only, so IPv4-only destinations such as Tailscale hosts are unreachable; a
`[pod] network = "host"` shares the host's network instead, its localhost services
included.

**Images.** `Containerfile` builds the base `apod` image (Fedora, Claude Code, opencode, gh, git,
Node, Python). A profile's `[pod] image`, a preset's (below), `-i IMAGE` or
`APOD_IMAGE` picks another. To add a project's toolchain, build on it:

```dockerfile
# ~/.config/apod/images/Containerfile.rust
FROM localhost/apod
USER root
RUN dnf install -y gcc gcc-c++ clang cmake make pkgconf-pkg-config openssl-devel && dnf clean all
ENV RUSTUP_HOME=/opt/rustup PATH=/opt/cargo/bin:$PATH
RUN mkdir -p /opt/rustup /opt/cargo && chown agent:agent /opt/rustup /opt/cargo
USER agent
RUN curl -fsSL https://sh.rustup.rs | CARGO_HOME=/opt/cargo sh -s -- -y --no-modify-path
ENV CARGO_HOME=/home/agent/.cargo
```

```sh
podman build -t rust -f ~/.config/apod/images/Containerfile.rust ~/.config/apod/images
```

```toml
# ~/.config/apod/profiles/work.toml
[pod]
image = "rust"
```

Put tools under `/opt`, not the home folder, so the mounted home can't hide them. Rebuild
derived images after rebuilding `apod`.

**Presets.** The image, mounts, network, volumes and environment belong to a kind of job more
than to an identity: the same toolchain and source trees serve your own profile and a bot's. Rather
than a profile for each pair (two homes and logins for one identity, its `[git]` and `[gh]`
copied), put them in a preset, `presets/NAME.toml` in the config folder, and pick it at launch:

```toml
# ~/.config/apod/presets/logos.toml
description = "Rust toolchain; the Logos repositories, read-only"
[pod]
image = "rust"
ro = ["~/src/logos-co", "~/src/logos-blockchain", "~/src/logos-messaging"]
```

```sh
apod work -p logos ~/src/logos-co/some-repo     # your profile, with the Rust toolchain and the Logos trees
apod bot -p logos ~/src/logos-co/some-repo      # the bot's identity, role and scope, the same pod settings
apod work ~/src/rad/apod                        # neither: the Logos trees aren't mounted
```

A preset adds to the profile's own `[pod]` (mounts, volumes and variables join it; `image` and
`network` replace it), and several apply in order (`-p logos,nix`); `-i`, `--ro` and `--rw` still
win.

A job can also come with what the agent may do, and where: a preset's `role` and `scope` are what
`--role` and `--scope` would give, so a triage job needn't be typed out at each launch:

```toml
# ~/.config/apod/presets/logos-triage.toml
description = "Triage the issues of the Logos ecosystem repositories"
role = "pm"                                             # issues and boards: edit, label, close
scope = ["gh:logos-co/ecosystem", "gh:logos-co/rfp"]    # added to the profile's
[pod]
rw = ["~/src/logos-co/ecosystem", "~/src/logos-co/rfp"]
```

`apod work -p logos-triage` acts as `work`'s identity, with the `pm` role and those repositories
in scope; `--role` still wins. Who acts (the git identity, the GitHub account, the keys), the
profile's own `allow` and `deny`, and its connectors stay the profile's. `apod presets` lists
them. The rules are in the [reference](reference.md#presets).

## A clone for worktree flows

Some flows add worktrees of their own: a runner that gives each piece of work a
`git worktree add`, or Claude Code subagents with `isolation: "worktree"` (under
`.claude/worktrees`). In a pod on a linked worktree, or on a main checkout with worktrees
outside the pod, `git worktree add` fails ([Mounts](#mounts-network-and-images)). Give such a
flow a clone of its own: a folder that only pods use, with every worktree inside it. All the
worktrees are then in the pod, so `git worktree add`, `remove` and `prune` work, and your
checkout's `.git` (its hooks and config) stays out of reach.

```sh
git clone https://github.com/acme/widget ~/src/pods/widget   # on the host, before any pod uses it
apod work ~/src/pods/widget                                   # its remote puts gh:acme/widget in scope
```

Or let the first pod clone it, through the broker. Launch in an empty folder, with the repository
in scope (the folder has no remote yet to put it there), and have the agent run
`git clone https://github.com/acme/widget .`:

```sh
mkdir -p ~/src/pods/widget
apod work --scope gh:acme/widget ~/src/pods/widget
```

Keep the clone in a host folder, not in the pod's `/tmp`: that goes when the pod ends, with any
work that wasn't pushed. Then:

- **Launch pods at the clone's root,** not in one of its worktrees: that's a pod on a linked
  worktree again, and `git worktree add` fails in it.
- **Work leaves by push.** The broker checks each push against the role and scope. To review
  a pod's work, fetch the branch it pushed into your own checkout, from the remote
  (`git fetch origin BRANCH`), not from the pods' folder.
- **The folder is the pods'.** They write its hooks, its config and its `.claude/settings.json`,
  so don't run git, or any other tool, in it on the host. Reading or deleting its files is fine.
  A pod can add remotes, which join later pods' scope: read `.git/config` before launching a
  more powerful profile there (`cat ~/src/pods/widget/.git/config`).
- **Pods in the same clone share its `.git`,** as worktrees do. Each works in its own worktrees,
  and all of them are in every pod. For pods that mustn't touch each other's work, give each a
  clone.
- **A big repository:** `git clone --filter=blob:none` fetches the files' contents as git needs
  them, through the broker too. Avoid a shallow clone (`--depth`): rebases and merges need the
  history.
- **A worktree added outside the clone** (in `/tmp`, say) leaves its record behind when the pod
  ends. Later launches take it for a worktree in use on the host, and `.git/worktrees` is then
  read-only in the pod. Delete that record on the host:
  `rm -r ~/src/pods/widget/.git/worktrees/NAME`.

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
apod selinux install       # sudo semodule -i ...; records it in ~/.local/state/apod/selinux
apod broker restart        # the broker now runs at s0
apod selinux status        # or remove, to go back to label=disable
```

If a pod can't reach the broker after this, SELinux has denied something the module doesn't
allow yet: `sudo ausearch -m avc -ts recent` shows what, and `apod selinux remove` goes back to
running these pods with labelling off.

## claude.ai connectors and MCP servers

claude.ai connectors (Notion, Google Docs, …) come with the Claude account and act as you, so apod
turns them off unless enabled for the run with `-c` (comma-separated, repeatable) or the profile's
`[pod] connectors`; `none` turns off the groups before it (`-c none`: all of them). This, and the
local MCP servers below, apply to Claude only: opencode and bash ignore `-c`, with a warning.

Groups: `notion`, `gdoc` (Docs, Drive, Sheets), `todoist`, `canva`, `lovable`, `utsjitsu`,
`claudedocs`. Other known connectors are denied, and a PreToolUse hook blocks any claude.ai
connector tool that isn't enabled, including ones added to the account later.

`-c google-docs-mcp` runs a local
[google-docs-mcp](https://github.com/fryorcraken/google-docs-mcp) checkout
(`APOD_GDOCS_MCP`, default `~/src/fryorcraken/google-docs-mcp`) in the pod from a read-only mount,
with scopes `docs,sheets,slides,drive` (`APOD_GDOCS_SCOPES`) and lazy tools. Each profile signs in
once on the host with `apod google-auth <profile>`. It can't be combined with `gdoc`.

## Work logs

Each session keeps a work log on the host, `~/.local/share/apod/worklog/<id>/worklog.md`, mounted in
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
(`apod reviewer --ro ~/.local/share/apod/worklog ~/reports`), rather than with your own `claude`.
opencode isn't told about its log yet (only Claude gets apod's notes at startup).

For more than the agent's account, mount a profile's Claude transcripts too, read-only:
`~/.local/share/apod/profiles/<profile>/home/.claude/projects` (a folder per project, with a
`<session>.jsonl` per session, the same id as the session's work log), or one project's folder
in it. Only that folder is allowed, not the rest of the home, which holds the profile's login,
nor a path through a symlink in it: the pod could have made `projects` a symlink to `~/.ssh`.

```sh
apod reviewer --ro ~/.local/share/apod/worklog --ro ~/.local/share/apod/profiles/work/home/.claude/projects ~/reports
```

The transcripts hold everything the sessions saw: the files they read and their commands' output
(a token one printed included). Like the logs, they're the pods' to write: a pod can put anything
there, its own login included ([SECURITY.md](../SECURITY.md#the-container)). opencode's sessions
aren't covered: they're elsewhere in the home.

## History, resume and herdr

Every launch is recorded in `~/.local/state/apod/history.jsonl`: profile, folder, options, Claude session
id and terminal tab. Each new Claude run gets its own session id, and pods are labelled with
profile, folder and session. `apod history` lists folders and profiles, most recent first.

`apod resume` offers only sessions that aren't running and have a saved conversation, and
relaunches with the same profile and options. If the current tab launched one of them, it resumes
that one; otherwise it lists them and asks. The tab id is the first set variable among
`$APOD_PANE_ENV` (default `HERDR_PANE_ID HC_SCOPE`). `--list` always asks, `--latest` takes the
newest, and `--claude` opens Claude's own picker.

To carry on with more than the session was launched with, give launch options to `apod resume`
(all but `-a`). They go after the recorded ones, as if typed after them: `--scope`, `--ro`,
`--rw` and `-p` add to the session's, a `--rw` over a recorded `--ro` of the same path makes it
read-write, and `--role`, `-i` and `--gh-user` replace the session's. `-c` adds to the session's
connectors (the profile's `[pod] connectors` included), and `-c none` turns them off. A
`--role`, `--scope` or `--gh-user` turns on the broker of a session launched with
`--no-broker`, and `--no-broker` drops the session's `--role`, `--scope` and `--gh-user`:

```sh
apod resume --scope gh:acme/gadget --rw ~/src/acme/gadget   # one more repository, and its checkout
apod resume --role dev                                      # a wider role than the launch's
```

The relaunch is recorded with its options, so the next `apod resume` keeps them; giving the same
ones again doesn't repeat them. To go back to fewer, launch the session yourself, with its
folder: `apod <profile> [options] <dir> -- --resume <session id>` (the id is the `session` of its
launches in `history.jsonl`).

[herdr](https://herdr.dev) is a terminal multiplexer for coding agents, and apod is built to run
in its panes:

- **Agent detection.** In a pane herdr sees `podman`, not the agent inside the container. apod sets
  `HERDR_AGENT=claude` (or `opencode`) on the podman process, so herdr reads the pane as that agent
  and shows its working, blocked and idle states. The variable stays on the host.
- **"Needs you" while the broker waits.** A request waiting for your approval looks like work to
  herdr: the agent's tool call just hasn't returned. When a pod is launched from a herdr pane
  (`HERDR_PANE_ID` and `HERDR_SOCKET_PATH` set), the broker reports that pane to herdr as
  `blocked` (source `apod:broker`, message "apod broker: <the request>") for as long as a request
  of the pod waits, and releases the pane when the last one is allowed, denied or times out, so
  herdr goes back to reading the screen. herdr's blocked rollups, notifications and `agent wait`
  then see it: a sound when the pane is in a background workspace, and a popup when it isn't in
  the active tab, if you turned popups on (`ui.toast.delivery`, `off` by default). Only for
  `claude` and `opencode` pods. It's best effort: if herdr isn't answering, requests wait as before.
  `APOD_HERDR=0` turns it off.
- **The same in the Herdr app** (herdr-core: `HC_TERMINAL_ID` set). There a terminal's status is
  what its program writes in its output (OSC 7501, the Program Status Protocol), which Claude Code
  does, and the app has no call to set it from outside. So apod gives the broker the terminal's
  device (`tty`), and while a request of the pod waits, the broker writes the status there itself:
  `blocked` (`kind=permission`, message "apod broker: <the request>"), so the app plays its sound
  and shows its popup, then `working` once the last one is allowed, denied or times out (Claude's
  tool call goes on; its next report replaces it). Only for `claude` pods, and only while no
  `apod broker ui` is open in a Herdr app terminal: that one says "needs you" in its own terminal
  instead, where you approve. It writes nothing else to the terminal, and only to a `/dev/pts`
  device of yours. `APOD_HERDR=0` turns it off.
- **Recovery after a herdr restart or crash.** herdr restores container panes as plain shells in
  the right folder. Run `apod resume` there: apod recorded the tab each session was launched from
  (`HERDR_PANE_ID`, or `HC_SCOPE`, which survives a herdr server restart), so it goes straight back
  to that tab's session.
- **Many agents side by side.** Claude's agent view and background sessions are off inside pods (a
  pod lives only as long as its main process). Run several pods in herdr panes instead. Each pod
  has its own `~/.claude/sessions` (Claude's list of running sessions, by process id, which pods
  of a profile would otherwise overwrite); conversations, `/rename` titles and the work log are
  kept as before.

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
