"""git smart-HTTP pieces shared by the github and rad providers: pkt-lines, side-band, the ref
updates at the start of a push, and the replies the broker sends git while a push waits for
approval or when it's refused."""
import hashlib
import zlib

from core import ASK_TIMEOUT, GIT_BODY_LIMIT, BrokerError

ZERO = "0" * 40


def pkt(data):
    return b"%04x" % (len(data) + 4) + data


def sideband(band, data):
    out = b""
    for i in range(0, len(data), 65000):
        out += pkt(bytes([band]) + data[i:i + 65000])
    return out


def push_data(body, gzipped=False):
    """A receive-pack body, gunzipped: one gzip member only, so what's checked is all there is."""
    if not gzipped:
        return body
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    data = d.decompress(body, GIT_BODY_LIMIT)
    if d.unconsumed_tail:
        raise BrokerError("a push body that unpacks to more than the broker takes")
    if d.unused_data or not d.eof:
        raise BrokerError("a push body that isn't exactly one gzip stream")
    return data


def parse_push(body, gzipped=False):
    """A receive-pack request: (ref updates [(old, new, ref)], capabilities, push options,
    offset of the pack in the body)."""
    data = push_data(body, gzipped)
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


def _delta(base, delta):
    """Apply a git delta to base."""
    def varint(i):
        v = shift = 0
        while True:
            c = delta[i]
            i += 1
            v |= (c & 0x7f) << shift
            shift += 7
            if not c & 0x80:
                return v, i
    _, i = varint(0)
    size, i = varint(i)
    out = bytearray()
    while i < len(delta):
        c = delta[i]
        i += 1
        if c & 0x80:  # copy from base
            off = n = 0
            for k in range(4):
                if c & (1 << k):
                    off |= delta[i] << (8 * k)
                    i += 1
            for k in range(3):
                if c & (0x10 << k):
                    n |= delta[i] << (8 * k)
                    i += 1
            out += base[off:off + (n or 0x10000)]
        elif c:  # insert
            out += delta[i:i + c]
            i += c
        else:
            raise ValueError("bad delta")
    if len(out) != size:
        raise ValueError("bad delta size")
    return bytes(out)


def pack_commits(pack):
    """The commits in a push's pack: {oid: [parent oids]}, or None when the pack can't be read.
    An object stored as a delta against one the pack doesn't carry (a thin pack) is skipped: if it's
    a commit, the walk in fast_forward stops there and asks the server, which doesn't have it, so the
    push counts as a force push (it can't be shown not to be)."""
    if pack[:4] != b"PACK":
        return {} if not pack else None
    count = int.from_bytes(pack[8:12], "big")
    i, objs, by_oid, commits = 12, {}, {}, {}
    types = {1: b"commit", 2: b"tree", 3: b"blob", 4: b"tag"}
    for _ in range(count):
        start = i
        c = pack[i]
        i += 1
        kind, shift = (c >> 4) & 7, 4
        while c & 0x80:
            c = pack[i]
            i += 1
            shift += 7
        base = None
        if kind == 6:  # OFS_DELTA
            c = pack[i]
            i += 1
            off = c & 0x7f
            while c & 0x80:
                c = pack[i]
                i += 1
                off = ((off + 1) << 7) | (c & 0x7f)
            base = ("ofs", start - off)
        elif kind == 7:  # REF_DELTA
            base = ("ref", pack[i:i + 20].hex())
            i += 20
        d = zlib.decompressobj()
        data = d.decompress(pack[i:])
        i = len(pack) - len(d.unused_data)
        if base:
            b = objs.get(base[1]) if base[0] == "ofs" else by_oid.get(base[1])
            if b is None:
                if base[0] == "ref":
                    objs[start] = None  # thin pack: a base the server has
                    continue
                return None
            if b[0] is None:
                objs[start] = None
                continue
            t, data = b[0], _delta(b[1], data)
        else:
            t = types.get(kind)
            if t is None:
                return None
        oid = hashlib.sha1(b"%s %d\0" % (t, len(data)) + data).hexdigest()
        objs[start] = by_oid[oid] = (t, data)
        if t == b"commit":
            head = data.split(b"\n\n", 1)[0]
            commits[oid] = [ln[7:].decode() for ln in head.split(b"\n") if ln.startswith(b"parent ")]
    return commits


def fast_forward(old, new, commits, is_ancestor):
    """Whether new descends from old. commits: the pack's {oid: parents}; is_ancestor(a, b) asks the
    server about commits it already has (the walk's way out of the pack). False when it can't tell."""
    if commits is None:
        return False
    seen, todo, outside = set(), [new], []
    while todo:
        c = todo.pop()
        if c in seen:
            continue
        seen.add(c)
        if c == old:
            return True
        if c in commits:
            todo += commits[c]
        else:
            outside.append(c)
    return any(is_ancestor(old, c) for c in outside)


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
                     f"On the host: apod broker allow {ev['id']} [--session|--role], or apod broker ui")

    def report(self, results, unpack="ok"):
        """results: [(ref, None or error)]: ok/ng lines, in a side-band if git asked for one."""
        data = pkt(f"unpack {unpack}\n".encode()) + b"".join(
            pkt((f"ok {ref}\n" if err is None else f"ng {ref} {err}\n").encode()) for ref, err in results) + b"0000"
        self.start()
        self.h.wfile.write(sideband(1, data) + b"0000" if self.band else data)
        self.h.wfile.flush()
