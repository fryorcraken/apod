"""apod broker core: sessions, roles, scope, approvals, the audit log and the servers.

The broker runs on the host and does things for apod pods that would otherwise need credentials
in the pod. Each pod gets a session (a profile, a role and a scope) and a socket directory
mounted at /run/apod-broker. Providers (gh, github, rad, sign, ssh) turn what a pod asks for into
actions ("gh:pr create", "github:push branch", "sign:commit", "ssh:login") and targets (a
repository, a project, a host, a git identity). A request goes ahead when the role allows every
action and every target is in the session's scope; anything else waits for the user's approval
(apod broker ui, apod broker allow) until it times out.
"""
import collections
import fnmatch
import http.client
import importlib
import importlib.util
import itertools
import json
import os
import re
import secrets
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(os.environ.get("APOD_ROOT") or Path.home() / ".agent-pod")
BDIR = ROOT / "broker"
SDIR = BDIR / "sessions"
ADMIN = BDIR / "admin.sock"
AGENT = BDIR / "agent.sock"     # the broker's own ssh-agent: keys for ssh, signing and rad
GRANTS = BDIR / "grants.json"
LOGF = BDIR / "log.jsonl"
HERE = Path(__file__).resolve().parent
CHECKOUT = HERE.parent
ROLE_FILES = [CHECKOUT / "roles.toml", ROOT / "roles.toml"]  # built-in, then yours (added to them)
OLD_ROLE_FILE = ROOT / "personas.toml"  # before roles were called roles; read if roles.toml isn't there
SHIM = HERE / "pod-shim"
POD_DIR = "/run/apod-broker"
ASK_TIMEOUT = float(os.environ.get("APOD_BROKER_ASK_TIMEOUT", "90"))
if ASK_TIMEOUT.is_integer():
    ASK_TIMEOUT = int(ASK_TIMEOUT)

# Scope kinds, keyed by prefix: an entry is written PREFIX:VALUE in broker.toml's scope list and
# --scope, a target is (PREFIX, VALUE). Core knows none; extensions declare theirs with
# scope_kind() (gh:, gh-project:, rad:, ssh:, git-local: are the shipped extensions'). "unscoped"
# targets are core's: they're in scope only once the user approves them for the session.
SCOPES = {}
OLD_TARGET_KINDS = {"repo": "gh", "owner": "gh", "project": "gh-project", "host": "ssh"}
GLOBAL_CONFIG = ROOT / "broker.toml"   # defaults for every profile's broker.toml
EXTENSIONS = ROOT / "broker-extensions"  # the user's own extensions, *.py; a name here replaces a shipped one
SHIPPED_EXTENSIONS = CHECKOUT / "extensions"


class BrokerError(Exception):
    pass


LOCK = threading.RLock()
SESSIONS = {}   # id -> Session
SERVERS = {}    # id -> [servers]
PENDING = {}    # event id -> Pending
EVENTS = collections.deque(maxlen=500)
COUNTER = itertools.count(1)
PROVIDERS = {}  # name -> provider
ROUTES = []     # (method, path prefix, handler(h, sess, method), provider name)


def now():
    return time.time()


def provider(p):
    """Register a provider: an object with name and configure(sess, cfg, reg, out) -> bool, and
    optionally routes [(method, path prefix, handler(h, sess, method))], listen(sess) -> [servers]
    and close(session id). See README.md, "Writing a broker extension"."""
    if p.name in PROVIDERS:
        raise BrokerError(f"a provider named {p.name} is already registered")
    PROVIDERS[p.name] = p
    ROUTES.extend((m, prefix, fn, p.name) for m, prefix, fn in getattr(p, "routes", []))
    return p


class ScopeKind:
    """A scope kind an extension declares (see scope_kind)."""

    def __init__(self, prefix, provider="", parse=None, match=None, grant=None, label=None, entry=True, help="",
                 remote=None):
        self.prefix, self.provider, self.entry, self.help, self.remote = prefix, provider, entry, help, remote
        self._parse, self._match, self._grant, self._label = parse, match, grant, label

    def parse(self, v):
        """An entry's value as the user wrote it -> as stored."""
        return self._parse(v) if self._parse else v.strip()

    def match(self, sess, v):
        """Is the target value V in the session's scope?"""
        if self._match:
            return bool(self._match(sess, v))
        return any(fnmatch.fnmatchcase(v.lower(), p.lower()) for p in sess.scope.get(self.prefix, []))

    def grant(self, v):
        """The entry (prefix, value) that approving target value V for the session adds."""
        return self._grant(v) if self._grant else (self.prefix, v)

    def label(self, v):
        return self._label(v) if self._label else f"{self.prefix}:{v}"


def scope_kind(prefix, *, parse=None, match=None, grant=None, label=None, entry=True, help="", provider="",
               remote=None):
    """Declare a scope kind, written PREFIX:VALUE in broker.toml's scope list and --scope. A
    request's target (PREFIX, value) is in scope when match(sess, value) says so; by default,
    when it matches one of the session's PREFIX entries (shell-style patterns, case-insensitive).

    parse(value) normalizes an entry as written; grant(value) -> (prefix, value) is the entry
    that approving a target for the session adds (default: the target itself); label(value) is
    how the target is shown (default PREFIX:value); entry=False for a target kind that isn't
    written in the scope (its match looks at other kinds' entries); help is shown in apod
    broker scopes; remote(url) -> value or None says which repository a git remote URL names, for
    kinds that are repositories (see remote_target). A prefix declared twice is an error, like a
    provider name."""
    if not re.fullmatch(r"[a-z][a-z0-9-]*", prefix) or prefix == "unscoped":
        raise BrokerError(f"bad scope prefix {prefix!r}: lowercase letters, digits and -")
    if prefix in SCOPES:
        raise BrokerError(f"scope kind {prefix}: is already declared (by {SCOPES[prefix].provider or 'another extension'})")
    SCOPES[prefix] = k = ScopeKind(prefix, provider, parse, match, grant, label, entry, help, remote)
    return k


