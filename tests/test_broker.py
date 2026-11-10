#!/usr/bin/env python3
"""End-to-end tests for apod-broker and its pod shim, without GitHub, Radicle or sshd: a fake gh
(the real gh for --help, canned answers otherwise), a local git upstream (git http-backend), a
plain repository standing in for Radicle storage, real ssh-agents and ssh-keygen.
Run: python3 tests/test_broker.py (KEEP=1 keeps the temporary directory)
"""
import base64
import itertools
import socket
import struct
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
BROKER = HERE / "apod-broker"
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


class BrokerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (HERE / "tmp").mkdir(exist_ok=True)
        cls.tmp = Path(tempfile.mkdtemp(dir=HERE / "tmp", prefix="broker-test-"))
        cls.root = cls.tmp / "apod"
        cls.root.mkdir()
        bin_ = cls.tmp / "bin"
        bin_.mkdir()
        (bin_ / "gh").write_text(FAKE_GH)
        (bin_ / "gh").chmod(0o755)
        (bin_ / "git-remote-rad").write_text(FAKE_REMOTE_RAD)
        (bin_ / "git-remote-rad").chmod(0o755)
        for name in ("apod-git-forward", "apod-sign", "apod-ssh-connect"):
            os.symlink(SHIM, bin_ / name)
        # The example extension, installed as a user would, and a notify-send that records its args.
        (cls.root / "broker-extensions").mkdir()
        os.symlink(HERE / "extensions/notify.py", cls.root / "broker-extensions/notify.py")
        (cls.root / "broker-extensions/broken.py").write_text("raise RuntimeError('broken on purpose')\n")
        (bin_ / "fake-notify").write_text(f"#!/bin/sh\nprintf '%s|' \"$@\" >> {cls.tmp}/notified\n")
        (bin_ / "fake-notify").chmod(0o755)
        cls.n = itertools.count()
        # The host's ssh setup: a config entry for "fatso" (an echo server stands in for sshd) and
        # its host key in known_hosts; a user key for logins and one for signing.
        home = cls.tmp / "home"
        (home / ".ssh").mkdir(parents=True)
        cls.echo = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
        threading.Thread(target=cls.echo.serve_forever, daemon=True).start()
        port = cls.echo.server_address[1]
        for k in ("hostkey", "hostkey2", "userkey", "signkey"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", k, "-f", cls.tmp / k], check=True)
        (home / ".ssh/config").write_text(f"Host fatso\n  HostName 127.0.0.1\n  Port {port}\n"
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
        cls.env = dict(os.environ, APOD_ROOT=str(cls.root), APOD_GH_BIN=str(bin_ / "gh"),
                       APOD_BROKER_ASK_TIMEOUT="4", HOME=str(home), APOD_SSH_CONFIG=str(home / ".ssh/config"),
                       APOD_GH_GIT_UPSTREAM=f"http://127.0.0.1:{cls.upstream.server_address[1]}",
                       FAKE_RAD_ONWARD=str(cls.tmp / "radstore"), FAKE_RAD_NID=NID,
                       PATH=f"{bin_}:{os.environ['PATH']}")
        for k in list(cls.env):
            if k in ("GH_REPO", "SSH_AUTH_SOCK") or k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG")):
                cls.env.pop(k)
        cls.broker = subprocess.Popen([sys.executable, BROKER, "serve"], env=cls.env,
                                      stderr=open(cls.tmp / "broker.log", "w"))
        for _ in range(100):
            if (cls.root / "broker/admin.sock").exists():
                break
            time.sleep(0.05)
        cls.addClassCleanup(cls.broker.kill)
        cls.sess = cls.register("dev", ["acme/widget"])
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
        if not os.environ.get("KEEP"):
            shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def register(cls, persona, scope, user="", toml="", remotes=()):
        pdir = cls.tmp / "profiles" / f"p{next(cls.n)}"
        pdir.mkdir(parents=True)
        (pdir / "env").write_text("GIT_AUTHOR_NAME=Agent\nGIT_AUTHOR_EMAIL=agent@example.com\n"
                                  "GIT_COMMITTER_NAME=Agent\nGIT_COMMITTER_EMAIL=agent@example.com\n")
        (pdir / "broker.toml").write_text((f'[gh]\nuser = "{user}"\n' if user else "") + toml)
        args = ["register", "--profile", "test", "--profile-dir", str(pdir), "--persona", persona, "--project", "/p"]
        for s in scope:
            args += ["--scope", s]
        for u in remotes:
            args += ["--remote", u]
        r = subprocess.run([sys.executable, BROKER, *args], env=cls.env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        sess = json.loads(r.stdout)
        sess["_pdir"] = pdir
        for k in sess["need_keys"]:  # what apod does with ssh-add
            subprocess.run(["ssh-add", "-q", k["path"]], env=dict(cls.env, SSH_AUTH_SOCK=sess["agent"]), check=True)
        return sess

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
        self.assertEqual(out["GH_TOKEN"], "tok-active")
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

    def test_agent_sees_permissions_and_session_name(self):
        sess = self.register("pm", ["acme/widget", "project:acme/11"])
        self.assertIn("project item-edit", sess["context"])
        self.assertIn("project:acme/11", sess["context"])
        self.assertIn("gh auth status", sess["context"])
        r = self.gh("auth", "status", sess=sess)
        self.assertIn("persona pm", r.stdout)
        self.assertIn("  gh: ", r.stdout)
        # Claude Code's /rename lands in the transcript, in the profile's home; the TUI shows it.
        d = sess["_pdir"] / "home/.claude/projects/-p"
        d.mkdir(parents=True)
        (d / "s1.jsonl").write_text('{"type":"user"}\n{"type":"custom-title","customTitle":"[eco] boards","sessionId":"s1"}\n')
        names = {s["id"]: s.get("name") for s in self.state()["sessions"]}
        self.assertEqual(names[sess["id"]], "[eco] boards")
        with (d / "s1.jsonl").open("a") as f:
            f.write('{"type":"custom-title","customTitle":"renamed","sessionId":"s1"}\n')
        self.assertEqual({s["id"]: s.get("name") for s in self.state()["sessions"]}[sess["id"]], "renamed")

    def state(self):
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(self.root / "broker/admin.sock"))
            s.sendall(b"POST /state HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
            data = b"".join(iter(lambda: s.recv(65536), b""))
        return json.loads(data.split(b"\r\n\r\n", 1)[1])

    def test_extension(self):
        (self.root / "personas.toml").write_text('[personas.dev-notify]\nextends = ["dev"]\nnotify = ["send"]\n')
        self.addCleanup((self.root / "personas.toml").unlink)
        sess = self.register("dev-notify", ["acme/widget"], toml=f'[notify]\nenabled = true\n'
                             f'command = ["{self.tmp}/bin/fake-notify"]\n')
        self.assertIn("notify", sess["providers"])
        self.assertIn(["%s" % (HERE / "extensions/apod-notify"), "/usr/local/bin/apod-notify", "ro"], sess["mounts"])
        self.assertIn("  notify: send", sess["context"])
        e = dict(self.env, APOD_BROKER_SOCK=str(Path(sess["dir"]) / "broker.sock"))
        client = [sys.executable, HERE / "extensions/apod-notify"]
        r = subprocess.run([*client, "build", "done"], env=e, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.tmp / "notified").read_text(), "apod test|build done|")
        # notify:urgent isn't in the persona: it waits, then times out.
        r = subprocess.run([*client, "--urgent", "help"], env=e, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not allowed for persona dev-notify: notify:urgent", r.stderr)
        # The broken extension was skipped, with a warning in the broker's log.
        self.assertIn("broken.py not loaded", (self.tmp / "broker.log").read_text())

    def test_links_in_text_are_not_targets(self):
        body = "see https://github.com/other/research and https://github.com/orgs/other/projects/3"
        out = self.ran("issue", "create", "--title", "https://github.com/other/x", "--body", body)
        self.assertEqual(out["argv"][:2], ["issue", "create"])
        r = self.gh("pr", "view", "https://github.com/evil/repo/pull/1", expect=1)  # a positional URL is a target
        self.assertIn("evil/repo", r.stderr)

    def test_hard_deny(self):
        r = self.gh("api", "user", expect=1)  # not in persona: asks
        self.assertIn("api GET /user", r.stderr)
        r = self.gh("secret", "list", expect=1)
        self.assertIn("not allowed for persona dev: gh:secret list", r.stderr)
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

    def test_api_rest(self):
        self.ran("api", "repos/{owner}/{repo}/pulls")
        self.ran("api", "/repos/acme/widget/issues/1/comments", "-f", "body=x")
        r = self.gh("api", "-X", "DELETE", "repos/acme/widget/git/refs/heads/x", expect=1)
        self.assertIn("api DELETE /repos/{repo}/git/refs/heads/x", r.stderr)
        r = self.gh("api", "repos/evil/repo/pulls", expect=1)
        self.assertIn("out of this session's scope: evil/repo", r.stderr)
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

    def test_projects_pm(self):
        pm = self.register("pm", ["acme/widget", "project:acme/5"])
        self.ran("project", "item-list", "5", "--owner", "acme", sess=pm)
        r = self.gh("project", "item-edit", "--project-id", "PVT_kwDOBBB222", "--id", "PVTI_lADOAAA444", sess=pm, expect=1)
        self.assertIn("project evil/9", r.stderr)
        self.ran("project", "item-edit", "--project-id", "PVT_kwDOAAA111", "--id", "PVTI_lADOAAA444", "--text", "t", sess=pm)
        r = self.gh("project", "item-edit", "--project-id", "PVT_kwDOAAA111", "--id", "junk", sess=pm, expect=1)
        self.assertIn("unscoped: node junk", r.stderr)
        r = self.gh("project", "item-list", "6", "--owner", "acme", sess=pm, expect=1)
        self.assertIn("project acme/6", r.stderr)
        r = self.gh("pr", "create", "--title", "x", "--body", "y", sess=pm, expect=1)
        self.assertIn("not allowed for persona pm", r.stderr)

    def test_fork_adds_scope(self):
        m = self.register("maintainer", ["acme/widget"], user="octo")
        out = self.ran("repo", "fork", sess=m)
        self.assertEqual(out["argv"], ["repo", "fork", "--clone=false", "--remote=false", "acme/widget"])
        self.assertEqual(out["GH_TOKEN"], "tok-octo")
        self.ran("pr", "list", "-R", "octo/widget", sess=m)

    def test_auth_local(self):
        r = self.gh("auth", "status")
        self.assertIn("persona dev", r.stdout)
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
        self.assertTrue(any(a == "Basic eC1hY2Nlc3MtdG9rZW46dG9rLWFjdGl2ZQ==" for _, _, a in CGIUpstream.seen))
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

    def test_sign_commits(self):
        sess = self.register("dev", [], toml=f'[sign]\nkey = "{self.tmp}/signkey"\n')
        self.assertIn("sign", sess["providers"])
        repo = self.tmp / "signed"
        e = self.pod_env(sess)
        subprocess.run(["git", "init", "-q", repo], check=True, env=e)
        r = subprocess.run(["git", "-C", repo, "commit", "-q", "--allow-empty", "-m", "signed"], env=e,
                           capture_output=True, text=True)
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
        # Signing as someone else: out of scope, waits, fails.
        r = subprocess.run(["git", "-C", repo, "commit", "-q", "--allow-empty", "-m", "other",
                            "--author", "Mallory <m@evil.example>"], env=e, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("as m@evil.example", self.admin("log", "-n", "3").stdout)

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

    def test_ssh_agent_signs_only_for_hosts_in_scope(self):
        sess = self.register("ops", [], toml=f'[ssh]\nkey = "{self.tmp}/userkey"\nhosts = ["fatso"]\n')
        sock = Path(sess["dir"]) / "agent.sock"
        user = blob_of(self.tmp / "userkey.pub")
        a = Agent(sock)
        self.assertEqual(a.identities(), [user])  # the broker's other keys (signing) aren't offered
        sid = os.urandom(32)
        self.assertEqual(self.bind(a, "hostkey", sid), 6)
        self.assertIsNotNone(a.sign(user, self.login_data(sid, "me", user)))
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
        sess = self.register("ops", [], toml=f'[ssh]\nkey = "{self.tmp}/userkey"\nhosts = ["fatso"]\n')
        e = self.pod_env(sess)
        r = subprocess.run([str(self.shim_bin / "apod-ssh-connect"), "fatso", "22"], input=b"hello", env=e,
                           capture_output=True, timeout=20)
        self.assertEqual(r.stdout, b"hello", r.stderr)
        r = subprocess.run([str(self.shim_bin / "apod-ssh-connect"), "evil.example", "22"], input=b"x", env=e,
                           capture_output=True, timeout=20)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(b"rejected", r.stderr)
        known = (Path(sess["dir"]) / "known_hosts").read_text()
        self.assertIn("fatso ssh-ed25519", known)
        self.assertIn("ProxyCommand", (Path(sess["dir"]) / "ssh_config").read_text())

    # --- rad -------------------------------------------------------------------------------

    def test_rad_fetch_and_push(self):
        toml = f'[rad]\nnid = "{NID}"\nstorage = "{self.tmp}/radstore"\nhome = "{self.tmp}/radhome"\n'
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
        # Rewriting topic: a force push, which dev doesn't have.
        subprocess.run(["git", "-C", d, "commit", "-q", "--amend", "--allow-empty", "-m", "rewritten"], env=e, check=True)
        r = subprocess.run(["git", "-C", d, "push", "-f", "origin", "HEAD:refs/heads/topic"], env=e, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("rad:push force", r.stderr)
        self.assertEqual(onward("refs/heads/topic"), head)

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
                              env=dict(self.env, TERM="xterm-256color"), start_new_session=True)
        os.close(slave)
        screen = b""

        def read_until(text, timeout=5):
            nonlocal screen
            end = time.time() + timeout
            while time.time() < end:
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
        time.sleep(0.5)
        os.write(master, b"\x1b[<0;3;30M\x1b[<0;3;30m")
        o, e = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 0, e)
        os.write(master, b"\x1b[<0;16;2M\x1b[<0;16;2m")  # the Sessions tab
        self.assertTrue(read_until(b"acme/widget"))
        os.write(master, b"q")
        ui.wait(timeout=5)
        os.close(master)
        self.assertEqual(ui.returncode, 0)

if __name__ == "__main__":
    unittest.main(verbosity=2)
