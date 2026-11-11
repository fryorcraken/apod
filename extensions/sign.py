"""sign provider: git in the pod signs commits and tags with an SSH key it never sees. git calls
gpg.ssh.program (the pod's apod-sign) with the object to sign; the broker checks it and signs it
with the key in the broker's agent.

The profile holds the key ([sign] key) and the identity (its git author); the role holds the
actions; the scope says where: the repository the commit is for.

Actions: sign:commit, sign:tag; sign:other (anything that isn't a commit or tag object); sign:as
other (a commit or tag whose author, committer or tagger isn't the profile's git identity).
Targets: the repository, by the remote git would push the branch to (gh:OWNER/REPO, rad:RID), or
git-local:PATH for a repository with no such remote. The pod reports both, so they keep honest
agents to the scope rather than stop a hostile one: the push itself is checked separately."""
import base64
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from api import (AGENT, BDIR, POD_DIR, SHIM, BrokerError, Provider, Request, allowed, check, need_key, provider,
                 read_body, rejected_msg, remote_target, scope_kind)


def classify(payload, namespace, sess=None, remote="", toplevel=""):
    text = payload.decode("utf-8", "replace")
    head, _, msg = text.partition("\n\n")
    subject = msg.strip().splitlines()[0][:72] if msg.strip() else ""
    if namespace != "git":
        kind = "other"
    elif text.startswith("tree "):
        kind = "commit"
    elif text.startswith("object "):
        kind = "tag"
    else:
        kind = "other"
    req = Request("sign", f"sign {kind}: {subject}" if subject else f"sign {kind}")
    req.actions = [f"sign:{kind}"]
    fields = ("author", "committer") if kind == "commit" else ("tagger",) if kind == "tag" else ()
    mine = {e.lower() for e in (sess.identity.get("emails", []) if sess else [])}
    others = []
    for line in head.splitlines():
        k, _, v = line.partition(" ")
        if k in fields:
            m = re.search(r"<([^>]*)>", v)
            email = (m.group(1) if m else v).lower()
            if email not in mine and email not in others:
                others.append(email)
    if others:
        req.actions.append("sign:as other")
        req.display += f" (as {', '.join(others)})"
    # Where: the repository, by its push remote; a repository without one, by its path.
    t = remote_target(remote) if remote else None
    if t:
        req.targets.append(t)
    elif toplevel:
        req.targets.append(("git-local", toplevel))
    else:
        req.targets.append(("unscoped", "signing outside a git repository"))
    return req


def handle(h, sess, method):
    try:
        r = json.loads(read_body(h))
        payload = base64.b64decode(r["data"])
        namespace = r.get("namespace", "git")
        pubkey = r.get("pubkey", "").split()
        remote, toplevel = str(r.get("remote", ""))[:500], str(r.get("toplevel", ""))[:500]
    except (ValueError, KeyError):
        return h.send_json({"error": "bad request"}, 400)
    cfg = sess.cfg.get("sign")
    if not cfg:
        return h.send_json({"error": "signing isn't set up for this pod"}, 400)
    if len(pubkey) < 2 or pubkey[1] != cfg["blob"]:
        return h.send_json({"error": "not this pod's signing key (use the user.signingkey apod sets)"}, 400)
    req = classify(payload, namespace, sess, remote, toplevel)
    ok, ev = check(sess, req)
    if not ok:
        return h.send_json({"error": rejected_msg(req, ev)}, 403)
    (BDIR / "tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=BDIR / "tmp") as d:
        data, pub = Path(d) / "data", Path(d) / "key.pub"
        data.write_bytes(payload)
        pub.write_text(cfg["pub"] + "\n")
        p = subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-n", namespace, "-U", "-f", str(pub), str(data)],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           env=dict(os.environ, SSH_AUTH_SOCK=str(AGENT)), timeout=120)
        sig = Path(str(data) + ".sig")
        if p.returncode or not sig.exists():
            ev["exit"] = p.returncode
            return h.send_json({"error": f"signing failed on the host: {p.stderr.strip()} "
                                         f"(is the key loaded? apod broker keys)"}, 500)
        ev["exit"] = 0
        return h.send_json({"signature": sig.read_text()})


class SignProvider(Provider):
    name = "sign"
    routes = [("POST", "/sign", handle)]

    def keys(self, cfg):
        return [(cfg["key"], cfg.get("confirm", False))] if cfg.get("key") else []

    def configure(self, sess, cfg, reg, out):
        """[sign] key: git in the pod can sign. Commits and tags are signed by default
        (commit.gpgsign, tag.gpgsign) when the role allows sign:commit / sign:tag; otherwise
        git commit -S still works, and waits for approval."""
        if not cfg.get("key"):
            return False
        signs = {a: allowed(sess, f"sign:{a}") for a in ("commit", "tag")}
        # The project folder is where a repository without a GitHub or Radicle remote may be signed.
        if sess.project:
            sess.add_scope(("git-local", sess.project))
            sess.add_scope(("git-local", sess.project.rstrip("/") + "/*"))
        try:
            line, blob = need_key(sess, out, cfg["key"], cfg.get("confirm", False), "signing commits")
        except BrokerError as e:
            out["context"].append(f"Commit signing is unavailable: {e}.")
            return False
        sess.cfg["sign"] = {"pub": line, "blob": blob}
        out["mounts"].append([str(SHIM), "/usr/local/bin/apod-sign", "ro"])
        out["git_config"] += [["gpg.format", "ssh"], ["gpg.ssh.program", "/usr/local/bin/apod-sign"],
                              ["user.signingkey", f"key::{line}"]]
        # So git log --show-signature and verify-commit in the pod accept the profile's own signatures.
        emails = sess.identity.get("emails") or ["*"]
        sess.dir.mkdir(parents=True, exist_ok=True)
        (sess.dir / "allowed_signers").write_text("".join(f'{e} namespaces="git" {line}\n' for e in emails))
        out["git_config"].append(["gpg.ssh.allowedSignersFile", f"{POD_DIR}/allowed_signers"])
        if cfg.get("auto", True):
            out["git_config"] += [[f"{a}.gpgsign", "true"] for a in ("commit", "tag") if signs[a]]
        on = " and ".join(f"{a}s" for a in ("commit", "tag") if signs[a])
        out["context"].append(
            (f"git {on} are signed by the broker" if on else f"Role {sess.role} doesn't sign: git commit -S asks the "
             f"user, through the broker") +
            " with an SSH key held on the host (gpg.format=ssh), in the repositories in scope (by the remote the "
            "branch pushes to) and as the profile's git identity; anything else needs approval.")
        return True


provider(SignProvider())
scope_kind("git-local", provider="sign",
           help="git-local:PATH (sign in a repository with no GitHub or Radicle remote; the project folder is in "
                "scope)")
