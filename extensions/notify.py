"""Example apod broker extension: the agent sends you a desktop notification on the host.

In the pod: apod-notify [--urgent] TEXT. The broker checks the request (action notify:send or
notify:urgent; no target, so the scope doesn't come into it) against the role, then runs
notify-send on the host, titled with the agent session's name.

To use it:

    mkdir -p ~/.agent-pod/broker-extensions
    ln -s ~/src/rad/apod/extensions/notify.py ~/.agent-pod/broker-extensions/
    apod broker restart

    # ~/.agent-pod/<profile>/broker.toml
    [notify]
    enabled = true
    # command = ["notify-send", "-a", "apod"]    # the default; called with TITLE TEXT
    # urgent = ["-u", "critical"]                 # added to the command for --urgent

    # ~/.agent-pod/roles.toml: let dev pods send notifications without asking (this adds to
    # the built-in dev role)
    [roles.dev]
    notify = ["send"]

Without a role that allows it, each notification waits for your approval like any other
request (approve "for the role" once to stop being asked). See README.md, "Writing a broker
extension", for the API this uses.
"""
import json
import shutil
import subprocess
from pathlib import Path

from core import Request, check, provider, read_body, rejected_msg

CLIENT = Path(__file__).resolve().parent / "apod-notify"


def handle(h, sess, method):
    # 1. Read and validate what the pod sent. Everything from the pod is untrusted.
    try:
        body = json.loads(read_body(h))
        text = str(body["text"]).strip()[:500]
        urgent = bool(body.get("urgent", False))
    except (ValueError, KeyError, TypeError):
        return h.send_json({"error": "bad request: expected {\"text\": ..., \"urgent\": false}"}, 400)
    if not text:
        return h.send_json({"error": "empty notification"}, 400)

    # 2. Classify it: what it does (actions) and what it touches (targets; none here).
    req = Request("notify", f"notify{' (urgent)' if urgent else ''}: {text[:60]}")
    req.actions = ["notify:urgent" if urgent else "notify:send"]

    # 3. Check it: allowed by the role, or approved by the user while it waits.
    ok, ev = check(sess, req)
    if not ok:
        return h.send_json({"error": rejected_msg(req, ev)}, 403)

    # 4. Act on the host, with a fixed command and no shell: the text is an argument.
    cmd = sess.cfg["notify"]["command"] + (sess.cfg["notify"]["urgent"] if urgent else [])
    r = subprocess.run([*cmd, sess.title() or f"apod {sess.profile}", text], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=30)
    ev["exit"] = r.returncode
    if r.returncode:
        return h.send_json({"error": f"{cmd[0]} failed on the host: {r.stderr.strip()}"}, 500)
    return h.send_json({"ok": True})


class NotifyProvider:
    name = "notify"  # the [notify] table in broker.toml, the notify list in roles, notify:* actions
    routes = [("POST", "/notify", handle)]

    def configure(self, sess, cfg, reg, out):
        """Called when a pod registers, with broker.toml's [notify] table. Returns whether the
        provider is active for this pod."""
        if not cfg.get("enabled"):
            return False
        cmd = list(cfg.get("command") or ["notify-send", "-a", "apod"])
        if not shutil.which(cmd[0]):
            out["context"].append(f"apod-notify is unavailable: {cmd[0]} isn't installed on the host.")
            return False
        # sess.cfg is saved with the session, so this survives a broker restart.
        sess.cfg["notify"] = {"command": cmd, "urgent": list(cfg.get("urgent", ["-u", "critical"]))}
        out["mounts"].append([str(CLIENT), "/usr/local/bin/apod-notify", "ro"])
        out["context"].append("apod-notify [--urgent] TEXT sends the user a desktop notification on the host: "
                              "use it when you finish a long task or need their input.")
        return True


provider(NotifyProvider())
