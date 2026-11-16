"""A profile's configuration, read from one place.

A profile is a folder, ~/.agent-pod/<profile>/ (APOD_ROOT overrides ~/.agent-pod). Its settings
are in profile.toml. Profiles made before it existed are spread over several files (broker.toml,
env, mounts, image, network, volumes, connectors, github, ssh-key, ssh-confirm), which are still
read, the same way as ever, when there is no profile.toml: `apod migrate` converts them.

Both `apod` (through `apod-broker profile`) and the broker (register) call load(), so they read
the same thing. This module imports nothing from the broker: it works without it running, and
`apod-broker profile` doesn't load extensions.

load() returns a dict (and `apod-broker profile --profile-dir DIR` prints it as JSON):

    dir, file        the profile folder; the file the settings come from ("" if there is none)
    format           "profile.toml", "old files" or "none"
    broker           whether the broker acts for the pod (profile.toml: broker = ..., default
                     true; old files: broker.toml exists)
    role, scope, allow, deny   the broker's, as in profile.toml ("role" is None when not set)
    pod              image, network (None when not set), mounts [[mode, path], ...], volumes
                     {name: path}, connectors [...], env {NAME: value}
    git              name, email, committer_name, committer_email: the keys that are set
    token            profile: whose GitHub token (podman secret gh-<profile>) a launch without
                     the broker uses, None for the profile's own
    ssh_agent        key (None when not set), confirm
    tables           the other tables of profile.toml (or of broker.toml): the extensions'
    warnings         things to tell the user, as text
"""
import json
import os
import re
import sys
import tomllib
from pathlib import Path

OLD_FILES = ("broker.toml", "env", "mounts", "image", "network", "volumes", "connectors", "github",
             "ssh-key", "ssh-confirm")
# Folders in the apod root that are not profiles.
NOT_PROFILES = ("broker", "broker-extensions", "worklog")
TOP_KEYS = {"broker": bool, "role": str, "scope": list, "allow": list, "deny": list}
# Tables the profile owns; any other table belongs to a broker extension.
OWN_TABLES = {"pod": {"image", "network", "ro", "rw", "volumes", "connectors", "env"},
              "git": {"name", "email", "committer_name", "committer_email"},
              "token": {"profile"},
              "ssh-agent": {"key", "confirm"}}
IDENTITY_ENV = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")
GIT_KEYS = {"GIT_AUTHOR_NAME": "name", "GIT_AUTHOR_EMAIL": "email",
            "GIT_COMMITTER_NAME": "committer_name", "GIT_COMMITTER_EMAIL": "committer_email"}
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CHECKOUT = Path(__file__).resolve().parent.parent  # this apod checkout


class ProfileError(Exception):
    pass


def root():
    return Path(os.environ.get("APOD_ROOT") or Path.home() / ".agent-pod")


def expand(p):
    """A leading ~ is the home folder, as apod has always read it."""
    home = os.environ.get("HOME") or str(Path.home())
    return home + p[1:] if p == "~" or p.startswith("~/") else p


def empty():
    return {"broker": True, "role": None, "scope": [], "allow": [], "deny": [],
            "pod": {"image": None, "network": None, "mounts": [], "volumes": {}, "connectors": [], "env": {}},
            "git": {}, "token": {"profile": None}, "ssh_agent": {"key": None, "confirm": False},
            "tables": {}, "warnings": []}


# ---------------------------------------------------------------------------------------------
# profile.toml

def check_list(where, key, v, actions=False):
    if not isinstance(v, list) or not all(isinstance(a, str) for a in v) or (actions and not all(":" in a for a in v)):
        raise ProfileError(f'{where}: {key} is a list of "PROVIDER:ACTION" patterns' if actions
                           else f"{where}: {key} is a list of strings")
    return list(v)


