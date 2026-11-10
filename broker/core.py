"""apod broker core: sessions, personas, scope, approvals, the audit log and the servers.

The broker runs on the host and does things for apod pods that would otherwise need credentials
in the pod. Each pod gets a session (a profile, a persona and a scope) and a socket directory
mounted at /run/apod-broker. Providers (gh, github, rad, sign, ssh) turn what a pod asks for into
actions ("gh:pr create", "github:push branch", "sign:commit", "ssh:login") and targets (a
repository, a project, a host, a git identity). A request goes ahead when the persona allows every
action and every target is in the session's scope; anything else waits for the user's approval
(apod broker ui, apod broker allow) until it times out.
"""
import collections
import fnmatch
import http.client
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
PERSONA_FILES = [CHECKOUT / "personas.toml", ROOT / "personas.toml"]
SHIM = HERE / "pod-shim"
POD_DIR = "/run/apod-broker"
ASK_TIMEOUT = float(os.environ.get("APOD_BROKER_ASK_TIMEOUT", "90"))
if ASK_TIMEOUT.is_integer():
    ASK_TIMEOUT = int(ASK_TIMEOUT)

# Scope kinds and how they're written in broker.toml's scope list and --scope ("" for the two
# GitHub kinds: OWNER/REPO and OWNER). Providers add their own with scope_kind().
SCOPE_KINDS = ["repo", "owner", "project", "rad", "host", "identity"]
SCOPE_PREFIX = {"project": "project:", "rad": "rad:", "host": "ssh:", "identity": "email:"}
EXTENSIONS = ROOT / "broker-extensions"  # the user's own providers, *.py, loaded after the built-in ones


class BrokerError(Exception):
    pass


LOCK = threading.RLock()
SESSIONS = {}   # id -> Session
SERVERS = {}    # id -> [servers]
PENDING = {}    # event id -> Pending
EVENTS = collections.deque(maxlen=500)
COUNTER = itertools.count(1)
PROVIDERS = {}  # name -> provider
ROUTES = []     # (method, path prefix, handler(h, sess, method))


def now():
    return time.time()


def provider(p):
    """Register a provider: an object with name and configure(sess, cfg, reg, out) -> bool, and
    optionally routes [(method, path prefix, handler(h, sess, method))], listen(sess) -> [servers]
    and close(session id). See README.md, "Writing a broker extension"."""
    if p.name in PROVIDERS:
        raise BrokerError(f"a provider named {p.name} is already registered")
    PROVIDERS[p.name] = p
    ROUTES.extend(getattr(p, "routes", []))
    return p


def scope_kind(kind, prefix):
    """Add a scope kind, written PREFIXvalue in broker.toml and --scope (e.g. "url", "url:"). A
    target (kind, value) is in scope when it matches one of the session's entries of that kind
    (shell-style patterns, case-insensitive)."""
    if not re.fullmatch(r"[a-z][a-z0-9-]*", kind) or not prefix.endswith(":"):
        raise BrokerError(f"bad scope kind {kind!r} or prefix {prefix!r}")
    if kind not in SCOPE_KINDS:
        SCOPE_KINDS.append(kind)
    SCOPE_PREFIX[kind] = prefix


def load_extensions():
    """Import ~/.agent-pod/broker-extensions/*.py, in name order; each registers its providers.
    One that fails is skipped, with the error on stderr (the broker's log)."""
    import importlib.util
    for f in sorted(EXTENSIONS.glob("*.py")) if EXTENSIONS.is_dir() else []:
        try:
            spec = importlib.util.spec_from_file_location(f"apod_ext_{f.stem}", f)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
        except Exception as e:  # noqa: BLE001 (a broken extension mustn't stop the broker)
            print(f"apod-broker: extension {f} not loaded: {e!r}", file=sys.stderr, flush=True)


def expand(path):
    return Path(os.path.expanduser(str(path)))


