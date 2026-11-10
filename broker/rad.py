"""rad provider: the pod's rad:// remotes point at the broker (through apod-git-forward).

Fetches are served from the host's Radicle storage (read-only, git http-backend). A push goes
into a staging repository the broker owns (so nothing from the pod runs on the host), its ref
updates are checked, and the broker then pushes them on with the host's git-remote-rad as the
host's Radicle identity (its key loaded in the broker's agent). Push options (-o patch.message=...)
are passed on.

Actions: rad:fetch, rad:push branch|default-branch|tag|delete|patch|other-ref, rad:push force
(a branch update that isn't a fast-forward). Targets: ("rad", RID)."""
import os
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import gitwire
from core import (AGENT, BDIR, EVENTS, BrokerError, Request, check, evaluate, expand, need_key, now, provider,
                  read_body, rejected_msg, run_quiet, send_text)

RAD_URL = re.compile(r"^rad://(z[1-9A-HJ-NP-Za-km-z]+)(?:/(z[1-9A-HJ-NP-Za-km-z]+))?/?$")


def git(*args, cwd=None, env=None):
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", **extra)
    return env


def backend(h, root, path_info, body, extra_env=None):
    """Answer with git http-backend (CGI) over ROOT."""
    u = urllib.parse.urlparse(h.path)
    env = clean_env(GIT_PROJECT_ROOT=str(root), GIT_HTTP_EXPORT_ALL="1", PATH_INFO=path_info,
                    QUERY_STRING=u.query, REQUEST_METHOD=h.command, REMOTE_USER="apod",
                    CONTENT_TYPE=h.headers.get("Content-Type", ""), CONTENT_LENGTH=str(len(body or b"")),
                    HTTP_CONTENT_ENCODING=h.headers.get("Content-Encoding", ""),
                    GIT_PROTOCOL=h.headers.get("Git-Protocol", ""), **(extra_env or {}))
    p = subprocess.run(["git", "http-backend"], input=body or b"", env=env, capture_output=True)
    head, _, out = p.stdout.partition(b"\r\n\r\n")
    status, headers = 200, []
    for line in head.decode("latin-1").split("\r\n"):
        k, _, v = line.partition(": ")
        if k.lower() == "status":
            status = int(v.split()[0])
        elif k:
            headers.append((k, v))
    if not head:
        return send_text(h, f"git http-backend failed: {p.stderr.decode(errors='replace').strip()}", 500)
    return status, headers, out


def send_backend(h, res):
    if not isinstance(res, tuple):
        return
    status, headers, out = res
    h.send_response(status)
    for k, v in headers:
        if k.lower() not in ("connection", "content-length"):
            h.send_header(k, v)
    h.send_header("Content-Length", str(len(out)))
    h.send_header("Connection", "close")
    h.end_headers()
    h.wfile.write(out)


def staging(sess, rid):
    """The session's staging repository for RID, its branches and tags synced from the host's
    namespace in storage (and the canonical refs, as bases for the pod's packs)."""
    cfg = sess.cfg["rad"]
    root = BDIR / "staging" / sess.id
    repo = root / f"{rid}.git"
    if not repo.exists():
        root.mkdir(parents=True, exist_ok=True)
        git("init", "-q", "--bare", str(repo), env=clean_env())
        git("config", "receive.advertisePushOptions", "true", cwd=repo, env=clean_env())
        git("config", "core.hooksPath", os.devnull, cwd=repo, env=clean_env())
    store = Path(cfg["storage"]) / rid
    nid = cfg["nid"]
    git("fetch", "-q", "--prune", "--no-tags", str(store),
        f"+refs/namespaces/{nid}/refs/heads/*:refs/heads/*", f"+refs/namespaces/{nid}/refs/tags/*:refs/tags/*",
        "+refs/heads/*:refs/canonical/*", cwd=repo, env=clean_env())
    return root, repo


def default_branch(sess, rid):
    r = git("symbolic-ref", "--short", "HEAD", cwd=Path(sess.cfg["rad"]["storage"]) / rid, env=clean_env())
    return r.stdout.strip() or None


