"""ssh provider: the pod's SSH_AUTH_SOCK is a filtering agent served by the broker. The keys stay
in the broker's own ssh-agent; a login signature goes through only to a host in the session's
scope. The destination is the one OpenSSH (8.9+) binds to the agent connection: the server's host
key, proven by its signature (session-bind@openssh.com, verified by the real ssh-agent), looked up
in the host's known_hosts. Optionally (the default), the pod's ssh connects through the broker
(ProxyCommand apod-ssh-connect), so the pod needs no route to the host and the broker checks the
destination before connecting.

Actions: ssh:connect, ssh:login. Targets: ("ssh", "USER@NAME"), NAME as in the scope
("devbox", "root@nas", "key:SHA256:..."); "@NAME" for a connection (no user yet). A scope entry
NAME covers logins as the user the host's ssh config gives NAME (its User, else yours) only;
USER@NAME, as USER only. On for a pod with an [ssh] key and hosts in scope ([ssh] hosts or --scope ssh:); the key is
asked for at launch only when the role allows ssh:login."""
import base64
import fnmatch
import hashlib
import os
import socket
import socketserver
import struct
import threading
from pathlib import Path

from api import (AGENT, POD_DIR, SHIM, BrokerError, Provider, Request, check, live_session, need_key, provider, run_quiet,
                 scope_kind, unix_listen)

FAILURE, SUCCESS, IDENTITIES, IDENTITIES_ANSWER, SIGN, EXTENSION = 5, 6, 11, 12, 13, 27


def fingerprint(blob):
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


class Reader:
    def __init__(self, data):
        self.d, self.i = data, 0

    def u8(self):
        self.i += 1
        return self.d[self.i - 1]

    def u32(self):
        self.i += 4
        return struct.unpack(">I", self.d[self.i - 4:self.i])[0]

    def string(self):
        n = self.u32()
        if self.i + n > len(self.d):
            raise ValueError("truncated")
        self.i += n
        return self.d[self.i - n:self.i]


def sshstr(b):
    return struct.pack(">I", len(b)) + b


def ssh_conf(name):
    """The host's ssh config for NAME (ssh -G), as a dict of lowercase keys."""
    r = run_quiet(["ssh", "-G", *(["-F", os.environ["APOD_SSH_CONFIG"]] if os.environ.get("APOD_SSH_CONFIG") else []), name])
    conf = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition(" ")
        conf.setdefault(k, v)
    return conf


def resolve(name):
    """What the host's ssh config makes of NAME: (hostname, port, known_hosts files, alias)."""
    conf = ssh_conf(name)
    files = (conf.get("userknownhostsfile", "~/.ssh/known_hosts") + " " +
             conf.get("globalknownhostsfile", "/etc/ssh/ssh_known_hosts")).split()
    return (conf.get("hostname", name), int(conf.get("port", 22)),
            [os.path.expanduser(f) for f in files if f != "none"], conf.get("hostkeyalias"))


_known = {}


def host_keys(name):
    """Host key blobs the host's known_hosts has for NAME (an ssh config name)."""
    if name in _known:
        return _known[name]
    hostname, port, files, alias = resolve(name)
    lookups = {alias or hostname, name}
    if port != 22:
        lookups |= {f"[{alias or hostname}]:{port}", f"[{name}]:{port}"}
    blobs = set()
    for f in files:
        if not os.path.exists(f):
            continue
        for lk in lookups:
            for line in run_quiet(["ssh-keygen", "-F", lk, "-f", f]).stdout.splitlines():
                parts = line.split()
                if len(parts) >= 3 and not line.startswith("#"):
                    try:
                        blobs.add(base64.b64decode(parts[2]))
                    except ValueError:
                        pass
    _known[name] = blobs
    return blobs


def names_for_key(sess, blob):
    """Scope names whose known host keys include BLOB, else names in unhashed known_hosts."""
    fp = fingerprint(blob)
    names = []
    for entry in sess.entries("ssh"):
        name = entry.rpartition("@")[2]
        if name.lower() == f"key:{fp}".lower() or blob in host_keys(name):
            names.append(name)
    if names:
        return names, fp
    for f in resolve("localhost")[2]:
        try:
            for line in Path(f).read_text().splitlines():
                parts = line.split()
                if len(parts) >= 3 and not parts[0].startswith("|") and base64.b64decode(parts[2]) == blob:
                    names += parts[0].split(",")
        except (OSError, ValueError):
            pass
    return names, fp


