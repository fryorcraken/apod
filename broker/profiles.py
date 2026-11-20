"""A profile's configuration, read from one place.

A profile is one file, profiles/NAME.toml in apod's config folder, with its home in the data
folder (paths.py says where). In the legacy layout (~/.agent-pod), a profile is a folder,
<root>/NAME/, with its settings in profile.toml and its home in it; profiles made before
profile.toml existed are spread over several files there (broker.toml, env, mounts, image,
network, volumes, connectors, github, ssh-key, ssh-confirm), which are still read, the same way as
ever, when there is no profile.toml: `apod migrate` converts them. The old files, and .old/ where
migrate moves them, are the legacy layout's only.

Both `apod` (through `apod-broker profile`) and the broker (register) call load(), so they read
the same thing. This module imports nothing from the broker but paths.py: it works without it
running, and `apod-broker profile` doesn't load extensions.

load(NAME, or a profile's file or legacy folder) returns a dict (`apod-broker profile --profile
NAME` prints it as JSON):

    name, home       the profile's name and its home folder (the pod's $HOME)
    dir, file        its folder (the legacy layout; None otherwise); the file the settings come
                     from ("" if there is none)
    format           "profile.toml", "old files" or "none"
    broker           whether the broker acts for the pod (profile.toml: broker = ..., default
                     true; old files: broker.toml exists)
    role, scope, allow, deny   the broker's, as in profile.toml ("role" is None when not set)
    pod              image, network (None when not set), mounts [[mode, path], ...], volumes
                     {name: path}, connectors [...], env {NAME: value}, env_from [NAME, ...]
                     (host variables passed by name: their values are never in the file)
    git              name, email, committer_name, committer_email: the keys that are set
    token            profile: whose GitHub token (podman secret gh-<profile>) a launch without
                     the broker uses, None for the profile's own
    ssh_agent        key (None when not set), confirm
    tables           the other tables of profile.toml (or of broker.toml): the extensions'
    presets          the presets applied (--preset NAME), [{name, file, description, role, scope}]:
                     their pod settings are in pod, their role in role, their scope in scope
    warnings         things to tell the user, as text

A preset (presets/NAME.toml in the config folder, the new layout's only) is a [pod] table, a role
and a scope that any profile can launch with: see load_preset. `apod-broker presets` lists them.
"""
import json
import os
import re
import sys
import tomllib
from pathlib import Path

import paths

OLD_FILES = ("broker.toml", "env", "mounts", "image", "network", "volumes", "connectors", "github",
             "ssh-key", "ssh-confirm")
