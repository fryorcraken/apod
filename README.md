# apod: agents in podman

`apod` runs coding agents (Claude Code, opencode, or a plain shell) in rootless podman containers,
one **profile** per identity. Each profile has its own GitHub token, git identity, Claude login,
mounts, image and network, so an agent can run with no permission prompts while the container,
not the agent, is the security boundary.

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
`apod token` and `apod google-auth`.

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
apod token bot                      # fine-grained GitHub token, prompted without echo
apod bot ~/src/some-repo            # Claude Code in a pod; first run: log in to Claude once
```

```sh
apod bot -a opencode ~/src/some-repo        # opencode instead
apod bot -a bash .                          # a shell in the pod (gh and git as the profile)
apod bot ~/src/some-repo -- --continue      # anything after -- goes to the agent
```

Only the project folder is mounted (read-write). `gh` and `git push` use the profile's token:
GitHub SSH remotes, including `github-*` host aliases from `~/.ssh/config`, are rewritten to HTTPS
inside the pod. Claude is told at startup what is mounted and what isn't.

## Commands

```
apod <profile> [-a claude|opencode|bash] [-i IMAGE] [-c CONNECTORS] [--ro PATH]... [--rw PATH]... [dir] [-- agent args]
apod resume [--list|--latest|--claude] [dir]   resume a stopped Claude session started in dir
apod history                                   folders and profiles launched, most recent first
apod ps                                        running pods: profile, session, status, folder
apod profiles                                  profiles, and whether each has a GitHub token / Google login
apod token <profile> [--rm]                    set, replace or remove a profile's GitHub token
apod google-auth <profile>                     sign a profile in to Google for google-docs-mcp
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
| `github` | Use another profile's GitHub token (`gh-<that profile>`). |
| `ssh-key` | Path to a passphrase-protected private key, served from a per-run ssh-agent. |
| `ssh-confirm` | If present, confirm every use of that key on the host. |
| `connectors` | Default claude.ai connector groups (`-c` replaces them). |
| `google-credentials.json` | OAuth client for google-docs-mcp, instead of the checkout's. |

The GitHub token is the podman secret `gh-<profile>`; without one, `gh` and HTTPS pushes are
unauthenticated.

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
host aliases in `home/.ssh/config`. These pods run without SELinux labelling, which would otherwise
block the socket.

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

## Security notes

- The container is the boundary: the agent runs with no permission prompts, and sees only the
  profile's home, the project folder and the profile's mounts.
- Pods can write `.git/hooks` and `.claude/settings.json` in a mounted repo, which then run on the
  host. Give pods their own worktrees, or point `core.hooksPath` at an empty folder on the host.
- Two pods on one checkout will collide; use one worktree per pod.
- Tokens should be fine-grained and scoped to what the profile needs.

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