def handle(h, sess, method):
    u = urllib.parse.urlparse(h.path)
    m = re.match(r"^/git/rad/(z\w+)(?:/(z\w+))?/(info/refs|git-upload-pack|git-receive-pack)$", u.path)
    if not m or "rad" not in sess.cfg:
        return send_text(h, f"not a Radicle request the broker handles: {u.path}", 404)
    rid, nid, endpoint = m.groups()
    service = urllib.parse.parse_qs(u.query).get("service", [""])[0] if endpoint == "info/refs" else endpoint
    if service not in ("git-upload-pack", "git-receive-pack") or (endpoint == "info/refs") != (method == "GET"):
        return send_text(h, "unsupported git request", 400)
    body = read_body(h) if method == "POST" else None
    cfg = sess.cfg["rad"]
    push = service == "git-receive-pack"
    req = Request("rad", f"git {'push' if push else 'fetch'} rad:{rid}" + (f"/{nid}" if nid else ""))
    req.targets = [("rad", rid)]
    if not push:
        req.actions = ["rad:fetch"]
        if method == "GET":
            ok, ev = check(sess, req)
            if not ok:
                return send_text(h, rejected_msg(req, ev))
        elif evaluate(sess, req)[0] != "allow" and not any(
                e["session"] == sess.id and e["request"] == req.display and now() - e["ts"] < 600
                and e["decision"].startswith(("allowed", "approved")) for e in list(EVENTS)):
            return send_text(h, f"fetch of rad:{rid} not allowed")
        hide = ["refs/rad", "refs/cobs"] + ([] if nid else ["refs/namespaces"])
        extra = {"GIT_CONFIG_COUNT": str(len(hide))}
        for i, r in enumerate(hide):
            extra[f"GIT_CONFIG_KEY_{i}"], extra[f"GIT_CONFIG_VALUE_{i}"] = "uploadpack.hideRefs", r
        if nid:
            extra["GIT_NAMESPACE"] = nid
        return send_backend(h, backend(h, cfg["storage"], f"/{rid}/{endpoint}", body, extra))
    if nid and nid != cfg["nid"]:
        return send_text(h, f"can't push to another peer's namespace ({nid}); push to rad://{rid}")
    if method == "GET":
        if not sess.in_scope(("rad", rid)):
            req.display += " (before a push)"
            ok, ev = check(sess, req, extra={"once_window": 120})
            if not ok:
                return send_text(h, rejected_msg(req, ev))
        root, _ = staging(sess, rid)
        return send_backend(h, backend(h, root, f"/{rid}.git/info/refs", None))
    return handle_push(h, sess, req, rid, body)


def kind(old, new, ref, default):
    if ref == "refs/patches" or ref.startswith(("refs/patches/", "refs/heads/patches/")):
        return "push patch"
    return gitwire.ref_kind(old, new, ref, default)