def run_quiet(cmd, **kw):
    kw.setdefault("stdin", subprocess.DEVNULL)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 30), **kw)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


# ---------------------------------------------------------------------------------------------
# Personas, grants, scope

def load_personas():
    raw = {}
    for f in PERSONA_FILES:
        if f.exists():
            raw.update(tomllib.loads(f.read_text()).get("personas", {}))
    return raw


def load_grants():
    try:
        return json.loads(GRANTS.read_text())
    except (OSError, ValueError):
        return {"personas": {}}


def save_grants(g):
    tmp = GRANTS.with_suffix(".tmp")
    tmp.write_text(json.dumps(g, indent=2) + "\n")
    tmp.replace(GRANTS)


def persona_rules(name, raw=None, seen=None):
    """(allow, deny) patterns of a persona, as "provider:action": its own (one list per provider),
    its grants, and those of the personas it extends."""
    raw = load_personas() if raw is None else raw
    seen = set() if seen is None else seen
    if name not in raw:
        raise BrokerError(f"no persona {name} (known: {', '.join(sorted(raw)) or 'none'})")
    if name in seen:
        return [], []
    seen.add(name)
    p = raw[name]
    allow = list(p.get("allow", [])) + list(load_grants()["personas"].get(name, []))
    deny = list(p.get("deny", []))
    for pname, rules in p.items():
        if pname in PROVIDERS and isinstance(rules, list):
            allow += [f"{pname}:{a}" for a in rules]
    ext = p.get("extends", [])
    for e in [ext] if isinstance(ext, str) else ext:
        a, d = persona_rules(e, raw, seen)
        allow += a
        deny += d
    return allow, deny


def matches(action, patterns):
    return any(fnmatch.fnmatchcase(action, p) for p in patterns)


def parse_scope(entry):
    """A scope entry as written by the user -> (kind, value)."""
    e = entry.strip()
    for kind, prefix in SCOPE_PREFIX.items():
        if e.lower().startswith(prefix):
            return kind, e[len(prefix):].strip()
    if "/" in e:
        return "repo", e.lower().removesuffix(".git")
    return "owner", e.lower()


def fmt_target(t):
    kind, v = t
    return {"repo": v, "owner": f"owner {v}", "project": f"project {v}", "projects-of": f"projects of {v}",
            "forkdest": f"fork into {v}", "rad": f"rad:{v}", "host": f"ssh {v}", "identity": f"as {v}",
            "unscoped": f"unscoped: {v}"}.get(kind, f"{kind} {v}")


def fmt_scope(kind, v):
    return v if kind in ("repo", "owner") else f"{SCOPE_PREFIX.get(kind, kind + ':')}{v}"


