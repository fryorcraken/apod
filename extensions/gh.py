"""gh provider: the pod's gh is a shim; the broker runs the real gh on the host as one of the
host's gh accounts, in an empty directory with a config and home of its own, after classifying
the command line into actions ("gh:pr create", "gh:api GET /repos/{repo}/pulls",
"gh:graphql mutation addComment") and targets (repositories, owners, projects)."""
import base64
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
from pathlib import Path

from core import (BDIR, HARD_DENY, LOCK, SHIM, BrokerError, Request, check, now, provider, read_body, scope_kind,
                  rejected_msg, waiting_msg)

# Never allowed, whatever the role or grants say: these would hand out credentials, change the
# broker's own gh, open things on the host, or need the pod's local git (the shim does those).
GH_HARD_DENY = ["auth *", "extension *", "alias *", "config *", "ssh-key *", "gpg-key *",
             "codespace *", "browse", "repo deploy-key add", "pr checkout", "repo clone",
             "repo set-default", "run download", "release download", "gist clone",
             "graphql subscription *"]
# Commands that act on one repository (-R, or the pod's current repository).
REPO_GROUPS = {"pr", "issue", "run", "workflow", "release", "label", "cache", "ruleset",
               "secret", "variable", "attestation"}
# pr commands whose PR argument defaults to the current branch.
SELECTOR_CMDS = {"pr view", "pr checks", "pr diff", "pr edit", "pr comment", "pr merge", "pr ready",
                 "pr close", "pr reopen", "pr review", "pr update-branch", "pr lock", "pr unlock",
                 "pr revert"}
# Flags that make a command more powerful; each adds the action "<command> --<flag>".
SENSITIVE = {"pr merge": ["admin"], "repo create": ["public"], "repo edit": ["visibility"],
             "issue edit": ["add-project", "remove-project"], "pr edit": ["add-project", "remove-project"]}
# Arguments that name local files without being typed "file" in gh's help: positionals from this
# index on, and string flags. Their contents come from the pod, like --body-file.
POSITIONAL_FILES = {"gist create": 0, "release create": 1, "release upload": 1, "release verify-asset": 1,
                    "attestation verify": 0}
PATH_FLAGS = {"gist edit": ["add"], "attestation verify": ["bundle", "custom-trusted-root"],
              "attestation trusted-root": ["tuf-root"]}
NODE_ID = re.compile(r"^(?:[A-Z]{1,8}_[A-Za-z0-9_-]{6,}|MD[A-Za-z0-9+/]{6,}={0,2})$")
GH_URL = re.compile(r"https?://(?:www\.)?github\.com/([^/\s?#]+)(?:/([^/\s?#]+))?(?:/([^/\s?#]+))?(?:/([^/\s?#]+))?")
# Flags whose values are prose (titles, bodies, notes): a link in them is something the text
# mentions, not something the command acts on, so it's not a target.
TEXT_FLAGS = {"body", "title", "subject", "message", "notes", "description", "desc", "readme", "comment",
              "name", "homepage", "field", "raw-field", "jq", "template", "query", "search"}
NOT_OWNERS = {"orgs", "users", "settings", "marketplace", "sponsors", "notifications", "apps",
              "features", "enterprise", "enterprises", "topics", "collections", "search", "login",
              "new", "organizations", "explore", "codespaces", "pulls", "issues"}



def gh_bin():
    return os.environ.get("APOD_GH_BIN") or shutil.which("gh") or "gh"


def gh_env(token=None, tty=0, repo=None):
    """Environment for the real gh: an empty config dir and home of the broker's own (no host
    aliases, extensions or git config), no prompts, editors, pagers or browsers."""
    home = BDIR / "home"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
        "LANG": os.environ.get("LANG", "C.UTF-8"), "GH_CONFIG_DIR": str(home / "gh"),
        "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local/state"), "XDG_DATA_HOME": str(home / ".local/share"),
        "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1",
        "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1", "GH_SPINNER_DISABLED": "1",
        "GH_PAGER": "cat", "PAGER": "cat", "GH_EDITOR": "false", "EDITOR": "false",
        "VISUAL": "false", "GIT_EDITOR": "false", "GH_BROWSER": "false", "BROWSER": "false",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
    }
    if token:
        env["GH_TOKEN"] = token
    if tty:
        env["GH_FORCE_TTY"] = str(tty)
        env["CLICOLOR_FORCE"] = "1"
    if repo:
        env["GH_REPO"] = repo
    return env


_tokens = {}


def token_for(user):
    """The host gh's token for a GitHub account (the active account when user is empty)."""
    t, ts = _tokens.get(user, (None, 0))
    if t and now() - ts < 300:
        return t
    env = dict(os.environ)
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    cmd = [gh_bin(), "auth", "token", "--hostname", "github.com"] + (["--user", user] if user else [])
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
    t = r.stdout.strip()
    if r.returncode or not t:
        raise BrokerError(f"no gh login on the host for {user or 'the active account'} "
                          f"({r.stderr.strip() or 'gh auth token failed'}); run gh auth login on the host")
    _tokens[user] = (t, now())
    return t


