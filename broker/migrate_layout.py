"""apod migrate --layout [--yes]: the legacy layout (~/.agent-pod, or APOD_ROOT) to the new one.

It shows what moves where, asks, and then moves: each profile's settings to profiles/NAME.toml in
the config folder (old profile files converted on the way, with the same check as `apod migrate`),
its home to the data folder, its Google client beside its file; broker.toml to config.toml,
roles.toml and broker-extensions/ (now extensions/) to the config folder; worklog/ to the data
folder; history.jsonl, selinux and the broker's state (its log, approvals, saved sessions, the
gh-api CA, its own home) to the state folder. Nothing is deleted: what isn't moved (the old files,
.old/, stale sockets) stays in the legacy folder, which is renamed ~/.agent-pod.migrated at the
end. It refuses while a broker or a pod of the legacy layout runs, and while a file it would move
to is there already with something else in it. Run again, it finishes what's left; once done,
there's nothing to do.
"""
import os
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

import paths
import profiles

USAGE = "usage: apod migrate --layout [--yes]"


def tilde(p):
    return profiles.tilde(str(p))


def is_socket(p):
    try:
        return stat.S_ISSOCK(os.lstat(p).st_mode)
    except OSError:
        return False


def broker_running(root):
    """Whether a broker of the legacy layout answers on its admin socket."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(root / "broker/admin.sock"))
        return True
    except OSError:
        return False
    finally:
        s.close()


def running_pods(root):
    """apod's running pods that mount something from the legacy folder `root` (their names); []
    when there's no podman. A pod podman can't describe counts as one."""
    def podman(*args):
        try:
            return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return None
    r = podman("ps", "-q", "--filter", "label=apod.profile")
    ids = r.stdout.split() if r and r.returncode == 0 else []
    if not ids:
        return []
    r = podman("inspect", "--format", "{{.Name}}{{range .Mounts}} {{.Source}}{{end}}", *ids)
    if not r or r.returncode != 0:
        return ids
    top, out = os.path.realpath(root), []
    for line in r.stdout.splitlines():
        name, *sources = line.split()
        if any(profiles.inside(os.path.realpath(s), top) for s in sources):
            out.append(name)
    return out


def profile_text(pdir):
    """What profiles/NAME.toml gets for a legacy profile folder: its profile.toml's text, or its
    old files converted (checked as `apod migrate` checks them), or broker = false for a folder
    with no settings at all (which launches without the broker; an empty file would have it)."""
    f = pdir / "profile.toml"
    if f.exists():
        profiles.load(pdir, guard=False)  # fails if it's no good
        return f.read_text(), "its profile.toml"
    old = profiles.old_files_in(pdir)
    if not old:
        return "broker = false   # a profile folder without settings: no broker, as before\n", "no settings: broker = false"
    text, _ = profiles.convert_old(pdir, "apod migrate --layout")
    return text, f"converted from {', '.join(old)}"


def plan(root, new):
    """The moves, [(source, target, how)] with how "move", or ("write", text, why) for a profile's
    file, written from its folder's settings (which stay where they are, so that a second run
    finds the same text); what stays in the legacy folder; the new folders."""
    ops, stays = [], []
    conf, data, state = new.config, new.data, new.state
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name in paths.LEGACY_FOLDERS:
            continue
        if paths.name_error(d.name):
            stays.append(f"{d.name}/ (not a profile name)")
            continue
        p = new.profile(d.name)
        text, why = profile_text(d)
        ops.append((d / "profile.toml" if why == "its profile.toml" else d, p["file"], ("write", text, why)))
        for src, dst in ((d / "home", p["home"]), (d / "google-credentials.json", p["google_credentials"])):
            if src.exists():
                ops.append((src, dst, "move"))
        for x in sorted(d.iterdir()):
            if x.name not in ("home", "google-credentials.json", "profile.toml"):
                stays.append(f"{d.name}/{x.name}")
    roles = root / "roles.toml" if (root / "roles.toml").exists() else root / "personas.toml"
    for src, dst in ((root / "broker.toml", new.config_file), (roles, new.roles),
                     (root / "broker-extensions", new.extensions), (root / "worklog", new.worklog),
                     (root / "history.jsonl", new.history), (root / "selinux", new.selinux)):
        if src.exists():
            ops.append((src, dst, "move"))
    if (root / "roles.toml").exists() and (root / "personas.toml").exists():
        stays.append("personas.toml (roles.toml is read instead)")
    b = root / "broker"
    if b.is_dir():
        for x in sorted(b.iterdir()):
            if x.name == "sessions" and x.is_dir():
                for s in sorted(x.iterdir()):
                    if s.suffix == ".json" and s.is_file():
                        ops.append((s, new.sessions_state / s.name, "move"))  # a session's saved state
                    else:
                        stays.append(f"broker/sessions/{s.name}")  # a pod's folder: runtime, made again
            elif is_socket(x):
                stays.append(f"broker/{x.name}")
            else:
                ops.append((x, new.broker / x.name, "move"))
    for x in sorted(root.iterdir()):
        if not x.is_dir() and x.name not in ("broker.toml", "roles.toml", "personas.toml", "history.jsonl", "selinux"):
            stays.append(x.name)
    return [(Path(s), Path(t), how) for s, t, how in ops], stays, (conf, data, state)


def same_text(target, text):
    try:
        return target.read_text() == text
    except OSError:
        return False


def main(argv):
    yes = False
    for a in argv:
        if a == "--layout":
            continue
        if a in ("--yes", "-y"):
            yes = True
        else:
            print(USAGE, file=sys.stderr)
            return 2
    say, ask = profiles.prompts(yes)
    try:
        cur = paths.layout()
        root = cur.legacy_root
        env = {k: v for k, v in os.environ.items() if k != "APOD_ROOT"}
        new = paths.Layout(env, force_new=True)
    except paths.PathError as e:
        say(f"apod: {e}")
        return 2
    if not root.is_dir():
        say(f"apod: nothing to migrate: there's no {tilde(root)}" + ("" if cur.legacy else " (this is the new layout already)"))
        return 0
    r = os.path.realpath(root)
    inner = [d for d in (new.config, new.data, new.state) if profiles.inside(os.path.realpath(d), r)]
    if inner:
        say(f"apod: {', '.join(map(tilde, inner))} is inside {tilde(root)}, which is renamed at the end: set "
            f"APOD_CONFIG_DIR, APOD_DATA_DIR and APOD_STATE_DIR (or the XDG_* variables) to folders outside it")
        return 2
    if broker_running(root):
        # Beside the new layout, `apod broker stop` would stop the new layout's broker.
        stop = "apod broker stop" if cur.legacy else f"APOD_ROOT={tilde(root)} apod broker stop"
        say(f"apod: a broker of the legacy layout is running: {stop} (and exit its pods), then run apod migrate "
            f"--layout again")
        return 2
    pods = running_pods(root)
    if pods:
        say(f"apod: pods of the legacy layout are running ({', '.join(pods)}): exit them (and apod broker stop), then "
            f"run apod migrate --layout again")
        return 2
    try:
        ops, stays, (conf, data, state) = plan(root, new)
    except (profiles.ProfileError, paths.PathError) as e:
        say(f"apod: {e}")
        return 2
    todo, conflicts = [], []
    for src, dst, how in ops:
        if isinstance(how, tuple) and same_text(dst, how[1]):
            continue  # written by an earlier run
        if dst.exists() or dst.is_symlink():
            conflicts.append(dst)
        else:
            todo.append((src, dst, how))
    if conflicts:
        say("apod: these are there already, so nothing was moved (move them away, or merge them by hand, then run "
            "apod migrate --layout again):\n" + "".join(f"  {tilde(c)}\n" for c in conflicts))
        return 2
    say(f"apod: the legacy layout, {tilde(root)}, to the new one:")
    for k, d in (("config", conf), ("data", data), ("state", state)):
        say(f"  {k:<7} {tilde(d)}")
    say("")
    for src, dst, how in todo:
        shown = f"{src.relative_to(root)}{'/' if src.is_dir() else ''}" + (f" ({how[2]})" if isinstance(how, tuple) else "")
        say(f"  {shown:<40} -> {tilde(dst)}")
        if isinstance(how, tuple) and how[2].startswith("converted"):
            say("".join("        " + line + "\n" for line in how[1].splitlines()).rstrip("\n"))
    if not todo:
        say("  nothing left to move")
    dest = root.with_name(root.name + ".migrated")
    i = 1
    while dest.exists():
        dest, i = root.with_name(f"{root.name}.migrated.{i}"), i + 1
    if stays:
        say(f"\nThese stay in the legacy folder: {', '.join(stays)}.")
    say(f"Then {tilde(root)} becomes {tilde(dest)}: nothing is deleted.")
    # Mounts the new layout's guard refuses (a mount of ~/.config, say): say so before, not at launch.
    for src, dst, how in todo:
        if isinstance(how, tuple):
            try:
                p = profiles.from_toml(profiles.tomllib.loads(how[1]), dst)
                for mode, path in p["pod"]["mounts"]:
                    profiles.check_mount(mode, path, dst.name, lay=new)
            except profiles.ProfileError as e:
                say(f"apod: careful: {e}")
    if not ask("Move them?"):
        say("apod: nothing moved")
        return 1
    for d in (conf, data, state):
        os.makedirs(d, mode=0o700, exist_ok=True)
    for src, dst, how in todo:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(how, tuple):
            # Not world-readable: [pod.env] may hold what an old env file kept.
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as out:
                out.write(how[1])
        else:
            shutil.move(str(src), str(dst))
    os.rename(root, dest)
    say(f"apod: moved; {tilde(root)} is now {tilde(dest)}: delete it once your profiles work in the new layout.")
    if os.environ.get("APOD_ROOT"):
        say("apod: unset APOD_ROOT: it means the legacy layout")
    say("apod: apod broker paths shows where things are now")
    return 0