TOP_KEYS = {"broker": bool, "role": str, "scope": list, "allow": list, "deny": list}
# Tables the profile owns; any other table belongs to a broker extension.
OWN_TABLES = {"pod": {"image", "network", "ro", "rw", "volumes", "connectors", "env", "env_from"},
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


def expand(p):
    """A leading ~ is the home folder, as apod has always read it."""
    home = os.environ.get("HOME") or str(Path.home())
    return home + p[1:] if p == "~" or p.startswith("~/") else p


def empty():
    return {"broker": True, "role": None, "scope": [], "allow": [], "deny": [],
            "pod": {"image": None, "network": None, "mounts": [], "volumes": {}, "connectors": [], "env": {},
                    "env_from": []},
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


def pod_from_toml(pod, where):
    """The pod settings of a [pod] table (a profile's or a preset's), checked; its keys are known
    (check_table)."""
    out = empty()["pod"]
    for k in ("image", "network"):
        if k in pod:
            out[k] = check_str(f"{where}: [pod]", k, pod[k], nospace=True)
    for mode in ("ro", "rw"):
        if mode in pod:
            out["mounts"] += [[mode, expand(m)] for m in check_list(f"{where}: [pod]", mode, pod[mode])]
    if "connectors" in pod:
        out["connectors"] = check_list(f"{where}: [pod]", "connectors", pod["connectors"])
    if "volumes" in pod:
        vols = pod["volumes"]
        if not isinstance(vols, dict) or not all(isinstance(k, str) and isinstance(v, str) and k and v.startswith("/")
                                                 for k, v in vols.items()):
            raise ProfileError(f'{where}: [pod] volumes is a table of NAME = "/path in the pod"')
        out["volumes"] = dict(vols)
    if "env" in pod:
        env = pod["env"]
        if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
            raise ProfileError(f"{where}: [pod.env] is a table of NAME = \"value\" (values are strings: quote them)")
        for k in env:
            if not ENV_NAME.fullmatch(k):
                raise ProfileError(f"{where}: [pod.env] {k!r} isn't an environment variable name")
            if k in IDENTITY_ENV:
                raise ProfileError(f"{where}: [pod.env] {k}: the git identity is [git] name and email")
        out["env"] = dict(env)
    if "env_from" in pod:
        # Names of host variables, passed by name at launch: the file holds no value.
        names = check_list(f"{where}: [pod]", "env_from", pod["env_from"])
        for k in names:
            if not ENV_NAME.fullmatch(k):
                raise ProfileError(f"{where}: [pod] env_from: {k!r} isn't an environment variable name")
            if k in IDENTITY_ENV:
                raise ProfileError(f"{where}: [pod] env_from: {k}: the git identity is [git] name and email")
            if k in out["env"]:
                raise ProfileError(f"{where}: [pod] env_from: {k} is in [pod.env] too: one or the other")
        out["env_from"] = list(dict.fromkeys(names))
    return out


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
    p["pod"] = pod_from_toml(data.get("pod", {}), where)
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


# What each of apod's folders holds, for the mount guard's message.
GUARDED_WHY = {
    "apod's folder": "holds every profile's login, the broker's keys and state, and the profiles' settings: a pod "
                     "could read them, or rewrite its own configuration",
    "apod's config folder": "holds the profiles' settings, your roles and broker extensions: a pod could rewrite "
                            "its own configuration",
    "apod's data folder": "holds every profile's home, with its logins: a pod could read another profile's",
    "apod's state folder": "holds the broker's state (its approvals, sessions and keys): a pod could read or change it",
    "apod's runtime folder": "holds the broker's sockets: a pod could approve its own requests",
    "apod's legacy folder": "holds what an apod from before the new layout reads (its roles, broker extensions and "
                            "the profiles still in it, with their logins): a pod could read them, or rewrite that "
                            "apod's configuration",
    "the apod checkout": "is the code the broker runs on the host: a pod could change it",
}


# A profile's Claude transcripts, in <folder>/<profile>/ (paths.py: Layout.transcripts_in).
TRANSCRIPTS = ("home", ".claude", "projects")


def in_transcripts(p, folders):
    """Whether real path p is, or is inside, a profile's Claude transcripts: <profile>/home/.claude/projects
    in one of folders (Layout.transcripts_in, resolved)."""
    for x in folders:
        if p != x and inside(p, x):
            parts = os.path.relpath(p, x).split("/")
            if parts[1:4] == list(TRANSCRIPTS) and not paths.name_error(parts[0]):
                return True
    return False


def symlinks(path):
    """The symlinks that resolving path follows, each where it is (its folder resolved), as
    realpath -m follows them: missing parts are fine. None for a loop."""
    found = []

    def walk(base, rest):
        for name in rest.split("/"):
            if name in ("", "."):
                continue
            if name == "..":
                base = os.path.dirname(base)
                continue
            nxt = os.path.join(base, name)
            if not os.path.islink(nxt):
                base = nxt
                continue
            found.append(nxt)
            if len(found) > 40:
                raise RecursionError
            t = os.readlink(nxt)
            base = walk("/" if t.startswith("/") else base, t)
        return base
    p = expand(path)
    try:
        walk("/", p if p.startswith("/") else os.path.join(os.getcwd(), p))
    except RecursionError:
        return None
    return found


def check_mount(mode, path, where, lay=None):
    """Refuse a mount that would let a pod read or change apod's own configuration: one that is,
    contains or is inside one of apod's folders (paths.py: the profiles' settings and logins, the
    broker's keys, state and sockets) or this checkout (the code the host runs), one whose path
    goes through a symlink in them (a pod's, maybe: it writes its home and work log), and a
    read-write mount of your home folder. A read-only mount of the work logs (or one of them), the
    history, the broker's request log or a profile's Claude transcripts (or what's in them) is
    fine. `where` says what asked for it; `lay` is the layout (the current one by default)."""
    lay, p = lay or paths.layout(), real(path)
    guarded = [(label, real(str(x))) for label, x in lay.guarded + [("the apod checkout", CHECKOUT)]]
    links = symlinks(path)
    if links is None:
        raise ProfileError(f"{where}: {mode} mount {path} is refused: too many levels of symlinks (a loop?)")
    for link in links:
        for label, x in guarded:
            if inside(link, x):
                raise ProfileError(f"{where}: {mode} mount {path} is refused: it goes through {link}, a symlink in "
                                   f"{label} {x}, where pods write (their homes and work logs; the checkout, for "
                                   f"one that works on apod): one may have made it to give another pod whatever it "
                                   f"leads to. Mount where it leads instead, if that's what you mean")
    readable = [real(str(x)) for x in lay.readable]
    homes = [real(str(x)) for x in lay.transcripts_in]
    if mode == "ro" and (any(inside(p, x) for x in readable) or in_transcripts(p, homes)):
        return
    readable += [f"{x}/<profile>/{'/'.join(TRANSCRIPTS)}" for x in homes]
    for label, x in guarded:
        rel = "is" if p == x else "is inside" if inside(p, x) else "contains" if inside(x, p) else None
        if rel:
            raise ProfileError(f"{where}: {mode} mount {path}{'' if p == expand(path) else f' ({p})'} is refused: it "
                               f"{rel} {label} {x}, which {GUARDED_WHY[label]}. (Only read-only mounts of "
                               f"{', '.join(readable[:-1])} and {readable[-1]} are allowed.)")
    home = real("~")
    if mode == "rw" and p == home:
        raise ProfileError(f"{where}: rw mount {path} is refused: it is your home folder, which gives the pod write "
                           f"access to everything of yours (ssh keys, shell startup files, apod's configuration). "
                           f"Mount the folders it needs")


def check_project(project, lay=None):
    """Refuse a launch's project folder (mounted read-write) that is, contains or is inside one of
    apod's folders: `cd ~/.config && apod dev` would give the pod apod's configuration (its role,
    the extensions the broker runs). Unlike a --ro/--rw mount, the project may be the apod
    checkout: a pod that works on apod itself (SECURITY.md says what that gives it)."""
    lay, p = lay or paths.layout(), real(project)
    for label, x in lay.guarded:
        x = real(str(x))
        rel = "is" if p == x else "is inside" if inside(p, x) else "contains" if inside(x, p) else None
        if rel:
            raise ProfileError(f"the project {project} is refused: it {rel} {label} {x}, which {GUARDED_WHY[label]}. "
                               f"Launch apod in another folder")


# ---------------------------------------------------------------------------------------------
# Presets: presets/NAME.toml in apod's config folder, a kind of job any profile can launch with
# (apod PROFILE -p NAME). A profile is who acts; a preset is what the pod runs with (the image,
# mounts, network, volumes and environment) and, if it says so, the role and scope it works
# with. apod passes those to the broker as it does --role and --scope (the broker reads the
# profile's own file, not the presets), so they are no more than what the command line can
# give. Who acts (git identity, GitHub account, keys) and what else the profile allows or denies
# stay the profile's. Connectors aren't in presets either: they act as the profile's own Claude
# and Google logins.

PRESET_POD = OWN_TABLES["pod"] - {"connectors"}
PRESET_KEYS = ("description", "role", "scope")


def load_preset(name, lay=None):
    """The preset NAME, checked: {name, file, description, role, scope, pod} (role None when not
    set; pod as a profile's). Raises ProfileError (in the legacy layout too, which has no
    presets)."""
    lay = lay or paths.layout()
    try:
        f = lay.preset_file(name)
    except paths.PathError as e:
        raise ProfileError(str(e))
    if not f.is_file():
        known = lay.preset_names()
        raise ProfileError(f"no preset {name}: {f} doesn't exist" + (f" (presets: {' '.join(known)})" if known else ""))
    try:
        data = tomllib.loads(f.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ProfileError(f"{f}: {e}")
    for k, v in data.items():
        if k == "description":
            check_str(f, k, v)
        elif k == "role":
            check_str(f, k, v, nospace=True)
            if v == "none":
                raise ProfileError(f'{f}: role = "none": a preset doesn\'t turn the broker off (--no-broker does)')
        elif k == "scope":
            check_list(f, k, v)
        elif k != "pod":
            raise ProfileError(f"{f}: a preset has description, role, scope and [pod] only, not {k}: who acts, and "
                               f"what else it may do, are the profile's (apod PROFILE -p {name})")
    pod = data.get("pod", {})
    if not isinstance(pod, dict):
        raise ProfileError(f"{f}: [pod] is a table")
    for k in PRESET_KEYS:
        if k in pod:
            raise ProfileError(f"{f}: [pod] has {k} = ...: move it above [pod] (a key written after a [table] "
                               f"line belongs to that table)")
    if "connectors" in pod:
        raise ProfileError(f"{f}: [pod] connectors: a preset has none, since connectors act as the profile's own "
                           f"Claude and Google logins: set them in the profile's [pod] connectors, or with -c")
    check_table(f, "pod", pod, PRESET_POD)
    return {"name": name, "file": str(f), "description": data.get("description"), "role": data.get("role"),
            "scope": list(data.get("scope", [])), "pod": pod_from_toml(pod, f)}


def apply_preset(pod, pre):
    """Preset `pre`'s pod settings over `pod` (a profile's, or with earlier presets applied), in
    place: its image and network replace the ones set before; its mounts come after theirs (apod
    mounts a path once, with the last mode it's given); a volume replaces any volume at the same
    path in the pod; its environment replaces the same names, [pod.env] and env_from alike."""
    q = pre["pod"]
    for k in ("image", "network"):
        if q[k]:
            pod[k] = q[k]
    pod["mounts"] += q["mounts"]
    for vname, vpath in q["volumes"].items():
        for n in [n for n, p in pod["volumes"].items() if p == vpath]:
            del pod["volumes"][n]
        pod["volumes"][vname] = vpath
    for k, v in q["env"].items():
        pod["env"][k] = v
        if k in pod["env_from"]:
            pod["env_from"].remove(k)
    for k in q["env_from"]:
        pod["env"].pop(k, None)
        if k not in pod["env_from"]:
            pod["env_from"].append(k)


# ---------------------------------------------------------------------------------------------

def old_files_in(pdir):
    return [f for f in OLD_FILES if (Path(pdir) / f).exists()]


def locate(spec):
    """(name, folder, file, home) of a profile given by its name (paths.py says where it is), its
    file (a path ending in .toml that isn't a folder; folder None) or its folder (any other path,
    with its home in it: the legacy layout)."""
    s = str(spec)
    if "/" not in s:
        try:
            p = paths.layout().profile(s)
        except paths.PathError as e:
            raise ProfileError(str(e))
        return s, p["dir"] and Path(p["dir"]), Path(p["file"]), Path(p["home"])
    path = Path(s)
    if s.endswith(".toml") and not path.is_dir():
        name = path.name[:-5]
        return name, None, path, None if paths.name_error(name) else Path(paths.layout().profile(name)["home"])
    return path.name, path, path / "profile.toml", path / "home"


def load(spec, project=None, guard=True, presets=()):
    """The profile `spec` (a name, or a path: see locate), checked, with the `presets` (names)
    applied over it, in order: their pod settings over its pod (apply_preset), the last role one
    sets over its role, their scope after its scope. Raises ProfileError. A mount of the launch's
    `project` isn't checked: the project is mounted anyway. The broker (guard=False, no presets)
    doesn't mount anything: apod checks the mounts when it launches, and passes the presets' role
    and scope to the broker as --role and --scope."""
    name, pdir, f, home = locate(spec)
    old = old_files_in(pdir) if pdir else []  # the old files: a legacy profile folder's only
    warnings = []
    if pdir is None and not f.is_file():
        raise ProfileError(f"no profile {name}: {f} doesn't exist")
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
    p["name"], p["dir"], p["home"] = name, pdir and str(pdir), home and str(home)
    p["warnings"] = warnings + p["warnings"]
    where = f if f.exists() else pdir / "mounts"
    for mode, path in p["pod"]["mounts"] if guard else []:
        if not (project and real(path) == real(project)):
            check_mount(mode, path, where)
    p["presets"] = []
    for n in presets:
        pre = load_preset(n)
        for mode, path in pre["pod"]["mounts"] if guard else []:
            if not (project and real(path) == real(project)):
                check_mount(mode, path, pre["file"])
        apply_preset(p["pod"], pre)
        if pre["role"]:
            p["role"] = pre["role"]
        p["scope"] += [s for s in pre["scope"] if s not in p["scope"]]
        p["presets"].append({k: pre[k] for k in ("name", "file", "description", "role", "scope")})
    return p


def names():
    """The profiles there are, by name."""
    return paths.layout().profile_names()


# ---------------------------------------------------------------------------------------------
# apod-broker profile --profile NAME ...

USAGE = ("usage: apod-broker profile [--profile NAME | --profile-file FILE | --profile-dir DIR] [--preset NAME]... "
         "[--project DIR] [--ro PATH]... [--rw PATH]... [--json] [--quiet]\n"
         "(with no profile, it only checks the --ro and --rw mounts)")


def main(argv):
    spec, project, quiet, cli, presets, it = None, None, False, [], [], iter(argv)
    for a in it:
        if a == "--preset":
            n = next(it, None)
            if n is None:
                print(USAGE, file=sys.stderr)
                return 2
            presets.append(n)
            continue
        if a in ("--profile", "--profile-file", "--profile-dir"):
            spec = next(it, None)
            # A folder given as a name (no "/") is a folder still.
            if a == "--profile-dir" and spec and "/" not in spec:
                spec = f"./{spec}"
            elif a == "--profile-file" and spec and not spec.endswith(".toml"):
                print(f"apod: --profile-file {spec}: a profile's file ends in .toml", file=sys.stderr)
                return 2
            elif a == "--profile-file" and spec and "/" not in spec:
                spec = f"./{spec}"
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
    if not spec and (not cli or presets):
        print(USAGE, file=sys.stderr)
        return 2
    try:
        if project:
            check_project(project)  # mounted read-write
        p = load(spec, project, presets=presets) if spec else {"warnings": []}
        for mode, path in cli:
            if path is None:
                raise ProfileError(USAGE)
            if not (project and real(path) == real(project)):
                check_mount(mode, path, f"--{mode}")
    except (ProfileError, paths.PathError) as e:
        print(f"apod: {e}", file=sys.stderr)
        return 2
    if not quiet:
        for w in p["warnings"]:
            print(f"apod: {w}", file=sys.stderr)
    print(json.dumps(p))
    return 0


def presets_main(argv):
    """apod presets (apod-broker presets): each preset, with its description and what it sets."""
    if argv:
        print("usage: apod presets", file=sys.stderr)
        return 2
    lay = paths.layout()
    if lay.presets is None:
        print("apod: presets are in the new layout only (apod migrate --layout)", file=sys.stderr)
        return 2
    names = lay.preset_names()
    if not names:
        print(f"apod: no presets: {tilde(str(lay.presets))}/NAME.toml makes one (docs/reference.md#presets)",
              file=sys.stderr)
        return 0
    for n in names:
        try:
            pre = load_preset(n, lay)
        except ProfileError as e:
            print(f"{n:<20} invalid: {e}")
            continue
        q = pre["pod"]
        sets = [f"role {pre['role']}"] if pre["role"] else []
        sets += [f"scope {' '.join(pre['scope'])}"] if pre["scope"] else []
        sets += [f"image {q['image']}"] if q["image"] else []
        sets += [f"network {q['network']}"] if q["network"] else []
        for what, k in (("mount", "mounts"), ("volume", "volumes"), ("variable", "env")):
            c = len(q[k]) + (len(q["env_from"]) if k == "env" else 0)
            if c:
                sets.append(f"{c} {what}{'s' if c > 1 else ''}")
        print(f"{n:<20} {', '.join(sets) or 'nothing'}" + (f"  ({pre['description']})" if pre["description"] else ""))
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


def convert_old(pdir, again):
    """(profile.toml's text, the profile it reads as) for a folder's old files, checked: it must
    mean what the old files did. Raises ProfileError, saying to run `again` once they're fixed."""
    name = pdir.name
    p = from_old_files(pdir, [])
    text = toml_of_old(pdir, p)
    try:
        new = from_toml(tomllib.loads(text), pdir / "profile.toml")
    except (tomllib.TOMLDecodeError, ProfileError) as e:
        raise ProfileError(f"{name}: the old files don't convert to a profile.toml ({e}); fix them (nothing was "
                           f"changed) and run {again} again")
    # [pod] ro and rw keep each list's order, not the order of mixed lines: read-only mounts first.
    p["pod"]["mounts"] = [m for m in p["pod"]["mounts"] if m[0] == "ro"] + [m for m in p["pod"]["mounts"] if m[0] == "rw"]
    for k in SAME:
        if new[k] != p[k]:
            raise ProfileError(f"{name}: converting the old files changed {k} ({p[k]!r} became {new[k]!r}); "
                               f"nothing was changed. Please report this")
    return text, new


def prompts(yes):
    """(say, ask) for the migrations: messages on stderr, and a [Y/n] question (yes: always yes;
    no answer: no)."""
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
    return say, ask


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
    text, new = convert_old(pdir, f"apod migrate {name}")
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
    say, ask = prompts(yes)
    lay = paths.layout()
    if not lay.legacy:
        say(f"apod: in this layout a profile is one file already ({lay.profiles}/NAME.toml): nothing to migrate")
        return 0
    try:
        dirs = [Path(lay.profile(n)["dir"]) for n in (lay.profile_names() if everyone else names)]
    except paths.PathError as e:
        say(f"apod: {e}")
        return 2
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
