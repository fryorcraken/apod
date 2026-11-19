"""github provider: the pod's github.com remotes point at the broker (through apod-git-forward
in the pod), which checks each fetch and each pushed ref and forwards the request to GitHub with
the host gh account's token. Actions: github:fetch, github:fetch public (a public repository
outside the scope), github:push branch|default-branch|tag|delete|other-ref, and github:push force
for a branch update that isn't a fast-forward (with or without a lease: the broker can't tell).
A request for a repository outside the scope that GitHub answers 404 for (no such repository, or
the account can't see it) is refused at once: approving it would only lead to a failed fetch."""
import base64
import http.client
import json
import os
import re
import urllib.parse
import zlib
from pathlib import Path

import gitwire
from api import (GIT_BODY_LIMIT, BrokerError, Provider, Request, check, evaluate, now, provider, read_body,
                 recently_allowed, rejected_msg, send_text)
from gh import gh_query, gh_run, gh_user, token_for

UPSTREAM = os.environ.get("APOD_GH_GIT_UPSTREAM", "https://github.com")
_repo_info = {}


def repo_info(sess, repo):
    """A repository's default branch and visibility, as the session's account sees it; cached for
    five minutes, so a repository made private or a renamed default branch is seen soon.
    {"missing": True} when GitHub answers 404 (no such repository, or the account can't see it;
    not cached, so one created since is seen at once); {} when GitHub couldn't be asked."""
    key = (gh_user(sess), repo)
    info, ts = _repo_info.get(key, (None, 0))
    if info is None or now() - ts > 300:
        r = gh_run(sess, ["api", f"repos/{repo}", "--jq", "{default_branch, private}"])
        if r is not None and r.returncode != 0 and "(HTTP 404)" in r.stderr:
            return {"missing": True}
        try:
            info = json.loads(r.stdout if r is not None and r.returncode == 0 else "")
        except ValueError:
            return {}
        _repo_info[key] = (info, now())
    return info


def upstream_request(sess, method, path, body, h, gzip_ok=True):
    u = urllib.parse.urlparse(UPSTREAM)
    conn = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(
        u.hostname, u.port, timeout=600)
    names = ("Content-Type", "Accept", "Content-Encoding", "Git-Protocol", "User-Agent") + \
        (("Accept-Encoding",) if gzip_ok else ())
    headers = {k: h.headers[k] for k in names if h.headers.get(k)}
    tok = token_for(gh_user(sess))
    headers["Authorization"] = "Basic " + base64.b64encode(f"x-access-token:{tok}".encode()).decode()
    conn.request(method, path, body=body, headers=headers)
    return conn, conn.getresponse()


def relay_response(h, resp, started=False):
    if not started:
        h.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in ("connection", "transfer-encoding", "keep-alive"):
                h.send_header(k, v)
        h.send_header("Connection", "close")
        h.end_headers()
    for chunk in iter(lambda: resp.read(65536), b""):
        h.wfile.write(chunk)
    h.wfile.flush()


def handle(h, sess, method):
    u = urllib.parse.urlparse(h.path)
    m = re.match(r"^/git/github/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/(info/refs|git-upload-pack|git-receive-pack)$", u.path)
    if not m or m.group(1).strip(".") == "" or m.group(2).strip(".") == "":
        return send_text(h, f"not a git request the broker handles: {u.path}", 404)
    repo = f"{m.group(1)}/{m.group(2)}".lower()
    endpoint = m.group(3)
    service = urllib.parse.parse_qs(u.query).get("service", [""])[0] if endpoint == "info/refs" else endpoint
    if service not in ("git-upload-pack", "git-receive-pack") or (endpoint == "info/refs") != (method == "GET"):
        return send_text(h, "unsupported git request", 400)
    body = read_body(h, GIT_BODY_LIMIT) if method == "POST" else None
    upath = f"/{m.group(1)}/{m.group(2)}.git/{endpoint}" + (f"?{u.query}" if u.query else "")
    push = service == "git-receive-pack"
    req = Request("github", f"git {'push' if push else 'fetch'} github.com/{repo}")
    req.targets = [("gh", repo)]
    if not push:
        req.actions = ["github:fetch"]
    info = {} if sess.in_scope(("gh", repo)) else repo_info(sess, repo)
    if info.get("missing"):
        # GitHub would answer "not found" whatever the user said, so nobody is asked.
        who = gh_user(sess) or "the gh account"
        req.display += f" (not found as {who})"
        check(sess, req, ask=False)
        return send_text(h, f"github.com/{repo}: not found as {who} (GitHub answers 404: no such repository, "
                            "or the account can't see it), so the user wasn't asked. Check the name.", 404)

    def forward():
        conn, resp = upstream_request(sess, method, upath, body, h, gzip_ok=not push)
        relay_response(h, resp)

    if not push:
        if info.get("private") is False:
            req.actions, req.targets = ["github:fetch public"], []
        if method == "POST":
            # Checked (and logged) at the ref advertisement; the negotiation rounds follow it.
            if evaluate(sess, req)[0] != "allow" and not recently_allowed(sess, req):
                return send_text(h, f"fetch of {repo} not allowed")
            return forward()
    elif method == "GET":
        # The ref advertisement before a push: only the repository's scope matters here; the ref
        # updates themselves are checked when they come (POST).
        if sess.in_scope(("gh", repo)):
            return forward()
        req.display += " (before a push)"
    else:
        return handle_push(h, sess, req, repo, upath, body)
    # Approved once, a push's ref advertisement also lets the push itself through.
    ok, ev = check(sess, req, extra={"once_window": 120} if push else None)
    if not ok:
        return send_text(h, rejected_msg(req, ev))
    forward()


