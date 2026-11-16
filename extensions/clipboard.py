"""clipboard provider: the pod pastes images from the host's clipboard, never its text.

Claude Code pastes an image (Ctrl+V on Linux) by running `wl-paste -l` to see whether the
clipboard holds one, then `wl-paste --type image/png` to read it. In the pod, wl-paste is
apod-clipboard: it asks the broker, which runs the real wl-paste (Wayland) or xclip (X11) on the
host, with a fixed command, for image types only.

    # a profile's broker.toml
    allow = ["clipboard:image"]     # paste without asking; otherwise each paste waits for you
    [clipboard]
    enabled = true

Action: clipboard:image, for reading the image; no target, so the scope doesn't come into it.
Listing the clipboard's types isn't checked: the pod gets the image types only (whether an image
is there), never the text ones."""
import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

from api import Provider, Request, check, provider, read_body, rejected_msg

CLIENT = Path(__file__).resolve().parent / "apod-clipboard"
TYPES = ("image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp", "image/bmp")
LIMIT = 20 * 1024 * 1024  # bytes of image


def tool():
    """The host's clipboard tool, by the session the broker was started in: wl-paste on Wayland,
    xclip on X11; None without either."""
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        return "wayland"
    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        return "x11"
    return None


def host_run(argv):
    return subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=15)


def host_types():
    t = tool()
    if t == "wayland":
        r = host_run(["wl-paste", "--list-types"])
    elif t == "x11":
        r = host_run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
    else:
        return []
    if r.returncode:
        return []
    have = r.stdout.decode("utf-8", "replace").split()
    return [x for x in TYPES if x in have]


def handle_types(h, sess, method):
    try:
        types = host_types()
    except (OSError, subprocess.TimeoutExpired) as e:
        return h.send_json({"error": f"can't read the host's clipboard: {e}"}, 500)
    return h.send_json({"types": types})


def handle_read(h, sess, method):
    try:
        mime = str(json.loads(read_body(h))["type"])
    except (ValueError, KeyError, TypeError):
        return h.send_json({"error": "bad request: expected {\"type\": \"image/png\"}"}, 400)
    if mime not in TYPES:
        return h.send_json({"error": f"only images come from the host's clipboard ({', '.join(TYPES)})"}, 400)
    req = Request("clipboard", f"paste an image from the clipboard ({mime})")
    req.actions = ["clipboard:image"]
    ok, ev = check(sess, req)
    if not ok:
        return h.send_json({"error": rejected_msg(req, ev)}, 403)
    # Read the types again: what's on the clipboard may have changed while the request waited.
    try:
        if mime not in host_types():
            ev["exit"] = 1
            return h.send_json({"error": f"no {mime} on the host's clipboard"}, 404)
        if tool() == "wayland":
            r = host_run(["wl-paste", "--no-newline", "--type", mime])
        else:
            r = host_run(["xclip", "-selection", "clipboard", "-t", mime, "-o"])
    except (OSError, subprocess.TimeoutExpired) as e:
        ev["exit"] = -1
        return h.send_json({"error": f"can't read the host's clipboard: {e}"}, 500)
    ev["exit"] = r.returncode
    if r.returncode or not r.stdout:
        return h.send_json({"error": f"reading the clipboard failed on the host: "
                                     f"{r.stderr.decode('utf-8', 'replace').strip()}"}, 500)
    if len(r.stdout) > LIMIT:
        return h.send_json({"error": f"the image is over {LIMIT // 1024 // 1024} MB"}, 413)
    return h.send_json({"type": mime, "data": base64.b64encode(r.stdout).decode()})


class ClipboardProvider(Provider):
    name = "clipboard"
    description = "paste images from the host's clipboard (Claude Code's Ctrl+V)"
    guide = "docs/guides.md#pasting-images"
    routes = [("GET", "/clipboard/types", handle_types), ("POST", "/clipboard/read", handle_read)]

    def configure(self, sess, cfg, reg, out):
        """[clipboard] enabled = true: wl-paste in the pod reads images from the host's clipboard."""
        if cfg.get("enabled") is not True:
            return False
        if not tool():
            out["warnings"].append(
                "[clipboard] is on, but the broker can't reach a clipboard: it needs WAYLAND_DISPLAY and "
                "wl-paste (wl-clipboard), or DISPLAY and xclip. The broker keeps the environment it was "
                "started in: start it from your desktop session (apod broker restart).")
            return False
        out["mounts"].append([str(CLIENT), "/usr/local/bin/wl-paste", "ro"])
        out["context"].append("Images the user pastes (Ctrl+V) come from the host's clipboard through the broker; "
                              "text doesn't (wl-paste in the pod reads images only).")
        return True


provider(ClipboardProvider())