def remote_target(url):
    """The repository a git remote URL names, as a target (gh:OWNER/REPO, rad:RID, or an
    extension's kind), else None."""
    for k in SCOPES.values():
        v = k.remote(url) if k.remote else None
        if v:
            return k.prefix, v
    return None


def extension_files():
    """name -> file of every extension to load: the shipped ones (extensions/ in the checkout),
    then yours (~/.agent-pod/broker-extensions), which replace a shipped one of the same name. An
    empty file of that name turns a shipped extension off."""
    files = {}
    for d in (SHIPPED_EXTENSIONS, EXTENSIONS):
        for f in sorted(d.glob("*.py")) if d.is_dir() else []:
            # Extensions are imported by name, so one can't take the name of a module the broker
            # or Python uses (json.py would replace json for everything).
            if f.stem in sys.stdlib_module_names or f.stem in ("core", "gitwire", "tui") or not f.stem.isidentifier():
                print(f"apod-broker: extension {f} not loaded: {f.stem} is the name of a Python or broker module",
                      file=sys.stderr, flush=True)
                continue
            files[f.stem] = f
    return files


class _ExtensionFinder:
    """`import gh` in one extension gets the gh extension that's loaded (shipped or yours)."""

    def __init__(self, files):
        self.files = files

    def find_spec(self, name, path=None, target=None):
        if name in self.files:
            return importlib.util.spec_from_file_location(name, self.files[name])
        return None


def load_extensions(only=None):
    """Import the extensions (extension_files), each under its own name: the shipped ones in the
    order they depend on each other (gh before github), then the rest in name order. Each
    registers its providers and scope kinds. One that fails is skipped, with the error on
    stderr (the broker's log)."""
    files = extension_files()
    if only is not None:
        files = {k: v for k, v in files.items() if k in only}
    finder = _ExtensionFinder(files)
    sys.meta_path.insert(0, finder)
    loaded = []
    for name in sorted(files, key=lambda n: (SHIPPED_ORDER.index(n) if n in SHIPPED_ORDER else len(SHIPPED_ORDER), n)):
        try:
            importlib.import_module(name)
            loaded.append(name)
        except Exception as e:  # noqa: BLE001 (a broken extension mustn't stop the broker)
            sys.modules.pop(name, None)
            print(f"apod-broker: extension {files[name]} not loaded: {e!r}", file=sys.stderr, flush=True)
    return loaded


SHIPPED_ORDER = ["gh", "github", "rad", "sign", "ssh"]  # the order providers were always registered in


def expand(path):
    return Path(os.path.expanduser(str(path)))


def run_quiet(cmd, **kw):
    kw.setdefault("stdin", subprocess.DEVNULL)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 30), **kw)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


# ---------------------------------------------------------------------------------------------
# Roles, grants, scope

def merge_role(base, more):
    """A role from your roles.toml over the built-in one of the same name: its lists (per
    provider, allow, deny, extends) are added; its description replaces the built-in one."""
    out = dict(base)
    for k, v in more.items():
        if isinstance(v, (list, str)) and k != "description" and k in out:
            old = [out[k]] if isinstance(out[k], str) else list(out[k])
            out[k] = old + [x for x in ([v] if isinstance(v, str) else v) if x not in old]
        else:
            out[k] = v
    return out


def load_roles():
    raw = {}
    files = ROLE_FILES + ([OLD_ROLE_FILE] if OLD_ROLE_FILE.exists() and not ROLE_FILES[1].exists() else [])
    for f in files:
        if f.exists():
            data = tomllib.loads(f.read_text())
            for name, role in {**data.get("personas", {}), **data.get("roles", {})}.items():
                raw[name] = merge_role(raw[name], role) if name in raw else role
    return raw


def load_grants():
    try:
        g = json.loads(GRANTS.read_text())
    except (OSError, ValueError):
        return {"roles": {}}
    if "personas" in g:  # grants saved before roles were called roles
        g.setdefault("roles", {})
        for name, lst in g.pop("personas").items():
            g["roles"].setdefault(name, []).extend(a for a in lst if a not in g["roles"].get(name, []))
    g.setdefault("roles", {})
    return g


def save_grants(g):
    tmp = GRANTS.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(g, indent=2) + "\n")
    tmp.replace(GRANTS)


def role_rules(name, raw=None, seen=None):
    """(allow, deny) patterns of a role, as "provider:action": its own (one list per provider),
    its grants, and those of the roles it extends."""
    raw = load_roles() if raw is None else raw
    seen = set() if seen is None else seen
    if name not in raw:
        raise BrokerError(f"no role {name} (known: {', '.join(sorted(raw)) or 'none'})")
    if name in seen:
        return [], []
    seen.add(name)
    p = raw[name]
    allow = list(p.get("allow", [])) + list(load_grants()["roles"].get(name, []))
    deny = list(p.get("deny", []))
    for pname, rules in p.items():
        if pname in PROVIDERS and isinstance(rules, list):
            allow += [f"{pname}:{a}" for a in rules]
    ext = p.get("extends", [])
    for e in [ext] if isinstance(ext, str) else ext:
        a, d = role_rules(e, raw, seen)
        allow += a
        deny += d
    return allow, deny


def matches(action, patterns):
    return any(fnmatch.fnmatchcase(action, p) for p in patterns)


def parse_scope(entry):
    """A scope entry as the user wrote it -> (prefix, value). Raises BrokerError for a prefix
    no extension declares."""
    e = old_scope(entry) or entry.strip()
    prefix, colon, v = e.partition(":")
    k = SCOPES.get(prefix.lower()) if colon else None
    if not k or not k.entry:
        known = ", ".join(f"{p}:" for p, x in SCOPES.items() if x.entry)
        raise BrokerError(f"scope entry {entry.strip()!r}: no extension provides {prefix + ':' if colon else 'a kind'} "
                          f"(known: {known})")
    return k.prefix, k.parse(v)


def old_scope(entry):
    """An entry written in an older form (OWNER/REPO, OWNER, project:OWNER/N) -> as it's written
    now (gh:..., gh-project:...), else None."""
    e = entry.strip()
    for old, new in (("project:", "gh-project:"),):
        if e.lower().startswith(old):
            return new + e[len(old):].strip()
    if ":" not in e:
        return "gh:" + e
    return None


