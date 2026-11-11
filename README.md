# apod: agents in podman

`apod` runs coding agents (Claude Code, opencode, or a plain shell) in rootless podman containers.
With the host's [broker](#broker), the pod holds no credentials, and three things decide what
it can do (see [Concepts](#concepts)):

- its **profile** is *who* acts: the git identity, the GitHub account and the keys;
- its **role** is *what* it may do: a preset list of actions, such as open a PR, push a branch,
  sign a commit or log in over ssh;
- its **scope** is *where*: the repositories, project boards and hosts those actions may touch.

Anything outside them waits for your approval. The agent runs with no
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
`apod <profile> [--role ROLE] [--scope SCOPE]... <project>`. In short: the profile is *who*, the
role is *what*, the scope is *where*.

| Term | What it is | What it carries | Where it's set |
|---|---|---|---|
| **Profile** | *Who* acts: an agent identity, and what it can see. One per identity or kind of job, e.g. `bot`, `ops`. | Its git identity, name and email (`env`; the only place it's set). The keys it acts with (`broker.toml`: `[sign] key`, `[ssh] key`) and its GitHub account (`[gh] user`). Its home (`home/`): Claude or opencode login, history and settings, Google login; every pod of the profile shares it, at the same time too. Its mounts, image, network, volumes and connectors. How it reaches GitHub and hosts: either a token (`apod token`, `ssh-key`) or a `broker.toml` with its default role and scope, its GitHub account (`[gh] user`, or `--gh-user` for one launch) and its keys. | `~/.agent-pod/<profile>/` |
| **Role** | *What* a pod may do without asking: a preset of actions, since the full list is long. The same for every profile that uses it. | One list per broker provider: `gh` commands, git pushes to GitHub and Radicle, signing commits and tags, ssh logins. No identity, credentials or targets. | Built in (`roles.toml`: `reader`, `dev`, `maintainer`, `pm`, `ops`); `~/.agent-pod/roles.toml` adds to them. A profile's `role =`, or `--role`; default `reader`. |
| **Scope** | *Where* those actions may happen, as `KIND:VALUE` entries; each extension declares its kinds. | `gh:OWNER/REPO`, `gh:OWNER`, `gh-project:OWNER/N`, `rad:RID`, `ssh:[USER@]HOST`, `git-local:PATH`, and your extensions' kinds (`apod broker scopes` lists them). | The project's GitHub and Radicle remotes and its folder, automatically; the profile's `scope` and `[ssh] hosts`; `--scope`. |
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
| `env` | Required. The git identity, and the only place it's set: `GIT_AUTHOR_NAME` and `GIT_AUTHOR_EMAIL` (`GIT_COMMITTER_*` default to them). apod also sets `user.name` and `user.email` to match, over any `.gitconfig` in the pod's home or the project. Other variables here go into the pod's environment too. |
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
scope = ["gh:logos-co/*", "gh-project:logos-co/5"]   # plus the project's GitHub and Radicle remotes

[gh]
user = "fryorcraken"            # host gh account (gh auth login --hostname github.com); required for gh

[sign]
key = "~/.ssh/id_apod_broker_sign" # its .pub next to it; roles that sign turn commit.gpgsign on (auto = false: off)

[ssh]
key = "~/.ssh/id_apod_broker" # or keys = [...]; confirm = true: confirm each use (needs openssh-askpass)
hosts = ["devbox", "root@nas"]     # a host's name in the host's ~/.ssh/config, any user or USER@
proxy = true                    # connect through the broker (the default); false: the pod connects itself

[rad]
home = "~/.radicle"             # RAD_HOME of the identity pushes land as (default $RAD_HOME or ~/.radicle)
```

```sh
apod work ~/src/org/repo                           # as broker.toml says
apod work --role maintainer --scope 'gh:fryorcraken/*' ~/src/org/repo
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
  repositories in scope, fetch; ssh to the hosts in scope.
- `dev`: reader plus push branches (not the default branch), force pushes included, open Radicle
  patches, create and edit PRs, comment, create issues; sign commits.
- `maintainer`: dev plus merge and review PRs, edit and close issues, re-run CI, fork and create
  repositories, push tags and delete branches.
- `pm`: reader plus project boards (list, view, add and move items, add fields, templates) and
  editing, creating and commenting on issues.
- `ops`: ssh to the hosts in scope and sign commits; fetch only.

**Scope.** The project folder's GitHub and Radicle remotes (and the folder itself, for signing), then
`broker.toml`'s `scope` (and `[ssh] hosts`) and `--scope`. Each entry is `KIND:VALUE`, where the
kind belongs to an extension, and values take shell-style patterns (`*`). The shipped kinds:

| Entry | Extension | Covers |
|---|---|---|
| `gh:OWNER/REPO`, `gh:OWNER/*` | gh, github | A repository: gh commands, API calls and git pushes and fetches. |
| `gh:OWNER` | gh | The owner itself: owner-level reads, its project list, creating repositories there. |
| `gh-project:OWNER/NUMBER`, `gh-project:OWNER/*` | gh | A project board. |
| `rad:RID` | rad | A Radicle repository. |
| `ssh:HOST`, `ssh:USER@HOST` | ssh | A host as named in your host's ssh config, for any user or that user. |
| `git-local:PATH` | sign | Signing in a repository with no GitHub or Radicle remote, by its folder. The project folder (and the repositories under it) is in scope. |

`apod broker scopes` lists the kinds the loaded extensions provide. An entry of a kind no
extension provides is ignored, with a warning at launch. The older forms (`OWNER/REPO`, `OWNER`,
`project:OWNER/N`) still work, with a warning naming the new form. Forks and
repositories the session creates join its scope. The TUI and the log show targets the same way,
so what you see out of scope is what you'd add.

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
  `push force` (a branch update that isn't a fast-forward, added to the ref's own action), and for
  Radicle `push patch` (`HEAD:refs/patches`, push options such as `-o patch.message=...` passed
  on). `--force-with-lease` and `--force` look the same to the broker: the lease is checked by git
  in the pod, against its remote-tracking branch. For GitHub, the broker reads the commits in the
  push and asks GitHub about the ones it already has; a push it can't show to be a fast-forward
  counts as a force push. A Radicle push lands in
  a staging repository the broker owns first, then the broker pushes it on with the host's
  `git-remote-rad`; nothing from the pod's repository runs on the host.
- sign: the three parts each have their place.
  - **Who (profile):** `[sign] key` is the key, and the signature vouches for the profile's git
    identity. A commit or tag whose author, committer or tagger is anyone else adds the action
    `sign:as other`, which no built-in role allows.
  - **What (role):** `sign:commit` and `sign:tag`. `dev`, `maintainer` and `ops` have them, and
    their pods sign every commit and tag (`commit.gpgsign`, `tag.gpgsign`). Other roles don't
    sign by default; `git commit -S` waits for your approval.
  - **Where (scope):** the repository, by the remote the branch pushes to (`gh:OWNER/REPO`,
    `rad:RID`), so the scope you give for pushes covers signing too. A repository with no such
    remote counts by its folder (`git-local:PATH`). The pod reports which repository it's
    signing in, so this keeps an agent to its scope rather than stopping a hostile one; the
    push is checked separately.
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

### Signing commits

Pods sign commits and tags with an SSH key that stays on the host (the `sign` extension).
Setting it up for a profile:

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

3. **The key in `broker.toml`**, in the profile's file or in `~/.agent-pod/broker.toml` for every
   profile:

   ```toml
   [sign]
   key = "~/.ssh/id_apod_broker_sign"   # its .pub next to it
   confirm = true                       # optional: confirm each signature on the host (needs openssh-askpass)
   auto = false                         # optional: don't turn on commit.gpgsign, even for roles that sign
   ```

4. **A role that signs, and a scope that covers the repository.**
   - Role: `dev`, `maintainer` and `ops` sign every commit and tag. With `reader` or `pm`,
     `git commit -S` waits for your approval. Your own roles get it with `sign:commit` and
     `sign:tag`.
   - Scope: the repository's push remote (`gh:OWNER/REPO` or `rad:RID`). The project folder is
     always in scope, as `git-local:PATH`, for repositories without a GitHub or Radicle remote.
   - Anything else waits for approval: another repository, or a commit by someone other than the
     profile's identity (`sign:as other`).

5. **The public key on GitHub, as a signing key** (not an authentication key). Add it on the host
   to the account whose email is in `env`:

   ```sh
   gh ssh-key add ~/.ssh/id_apod_broker_sign.pub --type signing --title "apod pods"
   ```

The first launch after that loads the key into the broker's agent and asks for its passphrase.
After a broker restart, run `apod broker keys`. Then, in a pod:

```sh
git commit --allow-empty -m "signing test" && git log --show-signature -1   # Good "git" signature for bot@example.com
```

`git log --show-signature` and `git verify-commit` work in the pod because the broker gives it an
allowed-signers file with the profile's key and emails (`gpg.ssh.allowedSignersFile`). On the
host, verify with your own allowed-signers file, or on GitHub.

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

Everything the broker does for pods is an extension: a Python file that registers providers
(what a pod may ask for, and how it's checked) and scope kinds (`gh:`, `ssh:`...). The shipped
ones are in [`extensions/`](extensions/); yours go in `~/.agent-pod/broker-extensions/`, where a
file with a shipped one's name replaces it and an empty one turns it off.

[EXTENSIONS.md](EXTENSIONS.md) is the guide: how a request is checked, a tutorial (an extension
that lets pods restart a host service), the API reference, security rules for extension authors,
and how to test and debug one.

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
