"""git smart-HTTP pieces shared by the github and rad providers: pkt-lines, side-band, the ref
updates at the start of a push, and the replies the broker sends git while a push waits for
approval or when it's refused."""
import zlib

from core import ASK_TIMEOUT, BrokerError

ZERO = "0" * 40


def pkt(data):
    return b"%04x" % (len(data) + 4) + data


def sideband(band, data):
    out = b""
    for i in range(0, len(data), 65000):
        out += pkt(bytes([band]) + data[i:i + 65000])
    return out


def parse_push(body, gzipped=False):
    """A receive-pack request: (ref updates [(old, new, ref)], capabilities, push options,
    offset of the pack in the body)."""
    data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(body) if gzipped else body
    cmds, caps, opts, i = [], set(), [], 0
    section = "cmds"
    while i + 4 <= len(data):
        n = int(data[i:i + 4], 16)
        if n == 0:
            i += 4
            if section == "cmds" and "push-options" in caps:
                section = "opts"
                continue
            break
        line = data[i + 4:i + n].rstrip(b"\n")
        i += n
        if section == "opts":
            opts.append(line.decode())
            continue
        if line.startswith(b"shallow "):
            continue
        if b"\0" in line:
            line, c = line.split(b"\0", 1)
            caps |= set(c.decode().split())
        parts = line.decode().split(" ")
        if len(parts) != 3:
            raise BrokerError(f"unexpected push command: {line[:80]!r}")
        cmds.append(tuple(parts))
    return cmds, caps, opts, i


def encode_push(cmds, caps, opts, body, offset):
    """A receive-pack request again, from parsed ref updates (e.g. with refs renamed)."""
    out = b"".join(pkt(f"{o} {n} {r}".encode() + (b"\0" + " ".join(sorted(caps)).encode() if i == 0 else b"") + b"\n")
                   for i, (o, n, r) in enumerate(cmds)) + b"0000"
    if "push-options" in caps:
        out += b"".join(pkt(o.encode() + b"\n") for o in opts) + b"0000"
    return out + body[offset:]


def ref_kind(old, new, ref, default_branch):
    if set(new) == {"0"}:
        return "push delete"
    if ref.startswith("refs/tags/"):
        return "push tag"
    if default_branch and ref == f"refs/heads/{default_branch}" or \
            (not default_branch and ref in ("refs/heads/main", "refs/heads/master")):
        return "push default-branch"
    if ref.startswith("refs/heads/"):
        return "push branch"
    return "push other-ref"


class PushReply:
    """The response to a receive-pack POST, started early so that git shows the broker's
    messages ("remote: ...") while the push waits."""

    def __init__(self, h, caps):
        self.h, self.band, self.started = h, "side-band-64k" in caps or "side-band" in caps, False

    def start(self):
        if not self.started:
            self.h.send_response(200)
            self.h.send_header("Content-Type", "application/x-git-receive-pack-result")
            self.h.send_header("Cache-Control", "no-cache")
            self.h.send_header("Connection", "close")
            self.h.end_headers()
            self.started = True

    def message(self, text):
        if self.band:
            self.start()
            self.h.wfile.write(sideband(2, f"apod broker: {text}\n".encode()))
            self.h.wfile.flush()

    def waiting(self, ev):
        self.message(f"this push needs your approval ({ev['reason']}). Waiting up to {ASK_TIMEOUT}s. "
                     f"On the host: apod broker allow {ev['id']} [--session|--persona], or apod broker ui")

    def report(self, results, unpack="ok"):
        """results: [(ref, None or error)]: ok/ng lines, in a side-band if git asked for one."""
        data = pkt(f"unpack {unpack}\n".encode()) + b"".join(
            pkt((f"ok {ref}\n" if err is None else f"ng {ref} {err}\n").encode()) for ref, err in results) + b"0000"
        self.start()
        self.h.wfile.write(sideband(1, data) + b"0000" if self.band else data)
        self.h.wfile.flush()