def fmt_target(t):
    """A target as it's written in the scope, so what you see is what you'd add."""
    kind, v = t[0], t[1]
    if kind == "unscoped":
        return f"unscoped: {v}"
    kind = OLD_TARGET_KINDS.get(kind, kind)  # in log entries from before scope kinds were prefixes
    return SCOPES[kind].label(v) if kind in SCOPES else f"{kind}:{v}"


def fmt_scope(kind, v):
    return f"{kind}:{v}"


class Session:
    FIELDS = ("id", "profile", "role", "project", "scope", "grants", "cfg", "identity", "created")

    def __init__(self, d):
        for k in self.FIELDS:
            setattr(self, k, d.get(k))
        self.role = self.role or d.get("persona") or "reader"  # sessions saved before the rename
        self.scope = {}
        for k, vs in (self.scope or {}).items():  # sessions saved before kinds were prefixes: repo, host, ...
            lst = self.scope.setdefault(OLD_TARGET_KINDS.get(k, k), [])
            lst += [v for v in vs if v not in lst]
        self.grants = {k: list((self.grants or {}).get(k, [])) for k in ("actions", "unscoped")}
        self.cfg = self.cfg or {}
        self.identity = self.identity or {}
        self.temp = {}  # (kind, value) -> expiry, from a one-off approval that spans two requests

    def data(self):
        return {k: getattr(self, k) for k in self.FIELDS}

    @property
    def dir(self):
        """The pod's folder, mounted read-write at POD_DIR: nothing in it can be trusted."""
        return SDIR / self.id

    @property
    def state(self):
        """The session's saved state, outside the pod's folder."""
        return SDIR / f"{self.id}.json"

    @property
    def login(self):
        return (self.cfg.get("gh") or {}).get("login", "")

    def save(self):
        tmp = self.state.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(self.data(), indent=2) + "\n")
        tmp.replace(self.state)

    def in_scope(self, t):
        kind, v = t[0], t[1]
        if self.temp.get((kind, v), 0) > now():
            return True
        if kind == "unscoped":
            return v in self.grants["unscoped"]
        k = SCOPES.get(kind)
        return bool(k and k.match(self, v))

    def add_scope(self, t):
        """Add a scope entry (prefix, value), or what approving target t for the session adds."""
        kind, v = t[0], t[1]
        if kind == "unscoped":
            lst = self.grants["unscoped"]
        else:
            if kind in SCOPES:
                kind, v = SCOPES[kind].grant(v)
            lst = self.scope.setdefault(kind, [])
        if v not in lst:
            lst.append(v)

    def entries(self, prefix):
        """The session's scope entries of one kind."""
        return self.scope.get(prefix, [])

    def scope_list(self):
        return [fmt_scope(k, v) for k in self.scope for v in self.scope[k]]

    def summary(self):
        allow, _ = role_rules(self.role)
        return {"role": self.role, "login": self.login, "scope": self.scope_list(),
                "allowed": sorted(set(allow)), "identity": self.identity, "grants": self.grants,
                "name": self.title(), "text": self.describe()}

    def describe(self):
        """What this pod may do, for the agent: its system prompt at launch, gh auth status later
        (which also shows what the user approved since)."""
        allow, _ = role_rules(self.role)
        active = self.cfg.get("_providers") or []
        by = collections.defaultdict(list)
        for a in sorted(set(allow)):
            prov, _, act = a.partition(":")
            by[prov].append(act)
        lines = [f"apod broker: role {self.role}" + (f", GitHub user {self.login}" if self.login else "") + ".",
                 f"Scope (the repositories, projects, hosts and identities it may touch): "
                 f"{', '.join(self.scope_list()) or 'none'}.",
                 "Allowed without asking, on what's in scope:"]
        lines += [f"  {p}: {', '.join(by[p])}" for p in active if by.get(p)]
        granted = self.grants["actions"] + [f"unscoped: {u}" for u in self.grants["unscoped"]]
        if granted:
            lines.append(f"Approved by the user for this session: {', '.join(granted)}.")
        lines.append(f"Anything else, or outside the scope, waits up to {ASK_TIMEOUT}s for the user to approve it on "
                     f"the host, then fails. Before such a step, tell the user what you need and why; if it fails, "
                     f"ask them to approve it (or widen the scope) and retry once they say it is allowed. "
                     f"gh auth status shows the current permissions, including approvals made since launch.")
        return "\n".join(lines)

    def title(self):
        """The agent session's name (Claude Code's /rename), from the newest transcript for the
        project in the profile's home that's been written since this pod started."""
        home, proj = self.cfg.get("_home"), self.project
        if not home or not proj:
            return ""
        d = Path(home) / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", proj)
        try:
            files = [(f.stat().st_mtime, f) for f in d.glob("*.jsonl")]
        except OSError:
            return ""
        files = sorted((m, f) for m, f in files if m >= (self.created or 0) - 5)
        if not files:
            return ""
        f = files[-1][1]
        cache = self.__dict__.setdefault("_titles", {})
        off, name = cache.get(str(f), (0, ""))
        try:
            with f.open("rb") as fh:
                fh.seek(off)
                data = fh.read()
        except OSError:
            return name
        end = data.rfind(b"\n") + 1  # only whole lines; the rest is read next time
        for line in data[:end].splitlines():
            if b'"custom-title"' in line:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") == "custom-title" and r.get("customTitle"):
                    name = r["customTitle"]
        cache[str(f)] = (off + end, name)
        return name


class Request:
    """A classified request: what it does (actions) and what it touches (targets)."""

    def __init__(self, kind, display):
        self.kind, self.display = kind, display
        self.actions, self.targets = [], []
        self.argv, self.repo, self.created, self.notes, self.stdin = None, None, None, [], None
        self.help = False


def new_event(sess, req):
    ev = {"id": next(COUNTER), "ts": now(), "session": sess.id, "name": sess.title(), "profile": sess.profile,
          "role": sess.role, "login": sess.login, "project": sess.project, "kind": req.kind,
          "request": req.display, "actions": req.actions, "targets": [list(t) for t in req.targets],
          "missing": [], "out": [], "decision": "pending", "reason": "", "exit": None}
    with LOCK:
        EVENTS.append(ev)
    return ev