class Session:
    FIELDS = ("id", "profile", "persona", "project", "scope", "grants", "cfg", "identity", "created")

    def __init__(self, d):
        for k in self.FIELDS:
            setattr(self, k, d.get(k))
        self.scope = {k: list(v) for k, v in {**{k: [] for k in SCOPE_KINDS}, **(self.scope or {})}.items()}
        self.grants = {k: list((self.grants or {}).get(k, [])) for k in ("actions", "unscoped")}
        self.cfg = self.cfg or {}
        self.identity = self.identity or {}
        self.temp = {}  # (kind, value) -> expiry, from a one-off approval that spans two requests

    def data(self):
        return {k: getattr(self, k) for k in self.FIELDS}

    @property
    def dir(self):
        return SDIR / self.id

    @property
    def login(self):
        return (self.cfg.get("gh") or {}).get("login", "")

    def save(self):
        (self.dir / "session.json").write_text(json.dumps(self.data(), indent=2) + "\n")

    def in_scope(self, t):
        kind, v = t[0], t[1]
        if self.temp.get((kind, v), 0) > now():
            return True
        lv = v.lower()
        pats = lambda k: [p.lower() for p in self.scope.get(k, [])]
        if kind == "forkdest":
            return lv == self.login.lower() or lv in pats("owner")
        if kind == "projects-of":
            return lv in pats("owner") or any(p.split("/")[0] == lv for p in pats("project"))
        if kind == "unscoped":
            return v in self.grants["unscoped"]
        if kind == "host":
            # v is USER@NAME; an entry NAME allows any user, USER@NAME that user only.
            user, _, name = lv.rpartition("@")
            if not user:  # a connection: any entry for that host
                return any(fnmatch.fnmatchcase(name, p.rpartition("@")[2]) for p in pats("host"))
            return any(fnmatch.fnmatchcase(lv if "@" in p else name, p) for p in pats("host"))
        return any(fnmatch.fnmatchcase(lv, p) for p in pats(kind))

    def add_scope(self, t):
        kind, v = t[0], t[1]
        if kind == "unscoped":
            lst, v = self.grants["unscoped"], v
        elif kind == "projects-of":
            lst, v = self.scope["project"], f"{v}/*"
        elif kind == "forkdest":
            lst = self.scope["owner"]
        elif kind == "host":  # "@NAME" (a connection) allows NAME for any user; USER@NAME stays that user's
            lst, v = self.scope["host"], v[1:] if v.startswith("@") else v
        else:
            lst = self.scope.setdefault(kind, [])
        if v not in lst:
            lst.append(v)

    def scope_list(self):
        return [fmt_scope(k, v) for k in self.scope for v in self.scope[k]]

    def summary(self):
        allow, _ = persona_rules(self.persona)
        return {"persona": self.persona, "login": self.login, "scope": self.scope_list(),
                "allowed": sorted(set(allow)), "identity": self.identity, "grants": self.grants,
                "name": self.title(), "text": self.describe()}

    def describe(self):
        """What this pod may do, for the agent: its system prompt at launch, gh auth status later
        (which also shows what the user approved since)."""
        allow, _ = persona_rules(self.persona)
        active = self.cfg.get("_providers") or list(PROVIDERS)
        by = collections.defaultdict(list)
        for a in sorted(set(allow)):
            prov, _, act = a.partition(":")
            by[prov].append(act)
        lines = [f"apod broker: persona {self.persona}" + (f", GitHub user {self.login}" if self.login else "") + ".",
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
          "persona": sess.persona, "login": sess.login, "project": sess.project, "kind": req.kind,
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


HARD_DENY = []  # extended by providers: never allowed, whatever the persona or grants say


def evaluate(sess, req):
    allow, deny = persona_rules(sess.persona)
    allow = allow + sess.grants["actions"]
    hard = [a for a in req.actions if matches(a, HARD_DENY + deny)]
    if hard:
        return "deny", [], [], f"never allowed: {', '.join(hard)}"
    missing = [a for a in req.actions if not matches(a, allow)]
    out = [list(t) for t in req.targets if not sess.in_scope(t)]
    reasons = []
    if missing:
        reasons.append(f"not allowed for persona {sess.persona}: {', '.join(missing)}")
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
            f"On the host: apod broker allow {ev['id']} [--session|--persona], apod broker deny {ev['id']}, "
            f"or apod broker ui")


def decide(eid, how):
    """Approve (once, for the session, or for the persona) or deny a request. Approving a past
    request for the session or persona adds its grants so that a retry goes through."""
    if how not in ("once", "session", "persona", "deny"):
        raise BrokerError(f"unknown decision {how}")
    with LOCK:
        p = PENDING.get(eid)
        ev = p.ev if p else next((e for e in EVENTS if e["id"] == eid), None)
        if ev is None:
            raise BrokerError(f"no request {eid}")
        if not p and how in ("once", "deny"):
            raise BrokerError(f"request {eid} is no longer waiting; use --session or --persona to allow a retry")
        sess = SESSIONS.get(ev["session"])
        if how in ("session", "persona"):
            if how == "persona":
                g = load_grants()
                lst = g["personas"].setdefault(ev["persona"], [])
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

