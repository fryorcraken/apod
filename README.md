# apod — agents in podman, one GitHub identity per profile

## One-time setup

```sh
podman build -t apod ~/src/rad/scripts/apod
ln -s ~/src/rad/scripts/apod/apod ~/.local/bin/apod
# zsh completion (oh-my-zsh puts $ZSH_CUSTOM/completions on $fpath); then: rm -f ~/.zcompdump*; exec zsh
ln -s ~/src/rad/scripts/apod/_apod ~/.oh-my-zsh/custom/completions/_apod
```

In herdr, apod sets `HERDR_AGENT` on the podman process so herdr recognises the agent in the pane.

## Per profile (e.g. `marclaw`, `fryorcraken`)

```sh
# GitHub token (fine-grained, scoped): prompts without echo, stores or replaces podman secret
# gh-marclaw and prints the token's GitHub account. `apod token marclaw --rm` deletes it.
apod token marclaw
apod profiles        # list profiles and whether each has a token

# git identity for commits made in this pod
mkdir -p ~/.agent-pod/marclaw
cat > ~/.agent-pod/marclaw/env <<'EOF'
GIT_AUTHOR_NAME=marclawclaw
GIT_AUTHOR_EMAIL=REPLACE_ME
GIT_COMMITTER_NAME=marclawclaw
GIT_COMMITTER_EMAIL=REPLACE_ME
EOF

# first run: log in to Claude (and/or opencode) once; it persists in the profile's home
apod marclaw ~/src/logos-co/logos-docs
```

## Use

```sh
apod marclaw ~/src/logos-co/logos-docs              # Claude Code, no permission prompts
apod fryorcraken -a opencode ~/src/logos-co/rfp          # opencode as you
apod marclaw -a bash .                               # a shell in the pod (gh, git as that identity)
```

## Extra mounts

Optional `~/.agent-pod/<profile>/mounts`: one path per line, read-only unless prefixed `rw`.
The project dir is always mounted read-write, even when it sits inside a read-only entry.

```
# ~/.agent-pod/marclaw-logos/mounts
~/src/logos-co
~/src/logos-blockchain
rw ~/src/logos-co/tmp
```

For one run only, add files or folders on the command line (repeatable, before `--`):

```sh
apod marclaw --ro ~/notes/spec.md --ro ~/src/other-repo --rw ~/scratch ~/src/marclawclaw/research-cross-chain-dex
```

`-m` is short for `--ro`. A command-line entry overrides the mounts file's mode for the same path,
and a command-line path that doesn't exist is an error.

The first launch relabels each mounted tree for SELinux (`:z`), which can take a while on big trees.

Claude is told at startup (via `--append-system-prompt`) which paths are mounted, read-write or
read-only, and that nothing else from the host is available. opencode doesn't get this yet.

## Images

The image is `-i IMAGE` for one run, else the first line of `~/.agent-pod/<profile>/image`, else
`$APOD_IMAGE`, else `apod`. `logos-toolchain` adds Rust 1.98.1 (LEZ's `rust-toolchain.toml`) and
RISC Zero (rzup: cargo-risczero/r0vm 3.0.5, risc0 rust 1.91.1, cpp 2024.1.5) under `/opt`:

```sh
podman build -t logos-toolchain -f ~/src/rad/scripts/apod/Containerfile.logos-toolchain ~/src/rad/scripts/apod
echo logos-toolchain > ~/.agent-pod/marclaw-logos/image
```

Rebuild it after rebuilding `apod`, or when LEZ bumps its Rust or risc0 versions. Proving is CPU-only
in a pod; use `RISC0_DEV_MODE=1` for tests.

## claude.ai connectors

Connectors (Notion, Google Docs, …) come with the Claude account, not the pod, and act as you.
apod disables them unless you enable groups for the run with `-c` (comma-separated, repeatable):

```sh
apod marclaw -c notion ~/src/logos-co/logos-docs
apod marclaw -c gdoc,notion -- --continue
```

Groups: `notion`, `gdoc` (Docs, Drive, Sheets), `todoist`, `canva`, `lovable`, `utsjitsu`, `claudedocs`.
A profile's `connectors` file (e.g. `~/.agent-pod/jitsu/connectors` containing `gdoc,utsjitsu`) sets
its defaults; `-c` replaces them for the run and `-c none` turns them off.
Other known connectors are denied; a PreToolUse hook also blocks any claude.ai connector tool not
enabled, including ones added to the account later. Don't set `disableClaudeAiConnectors` in a
profile's settings: `true` anywhere wins and `-c` would stop working.

### google-docs-mcp (local)

`-c google-docs-mcp` runs `~/src/fryorcraken/google-docs-mcp` (override with `APOD_GDOCS_MCP`) in the
pod from a read-only mount; its OAuth client comes from the checkout's `credentials.json`. Each
profile signs in to Google once, on the host where the browser is:

```sh
apod google-auth jitsu                       # token saved under ~/.agent-pod/jitsu/home/.config/google-docs-mcp/
apod jitsu -c google-docs-mcp ~/src/franckjitsu/teaching-aid
```

Scopes are `docs,sheets,slides,drive` (no Gmail/Calendar; override with `APOD_GDOCS_SCOPES`, then
re-run `apod google-auth`). `MCP_LAZY_TOOLS=1` keeps its tool catalog small. It can't be combined
with `gdoc`.

## Changing mounts or connectors mid-session

A running pod can't gain mounts or connectors. Exit and relaunch with the new options, resuming
the conversation: `apod marclaw --rw ~/new/dir -c notion <project> -- --continue`.

## Notes

- The project dir is mounted read-write, plus any `mounts` entries. Give each pod its own worktree; two pods on one checkout will collide.
- Pods can write `.git/hooks` and `.claude/settings.json` in the mounted repo, which then run on the host.
  Use per-pod worktrees, or set `core.hooksPath` to an empty dir in your host git config.
- Not mounted yet: `/nix/store`, Android SDK/NDK. Add per project when needed.
- Each profile has its own agent list; use herdr on the host to see all pods.
