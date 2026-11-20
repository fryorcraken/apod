"""Where apod keeps its files, decided in one place.

apod (through `apod-broker paths --json`), profiles.py, the broker, its extensions and the tests ask
here; nothing else builds a path in apod's folders by hand. Like profiles.py, this module uses the
standard library only and imports nothing of apod's.

The layout follows the XDG Base Directory specification, on macOS too:

    config   $APOD_CONFIG_DIR, else ${XDG_CONFIG_HOME:-~/.config}/apod       what you write; versionable
             config.toml, roles.toml, extensions/, profiles/NAME.toml,
             profiles/NAME.google-credentials.json
    data     $APOD_DATA_DIR, else ${XDG_DATA_HOME:-~/.local/share}/apod      what you'd back up
             profiles/NAME/home/ (a pod's $HOME), worklog/<session>/
    state    $APOD_STATE_DIR, else ${XDG_STATE_HOME:-~/.local/state}/apod    what the tools write
             history.jsonl, broker/ (log.jsonl, grants.json, sessions/<id>.json, broker.log,
             gh-api/, home/, tmp/), selinux
    runtime  $APOD_RUNTIME_DIR, else $XDG_RUNTIME_DIR/apod, else $TMPDIR/apod-$UID: sockets only,
             mode 0700, gone at logout: admin.sock, agent.sock, sessions/<id>/ (what a pod
             mounts: broker.sock...), ssh-NAME-PID.sock

The legacy layout is one folder, ~/.agent-pod, with everything in it (a profile is a folder there,
the sockets are in its broker/). It's used when APOD_ROOT=DIR is set (the legacy layout rooted at
DIR), or when none of the new config, data and state folders exists and ~/.agent-pod does. `apod
migrate --layout` moves it to the new layout. All of it is the `legacy` branch of Layout below,
and the lines marked (legacy).
"""
import json
import os
import re
import stat
import sys
from pathlib import Path

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
COMMANDS = ("new", "migrate", "resume", "history", "ps", "profiles", "token", "google-auth", "selinux", "broker", "gh")
LEGACY_FOLDERS = ("broker", "broker-extensions", "worklog")  # folders in ~/.agent-pod that aren't profiles
NOTICE = "legacy layout: apod migrate --layout"


class PathError(Exception):
    pass


def _home(env):
    return Path(env.get("HOME") or Path.home())


def _dir(env, own, xdg, default):
    """$own, else $xdg/apod (only an absolute one counts, as the XDG specification says), else
    ~/default/apod."""
    if env.get(own):
        if not os.path.isabs(env[own]):
            raise PathError(f"{own} is a relative path ({env[own]}): give an absolute one")
        return Path(env[own])
    x = env.get(xdg, "")
    return (Path(x) if os.path.isabs(x) else _home(env) / default) / "apod"


def runtime_dir(env):
    """$APOD_RUNTIME_DIR, else $XDG_RUNTIME_DIR/apod, else $TMPDIR/apod-$UID (see ensure_private)."""
    if env.get("APOD_RUNTIME_DIR"):
        return _dir(env, "APOD_RUNTIME_DIR", "", "")
    if os.path.isabs(env.get("XDG_RUNTIME_DIR", "")):
        return Path(env["XDG_RUNTIME_DIR"]) / "apod"
    return Path(env.get("TMPDIR") or "/tmp") / f"apod-{os.getuid()}"


def ensure_private(d):
    """Create folder d (mode 0700) if it's missing, and refuse one that isn't a folder of yours
    with mode 0700: in a shared $TMPDIR, another user could have made it first."""
    d = Path(d)
    if not os.path.lexists(d):
        d.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise PathError(f"apod's runtime folder {d} isn't a folder (a symlink, or a file): remove it, or set "
                        f"APOD_RUNTIME_DIR")
    if st.st_uid != os.getuid():
        raise PathError(f"apod's runtime folder {d} belongs to someone else (uid {st.st_uid}): set APOD_RUNTIME_DIR "
                        f"(or XDG_RUNTIME_DIR) to a folder of yours")
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise PathError(f"apod's runtime folder {d} has mode {stat.S_IMODE(st.st_mode):04o}, not 0700: chmod 700 {d}")
    return d


def name_error(name):
    """Why NAME can't be a profile's name, or None."""
    if not NAME.fullmatch(name or ""):
        return f"{name!r} isn't a profile name: letters, digits, '.', '_' and '-', starting with a letter or digit"
    if name in COMMANDS or name in LEGACY_FOLDERS:
        return f"'{name}' is an apod command or folder, not a profile name"
    if name.endswith(".google-credentials"):
        return f"'{name}' ends in .google-credentials, which names a profile's Google OAuth client"
    return None


