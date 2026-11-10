# apod: agents in podman

`apod` runs coding agents (Claude Code, opencode, or a plain shell) in rootless podman containers.
Each pod runs as a **profile** (an agent identity: who it commits as, its logins, what it can
see). With the host's [broker](#broker), the pod holds no credentials: its **role** says what it
may do on GitHub, Radicle, commit signing and ssh, its **scope** says what it may touch, and
anything else waits for your approval (see [Concepts](#concepts)). The agent runs with no
permission prompts; the container, not the agent, is the security boundary.

## Why

I run Claude Code with `permissions.blockReadsOutsideWorkingDirectories` on, so it can't read
outside the folders I've allowed. That works well until the work involves scripting. Simple
commands go through, but Claude Code's permission analysis can't fully parse complex ones: pipes
into loops, command substitutions, variables, inline scripts. It can't tell whether they stay
within the working directories, so it asks. A session turns into death by a thousand approvals.
Approving them without reading defeats the point, and loosening the rule gives away the scope.

apod moves the boundary instead. The agent runs in a rootless container that only has the
project folder and what the profile mounts, so it can run anything without asking, and what it
can't see it can't touch.

Then I wanted agents to do some GitHub work too: push branches, open PRs, comment on issues, move
cards on a project board. Handing a pod a token gives it everything that token can do, and
fine-grained tokens are tedious to make and still coarse (a repository, not "open PRs here but
don't merge"). So apod's [broker](#broker) keeps credentials on the host and acts for the pod: each
pod gets a role (what it may do) and a scope (what it may touch), and anything else waits for
my approval in a TUI. The few prompts left are the ones worth reading.

The same pattern fits more than GitHub: Radicle pushes, signing commits with a key the pod never
sees, ssh to my hosts without a key per pod. So the broker is a small core (sessions, roles,
scope, approvals, the audit log) with each of these as an extension, and adding another is a
single Python file: see [Writing a broker extension](#writing-a-broker-extension).

## Where it lives

- **Radicle (primary):** `rad clone rad:z2CjA38QDC7c5tgBkPV23UippSCjK`, published by `fryorgearo`
  (`did:key:z6MkowunyxpkcgCx2DdndJwebjcpk5pEnhaDE8JH3MbLnDBe`).
- **GitHub (mirror):** <https://github.com/fryorcraken/apod>

Both are kept in sync: every push goes to both (`git sync`, see [Contributing](#contributing)).
Patches are welcome on either.

## Works with herdr

[herdr](https://herdr.dev) is a terminal multiplexer for coding agents. apod is built to run in
herdr panes:

- **Agent detection.** In a pane herdr sees `podman`, not the agent inside the container. apod sets
  `HERDR_AGENT=claude` (or `opencode`) on the podman process, so herdr reads the pane as that agent
  and shows its working, blocked and idle states. The variable stays on the host.
- **Recovery after a herdr restart or crash.** herdr's own session restore can't resume an agent
  inside a container: it restores those panes as plain shells in the right folder. Run
  `apod resume` there. apod records the tab each session was launched from (`HERDR_PANE_ID`, or
  `HC_SCOPE`, which survives a herdr server restart), so `apod resume` goes straight back to that
  tab's session. When the tab can't be matched, it lists the stopped sessions for the folder and
  asks.
- **Many agents side by side.** Claude's agent view and background sessions are off inside pods (a
  pod lives only as long as its main process). Run several pods in herdr panes instead.

## Install

Requirements: rootless podman, bash, jq, git; `gh` and `node` on the host only for
`apod token` and `apod google-auth`; for the broker, Python 3.11 or later and OpenSSH 8.9 or
later on the host (the pods' image has both), plus `gh` logged in for GitHub and `rad` for Radicle.

```sh
git clone https://github.com/fryorcraken/apod ~/src/rad/apod    # or: rad clone rad:z2CjA38QDC7c5tgBkPV23UippSCjK
podman build -t apod ~/src/rad/apod
ln -s ~/src/rad/apod/apod ~/.local/bin/apod
# zsh completion (oh-my-zsh puts $ZSH_CUSTOM/completions on $fpath), then: rm -f ~/.zcompdump*; exec zsh
ln -s ~/src/rad/apod/_apod ~/.oh-my-zsh/custom/completions/_apod
```

## Quick start

```sh
mkdir -p ~/.agent-pod/bot
cat > ~/.agent-pod/bot/env <<'ENV'
GIT_AUTHOR_NAME=my-bot
GIT_AUTHOR_EMAIL=12345+my-bot@users.noreply.github.com
GIT_COMMITTER_NAME=my-bot
GIT_COMMITTER_EMAIL=12345+my-bot@users.noreply.github.com
ENV
cat > ~/.agent-pod/bot/broker.toml <<'TOML'
role = "dev"                        # PRs, issues, branch pushes; anything else waits for you
[gh]
user = "my-bot"                     # a host gh login: gh auth login --hostname github.com
TOML
apod bot ~/src/some-repo            # Claude Code in a pod; first run: log in to Claude once
apod broker                         # in another terminal: approve what's out of scope
```

The [broker](#broker) is the recommended way to give a pod GitHub, Radicle, signing and ssh: the
pod holds no credentials, and you approve what's outside its role and scope. A profile can
use a token instead (`apod token bot` stores a fine-grained one, prompted without echo, and drop
the `broker.toml`); `gh` and `git push` in the pod then have whatever that token allows.

```sh
apod bot -a opencode ~/src/some-repo        # opencode instead
apod bot -a bash .                          # a shell in the pod (gh and git as the profile)
apod bot ~/src/some-repo -- --continue      # anything after -- goes to the agent
```

Agents run without permission prompts, since the container is the boundary:

- **Claude Code** runs with `--permission-mode bypassPermissions`.
- **opencode** (2.x, `@opencode/cli`) runs with `--auto`, which approves every permission a
  config doesn't explicitly deny: a `"deny"` in the profile's `opencode.json` still holds. It also
  runs with `--standalone`, a private server per pod instead of opencode's background service,
  which pods of the same profile would otherwise share through their common home.

Only the project folder is mounted (read-write). GitHub SSH remotes, including `github-*` host
aliases from `~/.ssh/config`, are rewritten to HTTPS inside the pod, so `gh` and `git push` go
through the broker (or use the profile's token). Claude is told at startup what is mounted and
what isn't.

## Concepts

A launch combines a **profile**, a **role**, a **scope** and a **project** into a **session**:
`apod <profile> [--role ROLE] [--scope SCOPE]... <project>`.

| Term | What it is | What it carries | Where it's set |
|---|---|---|---|
| **Profile** | An agent identity: who the agent is and what it can see. One per identity or kind of job, e.g. `bot`, `ops`. | Its git author (`env`). Its home (`home/`): Claude or opencode login, history and settings, Google login; every pod of the profile shares it, at the same time too. Its mounts, image, network, volumes and connectors. How it reaches GitHub and hosts: either a token (`apod token`, `ssh-key`) or a `broker.toml` with its default role and scope, its GitHub account (`[gh] user`, or `--gh-user` for one launch) and its keys. | `~/.agent-pod/<profile>/` |
| **Role** | What a pod may do without asking, by action: the same for every profile that uses it. | One list per broker provider: `gh` commands, git pushes to GitHub and Radicle, commit signing, ssh. No identity, credentials or targets. | Built in (`roles.toml`: `reader`, `dev`, `maintainer`, `pm`, `ops`); `~/.agent-pod/roles.toml` adds to them. A profile's `role =`, or `--role`; default `reader`. |
| **Scope** | What those actions may touch. | Repositories, owners, project boards, Radicle repositories, ssh hosts, git identities. | The project's GitHub and Radicle remotes and the profile's git identity, automatically; the profile's `scope` and `[ssh] hosts`; `--scope`. |
| **Project** | The folder the agent works in. | Mounted read-write; its remotes join the scope. | The folder argument. |
| **Session** | One running pod, registered with the broker. | Its profile, role and scope, and what you approve "for the session"; it all ends with the pod. | Each launch. |

The broker lets a request through when the role allows every action and every target is in the
scope; anything else waits for you. You can approve it once, for the session, or for the role.
**For the role** applies to every profile with that role, now and later (it's saved in
`~/.agent-pod/broker/grants.json`), so prefer "for the session" for one-offs.

`~/.agent-pod/broker.toml` holds settings shared by every profile that uses the broker: keys,
the Radicle home, extension settings. It never sets a role or scope: what a pod may do comes only
from its profile and its launch.

## Commands

```
apod <profile> [-a claude|opencode|bash] [-i IMAGE] [-c CONNECTORS] [--ro PATH]... [--rw PATH]...
     [--role ROLE] [--scope SCOPE]... [--gh-user ACCOUNT] [--no-broker] [dir] [-- agent args]
apod resume [--list|--latest|--claude] [dir]   resume a stopped Claude session started in dir
apod history                                   folders and profiles launched, most recent first
apod ps                                        running pods: profile, session, status, folder
apod profiles                                  profiles: broker role, GitHub token, Google login
apod token <profile> [--rm]                    set, replace or remove a profile's GitHub token
apod google-auth <profile>                     sign a profile in to Google for google-docs-mcp
apod broker [ui]|log [-f]|pending|sessions|roles  the broker: approval TUI, requests, sessions
apod broker allow ID [--session|--role]     approve a request (once, for the pod, for the role)
apod broker deny ID | grants [rm ROLE ACTION] | keys | restart | stop
apod selinux [install|remove|status]           SELinux labelling for broker and ssh-key pods
```

## Profile files

Everything lives in `~/.agent-pod/<profile>/` (`APOD_ROOT` overrides the root):

| File | Purpose |
|---|---|
| `env` | Required. Git identity (`GIT_AUTHOR_*`, `GIT_COMMITTER_*`) for commits made in the pod. |
| `home/` | The pod's persistent `$HOME`: Claude/opencode login, settings, history. |
| `mounts` | Extra mounts, one path per line, read-only unless prefixed `rw`. |
| `image` | Image for this profile (default `apod`; `-i` overrides per run). |
| `network` | `host` to share the host's network, e.g. to reach Tailscale hosts. |
| `volumes` | Named podman volumes, `NAME /path` per line, e.g. `apod-myproj-nix /nix`. |
| `broker.toml` | Recommended. Use the [broker](#broker) instead of a token and `ssh-key`: the default role and scope, the GitHub account, keys (`--no-broker` turns it off for a run). |
| `github` | Without the broker: use another profile's GitHub token (`gh-<that profile>`). |
| `ssh-key` | Path to a passphrase-protected private key, served from a per-run ssh-agent. |
| `ssh-confirm` | If present, confirm every use of that key on the host. |
| `connectors` | Default claude.ai connector groups (`-c` replaces them). |
| `google-credentials.json` | OAuth client for google-docs-mcp, instead of the checkout's. |

Without the broker, the GitHub token is the podman secret `gh-<profile>`; without one, `gh` and
HTTPS pushes are unauthenticated.

**Moving a profile from a token to the broker.** `apod profiles` shows each profile's token:
`set` (its own), `gh-OTHER` (another profile's, through its `github` file), or `unused` once a
`broker.toml` replaces it, and lists the tokens no profile launches with. For each profile:

1. Log the token's GitHub account in to the host's `gh`, once per account:
   `gh auth login --hostname github.com` (`gh auth status` lists them).
2. Add a `broker.toml` with a role, `[gh] user` and any extra scope. For `ssh-key` profiles,
   an `[ssh]` section with the key and `hosts` replaces `ssh-key`, `ssh-confirm` and, if that was
   only for ssh, `network`.
3. Relaunch the pod, check `gh auth status` and a push in it, then remove what the broker made
   redundant: `apod token PROFILE --rm`, the `github` file, and the token itself on GitHub
   (Settings → Developer settings → Personal access tokens).

### Broker

A pod uses the broker when its profile has a `broker.toml` or the launch passes `--role`,
`--scope` or `--gh-user` (`--no-broker` turns it off). It then gets no credentials at all. A broker on the host,
`apod-broker`, acts for the pod instead, through a socket directory mounted at
`/run/apod-broker`:

- **gh**: the pod's `gh` is a shim; the broker runs the real `gh` as the host `gh` account the
  profile names (`[gh] user`, or `--gh-user ACCOUNT` for one launch). A pod with no account named
  gets no `gh` and no GitHub remotes: the broker never falls back to your active login.
- **github**: git's github.com remotes go through the broker, which pushes and fetches with that
  account's token.
- **rad**: `rad://` remotes go through the broker, which fetches from the host's Radicle storage
  and pushes as the host's Radicle identity.
- **sign**: git signs commits and tags with an SSH key held by the broker (`gpg.format=ssh`).
- **ssh**: the pod's `SSH_AUTH_SOCK` is the broker's agent, which signs logins only to the hosts
  in scope; connections go through the host too, so the pod needs no route to them.

Every request is checked against the session's **role** (what it may do) and **scope** (what
it may touch); anything else waits for your approval. The broker starts with the first pod that
uses it and keeps running; its state is in `~/.agent-pod/broker/`. Each of these is an
extension of a small core, and you can add your own: see
[Writing a broker extension](#writing-a-broker-extension).

```toml
# ~/.agent-pod/work/broker.toml
role = "dev"
scope = ["logos-co/*", "project:logos-co/5"]   # plus the project's GitHub and Radicle remotes

[gh]
user = "fryorcraken"            # host gh account (gh auth login --hostname github.com); required for gh

[sign]
key = "~/.ssh/id_apod_broker_sign" # its .pub next to it; commit.gpgsign and tag.gpgsign on (auto = false: off)

[ssh]
key = "~/.ssh/id_apod_broker" # or keys = [...]; confirm = true: confirm each use (needs openssh-askpass)
hosts = ["devbox", "root@nas"]     # a host's name in the host's ~/.ssh/config, any user or USER@
proxy = true                    # connect through the broker (the default); false: the pod connects itself

[rad]
home = "~/.radicle"             # RAD_HOME of the identity pushes land as (default $RAD_HOME or ~/.radicle)
```

```sh
apod work ~/src/org/repo                           # as broker.toml says
apod work --role maintainer --scope fryorcraken/* ~/src/org/repo
apod broker                                        # approve requests in a terminal UI
```

**Settings for every profile.** `~/.agent-pod/broker.toml` takes the same tables (`[ssh]`,
`[sign]`, `[rad]`, extensions'), and they apply to every profile that uses the broker. A
profile's `broker.toml` overrides them key by key within each table. `role` and `scope` are
ignored there, with a warning. So you can set one ssh key for all pods and choose the hosts per
profile or per launch:

```toml
# ~/.agent-pod/broker.toml
[ssh]
key = "~/.ssh/id_apod_broker"
[sign]
key = "~/.ssh/id_apod_broker"
```

```sh
apod ops --role ops --scope ssh:devbox --scope ssh:root@nas ~/src/infra   # these two hosts only
```

The global file alone doesn't switch token profiles to the broker.

**Keys** (ssh, signing, and the Radicle key in `home/keys/radicle`) live in the broker's own
ssh-agent, never in the pod. At launch, apod loads the ones it doesn't hold yet with `ssh-add`,
which asks for their passphrases (they expire after `APOD_SSH_TTL`, default `4h`). After a broker
restart, `apod broker keys` loads them again; it lists every key the profiles' `broker.toml`
files and running pods use, and loads the missing ones, so you can also load them before
launching. One key can serve several hosts: the destination is
checked per login, so there's no need for a key per host.

**Roles** (`roles.toml` in this checkout; `apod broker roles` lists what each allows). Each has
one list per provider (`gh`, `github`, `rad`, `sign`, `ssh`). Add roles, or add actions to a
built-in one, in `~/.agent-pod/roles.toml`: a role there with a built-in's name adds its lists to
it (and its `description` replaces the built-in one). A pod with no role is a `reader`.

- `reader`: view PRs, issues, CI runs and repositories, read the REST and GraphQL APIs for the
  repositories in scope, fetch; sign commits; ssh to the hosts in scope.
- `dev`: reader plus push branches (not the default branch), open Radicle patches, create and edit
  PRs, comment, create issues.
- `maintainer`: dev plus merge and review PRs, edit and close issues, re-run CI, fork and create
  repositories, push tags and delete branches.
- `pm`: reader plus project boards (list, view, add and move items, add fields, templates) and
  editing, creating and commenting on issues.
- `ops`: ssh to the hosts in scope and sign commits; fetch only.

**Scope.** The project folder's GitHub and Radicle remotes, the profile's git identity, then
`broker.toml`'s `scope` (and `[ssh] hosts`) and `--scope`. Entries: `OWNER/REPO`, `OWNER/*`,
`OWNER` (owner-level reads, creating repositories there), `project:OWNER/NUMBER` (or
`project:OWNER/*`), `rad:RID`, `ssh:HOST` or `ssh:USER@HOST`, `email:ADDRESS` (signing as another
identity). Forks and repositories the session creates join its scope.

**Approvals.** A request outside the role or the scope waits (`APOD_BROKER_ASK_TIMEOUT`,
default 90 seconds; 0 rejects at once) and shows up in `apod broker` (a terminal UI),
`apod broker pending` and as a desktop notification. Allow it once, for the rest of the session,
or for the role (saved in `~/.agent-pod/broker/grants.json`). A rejected request can be allowed
after the fact for the session or the role, and the agent retries. `apod broker log -f`
follows every request with its decision. Some gh commands are never allowed: `auth`, `extension`,
`alias`, `config`, `ssh-key`, `gpg-key`, `codespace`, `browse`.

In the TUI: view 1 lists waiting requests (with the seconds left) above recent ones, with the
selected request's actions, targets and reason below; `a` allows once, `s` for the session, `r`
for the role, `d` denies. On a new waiting request it jumps to it, rings the bell and asks the
terminal for a desktop notification: `OSC 9` for Ghostty, iTerm2 and WezTerm, `OSC 99` for
kitty, `OSC 777` for foot, rxvt and VTE terminals (GNOME Terminal, Ptyxis), else just the bell.
`APOD_BROKER_NOTIFY` (`osc9`, `osc99`, `osc777`, `bell`, `none`) overrides the guess, e.g.
inside a multiplexer. The broker also calls `notify-send` itself when it's installed. View 2 lists
sessions with their scope and grants (`x` removes one), view 3 the role grants (`x` removes
one). Tab or 1–3 switch views, `q` quits. The mouse works too: click a tab, a row, or a button in
the footer (each shows its key); the wheel scrolls. To select text, hold Shift while you drag
(most terminals), or press `m` to hand the mouse back to the terminal (`m` again takes it back).
`y` copies the selected request's details to the clipboard (OSC 52: kitty, foot, Ghostty, WezTerm,
iTerm2, Alacritty, tmux with `set-clipboard on`; not GNOME Terminal or other VTE ones). Requests and sessions show the agent session's name
when it has one (Claude Code's `/rename`, read from the transcript in the profile's home), else
the profile.

**How requests are classified.**

- gh: commands by their full name, aliases resolved (`pr ls` is `pr list`), risky flags as their own
  actions (`pr merge --admin`); `gh api` by method and path (`api GET /repos/{repo}/pulls/*`);
  GraphQL by operation and top-level field (`graphql mutation addComment`), with `repository`,
  `organization`/`user` arguments and node ids (looked up on GitHub) checked against the scope. A
  query or mutation that can't be scoped that way waits for approval.
- github and rad pushes by ref: `push branch`, `push default-branch`, `push tag`, `push delete`,
  and for Radicle `push patch` (`HEAD:refs/patches`, push options such as `-o patch.message=...`
  passed on) and `push force` (a branch update that isn't a fast-forward). A Radicle push lands in
  a staging repository the broker owns first, then the broker pushes it on with the host's
  `git-remote-rad`; nothing from the pod's repository runs on the host.
- sign: `commit` or `tag`, by the author, committer or tagger email (in scope: the profile's git
  identity).
- ssh: `login` to the host whose host key the pod's ssh binds to the agent (OpenSSH 8.9's
  `session-bind`, verified by the broker's ssh-agent, matched against the host's `known_hosts`
  for the names in scope), every hop of a forwarded agent included; `connect` for the
  ProxyCommand, where `ssh NAME` follows the host's ssh config for NAME (`HostName`, `Port`, and
  `User`, unless the scope entry is `USER@NAME`). The pod gets the host's known keys for the hosts
  in scope.

**In the pod.** The broker runs `gh` with an empty config and home of its own and no access to the
pod's files, so the shim sends the current repository and branch and the contents of files a
command names (`--body-file`, `-F key=@file`, `--input`, release assets). Arguments that name host
paths are refused. `gh repo clone` and `gh pr checkout` run locally through git; `pr create
--fill` isn't available (pass `--title` and `--body`). The agent's system prompt lists its
role, scope and what it may do without asking, per provider; `gh auth status` shows the same,
plus what you've approved for the session since. Links in titles and bodies aren't targets.
Pods using the broker need SELinux to let them reach its socket: see [SELinux](#selinux).

Real-container check (the broker's own tests, `python3 tests/test_broker.py`, use a fake `gh`, a
local git server, a stand-in for Radicle storage and real ssh-agents):

```sh
apod work -a bash ~/src/org/repo
gh auth status; gh pr list; git fetch; git push origin HEAD:refs/heads/apod-broker-test
git commit --allow-empty -m test && git log --show-signature -1
ssh devbox true; ssh some-other-host true          # the second one waits: apod broker
git push rad HEAD:refs/patches -o patch.message="broker test"
gh pr merge 1                                      # waits: approve or deny in apod broker
```

### Mounts

```
# ~/.agent-pod/work/mounts
~/src/shared-libs
rw ~/src/work/tmp
```

For one run, add files or folders with `--ro PATH` (`-m`) and `--rw PATH`. A command-line entry
overrides the file's mode for the same path; a missing command-line path is an error. The project
folder stays writable even inside a read-only entry. The first launch relabels mounted trees for
SELinux (`:z`), which can take a while on big trees.

### SSH

`ssh-key` names a private key kept outside the profile's home. apod starts a dedicated ssh-agent on
the host for the run, asks for the passphrase, and passes only the agent socket into the pod; the
key expires after `APOD_SSH_TTL` (default `4h`) and the agent is killed when the pod exits. Put
host aliases in `home/.ssh/config`. Like broker pods, these need SELinux to let them reach the
agent's socket: see [SELinux](#selinux).

### SELinux

Pods normally run with SELinux labelling (podman's `container_t`), and their mounts are relabelled
for it (`:z`). Pods that use a socket on the host, the broker's or a per-run ssh-agent's, are the
exception: SELinux stops a container from connecting to a socket of one of your own processes, so
by default these pods run with labelling off (`label=disable`, and their mounts aren't
relabelled).

`apod selinux install` lets them keep it. It loads a one-rule policy module,
[`selinux/apod.cil`](selinux/apod.cil), with sudo: containers (`container_t`) may connect to unix
sockets of unconfined processes. These pods then run as ordinary containers with relabelled
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

### Network

Pods use podman's default network (pasta). On some hosts (seen on Wi-Fi) pasta gives pods IPv6
only, so IPv4-only destinations such as Tailscale hosts are unreachable; `network` set to `host`
shares the host's network instead, including its localhost services.

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

A running pod can't gain mounts or connectors: exit and relaunch with the new options and
`-- --continue`, or `apod resume`.

## History and resume

Every launch is recorded in `~/.agent-pod/history.jsonl`: profile, folder, options, Claude session
id and terminal tab. Each new Claude run gets its own session id, and pods are labelled with
profile, folder and session.

`apod resume` offers only sessions that aren't running and have a saved conversation, and relaunches
with the same profile and options. If the current tab launched one of them, it resumes that one;
otherwise it lists them and asks. The tab id is the first set variable among `$APOD_PANE_ENV`
(default `HERDR_PANE_ID HC_SCOPE`). `--list` always asks, `--latest` takes the newest, and `--claude`
opens Claude's own picker.

## Images

`Containerfile` builds the base `apod` image (Fedora, Claude Code, opencode, gh, git, Node, Python).
Two examples build on it for specific projects:

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

## Writing a broker extension

Every broker feature is a **provider**: `gh`, `github`, `rad`, `sign` and `ssh` in `broker/`, and
your own in `~/.agent-pod/broker-extensions/*.py`. The broker loads its own providers first,
then yours in name order. If one of yours fails to load, the broker skips it and writes the error
to `~/.agent-pod/broker/broker.log`. Run `apod broker restart` after adding or changing one.
[`extensions/notify.py`](extensions/notify.py) is a complete, tested example: `apod-notify TEXT`
in the pod sends you a desktop notification. It's about 90 lines with comments.

### How a request flows

```
pod: a client (a shim, git config, ssh, your own script)
  │  HTTP over $APOD_BROKER_SOCK (/run/apod-broker/broker.sock, one socket per pod)
  ▼
broker: the route (method, path prefix) → your handler(h, sess, method)
  1. validate what the pod sent (it's untrusted)
  2. classify it: a Request with actions ("notify:send") and targets (("repo", "acme/widget"))
  3. check(sess, req): role allows every action and every target is in scope → go ahead;
     otherwise it waits for approval (TUI, apod broker allow), up to APOD_BROKER_ASK_TIMEOUT
  4. act on the host, reply to the pod
```

Nothing you add needs changes to `apod`. A provider tells `apod` what to set up in the pod when
the pod registers, and `apod` applies that generically.

### A provider

```python
# ~/.agent-pod/broker-extensions/hello.py
from core import Request, check, provider, read_body, rejected_msg

def handle(h, sess, method):                  # h: a BaseHTTPRequestHandler; reply with h.send_json
    name = read_body(h).decode()[:100]
    req = Request("hello", f"hello {name}")   # kind, and how the TUI and log show the request
    req.actions = ["hello:greet"]             # "<provider>:<action>", what role lists match
    req.targets = [("identity", name)]        # what it touches; checked against the scope
    ok, ev = check(sess, req)                 # blocks while it waits for approval
    if not ok:
        return h.send_json({"error": rejected_msg(req, ev)}, 403)
    return h.send_json({"greeting": f"hello {name}"})

class Hello:
    name = "hello"                            # broker.toml [hello], role list "hello"
    routes = [("POST", "/hello", handle)]     # method (GET, POST, CONNECT) and path prefix

    def configure(self, sess, cfg, reg, out): # at registration; cfg is broker.toml's [hello]
        if not cfg.get("enabled"):
            return False                      # not active for this pod
        out["context"].append("POST /hello on $APOD_BROKER_SOCK greets someone.")
        return True

provider(Hello())
```

The provider object:

| Attribute | |
|---|---|
| `name` | Required, unique. It names the `broker.toml` table, the role list and the action prefix. |
| `configure(sess, cfg, reg, out)` | Required. Runs when a pod registers. `cfg` is the provider's `broker.toml` table (`{}` if absent). `reg` is the registration (`profile`, `project`, `remotes`, …). Return whether the provider is active for this pod. |
| `routes` | `[(method, path prefix, handler)]`, matched in registration order. `/whoami` is the core's. |
| `listen(sess)` | Optional. Start extra servers in `sess.dir` (mounted in the pod), e.g. a socket that isn't HTTP; return them so the core stops them with the session. `ssh` uses this for its agent. |
| `close(session_id)` | Optional. Clean up when a pod's session ends. |
| `keys(cfg)` | Optional. The keys a `broker.toml` table names, `[(path, confirm)]`, for `apod broker keys` to list and load before any pod starts. |

What `configure` can add to `out`. `apod` applies each of these to the pod:

| Key | |
|---|---|
| `out["mounts"]` | `[host path, pod path, "ro"]`, e.g. a client script into `/usr/local/bin`. |
| `out["env"]` | Environment variables for the pod. |
| `out["git_config"]` | `[key, value]` pairs, passed as `GIT_CONFIG_*`. `{forward}` in a key becomes the pod's git forwarder URL. |
| `out["context"]` | Sentences for the agent's system prompt: what's available and how to use it. |
| `need_key(sess, out, path)` | Not a key, a function: the broker's ssh-agent must hold this key; `apod` runs `ssh-add` at launch if it doesn't. Returns the public key line and blob. |

### The core's API

All from `core`:

- **`Request(kind, display)`** has `.actions`, a list of `"<provider>:<action>"`. The role
  lists in `roles.toml` match the part after the colon, with shell-style patterns. It also has
  `.targets`, a list of `(kind, value)` pairs. A request with no targets is checked against the
  role only.
- **`check(sess, req, waiting=None, extra=None)`** returns `(ok, event)`. If the request has to
  wait, `waiting(event)` is called first, so you can tell the pod: `github` and `rad` show git a
  "remote:" line this way. `extra` is merged into the event. Set `event["exit"]` once you've
  acted, so the TUI shows a failure.
- **`rejected_msg(req, ev)`** and **`waiting_msg(req, ev)`** produce the messages the pod should
  see. They tell the agent how the user can approve the request.
- **Scope kinds** are `repo` (`OWNER/REPO`), `owner`, `project` (`project:OWNER/N`), `rad`
  (`rad:RID`), `host` (`ssh:[USER@]HOST`) and `identity` (`email:ADDRESS`). Add your own with
  **`scope_kind("url", "url:")`**: users then write `url:https://docs.example.com/*` in `scope`
  or `--scope`, and a target `("url", …)` is in scope when it matches an entry, case-insensitively,
  with shell-style patterns. A target `("unscoped", "what")` is never in scope; approving it for
  the session remembers it.
- **`HARD_DENY`** is a list of action patterns that are never allowed, whatever the role says.
- **The session (`sess`)** has `.profile`, `.role`, `.project`, `.scope`, `.identity` (the
  profile's git name and emails), `.login` (the `gh` account, if any), `.title()` (the agent
  session's name) and `.dir` (the session's directory, `/run/apod-broker` in the pod).
  `.cfg[name]` is yours for per-session state, saved with the session so it survives a broker
  restart. Keys starting with `_` are the core's.
- **Helpers**: `read_body(h)`, `h.send_json(obj, status)`, `send_text(h, text, status)`,
  `BrokerError`, `AGENT` (the broker's ssh-agent socket, for `SSH_AUTH_SOCK`), `BDIR` (the
  broker's state directory; use `BDIR / "tmp"` for scratch files).

### Roles

An extension's actions belong in a role list named after the provider. A role in
`~/.agent-pod/roles.toml` with a built-in role's name adds to it, so to let `dev` pods use an
extension:

```toml
[roles.dev]
notify = ["send"]
```

Or approve the first request "for the role" in the TUI (`r`), which saves the grant in
`~/.agent-pod/broker/grants.json`. The agent's system prompt and `gh auth status` list an active
provider's allowed actions with the rest.

### The pod side

The pod reaches the broker only through `$APOD_BROKER_SOCK`: HTTP over a unix socket. You can
connect to it in either of two ways:

- **Mount a client.** [`extensions/apod-notify`](extensions/apod-notify) is about 30 lines of
  Python. The default image has Python, and the `gh` shim needs it anyway. `curl --unix-socket` works too,
  if your image has curl.
- **Point an existing tool at the broker.** The built-in providers do this:
  - `sign` sets `gpg.ssh.program`;
  - `github` and `rad` set `url.<forwarder>.insteadOf`;
  - `ssh` sets `SSH_AUTH_SOCK` and a `ProxyCommand`.

  The pod-side programs are all one script, `broker/pod-shim`, which dispatches on the name it's
  run as.

### Rules of thumb

- **Treat everything from the pod as hostile.** Validate it, cap its size, never pass it to a
  shell, and never let it name a path on the host. The `gh` provider refuses arguments that name
  host files, and it receives the contents of files from the pod rather than reading them on the
  host.
- **Classify before acting, and classify by what the request does, not what it says.** For
  example, `gh` resolves GraphQL node ids on GitHub to find the real repository. A link in an
  issue body is not a target.
- **Keep requests short.** An agent's command usually times out at 2 minutes, and the approval
  wait defaults to 90 seconds. Don't stack a long action on top of it.
- **Test like `tests/test_broker.py`.** Use a throwaway `APOD_ROOT`, start `apod-broker serve`,
  `register` a session with a `--profile-dir` holding your `broker.toml`, and call the session's
  socket. The example extension's test, `test_extension`, is a template.

## Security

[SECURITY.md](SECURITY.md) has the threat model, what apod guarantees and assumes, what it
doesn't protect against, a hardening checklist, and how to report a vulnerability. In short:

- The container is the boundary. The agent runs with no permission prompts and sees only the
  project folder, the profile's home and the profile's mounts.
- With the broker, pods hold no token or key. Every GitHub, Radicle, signing and ssh request is
  checked against the role and scope, and anything else waits for your approval.
- Pods can still damage what's in scope, send what they can read over the network, and write
  files in the project (`.git/hooks`, `.claude/settings.json`) that run when you use it on the
  host. Give pods their own worktrees, one per pod.

## Contributing

The repository is on Radicle and mirrored to GitHub. To push to both from a clone with both
remotes (`rad` and `origin`):

```sh
git config alias.sync '!b=$(git branch --show-current); git push rad "$b" && git push origin "$b"'
git sync
```

## Licence

Dual-licensed under MIT ([LICENSE-MIT](LICENSE-MIT)) or Apache-2.0
([LICENSE-APACHE-v2](LICENSE-APACHE-v2)), at your option.