def check_str(where, key, v, nospace=False):
    if not isinstance(v, str) or not v.strip() or (nospace and re.search(r"\s", v)):
        raise ProfileError(f"{where}: {key} is a string" + (" without spaces" if nospace else "") + ", not empty")
    return v


def check_table(where, table, v, allowed):
    """The keys of one of the profile's own tables must be known."""
    if not isinstance(v, dict):
        raise ProfileError(f"{where}: [{table}] is a table")
    bad = sorted(set(v) - allowed)
    if bad:
        raise ProfileError(f"{where}: [{table}] has no key {', '.join(bad)} (it has {', '.join(sorted(allowed))})")


def from_toml(data, where):
    """The profile from a parsed profile.toml, checked."""
    p = empty()
    # A key written after the first [table] line belongs to that table, so role = ..., scope =
    # ... that ended up there were not meant to be.
    for t, v in data.items():
        if isinstance(v, dict):
            for k in TOP_KEYS:
                if k in v:
                    raise ProfileError(f"{where}: [{t}] has {k} = ...: move it above the first [table] "
                                       f"(a key written after a [table] line belongs to that table)")
    for k, v in data.items():
        if isinstance(v, dict):
            continue
        if k not in TOP_KEYS:
            raise ProfileError(f"{where}: unknown key {k} (top-level keys: {', '.join(TOP_KEYS)}; "
                               f"the rest is in tables: [pod], [git], [token], [ssh-agent], and the extensions')")
        if k == "broker":
            if not isinstance(v, bool):
                raise ProfileError(f"{where}: broker is true or false")
            p["broker"] = v
        elif k == "role":
            p["role"] = check_str(where, k, v, nospace=True)
        else:
            p[k] = check_list(where, k, v, actions=k in ("allow", "deny"))
    for t, allowed in OWN_TABLES.items():
        if t in data:
            check_table(where, t, data[t], allowed)
    pod = data.get("pod", {})
    for k in ("image", "network"):
        if k in pod:
            p["pod"][k] = check_str(f"{where}: [pod]", k, pod[k], nospace=True)
    for mode in ("ro", "rw"):
        if mode in pod:
            p["pod"]["mounts"] += [[mode, expand(m)] for m in check_list(f"{where}: [pod]", mode, pod[mode])]
    if "connectors" in pod:
        p["pod"]["connectors"] = check_list(f"{where}: [pod]", "connectors", pod["connectors"])
    if "volumes" in pod:
        vols = pod["volumes"]
        if not isinstance(vols, dict) or not all(isinstance(k, str) and isinstance(v, str) and k and v.startswith("/")
                                                 for k, v in vols.items()):
            raise ProfileError(f'{where}: [pod] volumes is a table of NAME = "/path in the pod"')
        p["pod"]["volumes"] = dict(vols)
    if "env" in pod:
        env = pod["env"]
        if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
            raise ProfileError(f"{where}: [pod.env] is a table of NAME = \"value\" (values are strings: quote them)")
        for k in env:
            if not ENV_NAME.fullmatch(k):
                raise ProfileError(f"{where}: [pod.env] {k!r} isn't an environment variable name")
            if k in IDENTITY_ENV:
                raise ProfileError(f"{where}: [pod.env] {k}: the git identity is [git] name and email")
        p["pod"]["env"] = dict(env)
    for k, v in data.get("git", {}).items():
        p["git"][k] = check_str(f"{where}: [git]", k, v)
        if k.endswith("email") and not re.fullmatch(r"[^@\s]+@[^@\s]+", v):
            raise ProfileError(f"{where}: [git] {k} isn't an email address")
    if "profile" in data.get("token", {}):
        t = data["token"]["profile"]
        if not isinstance(t, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", t):
            raise ProfileError(f"{where}: [token] profile is the name of a profile")
        p["token"]["profile"] = t
    ssh = data.get("ssh-agent", {})
    if "key" in ssh:
        p["ssh_agent"]["key"] = expand(check_str(f"{where}: [ssh-agent]", "key", ssh["key"]))
    if "confirm" in ssh:
        if not isinstance(ssh["confirm"], bool):
            raise ProfileError(f"{where}: [ssh-agent] confirm is true or false")
        p["ssh_agent"]["confirm"] = ssh["confirm"]
    p["tables"] = {k: v for k, v in data.items() if isinstance(v, dict) and k not in OWN_TABLES}
    return p


# ---------------------------------------------------------------------------------------------
# The old files

def first_word(f):
    """The first line, whitespace removed, as apod's `head -n1 | tr -d '[:space:]'`."""
    lines = f.read_text().splitlines()
    return re.sub(r"\s+", "", lines[0]) if lines else ""


def read_env_file(path):
    """KEY=VALUE lines, # comments skipped, outer quotes stripped from the value."""
    env = {}
    try:
        for line in Path(path).read_text().splitlines():
            k, eq, v = line.strip().partition("=")
            if eq and not k.startswith("#"):
                env[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return env


def from_old_files(pdir, warnings):
    p = empty()
    p["broker"] = False
    f = pdir / "broker.toml"
    if f.exists():
        p["broker"] = True
        try:
            data = tomllib.loads(f.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ProfileError(f"{f}: {e}")
        if "persona" in data and "role" not in data:
            data["role"] = data.pop("persona")
            warnings.append(f"{f}: persona = is now role = (it still works for now)")
        if "role" in data:
            p["role"] = data["role"] if isinstance(data["role"], str) else None
        for k in ("scope", "allow", "deny"):
            if k in data:
                p[k] = check_list(f, k, data[k], actions=k != "scope")
        p["tables"] = {k: v for k, v in data.items() if isinstance(v, dict)}
    env = read_env_file(pdir / "env")
    for k, key in GIT_KEYS.items():
        if env.get(k):
            p["git"][key] = env.pop(k)
        else:
            env.pop(k, None)
    for k in sorted(k for k in env if not ENV_NAME.fullmatch(k)):
        warnings.append(f"{pdir / 'env'}: {k!r} isn't an environment variable name; left out")
        del env[k]
    p["pod"]["env"] = env
    f = pdir / "mounts"
    if f.exists():
        for line in f.read_text().splitlines():
            w = line.split(None, 1)
            if not w or w[0].startswith("#"):
                continue
            if w[0] in ("rw", "ro"):
                if len(w) > 1:
                    p["pod"]["mounts"].append([w[0], expand(w[1].strip())])
            else:
                p["pod"]["mounts"].append(["ro", expand(w[0])])
    for k in ("image", "network"):
        if (pdir / k).exists():
            p["pod"][k] = first_word(pdir / k) or None
    f = pdir / "volumes"
    if f.exists():
        for line in f.read_text().splitlines():
            w = line.split(None, 1)
            if len(w) == 2 and not w[0].startswith("#"):
                p["pod"]["volumes"][w[0]] = w[1].strip()
    f = pdir / "connectors"
    if f.exists():
        for line in f.read_text().splitlines():
            if not line.strip().startswith("#"):
                p["pod"]["connectors"] += [c for c in re.split(r"[,\s]+", line) if c]
    if (pdir / "github").exists():
        p["token"]["profile"] = first_word(pdir / "github") or None
    if (pdir / "ssh-key").exists():
        k = first_word(pdir / "ssh-key")
        p["ssh_agent"]["key"] = expand(k) if k else None
    p["ssh_agent"]["confirm"] = (pdir / "ssh-confirm").exists()
    return p


# ---------------------------------------------------------------------------------------------
# The mount guard

def real(path):
    """Where a path really is, as realpath -m: symlinks followed, `..` resolved, may not exist."""
    return os.path.realpath(expand(path))


def inside(a, b):
    """Whether path a is b or inside it (both resolved)."""
    return a == b or a.startswith(b.rstrip("/") + "/")


def check_mount(mode, path, where):
    """Refuse a mount that would let a pod read or change apod's own configuration: one that is,
    contains or is inside the apod folder (~/.agent-pod: the profiles' logins, the broker's keys
    and state, and each profile.toml) or this checkout (the code the host runs), and a read-write
    mount of your home folder. A read-only mount of the apod folder's worklog/, history.jsonl or
    broker/log.jsonl, or of something inside worklog/, is fine. `where` says what asked for it."""
    r, c, p = real(str(root())), real(str(CHECKOUT)), real(path)
    if mode == "ro" and any(inside(p, f"{r}/{a}") for a in ("worklog", "history.jsonl", "broker/log.jsonl")):
        return
    for name, x, why in ((f"apod's folder {r}", r, "holds every profile's login, the broker's keys and state, and "
                          "the profiles' settings: a pod could read them, or rewrite its own configuration"),
                         (f"the apod checkout {c}", c, "is the code the broker runs on the host: a pod could "
                          "change it")):
        rel = "is" if p == x else "is inside" if inside(p, x) else "contains" if inside(x, p) else None
        if rel:
            raise ProfileError(f"{where}: {mode} mount {path}{'' if p == expand(path) else f' ({p})'} is refused: it "
                               f"{rel} {name}, which {why}. (Only read-only mounts of {r}/worklog, "
                               f"{r}/history.jsonl and {r}/broker/log.jsonl are allowed.)")
    home = real("~")
    if mode == "rw" and p == home:
        raise ProfileError(f"{where}: rw mount {path} is refused: it is your home folder, which gives the pod write "
                           f"access to everything of yours (ssh keys, shell startup files, apod's configuration). "
                           f"Mount the folders it needs")


# ---------------------------------------------------------------------------------------------

def old_files_in(pdir):
    return [f for f in OLD_FILES if (Path(pdir) / f).exists()]


def load(pdir, project=None, guard=True):
    """The profile in folder `pdir`, checked. Raises ProfileError. A mount of the launch's
    `project` isn't checked: the project is mounted anyway. The broker (guard=False) doesn't mount
    anything: apod checks the mounts when it launches."""
    pdir = Path(pdir)
    old = old_files_in(pdir)
    name = pdir.name
    f = pdir / "profile.toml"
    warnings = []
    if f.exists():
        try:
            data = tomllib.loads(f.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ProfileError(f"{f}: {e}")
        p = from_toml(data, f)
        p["format"], p["file"] = "profile.toml", str(f)
        if old:
            warnings.append(f"{name}: profile.toml is used; the old files ({', '.join(old)}) are ignored "
                            f"(apod migrate {name} moves them away)")
    else:
        p = from_old_files(pdir, warnings)
        p["format"] = "old files" if old else "none"
        p["file"] = str(pdir / "broker.toml") if (pdir / "broker.toml").exists() else ""
        if old:
            warnings.append(f"{name}: old profile files ({', '.join(old)}): apod migrate {name}")
    p["dir"] = str(pdir)
    p["warnings"] = warnings + p["warnings"]
    where = f if f.exists() else pdir / "mounts"
    for mode, path in p["pod"]["mounts"] if guard else []:
        if not (project and real(path) == real(project)):
            check_mount(mode, path, where)
    return p


def profile_dirs():
    """The profile folders in the apod root."""
    r = root()
    if not r.is_dir():
        return []
    return [d for d in sorted(r.iterdir()) if d.is_dir() and d.name not in NOT_PROFILES]


# ---------------------------------------------------------------------------------------------
# apod-broker profile --profile-dir DIR ...

USAGE = "usage: apod-broker profile --profile-dir DIR [--project DIR] [--ro PATH]... [--rw PATH]... [--json] [--quiet]"


def main(argv):
    pdir, project, quiet, cli, it = None, None, False, [], iter(argv)
    for a in it:
        if a == "--profile-dir":
            pdir = next(it, None)
        elif a == "--project":
            project = next(it, None)
        elif a in ("--ro", "--rw", "-m"):  # the launch's own mounts: checked like the profile's
            cli.append(("rw" if a == "--rw" else "ro", next(it, None)))
        elif a == "--json":
            pass  # the output is always JSON
        elif a in ("--quiet", "-q"):
            quiet = True
        else:
            print(USAGE, file=sys.stderr)
            return 2
    if not pdir:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        p = load(pdir, project)
        for mode, path in cli:
            if path is None:
                raise ProfileError(USAGE)
            if not (project and real(path) == real(project)):
                check_mount(mode, path, f"--{mode}")
    except ProfileError as e:
        print(f"apod: {e}", file=sys.stderr)
        return 2
    if not quiet:
        for w in p["warnings"]:
            print(f"apod: {w}", file=sys.stderr)
    print(json.dumps(p))
    return 0


# ---------------------------------------------------------------------------------------------
# apod migrate [PROFILE... | --all] [--yes]

def tq(s):
    """A TOML basic string."""
    return json.dumps(s, ensure_ascii=False)


def tlist(items):
    return "[" + ", ".join(tq(i) for i in items) + "]"


def tilde(path):
    home = os.environ.get("HOME") or str(Path.home())
    return "~" + path[len(home):] if path.startswith(home + "/") else path


def toml_of_old(pdir, p):
    """profile.toml's text for a profile read from the old files `p`: broker = false when there's
    no broker.toml, else broker.toml's own text (its comments too) with the new tables after it."""
    out = []
    bt = pdir / "broker.toml"
    if bt.exists():
        text = bt.read_text()
        # persona was role's old name: a top-level key, so only before the first [table]
        if "role" not in tomllib.loads(text):
            m = re.search(r"(?m)^\[", text)
            cut = m.start() if m else len(text)
            text = re.sub(r"(?m)^persona(\s*=)", r"role\1", text[:cut]) + text[cut:]
        out.append(text.rstrip() + "\n")
    else:
        out.append("broker = false\n")
    pod = p["pod"]
    lines = []
    for k in ("image", "network"):
        if pod[k]:
            lines.append(f"{k} = {tq(pod[k])}")
    for mode in ("ro", "rw"):
        paths = [tilde(path) for m, path in pod["mounts"] if m == mode]
        if paths:
            lines.append(f"{mode} = {tlist(paths)}")
    if pod["volumes"]:
        lines.append("volumes = { " + ", ".join(f"{tq(k)} = {tq(v)}" for k, v in pod["volumes"].items()) + " }")
    if pod["connectors"]:
        lines.append(f"connectors = {tlist(pod['connectors'])}")
    env = [f"{k} = {tq(v)}" for k, v in pod["env"].items()]
    if lines or env:
        out.append("\n[pod]\n" + "\n".join(lines) + ("\n" if lines else ""))
    if env:
        out.append("\n[pod.env]\n" + "\n".join(env) + "\n")
    if p["git"]:
        out.append("\n[git]   # the identity (GIT_AUTHOR_* and GIT_COMMITTER_* in the old env file)\n"
                   + "".join(f"{k} = {tq(p['git'][k])}\n" for k in ("name", "email", "committer_name", "committer_email")
                             if k in p["git"]))
    if p["token"]["profile"]:
        out.append(f"\n[token]   # without the broker: another profile's GitHub token\nprofile = {tq(p['token']['profile'])}\n")
    ssh = p["ssh_agent"]
    if ssh["key"] or ssh["confirm"]:
        out.append("\n[ssh-agent]\n" + (f"key = {tq(tilde(ssh['key']))}\n" if ssh["key"] else "")
                   + ("confirm = true\n" if ssh["confirm"] else ""))
    return "".join(out)


SAME = ("broker", "role", "scope", "allow", "deny", "pod", "git", "token", "ssh_agent", "tables")


def move_old(pdir, names):
    """Move the old files into <profile>/.old/ (nothing is deleted)."""
    old = pdir / ".old"
    old.mkdir(mode=0o700, exist_ok=True)
    for n in names:
        dest, i = old / n, 1
        while dest.exists():
            dest, i = old / f"{n}.{i}", i + 1
        os.replace(pdir / n, dest)


def migrate_one(pdir, ask, say):
    """Migrate one profile folder; returns whether it changed anything. `ask(question)` says
    whether to go on."""
    name, f = pdir.name, pdir / "profile.toml"
    old = old_files_in(pdir)
    if f.exists():
        load(pdir)  # fails if the profile.toml is no good
        if not old:
            say(f"{name}: already migrated: nothing to do")
            return False
        say(f"{name}: profile.toml exists, so the old files ({', '.join(old)}) are ignored.")
        if ask(f"Move them to {tilde(str(pdir))}/.old/?"):
            move_old(pdir, old)
            say(f"{name}: moved the old files to .old/")
            return True
        return False
    if not old:
        say(f"{name}: no settings files: nothing to migrate")
        return False
    p = from_old_files(pdir, [])
    text = toml_of_old(pdir, p)
    # The new file must mean what the old ones did: check before anything is written.
    try:
        new = from_toml(tomllib.loads(text), f)
    except (tomllib.TOMLDecodeError, ProfileError) as e:
        raise ProfileError(f"{name}: the old files don't convert to a profile.toml ({e}); fix them (the old files "
                           f"are untouched) and run apod migrate {name} again")
    # [pod] ro and rw keep each list's order, not the order of mixed lines: read-only mounts first.
    p["pod"]["mounts"] = [m for m in p["pod"]["mounts"] if m[0] == "ro"] + [m for m in p["pod"]["mounts"] if m[0] == "rw"]
    for k in SAME:
        if new[k] != p[k]:
            raise ProfileError(f"{name}: converting the old files changed {k} ({p[k]!r} became {new[k]!r}); "
                               f"nothing was written. Please report this")
    for mode, path in new["pod"]["mounts"]:
        check_mount(mode, path, f"{name}: mounts")
    say(f"{name}: {tilde(str(f))} would be:\n")
    say("".join("    " + l + "\n" for l in text.splitlines()))
    say(f"and the old files ({', '.join(old)}) would move to .old/ in {tilde(str(pdir))}.")
    if not ask("Write it?"):
        say(f"{name}: left as it was")
        return False
    # Not world-readable: [pod.env] may hold what an old env file kept (tokens, API keys).
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(text)
    move_old(pdir, old)
    say(f"{name}: wrote profile.toml; the old files are in {tilde(str(pdir))}/.old/")
    return True


def migrate_main(argv):
    names, everyone, yes = [], False, False
    for a in argv:
        if a == "--all":
            everyone = True
        elif a in ("--yes", "-y"):
            yes = True
        elif a.startswith("-"):
            print("usage: apod migrate [PROFILE | --all] [--yes]", file=sys.stderr)
            return 2
        else:
            names.append(a)
    if bool(names) == everyone or len(names) > 1:
        print("usage: apod migrate [PROFILE | --all] [--yes]", file=sys.stderr)
        return 2

    def say(text):
        print(text, file=sys.stderr)

    def ask(question):
        if yes:
            return True
        sys.stderr.write(f"{question} [Y/n] ")
        sys.stderr.flush()
        try:
            return input().strip().lower() in ("", "y", "yes")
        except EOFError:
            print(file=sys.stderr)
            return False
    dirs = profile_dirs() if everyone else [root() / names[0]]
    status = 0
    for d in dirs:
        if not d.is_dir():
            say(f"apod: no profile named {d.name}")
            status = 2
            continue
        try:
            migrate_one(d, ask, say)
        except ProfileError as e:
            say(f"apod: {e}")
            status = 2
    return status