class Layout:
    """apod's folders and files for an environment (os.environ by default). force_new: the new
    layout's, even where the legacy one is in use (apod migrate --layout's target)."""

    def __init__(self, env=None, force_new=False):
        env = os.environ if env is None else env
        self.config = _dir(env, "APOD_CONFIG_DIR", "XDG_CONFIG_HOME", ".config")
        self.data = _dir(env, "APOD_DATA_DIR", "XDG_DATA_HOME", ".local/share")
        self.state = _dir(env, "APOD_STATE_DIR", "XDG_STATE_HOME", ".local/state")
        self.runtime = runtime_dir(env)
        self.own_runtime = True  # a folder of its own: made private here (ensure_runtime)
        self.legacy_root = Path(env["APOD_ROOT"]) if env.get("APOD_ROOT") else _home(env) / ".agent-pod"  # (legacy)
        self.legacy = not force_new and (bool(env.get("APOD_ROOT")) or (  # (legacy)
            not any(d.exists() for d in (self.config, self.data, self.state)) and self.legacy_root.is_dir()))
        self.notice = None
        if self.legacy:
            # The legacy layout: one folder for all of it, a profile a folder in it, the sockets in
            # its broker/. Deleting this branch (and the lines marked legacy) removes it.
            r = self.legacy_root
            self.notice = NOTICE + (", then unset APOD_ROOT" if env.get("APOD_ROOT") else "")
            self.config = self.data = self.state = r
            self.runtime, self.own_runtime = r / "broker", False  # the broker's state folder, made private by the broker
            self.config_file = r / "broker.toml"
            self.extensions = r / "broker-extensions"
            self.old_roles = r / "personas.toml"  # before roles were called roles
            self.worklog = r / "worklog"
            self.history = r / "history.jsonl"
            self.broker = r / "broker"
            self.selinux = r / "selinux"
            self.profiles = r
            self.homes = r  # <profile>/home: each profile's
            self.guarded = [("apod's folder", r)]
            self._names = lambda: [d.name for d in sorted(r.iterdir()) if d.is_dir()] if r.is_dir() else []
            self._profile = lambda n, pid: {
                "dir": r / n, "file": r / n / "profile.toml", "home": self.homes / n / "home",
                "google_credentials": r / n / "google-credentials.json",
                "exists": (r / n).is_dir(), "ssh_agent_dir": r / n / "agent",
                **({"ssh_agent_sock": r / n / "agent" / f"ssh-{pid}.sock", "ssh_agent_mount": r / n / "agent"}
                   if pid else {})}
        else:
            self.config_file = self.config / "config.toml"
            self.extensions = self.config / "extensions"
            self.old_roles = None
            self.worklog = self.data / "worklog"
            self.history = self.state / "history.jsonl"
            self.broker = self.state / "broker"
            self.selinux = self.state / "selinux"
            self.profiles = self.config / "profiles"
            self.homes = self.data / "profiles"  # <profile>/home: each profile's
            self.guarded = [("apod's config folder", self.config), ("apod's data folder", self.data),
                            ("apod's state folder", self.state), ("apod's runtime folder", self.runtime),
                            # (legacy) the old apod's, while one runs beside this one: its settings and logins
                            ("apod's legacy folder", self.legacy_root)]
            p = self.profiles
            self._names = lambda: [f.name[:-5] for f in sorted(p.glob("*.toml")) if f.is_file()]
            self._profile = lambda n, pid: {
                "dir": None, "file": p / f"{n}.toml", "home": self.homes / n / "home",
                "google_credentials": p / f"{n}.google-credentials.json",
                "exists": (p / f"{n}.toml").is_file(), "ssh_agent_dir": self.runtime,
                **({"ssh_agent_sock": self.runtime / f"ssh-{n}-{pid}.sock",
                    "ssh_agent_mount": self.runtime / f"ssh-{n}-{pid}.sock"} if pid else {})}
        if not self.legacy and self.legacy_root.is_dir():  # (legacy) left beside the new layout: say so
            r = self.legacy_root
            left = [d.name for d in sorted(r.iterdir()) if d.is_dir() and not name_error(d.name)]
            self.notice = (f"{r} still has profiles of the legacy layout ({', '.join(left)}), which only the old apod "
                           f"launches: apod migrate --layout PROFILE moves one" if left else
                           f"{r} is there too, and isn't used: apod migrate --layout moves what's in it")
        self.roles = self.config / "roles.toml"
        self.log = self.broker / "log.jsonl"              # every request the broker decided
        self.daemon_log = self.broker / "broker.log"       # the broker's own output
        self.grants = self.broker / "grants.json"          # approvals saved for roles
        self.sessions_state = self.broker / "sessions"     # <id>.json: each session's saved state
        self.sessions_run = self.runtime / "sessions"      # <id>/: each session's folder, mounted in its pod
        self.admin_sock = self.runtime / "admin.sock"
        self.agent_sock = self.runtime / "agent.sock"      # the broker's ssh-agent
        # Read-only mounts that are fine although they're in a guarded folder: these, and a
        # profile's Claude transcripts (<profile>/home/.claude/projects in a folder of
        # transcripts_in; profiles.py checks those, since the profile's pods write there).
        self.readable = [self.worklog, self.history, self.log]
        self.transcripts_in = [self.homes]
        if not self.legacy and self.legacy_root.is_dir():  # (legacy) the old apod's too, while one runs beside this one
            r = self.legacy_root
            self.readable += [r / "worklog", r / "history.jsonl", r / "broker" / "log.jsonl"]
            self.transcripts_in.append(r)

    def ensure_runtime(self):
        """The runtime folder, made (mode 0700) if it's missing; PathError if it isn't private
        (ensure_private). The legacy layout's is the broker's folder, which the broker makes."""
        return ensure_private(self.runtime) if self.own_runtime else self.runtime

    @property
    def layout(self):
        return "legacy" if self.legacy else "xdg"

    def profile_names(self):
        """The profiles there are, by name."""
        return [n for n in self._names() if not name_error(n)]

    def profile(self, name, pid=None):
        """A profile's paths (whether or not it exists): its settings file, its folder (the
        legacy layout; None otherwise), its home, its Google OAuth client, its per-run ssh-agent
        socket (with pid). Raises PathError for a name that can't be a profile's."""
        why = name_error(name)
        if why:
            raise PathError(why)
        return dict(self._profile(name, pid), name=name)

    def as_json(self, profile=None, pid=None):
        def s(v):
            return {k: s(x) for k, x in v.items()} if isinstance(v, dict) else str(v) if isinstance(v, Path) else v
        out = {k: getattr(self, k) for k in (
            "layout", "legacy", "notice", "config", "data", "state", "runtime", "config_file", "roles", "extensions",
            "profiles", "worklog", "history", "broker", "selinux", "log", "grants", "sessions_state",
            "sessions_run", "admin_sock", "agent_sock")}
        out["legacy_root"] = self.legacy_root
        out["all_profiles"] = {n: self.profile(n) for n in self.profile_names()}
        if profile is not None:
            out["profile"] = self.profile(profile, pid)
        return s(out)


