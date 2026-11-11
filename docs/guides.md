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

What the shipped extensions do for a pod:

| Extension | In the pod | Set up with |
|---|---|---|
| `gh` | `gh` is a shim; the broker runs the real `gh` as the host account the profile names. | `[gh] user`: [GitHub](#github) |
| `github` | git's github.com remotes go through the broker, with that account's token. | `[gh] user` |
| `rad` | `rad://` remotes go through the broker, which pushes as the host's Radicle identity. | a `rad://` remote, or `[rad]`: [Radicle](#radicle) |
| `sign` | git signs commits and tags with a key the broker holds. | `[sign] key`: [Signing commits](#signing-commits) |
| `ssh` | `ssh HOST` logs in with the broker's agent, to the hosts in scope, through the host. | `[ssh]`: [SSH](#ssh) |
| `notify` | `apod-notify TEXT` sends you a desktop notification. | `[notify] enabled`: [Notifications](#notifications) |

A full profile `broker.toml` ([reference](reference.md#brokertoml) for every key):

```toml
# ~/.agent-pod/work/broker.toml
role = "dev"
scope = ["gh:logos-co/*", "gh-project:logos-co/5"]   # plus the project's GitHub and Radicle remotes

[gh]
user = "work-bot"                    # a host gh account (gh auth login); required for gh and GitHub remotes

[sign]
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
`[sign]`, `[rad]`, `[notify]`, your extensions'), for every profile that uses the broker. A
profile's `broker.toml` overrides them key by key within each table. `role` and `scope` are
ignored there, with a warning: what a pod may do comes only from its profile and its launch. The
global file alone doesn't switch a token profile to the broker.

```toml
# ~/.agent-pod/broker.toml
[ssh]
key = "~/.ssh/id_apod_broker"
[sign]
key = "~/.ssh/id_apod_broker_sign"
```

**Keys** (ssh, signing, and Radicle's) live in the broker's own ssh-agent, never in the pod. At
launch, apod loads the ones it doesn't hold yet with `ssh-add`, which asks for their passphrases;
they expire after `APOD_SSH_TTL` (default `4h`). After a broker restart, or before launching,
`apod broker keys` lists every key the profiles and running pods use and loads the missing ones.
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
3. **The profile:** the identity in `env` and the account in `broker.toml`:

   ```sh
   # ~/.agent-pod/bot/env
   GIT_AUTHOR_NAME=my-bot
   GIT_AUTHOR_EMAIL=12345+my-bot@users.noreply.github.com
   ```

   ```toml
   # ~/.agent-pod/bot/broker.toml
   role = "dev"
   [gh]
   user = "my-bot"
   ```

   `--gh-user ACCOUNT` names another account for one launch. A profile that names none gets no
   `gh` and no GitHub remotes: the broker never falls back to your active login.
4. **The scope.** The project's GitHub remotes are in scope automatically. Add more with
   `scope = ["gh:ORG/*"]` or `--scope gh:ORG/REPO` ([Roles and scope](#roles-and-scope)).
5. **Check it** in a pod (`apod bot -a bash ~/src/org/repo`): `gh auth status` shows the account,
   role, scope and what's allowed; then `gh pr list` and a push of a branch.

**In the pod.** GitHub ssh remotes, `github-*` host aliases from the host's `~/.ssh/config`
included, are rewritten to HTTPS, so they go through the broker. The broker runs `gh` with an
empty config and home of its own and no access to the pod's files: the shim sends the current
repository and branch and the contents of files a command names (`--body-file`, `-F key=@file`,
`--input`, release assets). Arguments that name host paths are refused. `gh repo clone` and
`gh pr checkout` run locally through git; `gh pr create --fill` isn't available (pass `--title`
and `--body`).

## Radicle

Pods fetch and push `rad://` remotes through the broker, which pushes as the host's Radicle
identity. Nothing from the pod's repository runs on the host: a push lands in a staging
repository the broker owns, then goes on with the host's `git-remote-rad`.

1. **Radicle on the host:** `rad` installed, an identity (`rad auth`), and the node running so
   pushes reach the network.
2. **The project** has a `rad://` remote (`rad clone`, or `rad init`, which adds one): its
   repository is in scope automatically. Others: `scope = ["rad:RID"]` or `--scope rad:RID`.
3. **Optional settings**, in the profile's or the global `broker.toml`:

   ```toml
   [rad]
   home = "~/.radicle-pods"    # RAD_HOME of the identity pushes land as (default $RAD_HOME or ~/.radicle)
   # key = "..."               # the signing key (default: home/keys/radicle)
   # confirm = true            # confirm each use of the key on the host (needs openssh-askpass)
   # nid, storage              # the node id and storage folder, if rad can't tell
   ```

   Pushes otherwise look like yours. To keep pods' patches apart, make a separate identity
   (`RAD_HOME=~/.radicle-pods rad auth`) and point `home` at it.
4. **Check it** in a pod: `git fetch rad`, then `git push rad HEAD:refs/patches -o
   patch.message="test"` opens a patch (`dev` may).

Roles: `dev` pushes branches (force pushes too) and opens patches; `maintainer` also pushes tags
and deletes branches. The default branch waits for you.

## Signing commits

Pods sign commits and tags with an SSH key that stays on the host. Who signs is the profile (its
key and identity), what is the role (`sign:commit`, `sign:tag`), where is the repository.

1. **A key** for pods' signatures. Use a separate one from yours if a "Verified" badge should
   tell your commits apart from a pod's:

   ```sh
   ssh-keygen -t ed25519 -f ~/.ssh/id_apod_broker_sign -C "apod pods"
   ```

2. **The identity** the signatures vouch for, in the profile's `env`. For GitHub to show
   "Verified", the email has to be a verified email of the GitHub account in step 5:

   ```sh
   # ~/.agent-pod/work/env
   GIT_AUTHOR_NAME=Work Bot
   GIT_AUTHOR_EMAIL=bot@example.com
   ```

3. **The key in `broker.toml`**, the profile's or `~/.agent-pod/broker.toml` for every profile:

   ```toml
   [sign]
   key = "~/.ssh/id_apod_broker_sign"   # its .pub next to it
   confirm = true                       # optional: confirm each signature on the host (needs openssh-askpass)
   auto = false                         # optional: don't turn on commit.gpgsign, even for roles that sign
   ```

4. **A role that signs, and a scope that covers the repository.**
   - Role: `dev`, `maintainer` and `ops` sign every commit and tag. With `reader` or `pm`,
     `git commit -S` waits for your approval. Your own roles get it with `sign = ["commit",
     "tag"]`.
   - Scope: the repository, by the remote the branch pushes to (`gh:OWNER/REPO` or `rad:RID`), so
     the scope you give for pushes covers signing too. The project folder and the repositories
     under it are always in scope, as `git-local:PATH`, for repositories with no such remote.
   - Anything else waits: another repository, or a commit by someone other than the profile's
     identity (`sign:as other`).

5. **The public key on GitHub, as a signing key** (not an authentication key), on the account
   whose email is in `env`:

   ```sh
   gh ssh-key add ~/.ssh/id_apod_broker_sign.pub --type signing --title "apod pods"
   ```

The next launch loads the key into the broker's agent and asks for its passphrase. Then, in a pod:

```sh
git commit --allow-empty -m "signing test" && git log --show-signature -1   # Good "git" signature for bot@example.com
```

`git log --show-signature` and `git verify-commit` work in the pod because the broker gives it an
allowed-signers file with the profile's key and emails. The pod tells the broker which
repository it's signing in, so this keeps an agent to its scope rather than stopping a hostile
one; pushing the commit is checked separately ([SECURITY.md](../SECURITY.md)).

## SSH

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
   hosts = ["devbox", "deploy@nas"] # NAME: any user; USER@NAME: that user only
   # confirm = true                 # confirm each login on the host (needs openssh-askpass)
   # proxy = false                  # the pod connects itself (it needs a route to the hosts)
   ```

   `--scope ssh:HOST` adds a host for one launch. `reader` and `ops` (and roles that extend
   `reader`) may log in to the hosts in scope; another host waits for you.
4. **Check it** in a pod: `ssh devbox true` logs in; `ssh some-other-host true` waits.

Only port 22 or the host's configured ssh port goes through without asking; another port is a
tunnel (`ssh:connect port`). Once logged in, the agent can do anything that account can.

**Without the broker:** a profile's `ssh-key` file names a passphrase-protected private key kept
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

**Choosing a role.** `apod broker roles` lists what each allows.

| Role | For | May |
|---|---|---|
| `reader` | Reviewing, answering questions | View PRs, issues, CI and repositories; read the APIs for what's in scope; fetch; ssh to the hosts in scope. |
| `dev` | Coding | reader, plus push branches (force pushes too, not the default branch), open Radicle patches, create and edit PRs, comment, create issues; sign commits. |
| `maintainer` | Landing work | dev, plus merge and review PRs, edit and close issues, re-run CI, fork and create repositories, push tags, delete branches. |
| `pm` | Project boards | reader, plus project boards (items, fields, templates) and creating, editing and commenting on issues. |
| `ops` | Hosts | ssh to the hosts in scope, sign commits; fetch only. |

A pod's role is `--role`, else its profile's `role =`, else `reader`. One profile can take
different roles per launch: `apod bot --role pm ~/src/org/planning`.

**Your own roles**, in `~/.agent-pod/roles.toml`. A role with a built-in's name adds to it; a new
name is a new role ([format](reference.md#rolestoml)):

```toml
[roles.dev]                 # adds to the built-in dev
gh = ["pr merge"]           # dev pods may merge too

[roles.triage]              # a new role
description = "reader + label and close issues"
extends = ["reader"]
gh = ["issue edit", "issue close"]
deny = ["gh:issue delete"]
```

**Scope** is where the actions may happen: `KIND:VALUE` entries, with shell-style patterns
(`*`). It's built from the project's GitHub and Radicle remotes (and its folder, for signing),
the profile's `scope` and `[ssh] hosts`, and `--scope`. Forks and repositories the session
creates join it. The kinds:

| Entry | Covers |
|---|---|
| `gh:OWNER/REPO`, `gh:OWNER/*` | A repository: gh commands, API calls, git fetches and pushes. |
| `gh:OWNER` | The owner itself: owner-level reads, its project list, creating repositories there. |
| `gh-project:OWNER/NUMBER`, `gh-project:OWNER/*` | A project board. |
| `rad:RID` | A Radicle repository. |
| `ssh:HOST`, `ssh:USER@HOST` | A host as named in your ssh config, for any user or that user. |
| `git-local:PATH` | Signing in a repository with no GitHub or Radicle remote, by its folder. |

Extensions add kinds; `apod broker scopes` lists them all. An entry of a kind no extension
provides is ignored, with a warning at launch. The older forms (`OWNER/REPO`, `OWNER`,
`project:OWNER/N`) still work, with a warning naming the new form.

Keep scope narrow: `gh:logos-co/*` covers every repository of the org.

## Approving requests

A request outside the role or the scope waits (`APOD_BROKER_ASK_TIMEOUT`, default 90 seconds; 0
rejects at once). It shows up in `apod broker` (a terminal UI), in `apod broker pending`, and as
a desktop notification. Allow it:

- **once**: this request;
- **for the session**: its actions and targets, until the pod exits;
- **for the role**: its actions, for every profile with that role, now and later (saved in
  `~/.agent-pod/broker/grants.json`; `apod broker grants` lists them, `grants rm ROLE ACTION`
  removes one). Prefer the session for one-offs.

A rejected request can be allowed after the fact, and the agent retries. From a shell:
`apod broker allow ID [--session|--role]`, `apod broker deny ID`. `apod broker log -f` follows
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
`mounts` file, and your `~/.claude/CLAUDE.md` and `~/.claude/skills` read-only.

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
`connectors` file; `-c none` turns them off.

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
