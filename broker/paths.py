"""Where apod keeps its files, decided in one place.

apod (through `apod-broker paths --json`), profiles.py, the broker, its extensions and the tests ask
here; nothing else builds a path in apod's folders by hand. Like profiles.py, this module uses the
standard library only and imports nothing of apod's.

The layout follows the XDG Base Directory specification, on macOS too:

    config   $APOD_CONFIG_DIR, else ${XDG_CONFIG_HOME:-~/.config}/apod       what you write; versionable
             config.toml, roles.toml, extensions/, profiles/NAME/ (profile.toml,
             google-credentials.json)
    data     $APOD_DATA_DIR, else ${XDG_DATA_HOME:-~/.local/share}/apod      what you'd back up
             profiles/NAME/home/ (a pod's $HOME), worklog/<session>/
    state    $APOD_STATE_DIR, else ${XDG_STATE_HOME:-~/.local/state}/apod    what the tools write
             history.jsonl, broker/ (log.jsonl, grants.json, sessions/, broker.log, gh-api/,
             home/, tmp/, its sockets), selinux

The legacy layout is one folder, ~/.agent-pod, with everything in it (a profile is a folder there,
the sockets are in its broker/). It's used when APOD_ROOT=DIR is set (the legacy layout rooted at
DIR), or when none of the new config, data and state folders exists and ~/.agent-pod does. `apod
migrate --layout` moves it to the new layout. All of it is the `legacy` branch of Layout below.
"""
import json
import os
import re
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
    """apod's folders and files for an environment (os.environ by default)."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.config = _dir(env, "APOD_CONFIG_DIR", "XDG_CONFIG_HOME", ".config")
        self.data = _dir(env, "APOD_DATA_DIR", "XDG_DATA_HOME", ".local/share")
        self.state = _dir(env, "APOD_STATE_DIR", "XDG_STATE_HOME", ".local/state")
        self.legacy_root = Path(env["APOD_ROOT"]) if env.get("APOD_ROOT") else _home(env) / ".agent-pod"
        self.legacy = bool(env.get("APOD_ROOT")) or (
            not any(d.exists() for d in (self.config, self.data, self.state)) and self.legacy_root.is_dir())
        self.notice = None
        if self.legacy:
            # The legacy layout: one folder for all of it, a profile a folder in it, the sockets in
            # its broker/. Deleting this branch (and the two lines above) removes it.
            r = self.legacy_root
            self.notice = NOTICE + (", then unset APOD_ROOT" if env.get("APOD_ROOT") else "")
            self.config = self.data = self.state = r
            self.config_file = r / "broker.toml"
            self.extensions = r / "broker-extensions"
            self.old_roles = r / "personas.toml"  # before roles were called roles
            self.worklog = r / "worklog"
            self.history = r / "history.jsonl"
            self.broker = r / "broker"
            self.selinux = r / "selinux"
            self.profiles = r
            self.guarded = [("apod's folder", r)]
            self._names = lambda: [d.name for d in sorted(r.iterdir()) if d.is_dir()] if r.is_dir() else []
            self._profile = lambda n, pid: {
                "dir": r / n, "file": r / n / "profile.toml", "home": r / n / "home",
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
            self.guarded = [("apod's config folder", self.config), ("apod's data folder", self.data),
                            ("apod's state folder", self.state)]
            p = self.profiles
            self._names = lambda: [d.name for d in sorted(p.iterdir()) if d.is_dir()] if p.is_dir() else []
            self._profile = lambda n, pid: {
                "dir": p / n, "file": p / n / "profile.toml", "home": self.data / "profiles" / n / "home",
                "google_credentials": p / n / "google-credentials.json",
                "exists": (p / n).is_dir(), "ssh_agent_dir": self.data / "profiles" / n / "agent",
                **({"ssh_agent_sock": self.data / "profiles" / n / "agent" / f"ssh-{pid}.sock",
                    "ssh_agent_mount": self.data / "profiles" / n / "agent"} if pid else {})}
        self.roles = self.config / "roles.toml"
        self.log = self.broker / "log.jsonl"              # every request the broker decided
        self.daemon_log = self.broker / "broker.log"       # the broker's own output
        self.grants = self.broker / "grants.json"          # approvals saved for roles
        self.sessions_state = self.broker / "sessions"     # <id>.json: each session's saved state
        self.sessions_run = self.broker / "sessions"       # <id>/: each session's folder, mounted in its pod
        self.admin_sock = self.broker / "admin.sock"
        self.agent_sock = self.broker / "agent.sock"       # the broker's ssh-agent
        # Read-only mounts that are fine although they're in a guarded folder.
        self.readable = [self.worklog, self.history, self.log]

    @property
    def layout(self):
        return "legacy" if self.legacy else "xdg"

    def profile_names(self):
        """The profiles there are, by name."""
        return [n for n in self._names() if not name_error(n)]

    def profile(self, name, pid=None):
        """A profile's paths (whether or not it exists): its folder and settings file, its home,
        its Google OAuth client, its per-run ssh-agent socket (with pid). Raises PathError for a
        name that can't be a profile's."""
        why = name_error(name)
        if why:
            raise PathError(why)
        return dict(self._profile(name, pid), name=name)

    def as_json(self, profile=None, pid=None):
        def s(v):
            return {k: s(x) for k, x in v.items()} if isinstance(v, dict) else str(v) if isinstance(v, Path) else v
        out = {k: getattr(self, k) for k in (
            "layout", "legacy", "notice", "config", "data", "state", "config_file", "roles", "extensions",
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
         "       apod-broker paths --check-name NAME")


def main(argv):
    """apod-broker paths: where apod's files are. --json for apod; --check-name NAME says why NAME
    can't be a profile's (exit 2)."""
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
    for k in ("config", "data", "state"):
        print(f"{k:<8} {out[k]}")
    for k in ("config_file", "roles", "extensions", "profiles", "worklog", "history", "broker", "admin_sock"):
        print(f"  {k:<12} {out[k]}")
    for n, p in out["all_profiles"].items():
        print(f"  profile {n}: {p['file']}, home {p['home']}")
    if profile is not None:
        print(f"  profile {profile}: {out['profile']['file']}, home {out['profile']['home']}"
              + ("" if out["profile"]["exists"] else " (doesn't exist)"))
    return 0
