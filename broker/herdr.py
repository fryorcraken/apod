"""herdr: while a pod's request waits for your approval, its herdr pane shows as blocked.

herdr (a terminal multiplexer for coding agents) reads the agent's state from the pane's screen.
A request waiting in the broker looks like "working" there: the agent's tool call just doesn't
return. So the broker tells herdr itself, over herdr's socket API (pane.report_agent), that the
agent needs a decision, and lets go (pane.release_agent) when the last waiting request of that
pane is decided or times out; herdr then reads the screen again.

apod gives the broker the pane when it registers a pod launched from a herdr pane (HERDR_PANE_ID,
HERDR_SOCKET_PATH). Everything here is best effort: a missing or old herdr never holds up, or
fails, a request.

The Herdr app (herdr-core, HC_TERMINAL_ID) has no call for that: a terminal's status is what its
program writes in its output, OSC 7501 (the Program Status Protocol), and the app alerts when it
turns blocked. Claude Code writes its own (app=claude-code); while a request waits, the tool call
it runs keeps it "working". So for a pod launched from such a terminal, apod gives the broker the
terminal's device, and the broker writes the status there itself: blocked while a request waits,
then working again, as Claude's was (its next report replaces it either way).
"""
import base64
import json
import os
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
TTY = re.compile(r"/dev/pts/[0-9]{1,6}")
APPS = {"claude": "claude-code"}  # the agents that write their own OSC 7501 status, by the name they write

_lock = threading.Lock()
_waiting = {}  # (socket, pane), or ("tty", device) -> ids of the requests waiting there
_warned = set()
_queue = queue.Queue()  # calls to herdr, in order: a worker sends them, so a slow herdr never holds up a request
_worker = []


def target(pane, sock, agent):
    """What register() keeps for a pod: where to report, or None when what apod passed isn't usable."""
    if not (pane and sock and PANE.fullmatch(pane) and sock.startswith("/") and AGENT.fullmatch(agent or "")):
        return None
    return {"pane": pane, "socket": sock, "agent": agent}


def tty_target(tty, agent):
    """What register() keeps for a pod launched from a Herdr app terminal: its device, or None."""
    if not (tty and TTY.fullmatch(tty) and agent in APPS):
        return None
    return {"tty": tty, "app": APPS[agent]}


def status_report(t, state, kind=None, msg=None):
    """An OSC 7501 report of the terminal's status, as Claude Code writes its own. The message is
    base64, so the request's text puts no control characters in your terminal."""
    fields = [f"state={state}", f"app={t['app']}"] + ([f"kind={kind}"] if kind else [])
    if msg:
        fields.append("msg=" + base64.b64encode(msg.encode()).decode())
    return f"\x1b]7501;{':'.join(fields)}\x1b\\".encode()


def _write(t, seq):
    """Write a report to the terminal: one short write, never waiting for the terminal."""
    key = (t["tty"], "status")
    try:
        fd = os.open(t["tty"], os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            if not os.isatty(fd) or os.fstat(fd).st_uid != os.getuid():
                raise OSError("not a terminal of yours")
            os.write(fd, seq)
        finally:
            os.close(fd)
        _warned.discard(key)
    except OSError as e:
        if key not in _warned:
            _warned.add(key)
            print(f"apod-broker: status for terminal {t['tty']}: {e}", file=sys.stderr, flush=True)


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
        if method == "status":
            _write(t, params["seq"])
        else:
            _call(t, method, **params)


def _targets(sess):
    """Where this session's waiting requests show: its herdr pane, its Herdr app terminal."""
    cfg = (sess.cfg or {}) if sess else {}
    return [t for t in (cfg.get("_herdr"), cfg.get("_status_tty")) if t]


def _key(t):
    return ("tty", t["tty"]) if "tty" in t else (t["socket"], t["pane"])


def _text(s):
    """Text for herdr's UI: the pod shapes the request's display, so no control characters."""
    return "".join(c if c.isprintable() else " " for c in s)


def blocked(sess, ev, terminal=True):
    """A request of this session starts waiting for approval. terminal=False: not in its Herdr app
    terminal (an open apod broker ui shows "needs you" in its own)."""
    msg = _text(f"apod broker: {ev['request']}")[:200]
    for t in _targets(sess):
        if "tty" in t and not terminal:
            continue
        with _lock:
            _waiting.setdefault(_key(t), set()).add(ev["id"])
            if "tty" in t:
                _send(t, "status", seq=status_report(t, "blocked", kind="permission", msg=msg))
            else:
                _send(t, "pane.report_agent", state="blocked", message=msg)


def unblocked(sess, ev):
    """The request was decided, or gave up waiting. Once nothing else of it is waiting, the pane goes
    back to herdr's own reading of the screen, and the terminal to working (the agent's tool call
    goes on)."""
    for t in _targets(sess):
        key = _key(t)
        with _lock:
            ids = _waiting.get(key, set())
            if ev["id"] not in ids:  # not shown there (the TUI showed it)
                continue
            ids.discard(ev["id"])
            if ids:
                continue
            _waiting.pop(key, None)
            if "tty" in t:
                _send(t, "status", seq=status_report(t, "working"))
            else:
                _send(t, "pane.release_agent")


def release_all(sessions):
    """When the broker starts: a request that was waiting when the last broker ended is gone, but its
    pane may still show as blocked. Releasing a pane nothing holds is harmless. Not the terminals:
    a pod that's gone may have left its device to another terminal, and an agent that's still there
    writes its status again on its next change."""
    for sess in sessions:
        t = (sess.cfg or {}).get("_herdr")
        if t:
            with _lock:
                _send(t, "pane.release_agent")