def gh_user(sess):
    return (sess.cfg.get("gh") or {}).get("user", "")


def gh_query(sess, args, timeout=30):
    """Run gh on the broker's own behalf (lookups); returns stdout or None."""
    try:
        r = subprocess.run([gh_bin(), *args], capture_output=True, text=True, timeout=timeout,
                           env=gh_env(token_for(gh_user(sess))), cwd=BDIR / "home",
                           stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, BrokerError):
        return None
    return r.stdout if r.returncode == 0 else None


# ---------------------------------------------------------------------------------------------
# gh command lines

class Spec:
    def __init__(self):
        self.subs, self.flags, self.short, self.canon = set(), {}, {}, None


_specs = {}


def spec(path):
    """Commands and flags of gh <path>, read from its --help (None if there's no such command)."""
    key = tuple(path)
    if key in _specs:
        return _specs[key]
    try:
        r = subprocess.run([gh_bin(), *path, "--help"], capture_output=True, text=True, timeout=20,
                           env=gh_env(), cwd=BDIR / "home", stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode:
        _specs[key] = None
        return None
    s, section = Spec(), None
    for line in r.stdout.splitlines():
        if line and not line[0].isspace():
            section = line.strip()
            continue
        if section == "USAGE" and s.canon is None:
            m = re.match(r"\s+gh((?: [a-z][\w-]*)*)", line)
            if m:
                s.canon = m.group(1).split()
        elif section and section.endswith("COMMANDS"):
            m = re.match(r"\s+([\w-]+):\s", line)
            if m:
                s.subs.add(m.group(1))
        elif section in ("FLAGS", "INHERITED FLAGS"):
            m = re.match(r"\s+(?:-(\w), )?--([\w-]+)(?: (\S+))?(?:\s{2,}|$)", line)
            if m:
                s.flags[m.group(2)] = m.group(3)
                if m.group(1):
                    s.short[m.group(1)] = m.group(2)
    if s.canon is None:
        s.canon = list(path)
    _specs[key] = s
    return s


def takes_value(s, tok):
    if tok.startswith("--"):
        return "=" not in tok and bool(s.flags.get(tok[2:]))
    long = s.short.get(tok[1:2])
    return len(tok) == 2 and bool(long and s.flags.get(long))


def parse_flags(s, toks):
    """[(name, value or None)] and positionals. Unknown flags keep their leading dashes."""
    flags, pos, i = [], [], 0
    while i < len(toks):
        t = toks[i]
        if t == "--":
            pos += toks[i + 1:]
            break
        if t.startswith("--") and len(t) > 2:
            name, eq, val = t[2:].partition("=")
            if name not in s.flags:
                flags.append(("--" + name, val if eq else None))
            elif eq:
                flags.append((name, val))
            elif s.flags[name]:
                if i + 1 >= len(toks):
                    raise BrokerError(f"flag needs an argument: --{name}")
                flags.append((name, toks[i + 1]))
                i += 1
            else:
                flags.append((name, None))
        elif t.startswith("-") and len(t) > 1:
            j = 1
            while j < len(t):
                long = s.short.get(t[j])
                if long is None:
                    flags.append(("-" + t[j:], None))
                    break
                if s.flags.get(long):
                    rest = t[j + 1:]
                    if rest:
                        val = rest[1:] if rest.startswith("=") else rest
                    elif i + 1 < len(toks):
                        val = toks[i + 1]
                        i += 1
                    else:
                        raise BrokerError(f"flag needs an argument: -{t[j]}")
                    flags.append((long, val))
                    break
                flags.append((long, None))
                j += 1
        else:
            pos.append(t)
        i += 1
    return flags, pos


def parse_gh(argv):
    """-> (canonical command path, spec, flags, positionals)."""
    path, s, i, pre = [], spec([]), 0, []
    if s is None:
        raise BrokerError("gh isn't working on the host (gh --help failed)")
    while s.subs:
        j = i
        while j < len(argv) and argv[j].startswith("-") and argv[j] != "--":
            j += 2 if takes_value(s, argv[j]) else 1
        if j >= len(argv) or argv[j] == "--":
            break
        cand = path + [argv[j]]
        s2 = spec(cand)
        if s2 is None:
            raise BrokerError(f"unknown command: gh {' '.join(cand)}")
        pre += argv[i:j]
        path, s, i = s2.canon, s2, j + 1
    if not path:
        raise BrokerError("no gh command given")
    flags, pos = parse_flags(s, pre + argv[i:])
    return path, s, flags, pos


def rebuild(path, flags, pos):
    out = list(path)
    for n, v in flags:
        if n.startswith("-"):
            out.append(n if v is None else f"{n}={v}")
        else:
            out.append(f"--{n}" if v is None else f"--{n}={v}")
    if any(p.startswith("-") for p in pos):
        out.append("--")
    return out + list(pos)


def opt(flags, name):
    vals = [v for n, v in flags if n == name]
    return vals[-1] if vals else None


def optall(flags, name):
    return [v for n, v in flags if n == name]


def has(flags, name):
    return any(n == name and v not in ("false", "0") for n, v in flags)


def norm_repo(s):
    s = s.strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", s)
    if m:
        return f"{m.group(1)}/{m.group(2)}".lower()
    parts = s.split("/")
    if len(parts) == 3:
        if parts[0].lower() != "github.com":
            raise BrokerError(f"only github.com is supported, not {parts[0]}")
        parts = parts[1:]
    if len(parts) == 2 and all(parts):
        return f"{parts[0]}/{parts[1].removesuffix('.git')}".lower()
    raise BrokerError(f"not a repository: {s} (expected OWNER/REPO)")


def url_targets(values):
    out = []
    for v in values:
        for m in GH_URL.finditer(v or ""):
            a, b, c, d = m.groups()
            if a in ("orgs", "users") and b and c == "projects" and d and d.isdigit():
                out.append(("gh-project", f"{b}/{d}".lower()))
            elif a.lower() not in NOT_OWNERS and b:
                out.append(("gh", f"{a}/{b.removesuffix('.git')}".lower()))
    return out


def write_file(work, files, key, n):
    """Write a file the shim sent (keyed by the path as the agent gave it) into the request's
    work dir; returns its path there."""
    if key not in files:
        raise BrokerError(f"file not found in the pod: {key}")
    d = work / f"f{n}"
    d.mkdir()
    name = re.sub(r"[^\w.+-]", "_", Path(key).name)
    p = d / (name if name not in ("", ".", "..") else "file")
    p.write_bytes(files[key])
    return str(p)


def classify_gh(sess, argv, ctx, files, stdin, work):
    req = Request("gh", shlex.join(["gh", *argv]))
    head = argv[:argv.index("--")] if "--" in argv else argv
    if not argv or argv[0] in ("help", "--version", "version") or "--help" in head or "-h" in head:
        req.help, req.argv = True, argv
        return req
    path, s, flags, pos = parse_gh(argv)
    if s.subs:  # a command group without a subcommand: gh prints its help
        req.help, req.argv = True, argv
        return req
    cmd = " ".join(path)
    group, sub = path[0], (path[1] if len(path) > 1 else "")
    req.actions = [cmd] + [f"{cmd} --{f}" for f in SENSITIVE.get(cmd, []) if has(flags, f)]
    login = (sess.login or "").lower()

    # Files: flags of type "file", and release assets, come from what the shim sent.
    n = itertools.count()
    for i, (name, v) in enumerate(flags):
        if s.flags.get(name) == "file" and v is not None:
            flags[i] = (name, v if v == "-" else write_file(work, files, v, next(n)))
    for i, (name, v) in enumerate(flags):
        if name in PATH_FLAGS.get(cmd, []) and v not in (None, "-"):
            flags[i] = (name, write_file(work, files, v, next(n)))
    if cmd in POSITIONAL_FILES:
        k = POSITIONAL_FILES[cmd]
        pos = pos[:k] + [p if p == "-" else write_file(work, files, p.split("#")[0], next(n)) +
                         ("#" + p.split("#", 1)[1] if "#" in p and cmd.startswith("release") else "")
                         for p in pos[k:]]

    repo_flag = opt(flags, "repo")
    base = norm_repo(repo_flag) if repo_flag and group != "project" else (ctx.get("repo") or None)
    if base:
        base = base.lower()
    req.repo = base
    if group in REPO_GROUPS:
        if not base:
            raise BrokerError("no repository: run gh in a clone with a GitHub remote, or pass -R OWNER/REPO")
        req.targets.append(("gh", base))
    if cmd in SELECTOR_CMDS and not pos and ctx.get("branch"):
        pos = [ctx.get("head") or ctx["branch"]]
    if cmd == "pr create":
        for f in ("fill", "fill-first", "fill-verbose"):
            if has(flags, f):
                raise BrokerError(f"--{f} needs the pod's git history, which the broker can't see: pass --title and --body")
        if opt(flags, "head") is None and ctx.get("branch"):
            flags.append(("head", ctx.get("head") or ctx["branch"]))
    if cmd == "issue transfer" and len(pos) > 1:
        req.targets.append(("gh", norm_repo(pos[1])))
    if group == "repo":
        if sub == "create":
            if not pos:
                raise BrokerError("gh repo create needs a name (OWNER/NAME or NAME) here")
            owner, _, name = pos[0].rpartition("/")
            owner = (owner or login).lower()
            req.targets.append(("gh", owner))
            req.created = f"{owner}/{name}".lower()
            flags = [(k, v) for k, v in flags if k not in ("clone", "remote", "source", "push")]
        elif sub == "list":
            req.targets.append(("gh", (pos[0] if pos else login).lower()))
        elif sub == "fork":
            src = norm_repo(pos[0]) if pos else base
            if not src:
                raise BrokerError("no repository to fork: pass OWNER/REPO")
            dest = (opt(flags, "org") or login).lower()
            req.targets += [("gh", src), ("gh-fork-into", dest)]
            req.created = f"{dest}/{(opt(flags, 'fork-name') or src.split('/')[1]).lower()}"
            flags = [(k, v) for k, v in flags if k not in ("clone", "remote", "remote-name")]
            flags += [("clone", "false"), ("remote", "false")]
            pos = [src] + pos[1:]
            req.notes.append(f"the fork isn't added as a git remote here: git remote add fork https://github.com/{req.created}")
        elif sub in ("gitignore", "license"):
            pass
        elif sub == "sync":
            req.targets.append(("gh", norm_repo(pos[0]) if pos else base))
            if opt(flags, "source"):
                req.targets.append(("gh", norm_repo(opt(flags, "source"))))
        else:
            r = norm_repo(pos[0]) if pos and sub in ("view", "edit", "delete", "archive", "unarchive") else base
            if not r:
                raise BrokerError("no repository: pass OWNER/REPO or -R OWNER/REPO")
            req.targets.append(("gh", r))
    elif group == "search":
        rs, os_ = optall(flags, "repo"), optall(flags, "owner")
        req.targets += [("gh", norm_repo(r)) for r in rs] + [("gh", o.lower()) for o in os_]
        if not rs and not os_:
            req.targets.append(("unscoped", f"{cmd} across GitHub"))
    elif group == "project":
        owner = opt(flags, "owner")
        owner = (login if owner in (None, "@me") else owner).lower()
        number = pos[0] if pos and pos[0].isdigit() else None
        if sub in ("list", "create"):
            req.targets.append(("gh-projects-of", owner))
        elif sub == "copy":
            src = opt(flags, "source-owner")
            src = (login if src in (None, "@me") else src).lower()
            req.targets.append(("gh-project", f"{src}/{number}"))
            tgt = opt(flags, "target-owner")
            req.targets.append(("gh-projects-of", (login if tgt in (None, "@me") else tgt).lower()))
        elif number:
            req.targets.append(("gh-project", f"{owner}/{number}"))
        if sub in ("link", "unlink") and repo_flag:
            req.targets.append(("gh", norm_repo(repo_flag if "/" in repo_flag else f"{owner}/{repo_flag}")))
        ids = [v for k in ("id", "project-id", "field-id") for v in optall(flags, k)]
        req.targets += resolve_nodes(sess, ids)
        if not req.targets:
            req.targets.append(("unscoped", f"{cmd} without a project"))
    elif group == "api":
        return classify_api(sess, req, flags, pos, ctx, files, stdin, work, n)
    elif group in ("gist", "status", "agent-task", "preview"):
        req.targets.append(("unscoped", cmd))
    req.targets += url_targets(pos + [v for n, v in flags if v and n not in TEXT_FLAGS])
    req.targets = list(dict.fromkeys(req.targets))
    req.argv = rebuild(path, flags, pos)
    check_host_paths(req.argv, work)
    return req


def check_host_paths(argv, work):
    """Refuse arguments that name an existing host path: gh runs on the host, and a path the
    agent gives means a file in the pod (sent by the shim and rewritten above), never the host's."""
    for a in argv:
        for c in {a, a.partition("=")[2], a.partition("=@")[2], a.lstrip("@")}:
            if c and (c.startswith(("/", "~")) or "../" in c or c == "..") and not c.startswith(str(work) + "/"):
                if os.path.lexists(os.path.expanduser(c)) or "../" in c:
                    raise BrokerError(f"refers to a path on the host, which gh can't use here: {c}")


# ---------------------------------------------------------------------------------------------
# gh api: REST and GraphQL

def api_fields(flags, files, stdin):
    """gh api's -f/-F parameters as a dict (typed for -F, @file read from what the shim sent)."""
    params = {}
    for name, v in flags:
        if name not in ("field", "raw-field") or v is None:
            continue
        k, _, val = v.partition("=")
        if name == "field":
            if val.startswith("@"):
                src = val[1:]
                val = (stdin or b"" if src == "-" else files.get(src, b"")).decode("utf-8", "replace")
            elif val in ("true", "false"):
                val = val == "true"
            elif val == "null":
                val = None
            elif re.fullmatch(r"-?\d+", val):
                val = int(val)
        parts = [k.split("[", 1)[0]] + re.findall(r"\[([^\]]*)\]", k)
        cur = params
        for j, part in enumerate(parts):
            if j == len(parts) - 1:
                cur[part] = val
            elif parts[j + 1] == "":
                cur.setdefault(part, []).append(val)
                break
            else:
                cur = cur.setdefault(part, {})
    return params


def classify_api(sess, req, flags, pos, ctx, files, stdin, work, n):
    host = opt(flags, "hostname")
    if host not in (None, "github.com"):
        raise BrokerError(f"only github.com is supported, not {host}")
    if not pos:
        raise BrokerError("gh api needs an endpoint")
    endpoint = pos[0]
    params = api_fields(flags, files, stdin)
    inp = opt(flags, "input")
    method = (opt(flags, "method") or ("POST" if params or inp else "GET")).upper()
    # Files for -F key=@path and --input.
    for i, (name, v) in enumerate(flags):
        if name == "field" and v and "=@" in v:
            k, _, src = v.partition("=@")
            if src != "-":
                flags[i] = (name, f"{k}=@{write_file(work, files, src, next(n))}")
        if name == "input" and v and v != "-":
            flags[i] = (name, write_file(work, files, v, next(n)))
    req.argv = rebuild(["api"], flags, pos)
    check_host_paths(req.argv, work)
    owner, _, rname = (ctx.get("repo") or "/").partition("/")
    if endpoint == "graphql":
        body = None
        if inp:
            raw = stdin if inp == "-" else files.get(inp)
            try:
                body = json.loads(raw or b"")
            except ValueError:
                body = {}
            query, variables = body.get("query"), body.get("variables") or {}
        else:
            variables = dict(params)
            query = variables.pop("query", None)
            variables.pop("operationName", None)
        if not isinstance(query, str):
            raise BrokerError("gh api graphql needs a query (-f query=...)")
        classify_graphql(sess, req, query, variables)
        req.targets = list(dict.fromkeys(req.targets))
        return req
    path = endpoint
    if re.match(r"https?://", path):
        u = urllib.parse.urlparse(path)
        if u.hostname != "api.github.com":
            raise BrokerError(f"only api.github.com is supported, not {u.hostname}")
        path = u.path
    path = path.split("?")[0].lstrip("/")
    path = path.replace("{owner}", owner).replace("{repo}", rname).replace("{branch}", ctx.get("branch") or "{branch}")
    if "{" in path and ("{owner}" in endpoint or "{repo}" in endpoint) and not ctx.get("repo"):
        raise BrokerError("{owner}/{repo} need a current repository: run in a clone or use the full path")
    m = re.match(r"repos/([^/]+)/([^/]+)(/.*)?$", path)
    if m:
        req.targets.append(("gh", f"{m.group(1)}/{m.group(2)}".lower()))
        req.actions = [f"api {method} /repos/{{repo}}{m.group(3) or ''}"]
        return req
    m = re.match(r"(orgs|users)/([^/]+)(/.*)?$", path)
    if m:
        req.targets.append(("gh", m.group(2).lower()))
        req.actions = [f"api {method} /{m.group(1)}/{{owner}}{m.group(3) or ''}"]
        return req
    req.actions = [f"api {method} /{path}"]
    return req


def gql_tokens(s):
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in " \t\r\n,﻿":
            i += 1
        elif c == "#":
            while i < n and s[i] != "\n":
                i += 1
        elif s.startswith('"""', i):
            j = s.find('"""', i + 3)
            if j < 0:
                raise ValueError("unterminated block string")
            yield ("str", s[i + 3:j])
            i = j + 3
        elif c == '"':
            j = i + 1
            while j < n and s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            yield ("str", json.loads(s[i:j + 1]))
            i = j + 1
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            yield ("name", s[i:j])
            i = j
        elif c == "$":
            j = i + 1
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            yield ("var", s[i + 1:j])
            i = j
        elif c == "-" or c.isdigit():
            j = i + 1
            while j < n and (s[j].isalnum() or s[j] in ".+-"):
                j += 1
            yield ("num", s[i:j])
            i = j
        elif s.startswith("...", i):
            yield ("p", "...")
            i += 3
        elif c in "{}()[]:=!@|&":
            yield ("p", c)
            i += 1
        else:
            raise ValueError(f"unexpected character {c!r}")


class GqlParser:
    """Just enough GraphQL to see each operation's top-level fields and their arguments."""

    def __init__(self, s):
        self.t, self.i = list(gql_tokens(s)), 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self, want=None):
        tok = self.peek()
        if tok[0] is None or (want and tok[1] != want):
            raise ValueError(f"expected {want or 'more'}, got {tok[1]}")
        self.i += 1
        return tok

    def skip_balanced(self, open_, close):
        depth = 0
        while True:
            k, v = self.take()
            if k == "p" and v == open_:
                depth += 1
            elif k == "p" and v == close:
                depth -= 1
                if depth == 0:
                    return

    def skip_directives(self):
        while self.peek() == ("p", "@"):
            self.take()
            self.take()
            if self.peek() == ("p", "("):
                self.skip_balanced("(", ")")

    def value(self):
        k, v = self.take()
        if k == "var":
            return ("var", v)
        if k == "p" and v == "[":
            out = []
            while self.peek() != ("p", "]"):
                out.append(self.value())
            self.take("]")
            return out
        if k == "p" and v == "{":
            out = {}
            while self.peek() != ("p", "}"):
                name = self.take()[1]
                self.take(":")
                out[name] = self.value()
            self.take("}")
            return out
        if k == "name":
            return {"true": True, "false": False, "null": None}.get(v, v)
        return v

    def selection(self):
        fields = []
        self.take("{")
        while self.peek() != ("p", "}"):
            if self.peek() == ("p", "..."):
                self.take()
                fields.append(("...", {}))
                if self.peek() == ("name", "on"):
                    self.take()
                    self.take()
                elif self.peek()[0] == "name":
                    self.take()
                self.skip_directives()
                if self.peek() == ("p", "{"):
                    self.skip_balanced("{", "}")
                continue
            name = self.take()[1]
            if self.peek() == ("p", ":"):
                self.take()
                name = self.take()[1]
            args = {}
            if self.peek() == ("p", "("):
                self.take()
                while self.peek() != ("p", ")"):
                    a = self.take()[1]
                    self.take(":")
                    args[a] = self.value()
                self.take(")")
            self.skip_directives()
            if self.peek() == ("p", "{"):
                self.skip_balanced("{", "}")
            fields.append((name, args))
        self.take("}")
        return fields

    def operations(self):
        ops = []
        while self.peek()[0] is not None:
            k, v = self.peek()
            if k == "name" and v == "fragment":
                while self.peek() != ("p", "{"):
                    self.take()
                self.skip_balanced("{", "}")
            elif k == "name" and v in ("query", "mutation", "subscription"):
                self.take()
                if self.peek()[0] == "name":
                    self.take()
                if self.peek() == ("p", "("):
                    self.skip_balanced("(", ")")
                self.skip_directives()
                ops.append((v, self.selection()))
            elif (k, v) == ("p", "{"):
                ops.append(("query", self.selection()))
            else:
                raise ValueError(f"unexpected {v}")
        return ops


