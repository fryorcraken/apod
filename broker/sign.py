"""sign provider: git in the pod signs commits and tags with an SSH key it never sees. git calls
gpg.ssh.program (the pod's apod-sign) with the object to sign; the broker checks it (a commit or a
tag, by the profile's git identity) and signs it with the key in the broker's agent.

Actions: sign:commit, sign:tag, sign:other (anything that isn't a commit or tag object).
Targets: the author, committer or tagger email (scope: the profile's git identity)."""
import base64
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from core import AGENT, BDIR, SHIM, BrokerError, Request, check, need_key, provider, read_body, rejected_msg


def classify(payload, namespace):
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
    for line in head.splitlines():
        k, _, v = line.partition(" ")
        if k in fields:
            m = re.search(r"<([^>]*)>", v)
            req.targets.append(("identity", (m.group(1) if m else v).lower()))
    req.targets = list(dict.fromkeys(req.targets))
    return req


def handle(h, sess, method):
    try:
        r = json.loads(read_body(h))
        payload = base64.b64decode(r["data"])
        namespace = r.get("namespace", "git")
        pubkey = r.get("pubkey", "").split()
    except (ValueError, KeyError):
        return h.send_json({"error": "bad request"}, 400)
    cfg = sess.cfg.get("sign")
    if not cfg:
        return h.send_json({"error": "signing isn't set up for this pod"}, 400)
    if len(pubkey) < 2 or pubkey[1] != cfg["blob"]:
        return h.send_json({"error": "not this pod's signing key (use the user.signingkey apod sets)"}, 400)
    req = classify(payload, namespace)
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


class SignProvider:
    name = "sign"
    routes = [("POST", "/sign", handle)]

    def configure(self, sess, cfg, reg, out):
        if not cfg.get("key"):
            return False
        try:
            line, blob = need_key(sess, out, cfg["key"], cfg.get("confirm", False))
        except BrokerError as e:
            out["context"].append(f"Commit signing is unavailable: {e}.")
            return False
        sess.cfg["sign"] = {"pub": line, "blob": blob}
        out["mounts"].append([str(SHIM), "/usr/local/bin/apod-sign", "ro"])
        out["git_config"] += [["gpg.format", "ssh"], ["gpg.ssh.program", "/usr/local/bin/apod-sign"],
                              ["user.signingkey", f"key::{line}"]]
        if cfg.get("auto", True):
            out["git_config"] += [["commit.gpgsign", "true"], ["tag.gpgsign", "true"]]
        out["context"].append("git commits and tags are signed by the broker with an SSH key held on the host "
                              "(gpg.format=ssh); signing as anyone but the profile's git identity needs approval.")
        return True


provider(SignProvider())
