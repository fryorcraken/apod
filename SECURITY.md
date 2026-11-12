# Security

This file covers what apod is meant to protect, what it actually guarantees, what it assumes,
and what it doesn't cover. If you rely on apod to contain an agent, read the
[limitations](#what-apod-does-not-protect-against).

## Reporting a vulnerability

Please report security issues privately, not in a public issue or patch: use GitHub's private
vulnerability reporting on <https://github.com/fryorcraken/apod> (Security → Report a
vulnerability). Include the apod commit, what an agent in a pod can do that it shouldn't, and
the steps to reproduce it.

## Threat model

**Trusted:** you, your host account, the host's software (podman, the kernel, `gh`, git, ssh,
Radicle), and the apod checkout the broker runs from.

**Not trusted: the agent and everything in the pod.** An agent can be wrong, or it can be
steered by what it reads. A README, an issue, a web page or a dependency can carry instructions
(prompt injection). apod assumes anything running in the pod may try to:

- read or change files outside the project;
- use credentials beyond what the task needs;
- act on GitHub, Radicle or hosts it wasn't meant to touch;
- mislead you into approving something.

**The goal:** the agent runs with no permission prompts, and a misbehaving one is limited to:

- the project folder and what the profile mounts;
- whatever the profile's role and scope allow (with the broker);
- anything you approve, which you see as it happens.

apod does not try to make the agent's work *correct*. It limits how much damage the agent can
do and leaves a record of what it did.

## What apod provides

### The container

- **Each pod is a rootless podman container,** running as your uid inside a user namespace
  (`--userns=keep-id`). It's removed when the agent exits.
- **Files on the host:** the pod sees only these, and nothing else from your home or the
  filesystem:
  - the project folder (read-write);
  - the profile's own home (`~/.agent-pod/<profile>/home`, read-write);
  - its session's work log folder (`~/.agent-pod/worklog/<id>`, read-write);
  - the profile's `mounts`, read-only unless marked `rw`;
  - your `~/.claude/CLAUDE.md` and `~/.claude/skills`, read-only.
- **Processes:** the host's processes, other pods and their files are not visible.
- **Profiles are separate:** each has its own home, so its own Claude login, history and
  settings. One profile's pod can't read another's.
- **The agent is told what it can see:** Claude's system prompt lists the mounts and, with the
  broker, the role and scope. That's for the agent's benefit, not a security control.

### Credentials stay on the host (broker)

With a `broker.toml`, a pod holds no GitHub token and no ssh, signing or Radicle key:

- **GitHub:** `gh` and git's github.com remotes go to the host's broker, which uses the host
  `gh` login the profile names (`[gh] user`). The token never enters the pod. A profile that names
  no account gets no GitHub access through the broker; it never falls back to your active login.
- **Keys** (ssh, commit signing, Radicle) are held by the broker's own ssh-agent on the host. The
  pod gets a filtering agent that offers only the session's ssh keys. It signs only ssh login
  requests, never arbitrary data, and pods can't add, remove or lock its keys.
- **Extensions are off unless set up.** Every extension is loaded, but is on for a pod only when
  its `broker.toml` table sets it up (an account, a key, `[rad] autodetect`), it isn't
  `enabled = false`, and the pod's scope has something of its kind. A folder's `rad://` remote
  alone doesn't give a pod Radicle, and keys are unlocked only for roles that use them.
- **The broker listens only on unix sockets.** There's a directory per pod, mounted into that pod
  alone, and an admin socket in `~/.agent-pod/broker/` (mode 0700). There is no TCP listener.
- **The pod's directory is the pod's.** It's mounted read-write, so the broker keeps nothing it
  trusts there: a session's role, scope and approvals are saved next to it, out of the pod's reach,
  and what the broker writes into it (ssh `known_hosts`, signers) only informs tools in the pod.

Without the broker, a profile's GitHub token is in the pod's environment (`GH_TOKEN`). An agent
can read it and use it for anything the token allows.

### Every request is checked

The broker turns each request into actions and targets, and checks them before acting:

- **Actions** are what the request does: `gh:pr create`, `github:push branch`, `git-sign:commit`,
  `ssh:login`.
- **Targets** are what it touches: a repository, a project board, a Radicle repo, a host, a git
  identity.
