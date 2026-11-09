#!/usr/bin/env python3
"""End-to-end tests for apod-broker and gh-shim, without GitHub: a fake gh (real gh for --help,
canned answers otherwise) and a local git upstream (git http-backend). Run: python3 tests/test_broker.py
"""
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
SHIM = HERE / "gh-shim"
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
        os.symlink(SHIM, bin_ / "apod-git-forward")
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
                       APOD_GH_ASK_TIMEOUT="4",
                       APOD_GH_GIT_UPSTREAM=f"http://127.0.0.1:{cls.upstream.server_address[1]}")
        cls.env.pop("GH_REPO", None)
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
    def register(cls, persona, scope, user=""):
        args = ["register", "--profile", "test", "--persona", persona, "--project", "/p"]
        if user:
            args += ["--user", user]
        for s in scope:
            args += ["--scope", s]
        r = subprocess.run([sys.executable, BROKER, *args], env=cls.env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    def gh(self, *args, sess=None, cwd=None, input=None, expect=0, env=None):
        e = dict(self.env, APOD_GH_SOCK=str(Path((sess or self.sess)["dir"]) / "gh.sock"), **(env or {}))
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

    def test_hard_deny(self):
        r = self.gh("api", "user", expect=1)  # not in persona: asks
        self.assertIn("api GET /user", r.stderr)
        r = self.gh("secret", "list", expect=1)
        self.assertIn("not allowed for persona dev: secret list", r.stderr)
        r = self.gh("extension", "list", expect=1)
        self.assertIn("never allowed", r.stderr)

    def test_approve_once_and_for_session(self):
        p = subprocess.Popen([sys.executable, SHIM, "pr", "merge", "7", "--squash"], cwd=self.work,
                             env=dict(self.env, APOD_GH_SOCK=str(Path(self.sess["dir"]) / "gh.sock")),
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
        eid = r.stderr.split("apod gh allow ")[1].split()[0]
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
        # What apod's wrapper does in the pod: start the forwarder, point github.com at it.
        e = dict(self.env, APOD_GH_SOCK=str(Path(sess["dir"]) / "gh.sock"), GIT_TERMINAL_PROMPT="0",
                 GIT_CONFIG_NOSYSTEM="1", HOME=str(self.tmp))
        url = subprocess.run([str(self.shim_bin / "apod-git-forward")], env=e, capture_output=True,
                             text=True, check=True).stdout.strip()
        e.update(GIT_CONFIG_COUNT="3", GIT_CONFIG_KEY_0=f"url.{url}.insteadOf", GIT_CONFIG_VALUE_0="https://github.com/",
                 GIT_CONFIG_KEY_1=f"url.{url}.insteadOf", GIT_CONFIG_VALUE_1="git@github.com:",
                 GIT_CONFIG_KEY_2="user.name", GIT_CONFIG_VALUE_2="t")
        return e

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
        self.assertIn("git push default-branch", r.stderr)
        r = subprocess.run(["git", "-C", d, "push", "origin", ":topic"], env=e, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("git push delete", r.stderr)
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

    def test_ui_requires_token(self):
        import urllib.request
        import urllib.error
        url = self.admin("ui").stdout.strip()
        base = url.split("/?")[0]
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/api/state")
        st = json.loads(urllib.request.urlopen(urllib.request.Request(
            base + "/api/state", headers={"X-Apod-Token": url.split("t=")[1]})).read())
        self.assertTrue(st["sessions"])
        self.assertIn("apod gh broker", urllib.request.urlopen(url).read().decode())


if __name__ == "__main__":
    unittest.main(verbosity=2)
