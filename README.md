# apod — agents in podman, one GitHub identity per profile

## One-time setup

```sh
podman build -t apod ~/src/rad/scripts/apod
ln -s ~/src/rad/scripts/apod/apod ~/.local/bin/apod
```

## Per profile (e.g. `marclaw`, `fryorcraken`)

```sh
# GitHub token (fine-grained, scoped): paste it, then Ctrl-D
podman secret create gh-marclaw -

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

## Notes

- The project dir is mounted read-write, plus any `mounts` entries. Give each pod its own worktree; two pods on one checkout will collide.
- Pods can write `.git/hooks` and `.claude/settings.json` in the mounted repo, which then run on the host.
  Use per-pod worktrees, or set `core.hooksPath` to an empty dir in your host git config.
- Not mounted yet: `/nix/store`, Android SDK/NDK. Add per project when needed.
- Each profile has its own agent list; use herdr on the host to see all pods.