def handle_push(h, sess, req, repo, upath, body):
    try:
        data = gitwire.push_data(body, h.headers.get("Content-Encoding", "").lower() == "gzip")
        cmds, caps, _, offset = gitwire.parse_push(data)
    except (BrokerError, ValueError, zlib.error) as e:
        return send_text(h, f"couldn't read the push: {e}")
    default = repo_info(sess, repo).get("default_branch")
    kinds = [gitwire.ref_kind(o, n, r, default) for o, n, r in cmds]
    asked = []

    def is_ancestor(a, b):
        if len(asked) >= 20:
            return False
        asked.append(b)
        return gh_query(sess, ["api", f"repos/{repo}/compare/{a}...{b}", "--jq", ".status"]) in ("ahead\n", "identical\n")

    # A branch update that isn't a fast-forward (git push --force or --force-with-lease) is its own action.
    # The pack is read only when there's such an update to check: it holds every object, inflated.
    updates = [(o, n, r) for o, n, r in cmds if o != gitwire.ZERO and n != gitwire.ZERO and r.startswith("refs/heads/")]
    commits = None
    if updates:
        try:
            commits = gitwire.pack_commits(data[offset:])
        except (IndexError, ValueError, zlib.error):
            pass
    force = [r for o, n, r in updates if not gitwire.fast_forward(o, n, commits, is_ancestor)]
    if force:
        kinds.append("push force")
    req.actions = list(dict.fromkeys("github:" + k for k in kinds)) or ["github:push"]
    req.display = f"git push {'--force ' if force else ''}github.com/{repo} " + " ".join(r for _, _, r in cmds)
    reply = gitwire.PushReply(h, caps)
    ok, ev = check(sess, req, reply.waiting)
    if not ok:
        msg = f"push rejected ({ev['decision']}): {ev['reason']}"
        if not reply.band and not reply.started:
            return send_text(h, msg)
        reply.message(msg)
        return reply.report([(r, msg) for _, _, r in cmds])
    conn, resp = upstream_request(sess, "POST", upath, body, h, gzip_ok=False)
    if reply.started and resp.status != 200:
        h.wfile.write(gitwire.sideband(3, f"GitHub answered {resp.status} {resp.reason}\n".encode()))
        return
    relay_response(h, resp, reply.started)


def ssh_aliases():
    """github-* host aliases in the host's ~/.ssh/config (remotes like github-work:org/repo)."""
    try:
        text = (Path.home() / ".ssh/config").read_text()
    except OSError:
        return []
    return [a for line in text.splitlines() if line.strip().lower().startswith("host ")
            for a in line.split()[1:] if a.startswith("github-")]


class GithubProvider(Provider):
    name = "github"
    description = "git fetch and push to github.com remotes, with the gh account's token"
    guide = "docs/guides.md#github"
    routes = [("GET", "/git/github/", handle), ("POST", "/git/github/", handle)]
    requires = ["gh"]  # the token is the gh account's: no account, no GitHub remotes

    def configure(self, sess, cfg, reg, out):
        prefixes = ["https://github.com/", "git@github.com:", "github.com:", "ssh://git@github.com/"]
        for a in ssh_aliases():
            prefixes += [f"git@{a}:", f"{a}:", f"ssh://git@{a}/"]
        out["git_config"] += [["url.{forward}github/.insteadOf", p] for p in prefixes]
        out["context"].append("git fetch and push to github.com go through the broker (no token in the pod); "
                              "pushes are checked per ref (e.g. branches but not the default branch).")
        return True


provider(GithubProvider())