- **The checks:** the role must allow every action, and every target must be in the session's
  scope. Anything else waits for your approval, and it's refused if you don't answer in time
  (90 s by default).
- **Some actions are never allowed,** whatever the role or approvals say:
  - every `gh auth`, `gh config`, `gh extension`, `gh alias`, `gh ssh-key`, `gh gpg-key` and
    `gh codespace` command;
  - `gh repo deploy-key add`;
  - GraphQL subscriptions.
- **Requests are classified by what they do, not what they say:**
  - `gh` commands are parsed against `gh`'s own help, with aliases resolved. Risky flags, like
    `pr merge --admin`, count as their own actions.
  - REST calls are classified by method and path.
  - GraphQL is parsed down to its top-level fields. Node ids are looked up on GitHub to find the
    repository they belong to.
  - Links in titles and bodies are not targets.
- **git pushes are checked ref by ref.** Branch, default branch, tag, delete and patch are
  separate actions, and so is a force push (an update that isn't a fast-forward).
- **Signing:** commits and tags are signed only as the profile's git identity, and only in the
  repositories in scope, unless you approve otherwise. The broker sees the identity in the object
  it signs, but it learns the repository from the pod: a hostile agent can name an in-scope
  repository and use the signed commit elsewhere. Pushing it is still checked against the scope.
- **ssh:** the agent signs a login only for a host in scope. The broker identifies the host by the
  host key OpenSSH binds to the session (`session-bind@openssh.com`, verified by the real agent),
  matched against your `known_hosts`. Connections go through the broker, which connects only to
  hosts in scope, on their ssh port, or to ones you approve.
- **A provider answers only the pods it's set up for.** A pod whose profile names no GitHub
  account, or has no `[ssh]`, gets nothing from those providers even if it calls the broker
  directly.

### Nothing from the pod runs on the host

- **`gh` runs on the host with no access to the pod's files.** It gets an empty working directory,
  a config and home of its own, and no editor, browser or prompts. The shim sends the contents of
  the files a command names; the broker writes them into that directory. Arguments that name host
  paths are refused.
- **Radicle pushes land in a staging repository the broker owns,** with hooks disabled, and the
  broker pushes on from there. Nothing from the pod's repository (hooks, config) runs on the host.
- **Fetches** are served from the host's Radicle storage, limited to the repository asked for, or
  proxied to GitHub.

### Auditing

- **Every request is logged** to `~/.agent-pod/broker/log.jsonl`: what was asked, the actions and
  targets, the decision and the exit code.
- **You can follow it live:** in `apod broker` (the TUI), or with `apod broker log -f`.
- **You can withdraw approvals:** approvals for a session end with the pod, and you can remove a
  scope entry or grant in the TUI. Approvals for a role are kept in
  `~/.agent-pod/broker/grants.json` and apply to every profile with that role.

## What apod assumes

- **The host is trusted and up to date,** especially the kernel and podman: container isolation
  is only as strong as they are.
- **The apod checkout and your extensions are trusted.** The broker runs from the checkout, with
  its shipped extensions (`extensions/`), and runs yours from `~/.agent-pod/broker-extensions/`,
  where a file can replace a shipped extension. Pods can't write to any of them, as long as you
  don't mount them into a pod.
- **You read what you approve.** Approve by the actions and targets the broker lists, not by the
  one-line summary, which the agent's arguments partly shape.
- **The host's `gh` logins are the accounts you mean pods to act as.** A role limits what a
  pod can do with an account. It doesn't limit the account itself.
- **Scope entries are as narrow as you write them.** `gh:fryorcraken/*` covers every repository of
  that account. `ssh:nas` allows one user on nas, the one your ssh config logs in as (its
  `User`, else your user name); `ssh:root@nas` allows root.

## What apod does not protect against

- **Anything on a host a pod can ssh to.** An ssh login gives the pod a shell: it can run any
  command there, as that account, and the broker sees none of it. Adding a host to a pod's scope
  hands it everything that account can do there; only the list of hosts (and users) is
  enforced. Use an unprivileged account per host, and scope pods to the hosts their job needs.
- **Damage within the scope.** Anything the role allows on a target in scope happens without
  asking. Examples: a `dev` pod can push branches, open PRs and comment on issues in those
  repositories, and a `pm` pod can edit issues and boards. Give each profile the narrowest
  role and scope that fit the job.
- **The project folder is writable, and you run things from it on the host.** An agent can write
  `.git/hooks`, `.git/config`, `.envrc`, `.claude/settings.json`, `Makefile`s and other files
  that run code when *you* later use the repository on the host. Give pods their own worktrees,
  point `core.hooksPath` at an empty directory, and review what a pod changed before running
  anything from it.
- **Work logs are written by pods.** They're the agent's account of its work, not a record (the
  broker's log is), and text in them can try to steer whatever agent later reads them. Review
  them in a pod with the logs mounted read-only, not with an agent on the host.
- **Network egress.** Pods can reach the internet, so a pod can send whatever it can read (the
  project, its mounts, its own home) anywhere. With `network = host`, a pod can also reach the
  host's localhost services and your LAN or Tailscale hosts directly. The ssh agent still signs
  logins only for hosts in scope.
- **Credentials that live in the pod's home:**
  - the profile's Claude login;
  - its Google login for google-docs-mcp, with the scopes it was granted;
  - claude.ai connectors you enable with `-c`;
  - without the broker, the GitHub token.

  An agent in the pod can use or exfiltrate all of these.
- **What happens after an ssh login.** Once logged in to a host in scope, the agent can do
  anything that account can do there. Use a dedicated account on the host if that matters.
- **What signatures mean.** A commit signed by the broker's key says that a pod using that key
  made it, not that you reviewed it. If "Verified" on GitHub should mean "reviewed by a human",
  use a separate signing key for pods.
- **Radicle identity.** Pushes go out as the host's Radicle identity unless `[rad] home` points at
  a separate one, so a pod's patches look like yours.
- **Reads within the scope.** Roles allow broad reads on targets in scope, such as
  `api GET /repos/{repo}/*`, and `github:fetch public` allows fetching any public repository.
  Private data in scope is readable. A read can also reach past the scope: a GraphQL query is
  scoped by its top-level fields, so one on an in-scope repository can follow links to others
  (its owner's repositories, a fork's parent), and REST lists such as forks do the same. Treat
  everything the account can read as readable.
- **SELinux is off for broker pods unless you install apod's module.** By default they run with
  `label=disable`, because SELinux stops a container from connecting to the broker's socket; the
  same goes for pods with an `ssh-key`. `apod selinux install` loads a one-rule module that lets
  containers connect to sockets of unconfined processes, limited in practice to sockets mounted
  into a pod and served at level `s0` (the broker, per-run ssh-agents), so these pods keep SELinux
  labelling as ordinary containers. Other pods always
  keep it.
- **Denial of service.** There is no rate limit: a pod can flood you with approval requests or
  notifications. Deny them, and stop the pod.
- **Bugs in the broker.** The broker parses untrusted input from pods: HTTP, git pkt-lines,
  GraphQL, the ssh agent protocol, and `gh` arguments. It runs as you on the host. A parsing or
  classification bug can let a pod do more than its role allows. Likewise, an extension is
  only as careful as its author: read one before you install it. The rules extensions must
  follow (treat the pod's input as hostile, classify by effect, widen the scope only from the
  host's side, fail closed) are in [EXTENSIONS.md](EXTENSIONS.md#security-rules).
- **Two pods on one checkout** can interfere with each other. Use a worktree per pod.

## Hardening checklist

- Use the broker rather than tokens. Where you keep a token, make it fine-grained and limited to
  the repositories and permissions the profile needs.
- On SELinux hosts, run `apod selinux install` so broker and `ssh-key` pods keep SELinux
  labelling.
- Give each profile the narrowest role and scope it needs. Grant extra access per session
  rather than per role when it's a one-off.
- Give pods their own worktrees, and set `core.hooksPath` on the host for repositories that pods
  write to.
- Use a dedicated host account for agent ssh logins, and `ssh:USER@HOST` scope entries.
- Use a separate key for pods' commit signatures, and a separate Radicle identity if pods
  shouldn't publish as you.
- Keep `~/.agent-pod`, the apod checkout and `~/.agent-pod/broker-extensions` out of every pod's
  mounts.
- Review `~/.agent-pod/broker/grants.json` from time to time, and look at the log
  (`apod broker log`) after long unattended runs.