def log_event(ev):
    try:
        with LOGF.open("a") as f:
            f.write(json.dumps(ev) + "\n")
    except OSError:
        pass


class Pending:
    def __init__(self, ev):
        self.ev, self.done, self.decision = ev, threading.Event(), None


HARD_DENY = []  # extended by providers: never allowed, whatever the role or grants say


def evaluate(sess, req):
    allow, deny = role_rules(sess.role)
    allow = allow + sess.grants["actions"]
    hard = [a for a in req.actions if matches(a, HARD_DENY + deny)]
    if hard:
        return "deny", [], [], f"never allowed: {', '.join(hard)}"
    missing = [a for a in req.actions if not matches(a, allow)]
    out = [list(t) for t in req.targets if not sess.in_scope(t)]
    reasons = []
    if missing:
        reasons.append(f"not allowed for role {sess.role}: {', '.join(missing)}")
    if out:
        reasons.append(f"out of this session's scope: {', '.join(fmt_target(t) for t in out)}")
    return ("ask" if reasons else "allow"), missing, out, "; ".join(reasons)


def desktop_notify(ev):
    if not shutil.which("notify-send"):
        return
    try:
        subprocess.Popen(["notify-send", "-a", "apod", f"apod broker: {ev.get('name') or ev['profile']} asks",
                          f"{ev['request']}\napod broker allow {ev['id']}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    except OSError:
        pass


def authorize(sess, req, ev, waiting=None):
    """True if the request may go ahead: allowed outright, or approved while it waits."""
    verdict, missing, out, reason = evaluate(sess, req)
    ev.update(missing=missing, out=out, reason=reason)
    if verdict == "allow":
        ev["decision"] = "allowed"
        return True
    if verdict == "deny" or ASK_TIMEOUT <= 0:
        ev["decision"] = "denied" if verdict == "deny" else "rejected"
        return False
    p = Pending(ev)
    with LOCK:
        PENDING[ev["id"]] = p
    if waiting:
        waiting(ev)
    desktop_notify(ev)
    got = p.done.wait(ASK_TIMEOUT)
    with LOCK:
        PENDING.pop(ev["id"], None)
    if not got:
        ev["decision"] = "timed out"
        return False
    if p.decision == "deny":
        ev["decision"] = "denied by you"
        return False
    ev["decision"] = f"approved ({p.decision})"
    return True


def check(sess, req, waiting=None, extra=None):
    """authorize() with the event logged: (ok, event)."""
    # The same action or target twice (a GraphQL request with 20 identical mutations) is one.
    req.actions = list(dict.fromkeys(req.actions))
    req.targets = list(dict.fromkeys(tuple(t) for t in req.targets))
    ev = new_event(sess, req)
    ev.update(extra or {})
    try:
        ok = authorize(sess, req, ev, waiting)
    except BrokerError as e:
        ev.update(decision="error", reason=str(e))
        ok = False
    log_event(ev)
    return ok, ev


def rejected_msg(req, ev):
    return (f"rejected ({ev['decision']}): `{req.display}`: {ev['reason']}. "
            f"The user can allow it (apod broker allow {ev['id']} --session) and you can retry.")


def waiting_msg(req, ev):
    return (f"`{req.display}` needs your approval ({ev['reason']}). Waiting up to {ASK_TIMEOUT}s. "
            f"On the host: apod broker allow {ev['id']} [--session|--role], apod broker deny {ev['id']}, "
            f"or apod broker ui")


def decide(eid, how):
    """Approve (once, for the session, or for the role) or deny a request. Approving a past
    request for the session or role adds its grants so that a retry goes through."""
    if how not in ("once", "session", "role", "deny"):
        raise BrokerError(f"unknown decision {how}")
    with LOCK:
        p = PENDING.get(eid)
        ev = p.ev if p else next((e for e in EVENTS if e["id"] == eid), None)
        if ev is None:
            raise BrokerError(f"no request {eid}")
        if not p and how in ("once", "deny"):
            raise BrokerError(f"request {eid} is no longer waiting; use --session or --role to allow a retry")
        sess = SESSIONS.get(ev["session"])
        if how in ("session", "role"):
            if how == "role":
                g = load_grants()
                lst = g["roles"].setdefault(ev["role"], [])
                lst += [a for a in ev["missing"] if a not in lst]
                save_grants(g)
            elif sess:
                sess.grants["actions"] += [a for a in ev["missing"] if a not in sess.grants["actions"]]
            if sess:
                for t in ev["out"]:
                    sess.add_scope(t)
                sess.save()
        elif how == "once" and sess and ev.get("once_window"):
            # This approval also covers the request that follows (e.g. a push after its ref
            # advertisement).
            for t in ev["out"]:
                sess.temp[(t[0], t[1])] = now() + ev["once_window"]
        if p:
            p.decision = how
            p.done.set()
        return ev


# ---------------------------------------------------------------------------------------------
# HTTP on unix sockets

BODY_LIMIT = 64 << 20   # a request from a pod; git pushes may be larger (GIT_BODY_LIMIT)
GIT_BODY_LIMIT = 2 << 30


def read_body(h, limit=BODY_LIMIT):
    """A request's body, at most limit bytes (BrokerError beyond that)."""
    if h.headers.get("Transfer-Encoding", "").lower() == "chunked":
        chunks, total = [], 0
        while True:
            n = int(h.rfile.readline(64).split(b";")[0].strip() or b"0", 16)
            if n == 0:
                while h.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                    pass
                break
            total += n
            if total > limit:
                raise BrokerError(f"request body over {limit >> 20} MiB")
            chunks.append(h.rfile.read(n))
            h.rfile.readline(64)
        return b"".join(chunks)
    n = int(h.headers.get("Content-Length") or 0)
    if n > limit or n < 0:
        raise BrokerError(f"request body over {limit >> 20} MiB")
    return h.rfile.read(n)


def send_text(h, text, status=403):
    data = f"apod broker: {text}\n".encode()
    h.send_response(status)
    h.send_header("Content-Type", "text/plain; charset=utf-8")
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Connection", "close")
    h.end_headers()
    h.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def address_string(self):
        return "local"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, status=200):
        data = (json.dumps(obj) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True


class SessionHandler(Handler):
    """A pod's requests: routed to the provider that registered the path."""

    def route(self, method):
        self.close_connection = True
        sess = SESSIONS.get(self.server.session_id)
        if not sess:
            return self.send_json({"error": "session closed"}, 410)
        if method == "GET" and self.path == "/whoami":
            return self.send_json(sess.summary())
        for m, prefix, fn, pname in ROUTES:
            if m == method and self.path.startswith(prefix):
                if pname not in (sess.cfg.get("_providers") or []):
                    # Only the providers set up for this pod answer it: one the profile doesn't
                    # configure (no [gh] user, no [ssh]...) must not act with the host's defaults.
                    return send_text(self, f"{pname} isn't set up for this pod", 403)
                try:
                    return fn(self, sess, method)
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as e:  # keep the broker up; tell the client if we still can
                    print(f"apod-broker: {fn.__module__}.{fn.__name__}: {e!r}", file=sys.stderr, flush=True)
                    try:
                        send_text(self, f"internal error: {e}", 500)
                    except OSError:
                        pass
                    return
        self.send_json({"error": "not found"}, 404)

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_CONNECT(self):
        self.route("CONNECT")


class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def unix_listen(path, handler):
    if len(os.fsencode(path)) > 107:
        raise BrokerError(f"socket path too long for a unix socket: {path} (use a shorter APOD_ROOT)")
    if os.path.lexists(path):
        os.unlink(path)
    srv = UnixServer(str(path), handler)
    os.chmod(path, 0o600)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def listen_session(sess):
    sess.dir.mkdir(parents=True, exist_ok=True)
    srv = unix_listen(sess.dir / "broker.sock", SessionHandler)
    srv.session_id = sess.id
    servers = [srv]
    for p in PROVIDERS.values():
        if hasattr(p, "listen"):
            servers += p.listen(sess) or []
    SERVERS[sess.id] = servers


# ---------------------------------------------------------------------------------------------
# The broker's ssh-agent

AGENT_PROC = {}


def start_agent():
    if AGENT_PROC.get("p") and AGENT_PROC["p"].poll() is None:
        return
    if AGENT.exists():
        AGENT.unlink()
    AGENT_PROC["p"] = subprocess.Popen(["ssh-agent", "-D", "-a", str(AGENT)], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if AGENT.exists():
            return
        time.sleep(0.05)


def agent_keys():
    """Public key blobs (base64) loaded in the broker's agent."""
    r = run_quiet(["ssh-add", "-L"], env=dict(os.environ, SSH_AUTH_SOCK=str(AGENT)))
    return {line.split()[1] for line in r.stdout.splitlines() if len(line.split()) >= 2}


def pubkey_of(path):
    """(public key line, base64 blob) for a private key path, from its .pub file."""
    p = expand(path)
    pub = Path(str(p) + ".pub")
    if not pub.exists():
        raise BrokerError(f"{pub} is missing (needed next to {p}; ssh-keygen -y -f {p} > {pub})")
    line = pub.read_text().strip()
    return line, line.split()[1]


def need_key(sess, out, path, confirm=False):
    """Note a key the session uses, held in the broker's agent; apod loads it if it's missing
    (ssh-add asks for the passphrase on the user's terminal)."""
    line, blob = pubkey_of(path)
    k = {"path": str(expand(path)), "confirm": bool(confirm)}
    keys = sess.cfg.setdefault("_keys", [])
    if k not in keys:
        keys.append(k)
    if blob not in agent_keys() and k not in out["need_keys"]:
        out["need_keys"].append(k)
    return line, blob


# ---------------------------------------------------------------------------------------------
# Sessions

def read_env_file(path):
    env = {}
    try:
        for line in Path(path).read_text().splitlines():
            k, eq, v = line.strip().partition("=")
            if eq and not k.startswith("#"):
                env[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return env


def read_toml(f):
    try:
        return tomllib.loads(f.read_text()) if f.exists() else {}
    except tomllib.TOMLDecodeError as e:
        raise BrokerError(f"{f}: {e}")


def merge_config(base, over):
    """A profile's broker.toml over the global one: tables merge key by key, and any other value
    (a table's key or hosts) in the profile replaces the global one."""
    out = dict(base)
    for k, v in over.items():
        out[k] = merge_config(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(pdir, warnings=None):
    """The global broker.toml's tables (settings shared by every profile: keys, the Radicle
    home, ...) under the profile's broker.toml. What a pod may do (role, scope) comes only from
    the profile and the launch."""
    glob = read_toml(GLOBAL_CONFIG)
    for k in [k for k, v in glob.items() if not isinstance(v, dict)]:
        glob.pop(k)
        if warnings is not None:
            warnings.append(f"{GLOBAL_CONFIG}: {k} is ignored there (set it in a profile's broker.toml "
                            f"or at launch); it holds only settings shared by every profile")
    cfg = read_toml(Path(pdir) / "broker.toml")
    if "persona" in cfg and "role" not in cfg:
        cfg["role"] = cfg.pop("persona")
        if warnings is not None:
            warnings.append(f"{Path(pdir) / 'broker.toml'}: persona = is now role = (it still works for now)")
    return merge_config(glob, cfg)


def register(r):
    sid = r.get("id") or secrets.token_hex(6)
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", sid):
        raise BrokerError("bad session id")
    pdir = Path(r.get("profile_dir") or ROOT / r.get("profile", ""))
    warnings = []
    cfg = load_config(pdir, warnings)
    if r.get("gh_user"):  # --gh-user: this launch's GitHub account, over [gh] user
        cfg["gh"] = dict(cfg.get("gh") or {}, user=r["gh_user"])
    role = r.get("role") or cfg.get("role") or "reader"
    role_rules(role)  # fails on an unknown role
    env = read_env_file(pdir / "env")
    identity = {"name": env.get("GIT_COMMITTER_NAME") or env.get("GIT_AUTHOR_NAME", ""),
                "emails": sorted({e for e in (env.get("GIT_AUTHOR_EMAIL"), env.get("GIT_COMMITTER_EMAIL")) if e})}
    sess = Session({"id": sid, "profile": r.get("profile", ""), "role": role,
                    "project": r.get("project", ""), "created": now(), "identity": identity,
                    "cfg": {"_home": str(pdir / "home")}})
    old = []
    for e in list(cfg.get("scope", [])) + list(r.get("scope", [])):
        if e.strip():
            try:
                sess.add_scope(parse_scope(e))
            except BrokerError as ex:
                warnings.append(f"{ex}; ignored")
                continue
            if old_scope(e):
                old.append(f"{e.strip()} → {old_scope(e)}")
    if old:
        warnings.append(f"scope entries in the old form; write them as {', '.join(old)}")
    out = {"env": {"APOD_BROKER_SOCK": f"{POD_DIR}/broker.sock"}, "git_config": [], "mounts": [],
           "context": [], "need_keys": [], "providers": [], "warnings": warnings}
    start_agent()
    for p in PROVIDERS.values():
        try:
            if p.configure(sess, cfg.get(p.name) or {}, r, out):
                out["providers"].append(p.name)
        except BrokerError:
            raise
        except Exception as e:  # one broken extension doesn't stop every pod
            print(f"apod-broker: {p.name}.configure: {e!r}", file=sys.stderr, flush=True)
            out["warnings"].append(f"broker extension {p.name} failed ({e}); it's off for this pod")
    sess.cfg["_providers"] = out["providers"]
    with LOCK:
        if sid in SESSIONS:
            unregister(sid)
        SESSIONS[sid] = sess
        listen_session(sess)
        sess.save()
    head = "The host's apod broker acts for this pod.\n" + sess.describe()
    return {"id": sid, "dir": str(sess.dir), "pod_dir": POD_DIR, "ask_timeout": ASK_TIMEOUT,
            "agent": str(AGENT), **out, "context": "\n".join([head] + out["context"]), **sess.summary(),
            "warnings": out["warnings"]}


def unregister(sid):
    with LOCK:
        SESSIONS.pop(sid, None)
        for srv in SERVERS.pop(sid, []):
            srv.shutdown()
            srv.server_close()
        shutil.rmtree(SDIR / sid, ignore_errors=True)
        (SDIR / f"{sid}.json").unlink(missing_ok=True)
        for p in PROVIDERS.values():
            if hasattr(p, "close"):
                p.close(sid)


def state():
    with LOCK:
        return {"sessions": [dict(s.data(), scope_list=s.scope_list(), name=s.title()) for s in SESSIONS.values()],
                "pending": [p.ev for p in PENDING.values()],
                "events": list(EVENTS)[-200:],
                "grants": load_grants()["roles"],
                "ask_timeout": ASK_TIMEOUT}


def needed_keys():
    """Keys sessions use that the broker's agent doesn't hold (after a broker restart, a TTL)."""
    have, need = agent_keys(), []
    for s in SESSIONS.values():
        for k in s.cfg.get("_keys", []):
            try:
                if pubkey_of(k["path"])[1] not in have and k not in need:
                    need.append(k)
            except BrokerError:
                pass
    return need


def key_report():
    """Every key the broker's agent should hold: those named in a profile's broker.toml (by each
    provider's keys(cfg)) and those running pods use. [{path, confirm, users, loaded, error}]."""
    have, keys = agent_keys(), {}

    def note(path, confirm, user):
        p = str(expand(path))
        k = keys.setdefault(p, {"path": p, "confirm": False, "users": []})
        k["confirm"] = k["confirm"] or bool(confirm)
        if user not in k["users"]:
            k["users"].append(user)
    for f in [GLOBAL_CONFIG] + sorted(ROOT.glob("*/broker.toml")):
        try:
            cfg = read_toml(f) if f == GLOBAL_CONFIG else load_config(f.parent)
        except (OSError, BrokerError):
            continue
        who = "all profiles" if f == GLOBAL_CONFIG else f.parent.name
        for p in PROVIDERS.values():
            if hasattr(p, "keys") and isinstance(cfg.get(p.name), dict):
                for path, confirm in p.keys(cfg[p.name]):
                    path = str(expand(path))
                    if f != GLOBAL_CONFIG and any(u.startswith("all profiles [")
                                                  for u in keys.get(path, {}).get("users", [])):
                        continue  # inherited from the global file
                    note(path, confirm, f"{who} [{p.name}]")
    for s in SESSIONS.values():
        for k in s.cfg.get("_keys", []):
            listed = keys.get(k["path"], {}).get("users", [])
            if not any(u.startswith((f"{s.profile} [", "all profiles [")) for u in listed):
                note(k["path"], k["confirm"], f"{s.profile} (running pod)")
    for k in keys.values():
        try:
            k["loaded"] = pubkey_of(k["path"])[1] in have
        except BrokerError as e:
            k["loaded"], k["error"] = False, str(e)
    return list(keys.values())


def admin_call(path, body):
    if path == "/register":
        return register(body)
    if path == "/unregister":
        unregister(body["id"])
        return {"ok": True}
    if path == "/decide":
        return decide(int(body["id"]), body["how"])
    if path == "/grants/remove":
        with LOCK:
            g = load_grants()
            lst = g["roles"].get(body["role"], [])
            if body["action"] in lst:
                lst.remove(body["action"])
            save_grants(g)
        return {"ok": True}
    if path == "/scope/remove":
        with LOCK:
            sess = SESSIONS[body["session"]]
            kind, v = body.get("kind"), body["value"]
            if kind in sess.scope and v in sess.scope[kind]:
                sess.scope[kind].remove(v)
            for k in ("actions", "unscoped"):
                if v in sess.grants[k]:
                    sess.grants[k].remove(v)
            sess.save()
        return {"ok": True}
    if path == "/state":
        return state()
    if path == "/keys":
        return {"keys": key_report(), "need_keys": needed_keys(), "agent": str(AGENT)}
    if path == "/stop":
        def stop():
            time.sleep(0.2)
            p = AGENT_PROC.get("p")
            if p:
                p.terminate()
            os._exit(0)
        threading.Thread(target=stop, daemon=True).start()
        return {"ok": True}
    raise BrokerError(f"unknown admin call {path}")


class AdminHandler(Handler):
    def do_POST(self):
        self.close_connection = True
        try:
            body = json.loads(read_body(self) or b"{}")
            self.send_json(admin_call(self.path, body))
        except (BrokerError, KeyError, ValueError, TypeError, AttributeError) as e:
            self.send_json({"error": str(e)}, 400)


def live_pods():
    """The broker session ids of the pods podman has (running or not), or None if podman can't say."""
    try:
        r = subprocess.run(["podman", "ps", "-a", "--filter", "label=apod.broker",
                            "--format", '{{index .Labels "apod.broker"}}'],
                           capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return set(r.stdout.split()) if r.returncode == 0 else None


def prune_loop(every=300, grace=600):
    """Ends sessions whose pod is gone (apod killed before it could unregister): their sockets,
    approvals and scope shouldn't outlive the pod. A session younger than grace may still be
    launching its pod."""
    while True:
        live = live_pods()
        if live is not None:
            for sid, sess in list(SESSIONS.items()):
                if sid not in live and now() - sess.created > grace:
                    print(f"apod-broker: session {sid} ({sess.profile}): its pod is gone; ending it",
                          file=sys.stderr, flush=True)
                    unregister(sid)
        time.sleep(every)


def serve():
    for d in (BDIR, SDIR, BDIR / "home" / "gh", BDIR / "tmp"):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(BDIR, 0o700)
    shutil.rmtree(BDIR / "tmp", ignore_errors=True)
    (BDIR / "tmp").mkdir()
    (BDIR / "ui.json").unlink(missing_ok=True)  # left by the web page earlier versions had
    global COUNTER
    last = 0
    try:
        with LOGF.open() as f:
            for line in f:
                try:
                    last = max(last, json.loads(line).get("id", 0))
                except ValueError:
                    pass
    except OSError:
        pass
    COUNTER = itertools.count(last + 1)
    if ADMIN.exists():
        try:
            client("/state", {})
            sys.exit("apod-broker: already running")
        except (OSError, BrokerError):
            ADMIN.unlink()
    start_agent()
    for d in SDIR.iterdir():
        if d.is_dir() and not (SDIR / f"{d.name}.json").is_file():
            # A pod's folder without saved state, or from before the state moved out of it (a pod could
            # have edited that one): the session is gone.
            shutil.rmtree(d, ignore_errors=True)
    for f in SDIR.glob("*.json"):
        try:
            sess = Session(json.loads(f.read_text()))
        except (OSError, ValueError):
            f.unlink(missing_ok=True)
            shutil.rmtree(SDIR / f.stem, ignore_errors=True)
            continue
        try:
            SESSIONS[sess.id] = sess
            listen_session(sess)
        except (OSError, BrokerError) as e:
            SESSIONS.pop(sess.id, None)
            print(f"apod-broker: dropping session {sess.id}: {e}", file=sys.stderr)
            f.unlink(missing_ok=True)
            shutil.rmtree(SDIR / f.stem, ignore_errors=True)

    threading.Thread(target=prune_loop, daemon=True).start()

    def term(*_):
        p = AGENT_PROC.get("p")
        if p:
            p.terminate()
        os._exit(0)
    signal.signal(signal.SIGTERM, term)
    admin = UnixServer(str(ADMIN), AdminHandler)
    os.chmod(ADMIN, 0o600)
    print(f"apod-broker: up ({len(SESSIONS)} sessions; providers: {', '.join(PROVIDERS)})", file=sys.stderr, flush=True)
    admin.serve_forever()


# ---------------------------------------------------------------------------------------------
# Client side (apod and apod broker ...)

class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=None):
        super().__init__("localhost", timeout=timeout)
        self.sock_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(self.sock_path))


def client(path, body, timeout=120):
    c = UnixHTTP(ADMIN, timeout)
    c.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
    r = c.getresponse()
    out = json.loads(r.read() or b"{}")
    if r.status != 200:
        raise BrokerError(out.get("error", f"broker answered {r.status}"))
    return out


def ensure(script):
    try:
        client("/state", {}, timeout=5)
        return
    except (OSError, BrokerError, ValueError):
        pass
    BDIR.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(script), "serve"]
    if (ROOT / "selinux").exists() and shutil.which("runcon") and shutil.which("selinuxenabled") \
            and subprocess.run(["selinuxenabled"]).returncode == 0:
        # apod selinux install: pods may connect to the broker's sockets if it runs at s0 (MCS:
        # a container can only reach a peer its categories dominate).
        cmd = ["runcon", "-l", "s0", *cmd]
    with (BDIR / "broker.log").open("a") as log:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True, cwd=str(BDIR))
    for _ in range(50):
        time.sleep(0.1)
        try:
            client("/state", {}, timeout=5)
            return
        except (OSError, BrokerError, ValueError):
            pass
    sys.exit(f"apod-broker: didn't start; see {BDIR / 'broker.log'}")


def add_keys(keys, agent, ttl=None):
    """Load keys into the broker's agent; ssh-add asks for passphrases on this terminal. Returns
    how many loaded."""
    n = 0
    for k in keys:
        cmd = ["ssh-add", "-q", "-t", ttl or os.environ.get("APOD_SSH_TTL", "4h")] + (["-c"] if k.get("confirm") else []) + [k["path"]]
        if subprocess.call(cmd, env=dict(os.environ, SSH_AUTH_SOCK=agent)):
            print(f"apod broker: couldn't load {k['path']}", file=sys.stderr)
        else:
            n += 1
    return n


def fmt_time(ts):
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


def print_event(e):
    tg = ", ".join(fmt_target(tuple(t)) for t in e.get("targets", []))
    line = f"{e['id']:>5}  {fmt_time(e['ts'])}  {e['profile']:<14} {e['decision']:<20} {e['request']}"
    if tg:
        line += f"  [{tg}]"
    if e.get("reason") and not e["decision"].startswith("allowed"):
        line += f"\n       {e['reason']}"
    print(line)


USAGE = """usage: apod broker [ui]                      approve requests in a terminal UI
       apod broker log [-f] [-n N] | pending | sessions | roles | scopes
       apod broker allow ID [--session|--role] | deny ID
       apod broker grants [rm ROLE ACTION] | keys | restart | stop"""


def main(argv, script):
    cmd, args = (argv[0], argv[1:]) if argv else ("help", [])
    if cmd == "serve":
        serve()
        return 0
    try:
        if cmd == "register":
            # register --id ID --profile P --profile-dir DIR --project DIR [--role R] [--gh-user U] [--scope S]... [--remote URL]...
            r = {"scope": [], "remotes": []}
            it = iter(args)
            for a in it:
                k = a.lstrip("-").replace("-", "_")
                if k in ("scope", "remote"):
                    r["scope" if k == "scope" else "remotes"].append(next(it))
                else:
                    r[k] = next(it)
            ensure(script)
            out = client("/register", r)
            for w in out.get("warnings", []):
                print(f"apod broker: {w}", file=sys.stderr)
            print(json.dumps(out))
        elif cmd == "unregister":
            client("/unregister", {"id": args[0]}, timeout=10)
        elif cmd == "ensure":
            ensure(script)
        elif cmd == "keys":
            ensure(script)
            r = client("/keys", {})
            if not r["keys"]:
                print(f"apod broker: no keys to load: neither {GLOBAL_CONFIG} nor a profile's broker.toml names one "
                      "([ssh] key or keys, [sign] key, [rad] key), and no running pod uses one", file=sys.stderr)
                return 0
            home = os.path.expanduser("~")
            short = lambda p: "~" + p[len(home):] if p.startswith(home + "/") else p
            width = max(len(short(k["path"])) for k in r["keys"])
            print(f"apod broker: keys for the broker's agent ({short(r['agent'])})")
            for k in r["keys"]:
                status = "loaded" if k["loaded"] else "error" if k.get("error") else "to load"
                print(f"  {status:<8} {short(k['path']):<{width}}  {', '.join(k['users'])}"
                      + (f"\n           {k['error']}" if k.get("error") else ""))
            sys.stdout.flush()
            todo = [k for k in r["keys"] if not k["loaded"] and not k.get("error")]
            n = add_keys(todo, r["agent"])
            print("apod broker: every key is already loaded, nothing to do" if not todo else
                  f"apod broker: loaded {n} of {len(todo)} key(s), for {os.environ.get('APOD_SSH_TTL', '4h')}",
                  file=sys.stderr)
        elif cmd in ("allow", "deny"):
            if not args:
                sys.exit(f"usage: apod broker {cmd} ID" + (" [--session|--role]" if cmd == "allow" else ""))
            how = ("deny" if cmd == "deny" else "role" if {"--role", "--persona"} & set(args) else
                   "session" if "--session" in args else "once")
            ev = client("/decide", {"id": int(args[0]), "how": how})
            print(f"apod broker: {'denied' if how == 'deny' else 'allowed (' + how + ')'}: {ev['request']}")
        elif cmd == "pending":
            st = client("/state", {})
            for e in st["pending"]:
                print_event(e)
            if not st["pending"]:
                print("apod broker: nothing waiting for approval", file=sys.stderr)
        elif cmd == "log":
            n = int(args[args.index("-n") + 1]) if "-n" in args else 30
            shown, first = {}, True
            while True:
                st = client("/state", {})
                for e in st["events"][-n:] if first else st["events"]:
                    if shown.get(e["id"]) != e["decision"]:
                        print_event(e)
                        shown[e["id"]] = e["decision"]
                sys.stdout.flush()
                if "-f" not in args:
                    break
                first = False
                time.sleep(1)
        elif cmd == "sessions":
            for s in client("/state", {})["sessions"]:
                granted = s["grants"]["actions"] + s["grants"]["unscoped"]
                print(f"{s['id']}  {s['profile']:<14} {s['role']:<11} {s['project']}\n"
                      f"    scope: {', '.join(s['scope_list']) or '-'}"
                      + (f"\n    granted: {', '.join(granted)}" if granted else ""))
        elif cmd == "grants":
            if args[:1] == ["rm"]:
                client("/grants/remove", {"role": args[1], "action": " ".join(args[2:])})
            for p, lst in load_grants()["roles"].items():
                for a in lst:
                    print(f"{p:<12} {a}")
        elif cmd in ("roles", "personas"):
            raw = load_roles()
            for name in raw:
                allow, deny = role_rules(name, raw)
                print(f"{name}: {raw[name].get('description', '')}\n  allow: {', '.join(allow)}"
                      + (f"\n  deny: {', '.join(deny)}" if deny else ""))
        elif cmd == "scopes":
            # The scope kinds the extensions declare (this process loads the same ones as the broker).
            for p, k in SCOPES.items():
                if k.entry:
                    print(f"{p + ':':<12} {k.help or '(no description)'}  [{k.provider or '?'}]")
            for name, f in extension_files().items():
                print(f"extension {name}: {f}", file=sys.stderr)
        elif cmd == "ui":
            import curses
            import tui
            ensure(script)
            curses.wrapper(tui.run)
        elif cmd == "stop":
            client("/stop", {}, timeout=5)
        elif cmd == "restart":
            try:
                client("/stop", {}, timeout=5)
                time.sleep(0.5)
            except (OSError, BrokerError):
                pass
            ensure(script)
        else:
            print(USAGE, file=sys.stderr)
            return 2
    except (OSError, BrokerError) as e:
        if isinstance(e, (FileNotFoundError, ConnectionRefusedError)):
            sys.exit("apod broker: the broker isn't running (it starts with the first pod that uses it)")
        sys.exit(f"apod broker: {e}")
    except (StopIteration, IndexError, ValueError) as e:  # a missing or malformed argument
        sys.exit(f"apod broker {cmd}: bad arguments ({e or 'missing value'})\n{USAGE}")
    return 0
