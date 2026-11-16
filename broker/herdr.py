"""herdr: while a pod's request waits for your approval, its herdr pane shows as blocked.

herdr (a terminal multiplexer for coding agents) reads the agent's state from the pane's screen.
A request waiting in the broker looks like "working" there: the agent's tool call just doesn't
return. So the broker tells herdr itself, over herdr's socket API (pane.report_agent), that the
agent needs a decision, and lets go (pane.release_agent) when the last waiting request of that
pane is decided or times out; herdr then reads the screen again.

apod gives the broker the pane when it registers a pod launched from a herdr pane (HERDR_PANE_ID,
HERDR_SOCKET_PATH). Everything here is best effort: a missing or old herdr never holds up, or
fails, a request.
"""
import json
import queue
import re
import socket
import sys
import threading
import time

SOURCE = "apod:broker"  # herdr: a stable id for this integration; "herdr:" is reserved for its own
TIMEOUT = 2.0           # seconds for one call to herdr, in all: connecting, sending and the answer
PANE = re.compile(r"[A-Za-z0-9:._-]{1,80}")
AGENT = re.compile(r"[a-z][a-z0-9_-]{0,31}")

_lock = threading.Lock()
_waiting = {}  # (socket, pane) -> ids of the requests waiting in that pane
_warned = set()
_queue = queue.Queue()  # calls to herdr, in order: a worker sends them, so a slow herdr never holds up a request
_worker = []


def target(pane, sock, agent):
    """What register() keeps for a pod: where to report, or None when what apod passed isn't usable."""
    if not (pane and sock and PANE.fullmatch(pane) and sock.startswith("/") and AGENT.fullmatch(agent or "")):
        return None
    return {"pane": pane, "socket": sock, "agent": agent}


def _call(t, method, **params):
    req = {"id": f"apod-{method}", "method": method, "params": {"pane_id": t["pane"], "source": SOURCE,
                                                                 "agent": t["agent"], **params}}
    key = (t["pane"], method)
    deadline = time.monotonic() + TIMEOUT
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT)
            s.connect(t["socket"])
            s.sendall(json.dumps(req).encode() + b"\n")
            buf = b""
            while b"\n" not in buf and len(buf) < 1 << 20:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("no answer")
                s.settimeout(left)
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        reply = json.loads(buf.split(b"\n")[0] or b"{}")
        err = reply.get("error") if isinstance(reply, dict) else None
        if err:
            raise ValueError(err.get("message", err) if isinstance(err, dict) else err)
        _warned.discard(key)
    except Exception as e:  # whatever herdr does, it isn't the request's problem
        # Once per pane and kind of failure until it works again: a herdr that's gone shouldn't
        # fill the broker's log.
        if key not in _warned:
            _warned.add(key)
            print(f"apod-broker: herdr {method} for pane {t['pane']}: {e}", file=sys.stderr, flush=True)


def _send(t, method, **params):
    """Queue a call (the caller holds _lock, so the calls go out in the order the states changed)."""
    if not _worker:
        _worker.append(threading.Thread(target=_drain, daemon=True, name="herdr"))
        _worker[0].start()
    _queue.put((t, method, params))


def _drain():
    while True:
        t, method, params = _queue.get()
        _call(t, method, **params)


def _text(s):
    """Text for herdr's UI: the pod shapes the request's display, so no control characters."""
    return "".join(c if c.isprintable() else " " for c in s)


def blocked(sess, ev):
    """A request of this session starts waiting for approval."""
    t = (sess.cfg or {}).get("_herdr")
    if not t:
        return
    key = (t["socket"], t["pane"])
    with _lock:
        _waiting.setdefault(key, set()).add(ev["id"])
        _send(t, "pane.report_agent", state="blocked", message=_text(f"apod broker: {ev['request']}")[:200])


def unblocked(sess, ev):
    """The request was decided, or gave up waiting. The pane goes back to herdr's own reading of the
    screen once nothing else of it is waiting."""
    t = (sess.cfg or {}).get("_herdr") if sess else None
    if not t:
        return
    key = (t["socket"], t["pane"])
    with _lock:
        ids = _waiting.get(key, set())
        ids.discard(ev["id"])
        if ids:
            return
        _waiting.pop(key, None)
        _send(t, "pane.release_agent")


def release_all(sessions):
    """When the broker starts: a request that was waiting when the last broker ended is gone, but its
    pane may still show as blocked. Releasing a pane nothing holds is harmless."""
    for sess in sessions:
        t = (sess.cfg or {}).get("_herdr")
        if t:
            with _lock:
                _send(t, "pane.release_agent")