class AgentProxy(socketserver.StreamRequestHandler):
    """One pod agent connection, relayed to its own connection to the broker's ssh-agent (so the
    agent's per-connection session bindings line up)."""

    def read_msg(self, f):
        hdr = f.read(4)
        if len(hdr) < 4:
            return None
        n = struct.unpack(">I", hdr)[0]
        if n > 256 * 1024:
            return None
        return f.read(n)

    def send(self, msg):
        self.wfile.write(struct.pack(">I", len(msg)) + msg)
        self.wfile.flush()

    def upstream(self, msg):
        self.up.sendall(struct.pack(">I", len(msg)) + msg)
        return self.read_msg(self.upf)

    def handle(self):
        sess = live_session(self.server.session_id)
        try:
            self.up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.up.connect(str(AGENT))
        except OSError:
            return
        self.upf = self.up.makefile("rb")
        self.binds = []
        try:
            while sess:
                msg = self.read_msg(self.rfile)
                if not msg:
                    break
                self.send(self.dispatch(sess, msg))
        except (OSError, ValueError, IndexError, struct.error):
            pass
        finally:
            self.up.close()

    def dispatch(self, sess, msg):
        cfg = sess.cfg.get("ssh") or {}
        allowed = {base64.b64decode(b) for b in cfg.get("blobs", [])}
        t = msg[0]
        if t == IDENTITIES:
            ans = self.upstream(msg)
            if not ans or ans[0] != IDENTITIES_ANSWER:
                return bytes([FAILURE])
            r, keep = Reader(ans[1:]), []
            for _ in range(r.u32()):
                blob, comment = r.string(), r.string()
                if blob in allowed:
                    keep.append(sshstr(blob) + sshstr(comment))
            return bytes([IDENTITIES_ANSWER]) + struct.pack(">I", len(keep)) + b"".join(keep)
        if t == EXTENSION:
            r = Reader(msg[1:])
            name = r.string()
            if name != b"session-bind@openssh.com":
                return bytes([FAILURE])
            ans = self.upstream(msg)
            if ans and ans[0] == SUCCESS:  # the real agent checked the host key's signature
                hostkey, sid = r.string(), r.string()
                r.string()
                self.binds.append((hostkey, sid, bool(r.u8())))
            return ans or bytes([FAILURE])
        if t == SIGN:
            r = Reader(msg[1:])
            key, data = r.string(), r.string()
            if key not in allowed:
                return bytes([FAILURE])
            if not self.login_allowed(sess, data):
                return bytes([FAILURE])
            return self.upstream(msg) or bytes([FAILURE])
        return bytes([FAILURE])  # adding, removing, locking keys: not from a pod

    def login_allowed(self, sess, data):
        try:
            r = Reader(data)
            sid = r.string()
            if r.u8() != 50:
                raise ValueError
            user = r.string().decode()
            r.string()
            method = r.string()
            if not method.startswith(b"publickey"):
                raise ValueError
        except (ValueError, IndexError, struct.error, UnicodeDecodeError):
            req = Request("ssh", "ssh agent: sign something that isn't an SSH login")
            req.actions, req.targets = ["ssh:sign other"], [("unscoped", "not an ssh login")]
            return check(sess, req)[0]
        bound = [b for b in self.binds if b[1] == sid]
        if not bound:
            req = Request("ssh", f"ssh login as {user} to an unverified host")
            req.actions = ["ssh:login"]
            req.targets = [("unscoped", "ssh login without a host binding (OpenSSH before 8.9?)")]
            return check(sess, req)[0]
        hops = [b for b in self.binds if b[2]] + bound[-1:]
        targets, labels = [], []
        for hostkey, _, _ in hops:
            names, fp = names_for_key(sess, hostkey)
            inscope = [n for n in names if sess.in_scope(("ssh", f"{user}@{n}"))]
            name = (inscope or names or [f"key:{fp}"])[0]
            targets.append(("ssh", f"{user}@{name}" if inscope or not names else f"{user}@key:{fp}"))
            labels.append(f"{name}" + ("" if inscope or name.startswith("key:") else f" ({fp})"))
        req = Request("ssh", f"ssh login as {user} to {' via '.join(reversed(labels))}")
        req.actions = ["ssh:login"]
        req.targets = targets
        return check(sess, req)[0]


def connect_target(sess, host):
    """Is HOST (the pod's %h) a scope name, or what a scope name resolves to? -> (name, addr, port)."""
    for entry in sess.entries("ssh"):
        name = entry.rpartition("@")[2]
        if name.startswith("key:"):
            continue
        hostname, port, _, _ = resolve(name)
        if host.lower() in (name.lower(), hostname.lower()):
            return name, hostname, port
    return None, host, None


def handle_connect(h, sess, method):
    host, _, port = h.path.rpartition(":")
    if not host or not port.isdigit():
        return h.send_json({"error": "CONNECT host:port"}, 400)
    name, addr, cport = connect_target(sess, host)
    req = Request("ssh", f"ssh connect {host}:{port}")
    req.actions = ["ssh:connect"]
    if port != "22" and (name is None or int(port) != cport):
        # Scope names a host for ssh; another port on it is a tunnel to something else.
        req.actions.append("ssh:connect port")
    req.targets = [("ssh", f"@{name or host}")]
    ok, ev = check(sess, req)
    if not ok:
        return h.send_json({"error": f"connection to {host} rejected ({ev['decision']}): {ev['reason']}"}, 403)
    if name is None:
        addr, cport, _, _ = resolve(host)
    try:
        up = socket.create_connection((addr, cport if port == "22" else int(port)), timeout=20)
    except OSError as e:
        return h.send_json({"error": f"can't connect to {addr}: {e}"}, 502)
    up.settimeout(None)
    h.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    h.wfile.flush()
    pod = h.connection

    def pipe(a, b):
        try:
            while True:
                d = a.recv(65536)
                if not d:
                    break
                b.sendall(d)
        except OSError:
            pass
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    t = threading.Thread(target=pipe, args=(up, pod), daemon=True)
    t.start()
    pipe(pod, up)
    t.join()
    up.close()