def gql_resolve(v, variables):
    if isinstance(v, tuple) and v[0] == "var":
        return variables.get(v[1])
    if isinstance(v, list):
        return [gql_resolve(x, variables) for x in v]
    if isinstance(v, dict):
        return {k: gql_resolve(x, variables) for k, x in v.items()}
    return v


def node_ids(v):
    if isinstance(v, str):
        return [v] if NODE_ID.match(v) else []
    if isinstance(v, list):
        return [i for x in v for i in node_ids(x)]
    if isinstance(v, dict):
        return [i for x in v.values() for i in node_ids(x)]
    return []


def classify_graphql(sess, req, query, variables):
    try:
        ops = GqlParser(query).operations()
    except (ValueError, json.JSONDecodeError) as e:
        req.actions = ["graphql (unparsed)"]
        req.targets = [("unscoped", f"graphql query the broker couldn't parse: {e}")]
        return
    req.actions = []
    for optype, fields in ops:
        for name, args in fields:
            if name == "__typename":
                continue
            req.actions.append(f"graphql {optype} {name}")
            args = gql_resolve(args, variables)
            if name == "...":
                req.targets.append(("unscoped", f"graphql {optype} with a top-level fragment"))
            elif optype == "query" and name == "repository":
                o, r = args.get("owner"), args.get("name")
                if isinstance(o, str) and isinstance(r, str):
                    req.targets.append(("gh", f"{o}/{r}".lower()))
                else:
                    req.targets.append(("unscoped", "graphql repository without a literal owner and name"))
            elif optype == "query" and name in ("organization", "user", "repositoryOwner"):
                lg = args.get("login")
                req.targets.append(("gh", lg.lower()) if isinstance(lg, str) else
                                   ("unscoped", f"graphql {name} without a literal login"))
            elif optype == "query" and name in ("node", "nodes"):
                ids = node_ids(args)
                req.targets += resolve_nodes(sess, ids) if ids else [("unscoped", f"graphql {name} without ids")]
            elif optype == "mutation":
                ids = node_ids(args)
                if ids:
                    req.targets += resolve_nodes(sess, ids)
                else:
                    req.targets.append(("unscoped", f"graphql mutation {name} (no node ids to scope it)"))
    if not req.actions:
        req.actions = ["graphql (empty)"]