def layout(env=None):
    return Layout(env)


USAGE = ("usage: apod-broker paths [--json] [--profile NAME [--pid PID]]\n"
         "       apod-broker paths --check-name NAME | --ensure-runtime")


def main(argv):
    """apod-broker paths: where apod's files are. --json for apod; --check-name NAME says why NAME
    can't be a profile's (exit 2); --ensure-runtime makes the runtime folder (exit 2 if it isn't
    private)."""
    js, profile, pid, it = False, None, None, iter(argv)
    try:
        for a in it:
            if a == "--json":
                js = True
            elif a == "--profile":
                profile = next(it)
            elif a == "--pid":
                pid = next(it)
            elif a == "--check-name":
                why = name_error(next(it))
                if why:
                    print(f"apod: {why}", file=sys.stderr)
                    return 2
                return 0
            elif a == "--ensure-runtime":
                layout().ensure_runtime()
                return 0
            else:
                raise StopIteration
    except StopIteration:
        print(USAGE, file=sys.stderr)
        return 2
    except PathError as e:
        print(f"apod: {e}", file=sys.stderr)
        return 2
    try:
        out = layout().as_json(profile, pid)
    except PathError as e:
        print(f"apod: {e}", file=sys.stderr)
        return 2
    if js:
        print(json.dumps(out))
        return 0
    print(f"layout   {out['layout']}" + (f" ({out['legacy_root']}; {out['notice']})" if out["legacy"] else ""))
    for k in ("config", "data", "state", "runtime"):
        print(f"{k:<8} {out[k]}")
    for k in ("config_file", "roles", "extensions", "profiles", "worklog", "history", "broker", "admin_sock"):
        print(f"  {k:<12} {out[k]}")
    for n, p in out["all_profiles"].items():
        print(f"  profile {n}: {p['file']}, home {p['home']}")
    if profile is not None:
        print(f"  profile {profile}: {out['profile']['file']}, home {out['profile']['home']}"
              + ("" if out["profile"]["exists"] else " (doesn't exist)"))
    return 0
