"""gh-api provider: https://api.github.com from the pod, through the broker, for tools that call
GitHub's API themselves with the token `gh auth token` gives them (Claude Code's PR status, for
one). Off unless the profile has [gh-api] enabled = true; it needs gh (the account is [gh] user).

How: in the pod, api.github.com resolves to 127.0.0.1, where apod-gh-api-relay passes each
connection to the broker. The broker ends TLS there with a certificate from a CA of its own (in
~/.local/state/apod/broker/gh-api/: its key never leaves the host, and it is constrained to vouch for
api.github.com only; the pod trusts it through NODE_EXTRA_CA_CERTS). It checks each request as it
would `gh api` (the role's actions and the scope; it never waits for approval: what isn't
allowed outright is refused at once) and sends it on with the account's token. `gh auth token`
in the pod prints a placeholder, so the token never enters the pod.

Actions: gh's, as for gh api: "gh:api GET /repos/{repo}/pulls", "gh:graphql query repository".
Targets: gh's (gh:OWNER/REPO, gh:OWNER)."""
import http.client
import json
import os
import secrets
import shutil
import ssl
import tempfile
import urllib.parse
from pathlib import Path

import gh
from api import BDIR, BODY_LIMIT, LOCK, BrokerError, Provider, Request, check, provider, rejected_msg, run_quiet

HOST = "api.github.com"
PLACEHOLDER = "apod-gh-api-placeholder-token"  # what gh auth token prints in the pod
CERTS = BDIR / "gh-api"
POD_DIR = "/run/apod-gh-api"
RELAY = Path(__file__).resolve().parent / "apod-gh-api-relay"
# Request headers not passed on: hop-by-hop ones, and those the broker sets itself.
DROP = {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "te", "trailer",
        "transfer-encoding", "upgrade", "host", "authorization", "content-length"}


# ---------------------------------------------------------------------------------------------
# The CA and api.github.com's certificate

CA_CONF = """[req]
distinguished_name = dn
prompt = no
[dn]
CN = apod broker gh-api CA
[ext]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
nameConstraints = critical, permitted;DNS:api.github.com
subjectKeyIdentifier = hash
"""
LEAF_CONF = """[req]
distinguished_name = dn
prompt = no
[dn]
CN = api.github.com
[ext]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = DNS:api.github.com
authorityKeyIdentifier = keyid
subjectKeyIdentifier = hash
"""


def openssl(*args, conf=None):
    with tempfile.NamedTemporaryFile("w", dir=CERTS, suffix=".cnf") as f:
        if conf:
            f.write(conf)
            f.flush()
        r = run_quiet(["openssl", *[a.replace("{conf}", f.name) for a in args]], cwd=CERTS)
    if r.returncode:
        raise BrokerError(f"openssl {args[0]} failed: {r.stderr.strip()[-300:]}")


def ensure_certs():
    """The CA (made once) and api.github.com's certificate (made again 30 days before it expires)."""
    ca, cakey, leaf, key = CERTS / "ca.pem", CERTS / "ca.key", CERTS / "leaf.pem", CERTS / "leaf.key"
    have = all(p.exists() for p in (ca, leaf, key))
    if have and not shutil.which("openssl"):
        return  # can't check its date; it was made by an openssl that was there
    if have and run_quiet(["openssl", "x509", "-checkend", "2592000", "-noout", "-in", str(leaf)]).returncode == 0:
        return
    if not shutil.which("openssl"):
        raise BrokerError("openssl isn't installed on the host (it makes the broker's certificate for api.github.com)")
    CERTS.mkdir(parents=True, exist_ok=True)
    os.chmod(CERTS, 0o700)
    if not (ca.exists() and cakey.exists()):
        openssl("req", "-x509", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                "-keyout", str(cakey), "-out", str(ca), "-days", "3650", "-config", "{conf}", "-extensions", "ext",
                conf=CA_CONF)
    csr = CERTS / "leaf.csr"
    openssl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
            "-keyout", str(key), "-out", str(csr), "-config", "{conf}", conf=LEAF_CONF)
    openssl("x509", "-req", "-in", str(csr), "-CA", str(ca), "-CAkey", str(cakey),
            "-set_serial", "0x1" + secrets.token_hex(15), "-days", "397", "-out", str(leaf),
            "-extfile", "{conf}", "-extensions", "ext", conf=LEAF_CONF)
    csr.unlink(missing_ok=True)
    for p in (cakey, key):
        os.chmod(p, 0o600)
    _ctx.clear()