def handle_push(h, sess, req, rid, body):
    cfg = sess.cfg["rad"]
    try:
        cmds, caps, opts, offset = gitwire.parse_push(body)
    except (BrokerError, ValueError) as e:
        return send_text(h, f"couldn't read the push: {e}")
    default = default_branch(sess, rid)
    req.actions = list(dict.fromkeys("rad:" + kind(o, n, r, default) for o, n, r in cmds)) or ["rad:push"]
    req.display = f"git push rad:{rid} " + " ".join(r for _, _, r in cmds) + "".join(f" -o {o}" for o in opts)
    reply = gitwire.PushReply(h, caps)

    def refuse(msg):
        if not reply.band and not reply.started:
            return send_text(h, msg)
        reply.message(msg)
        reply.report([(r, msg) for _, _, r in cmds])

    ok, ev = check(sess, req, reply.waiting)
    if not ok:
        return refuse(f"push rejected ({ev['decision']}): {ev['reason']}")
    # Into staging first: the pack lands in a repository the broker owns. receive-pack refuses
    # refs/patches (one level under refs/), which git-remote-rad takes, so it goes in as
    # refs/apod/patches.
    root, repo = staging(sess, rid)
    local = {r: ("refs/apod/patches" if r == "refs/patches" else r) for _, _, r in cmds}
    body = gitwire.encode_push([(o, n, local[r]) for o, n, r in cmds], caps, opts, body, offset)
    res = backend(h, root, f"/{rid}.git/git-receive-pack", body)
    for old, new, ref in cmds:
        if new != gitwire.ZERO and git("rev-parse", "--verify", "-q", local[ref], cwd=repo,
                                       env=clean_env()).stdout.strip() != new:
            return refuse(f"the push didn't arrive in the broker's staging repository ({ref})"
                          + (f": {res[2][-300:]!r}" if isinstance(res, tuple) else ""))
    force = [r for o, n, r in cmds if o != gitwire.ZERO and n != gitwire.ZERO and r.startswith("refs/heads/")
             and git("merge-base", "--is-ancestor", o, n, cwd=repo, env=clean_env()).returncode != 0]
    if force:
        freq = Request("rad", f"git push --force rad:{rid} " + " ".join(force))
        freq.actions, freq.targets = ["rad:push force"], [("rad", rid)]
        ok, ev = check(sess, freq, reply.waiting)
        if not ok:
            return refuse(f"force push rejected ({ev['decision']}): {ev['reason']}")
    # Then on to Radicle, as the host's identity.
    specs = [f"{'+' if r in force else ''}{n}:{r}" if n != gitwire.ZERO else f":{r}" for o, n, r in cmds]
    env = clean_env(RAD_HOME=cfg["home"], SSH_AUTH_SOCK=str(AGENT))
    p = subprocess.run(["git", "push", "--porcelain", *[a for o in opts for a in ("-o", o)],
                        f"rad://{rid}/{cfg['nid']}", *specs], cwd=repo, env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=600)
    for line in p.stderr.splitlines():
        if line.strip():
            reply.message(f"rad: {line}")
    results, seen = [], {}
    for line in p.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and ":" in parts[1]:
            seen[parts[1].split(":", 1)[1]] = None if parts[0].strip() != "!" else parts[2]
    for _, _, r in cmds:
        err = seen.get(r, None if p.returncode == 0 else "push to Radicle failed")
        results.append((r, err))
    ev["exit"] = p.returncode
    for _, _, r in cmds:
        if r == "refs/patches" or r.startswith("refs/patches/"):
            git("update-ref", "-d", local[r], cwd=repo, env=clean_env())
    reply.report(results)


class RadProvider:
    name = "rad"
    routes = [("GET", "/git/rad/", handle), ("POST", "/git/rad/", handle)]

    def keys(self, cfg):
        """The key pushes are signed with: [rad] key, else the identity's key in RAD_HOME."""
        home = expand(cfg.get("home") or os.environ.get("RAD_HOME") or "~/.radicle")
        key = cfg.get("key") or (str(home / "keys/radicle") if (home / "keys/radicle.pub").exists() else None)
        return [(key, cfg.get("confirm", False))] if key else []

    def configure(self, sess, cfg, reg, out):
        rids = [m.group(1) for m in (RAD_URL.match(u) for u in reg.get("remotes", [])) if m]
        if not rids and not cfg:
            return False
        home = str(expand(cfg.get("home") or os.environ.get("RAD_HOME") or "~/.radicle"))
        nid = cfg.get("nid")
        if not nid:
            if not shutil.which("rad"):
                out["context"].append("Radicle remotes are unavailable: rad isn't installed on the host.")
                return False
            nid = run_quiet(["rad", "self", "--nid"], env=dict(os.environ, RAD_HOME=home)).stdout.strip()
            if not nid:
                out["context"].append(f"Radicle remotes are unavailable: no Radicle identity in {home}.")
                return False
        storage = str(expand(cfg.get("storage") or Path(home) / "storage"))
        sess.cfg["rad"] = {"home": home, "nid": nid, "storage": storage}
        for rid in rids:
            sess.add_scope(("rad", rid))
        key = cfg.get("key") or (str(Path(home) / "keys/radicle") if (Path(home) / "keys/radicle.pub").exists() else None)
        if key:
            try:
                need_key(sess, out, key, cfg.get("confirm", False))
            except BrokerError as e:
                out["context"].append(f"Radicle pushes may fail: {e}.")
        out["git_config"].append(["url.{forward}rad/.insteadOf", "rad://"])
        out["context"].append(f"git fetch and push to rad:// remotes go through the broker: pushes land as the host's "
                              f"Radicle identity ({nid}); HEAD:refs/patches opens a patch (-o patch.message=...).")
        return True


provider(RadProvider())