def read_body(h):
    if h.headers.get("Transfer-Encoding", "").lower() == "chunked":
        chunks = []
        while True:
            n = int(h.rfile.readline().split(b";")[0].strip() or b"0", 16)
            if n == 0:
                while h.rfile.readline() not in (b"\r\n", b"\n", b""):
                    pass
                break
            chunks.append(h.rfile.read(n))
            h.rfile.readline()
        return b"".join(chunks)
    return h.rfile.read(int(h.headers.get("Content-Length") or 0))


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
        for m, prefix, fn in ROUTES:
            if m == method and self.path.startswith(prefix):
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


def register(r):
    sid = r.get("id") or secrets.token_hex(6)
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", sid):
        raise BrokerError("bad session id")
    pdir = Path(r.get("profile_dir") or ROOT / r.get("profile", ""))
    try:
        cfg = tomllib.loads((pdir / "broker.toml").read_text()) if (pdir / "broker.toml").exists() else {}
    except tomllib.TOMLDecodeError as e:
        raise BrokerError(f"{pdir / 'broker.toml'}: {e}")
    persona = r.get("persona") or cfg.get("persona") or ""
    if not persona:
        raise BrokerError(f"no persona: set persona in {pdir / 'broker.toml'} or pass --persona")
    persona_rules(persona)  # fails on an unknown persona
    env = read_env_file(pdir / "env")
    identity = {"name": env.get("GIT_COMMITTER_NAME") or env.get("GIT_AUTHOR_NAME", ""),
                "emails": sorted({e for e in (env.get("GIT_AUTHOR_EMAIL"), env.get("GIT_COMMITTER_EMAIL")) if e})}
    sess = Session({"id": sid, "profile": r.get("profile", ""), "persona": persona,
                    "project": r.get("project", ""), "created": now(), "identity": identity,
                    "cfg": {"_home": str(pdir / "home")}})
    for e in list(cfg.get("scope", [])) + list(r.get("scope", [])):
        if e.strip():
            sess.add_scope(parse_scope(e))
    for e in identity["emails"]:
        sess.add_scope(("identity", e))
    out = {"env": {"APOD_BROKER_SOCK": f"{POD_DIR}/broker.sock"}, "git_config": [], "mounts": [],
           "context": [], "need_keys": [], "providers": []}
    start_agent()
    for p in PROVIDERS.values():
        if p.configure(sess, cfg.get(p.name) or {}, r, out):
            out["providers"].append(p.name)
    with LOCK:
        if sid in SESSIONS:
            unregister(sid)
        SESSIONS[sid] = sess
        listen_session(sess)
        sess.save()
    sess.cfg["_providers"] = out["providers"]
    sess.save()
    head = "The host's apod broker acts for this pod.\n" + sess.describe()
    return {"id": sid, "dir": str(sess.dir), "pod_dir": POD_DIR, "ask_timeout": ASK_TIMEOUT,
            "agent": str(AGENT), **out, "context": "\n".join([head] + out["context"]), **sess.summary()}


def unregister(sid):
    with LOCK:
        SESSIONS.pop(sid, None)
        for srv in SERVERS.pop(sid, []):
            srv.shutdown()
            srv.server_close()
        shutil.rmtree(SDIR / sid, ignore_errors=True)
        for p in PROVIDERS.values():
            if hasattr(p, "close"):
                p.close(sid)