_nodes = {}
NODE_QUERY = """query($ids:[ID!]!){nodes(ids:$ids){__typename id
... on Repository{nameWithOwner}
... on Issue{repository{nameWithOwner}} ... on PullRequest{repository{nameWithOwner}}
... on IssueComment{repository{nameWithOwner}} ... on PullRequestReview{repository{nameWithOwner}}
... on PullRequestReviewComment{repository{nameWithOwner}} ... on PullRequestReviewThread{repository{nameWithOwner}}
... on Label{repository{nameWithOwner}} ... on Milestone{repository{nameWithOwner}}
... on Discussion{repository{nameWithOwner}} ... on Commit{repository{nameWithOwner}} ... on Ref{repository{nameWithOwner}}
... on ProjectV2{number owner{...O}} ... on ProjectV2Item{project{number owner{...O}}}
... on ProjectV2Field{project{number owner{...O}}} ... on ProjectV2SingleSelectField{project{number owner{...O}}}
... on ProjectV2IterationField{project{number owner{...O}}} ... on ProjectV2View{project{number owner{...O}}}
... on User{login} ... on Organization{login} ... on Bot{login}}}
fragment O on ProjectV2Owner{... on User{login} ... on Organization{login}}"""


def node_target(n):
    if not n:
        return None
    t = n.get("__typename")
    repo = n.get("nameWithOwner") or (n.get("repository") or {}).get("nameWithOwner")
    if repo:
        return ("gh", repo.lower())
    proj = n if t == "ProjectV2" else n.get("project")
    if proj and proj.get("number") is not None:
        return ("gh-project", f"{(proj.get('owner') or {}).get('login', '?')}/{proj['number']}".lower())
    if t == "Organization":
        return ("gh", n["login"].lower())
    if t in ("User", "Bot"):
        return ("people", n["login"].lower())  # a person referenced (assignee, reviewer): no scope
    return None