_ctx = {}


def server_ctx():
    with LOCK:
        if "ctx" not in _ctx:
            ensure_certs()
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(CERTS / "leaf.pem", CERTS / "leaf.key")
            ctx.set_alpn_protocols(["http/1.1"])
            _ctx["ctx"] = ctx
        return _ctx["ctx"]


# ---------------------------------------------------------------------------------------------
# Requests

def classify(sess, method, target, body):
    """A request to api.github.com as the Request gh api would make of it."""
    path = urllib.parse.urlsplit(target).path
    shown = target if len(target) <= 160 else target[:157] + "..."
    req = Request("gh", f"{method} https://{HOST}{shown} (direct)")
    if path.rstrip("/") == "/graphql":
        try:
            b = json.loads(body or b"")
            query, variables = b["query"], b.get("variables") or {}
            if method != "POST" or not isinstance(query, str) or not isinstance(variables, dict):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise BrokerError("a GraphQL request is a POST with a JSON body that has a query")
        gh.classify_graphql(sess, req, query, variables)
        req.display = f"graphql (direct) {' '.join(query.split())[:150]}"
    else:
        gh.classify_rest(req, method, path.lstrip("/"))
    req.actions = ["gh:" + a for a in req.actions]
    return req


def upstream():
    """api.github.com, or the tests' server (APOD_GH_API_UPSTREAM, http://HOST:PORT)."""
    u = os.environ.get("APOD_GH_API_UPSTREAM")
    if u:
        p = urllib.parse.urlsplit(u)
        return http.client.HTTPConnection(p.hostname, p.port, timeout=60)
    return http.client.HTTPSConnection(HOST, timeout=60, context=ssl.create_default_context())


def forward(sess, method, target, headers, body):
    """-> (status, reason, [(header, value)], body)"""
    try:
        req = classify(sess, method, target, body)
    except BrokerError as e:
        return reply(400, str(e))
    ok, ev = check(sess, req, ask=False)
    if not ok:
        return reply(403, rejected_msg(req, ev))
    try:
        token = gh.token_for(gh.gh_user(sess))
    except BrokerError as e:
        return reply(502, str(e))
    out = [(k, v) for k, v in headers if k.lower() not in DROP]
    out += [("Host", HOST), ("Authorization", f"Bearer {token}")]
    if body or method in ("POST", "PUT", "PATCH"):
        out.append(("Content-Length", str(len(body))))
    c = upstream()
    try:
        c.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
        for k, v in out:
            c.putheader(k, v)
        c.endheaders(body or None)
        r = c.getresponse()
        data = r.read()
    except (OSError, http.client.HTTPException) as e:
        ev["exit"] = 502
        return reply(502, f"the broker couldn't reach {HOST}: {e}")
    finally:
        c.close()
    ev["exit"] = r.status
    rh = [(k, v) for k, v in r.getheaders() if k.lower() not in DROP | {"set-cookie"}]
    return r.status, r.reason, rh, data


def reply(status, message):
    body = json.dumps({"message": f"apod broker: {message}"}).encode()
    return status, {400: "Bad Request", 403: "Forbidden", 413: "Payload Too Large",
                    502: "Bad Gateway"}.get(status, ""), [("Content-Type", "application/json")], body


def read_request(f):
    """One HTTP/1.1 request from the pod's TLS stream: (method, target, headers, body, close), or
    None at the end."""
    line = f.readline(8193)
    while line in (b"\r\n", b"\n"):
        line = f.readline(8193)
    if not line:
        return None
    parts = line.decode("latin-1").split()
    if len(parts) != 3 or not parts[1].startswith("/"):
        raise ValueError("bad request line")
    headers = []
    while True:
        h = f.readline(8193)
        if h in (b"\r\n", b"\n", b""):
            break
        k, _, v = h.decode("latin-1").partition(":")
        headers.append((k.strip(), v.strip()))
        if len(headers) > 100:
            raise ValueError("too many headers")
    hd = {k.lower(): v for k, v in headers}
    body = b""
    if "chunked" in hd.get("transfer-encoding", "").lower():
        while True:
            n = int(f.readline(64).split(b";")[0].strip() or b"0", 16)
            if n == 0:
                while f.readline(1024) not in (b"\r\n", b"\n", b""):
                    pass
                break
            if len(body) + n > BODY_LIMIT:
                raise ValueError("body too large")
            body += f.read(n)
            f.readline(8)
    elif hd.get("content-length"):
        n = int(hd["content-length"])
        if n > BODY_LIMIT:
            raise ValueError("body too large")
        body = f.read(n)
    return parts[0].upper(), parts[1], headers, body, hd.get("connection", "").lower() == "close"