def state():
    with LOCK:
        return {"sessions": [dict(s.data(), scope_list=s.scope_list(), name=s.title()) for s in SESSIONS.values()],
                "pending": [p.ev for p in PENDING.values()],
                "events": list(EVENTS)[-200:],
                "grants": load_grants()["personas"],
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


def admin_call(path, body):
    if path == "/register":
        return register(body)
    if path == "/unregister":
        unregister(body["id"])
        return {"ok": True}
    if path == "/decide":
        return decide(int(body["id"]), body["how"])
    if path == "/grants/remove":
        g = load_grants()
        lst = g["personas"].get(body["persona"], [])
        if body["action"] in lst:
            lst.remove(body["action"])
        save_grants(g)
        return {"ok": True}
    if path == "/scope/remove":
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
        return {"need_keys": needed_keys(), "agent": str(AGENT)}
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
        except (BrokerError, KeyError, ValueError) as e:
            self.send_json({"error": str(e)}, 400)


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
        try:
            sess = Session(json.loads((d / "session.json").read_text()))
        except (OSError, ValueError):
            shutil.rmtree(d, ignore_errors=True)
            continue
        try:
            SESSIONS[sess.id] = sess
            listen_session(sess)
        except (OSError, BrokerError) as e:
            SESSIONS.pop(sess.id, None)
            print(f"apod-broker: dropping session {sess.id}: {e}", file=sys.stderr)
            shutil.rmtree(d, ignore_errors=True)

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
    with (BDIR / "broker.log").open("a") as log:
        subprocess.Popen([sys.executable, str(script), "serve"], stdin=subprocess.DEVNULL,
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
    """Load keys into the broker's agent; ssh-add asks for passphrases on this terminal."""
    for k in keys:
        cmd = ["ssh-add", "-q", "-t", ttl or os.environ.get("APOD_SSH_TTL", "4h")] + (["-c"] if k.get("confirm") else []) + [k["path"]]
        if subprocess.call(cmd, env=dict(os.environ, SSH_AUTH_SOCK=agent)):
            print(f"apod broker: couldn't load {k['path']}", file=sys.stderr)


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
       apod broker log [-f] [-n N] | pending | sessions | personas
       apod broker allow ID [--session|--persona] | deny ID
       apod broker grants [rm PERSONA ACTION] | keys | restart | stop"""


def main(argv, script):
    cmd, args = (argv[0], argv[1:]) if argv else ("help", [])
    if cmd == "serve":
        serve()
        return 0
    try:
        if cmd == "register":
            # register --id ID --profile P --profile-dir DIR --project DIR [--persona P] [--scope S]... [--remote URL]...
            r = {"scope": [], "remotes": []}
            it = iter(args)
            for a in it:
                k = a.lstrip("-").replace("-", "_")
                if k in ("scope", "remote"):
                    r["scope" if k == "scope" else "remotes"].append(next(it))
                else:
                    r[k] = next(it)
            ensure(script)
            print(json.dumps(client("/register", r)))
        elif cmd == "unregister":
            client("/unregister", {"id": args[0]}, timeout=10)
        elif cmd == "ensure":
            ensure(script)
        elif cmd == "keys":
            ensure(script)
            r = client("/keys", {})
            add_keys(r["need_keys"], r["agent"])
            if not r["need_keys"]:
                print("apod broker: the broker's agent has every key the sessions use", file=sys.stderr)
        elif cmd in ("allow", "deny"):
            if not args:
                sys.exit(f"usage: apod broker {cmd} ID" + (" [--session|--persona]" if cmd == "allow" else ""))
            how = "deny" if cmd == "deny" else "persona" if "--persona" in args else "session" if "--session" in args else "once"
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
                print(f"{s['id']}  {s['profile']:<14} {s['persona']:<11} {s['project']}\n"
                      f"    scope: {', '.join(s['scope_list']) or '-'}"
                      + (f"\n    granted: {', '.join(granted)}" if granted else ""))
        elif cmd == "grants":
            if args[:1] == ["rm"]:
                client("/grants/remove", {"persona": args[1], "action": " ".join(args[2:])})
            for p, lst in load_grants()["personas"].items():
                for a in lst:
                    print(f"{p:<12} {a}")
        elif cmd == "personas":
            raw = load_personas()
            for name in raw:
                allow, deny = persona_rules(name, raw)
                print(f"{name}: {raw[name].get('description', '')}\n  allow: {', '.join(allow)}"
                      + (f"\n  deny: {', '.join(deny)}" if deny else ""))
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
    return 0