def resolve_nodes(sess, ids):
    """Targets of GraphQL node ids (what repository or project each belongs to)."""
    todo = [i for i in dict.fromkeys(ids) if i not in _nodes]
    if todo:
        out = gh_query(sess, ["api", "graphql", "-f", f"query={NODE_QUERY}"] +
                       [a for i in todo for a in ("-f", f"ids[]={i}")])
        try:
            nodes = json.loads(out or "")["data"]["nodes"]
        except (ValueError, KeyError, TypeError):
            nodes = [None] * len(todo)
        for i, node in zip(todo, nodes):
            _nodes[i] = node_target(node)
    targets = []
    for i in ids:
        t = _nodes.get(i)
        if t is None:
            targets.append(("unscoped", f"node {i}"))
        elif t[0] != "people":
            targets.append(t)
    return targets


# ---------------------------------------------------------------------------------------------
# Running gh

class Framer:
    """The response to the shim: one JSON object per line (stdout, stderr, exit status)."""

    def __init__(self, wfile):
        self.w, self.lock, self.broken = wfile, threading.Lock(), False

    def send(self, obj):
        with self.lock:
            if self.broken:
                return False
            try:
                self.w.write((json.dumps(obj) + "\n").encode())
                self.w.flush()
                return True
            except OSError:
                self.broken = True
                return False

    def data(self, stream, b):
        return self.send({"t": stream, "d": base64.b64encode(b).decode()})

    def msg(self, text):
        return self.data("err", f"apod broker: {text}\n".encode())