class SSHProvider(Provider):
    name = "ssh"
    description = "ssh logins to the hosts in scope, with the broker's agent, through the host"
    guide = "docs/guides.md#ssh"
    routes = [("CONNECT", "", handle_connect)]

    def keys(self, cfg):
        return [(k, cfg.get("confirm", False)) for k in cfg.get("keys") or ([cfg["key"]] if cfg.get("key") else [])]

    def targets(self, cfg, reg):
        return [("ssh", h) for h in cfg.get("hosts", [])]

    def configure(self, sess, cfg, reg, out):
        keys = cfg.get("keys") or ([cfg["key"]] if cfg.get("key") else [])
        if not keys:
            return False
        blobs = []
        for k in keys:
            try:
                blobs.append(need_key(sess, out, k, cfg.get("confirm", False), "ssh logins",
                                      actions=["ssh:login"])[1])
            except BrokerError as e:
                out["context"].append(f"SSH key {k} unavailable: {e}.")
        if not blobs:
            return False
        sess.cfg["ssh"] = {"blobs": blobs}
        out["env"]["SSH_AUTH_SOCK"] = f"{POD_DIR}/agent.sock"
        hosts = ", ".join(sess.entries("ssh")) or "none yet"
        if cfg.get("proxy", True):
            # The pod's ssh connects through the broker, and trusts the host's known keys for the scope.
            known = []
            for entry in sess.entries("ssh"):
                name = entry.rpartition("@")[2]
                for b in host_keys(name):
                    known.append(f"{name} {Reader(b).string().decode()} {base64.b64encode(b).decode()}")
            sess.dir.mkdir(parents=True, exist_ok=True)
            (sess.dir / "known_hosts").write_text("\n".join(known) + "\n")
            # `ssh NAME` logs in as the host's ssh would (or as the scope entry's USER@): the pod's
            # own user name means nothing to the remote host.
            users = {}
            for entry in sess.entries("ssh"):
                user, _, name = entry.rpartition("@")
                if not any(c in name for c in "*?[") and not name.startswith("key:"):
                    users.setdefault(name, user or ssh_conf(name).get("user", ""))
            (sess.dir / "ssh_config").write_text(
                "# apod broker: connections go through the host, which checks the destination\n"
                + "".join(f"Host {n}\n    User {u}\n" for n, u in users.items() if u)
                + "Host *\n    ProxyCommand /usr/local/bin/apod-ssh-connect %h %p\n"
                f"    GlobalKnownHostsFile {POD_DIR}/known_hosts\n")
            out["mounts"] += [[str(SHIM), "/usr/local/bin/apod-ssh-connect", "ro"],
                              [str(sess.dir / "ssh_config"), "/etc/ssh/ssh_config.d/50-apod-broker.conf", "ro"]]
            out["context"].append(f"ssh: connections go through the host (ProxyCommand), to hosts in scope ({hosts}); "
                                  "`ssh NAME` uses the host's ssh config for NAME (HostName, Port, User).")
        out["context"].append(f"ssh: SSH_AUTH_SOCK is the broker's agent, which signs logins only to hosts in "
                              f"scope ({hosts}); the keys aren't in the pod. Logging in to another host waits for "
                              f"the user's approval.")
        return True

    def listen(self, sess):
        if not sess.cfg.get("ssh"):
            return []
        return [unix_listen(sess.dir / "agent.sock", AgentProxy, sess.id)]


provider(SSHProvider())


def config_user(name):
    """The user the host's ssh config logs in to NAME as (its User, else your user name)."""
    return ssh_conf(name).get("user", "").lower()


def _ssh_match(sess, v):
    # v is USER@NAME (a login) or @NAME (a connection, no user yet). An entry USER@NAME allows
    # that user only; an entry NAME only the user the host's ssh config gives NAME, so that
    # `ssh root@NAME` isn't covered by it. A connection is allowed to any host with an entry.
    v = v.lower()
    user, _, name = v.rpartition("@")
    pats = [p.lower() for p in sess.entries("ssh")]
    if not user:
        return any(fnmatch.fnmatchcase(name, p.rpartition("@")[2]) for p in pats)
    for p in pats:
        if "@" in p:
            if fnmatch.fnmatchcase(v, p):
                return True
        elif fnmatch.fnmatchcase(name, p) and user == config_user(name):
            return True
    return False


scope_kind("ssh", provider="ssh", match=_ssh_match, old=("host",),
           grant=lambda v: ("ssh", v[1:] if v.startswith("@") else v),  # a connection: that host, as its configured user
           label=lambda v: f"ssh:{v[1:] if v.startswith('@') else v}",
           help="ssh:HOST (as the user the host's ssh config gives HOST) or ssh:USER@HOST (as USER), "
                "HOST as in the host's ssh config")