def handle_tunnel(h, sess, method):
    """The relay's connection, after "200": TLS as api.github.com, then requests until it closes."""
    try:
        ctx = server_ctx()
    except (BrokerError, OSError, ssl.SSLError) as e:
        return h.send_json({"error": f"gh-api: {e}"}, 500)
    h.close_connection = True
    h.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    h.wfile.flush()
    try:
        h.connection.settimeout(300)
        tls = ctx.wrap_socket(h.connection, server_side=True)
    except (OSError, ssl.SSLError):
        return
    f = tls.makefile("rb")
    try:
        while True:
            try:
                r = read_request(f)
            except (ValueError, OSError) as e:
                if isinstance(e, ValueError):
                    send(tls, *reply(400, str(e)), close=True)
                return
            if r is None:
                return
            method, target, headers, body, close = r
            send(tls, *forward(sess, method, target, headers, body), close=close)
            if close:
                return
    except (OSError, ssl.SSLError):
        return
    finally:
        try:
            tls.close()
        except OSError:
            pass


def send(sock, status, reason, headers, body, close=False):
    head = f"HTTP/1.1 {status} {reason}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers)
    head += f"Content-Length: {len(body)}\r\n" + ("Connection: close\r\n" if close else "") + "\r\n"
    sock.sendall(head.encode("latin-1") + body)


def handle_token(h, sess, method):
    """gh auth token in the pod: a placeholder, which the broker swaps for the real token."""
    return h.send_json({"token": PLACEHOLDER})


class GhApiProvider(Provider):
    name = "gh-api"
    description = "https://api.github.com from the pod (Claude Code's PR status), checked and authenticated by the broker"
    guide = "docs/guides.md#github-api-from-the-pod"
    requires = ["gh"]
    needs_target = False  # gh's scope is its own
    routes = [("POST", "/gh-api/tunnel", handle_tunnel), ("GET", "/gh-api/token", handle_token)]

    def configure(self, sess, cfg, reg, out):
        """[gh-api] enabled = true: the pod reaches api.github.com through the broker."""
        if cfg.get("enabled") is not True:
            return False
        if reg.get("network") == "host":
            out["warnings"].append("gh-api is off for this pod: it shares the host's network, where api.github.com "
                                   "can't be pointed at the pod's relay")
            return False
        try:
            server_ctx()
        except (BrokerError, OSError, ssl.SSLError) as e:
            out["warnings"].append(f"gh-api is off for this pod: {e}")
            return False
        out["mounts"] += [[str(CERTS / "ca.pem"), f"{POD_DIR}/ca.pem", "ro"],
                          [str(RELAY), "/usr/local/bin/apod-gh-api-relay", "ro"]]
        out["hosts"][HOST] = "127.0.0.1"
        out["sysctls"]["net.ipv4.ip_unprivileged_port_start"] = "443"  # the relay listens on 443, unprivileged
        out["start"].append(["apod-gh-api-relay"])
        out["env"]["NODE_EXTRA_CA_CERTS"] = f"{POD_DIR}/ca.pem"
        out["context"].append(
            f"https://{HOST} goes through the broker: requests are checked against the role and scope as gh api "
            f"would be (refused at once, never waiting for approval) and sent as the profile's GitHub account. gh auth "
            f"token prints a placeholder that only works there; Node and Bun tools trust the broker's certificate "
            f"(NODE_EXTRA_CA_CERTS); for curl, pass --cacert {POD_DIR}/ca.pem. Prefer gh for your own requests.")
        return True


provider(GhApiProvider())