def run_gh(sess, req, tty, out, work):
    env = gh_env(token_for(gh_user(sess)), tty, req.repo)
    p = subprocess.Popen([gh_bin(), *req.argv], cwd=work, env=env,
                         stdin=subprocess.PIPE if req.stdin is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def pump(src, name):
        for chunk in iter(lambda: src.read1(65536), b""):
            if not out.data(name, chunk):
                p.kill()
                break

    ts = [threading.Thread(target=pump, args=(p.stdout, "out"), daemon=True),
          threading.Thread(target=pump, args=(p.stderr, "err"), daemon=True)]
    for t in ts:
        t.start()
    if req.stdin is not None:
        try:
            p.stdin.write(req.stdin)
            p.stdin.close()
        except OSError:
            pass
    for t in ts:
        t.join()
    return p.wait()


def handle_gh(h, sess, method):
    try:
        r = json.loads(read_body(h))
        argv = [str(a) for a in r.get("argv", [])]
        ctx = r.get("ctx") or {}
        files = {k: base64.b64decode(v) for k, v in (r.get("files") or {}).items()}
        stdin = base64.b64decode(r["stdin"]) if r.get("stdin") is not None else None
        tty = int(r.get("tty") or 0)
    except (ValueError, TypeError, KeyError):
        h.send_error(400)
        return
    h.send_response(200)
    h.send_header("Content-Type", "application/x-ndjson")
    h.send_header("Connection", "close")
    h.end_headers()
    out = Framer(h.wfile)
    (BDIR / "tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=BDIR / "tmp") as work:
        work = Path(work)
        try:
            req = classify_gh(sess, argv, ctx, files, stdin, work)
        except BrokerError as e:
            out.msg(str(e))
            out.send({"t": "exit", "code": 1})
            return
        req.stdin = stdin
        if req.help:
            code = run_gh(sess, req, tty, out, work)
            out.send({"t": "exit", "code": code})
            return
        req.actions = ["gh:" + x for x in req.actions]
        ok, ev = check(sess, req, lambda ev: out.msg(waiting_msg(req, ev)))
        if not ok:
            out.msg(rejected_msg(req, ev))
            out.send({"t": "exit", "code": 1})
            return
        try:
            code = run_gh(sess, req, tty, out, work)
        except BrokerError as e:
            out.msg(str(e))
            code = 1
        ev["exit"] = code
        if code == 0 and req.created:
            with LOCK:
                sess.add_scope(("gh", req.created))
                sess.save()
            out.msg(f"added {req.created} to this session's scope")
        for note in req.notes if code == 0 else []:
            out.msg(note)
        out.send({"t": "exit", "code": code})




HARD_DENY.extend("gh:" + a for a in GH_HARD_DENY)
GH_REMOTE = re.compile(r"^(?:https?://(?:[^@/]+@)?github\.com/|(?:ssh://)?(?:git@)?(?:github\.com|github-[^:/]+)[:/])"
                       r"([^/]+)/([^/]+?)(?:\.git)?/?$")


def github_repo(url):
    m = GH_REMOTE.match(url or "")
    return f"{m.group(1)}/{m.group(2)}".lower() if m else None


def gh_login(user):
    """The GitHub login for a host gh account (raises BrokerError when there's no login)."""
    tok = token_for(user)
    if user:
        return user
    r = subprocess.run([gh_bin(), "api", "user", "--jq", ".login"], capture_output=True, text=True,
                       env=gh_env(tok), cwd=BDIR / "home", timeout=30, stdin=subprocess.DEVNULL)
    return r.stdout.strip()


class GhProvider:
    name = "gh"
    routes = [("POST", "/gh", handle_gh)]

    def configure(self, sess, cfg, reg, out):
        # The GitHub account is always named: never your active host login by default, which
        # would have a pod act as you.
        user = cfg.get("user", "")
        if not user:
            where = Path(reg.get("profile_dir") or "the profile") / "broker.toml"
            out["context"].append("gh and git pushes to GitHub are unavailable: this profile names no GitHub account.")
            if any(github_repo(u) for u in reg.get("remotes", [])):
                out["warnings"].append(f"gh and GitHub remotes are off for this pod: add [gh] user = \"ACCOUNT\" "
                                       f"to {where}, or pass --gh-user ACCOUNT (an account logged in to the "
                                       f"host's gh)")
            return False
        try:
            login = gh_login(user)
        except BrokerError as e:
            out["context"].append(f"gh and GitHub over git are unavailable: {e}.")
            out["warnings"].append(f"gh and GitHub remotes are off for this pod: {e}")
            return False
        sess.cfg["gh"] = {"user": user, "login": login}
        for url in reg.get("remotes", []):
            r = github_repo(url)
            if r:
                sess.add_scope(("gh", r))
        out["mounts"].append([str(SHIM), "/usr/local/bin/gh", "ro"])
        out["context"].append(
            f"gh is a shim: commands run on the host as GitHub user {login}. gh auth token and "
            f"pr create --fill (pass --title and --body) are unavailable; gh repo clone and gh pr checkout "
            f"run locally through git. Searches and lists across an owner (--owner X) need X itself in "
            f"scope: narrow them with --repo OWNER/REPO.")
        return True


provider(GhProvider())


def _gh_owner_entries(sess):
    return [p.lower() for p in sess.entries("gh") if "/" not in p]


# gh:OWNER/REPO is a repository (patterns too: gh:OWNER/*); gh:OWNER is the owner itself
# (owner-level reads, its project list, creating repositories there).
scope_kind("gh", provider="gh", parse=lambda v: v.strip().lower().removesuffix(".git"), remote=github_repo,
           help="gh:OWNER/REPO (gh:OWNER/* for every repository of OWNER), gh:OWNER (the owner itself)")
scope_kind("gh-project", provider="gh", parse=lambda v: v.strip().lower(),
           help="gh-project:OWNER/NUMBER (gh-project:OWNER/* for all of OWNER's projects)")
# Target kinds that are checked against those entries, not written in the scope themselves.
scope_kind("gh-projects-of", provider="gh", entry=False,
           match=lambda sess, v: v in _gh_owner_entries(sess) or any(
               p.lower().split("/")[0] == v for p in sess.entries("gh-project")),
           grant=lambda v: ("gh-project", f"{v}/*"), label=lambda v: f"gh-project:{v}/*")
scope_kind("gh-fork-into", provider="gh", entry=False,
           match=lambda sess, v: v == sess.login.lower() or v in _gh_owner_entries(sess),
           grant=lambda v: ("gh", v), label=lambda v: f"gh:{v} (fork into)")
