# Writing a broker extension

The broker's core (`broker/`) handles sessions, roles, scope, approvals and the log. Everything a
pod can do through it (`gh`, git to GitHub and Radicle, commit signing, ssh, notifications) is an
**extension**: a Python file that registers providers and scope kinds. This guide is for writing
one. For using the shipped ones, see the [README](README.md#broker).

- [Where extensions live](#where-extensions-live)
- [How a request is checked](#how-a-request-is-checked)
- [Tutorial: restart a host service](#tutorial-restart-a-host-service)
- [Testing](#testing)
- [Reference](#reference)
- [Security rules](#security-rules)
- [Debugging](#debugging)

## Where extensions live

- **Shipped:** [`extensions/`](extensions/) in the apod checkout: `gh`, `github`, `rad`, `sign`,
  `ssh` and `notify`. They're defaults, loaded like any other extension.
- **Yours:** `~/.agent-pod/broker-extensions/*.py`.
  - A file with a shipped extension's name (`ssh.py`) replaces it.
  - An empty file with that name turns it off.
  - A name can't be one of Python's own modules (`json.py`) or the broker's (`api`, `core`,
    `gitwire`, `tui`).

The shipped names load first (your file, where you replace one), then the rest, each in name
order. Extensions import each other by name: `github` does `from gh import gh_query`, so a
replacement `gh.py` is the one `github` gets, and it has to keep those functions.

An extension that fails to load is skipped. What it registered before failing is undone, and the
error goes to `~/.agent-pod/broker/broker.log`. Run `apod broker restart` after adding or
changing one; `apod broker scopes` lists the scope kinds and which file each extension came from.

Import from `api` (the [reference](#reference) lists every name). `api.API_VERSION` changes
when one of them changes in a way that breaks extensions. Anything else in `core` is the
broker's own and may change at any time.

## How a request is checked

```
pod: a client (your script, a shim, git config, ssh)
  │  HTTP over $APOD_BROKER_SOCK (/run/apod-broker/broker.sock, one socket per pod)
  ▼
broker: the route (method, path) → your handler(h, sess, method)
  1. read and validate what the pod sent: it's untrusted
  2. classify it: a Request with actions ("svc:restart") and targets (("unit", "web.service"))
  3. check(sess, req):
       - an action is never allowed (HARD_DENY, the role's deny)  → refused
       - the role allows every action and every target is in scope → go ahead
       - otherwise it waits for the user (apod broker, or apod broker allow ID) for up to
         APOD_BROKER_ASK_TIMEOUT (90 s), then it's refused
  4. act on the host, reply to the pod
```

- **Actions** are `"<provider>:<action>"`. A role's list for the provider (`svc = ["status"]` in
  `roles.toml`) matches the part after the colon, with shell-style patterns.
- **Targets** are `(kind, value)` pairs, checked against the session's scope entries of that
  kind (`unit:web.service`). The kind is a scope kind some extension declared.
- **Approving** a request once lets that request through; "for the session" also adds its
  actions and targets to the session; "for the role" saves the actions in
  `~/.agent-pod/broker/grants.json`.
- **A request with no targets** is checked against the role only. A target `("unscoped",
  "what")` is the core's: it's never in scope, and approving it for the session remembers it. Use
  it for a request you can't scope.

The pod side needs no changes to `apod`: at registration a provider says what to set up in the pod
(mounts, environment, git config, a note for the agent), and `apod` applies it.

## Tutorial: restart a host service

This extension lets a pod ask about, or restart, a systemd user service on the host, for example
the dev server it's working on. Pods name the service; the scope says which ones they may touch,
and the role says what they may do to them.

**The extension:**

```python
# ~/.agent-pod/broker-extensions/svc.py
"""svc: pods ask about or restart the host's systemd user services. Actions: svc:status,
svc:restart. Targets: unit:NAME.service."""
import json
import re
import subprocess
from pathlib import Path

from api import BrokerError, Provider, Request, check, provider, read_body, rejected_msg, scope_kind, send_text

CLIENT = Path(__file__).resolve().with_name("apod-svc")
UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,99}")
COMMANDS = {"status": ["status", "--no-pager"], "restart": ["restart"]}


def unit_name(v):
    """web -> web.service; patterns in scope entries (dev-*) stay as written."""
    v = v.strip()
    return v if v.endswith(".service") or "*" in v else v + ".service"


def handle(h, sess, method):
    # 1. What the pod sent: small, the shape we expect, a unit name we'd pass to systemctl.
    try:
        body = json.loads(read_body(h, limit=4096))
        action, unit = body["action"], body["unit"]
    except (BrokerError, ValueError, KeyError, TypeError):
        return send_text(h, 'expected {"action": "status" or "restart", "unit": NAME}', 400)
    if action not in COMMANDS or not isinstance(unit, str) or not UNIT.fullmatch(unit):
        return send_text(h, "unknown action, or not a unit name", 400)
    unit = unit_name(unit)
    # 2. What it does, and what it touches.
    req = Request("svc", f"systemctl --user {action} {unit}")
    req.actions = [f"svc:{action}"]
    req.targets = [("unit", unit)]
    # 3. Allowed, approved, or refused.
    ok, ev = check(sess, req)
    if not ok:
        return send_text(h, rejected_msg(req, ev))
    # 4. Act: a fixed command, no shell, and "--" before the pod's word.
    try:
        r = subprocess.run(["systemctl", "--user", *COMMANDS[action], "--", unit], capture_output=True,
                           text=True, timeout=60, stdin=subprocess.DEVNULL)
        code, output = r.returncode, r.stdout + r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        code, output = 1, str(e)
    ev["exit"] = code  # the TUI shows it
    return h.send_json({"exit": code, "output": output[-4000:]})


class Svc(Provider):
    name = "svc"                         # [svc] in broker.toml, svc = [...] in roles, svc:... actions
    routes = [("POST", "/svc", handle)]

    def configure(self, sess, cfg, reg, out):
        if not cfg.get("enabled"):
            return False
        for u in cfg.get("units", []):   # the profile's own units are in scope for its pods
            sess.add_scope(("unit", unit_name(u)))
        out["mounts"].append([str(CLIENT), "/usr/local/bin/apod-svc", "ro"])
        out["context"].append("apod-svc status|restart UNIT asks the host about, or restarts, one of its "
                              "systemd user services (the ones in scope; others wait for the user).")
        return True


scope_kind("unit", provider="svc", parse=unit_name,
           help="unit:NAME (a systemd user service on the host, e.g. unit:web or unit:dev-*)")
provider(Svc())
```

**The pod's client**, next to it (`chmod +x` it):

```python
#!/usr/bin/env python3
# ~/.agent-pod/broker-extensions/apod-svc
"""apod-svc status|restart UNIT: ask the host's apod broker to run systemctl --user on UNIT."""
import http.client
import json
import os
import socket
import sys


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(os.environ["APOD_BROKER_SOCK"])


if len(sys.argv) != 3:
    sys.exit("usage: apod-svc status|restart UNIT")
c = UnixHTTP("localhost", timeout=300)  # long enough to wait for the user's approval
c.request("POST", "/svc", json.dumps({"action": sys.argv[1], "unit": sys.argv[2]}),
          {"Content-Type": "application/json"})
r = c.getresponse()
body = r.read().decode()
if r.status != 200:
    sys.exit(body.strip())
out = json.loads(body)
print(out["output"], end="")
sys.exit(out["exit"])
```

**Turn it on** for a profile, and let its role use it:

```toml
# ~/.agent-pod/work/broker.toml
[svc]
enabled = true
units = ["web"]          # in scope for every pod of this profile; --scope unit:db adds one for a launch
```

```toml
# ~/.agent-pod/roles.toml: adds to the built-in dev role
[roles.dev]
svc = ["status", "restart"]
```

**Try it:** `apod broker restart`, launch a pod of that profile, and in the pod:

```sh
apod-svc status web       # runs: in scope, and dev may
apod-svc restart db       # waits for you in apod broker: db isn't in scope
apod broker log -n 5      # on the host: both requests, with their actions and targets
```

What each part does:

- **`scope_kind("unit", ...)`** makes `unit:` a scope kind: users write `unit:web` in `scope` or
  `--scope`, and `parse` stores it as `web.service`, the form the handler's targets use.
- **`configure`** runs when a pod registers. It decides whether the provider is on for the pod,
  widens the session's scope from the host's own config (never from what the pod says), and tells
  `apod` what to set up in the pod.
- **The handler** reads, validates, classifies, checks, then acts. It never lets the pod choose
  the command, only one word that's been validated, and it answers in a form the client prints.

## Testing

[`tests/test_broker.py`](tests/test_broker.py) runs a real broker against a throwaway
`APOD_ROOT`, with stub programs first on `PATH`. The tutorial's extension is one of its tests
(`test_extensions_md_tutorial`, which loads the code above from this file). The pattern:

1. Write your extension into `APOD_ROOT/broker-extensions/`, and a stub for what it runs on the
   host (`systemctl` here) that records its arguments.
2. Start `apod-broker serve` with `APOD_ROOT` set, and `APOD_BROKER_ASK_TIMEOUT=4` so that
   requests that wait fail quickly.
3. `apod-broker register --profile test --profile-dir DIR --project /p --role ROLE`, with the
   profile's `broker.toml` in `DIR`. It prints the registration as JSON: `dir` holds the pod's
   `broker.sock`, plus `env`, `mounts`, `context`, `providers`.
4. Call the socket, or run your client with `APOD_BROKER_SOCK` pointing at it. Cover what's
   allowed, what's out of scope, what the role doesn't allow, and input that's malformed.

```sh
python3 tests/test_broker.py            # all of them, about two minutes
KEEP=1 python3 tests/test_broker.py     # keep the temporary APOD_ROOT (its broker.log, log.jsonl)
```

## Reference

Everything here is imported from `api`.

### Providers

**`provider(p)`** registers a provider and returns it. Subclass **`Provider`**, or give any
object the same attributes:

| Attribute | |
|---|---|
| `name` | Required: lowercase letters, digits and `-`, unique, not a `roles.toml` key (`allow`, `deny`, `extends`, `description`) or `unscoped`. It names the `broker.toml` table, the role list and the action prefix. |
| `routes` | `[(method, path, handler)]`. `method` is `GET`, `POST` or `CONNECT`. `path` matches a request's path exactly, or as a prefix that ends at a `/` or `?` (`/svc` takes `/svc` and `/svc/x`, not `/svcx`); `CONNECT` may use `""` for every CONNECT. A route that overlaps another provider's is refused. `/whoami` is the core's. |
| `requires` | Names of providers this one needs. It's configured after them, and only when they're on for the pod (`github` requires `gh`). |
| `configure(sess, cfg, reg, out)` | Required. Runs when a pod registers, before its socket exists. Returns whether the provider is on for this pod; requests to its routes from a pod it's off for are refused. See below. |
| `listen(sess)` | Optional. Start sockets of your own in `sess.dir`, e.g. one that doesn't speak HTTP (`ssh` serves its agent this way). Called for every provider, on or off, at registration and when the broker starts again, so check `sess.cfg` first. Return the servers; the core stops them when the session ends. |
| `close(session_id)` | Optional. The session ended. |
| `keys(cfg)` | Optional. `[(key path, confirm)]` named by your `broker.toml` table, for `apod broker keys` to list and load before any pod starts. |

**`configure(sess, cfg, reg, out)`:**

- `cfg` is your table: `~/.agent-pod/broker.toml`'s, overridden key by key by the profile's
  (`{}` if neither has one).
- `reg` is the registration: `id`, `profile`, `profile_dir`, `project` (the host path), `role` and
  `gh_user` (when given at launch), `scope` (the launch's `--scope` entries), `remotes` (the
  project's git remote URLs).
- `configure` runs once per pod: after a broker restart, sessions come back from disk and only
  `listen` runs again. Keep what your handlers need in `sess.cfg[name]`.
- What you add to `out` is applied even if you return False.
- A `BrokerError` fails the registration (the pod doesn't start), with its message. Any other
  exception turns your provider off for that pod, with a warning.

What `configure` can add to `out`, which `apod` applies to the pod:

| Key | |
|---|---|
| `out["mounts"]` | `[host path, pod path, mode]`, mode `"ro"` or `"rw"`. Paths can't contain `:`, tabs or newlines, and the host path must exist. |
| `out["env"]` | `{name: value}` for the pod's environment, single-line values. `APOD_BROKER_SOCK` is set already. |
| `out["git_config"]` | `[[key, value]]`, passed as `GIT_CONFIG_*`, so they override the pod's git config files. `{forward}` in a key becomes the pod's git forwarder URL (`github`, `rad`). |
| `out["context"]` | Sentences for the agent's system prompt: what's available and how to use it. |
| `out["warnings"]` | Lines `apod` prints at launch, for the user. |
| `need_key(sess, out, path, confirm=False)` | A function, not a key: the broker's ssh-agent must hold this key, and `apod` runs `ssh-add` at launch if it doesn't. Returns `(public key line, blob)`; raises `BrokerError` if `path.pub` is missing. |

### Handlers

A route's handler is `handler(h, sess, method)`:

- `h` is the request (`http.server.BaseHTTPRequestHandler`): `h.path`, `h.headers`, `h.rfile`,
  `h.wfile`.
- `sess` is the pod's session (below).
- An exception is logged to `broker.log`, and the pod gets a 500.

| Name | |
|---|---|
| `read_body(h, limit=BODY_LIMIT)` | The request's body (Content-Length or chunked), at most `limit` bytes: `BrokerError` beyond. `BODY_LIMIT` is 64 MiB, `GIT_BODY_LIMIT` 2 GiB. Pass a small limit for small requests. |
| `h.send_json(obj, status=200)` | Reply with JSON. |
| `send_text(h, text, status=403)` | Reply with a line of text, prefixed `apod broker: `. |

### Requests and decisions

| Name | |
|---|---|
| `Request(kind, display)` | `kind` is your provider's name; `display` is how the TUI and log show the request, so make it say what will happen. Fill in `.actions` and `.targets`. |
| `check(sess, req, waiting=None, extra=None)` | `(ok, event)`. Removes duplicate actions and targets, decides, and logs. If the request has to wait, `waiting(event)` is called first (to tell the pod), then it blocks until the user answers or `ASK_TIMEOUT`. `extra` is merged into the event; `{"once_window": N}` makes approving it once also cover the same targets for `N` seconds (a git push's ref advertisement, then the push). Set `event["exit"]` after acting, for the TUI. |
| `evaluate(sess, req)` | `(verdict, missing actions, targets out of scope, reason)` with `verdict` `"allow"`, `"ask"` or `"deny"`, without logging or waiting. |
| `allowed(sess, action)` | Whether the role (with its approvals) allows an action outright. For setting a pod up (`sign` turns on `commit.gpgsign` only for roles that sign); the request itself still goes through `check`. |
| `recently_allowed(sess, req, within=600)` | Whether the same request (by `display`) went through for this session in the last `within` seconds: for the later rounds of one operation (a git fetch's negotiation), checked and logged once. |
| `rejected_msg(req, ev)`, `waiting_msg(req, ev)` | Messages for the pod, telling the agent how the user can approve. |
| `HARD_DENY` | A list of action patterns that are never allowed, whatever the role or approvals say. Append your provider's; don't add patterns that cover other providers' actions. |

### Scope kinds

**`scope_kind(prefix, *, provider="", parse=None, match=None, grant=None, label=None, entry=True,
help="", remote=None)`** declares a kind. `prefix` is lowercase letters, digits and `-`, declared
once (a second declaration fails that extension's load). Declare kinds before calling
`provider()`.

By default, a target `(prefix, value)` is in scope when it matches one of the session's
`prefix:` entries, with shell-style patterns, case-insensitive (`*` also matches `/`). The
arguments change that:

| Argument | |
|---|---|
| `provider` | The provider the kind belongs to, shown by `apod broker scopes`. |
| `parse(value)` | Normalizes an entry as written (`gh:` lowercases and drops `.git`). |
| `match(sess, value)` | Whether a target is in scope, e.g. from `sess.entries(prefix)` or other kinds' entries. `ssh:` uses it so that `ssh:nas` allows any user and `ssh:root@nas` only root. |
| `grant(value)` | The entry `(prefix, value)` that approving a target "for the session" adds. Default: the target itself. |
| `label(value)` | How the TUI and log show a target. Default `prefix:value`. |
| `entry=False` | A target kind users don't write; its `match` looks at other kinds' entries. gh's `gh-projects-of` is in scope with `gh:OWNER` or any `gh-project:OWNER/…`. |
| `help` | The line `apod broker scopes` shows. |
| `remote(url)` | The value this kind gives a git remote URL, or `None` if it's not one of its own. `gh:` turns `git@github.com:acme/widget.git` into `acme/widget`. |

**`remote_target(url)`** asks every kind's `remote` and returns the first `(prefix, value)`, or
`None`. `sign` uses it to scope a signature by the repository it's for.

### The session

`sess` is one pod's session:

| Member | |
|---|---|
| `id`, `profile`, `role`, `project`, `created` | The registration. |
| `identity` | `{"name", "emails"}`: the profile's git identity, from its `env`. |
| `scope`, `entries(prefix)` | The scope entries, `{prefix: [values]}`; one kind's list. |
| `in_scope(target)` | Whether a `(kind, value)` target is in scope. |
| `add_scope(target)` | Add an entry (through the kind's `grant`). Only from host-side facts: your `broker.toml` table, or something the broker created for the pod. |
| `grants` | What the user approved for the session: `{"actions": [...], "unscoped": [...]}`. |
| `cfg[name]` | Yours, for per-session state; saved with the session, so it survives a broker restart. Keys starting with `_` are the core's. |
| `login` | The GitHub account's login, when `gh` is on. |
| `title()` | The agent session's name, when there is one. |
| `dir` | The pod's folder on the host, mounted read-write at `POD_DIR` (`/run/apod-broker`) in the pod. The pod can change anything in it: write files there for tools in the pod, never read back from it. |
| `save()` | Write the session to disk, after changing it. |

Hold **`LOCK`** while changing `scope`, `grants` or `cfg` from a handler, then call `save()`;
other requests of the same pod run at the same time, in other threads.

### Sockets, keys, files and utilities

| Name | |
|---|---|
| `unix_listen(path, handler, session_id=None)` | Serve a unix socket (mode 0600) in a thread, with a `socketserver` handler class; the handler sees `self.server.session_id`. For `listen`. |
| `live_session(session_id)` | The session while it lasts, `None` once it ended: look it up on each connection. |
| `pubkey_of(path)` | `(public key line, base64 blob)` for a private key, from its `.pub` file (`BrokerError` if missing). |
| `AGENT` | The broker's ssh-agent socket (for `SSH_AUTH_SOCK` when you sign with a key it holds). |
| `BDIR`, `ROOT` | The broker's state folder (`~/.agent-pod/broker`) and `~/.agent-pod`. Use `BDIR / "tmp"` for scratch files; it's emptied when the broker starts. |
| `POD_DIR`, `SHIM` | Where `sess.dir` is in the pod; the shipped pod-side script (`broker/pod-shim`). |
| `ASK_TIMEOUT` | How long a request waits for approval, in seconds (`0`: refused at once). |
| `BrokerError` | The error to raise for anything the pod or the user should see as a message. |
| `run_quiet(cmd, timeout=30, **kw)` | `subprocess.run` with output captured, stdin closed and a timeout; failures to start return exit code 127. |
| `expand(path)` | `~` expanded, as a `Path`. |
| `now()` | `time.time()`. |

`gitwire` (in `broker/`) has the git smart-HTTP pieces `github` and `rad` share: pkt-lines,
parsing a push's ref updates, reading the commits in its pack, and replying to git while a push
waits. It isn't part of the versioned API.

The `gh` extension's helpers, for extensions that act on GitHub with the pod's account:
`gh_user(sess)` (the host `gh` account), `gh_query(sess, args)` (run `gh` for a lookup; stdout or
`None`), `token_for(user)` and `github_repo(url)`.

## Security rules

An extension runs on the host, as you, and handles requests from pods that may be hostile.

- **Treat everything from the pod as hostile.** Cap its size (`read_body(h, limit=...)`), check
  its shape, and validate every value before you use it. Run commands as an argument list, never
  through a shell, with `--` before anything the pod chose. Never let the pod name a path on the
  host: take file contents in the request instead, like `gh` does.
- **Classify by what the request does, not what it says.** Actions and targets must describe the
  real effect. `gh` resolves GraphQL node ids on GitHub to find the repository; text in an issue
  body is not a target. If you can't tell what a request touches, use an `unscoped` target, so
  that it asks.
- **Widen the scope only from the host's side:** your `broker.toml` table, or something the
  broker itself created (a fork it made). Never from the pod's request, from `sess.dir`, or from
  the project folder (`reg["remotes"]` comes from the project's `.git/config`, which the pod can
  edit; the shipped extensions accept that for the user's own remotes).
- **Fail closed.** When a lookup fails or input is ambiguous, ask or refuse; don't guess "allowed".
- **Don't trust `sess.dir`.** The pod can write there and replace files with symlinks. Write what
  tools in the pod need; never read it back.
- **Keep requests short.** An agent's command usually times out at 2 minutes and the approval
  wait is 90 seconds by default; don't stack a long action on top.
- **Leave the core's state alone:** the `_` keys in `sess.cfg`, other providers' actions in
  `HARD_DENY`, and anything in `core` that `api` doesn't export.

## Debugging

- `~/.agent-pod/broker/broker.log`: extensions that didn't load, and the tracebacks of handlers
  that failed.
- `apod broker scopes`: the scope kinds, and which file each extension loaded from.
- `apod broker log -f`, or `apod broker` (the TUI): requests as they come, with their actions,
  targets and decision.
- `apod broker pending`, `apod broker sessions`, `apod broker roles`.
- In the pod, `curl --unix-socket "$APOD_BROKER_SOCK" http://x/whoami` (or `gh auth status`):
  the session's role, scope and providers, as the broker sees them.
- `apod broker restart` after every change to an extension.
