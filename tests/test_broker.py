#!/usr/bin/env python3
"""End-to-end tests for apod-broker and its pod shim, without GitHub, Radicle or sshd: a fake gh
(the real gh for --help, canned answers otherwise), a local git upstream (git http-backend), a
plain repository standing in for Radicle storage, real ssh-agents and ssh-keygen.
Run: python3 tests/test_broker.py (KEEP=1 keeps the temporary directory; APOD_TEST_TMP=DIR puts it
there instead of tmp/). apod's folders are throwaway ones, set with APOD_CONFIG_DIR, APOD_DATA_DIR,
APOD_STATE_DIR and XDG_RUNTIME_DIR (or APOD_ROOT, for the legacy layout), never your own; the
sockets are in a short runtime folder, so a long checkout path is fine.
"""
import base64
import itertools
import socket
import struct
import http.server
import getpass
import json
import re
import os
import shutil
import socketserver
import ssl
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "broker"))
import paths  # noqa: E402 (where apod's files are, as the broker sees them)

BROKER = HERE / "apod-broker"
# The variables that say where apod's folders are: the tests set their own.
LAYOUT_VARS = ("APOD_ROOT", "APOD_CONFIG_DIR", "APOD_DATA_DIR", "APOD_STATE_DIR", "APOD_RUNTIME_DIR",
               "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR")


def host_env(**more):
    """os.environ without what says where apod's folders are (and the pod's own git setup)."""
    env = {k: v for k, v in os.environ.items() if k not in LAYOUT_VARS and k not in ("GH_REPO", "SSH_AUTH_SOCK")
           and not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG"))}
    return dict(env, **more)
SHIM = HERE / "broker/pod-shim"
RID = "z3gqcJUoA1n9HaHKufZs5FCSGazv5"
NID = "z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK"
REAL_GH = shutil.which("gh")

FAKE_GH = textwrap.dedent(f"""\
    #!/usr/bin/env python3
    import json, os, sys
    a = sys.argv[1:]
    if "--help" in a:
        os.execv({REAL_GH!r}, [{REAL_GH!r}] + a)
    if a[:2] == ["auth", "token"]:
        print("tok-" + (a[a.index("--user") + 1] if "--user" in a else "active")); sys.exit()
    if a[:2] == ["api", "user"]:
        print("octo"); sys.exit()
    if a[:1] == ["api"] and "/compare/" in a[1]:  # is A an ancestor of B, in the test's upstream
        import subprocess
        repo, _, ab = a[1][len("repos/"):].partition("/compare/")
        x, _, y = ab.partition("...")
        r = subprocess.run(["git", "-C", f"@UPSTREAM@/{{repo}}.git", "merge-base", "--is-ancestor", x, y])
        print("identical" if x == y else "ahead" if r.returncode == 0 else "diverged"); sys.exit()
    if a[:1] == ["api"] and a[1].startswith("repos/") and "--jq" in a:
        print(json.dumps({{"default_branch": "main", "private": a[1] != "repos/other/public"}})); sys.exit()
    if a[:2] == ["api", "graphql"] and any("nodes(ids:" in x for x in a):
        ids = [x.split("=", 1)[1] for x in a if x.startswith("ids[]=")]
        table = {{"PVT_kwDOAAA111": {{"__typename": "ProjectV2", "number": 5, "owner": {{"login": "acme"}}}},
                 "PVT_kwDOBBB222": {{"__typename": "ProjectV2", "number": 9, "owner": {{"login": "evil"}}}},
                 "PVTI_lADOAAA444": {{"__typename": "ProjectV2Item", "project": {{"number": 5, "owner": {{"login": "acme"}}}}}},
                 "I_kwDOAAA333": {{"__typename": "Issue", "repository": {{"nameWithOwner": "acme/widget"}}}}}}
        print(json.dumps({{"data": {{"nodes": [table.get(i) for i in ids]}}}})); sys.exit()
    stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
    files = {{}}
    for x in a:
        for c in [x, x.split("=", 1)[-1], x.split("=@", 1)[-1]]:
            if os.path.isfile(c):
                files[c.rsplit("/", 1)[-1]] = open(c).read()
    print(json.dumps({{"argv": a, "GH_REPO": os.environ.get("GH_REPO"), "GH_TOKEN": os.environ.get("GH_TOKEN"),
                      "stdin": stdin, "files": files, "cwd_has_git": os.path.exists(".git")}}))
""")


class CGIUpstream(http.server.BaseHTTPRequestHandler):
    """A GitHub stand-in for git: git http-backend over the bare repos in ROOT."""
    protocol_version = "HTTP/1.0"
    root = None
    seen = []

    def log_message(self, *a):
        pass

    def run(self):
        path, _, query = self.path.partition("?")
        CGIUpstream.seen.append((self.command, path, self.headers.get("Authorization")))
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        env = {"GIT_PROJECT_ROOT": str(self.root), "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": path,
               "QUERY_STRING": query, "REQUEST_METHOD": self.command, "REMOTE_USER": "x",
               "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
               "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
               "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""), "PATH": os.environ["PATH"]}
        r = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True)
        head, _, out = r.stdout.partition(b"\r\n\r\n")
        status = 200
        hdrs = []
        for line in head.decode().split("\r\n"):
            k, _, v = line.partition(": ")
            if k.lower() == "status":
                status = int(v.split()[0])
            elif k:
                hdrs.append((k, v))
        self.send_response(status)
        for k, v in hdrs:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = run


FAKE_REMOTE_RAD = textwrap.dedent("""\
    #!/usr/bin/env python3
    # A stand-in for git-remote-rad: pushes into our namespace in storage, $FAKE_RAD_ONWARD/<rid>
    # (refs/patches becomes refs/heads/patches-pushed), records push options next to it.
    import os, subprocess, sys
    url = sys.argv[2]
    rid = url.split("://", 1)[1].split("/")[0]
    onward = os.path.join(os.environ["FAKE_RAD_ONWARD"], rid)
    opts, out = [], sys.stdout
    for line in sys.stdin:
        line = line.rstrip("\\n")
        if line == "capabilities":
            out.write("push\\noption\\n\\n")
        elif line.startswith("option push-option "):
            opts.append(line.split(" ", 2)[2]); out.write("ok\\n")
        elif line.startswith("option "):
            out.write("unsupported\\n")
        elif line.startswith("list"):
            ns = "refs/namespaces/" + os.environ["FAKE_RAD_NID"] + "/"
            refs = subprocess.run(["git", "-C", onward, "for-each-ref", "--format=%(objectname) %(refname)", ns],
                                  capture_output=True, text=True).stdout.replace(ns, "")
            out.write(refs + "\\n")
        elif line.startswith("push "):
            batch = [line]
            for l in sys.stdin:
                if l.strip() == "":
                    break
                batch.append(l.strip())
            for b in batch:
                spec = b.split(" ", 1)[1]
                src, dst = spec.lstrip("+").split(":")
                to = "refs/namespaces/" + os.environ["FAKE_RAD_NID"] + "/" + \\
                    ("refs/heads/patches-pushed" if dst == "refs/patches" else dst)
                r = subprocess.run(["git", "push", "-q", "--force" if spec.startswith("+") else "--no-force",
                                    onward, f"{src}:{to}"], capture_output=True, text=True)
                out.write(f"ok {dst}\\n" if r.returncode == 0 else f"error {dst} {r.stderr.strip()}\\n")
            with open(onward + ".options", "a") as f:
                f.write("\\n".join(opts) + "\\n")
            out.write("\\n")
        elif line == "":
            break
        out.flush()
""")


class Echo(socketserver.BaseRequestHandler):
    """Stands in for sshd behind the broker's CONNECT."""

    def handle(self):
        for d in iter(lambda: self.request.recv(65536), b""):
            self.request.sendall(d)


def sshstr(b):
    return struct.pack(">I", len(b)) + b


class Agent:
    """A minimal ssh-agent protocol client."""

    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect(str(path))

    def call(self, msg):
        self.s.sendall(struct.pack(">I", len(msg)) + msg)
        n = struct.unpack(">I", self.s.recv(4))[0]
        out = b""
        while len(out) < n:
            out += self.s.recv(n - len(out))
        return out

    def identities(self):
        r = self.call(bytes([11]))
        n, i, blobs = struct.unpack(">I", r[1:5])[0], 5, []
        for _ in range(n):
            ln = struct.unpack(">I", r[i:i + 4])[0]
            blobs.append(r[i + 4:i + 4 + ln])
            i += 4 + ln
            i += 4 + struct.unpack(">I", r[i:i + 4])[0]
        return blobs

    def sign(self, blob, data):
        r = self.call(bytes([13]) + sshstr(blob) + sshstr(data) + struct.pack(">I", 0))
        return r[5:] if r[0] == 14 else None


def blob_of(pubfile):
    return base64.b64decode(Path(pubfile).read_text().split()[1])


TAGGER_EXT = textwrap.dedent('''
    import json
    from core import Request, check, provider, read_body, rejected_msg, scope_kind

    scope_kind("tag", provider="tagger", help="tag:NAME (a test kind)")

    def handle(h, sess, method):
        req = Request("tagger", "tag")
        req.actions, req.targets = ["tagger:tag"], [("tag", json.loads(read_body(h))["name"])]
        ok, ev = check(sess, req)
        return h.send_json({"ok": True} if ok else {"error": rejected_msg(req, ev)}, 200 if ok else 403)

    class Tagger:
        name = "tagger"
        routes = [("POST", "/tag", handle)]
        needs_target = False  # on without a tag: entry

        def configure(self, sess, cfg, reg, out):
            return True

    provider(Tagger())
''')


def md_code_blocks(md, marker):
    """{file name: code} of a Markdown file's code blocks whose first lines name a file under marker."""
    out = {}
    for block in re.findall(r"```\w*\n(.*?)```", md.read_text(), re.S):
        for line in block.splitlines()[:2]:
            if line.startswith("# " + marker):
                out[line[len("# " + marker):].strip()] = block
    return out


def unix_post(sock, path, body):
    import http.client

    class C(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect(sock)
    c = C("localhost", timeout=30)
    c.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}")


class FakeAPI(http.server.BaseHTTPRequestHandler):
    """api.github.com for the gh-api extension: records each request, answers with what it got."""
    protocol_version = "HTTP/1.1"
    seen = []

    def log_message(self, *a):
        pass

    def answer(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        FakeAPI.seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                             "host": self.headers.get("Host"), "body": body})
        out = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = answer


def make_gh_api_certs(d):
    """The broker's CA and api.github.com certificate, as the gh-api extension would make them
    with openssl; made here with the cryptography module where there's no openssl. False when
    neither is there."""
    d.mkdir(parents=True, exist_ok=True)
    if shutil.which("openssl"):
        return None  # the extension makes them itself
    try:
        import datetime
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError:
        return False
    now_ = datetime.datetime.now(datetime.timezone.utc)
    cakey, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    caname = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "apod broker gh-api CA")])
    ca = (x509.CertificateBuilder().subject_name(caname).issuer_name(caname).public_key(cakey.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now_).not_valid_after(now_ + datetime.timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .add_extension(x509.NameConstraints([x509.DNSName("api.github.com")], None), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(cakey.public_key()), critical=False)
          .sign(cakey, hashes.SHA256()))
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "api.github.com")]))
            .issuer_name(caname).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now_).not_valid_after(now_ + datetime.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("api.github.com")]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(cakey.public_key()), critical=False)
            .sign(cakey, hashes.SHA256()))
    pem = lambda c: c.public_bytes(serialization.Encoding.PEM)
    keypem = lambda k: k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption())
    (d / "ca.pem").write_bytes(pem(ca)), (d / "ca.key").write_bytes(keypem(cakey))
    (d / "leaf.pem").write_bytes(pem(leaf)), (d / "leaf.key").write_bytes(keypem(key))
    return True


class BrokerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = Path(os.environ.get("APOD_TEST_TMP") or HERE / "tmp")  # short enough for unix socket paths
        base.mkdir(exist_ok=True)
        cls.tmp = Path(tempfile.mkdtemp(dir=base, prefix="broker-test-"))
        # apod's folders: the new layout, the sockets in a short runtime folder (not under tmp/, which
        # may be deep in the checkout).
        cls.rundir = Path(tempfile.mkdtemp(prefix="apod-run-"))
        cls.layout_env = {"APOD_CONFIG_DIR": str(cls.tmp / "config"), "APOD_DATA_DIR": str(cls.tmp / "data"),
                          "APOD_STATE_DIR": str(cls.tmp / "state"), "XDG_RUNTIME_DIR": str(cls.rundir)}
        cls.P = paths.Layout(dict(cls.layout_env, HOME=str(cls.tmp / "home")))
        bin_ = cls.tmp / "bin"
        bin_.mkdir()
        (bin_ / "gh").write_text(FAKE_GH.replace("@UPSTREAM@", str(cls.tmp / "upstream")))
        (bin_ / "gh").chmod(0o755)
        (bin_ / "git-remote-rad").write_text(FAKE_REMOTE_RAD)
        (bin_ / "git-remote-rad").chmod(0o755)
        for name in ("apod-git-forward", "apod-sign", "apod-ssh-connect"):
            os.symlink(SHIM, bin_ / name)
        # The user's extensions: one with a scope kind of its own, one that replaces the shipped
        # notify (same name), one that's broken, one that takes a Python module's name; and a
        # notify-send that records its args.
        ext = cls.P.extensions
        ext.mkdir(parents=True)
        (ext / "tagger.py").write_text(TAGGER_EXT)
        (ext / "notify.py").write_text((HERE / "extensions/notify.py").read_text()
                                       .replace('Path(__file__).resolve().parent / "apod-notify"', f'Path("{HERE}/extensions/apod-notify")')
                                       .replace("sends the user a desktop notification", "(the user's own) notifies the user"))
        # broken registers a provider and a scope kind, then fails: both are undone.
        (ext / "broken.py").write_text(textwrap.dedent("""\
            from api import Provider, provider, scope_kind
            scope_kind("broken")
            class B(Provider):
                name = "broken"
                routes = [("POST", "/broken", lambda h, sess, m: h.send_json({}))]
                def configure(self, sess, cfg, reg, out):
                    return True
            provider(B())
            raise RuntimeError('broken on purpose')
            """))
        # Refused at registration: a route that overlaps gh's; on, but needs a provider that isn't.
        (ext / "badroute.py").write_text("from api import Provider, provider\n"
                                         "class P(Provider):\n    name = 'badroute'\n    routes = [('POST', '/gh/x', print)]\n"
                                         "provider(P())\n")
        # EXTENSIONS.md's tutorial, as the guide has it, with a systemctl that records its args.
        for name, code in md_code_blocks(HERE / "EXTENSIONS.md", "~/.config/apod/extensions/").items():
            (ext / name).write_text(code)
            (ext / name).chmod(0o755)
        (bin_ / "systemctl").write_text(f"#!/bin/sh\nprintf '%s ' \"$@\" >> {cls.tmp}/systemctl\necho active\n")
        (bin_ / "systemctl").chmod(0o755)
        (ext / "needy.py").write_text("from api import Provider, provider\n"
                                      "class P(Provider):\n    name = 'needy'\n    requires = ['nothere']\n"
                                      "    def configure(self, sess, cfg, reg, out):\n        return True\n"
                                      "provider(P())\n")
        (ext / "json.py").write_text("raise RuntimeError('should never load')\n")
        (bin_ / "fake-notify").write_text(f"#!/bin/sh\nprintf '%s|' \"$@\" >> {cls.tmp}/notified\n")
        (bin_ / "fake-notify").chmod(0o755)
        # The host's clipboard, for the clipboard extension: wl-paste reads the test's files.
        (bin_ / "wl-paste").write_text(f"#!/bin/sh\ncase \"$1\" in\n--list-types) cat {cls.tmp}/clip-types ;;\n"
                                       f"--no-newline) [ \"$3\" = \"$(cat {cls.tmp}/clip-is)\" ] && cat {cls.tmp}/clip-data ;;\n"
                                       "*) exit 2 ;;\nesac\n")
        (bin_ / "wl-paste").chmod(0o755)
        cls.n = itertools.count()
        # The host's ssh setup: a config entry for "devbox" (an echo server stands in for sshd) and
        # its host key in known_hosts; a user key for logins and one for signing.
        home = cls.tmp / "home"
        (home / ".ssh").mkdir(parents=True)
        cls.echo = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
        threading.Thread(target=cls.echo.serve_forever, daemon=True).start()
        port = cls.echo.server_address[1]
        for k in ("hostkey", "hostkey2", "userkey", "signkey"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", k, "-f", cls.tmp / k], check=True)
        (home / ".ssh/config").write_text(f"Host devbox\n  HostName 127.0.0.1\n  Port {port}\n"
                                          f"Host *\n  UserKnownHostsFile {home}/.ssh/known_hosts\n")
        hk = (cls.tmp / "hostkey.pub").read_text().split()
        (home / ".ssh/known_hosts").write_text(f"[127.0.0.1]:{port} {hk[0]} {hk[1]}\n")
        # Radicle storage: canonical refs and our namespace, and where pushes go on to.
        store = cls.tmp / "radstore" / RID
        subprocess.run(["git", "init", "-q", "--bare", "-b", "master", store], check=True)
        rseed = cls.tmp / "rseed"
        subprocess.run(["git", "init", "-q", "-b", "master", rseed], check=True)
        subprocess.run(["git", "-C", rseed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                        "--allow-empty", "-m", "rad init"], check=True)
        subprocess.run(["git", "-C", rseed, "push", "-q", str(store), "master",
                        f"master:refs/namespaces/{NID}/refs/heads/master"], check=True)
        cls.onward = store
        # Upstream git server with a bare repo acme/widget holding main.
        up = cls.tmp / "upstream"
        (up / "acme").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", up / "acme/widget.git"], check=True)
        seed = cls.tmp / "seed"
        subprocess.run(["git", "init", "-q", "-b", "main", seed], check=True)
        subprocess.run(["git", "-C", seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                        "--allow-empty", "-m", "init"], check=True)
        subprocess.run(["git", "-C", seed, "push", "-q", str(up / "acme/widget.git"), "main"], check=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", up / "other/public.git"], check=True)
        CGIUpstream.root = up
        cls.upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), CGIUpstream)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.api = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
        threading.Thread(target=cls.api.serve_forever, daemon=True).start()
        cls.gh_api_certs = make_gh_api_certs(cls.P.broker / "gh-api")
        cls.env = host_env(**cls.layout_env, APOD_GH_BIN=str(bin_ / "gh"),
                       APOD_GH_API_UPSTREAM=f"http://127.0.0.1:{cls.api.server_address[1]}",
                       APOD_BROKER_ASK_TIMEOUT="4", HOME=str(home), WAYLAND_DISPLAY="wayland-test", APOD_SSH_CONFIG=str(home / ".ssh/config"),
                       APOD_GH_GIT_UPSTREAM=f"http://127.0.0.1:{cls.upstream.server_address[1]}",
                       FAKE_RAD_ONWARD=str(cls.tmp / "radstore"), FAKE_RAD_NID=NID,
                       PATH=f"{bin_}:{os.environ['PATH']}")
        for k in list(cls.env):
            if k in ("GH_REPO", "SSH_AUTH_SOCK") or k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG")):
                cls.env.pop(k)
        cls.broker = subprocess.Popen([sys.executable, BROKER, "serve"], env=cls.env,
                                      stderr=open(cls.tmp / "broker.log", "w"))
        for _ in range(100):
            if cls.P.admin_sock.exists():
                break
            time.sleep(0.05)
        cls.addClassCleanup(cls.broker.kill)
        cls.sess = cls.register("dev", ["gh:acme/widget"])
        # A clone of acme/widget, as in the pod.
        cls.work = cls.tmp / "work"
        subprocess.run(["git", "init", "-q", "-b", "feature", cls.work], check=True)
        subprocess.run(["git", "-C", cls.work, "remote", "add", "origin", "git@github.com:acme/widget.git"], check=True)
        cls.shim_bin = bin_

    @classmethod
    def tearDownClass(cls):
        cls.broker.terminate()
        cls.broker.wait()
        cls.upstream.shutdown()
        cls.api.shutdown()
        if not os.environ.get("KEEP"):
            shutil.rmtree(cls.tmp, ignore_errors=True)
            shutil.rmtree(cls.rundir, ignore_errors=True)

    @classmethod
    def register(cls, role, scope, user="agentbot", toml="", remotes=(), warnings=None, project=None, extra=(), new=False):
        """Register a pod. The profile is in the old files (broker.toml, env), or with new=True in
        one profile.toml; `toml` is its top-level keys and tables."""
        pdir = cls.tmp / "profiles" / f"p{next(cls.n)}"
        pdir.mkdir(parents=True)
        if new:
            (pdir / "profile.toml").write_text(
                toml + '\n[git]\nname = "Agent"\nemail = "agent@example.com"\n' + (f'\n[gh]\nuser = "{user}"\n' if user else ""))
        else:
            (pdir / "env").write_text("GIT_AUTHOR_NAME=Agent\nGIT_AUTHOR_EMAIL=agent@example.com\n"
                                      "GIT_COMMITTER_NAME=Agent\nGIT_COMMITTER_EMAIL=agent@example.com\n")
            (pdir / "broker.toml").write_text(toml + (f'\n[gh]\nuser = "{user}"\n' if user else ""))
        proj = project or cls.tmp / "proj"  # by default a git repository, as a launch folder usually is
        if not project:
            (proj / ".git").mkdir(parents=True, exist_ok=True)
        args = ["register", "--profile", "test", "--profile-dir", str(pdir), "--project", str(proj)] + (["--role", role] if role else [])
        for s in scope:
            args += ["--scope", s]
        for u in remotes:
            args += ["--remote", u]
        args += list(extra)
        r = subprocess.run([sys.executable, BROKER, *args], env=cls.env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        if warnings is not None:
            warnings.append(r.stderr)
        sess = json.loads(r.stdout)
        sess["_pdir"] = pdir
        for k in sess["need_keys"]:  # what apod does with ssh-add
            subprocess.run(["ssh-add", "-q", k["path"]], env=dict(cls.env, SSH_AUTH_SOCK=sess["agent"]), check=True)
        return sess

    def write_profile(self, name, text):
        """A profile of apod's own (one `apod profiles` lists), written as a profile.toml."""
        p = self.P.profile(name)
        f = Path(p["file"])
        if not f.exists():  # a folder in the legacy layout, a file in the new one
            self.addCleanup(shutil.rmtree, p["dir"]) if p["dir"] else self.addCleanup(f.unlink)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)

    def pod_env(self, sess, forward=None):
        """The pod's environment for a session: its env and git config, paths mapped to the test's."""
        e = dict(self.env, APOD_BROKER_SOCK=str(Path(sess["dir"]) / "broker.sock"), GIT_TERMINAL_PROMPT="0",
                 GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        e.update({k: v.replace(sess["pod_dir"], sess["dir"]) for k, v in sess["env"].items()})
        cfg = [(k.replace("{forward}", forward or "x"), v.replace("/usr/local/bin/", str(self.shim_bin) + "/"))
               for k, v in sess["git_config"]] + [("user.name", "Agent"), ("user.email", "agent@example.com")]
        e["GIT_CONFIG_COUNT"] = str(len(cfg))
        for i, (k, v) in enumerate(cfg):
            e[f"GIT_CONFIG_KEY_{i}"], e[f"GIT_CONFIG_VALUE_{i}"] = k, v
        return e

    def gh(self, *args, sess=None, cwd=None, input=None, expect=0, env=None):
        e = dict(self.env, APOD_BROKER_SOCK=str(Path((sess or self.sess)["dir"]) / "broker.sock"), **(env or {}))
        r = subprocess.run([sys.executable, SHIM, *args], cwd=cwd or self.work, env=e, input=input,
                           capture_output=True, text=True)
        if expect is not None:
            self.assertEqual(r.returncode, expect, r.stderr)
        return r

    def ran(self, *args, **kw):
        return json.loads(self.gh(*args, **kw).stdout)

    def admin(self, *args):
        return subprocess.run([sys.executable, BROKER, *args], env=self.env, capture_output=True, text=True)

    def test_allowed_command_runs_with_token_and_repo(self):
        out = self.ran("pr", "list", "--state", "open")
        self.assertEqual(out["argv"], ["pr", "list", "--state=open"])
        self.assertEqual(out["GH_REPO"], "acme/widget")
        self.assertEqual(out["GH_TOKEN"], "tok-agentbot")
        self.assertFalse(out["cwd_has_git"])

    def test_alias_and_current_branch_selector(self):
        self.assertEqual(self.ran("pr", "ls")["argv"][:2], ["pr", "list"])
        self.assertEqual(self.ran("pr", "view")["argv"], ["pr", "view", "feature"])
        self.assertEqual(self.ran("pr", "comment", "-b", "hi")["argv"], ["pr", "comment", "--body=hi", "feature"])

    def test_pr_create_head_and_body_file(self):
        (self.work / "body.md").write_text("the body")
        out = self.ran("pr", "create", "--title", "T", "--body-file", "body.md")
        self.assertIn("--head=feature", out["argv"])
        self.assertEqual(out["files"], {"body.md": "the body"})
        out = self.ran("issue", "comment", "3", "-F", "-", input="from stdin")
        self.assertEqual(out["stdin"], "from stdin")
        r = self.gh("pr", "create", "--fill", expect=1)
        self.assertIn("--fill", r.stderr)

    def test_host_file_paths_are_not_read(self):
        r = self.gh("pr", "create", "--title", "T", "--body-file", "/etc/hostname-nope", expect=1)
        self.assertIn("file not found in the pod", r.stderr)

    def test_host_paths_refused(self):
        r = self.gh("api", "repos/acme/widget/issues", "-F", "body=@/nonexistent/key", expect=1)
        self.assertIn("file not found in the pod: /nonexistent/key", r.stderr)
        r = self.gh("issue", "create", "--title", "x", "--body", "/etc/passwd", expect=1)
        self.assertIn("path on the host", r.stderr)
        r = self.gh("pr", "view", "../../x", expect=1)
        self.assertIn("path on the host", r.stderr)

    def test_out_of_scope_repo_times_out(self):
        r = self.gh("pr", "list", "-R", "evil/repo", expect=1)
        self.assertIn("needs your approval", r.stderr)
        self.assertIn("timed out", r.stderr)
        # -h as a flag's value isn't a help request: gh would run the command.
        r = self.gh("issue", "create", "-R", "evil/repo", "--title", "-h", "--body", "x", expect=1)
        self.assertIn("needs your approval", r.stderr)

    def test_search_across_github(self):
        # A search without --repo or --owner is across GitHub: in scope only with gh-search:.
        sess = self.register("reader", ["gh:acme/widget", "gh-search:prs"])
        self.assertIn("searches across GitHub for prs", sess["context"])
        out = self.ran("search", "prs", "--author", "someone", sess=sess)
        self.assertEqual(out["argv"][:2], ["search", "prs"])
        r = self.gh("search", "issues", "--involves", "someone", sess=sess, expect=1)
        self.assertIn("gh-search:issues (gh search issues across GitHub)", r.stderr)
        sess = self.register("reader", ["gh-search:all"])  # all: every kind of search
        self.assertEqual(self.ran("search", "code", "x", sess=sess)["argv"][:2], ["search", "code"])
        self.assertEqual(self.ran("search", "issues", "x", sess=sess)["argv"][:2], ["search", "issues"])

    def test_agent_sees_permissions_and_session_name(self):
        sess = self.register("pm", ["gh:acme/widget", "gh-project:acme/11"])
        self.assertIn("project item-edit", sess["context"])
        self.assertIn("gh-project:acme/11", sess["context"])
        self.assertIn("gh auth status", sess["context"])
        r = self.gh("auth", "status", sess=sess)
        self.assertIn("role pm", r.stdout)
        self.assertIn("  gh: ", r.stdout)
        # Claude Code's /rename lands in the transcript, in the profile's home; the TUI shows it.
        d = sess["_pdir"] / "home/.claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(self.tmp / "proj"))
        d.mkdir(parents=True)
        (d / "s1.jsonl").write_text('{"type":"user"}\n{"type":"custom-title","customTitle":"[eco] boards","sessionId":"s1"}\n')
        names = {s["id"]: s.get("name") for s in self.state()["sessions"]}
        self.assertEqual(names[sess["id"]], "[eco] boards")
        with (d / "s1.jsonl").open("a") as f:
            f.write('{"type":"custom-title","customTitle":"renamed","sessionId":"s1"}\n')
        self.assertEqual({s["id"]: s.get("name") for s in self.state()["sessions"]}[sess["id"]], "renamed")

    def state(self):
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(self.P.admin_sock))
            s.sendall(b"POST /state HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
            data = b"".join(iter(lambda: s.recv(65536), b""))
        return json.loads(data.split(b"\r\n\r\n", 1)[1])

    def test_extension(self):
        self.P.roles.write_text('[roles.dev-notify]\nextends = ["dev"]\nnotify = ["send"]\n')
        self.addCleanup(self.P.roles.unlink)
        sess = self.register("dev-notify", ["gh:acme/widget"], toml=f'[notify]\nenabled = true\n'
                             f'command = ["{self.tmp}/bin/fake-notify"]\n')
        self.assertIn("notify", sess["providers"])
        self.assertIn(["%s" % (HERE / "extensions/apod-notify"), "/usr/local/bin/apod-notify", "ro"], sess["mounts"])
        self.assertIn("  notify: send", sess["context"])
        self.assertIn("(the user's own) notifies the user", sess["context"])  # the user's notify.py won
        e = dict(self.env, APOD_BROKER_SOCK=str(Path(sess["dir"]) / "broker.sock"))
        client = [sys.executable, HERE / "extensions/apod-notify"]
        r = subprocess.run([*client, "build", "done"], env=e, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.tmp / "notified").read_text(), "apod test|build done|")
        # notify:urgent isn't in the role: it waits, then times out.
        r = subprocess.run([*client, "--urgent", "help"], env=e, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not allowed for role dev-notify: notify:urgent", r.stderr)
        # The broken extension was skipped, with a warning in the broker's log; so was json.py.
        log = (self.tmp / "broker.log").read_text()
        self.assertIn("broken.py not loaded", log)
        self.assertIn("json.py not loaded: json is the name of a Python or broker module", log)

    def test_extension_scope_kind(self):
        """A user extension declares its own scope kind (tag:); the broker checks its targets
        against the session's tag: entries, and an unknown kind is ignored with a warning."""
        self.P.roles.write_text('[roles.tagger]\nextends = ["reader"]\ntagger = ["tag"]\n')
        self.addCleanup(self.P.roles.unlink)
        w = []
        sess = self.register("tagger", ["tag:alpha", "nope:x"], warnings=w)
        self.assertIn("tag:alpha", sess["scope"])
        self.assertIn("no extension provides nope:", w[0])
        self.assertIn("tag:", self.admin("scopes").stdout)
        sock = str(Path(sess["dir"]) / "broker.sock")
        self.assertEqual(unix_post(sock, "/tag", {"name": "alpha"})[0], 200)
        status, body = unix_post(sock, "/tag", {"name": "beta"})  # out of scope: waits, then times out
        self.assertEqual(status, 403)
        self.assertIn("out of this session's scope: tag:beta", body["error"])

    def test_extensions_md_tutorial(self):
        """The systemctl extension from EXTENSIONS.md works as the guide says."""
        self.P.roles.write_text('[roles.unitdev]\nextends = ["reader"]\nsystemctl = ["status"]\n')
        self.addCleanup(self.P.roles.unlink)
        sess = self.register("unitdev", [], toml='[systemctl]\nenabled = true\nunits = ["web"]\n')
        self.assertIn("systemctl", sess["providers"])
        self.assertIn("unit:web.service", sess["scope"])
        self.assertIn("systemctl reaches the host's user services", sess["context"])
        e = dict(self.env, APOD_BROKER_SOCK=str(Path(sess["dir"]) / "broker.sock"))
        client = [sys.executable, "-I", self.P.extensions / "systemctl"]  # -I: not its folder's json.py
        r = subprocess.run([*client, "--user", "status", "web"], env=e, capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, "active\n"), r.stderr)
        self.assertEqual((self.tmp / "systemctl").read_text(), "--user status --no-pager -- web.service ")
        r = subprocess.run([*client, "restart", "web"], env=e, capture_output=True, text=True)
        self.assertIn("not allowed for role unitdev: systemctl:restart", r.stderr)
        r = subprocess.run([*client, "status", "db"], env=e, capture_output=True, text=True)
        self.assertIn("out of this session's scope: unit:db.service", r.stderr)
        r = subprocess.run([*client, "status", "--now", "web"], env=e, capture_output=True, text=True)
        self.assertIn("--now isn't available", r.stderr)
        r = subprocess.run([*client, "--user", "daemon-reload"], env=e, capture_output=True, text=True)
        self.assertIn("only status, start, stop, restart", r.stderr)
        r = subprocess.run([*client, "status", "web", "db"], env=e, capture_output=True, text=True)
        self.assertIn("unit:db.service", r.stderr)
        self.assertEqual((self.tmp / "systemctl").read_text(), "--user status --no-pager -- web.service ")

    def test_extensions_command(self):
        """apod broker extensions: the list, and one extension's guide, roles and scope kinds."""
        out = self.admin("extensions").stdout
        self.assertIn("rad          git fetch and push to rad:// remotes", out)
        self.assertRegex(out, r"systemctl +systemctl --user on the host, for the units in scope  \[.*systemctl.py\]")
        out = self.admin("extensions", "rad").stdout
        self.assertIn("## Radicle\n", out)                       # its section of docs/guides.md
        self.assertNotIn("## Signing commits", out)              # and only that
        self.assertIn("dev (reader, and): push branch, push patch, push force", out)
        self.assertIn("rad:         rad:RID", out)
        out = self.admin("extensions", "systemctl").stdout     # yours: the module's docstring
        self.assertIn("systemctl: pods run the host's systemctl --user", out)
        self.assertIn("unit:", out)
        self.assertIn("Never allowed: auth *", self.admin("extensions", "gh").stdout)
        r = self.admin("extensions", "nope")
        self.assertIn("no extension 'nope'", r.stderr)

    def test_extension_api_checks(self):
        """What a broken or misdeclared extension registered doesn't stay behind."""
        self.assertNotIn("broken:", self.admin("scopes").stdout)
        for p in ("broken", "badroute", "needy"):
            self.assertNotIn(p, self.sess["providers"])
        self.assertIn("tagger", self.sess["providers"])  # from core import ... still works
        sock = str(Path(self.sess["dir"]) / "broker.sock")
        self.assertEqual(unix_post(sock, "/broken", {})[0], 404)
        log = (self.tmp / "broker.log").read_text()
        self.assertIn("broken on purpose", log)
        self.assertIn("overlaps gh's '/gh'", log)

    def test_keys_lists_and_loads_profile_keys(self):
        key = self.tmp / "profilekey"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "pk", "-f", key], check=True)
        self.write_profile("keyprof", f'scope = ["ssh:devbox"]\n[ssh]\nkey = "{key}"\n')
        r = self.admin("keys")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, rf"to load +{key} +keyprof \[ssh\]")
        self.assertRegex(r.stderr, r"loaded (\d) of \1 key\(s\)")  # with running pods' keys not asked for
        r = self.admin("keys")
        self.assertRegex(r.stdout, rf"loaded +{key} +keyprof \[ssh\]")
        self.assertIn("every key is already loaded", r.stderr)
        # An extension turned off for the profile: its key isn't listed.
        self.write_profile("keyprof", f'[ssh]\nkey = "{key}"\nenabled = false\n')
        self.assertNotIn("keyprof [ssh]", self.admin("keys").stdout)

    def test_terminal_notifications_drop_control_characters(self):
        sys.path.insert(0, str(HERE / "broker"))
        os.environ.pop("APOD_ROOT", None)
        for k, v in self.layout_env.items():
            os.environ.setdefault(k, v)
        import tui
        seq = tui.notify_seq("osc777", "a;b", "evil\x1b]0;pwned\x07 x")
        self.assertEqual(seq, "\x1b]777;notify;a,b;evil ]0,pwned  x\x1b\\")
        self.assertEqual(tui.notify_seq("bell", "t", "b"), "")

    def test_links_in_text_are_not_targets(self):
        body = "see https://github.com/other/research and https://github.com/orgs/other/projects/3"
        out = self.ran("issue", "create", "--title", "https://github.com/other/x", "--body", body)
        self.assertEqual(out["argv"][:2], ["issue", "create"])
        r = self.gh("pr", "view", "https://github.com/evil/repo/pull/1", expect=1)  # a positional URL is a target
        self.assertIn("evil/repo", r.stderr)

    def test_hard_deny(self):
        r = self.gh("api", "user", expect=1)  # not in role: asks
        self.assertIn("api GET /user", r.stderr)
        r = self.gh("secret", "list", expect=1)
        self.assertIn("not allowed for role dev: gh:secret list", r.stderr)
        r = self.gh("extension", "list", expect=1)
        self.assertIn("never allowed", r.stderr)

    def test_approve_once_and_for_session(self):
        p = subprocess.Popen([sys.executable, SHIM, "pr", "merge", "7", "--squash"], cwd=self.work,
                             env=dict(self.env, APOD_BROKER_SOCK=str(Path(self.sess["dir"]) / "broker.sock")),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        eid = None
        for _ in range(40):
            time.sleep(0.1)
            out = self.admin("pending").stdout
            if "pr merge" in out:
                eid = out.split()[0]
                break
        self.assertIsNotNone(eid, "request didn't show as pending")
        self.assertEqual(self.admin("allow", eid).returncode, 0)
        o, e = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 0, e)
        self.assertEqual(json.loads(o)["argv"], ["pr", "merge", "--squash", "7"])
        # Not granted beyond once: rejected again; then granted for the session, it passes.
        r = self.gh("pr", "merge", "8", expect=1)
        eid = r.stderr.split("apod broker allow ")[1].split()[0]
        self.assertEqual(self.admin("allow", eid, "--session").returncode, 0)
        self.gh("pr", "merge", "8")

    def test_herdr_pane_blocked_while_waiting(self):
        """A pod launched from a herdr pane: the pane is reported blocked while a request waits for
        approval, and released when it's decided or times out (or the herdr socket is gone)."""
        calls = []
        path = str(self.tmp / "herdr.sock")
        srv = socketserver.ThreadingUnixStreamServer(path, type("H", (socketserver.StreamRequestHandler,), {
            "handle": lambda h: (calls.append(json.loads(h.rfile.readline())),
                                 h.wfile.write(json.dumps({"id": calls[-1]["id"], "result": {}}).encode() + b"\n"))}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        sess = self.register("dev", ["gh:acme/widget"], extra=["--herdr-pane", "w1:p2", "--herdr-socket", path,
                                                              "--herdr-agent", "claude"])
        env = dict(self.env, APOD_BROKER_SOCK=str(Path(sess["dir"]) / "broker.sock"))

        def merge(n):
            return subprocess.Popen([sys.executable, SHIM, "pr", "merge", n], cwd=self.work, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def methods(n=0):
            """The methods called so far; with n, once there are that many (calls go out in the background)."""
            for _ in range(50 if n else 1):
                if len(calls) >= n:
                    break
                time.sleep(0.1)
            return [c["method"] for c in calls]

        def waiting():
            for _ in range(40):
                time.sleep(0.1)
                if "pane.report_agent" in methods():
                    return
            self.fail("the pane wasn't reported")

        # Allowed once: blocked, then released.
        p = merge("21")
        waiting()
        c = calls[0]
        self.assertEqual((c["method"], c["params"]["pane_id"], c["params"]["state"], c["params"]["agent"]),
                         ("pane.report_agent", "w1:p2", "blocked", "claude"))
        self.assertTrue(c["params"]["source"].startswith("apod:"))
        self.assertIn("pr merge", c["params"]["message"])
        self.assertEqual(methods(), ["pane.report_agent"])  # still waiting: not released
        eid = next(l.split()[0] for l in self.admin("pending").stdout.splitlines() if "pr merge 21" in l)
        self.admin("deny", eid)
        p.communicate(timeout=10)
        self.assertEqual(methods(2), ["pane.report_agent", "pane.release_agent"])
        self.assertEqual(calls[1]["params"]["pane_id"], "w1:p2")
        # Nobody answers: released when it times out (APOD_BROKER_ASK_TIMEOUT is 4).
        del calls[:]
        p = merge("22")
        p.communicate(timeout=15)
        self.assertEqual(methods(2), ["pane.report_agent", "pane.release_agent"])
        # herdr gone: the request still waits and is decided as usual.
        srv.shutdown()
        os.unlink(path)
        del calls[:]
        out, err = merge("23").communicate(timeout=15)
        self.assertEqual(calls, [])
        self.assertIn("timed out", err)

    def test_api_rest(self):
        self.ran("api", "repos/{owner}/{repo}/pulls")
        self.ran("api", "/repos/acme/widget/issues/1/comments", "-f", "body=x")
        r = self.gh("api", "-X", "DELETE", "repos/acme/widget/git/refs/heads/x", expect=1)
        self.assertIn("api DELETE /repos/{repo}/git/refs/heads/x", r.stderr)
        r = self.gh("api", "repos/evil/repo/pulls", expect=1)
        self.assertIn("out of this session's scope: gh:evil/repo", r.stderr)
        r = self.gh("api", "--hostname", "ghe.example.com", "user", expect=1)
        self.assertIn("only github.com", r.stderr)

    def test_graphql(self):
        q = 'query{ repository(owner:"acme", name:"widget"){ issues(first:1){nodes{title}} } }'
        self.ran("api", "graphql", "-f", f"query={q}")
        q = 'query($o:String!,$n:String!){ repository(owner:$o, name:$n){ id } }'
        self.ran("api", "graphql", "-f", f"query={q}", "-f", "o=acme", "-f", "n=widget")
        r = self.gh("api", "graphql", "-f", f"query={q}", "-f", "o=evil", "-f", "n=x", expect=1)
        self.assertIn("evil/x", r.stderr)
        r = self.gh("api", "graphql", "-f", "query={ viewer { login } }", expect=1)
        self.assertIn("graphql query viewer", r.stderr)
        m = 'mutation($id:ID!){ addComment(input:{subjectId:$id, body:"x"}){ clientMutationId } }'
        self.ran("api", "graphql", "-f", f"query={m}", "-f", "id=I_kwDOAAA333")
        r = self.gh("api", "graphql", "-f", "query=mutation{ deleteRepository(input:{}){ clientMutationId } }", expect=1)
        self.assertIn("graphql mutation deleteRepository", r.stderr)
        (self.work / "q.graphql").write_text('{ repository(owner:"acme", name:"widget"){ id } }')
        self.ran("api", "graphql", "-F", "query=@q.graphql")
        # GraphQL only through "api graphql", where the query is checked.
        for ep in ("/graphql", "https://api.github.com/graphql"):
            r = self.gh("api", ep, "-f", "query={ viewer { login } }", expect=1)
            self.assertIn("use gh api graphql", r.stderr)
        r = self.gh("api", "repos/acme/widget%2F..%2F..%2Fevil%2Fx", expect=1)
        self.assertIn("encoded", r.stderr)

    def test_projects_pm(self):
        w = []
        pm = self.register("pm", ["acme/widget", "project:acme/5"], warnings=w)  # the old forms still work
        self.assertIn("acme/widget → gh:acme/widget, project:acme/5 → gh-project:acme/5", w[0])
        self.assertEqual(pm["scope"][:2], ["gh:acme/widget", "gh-project:acme/5"])
        self.ran("project", "item-list", "5", "--owner", "acme", sess=pm)
        r = self.gh("project", "item-edit", "--project-id", "PVT_kwDOBBB222", "--id", "PVTI_lADOAAA444", sess=pm, expect=1)
        self.assertIn("gh-project:evil/9", r.stderr)
        self.ran("project", "item-edit", "--project-id", "PVT_kwDOAAA111", "--id", "PVTI_lADOAAA444", "--text", "t", sess=pm)
        r = self.gh("project", "item-edit", "--project-id", "PVT_kwDOAAA111", "--id", "junk", sess=pm, expect=1)
        self.assertIn("unscoped: node junk", r.stderr)
        r = self.gh("project", "item-list", "6", "--owner", "acme", sess=pm, expect=1)
        self.assertIn("gh-project:acme/6", r.stderr)
        r = self.gh("pr", "create", "--title", "x", "--body", "y", sess=pm, expect=1)
        self.assertIn("not allowed for role pm", r.stderr)

    def test_fork_adds_scope(self):
        m = self.register("maintainer", ["gh:acme/widget"], user="octo")
        out = self.ran("repo", "fork", sess=m)
        self.assertEqual(out["argv"], ["repo", "fork", "--clone=false", "--remote=false", "acme/widget"])
        self.assertEqual(out["GH_TOKEN"], "tok-octo")
        self.ran("pr", "list", "-R", "octo/widget", sess=m)

    def test_auth_local(self):
        r = self.gh("auth", "status")
        self.assertIn("role dev", r.stdout)
        self.gh("auth", "token", expect=1)
        self.gh("auth", "setup-git")

    def git_env(self, sess):
        # What apod's wrapper does in the pod: start the forwarder, put its URL in the git config.
        e = self.pod_env(sess)
        url = subprocess.run([str(self.shim_bin / "apod-git-forward")], env=e, capture_output=True,
                             text=True, check=True).stdout.strip()
        return self.pod_env(sess, url)

    def test_git_clone_push_branch_and_default(self):
        e = self.git_env(self.sess)
        d = self.tmp / "clone"
        r = subprocess.run(["git", "clone", "-q", "git@github.com:acme/widget.git", d], env=e, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any(a == "Basic eC1hY2Nlc3MtdG9rZW46dG9rLWFnZW50Ym90" for _, _, a in CGIUpstream.seen))
        subprocess.run(["git", "-C", d, "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"], env=e, check=True)
        r = subprocess.run(["git", "-C", d, "push", "origin", "HEAD:refs/heads/topic"], env=e, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(["git", "-C", d, "push", "origin", "HEAD:main"], env=e, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("needs your approval", r.stderr)
        self.assertIn("github:push default-branch", r.stderr)
        r = subprocess.run(["git", "-C", d, "push", "origin", ":topic"], env=e, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("github:push delete", r.stderr)
        # A fast-forward is a branch push; a rewrite is a force push, which dev may do.
        subprocess.run(["git", "-C", d, "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "y"], env=e, check=True)
        r = subprocess.run(["git", "-C", d, "push", "origin", "HEAD:refs/heads/topic"], env=e, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        subprocess.run(["git", "-C", d, "-c", "user.email=t@t", "commit", "-q", "--amend", "--allow-empty", "-m", "y2"],
                       env=e, check=True)
        r = subprocess.run(["git", "-C", d, "push", "--force-with-lease", "origin", "HEAD:refs/heads/topic"], env=e,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        log = self.admin("log", "-n", "2").stdout
        self.assertIn("allowed              git push github.com/acme/widget refs/heads/topic", log)
        self.assertIn("allowed              git push --force github.com/acme/widget refs/heads/topic", log)
        # Out of scope: a public repo can be fetched, not pushed.
        r = subprocess.run(["git", "ls-remote", "https://github.com/other/public"], env=e, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run(["git", "-C", d, "push", "https://github.com/other/public", "HEAD:refs/heads/x"], env=e,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("out of this session's scope", r.stderr)

    def test_git_push_approved_while_waiting(self):
        e = self.git_env(self.sess)
        d = self.tmp / "clone2"
        subprocess.run(["git", "clone", "-q", "https://github.com/acme/widget", d], env=e, check=True)
        subprocess.run(["git", "-C", d, "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "y"], env=e, check=True)
        p = subprocess.Popen(["git", "-C", d, "push", "origin", "HEAD:main"], env=e, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        eid = None
        for _ in range(40):
            time.sleep(0.1)
            out = self.admin("pending").stdout
            if "refs/heads/main" in out:
                eid = out.split()[0]
                break
        self.assertIsNotNone(eid)
        self.admin("allow", eid)
        o, err = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("waiting", err.lower())

    # --- sign ------------------------------------------------------------------------------

    def test_session_state_is_outside_the_pods_folder(self):
        # The pod's folder is mounted read-write: the broker must not keep its role and scope there.
        d = Path(self.sess["dir"])
        self.assertFalse((d / "session.json").exists())
        self.assertEqual(d, self.P.sessions_run / self.sess["id"])  # in the runtime folder
        state = json.loads((self.P.sessions_state / f"{self.sess['id']}.json").read_text())  # in the state folder
        self.assertEqual(state["role"], "dev")
        self.assertFalse(list(d.glob("*.json")))

    def test_saved_session_keeps_its_scope(self):
        # A restarted broker reloads each session from its state file: its scope must come back,
        # also from a session saved with the old kind names (repo, host).
        state = self.P.sessions_state / f"{self.sess['id']}.json"
        code = textwrap.dedent(f"""\
            import json, sys
            sys.path.insert(0, {str(HERE / "broker")!r})
            import core
            core.load_extensions()
            d = json.loads(open({str(state)!r}).read())
            s = core.Session(d)
            assert s.scope == d["scope"], (s.scope, d["scope"])
            assert s.in_scope(("gh", "acme/widget")), s.scope
            old = core.Session(dict(d, scope={{"repo": ["fryorcraken/x"], "host": ["devbox"]}}))
            assert old.scope == {{"gh": ["fryorcraken/x"], "ssh": ["devbox"]}}, old.scope
            """)
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env=dict(os.environ, **self.layout_env))
        self.assertEqual(p.returncode, 0, p.stderr)

    def gh_api_session(self):
        if self.gh_api_certs is False:
            self.skipTest("no openssl and no cryptography module to make the gh-api certificates")
        return self.register("dev", ["gh:acme/widget"], toml="[gh-api]\nenabled = true\n")

    def gh_api_tls(self, sess, sock=None):
        """A TLS connection to api.github.com as the pod has it: through the relay's socket (or
        straight to the broker's tunnel), checking the broker's certificate against its CA."""
        if sock is None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(Path(sess["dir"]) / "broker.sock"))
            sock.sendall(b"POST /gh-api/tunnel HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n")
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += sock.recv(4096)
            self.assertTrue(buf.startswith(b"HTTP/1.1 200"), buf)
        ctx = ssl.create_default_context(cafile=str(self.P.broker / "gh-api/ca.pem"))
        tls = ctx.wrap_socket(sock, server_hostname="api.github.com")
        return tls, tls.makefile("rb")

    @staticmethod
    def gh_api_call(tls, f, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        tls.sendall(f"{method} {path} HTTP/1.1\r\nHost: api.github.com\r\nAuthorization: Bearer apod-gh-api-placeholder-token"
                    f"\r\nAccept: application/vnd.github+json\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
        status = int(f.readline().split()[1])
        n = 0
        while (line := f.readline()) not in (b"\r\n", b""):
            if line.lower().startswith(b"content-length:"):
                n = int(line.split(b":")[1])
        return status, json.loads(f.read(n))

    def test_gh_api(self):
        sess = self.gh_api_session()
        self.assertIn("gh-api", sess["providers"])
        self.assertEqual(sess["hosts"], {"api.github.com": "127.0.0.1"})
        self.assertEqual(sess["sysctls"], {"net.ipv4.ip_unprivileged_port_start": "443"})
        self.assertEqual(sess["start"], [["apod-gh-api-relay"]])
        self.assertEqual(sess["env"]["NODE_EXTRA_CA_CERTS"], "/run/apod-gh-api/ca.pem")
        self.assertIn([str(self.P.broker / "gh-api/ca.pem"), "/run/apod-gh-api/ca.pem", "ro"], sess["mounts"])
        self.assertFalse(any("key" in m[0] for m in sess["mounts"]))  # the CA's key stays on the host
        # gh auth token: a placeholder with gh-api, nothing without it.
        e = self.pod_env(sess)
        r = subprocess.run([sys.executable, SHIM, "auth", "token"], env=e, capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "apod-gh-api-placeholder-token"))
        r = subprocess.run([sys.executable, SHIM, "auth", "token"], env=self.pod_env(self.sess), capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        FakeAPI.seen.clear()
        tls, f = self.gh_api_tls(sess)
        # REST in scope and role: sent on with the account's token, not the placeholder.
        st, out = self.gh_api_call(tls, f, "GET", "/repos/acme/widget/pulls?head=acme:feature&state=open&per_page=1")
        self.assertEqual((st, out["path"]), (200, "/repos/acme/widget/pulls?head=acme:feature&state=open&per_page=1"))
        self.assertEqual(FakeAPI.seen[-1]["auth"], "Bearer tok-agentbot")
        self.assertEqual(FakeAPI.seen[-1]["host"], "api.github.com")
        # GraphQL on the same connection, with an alias, as Claude Code's batch.
        q = 'query { rateLimit { remaining } a0: repository(owner:"acme",name:"widget"){ pullRequest(number:1){ reviewDecision } } }'
        st, out = self.gh_api_call(tls, f, "POST", "/graphql", {"query": q})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(FakeAPI.seen[-1]["body"])["query"], q)
        # Out of scope, or beyond the role: refused at once (no waiting for approval), never sent.
        n = len(FakeAPI.seen)
        t0 = time.time()
        st, out = self.gh_api_call(tls, f, "GET", "/repos/other/thing/pulls")
        self.assertEqual(st, 403)
        self.assertIn("out of this session's scope: gh:other/thing", out["message"])
        st, out = self.gh_api_call(tls, f, "DELETE", "/repos/acme/widget")
        self.assertEqual(st, 403)
        self.assertIn("not allowed for role dev", out["message"])
        st, out = self.gh_api_call(tls, f, "POST", "/graphql", {"query": 'mutation { deleteRepository(input:{}) { clientMutationId } }'})
        self.assertEqual(st, 403)
        self.assertLess(time.time() - t0, 3)  # APOD_BROKER_ASK_TIMEOUT is 4
        self.assertEqual(len(FakeAPI.seen), n)
        tls.close()
        # In the broker's log like any request, marked direct.
        log = [json.loads(x) for x in self.P.log.read_text().splitlines()]
        mine = [(e["request"], e["decision"]) for e in log if e["session"] == sess["id"]]
        self.assertIn(("GET https://api.github.com/repos/acme/widget/pulls?head=acme:feature&state=open&per_page=1 (direct)",
                       "allowed"), mine)
        self.assertIn(("GET https://api.github.com/repos/other/thing/pulls (direct)", "rejected"), mine)

    def test_gh_api_relay(self):
        # The pod's relay: a TCP port (443 in the pod) passed through to the broker's tunnel.
        sess = self.gh_api_session()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        e = dict(self.pod_env(sess), APOD_GH_API_PORT=str(port))
        r = subprocess.run([HERE / "extensions/apod-gh-api-relay"], env=e, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)

        def kill_relay():
            for d in Path("/proc").iterdir():
                try:
                    if d.name.isdigit() and f"APOD_GH_API_PORT={port}".encode() in (d / "environ").read_bytes():
                        os.kill(int(d.name), 9)
                except OSError:
                    pass
        self.addCleanup(kill_relay)
        tls, f = self.gh_api_tls(sess, socket.create_connection(("127.0.0.1", port)))
        st, out = self.gh_api_call(tls, f, "GET", "/repos/acme/widget")
        self.assertEqual((st, out["path"]), (200, "/repos/acme/widget"))
        tls.close()

    def test_gh_api_off_by_default_and_on_host_network(self):
        self.assertNotIn("gh-api", self.sess["providers"])
        self.assertEqual(self.sess["hosts"], {})
        if self.gh_api_certs is False:
            return
        warnings = []
        pdir_toml = "[gh-api]\nenabled = true\n"
        sess = self.register("dev", [], toml=pdir_toml, warnings=warnings)
        self.assertIn("gh-api", sess["providers"])
        # --network host: off, with a warning.
        pdir = self.tmp / "profiles" / "hostnet"
        pdir.mkdir(parents=True)
        (pdir / "broker.toml").write_text(pdir_toml + '[gh]\nuser = "agentbot"\n')
        r = subprocess.run([sys.executable, BROKER, "register", "--profile", "test", "--profile-dir", str(pdir),
                            "--project", str(self.tmp / "proj"), "--network", "host"], env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("gh-api", json.loads(r.stdout)["providers"])
        self.assertIn("shares the host's network", r.stderr)

    def test_clipboard(self):
        self.assertNotIn("clipboard", self.sess["providers"])  # off by default
        sess = self.register("dev", [], toml='allow = ["clipboard:image"]\n[clipboard]\nenabled = true\n')
        self.assertIn("clipboard", sess["providers"])
        self.assertIn([str(HERE / "extensions/apod-clipboard"), "/usr/local/bin/wl-paste", "ro"], sess["mounts"])
        e = dict(self.env, APOD_BROKER_SOCK=str(Path(sess["dir"]) / "broker.sock"))
        paste = lambda *a: subprocess.run([sys.executable, HERE / "extensions/apod-clipboard", *a], env=e,
                                          capture_output=True)
        img = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
        (self.tmp / "clip-types").write_text("text/plain\nUTF8_STRING\nimage/png\n")
        (self.tmp / "clip-is").write_text("image/png")
        (self.tmp / "clip-data").write_bytes(img)
        # What Claude Code runs: the image types (never the text ones), then the image.
        r = paste("-l")
        self.assertEqual((r.returncode, r.stdout), (0, b"image/png\n"), r.stderr)
        r = paste("--type", "image/png")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, img)
        # Text is refused in the pod, and by the broker.
        for args in ((), ("--type", "text/plain"), ("-p",), ("--watch", "cat")):
            r = paste(*args)
            self.assertEqual((r.returncode, r.stdout), (1, b""), args)
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(e["APOD_BROKER_SOCK"])
            body = b'{"type": "text/plain"}'
            s.sendall(b"POST /clipboard/read HTTP/1.0\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            self.assertIn(b" 400", b"".join(iter(lambda: s.recv(65536), b"")).split(b"\r\n")[0])
        # Nothing but text on the clipboard: no types, so Claude Code says there's no image.
        (self.tmp / "clip-types").write_text("text/plain\n")
        self.assertEqual(paste("-l").returncode, 1)
        r = paste("--type", "image/png")
        self.assertIn(b"no image/png on the host's clipboard", r.stderr)
        # Without the action in the role or the profile, a paste waits for approval (and times out).
        sess = self.register("dev", [], toml="[clipboard]\nenabled = true\n")
        e["APOD_BROKER_SOCK"] = str(Path(sess["dir"]) / "broker.sock")
        (self.tmp / "clip-types").write_text("image/png\n")
        r = paste("--type", "image/png")
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"not allowed for role dev: clipboard:image", r.stderr)

    def test_ssh_hosts_old_form(self):
        # [ssh] hosts, before hosts were scope entries: still in scope, with a warning.
        warnings = []
        sess = self.register("ops", [], toml=f'[ssh]\nkey = "{self.tmp}/userkey"\nhosts = ["devbox"]\n', warnings=warnings)
        self.assertIn("ssh", sess["providers"])
        self.assertIn("ssh:devbox", sess["scope"])
        self.assertIn('scope = ["ssh:devbox"]', warnings[0])

    def test_register_reads_profile_toml(self):
        # The same settings from profile.toml as from the old files: role, scope, allow, deny, the
        # extension tables, the git identity; and no warning about old files.
        w = []
        sess = self.register("", [], new=True, warnings=w, toml=(
            f'role = "dev"\nscope = ["ssh:devbox"]\nallow = ["ssh:connect", "ssh:login"]\ndeny = ["gh:pr create"]\n'
            f'[ssh]\nkey = "{self.tmp}/userkey"\n'))
        self.assertEqual(sess["role"], "dev")
        self.assertIn("ssh:login", sess["allowed"])
        self.assertNotIn("old profile files", w[0])
        self.assertIn("ssh", sess["providers"])
        state = json.loads((self.P.sessions_state / f"{sess['id']}.json").read_text())
        self.assertEqual(state["extra"], {"allow": ["ssh:connect", "ssh:login"], "deny": ["gh:pr create"]})
        self.assertEqual(state["identity"]["emails"], ["agent@example.com"])
        r = self.gh("pr", "create", "--title", "t", "--body", "b", sess=sess, cwd=self.work, expect=1)
        self.assertIn("never allowed", r.stderr)
        # The broker mounts nothing: a mount the guard would refuse (apod checks those) doesn't stop a session.
        self.register("", [], new=True, toml=f'[pod]\nro = ["{HERE}"]\n')
        # The launch's role still wins over profile.toml's.
        self.assertEqual(self.register("reader", [], new=True, toml='role = "dev"\n')["role"], "reader")
        # Old files warn once, with the way out.
        w = []
        self.register("", [], warnings=w)
        self.assertRegex(w[0], r"p\d+: old profile files \(broker.toml, env\): apod migrate p\d+")
        # A key after a [table] line is that table's: refused, not run with another role.
        pdir = self.tmp / "profiles" / "misplaced"
        pdir.mkdir(parents=True)
        (pdir / "profile.toml").write_text('[gh]\nuser = "x"\nrole = "dev"\n')
        r = subprocess.run([sys.executable, BROKER, "register", "--profile", "t", "--profile-dir", str(pdir),
                            "--project", str(self.tmp / "proj")], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[gh] has role = ...: move it above the first [table]", r.stderr)

    def test_profile_adds_to_role(self):
        # allow = [...] in a profile's broker.toml adds to its role, deny takes away; a scope entry
        # the role can't use at all gets a warning.
        w = []
        sess = self.register("dev", [], toml=f'scope = ["ssh:devbox"]\n[ssh]\nkey = "{self.tmp}/userkey"\n', warnings=w)
        self.assertIn("ssh:devbox in scope, but role dev allows nothing of ssh", w[0])
        w = []
        sess = self.register("dev", [], toml=f'scope = ["ssh:devbox"]\nallow = ["ssh:connect", "ssh:login"]\n'
                                             f'deny = ["gh:pr create"]\n[ssh]\nkey = "{self.tmp}/userkey"\n', warnings=w)
        self.assertNotIn("allows nothing of ssh", w[0])
        self.assertIn("ssh:login", sess["allowed"])
        self.assertIn("this profile allows ssh:connect, ssh:login", sess["text"])
        r = self.gh("pr", "create", "--title", "t", "--body", "b", sess=sess, cwd=self.work, expect=1)
        self.assertIn("never allowed", r.stderr)
        # Kept across a broker restart, like the role and scope.
        state = json.loads((self.P.sessions_state / f"{sess['id']}.json").read_text())
        self.assertEqual(state["extra"], {"allow": ["ssh:connect", "ssh:login"], "deny": ["gh:pr create"]})
        # Not a list of PROVIDER:ACTION: the launch fails, rather than run with a role it didn't mean.
        pdir = self.tmp / "profiles" / "badallow"
        pdir.mkdir(parents=True)
        (pdir / "broker.toml").write_text('allow = "ssh:login"\n')
        r = subprocess.run([sys.executable, BROKER, "register", "--profile", "t", "--profile-dir", str(pdir),
                            "--project", str(self.tmp / "proj")], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('allow is a list of "PROVIDER:ACTION" patterns', r.stderr)

    def test_sign_commits(self):
        # The profile holds the key, the role the actions, the scope the repositories.
        toml = f'[git-sign]\nkey = "{self.tmp}/signkey"\n'
        reader = self.register("reader", ["gh:acme/widget"], toml=toml)
        self.assertIn("git-sign", reader["providers"])  # git commit -S works, with approval
        # The old name of the table still works, with a warning.
        old = self.register("reader", ["gh:acme/widget"], toml=toml.replace("[git-sign]", "[sign]"))
        self.assertIn("git-sign", old["providers"])
        self.assertTrue(any("[sign] is now [git-sign]" in w for w in old["warnings"]))
        # A folder that isn't a git repository: nothing to sign, so off.
        (self.tmp / "notgit").mkdir(exist_ok=True)
        sess = self.register("dev", [], toml=toml, project=self.tmp / "notgit")
        self.assertNotIn("git-sign", sess["providers"])
        self.assertIn("git-sign (nothing in scope to act on", sess["context"])
        # A role that can't sign (reader: git commit -S waits for you): on, its key not asked for.
        self.assertFalse([k for k in reader["need_keys"] if k["path"].endswith("signkey")])
        self.assertNotIn(["commit.gpgsign", "true"], reader["git_config"])
        self.assertIn("Role reader doesn't sign", reader["context"])
        sess = self.register("dev", ["gh:acme/widget"], toml=toml)
        self.assertIn(["commit.gpgsign", "true"], sess["git_config"])
        self.assertIn(["gpg.ssh.allowedSignersFile", "/run/apod-broker/allowed_signers"], sess["git_config"])
        self.assertIn("namespaces=\"git\" ssh-", (Path(sess["dir"]) / "allowed_signers").read_text())
        e = self.pod_env(sess)

        def commit(repo, *extra, remote=None):
            if not (repo / ".git").exists():
                subprocess.run(["git", "init", "-q", repo], check=True, env=e)
                if remote:
                    subprocess.run(["git", "-C", repo, "remote", "add", "origin", remote], check=True, env=e)
            return subprocess.run(["git", "-C", repo, "commit", "-q", "--allow-empty", "-m", "c", *extra], env=e,
                                  capture_output=True, text=True)

        # In a repository in scope (by its push remote), as the profile's identity: signed.
        repo = self.tmp / "signed"
        r = commit(repo, remote="git@github.com:acme/widget.git")
        self.assertEqual(r.returncode, 0, r.stderr)
        obj = subprocess.run(["git", "-C", repo, "cat-file", "commit", "HEAD"], env=e, capture_output=True,
                             text=True).stdout
        self.assertIn("-----BEGIN SSH SIGNATURE-----", obj)
        allowed = self.tmp / "allowed_signers"
        allowed.write_text("agent@example.com " + (self.tmp / "signkey.pub").read_text())
        r = subprocess.run(["git", "-C", repo, "-c", f"gpg.ssh.allowedSignersFile={allowed}", "verify-commit", "HEAD"],
                           env=e, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Good", r.stderr)
        # As someone else: git-sign:as other isn't in the role; waits, fails.
        r = commit(repo, "--author", "Mallory <m@evil.example>")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not allowed for role dev: git-sign:as other", self.admin("log", "-n", "1").stdout)
        # In a repository out of scope: waits, fails.
        r = commit(self.tmp / "elsewhere", remote="https://github.com/evil/repo")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("out of this session's scope: gh:evil/repo", self.admin("log", "-n", "1").stdout)
        # A repository without a GitHub or Radicle remote: by its path; the project folder's are in scope.
        r = commit(self.tmp / "local")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"git-local:{self.tmp}/local", self.admin("log", "-n", "1").stdout)
        self.assertIn(f"git-local:{self.tmp}/proj/*", sess["scope"])

    # --- ssh -------------------------------------------------------------------------------

    def bind(self, agent, hostkey, sid):
        """session-bind@openssh.com as ssh sends it, signed by a host key held in a helper agent."""
        helper = self.tmp / "helper.sock"
        if not helper.exists():
            subprocess.Popen(["ssh-agent", "-D", "-a", str(helper)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                if helper.exists():
                    break
                time.sleep(0.05)
            for k in ("hostkey", "hostkey2"):
                subprocess.run(["ssh-add", "-q", self.tmp / k], env=dict(self.env, SSH_AUTH_SOCK=str(helper)), check=True)
        hk = blob_of(self.tmp / f"{hostkey}.pub")
        sig = Agent(helper).sign(hk, sid)
        msg = bytes([27]) + sshstr(b"session-bind@openssh.com") + sshstr(hk) + sshstr(sid) + sshstr(sig) + b"\0"
        return agent.call(msg)[0]

    def login_data(self, sid, user, key):
        return sshstr(sid) + bytes([50]) + sshstr(user.encode()) + sshstr(b"ssh-connection") + \
            sshstr(b"publickey") + b"\1" + sshstr(b"ssh-ed25519") + sshstr(key)

    def test_global_config_and_hosts_per_launch(self):
        self.P.config_file.write_text(f'role = "ops"\nscope = ["evil/*"]\n[ssh]\nkey = "{self.tmp}/userkey"\n')
        self.addCleanup(self.P.config_file.unlink)
        # The global key; hosts from the launch (--scope ssh:...). The global file's role and scope
        # are ignored, with a warning: the role is the default, reader.
        w = []
        sess = self.register("", ["ssh:devbox", "ssh:root@nas"], warnings=w)
        self.assertEqual(sess["role"], "reader")
        self.assertIn("role is ignored there", w[0])
        self.assertNotIn("evil/*", sess["scope"])
        self.assertIn("ssh", sess["providers"])
        self.assertEqual([s for s in sess["scope"] if s.startswith("ssh:")], ["ssh:devbox", "ssh:root@nas"])
        self.assertEqual(sess["env"]["SSH_AUTH_SOCK"], f"{sess['pod_dir']}/agent.sock")
        # A role that can't log in (dev: each login waits for you): on, its key not asked for.
        sess = self.register("dev", ["ssh:devbox"], toml='[ssh]\n')
        self.assertIn("ssh", sess["providers"])
        self.assertFalse([k for k in sess["need_keys"] if k["path"].endswith("userkey")])
        # A profile's table merges over the global one: its hosts, the global key.
        sess = self.register("ops", ["ssh:devbox"], toml='[ssh]\n')
        self.assertEqual(sess["role"], "ops")
        self.assertIn("ssh:devbox", sess["scope"])
        self.assertEqual(Agent(Path(sess["dir"]) / "agent.sock").identities(), [blob_of(self.tmp / "userkey.pub")])
        r = self.admin("keys")
        self.assertRegex(r.stdout, rf"{self.tmp}/userkey +all profiles \[ssh\]\n")
        # A profile with no hosts: no ssh, and its key isn't asked for at launch.
        sess = self.register("ops", [])
        self.assertNotIn("ssh", sess["providers"])
        self.assertFalse([k for k in sess["need_keys"] if k["path"].endswith("userkey")])
        self.assertNotIn("SSH_AUTH_SOCK", sess["env"])

    def test_github_needs_a_named_account(self):
        w = []
        sess = self.register("dev", [], user="", remotes=["git@github.com:acme/widget.git"], warnings=w)
        self.assertNotIn("gh", sess["providers"])
        self.assertNotIn("github", sess["providers"])
        # Called directly, the broker still doesn't act for it with the host's active login.
        r = self.gh("api", "rate_limit", sess=sess, expect=1)
        self.assertIn("gh isn't set up for this pod", r.stderr)
        self.assertIn('add [gh] user = "ACCOUNT"', w[0])
        self.assertIn("names no GitHub account", sess["context"])
        # --gh-user names it for one launch, over the profile's [gh] user.
        pdir = self.tmp / "profiles" / "ghuser"
        pdir.mkdir(parents=True)
        (pdir / "broker.toml").write_text('[gh]\nuser = "someoneelse"\n')
        r = subprocess.run([sys.executable, BROKER, "register", "--profile", "test", "--profile-dir", str(pdir),
                            "--project", "/p", "--gh-user", "octo"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["accounts"]["gh"], "octo")
        # No GitHub remote: no warning (a profile that doesn't use GitHub isn't nagged).
        w = []
        self.register("ops", [], user="", warnings=w)
        self.assertNotIn("[gh] user", w[0])

    def test_old_persona_names_still_work(self):
        w = []
        sess = self.register("", [], toml='persona = "maintainer"\n', warnings=w)
        self.assertEqual(sess["role"], "maintainer")
        self.assertIn("persona = is now role =", w[0])
        grants = self.P.grants
        old = grants.read_text() if grants.exists() else None
        self.addCleanup(lambda: grants.write_text(old) if old is not None else grants.unlink(missing_ok=True))
        grants.write_text(json.dumps({"personas": {"dev": ["gh:pr merge"]}}))
        self.assertIn("dev          gh:pr merge", self.admin("grants").stdout)

    def test_your_roles_add_to_built_in_ones(self):
        self.P.roles.write_text('[roles.dev]\nsign = ["other"]\n')
        self.addCleanup(self.P.roles.unlink)
        plain = {f[0]: f[2] for f in (l.split("\t") for l in self.admin("roles", "--plain").stdout.splitlines())}
        self.assertIn("git-sign:other", plain["dev"].split(","))  # sign = [...]: the old name still works
        self.assertIn("gh:pr create", plain["dev"].split(","))
        self.assertIn("gh:pr view", plain["dev"].split(","))  # inherited from reader
        # Readable: by provider and command; what dev adds on top of reader, not reader's again.
        out = self.admin("roles").stdout
        dev = out.split("\ndev\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("on top of reader:", dev)
        self.assertRegex(dev, r"git-sign +commit, tag, other")
        self.assertRegex(dev, r"pr: create, edit, comment, ready")
        self.assertNotRegex(dev, r"\bview\b")
        full = self.admin("roles", "dev").stdout
        self.assertRegex(full, r"pr: create, edit, comment, ready, list, view")

    def test_ssh_agent_signs_only_for_hosts_in_scope(self):
        sess = self.register("ops", ["ssh:devbox"], toml=f'[ssh]\nkey = "{self.tmp}/userkey"\n')
        sock = Path(sess["dir"]) / "agent.sock"
        user = blob_of(self.tmp / "userkey.pub")
        a = Agent(sock)
        self.assertEqual(a.identities(), [user])  # the broker's other keys (signing) aren't offered
        sid = os.urandom(32)
        self.assertEqual(self.bind(a, "hostkey", sid), 6)
        # ssh:devbox covers the user the host's ssh config gives devbox (no User: yours), no other.
        self.assertIsNotNone(a.sign(user, self.login_data(sid, getpass.getuser(), user)))
        self.assertIsNone(a.sign(user, self.login_data(sid, "root", user)))
        self.assertIn(f"ssh login as root to devbox", self.admin("log", "-n", "1").stdout)
        # A forged binding (signature over another session id) is refused by the real agent.
        b = Agent(sock)
        hk = blob_of(self.tmp / "hostkey.pub")
        self.assertEqual(b.call(bytes([27]) + sshstr(b"session-bind@openssh.com") + sshstr(hk) + sshstr(os.urandom(32))
                                + sshstr(b"\0" * 20) + b"\0")[0], 5)
        # No binding: refused (after asking).
        self.assertIsNone(b.sign(user, self.login_data(os.urandom(32), "me", user)))
        # Another host: waits for approval; approved for the session, it then passes.
        c = Agent(sock)
        sid2 = os.urandom(32)
        self.assertEqual(self.bind(c, "hostkey2", sid2), 6)
        self.assertIsNone(c.sign(user, self.login_data(sid2, "root", user)))
        log = self.admin("log", "-n", "1").stdout
        self.assertIn("ssh login as root to key:SHA256:", log)
        eid = log.split()[0]
        self.admin("allow", eid, "--session")
        self.assertIsNotNone(c.sign(user, self.login_data(sid2, "root", user)))
        # Not a login: never signed through the pod's agent.
        self.assertIsNone(a.sign(user, b"some other data"))

    def test_ssh_connect_through_broker(self):
        sess = self.register("ops", ["ssh:devbox"], toml=f'[ssh]\nkey = "{self.tmp}/userkey"\n')
        e = self.pod_env(sess)
        r = subprocess.run([str(self.shim_bin / "apod-ssh-connect"), "devbox", "22"], input=b"hello", env=e,
                           capture_output=True, timeout=20)
        self.assertEqual(r.stdout, b"hello", r.stderr)
        r = subprocess.run([str(self.shim_bin / "apod-ssh-connect"), "evil.example", "22"], input=b"x", env=e,
                           capture_output=True, timeout=20)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(b"rejected", r.stderr)
        # A host in scope, but another port: a tunnel, not ssh.
        r = subprocess.run([str(self.shim_bin / "apod-ssh-connect"), "devbox", "8080"], input=b"x", env=e,
                           capture_output=True, timeout=20)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(b"ssh:connect port", r.stderr)
        known = (Path(sess["dir"]) / "known_hosts").read_text()
        self.assertIn("devbox ssh-ed25519", known)
        conf = (Path(sess["dir"]) / "ssh_config").read_text()
        self.assertIn("ProxyCommand", conf)
        # ssh devbox in the pod logs in as the host's ssh would, not as the pod's user.
        self.assertIn(f"Host devbox\n    User {getpass.getuser()}\n", conf)
        sess = self.register("ops", ["ssh:deploy@nas"], toml=f'[ssh]\nkey = "{self.tmp}/userkey"\n')
        self.assertIn("Host nas\n    User deploy\n", (Path(sess["dir"]) / "ssh_config").read_text())

    # --- rad -------------------------------------------------------------------------------

    def test_rad_is_opt_in_and_on_only_for_radicle_projects(self):
        toml = f'[rad]\nnid = "{NID}"\nstorage = "{self.tmp}/radstore"\nhome = "{self.tmp}/radhome"\n'
        # Neither enabled nor autodetect: off, even in a Radicle project (with a warning); GitHub stays on.
        w = []
        sess = self.register("dev", [], toml=toml, remotes=[f"rad://{RID}", "https://github.com/o/r"], warnings=w)
        self.assertNotIn("rad", sess["providers"])
        self.assertNotIn(f"rad:{RID}", sess["scope"])
        self.assertIn("github", sess["providers"])
        self.assertIn("but the broker's rad extension is off", w[0])
        self.assertFalse(any("rad" in str(c) for c in sess["git_config"]))
        # A rad: entry without enabled: still off, and dropped from the scope.
        sess = self.register("dev", [f"rad:{RID}"], toml=toml)
        self.assertNotIn("rad", sess["providers"])
        self.assertIn("rad (not set up", sess["context"])
        self.assertNotIn(f"rad:{RID}", sess["scope"])
        # enabled = true: on for the rad: entries, not for the folder's remotes.
        on = toml + "enabled = true\n"
        self.assertIn("rad", self.register("dev", [f"rad:{RID}"], toml=on)["providers"])
        sess = self.register("dev", [], toml=on, remotes=[f"rad://{RID}"])
        self.assertNotIn("rad", sess["providers"])
        self.assertIn("rad (nothing in scope to act on", sess["context"])
        self.assertEqual(sess["need_keys"], [])
        # autodetect = true: the folder's rad remotes join the scope.
        auto = toml + "autodetect = true\n"
        sess = self.register("dev", [], toml=auto, remotes=[f"rad://{RID}"])
        self.assertIn("rad", sess["providers"])
        self.assertIn(f"rad:{RID}", sess["scope"])
        # A role that can't push: on (fetches), without asking for the identity's key.
        sess = self.register("reader", [], toml=auto, remotes=[f"rad://{RID}"])
        self.assertIn("rad", sess["providers"])
        self.assertFalse([k for k in sess["need_keys"] if "radicle" in k["path"]])
        # enabled = false wins.
        sess = self.register("dev", [], toml=auto + "enabled = false\n", remotes=[f"rad://{RID}"])
        self.assertNotIn("rad", sess["providers"])
        self.assertIn("rad (turned off", sess["context"])

    def test_rad_fetch_and_push(self):
        toml = f'[rad]\nautodetect = true\nnid = "{NID}"\nstorage = "{self.tmp}/radstore"\nhome = "{self.tmp}/radhome"\n'
        sess = self.register("dev", [], toml=toml, remotes=[f"rad://{RID}"])
        self.assertIn("rad", sess["providers"])
        self.assertIn(f"rad:{RID}", sess["scope"])
        e = self.git_env(sess)
        d = self.tmp / "radclone"
        r = subprocess.run(["git", "clone", "-q", f"rad://{RID}", d], env=e, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        refs = subprocess.run(["git", "-C", d, "ls-remote", "origin"], env=e, capture_output=True, text=True).stdout
        self.assertIn("refs/heads/master", refs)
        self.assertNotIn("namespaces", refs)
        subprocess.run(["git", "-C", d, "commit", "-q", "--allow-empty", "-m", "work"], env=e, check=True)
        r = subprocess.run(["git", "-C", d, "push", "origin", "HEAD:refs/heads/topic"], env=e, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        head = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"], env=e, capture_output=True, text=True).stdout.strip()
        onward = lambda ref: subprocess.run(["git", "-C", self.onward, "rev-parse", "-q", "--verify",
                                             f"refs/namespaces/{NID}/{ref}"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(onward("refs/heads/topic"), head)
        r = subprocess.run(["git", "-C", d, "push", "-o", "patch.message=hi", "origin", "HEAD:refs/patches"], env=e,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(onward("refs/heads/patches-pushed"), head)
        self.assertIn("patch.message=hi", Path(str(self.onward) + ".options").read_text())
        r = subprocess.run(["git", "-C", d, "push", "origin", "HEAD:master"], env=e, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("rad:push default-branch", r.stderr)
        self.assertNotEqual(onward("refs/heads/master"), head)
        # Rewriting topic: a force push, which dev may do; it goes on as one.
        subprocess.run(["git", "-C", d, "commit", "-q", "--amend", "--allow-empty", "-m", "rewritten"], env=e, check=True)
        r = subprocess.run(["git", "-C", d, "push", "-f", "origin", "HEAD:refs/heads/topic"], env=e, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        new = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"], env=e, capture_output=True, text=True).stdout.strip()
        self.assertEqual(onward("refs/heads/topic"), new)
        self.assertIn(f"git push --force rad:{RID} refs/heads/topic", self.admin("log", "-n", "3").stdout)

    def test_tui_approves(self):
        import fcntl
        import pty
        import select
        import struct
        import termios
        p = subprocess.Popen([sys.executable, SHIM, "release", "delete", "v9", "--yes"], cwd=self.work,
                             env=dict(self.env, APOD_BROKER_SOCK=str(Path(self.sess["dir"]) / "broker.sock")),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(40):
            if "release delete" in self.admin("pending").stdout:
                break
            time.sleep(0.1)
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
        ui = subprocess.Popen([sys.executable, BROKER, "ui"], stdin=slave, stdout=slave, stderr=slave,
                              env=dict(self.env, TERM="xterm-256color", APOD_BROKER_NOTIFY="osc9"), start_new_session=True)
        os.close(slave)
        screen = b""

        def read_until(text, timeout=5):
            nonlocal screen
            end = time.time() + timeout
            while time.time() < end:
                if text in screen:
                    return True
                if select.select([master], [], [], 0.1)[0]:
                    screen += os.read(master, 65536)
                    if text in screen:
                        return True
            return False

        self.assertTrue(read_until(b"release delete"), screen[-2000:])
        self.assertTrue(read_until(b"Allow once"))
        os.write(master, b"a")  # the key
        o, e = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 0, e)
        self.assertTrue(read_until(b"allowed once"))
        # The mouse: a click on the "a Allow once" button (bottom row, from column 1) approves too.
        p = subprocess.Popen([sys.executable, SHIM, "release", "delete", "v8", "--yes"], cwd=self.work,
                             env=dict(self.env, APOD_BROKER_SOCK=str(Path(self.sess["dir"]) / "broker.sock")),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertTrue(read_until(b"release delete v8"))
        # A request arriving while the TUI runs: a desktop notification through the terminal.
        self.assertTrue(read_until(b"\x1b]9;apod broker: test asks: gh release delete v8"), screen[-500:])
        time.sleep(0.5)
        os.write(master, b"\x1b[<0;3;30M\x1b[<0;3;30m")
        o, e = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 0, e)
        # y copies the selected request (OSC 52, base64); m lets go of the mouse to select text.
        os.write(master, b"y")
        self.assertTrue(read_until(b"\x1b]52;c;"), screen[-500:])
        copied = screen.split(b"\x1b]52;c;")[-1].split(b"\x1b")[0]
        self.assertIn(b"release delete v8", base64.b64decode(copied))
        os.write(master, b"m")
        self.assertTrue(read_until(b"mouse off"))
        self.assertTrue(read_until(b"Mouse on"))  # the button now turns it back on
        screen = b""
        os.write(master, b"m")
        self.assertTrue(read_until(b"elect text"))  # curses redraws only what changed
        os.write(master, b"\x1b[<0;16;2M\x1b[<0;16;2m")  # the Sessions tab
        self.assertTrue(read_until(b"acme/widget"))
        os.write(master, b"q")
        ui.wait(timeout=5)
        os.close(master)
        self.assertEqual(ui.returncode, 0)

class PathsTest(unittest.TestCase):
    """broker/paths.py: where apod's files are, for each environment."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="apod-paths-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def lay(self, **env):
        return paths.Layout(dict(env, HOME=str(self.home)))

    def test_folders(self):
        h = str(self.home)
        cases = [  # (environment, config, data, state)
            ({}, f"{h}/.config/apod", f"{h}/.local/share/apod", f"{h}/.local/state/apod"),
            ({"XDG_CONFIG_HOME": "/x/c", "XDG_DATA_HOME": "/x/d", "XDG_STATE_HOME": "/x/s"},
             "/x/c/apod", "/x/d/apod", "/x/s/apod"),
            ({"XDG_CONFIG_HOME": "rel/c", "XDG_DATA_HOME": "", "XDG_STATE_HOME": "rel"},  # only absolute ones count
             f"{h}/.config/apod", f"{h}/.local/share/apod", f"{h}/.local/state/apod"),
            ({"XDG_CONFIG_HOME": "/x/c", "APOD_CONFIG_DIR": "/a/c"}, "/a/c", f"{h}/.local/share/apod", f"{h}/.local/state/apod"),
            ({"XDG_DATA_HOME": "/x/d", "APOD_DATA_DIR": "/a/d"}, f"{h}/.config/apod", "/a/d", f"{h}/.local/state/apod"),
            ({"XDG_STATE_HOME": "/x/s", "APOD_STATE_DIR": "/a/s"}, f"{h}/.config/apod", f"{h}/.local/share/apod", "/a/s"),
        ]
        for env, config, data, state in cases:
            with self.subTest(env=env):
                p = self.lay(**env)
                self.assertFalse(p.legacy)
                self.assertIsNone(p.notice)
                self.assertEqual((str(p.config), str(p.data), str(p.state)), (config, data, state))
                self.assertEqual(p.config_file, Path(config) / "config.toml")
                self.assertEqual(p.roles, Path(config) / "roles.toml")
                self.assertEqual(p.extensions, Path(config) / "extensions")
                self.assertEqual(p.worklog, Path(data) / "worklog")
                self.assertEqual(p.history, Path(state) / "history.jsonl")
                self.assertEqual((p.broker, p.log, p.grants, p.sessions_state, p.selinux),
                                 (Path(state) / "broker", Path(state) / "broker/log.jsonl", Path(state) / "broker/grants.json",
                                  Path(state) / "broker/sessions", Path(state) / "selinux"))
                prof = p.profile("work", pid=42)
                self.assertEqual((prof["dir"], prof["file"], prof["home"], prof["google_credentials"]),
                                 (None, Path(config) / "profiles/work.toml", Path(data) / "profiles/work/home",
                                  Path(config) / "profiles/work.google-credentials.json"))
        with self.assertRaises(paths.PathError):
            self.lay(APOD_STATE_DIR="relative/state")

    def test_runtime_folder(self):
        uid = os.getuid()
        cases = [  # (environment, runtime folder)
            ({"XDG_RUNTIME_DIR": "/run/user/7"}, "/run/user/7/apod"),
            ({"XDG_RUNTIME_DIR": "/run/user/7", "APOD_RUNTIME_DIR": "/r/t"}, "/r/t"),
            ({"TMPDIR": "/var/tmp/x"}, f"/var/tmp/x/apod-{uid}"),       # no XDG_RUNTIME_DIR
            ({"XDG_RUNTIME_DIR": "rel", "TMPDIR": "/t"}, f"/t/apod-{uid}"),  # only an absolute one counts
            ({}, f"/tmp/apod-{uid}"),
        ]
        for env, rt in cases:
            with self.subTest(env=env):
                p = self.lay(**env)
                self.assertEqual(p.runtime, Path(rt))
                self.assertEqual((p.admin_sock, p.agent_sock, p.sessions_run),
                                 (Path(rt) / "admin.sock", Path(rt) / "agent.sock", Path(rt) / "sessions"))
                self.assertEqual(p.sessions_state, p.state / "broker/sessions")  # the state stays in state
                prof = p.profile("work", pid=42)
                self.assertEqual((prof["ssh_agent_sock"], prof["ssh_agent_mount"]),
                                 (Path(rt) / "ssh-work-42.sock", Path(rt) / "ssh-work-42.sock"))
        self.assertEqual(self.lay(APOD_ROOT="/r", XDG_RUNTIME_DIR="/run/user/7").runtime, Path("/r/broker"))

    def test_legacy(self):
        p = self.lay(APOD_ROOT="/r", APOD_CONFIG_DIR="/a/c")  # APOD_ROOT: the legacy layout, whatever else is set
        self.assertTrue(p.legacy)
        self.assertEqual(p.notice, "legacy layout: apod migrate --layout, then unset APOD_ROOT")
        self.assertEqual({k: str(getattr(p, k)) for k in ("config", "data", "state", "runtime", "config_file", "roles", "extensions",
                                                          "worklog", "history", "broker", "selinux", "admin_sock",
                                                          "agent_sock", "sessions_state", "sessions_run")},
                         {"config": "/r", "data": "/r", "state": "/r", "config_file": "/r/broker.toml",
                          "roles": "/r/roles.toml", "extensions": "/r/broker-extensions", "worklog": "/r/worklog",
                          "history": "/r/history.jsonl", "broker": "/r/broker", "selinux": "/r/selinux",
                          "admin_sock": "/r/broker/admin.sock", "agent_sock": "/r/broker/agent.sock", "runtime": "/r/broker",
                          "sessions_state": "/r/broker/sessions", "sessions_run": "/r/broker/sessions"})
        prof = p.profile("work", pid=42)
        self.assertEqual((prof["dir"], prof["file"], prof["home"], prof["google_credentials"], prof["ssh_agent_sock"],
                          prof["ssh_agent_mount"]),
                         (Path("/r/work"), Path("/r/work/profile.toml"), Path("/r/work/home"),
                          Path("/r/work/google-credentials.json"), Path("/r/work/agent/ssh-42.sock"), Path("/r/work/agent")))

    def test_legacy_detection(self):
        old = self.home / ".agent-pod"
        self.assertFalse(self.lay().legacy)                   # a new install: the new layout
        (old / "work").mkdir(parents=True)
        (old / "broker").mkdir()
        p = self.lay()
        self.assertTrue(p.legacy)                             # only ~/.agent-pod: the legacy layout
        self.assertEqual((p.config, p.notice), (old, "legacy layout: apod migrate --layout"))
        self.assertEqual(p.profile_names(), ["work"])
        for new in (".config/apod", ".local/share/apod", ".local/state/apod"):  # any of the new folders: the new layout
            with self.subTest(new=new):
                (self.home / new).mkdir(parents=True)
                p = self.lay()
                self.assertFalse(p.legacy)
                # ... and ~/.agent-pod, unused then, is pointed out.
                self.assertEqual(p.notice, f"{old} is there too, and isn't used: apod migrate --layout moves what's in it")
                (self.home / new).rmdir()
        self.assertTrue(self.lay().legacy)
        (self.home / "st/apod").mkdir(parents=True)  # the new folders where the environment says they are
        self.assertFalse(self.lay(XDG_STATE_HOME=str(self.home / "st")).legacy)
        self.assertTrue(self.lay(XDG_STATE_HOME=str(self.home / "nonesuch")).legacy)

    def test_profile_names(self):
        p = self.lay()
        for bad in ("", "-x", ".x", "a/b", "../x", "a b", "new", "broker", "worklog", "broker-extensions",
                    "x.google-credentials"):
            with self.subTest(name=bad):
                self.assertIsNotNone(paths.name_error(bad))
                with self.assertRaises(paths.PathError):
                    p.profile(bad)
        for good in ("work", "a.b", "a_b-c", "x.google-credentialsx", "0"):
            self.assertIsNone(paths.name_error(good), good)
        (p.profiles).mkdir(parents=True)
        for f in ("work.toml", "b.toml", "work.google-credentials.json", "notes.txt", "-bad.toml", "new.toml"):
            (p.profiles / f).write_text("")
        (p.profiles / "dir.toml").mkdir()
        (p.profiles / "folder").mkdir()
        self.assertEqual(p.profile_names(), ["b", "work"])

    def test_paths_command(self):
        env = dict(os.environ, HOME=str(self.home), APOD_CONFIG_DIR=str(self.home / "c"))
        for k in ("APOD_ROOT", "APOD_DATA_DIR", "APOD_STATE_DIR"):
            env.pop(k, None)
        r = subprocess.run([sys.executable, BROKER, "paths", "--json", "--profile", "work"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["layout"], out["config"], out["profile"]["file"], out["profile"]["exists"]),
                         ("xdg", str(self.home / "c"), str(self.home / "c/profiles/work.toml"), False))
        r = subprocess.run([sys.executable, BROKER, "paths", "--check-name", "migrate"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("is an apod command or folder", r.stderr)
        r = subprocess.run([sys.executable, BROKER, "paths", "--json", "--profile", "../x"], env=env, capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (2, ""))
        r = subprocess.run([sys.executable, BROKER, "paths"], env=env, capture_output=True, text=True)
        self.assertIn(f"config   {self.home}/c", r.stdout)


def unix_get(sock, path):
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(str(sock))
        s.sendall(f"GET {path} HTTP/1.0\r\n\r\n".encode())
        data = b"".join(iter(lambda: s.recv(65536), b""))
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body or b"{}")


class RuntimeTest(unittest.TestCase):
    """The broker's sockets are in the runtime folder (sockets only, mode 0700, gone at logout);
    what it keeps is in the state folder, so a session outlives a broker restart. In the legacy
    layout, both are ~/.agent-pod/broker/."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apod-rt-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "home").mkdir()
        (self.tmp / "proj/.git").mkdir(parents=True)
        # config, data and state deep enough that a socket there would be too long a path
        self.deep = self.tmp / ("deep-" + "x" * 100)

    def env(self, **layout):
        return host_env(HOME=str(self.tmp / "home"), APOD_BROKER_ASK_TIMEOUT="2", **layout)

    def new_layout(self):
        run = self.tmp / "run"
        run.mkdir(mode=0o700)
        return self.env(APOD_CONFIG_DIR=str(self.deep / "config"), APOD_DATA_DIR=str(self.deep / "data"),
                        APOD_STATE_DIR=str(self.deep / "state"), XDG_RUNTIME_DIR=str(run))

    def broker(self, env, *args, expect=0):
        r = subprocess.run([sys.executable, BROKER, *args], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, expect, r.stderr)
        return r

    def register(self, env, name="p"):
        """Start the broker (stopped at the end) and register a pod of profile NAME."""
        self.addCleanup(subprocess.run, [sys.executable, BROKER, "stop"], env=env, capture_output=True)
        prof = paths.Layout(env).profile(name)
        f = Path(prof["file"])
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text('[git]\nname = "A"\nemail = "a@example.com"\n')
        where = ["--profile-dir", str(prof["dir"])] if prof["dir"] else ["--profile-file", str(f)]
        r = self.broker(env, "register", "--profile", name, *where, "--project", str(self.tmp / "proj"))
        return json.loads(r.stdout)

    def test_sockets_are_in_the_runtime_folder(self):
        env = self.new_layout()
        P = paths.Layout(env)
        self.assertEqual(P.runtime, self.tmp / "run/apod")
        sess = self.register(env)
        self.assertEqual(stat.S_IMODE(P.runtime.stat().st_mode), 0o700)
        self.assertTrue(P.admin_sock.is_socket())
        self.assertEqual(sess["agent"], str(self.tmp / "run/apod/agent.sock"))
        self.assertEqual(sess["dir"], str(self.tmp / "run/apod/sessions" / sess["id"]))
        self.assertTrue((Path(sess["dir"]) / "broker.sock").is_socket())
        # What it keeps is in the state folder, and no socket is.
        self.assertTrue((P.sessions_state / f"{sess['id']}.json").is_file())
        self.assertTrue(P.daemon_log.is_file())
        self.assertEqual([f for f in P.state.rglob("*") if f.is_socket()], [])
        self.assertFalse(list(P.runtime.rglob("*.json")))

    def test_sessions_survive_a_restart(self):
        env = self.new_layout()
        sess = self.register(env)
        sock = Path(sess["dir"]) / "broker.sock"
        self.broker(env, "restart")
        self.assertIn(sess["id"], self.broker(env, "sessions").stdout)
        status, who = unix_get(sock, "/whoami")
        self.assertEqual((status, who["role"]), (200, "reader"))
        # The runtime folder goes at logout: the session comes back from its state.
        self.broker(env, "stop")
        shutil.rmtree(self.tmp / "run/apod")
        self.broker(env, "ensure")
        self.assertIn(sess["id"], self.broker(env, "sessions").stdout)
        self.assertEqual(unix_get(sock, "/whoami")[0], 200)

    def test_a_broker_without_its_runtime_folder_exits(self):
        # At logout the runtime folder goes, but a broker may live on: it exits, and the next one
        # (started at once here) keeps its ssh-agent's socket, at the same path.
        env = self.new_layout()
        P = paths.Layout(env)
        self.register(env)

        def brokers():  # this test's: the broker runs in its state folder
            out = []
            for d in Path("/proc").iterdir():
                try:
                    if d.name.isdigit() and os.readlink(d / "cwd") == str(P.broker) \
                            and b"serve" in (d / "cmdline").read_bytes():
                        out.append(d.name)
                except OSError:
                    pass
            return out
        old = brokers()
        self.assertEqual(len(old), 1)
        shutil.rmtree(P.runtime)
        self.broker(env, "ensure")
        for _ in range(100):
            if old[0] not in brokers():
                break
            time.sleep(0.1)
        self.assertEqual(len(brokers()), 1, "the old broker is gone, the new one runs")
        self.assertNotIn(old[0], brokers())
        self.assertIn("is gone (its folder removed at logout?) or another broker's: exiting", P.daemon_log.read_text())
        self.assertTrue(P.agent_sock.is_socket())
        r = subprocess.run(["ssh-add", "-l"], env=dict(env, SSH_AUTH_SOCK=str(P.agent_sock)), capture_output=True, text=True)
        self.assertIn(r.returncode, (0, 1), r.stderr)  # 1: no keys; 2: no agent

    def test_runtime_folder_without_xdg_runtime_dir(self):
        t = self.tmp / "t"
        t.mkdir()
        env = self.env(APOD_CONFIG_DIR=str(self.deep / "config"), TMPDIR=str(t))
        P = paths.Layout(env)
        rt = t / f"apod-{os.getuid()}"
        self.assertEqual(P.runtime, rt)
        self.broker(env, "paths", "--ensure-runtime")
        self.assertEqual(stat.S_IMODE(rt.stat().st_mode), 0o700)
        # Not private: refused, and the broker doesn't start.
        rt.chmod(0o755)
        r = self.broker(env, "paths", "--ensure-runtime", expect=2)
        self.assertIn(f"apod's runtime folder {rt} has mode 0755, not 0700: chmod 700 {rt}", r.stderr)
        r = self.broker(env, "ensure", expect=1)
        self.assertIn("has mode 0755, not 0700", r.stderr)
        rt.rmdir()
        rt.symlink_to(t)
        self.assertIn("isn't a folder", self.broker(env, "paths", "--ensure-runtime", expect=2).stderr)
        if os.getuid() != 0:  # someone else's: root's /, here
            with self.assertRaisesRegex(paths.PathError, "belongs to someone else"):
                paths.ensure_private(Path("/"))
        # APOD_RUNTIME_DIR: there, made with its parents.
        env = self.env(APOD_STATE_DIR=str(self.deep / "state"), APOD_RUNTIME_DIR=str(self.tmp / "a/b"))
        self.assertEqual(paths.Layout(env).runtime, self.tmp / "a/b")
        self.broker(env, "paths", "--ensure-runtime")
        self.assertEqual(stat.S_IMODE((self.tmp / "a/b").stat().st_mode), 0o700)

    def test_legacy_layout_keeps_its_sockets_in_broker(self):
        root = self.tmp / "ap"
        env = self.env(APOD_ROOT=str(root), XDG_RUNTIME_DIR=str(self.tmp / "unused"))
        sess = self.register(env)
        self.assertTrue((root / "broker/admin.sock").is_socket())
        self.assertEqual((sess["agent"], sess["dir"]), (str(root / "broker/agent.sock"),
                                                         str(root / "broker/sessions" / sess["id"])))
        self.assertTrue((root / "broker/sessions" / f"{sess['id']}.json").is_file())
        self.assertFalse((self.tmp / "unused").exists())


class ProfileLoaderTest(unittest.TestCase):
    """`apod-broker profile`: one profile's settings as JSON, from profile.toml or the old files."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="apod-profile-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.env = host_env(APOD_ROOT=str(self.dir), HOME="/home/agent-test")
        self.pdir = self.dir / "work"
        (self.pdir / "home").mkdir(parents=True)

    def load(self, expect=0, *extra):
        r = subprocess.run([sys.executable, BROKER, "profile", "--profile-dir", str(self.pdir), *extra],
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, expect, r.stderr)
        return (json.loads(r.stdout) if expect == 0 else None), r.stderr

    def write(self, name, text):
        (self.pdir / name).write_text(textwrap.dedent(text))

    NEW = """\
        broker = true
        role   = "reader"
        scope  = ["gh:fryorcraken/*", "gh-search:all"]
        allow  = ["clipboard:image"]
        deny   = ["gh:pr merge"]

        [pod]
        image = "myimage"
        network = "host"
        ro = ["~/ro1", "/ro2"]
        rw = ["/rw1"]
        volumes = { "apod-work-nix" = "/nix" }
        connectors = ["gdoc", "todoist"]
        [pod.env]
        SOME_VAR = "a b"
        QUOTED = "x"

        [git]
        name = "Bot Agent"
        email = "bot@example.com"

        [gh]
        user = "fryorcraken"
        """

    # The same profile in the old files.
    OLD = {
        "broker.toml": """\
            role = "reader"
            scope = ["gh:fryorcraken/*", "gh-search:all"]
            allow = ["clipboard:image"]
            deny = ["gh:pr merge"]
            [gh]
            user = "fryorcraken"
            """,
        "env": """\
            GIT_AUTHOR_NAME="Bot Agent"
            GIT_AUTHOR_EMAIL=bot@example.com
            # a comment
            SOME_VAR='a b'
            QUOTED="x"
            """,
        "mounts": "# mounts\n~/ro1\n/ro2\nrw /rw1\n",
        "image": "myimage\n", "network": "host\n",
        "volumes": "# a volume\napod-work-nix /nix\n", "connectors": "gdoc, todoist\n",
    }

    def test_profile_toml(self):
        self.write("profile.toml", self.NEW)
        p, err = self.load()
        self.assertEqual(err, "")
        self.assertEqual(p["format"], "profile.toml")
        self.assertEqual((p["broker"], p["role"], p["scope"], p["allow"], p["deny"]),
                         (True, "reader", ["gh:fryorcraken/*", "gh-search:all"], ["clipboard:image"], ["gh:pr merge"]))
        self.assertEqual(p["pod"], {"image": "myimage", "network": "host", "volumes": {"apod-work-nix": "/nix"},
                                    "mounts": [["ro", "/home/agent-test/ro1"], ["ro", "/ro2"], ["rw", "/rw1"]],
                                    "connectors": ["gdoc", "todoist"], "env": {"SOME_VAR": "a b", "QUOTED": "x"},
                                    "env_from": []})
        self.assertEqual(p["git"], {"name": "Bot Agent", "email": "bot@example.com"})
        self.assertEqual(p["tables"], {"gh": {"user": "fryorcraken"}})

    def test_defaults(self):
        self.write("profile.toml", "")
        p, err = self.load()
        self.assertEqual((p["broker"], p["role"], p["scope"], p["pod"]["image"], p["pod"]["mounts"]), (True, None, [], None, []))
        self.write("profile.toml", "broker = false\n[token]\nprofile = \"other\"\n[ssh-agent]\nkey = \"~/k\"\nconfirm = true\n")
        p, err = self.load()
        self.assertEqual((p["broker"], p["token"], p["ssh_agent"]),
                         (False, {"profile": "other"}, {"key": "/home/agent-test/k", "confirm": True}))

    def test_old_files(self):
        for f, text in self.OLD.items():
            self.write(f, text)
        self.write("github", "other\n")
        self.write("ssh-key", "~/key\n")
        self.write("ssh-confirm", "")
        p, err = self.load()
        self.assertEqual(p["format"], "old files")
        new = self.load_new(self.NEW)
        for k in ("broker", "role", "scope", "allow", "deny", "pod", "git", "tables"):
            self.assertEqual(p[k], new[k], k)
        self.assertEqual((p["token"], p["ssh_agent"]), ({"profile": "other"}, {"key": "/home/agent-test/key", "confirm": True}))
        self.assertIn("old profile files (broker.toml, env, mounts, image, network, volumes, connectors, github, "
                      "ssh-key, ssh-confirm): apod migrate work", err)
        self.assertEqual(p["warnings"], [err.strip().removeprefix("apod: ")])
        # Without broker.toml there is no broker; nothing at all: no warning either.
        (self.pdir / "broker.toml").unlink()
        self.assertFalse(self.load()[0]["broker"])
        for f in self.OLD:
            (self.pdir / f).unlink(missing_ok=True)
        for f in ("github", "ssh-key", "ssh-confirm"):
            (self.pdir / f).unlink()
        p, err = self.load()
        self.assertEqual((p["format"], p["broker"], err), ("none", False, ""))

    def load_new(self, text):
        d = self.dir / "new"
        d.mkdir()
        (d / "profile.toml").write_text(textwrap.dedent(text))
        r = subprocess.run([sys.executable, BROKER, "profile", "--profile-dir", str(d)], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        shutil.rmtree(d)
        return json.loads(r.stdout)

    def test_both_present_uses_profile_toml(self):
        self.write("profile.toml", 'role = "dev"\n')
        self.write("broker.toml", 'role = "maintainer"\n')
        self.write("env", "GIT_AUTHOR_NAME=Old\n")
        p, err = self.load()
        self.assertEqual((p["role"], p["git"]), ("dev", {}))
        self.assertIn("profile.toml is used; the old files (broker.toml, env) are ignored", err)
        self.assertEqual(self.load(0, "--quiet")[1], "")

    def test_old_persona(self):
        self.write("broker.toml", 'persona = "maintainer"\n')
        p, err = self.load()
        self.assertEqual(p["role"], "maintainer")
        self.assertIn("persona = is now role =", err)

    def test_a_key_after_a_table_is_refused(self):
        for k, v in (("role", '"dev"'), ("scope", '["gh:a/b"]'), ("allow", '["gh:pr list"]'), ("deny", "[]"), ("broker", "false")):
            for table in ("gh", "pod", "git", "token", "ssh-agent"):
                self.write("profile.toml", f"[{table}]\n{k} = {v}\n")
                _, err = self.load(2)
                self.assertIn(f"[{table}] has {k} = ...: move it above the first [table]", err)

    def test_unknown_keys_and_types_are_errors(self):
        bad = [
            ("[pod]\nimg = \"x\"\n", "[pod] has no key img"),
            ("[pod]\nmounts = []\n", "[pod] has no key mounts"),
            ("[git]\nnmae = \"x\"\n", "[git] has no key nmae"),
            ("[token]\nname = \"x\"\n", "[token] has no key name"),
            ("[ssh-agent]\nconfirmed = true\n", "[ssh-agent] has no key confirmed"),
            ("persona = \"dev\"\n", "unknown key persona"),
            ("broker = \"yes\"\n", "broker is true or false"),
            ("role = 3\n", "role is a string"),
            ("scope = \"gh:a/b\"\n", "scope is a list of strings"),
            ("allow = [\"nocolon\"]\n", 'allow is a list of "PROVIDER:ACTION" patterns'),
            ("[pod]\nro = \"/x\"\n", "ro is a list of strings"),
            ("[pod]\nimage = 3\n", "image is a string"),
            ("[pod]\nvolumes = { v = 3 }\n", "[pod] volumes is a table"),
            ("[pod.env]\nN = 3\n", "values are strings"),
            ("[pod.env]\n\"A B\" = \"x\"\n", "isn't an environment variable name"),
            ("[pod.env]\nGIT_AUTHOR_NAME = \"x\"\n", "the git identity is [git] name and email"),
            ("[pod]\nenv_from = \"API_KEY\"\n", "env_from is a list of strings"),
            ("[pod]\nenv_from = [\"A B\"]\n", "env_from: 'A B' isn't an environment variable name"),
            ("[pod]\nenv_from = [\"API_KEY=x\"]\n", "env_from: 'API_KEY=x' isn't an environment variable name"),
            ("[pod]\nenv_from = [\"GIT_COMMITTER_EMAIL\"]\n", "env_from: GIT_COMMITTER_EMAIL: the git identity is [git]"),
            ("[pod]\nenv_from = [\"API_KEY\"]\n[pod.env]\nAPI_KEY = \"x\"\n", "API_KEY is in [pod.env] too"),
            ("[git]\nemail = \"nope\"\n", "email isn't an email address"),
            ("[ssh-agent]\nconfirm = \"yes\"\n", "confirm is true or false"),
            ("[token]\nprofile = \"a b\"\n", "profile is the name of a profile"),
            ("[pod\n", "profile.toml"),
        ]
        for text, msg in bad:
            self.write("profile.toml", text)
            _, err = self.load(2)
            self.assertIn(msg, err, text)
            self.assertIn(str(self.pdir / "profile.toml"), err)

    def mount_error(self, mode, path, *, cli=False, old=False):
        """The loader's answer to a mount: None when allowed, else its error."""
        args = []
        if old:
            (self.pdir / "profile.toml").unlink(missing_ok=True)
            self.write("mounts", f"{mode} {path}\n")
        elif cli:
            self.write("profile.toml", "")
            args = [f"--{mode}", str(path)]
        else:
            self.write("profile.toml", f'[pod]\n{mode} = ["{path}"]\n')
        r = subprocess.run([sys.executable, BROKER, "profile", "--profile-dir", str(self.pdir), *args],
                           env=self.env, capture_output=True, text=True)
        return r.stderr if r.returncode else None

    def test_mount_guard(self):
        root = self.dir
        for d in ("worklog/s1", "broker", "work/home"):
            (root / d).mkdir(parents=True, exist_ok=True)
        (root / "history.jsonl").write_text("")
        (root / "broker/log.jsonl").write_text("")
        for how in ({}, {"cli": True}, {"old": True}):
            # Refused: the apod folder, what's in it, what holds it, and the checkout.
            for path in (root, root / "work", root / "work/home", root / "broker", root / "broker/sessions",
                         root / "roles.toml", root / "worklog/../broker", root.parent, "/", HERE, HERE / "broker",
                         HERE / "apod", HERE.parent):
                for mode in ("ro", "rw"):
                    err = self.mount_error(mode, path, **how)
                    self.assertIn(f"{mode} mount {path}", err or f"allowed {mode} {path} {how}")
                    self.assertIn("is refused", err)
            # Allowed: read-only, the worklog and what's in it, the history, the broker's log.
            for path in (root / "worklog", root / "worklog/s1", root / "worklog/s1/worklog.md", root / "history.jsonl",
                         root / "broker/log.jsonl", "/etc", "~/projects"):
                self.assertIsNone(self.mount_error("ro", path, **how), f"{path} {how}")
            # ... but not read-write, nor by a path that leads elsewhere.
            for path in (root / "worklog", root / "worklog/s1", root / "history.jsonl", root / "broker/log.jsonl"):
                self.assertIn("is refused", self.mount_error("rw", path, **how) or "")
            self.assertIn("is refused", self.mount_error("ro", root / "worklog/s1/../../broker", **how) or "")
            # Your home folder: not read-write; folders in it are fine.
            self.assertIn("is your home folder", self.mount_error("rw", "~", **how) or "")
            self.assertIn("is your home folder", self.mount_error("rw", "/home/agent-test", **how) or "")
            self.assertIsNone(self.mount_error("rw", "~/projects", **how))
            self.assertIsNone(self.mount_error("ro", "~", **how))
        # A symlink to the apod folder is the apod folder.
        link = self.dir.parent / f"{self.dir.name}-link"
        link.symlink_to(root)
        self.addCleanup(link.unlink)
        err = self.mount_error("rw", link)
        self.assertIn(f"is apod's folder {root.resolve()}", err or "")
        self.assertIn("(" + str(root.resolve()) + ")", err)
        # What it says, and the same for the old mounts file.
        err = self.mount_error("rw", root / "work")
        self.assertIn(str(self.pdir / "profile.toml"), err)
        self.assertIn("is inside apod's folder", err)
        self.assertIn("rewrite its own configuration", err)
        self.assertIn(str(self.pdir / "mounts"), self.mount_error("rw", root, old=True))
        self.assertTrue(self.mount_error("rw", root, cli=True).startswith("apod: --rw: rw mount"))
        # The launch folder is mounted anyway: no check (a pod working on apod itself).
        self.write("profile.toml", "")
        r = subprocess.run([sys.executable, BROKER, "profile", "--profile-dir", str(self.pdir), "--project", str(HERE),
                            "--rw", str(HERE)], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_profile_file_in_the_new_layout(self):
        """profiles/NAME.toml: one file, with its home in the data folder; no old files there."""
        env = dict(self.env, APOD_CONFIG_DIR=str(self.dir / "c"), APOD_DATA_DIR=str(self.dir / "d"),
                   APOD_STATE_DIR=str(self.dir / "s"))
        del env["APOD_ROOT"]
        (self.dir / "c/profiles").mkdir(parents=True)
        (self.dir / "c/profiles/work.toml").write_text(textwrap.dedent(self.NEW))
        (self.dir / "c/profiles/env").write_text("GIT_AUTHOR_NAME=Old\n")  # not a profile's file: ignored
        runs = [["--profile", "work"], ["--profile-file", str(self.dir / "c/profiles/work.toml")]]
        for args in runs:
            r = subprocess.run([sys.executable, BROKER, "profile", *args], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            p = json.loads(r.stdout)
            self.assertEqual((p["name"], p["dir"], p["file"], p["home"], p["format"]),
                             ("work", None, str(self.dir / "c/profiles/work.toml"), str(self.dir / "d/profiles/work/home"),
                              "profile.toml"))
            self.assertEqual(p["git"], {"name": "Bot Agent", "email": "bot@example.com"})
            self.assertEqual(r.stderr, "")
        for args, msg in ((["--profile", "nope"], f"no profile nope: {self.dir}/c/profiles/nope.toml doesn't exist"),
                          (["--profile", "new"], "is an apod command or folder"),
                          (["--profile", "x.google-credentials"], "ends in .google-credentials"),
                          (["--profile-file", "work.txt"], "a profile's file ends in .toml")):
            r = subprocess.run([sys.executable, BROKER, "profile", *args], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn(msg, r.stderr)
        # apod migrate: nothing to do in this layout.
        r = subprocess.run([sys.executable, BROKER, "migrate", "--all"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("a profile is one file already", r.stderr)

    def test_mount_guard_new_layout(self):
        """The four folders of the new layout, and the read-only exceptions in them."""
        d = self.dir
        env = dict(self.env, APOD_CONFIG_DIR=str(d / "c"), APOD_DATA_DIR=str(d / "d"), APOD_STATE_DIR=str(d / "s"),
                   APOD_RUNTIME_DIR=str(d / "r"))
        del env["APOD_ROOT"]
        for x in ("c/profiles", "c/extensions", "d/profiles/work/home", "d/worklog/s1", "s/broker/sessions", "r/sessions/x"):
            (d / x).mkdir(parents=True)
        for f in ("c/config.toml", "c/roles.toml", "s/history.jsonl", "s/broker/log.jsonl", "s/broker/grants.json",
                  "d/worklog/s1/worklog.md"):
            (d / f).write_text("")
        pf = d / "c/profiles/work.toml"

        def err(mode, path, cli=False):
            pf.write_text("" if cli else f'[pod]\n{mode} = ["{path}"]\n')
            r = subprocess.run([sys.executable, BROKER, "profile", "--profile", "work", *([f"--{mode}", str(path)] if cli else [])],
                               env=env, capture_output=True, text=True)
            return r.stderr if r.returncode else None
        for cli in (False, True):
            for path, what in ((d / "c", "is apod's config folder"), (d / "c/profiles", "is inside apod's config folder"),
                               (pf, "is inside apod's config folder"), (d / "c/roles.toml", "is inside apod's config folder"),
                               (d / "d", "is apod's data folder"), (d / "d/profiles/work/home", "is inside apod's data folder"),
                               (d / "s", "is apod's state folder"), (d / "s/broker/grants.json", "is inside apod's state folder"),
                               (d / "s/broker/sessions", "is inside apod's state folder"),
                               (d / "r", "is apod's runtime folder"), (d / "r/sessions/x", "is inside apod's runtime folder"),
                               (d, "contains apod's config folder"), ("/", "contains apod's config folder"),
                               (HERE, "is the apod checkout")):
                for mode in ("ro", "rw"):
                    e = err(mode, path, cli)
                    self.assertIn(f"{mode} mount {path}", e or f"allowed {mode} {path} cli={cli}")
                    self.assertIn(f"is refused: it {what}", e)
            # Read-only, the work logs, the history and the broker's log are fine; not read-write.
            for path in (d / "d/worklog", d / "d/worklog/s1", d / "d/worklog/s1/worklog.md", d / "s/history.jsonl",
                         d / "s/broker/log.jsonl"):
                self.assertIsNone(err("ro", path, cli), path)
                self.assertIn("is refused", err("rw", path, cli) or f"allowed rw {path}")
            self.assertIn("is inside apod's data folder", err("ro", d / "d/worklog/s1/../../profiles", cli) or "")
        e = err("rw", d / "r")
        self.assertIn("holds the broker's sockets", e)
        self.assertIn(f"(Only read-only mounts of {d}/d/worklog, {d}/s/history.jsonl and {d}/s/broker/log.jsonl are "
                      f"allowed.)", e)
        self.assertIn(str(pf), e)

    def test_needs_no_broker_and_no_extensions(self):
        # The old-files reading too: nothing is started, and the user's extensions aren't run.
        ext = self.dir / "broker-extensions"
        ext.mkdir()
        (ext / "boom.py").write_text(f'open({str(self.dir / "ran")!r}, "w").write("x")\n')
        self.write("profile.toml", 'role = "dev"\n')
        self.load()
        self.assertFalse((self.dir / "ran").exists())
        self.assertFalse((self.dir / "broker").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
