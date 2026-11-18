"""apod migrate --layout [PROFILE] [--yes]: the legacy layout (~/.agent-pod, or APOD_ROOT) to the
new one, all at once or one profile at a time.

All at once, it shows what moves where, asks, and then moves: each profile's settings to
profiles/NAME.toml in the config folder (old profile files converted on the way, with the same
check as `apod migrate`), its home to the data folder, its Google client beside its file;
broker.toml to config.toml, roles.toml and broker-extensions/ (now extensions/) to the config
folder; worklog/ to the data folder; history.jsonl, selinux and the broker's state (its log,
approvals, saved sessions, the gh-api CA, its own home) to the state folder. Nothing is deleted:
what isn't moved (the old files, .old/, stale sockets) stays in the legacy folder, which is renamed
~/.agent-pod.migrated at the end. It refuses while a broker or a pod of the legacy layout runs,
and while a file it would move to is there already with something else in it. Run again, it
finishes what's left; once done, there's nothing to do.

One profile (PROFILE), it moves that profile's file, home and Google client the same way, copies
its launches from the history, and copies what the profiles share (config.toml, roles.toml,
extensions/, the SELinux marker, the broker's approvals) unless the new layout has them already;
the rest of the profile's folder goes to ~/.agent-pod.migrated/PROFILE, out of the old apod's
sight. The legacy folder stays, for the profiles still in it: the old apod (and its broker, and
pods of the other profiles) can go on running; only a pod of PROFILE stops it. A run for all of
them after that takes the new layout as it finds it: the same shared files stay behind, the
history is merged, and the broker's state stays where the new broker has its own.
"""
import filecmp
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

import paths
import profiles

USAGE = "usage: apod migrate --layout [PROFILE] [--yes]"


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


def running_pods(top):
    """apod's running pods that mount something from the folder `top` (their names); [] when
    there's no podman. A pod podman can't describe counts as one."""
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
    top, out = os.path.realpath(top), []
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


def same(a, b):
    """Whether files or folders a and b hold the same (folders: the same files, recursively)."""
    a, b = Path(a), Path(b)
    if a.is_dir() != b.is_dir():
        return False
    if not a.is_dir():
        return filecmp.cmp(a, b, shallow=False)
    c = filecmp.dircmp(a, b)
    if c.left_only or c.right_only or c.funny_files:
        return False
    if filecmp.cmpfiles(a, b, c.common_files, shallow=False)[1:] != ([], []):
        return False
    return all(same(a / d, b / d) for d in c.common_dirs)


def free(path):
    """path, or path.1, path.2...: the first that isn't there."""
    p, i = path, 1
    while p.exists() or p.is_symlink():
        p, i = path.with_name(f"{path.name}.{i}"), i + 1
    return p


def shared(root, new):
    """What the profiles share: [(in the legacy folder, in the new layout)], for those the legacy
    folder has."""
    roles = root / "roles.toml" if (root / "roles.toml").exists() else root / "personas.toml"
    pairs = [(root / "broker.toml", new.config_file), (roles, new.roles), (root / "broker-extensions", new.extensions),
             (root / "selinux", new.selinux)]
    return [(s, d) for s, d in pairs if s.exists()]


def profile_ops(root, new, d):
    """A profile folder's moves: its file written, its home and Google client moved."""
    p = new.profile(d.name)
    text, why = profile_text(d)
    ops = [(d / "profile.toml" if why == "its profile.toml" else d, p["file"], ("write", text, why))]
    for src, dst in ((d / "home", p["home"]), (d / "google-credentials.json", p["google_credentials"])):
        if src.exists():
            ops.append((src, dst, ("move",)))
    return ops


def plan(root, new):
    """All of the legacy folder: the moves, [(source, target, how)] with how ("move",), ("write",
    text, why) for a profile's file (written from its folder's settings, which stay where they are,
    so that a second run finds the same text) or ("history", None), the history merged into the
    new layout's; what stays in the legacy folder."""
    ops, stays = [], []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name in paths.LEGACY_FOLDERS:
            continue
        if paths.name_error(d.name):
            stays.append(f"{d.name}/ (not a profile name)")
            continue
        ops += profile_ops(root, new, d)
        for x in sorted(d.iterdir()):
            if x.name not in ("home", "google-credentials.json", "profile.toml"):
                stays.append(f"{d.name}/{x.name}")
    for src, dst in shared(root, new):
        if dst.exists() and same(src, dst):  # copied there by a migration of one profile
            stays.append(f"{src.name} (the same in the new layout)")
        else:
            ops.append((src, dst, ("move",)))  # a conflict if dst is there
    if (root / "roles.toml").exists() and (root / "personas.toml").exists():
        stays.append("personas.toml (roles.toml is read instead)")
    if (root / "worklog").is_dir() and new.worklog.exists():
        for s in sorted((root / "worklog").iterdir()):
            if (new.worklog / s.name).exists():
                stays.append(f"worklog/{s.name}")
            else:
                ops.append((s, new.worklog / s.name, ("move",)))
    elif (root / "worklog").exists():
        ops.append((root / "worklog", new.worklog, ("move",)))
    if (root / "history.jsonl").exists():
        ops.append((root / "history.jsonl", new.history, ("history", None) if new.history.exists() else ("move",)))
    b = root / "broker"
    if b.is_dir():
        for x in sorted(b.iterdir()):
            if x.name == "sessions" and x.is_dir():
                for s in sorted(x.iterdir()):
                    if s.suffix == ".json" and s.is_file() and not (new.sessions_state / s.name).exists():
                        ops.append((s, new.sessions_state / s.name, ("move",)))  # a session's saved state
                    else:
                        stays.append(f"broker/sessions/{s.name}")  # a pod's folder: runtime, made again
            elif is_socket(x):
                stays.append(f"broker/{x.name}")
            elif (new.broker / x.name).exists():
                stays.append(f"broker/{x.name} (the new layout's broker has its own)")
            else:
                ops.append((x, new.broker / x.name, ("move",)))
    for x in sorted(root.iterdir()):
        if not x.is_dir() and x.name not in ("broker.toml", "roles.toml", "personas.toml", "history.jsonl", "selinux"):
            stays.append(x.name)
    return [(Path(s), Path(t), how) for s, t, how in ops], stays


def plan_one(root, new, name):
    """One profile: its moves (as plan's, and ("copy",) for what the profiles share and the new
    layout doesn't have yet, ("history", NAME) for its launches), then the rest of its folder to
    ~/.agent-pod.migrated/NAME; notes on what the new layout has already, but different."""
    d = root / name
    ops, notes = profile_ops(root, new, d), []
    for src, dst in shared(root, new) + [(root / "broker/grants.json", new.grants)]:
        if not src.exists():
            continue
        if not dst.exists():
            ops.append((src, dst, ("copy",)))
        elif not same(src, dst):
            notes.append(f"{tilde(dst)} is the new layout's, and isn't the same as {tilde(src)}, which the old apod "
                         f"reads: apod (this one) uses {tilde(dst)}")
    if (root / "history.jsonl").exists():
        ops.append((root / "history.jsonl", new.history, ("history", name)))
    ops.append((d, free(root.with_name(root.name + ".migrated") / name), ("move",)))  # the rest of it, last
    return [(Path(s), Path(t), how) for s, t, how in ops], notes


def history(src, dst, name):
    """Copy NAME's launches from src into dst (name None: all of them), in time order, each once."""
    def lines(f):
        try:
            return [l for l in Path(f).read_text().splitlines() if l.strip()]
        except OSError:
            return []

    def entry(line):
        try:
            e = json.loads(line)
            return e if isinstance(e, dict) else {}
        except ValueError:
            return {}
    have = lines(dst)
    add = [l for l in lines(src) if name is None or entry(l).get("profile") == name]
    seen, out = set(), []
    for l in have + add:
        if l not in seen:
            seen.add(l)
            out.append(l)
    ts = {l: entry(l).get("ts") if isinstance(entry(l).get("ts"), (int, float)) else 0 for l in out}
    out.sort(key=lambda l: ts[l])  # stable: entries without a time keep their order
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_text("".join(l + "\n" for l in out))
    os.replace(tmp, dst)


def same_text(target, text):
    try:
        return target.read_text() == text
    except OSError:
        return False


def main(argv):
    yes, name = False, None
    for a in argv:
        if a == "--layout":
            continue
        if a in ("--yes", "-y"):
            yes = True
        elif a.startswith("-") or name:
            print(USAGE, file=sys.stderr)
            return 2
        else:
            name = a
    say, ask = profiles.prompts(yes)
    try:
        cur = paths.layout()
        root = cur.legacy_root
        env = {k: v for k, v in os.environ.items() if k != "APOD_ROOT"}
        new = paths.Layout(env, force_new=True)
    except paths.PathError as e:
        say(f"apod: {e}")
        return 2
    again = f"apod migrate --layout{f' {name}' if name else ''}"
    if not root.is_dir():
        say(f"apod: nothing to migrate: there's no {tilde(root)}" + ("" if cur.legacy else " (this is the new layout already)"))
        return 0
    r = os.path.realpath(root)
    inner = [d for d in (new.config, new.data, new.state) if profiles.inside(os.path.realpath(d), r)]
    if inner:
        say(f"apod: {', '.join(map(tilde, inner))} is inside {tilde(root)}, which is renamed at the end: set "
            f"APOD_CONFIG_DIR, APOD_DATA_DIR and APOD_STATE_DIR (or the XDG_* variables) to folders outside it")
        return 2
    if name:
        err = paths.name_error(name)
        if err:
            say(f"apod: {err}")
            return 2
        if not (root / name).is_dir():
            if new.profile(name)["exists"]:
                say(f"apod: {name} is in the new layout already: nothing to migrate")
                return 0
            say(f"apod: no profile {name} in {tilde(root)}")
            return 2
        pods = running_pods(root / name)  # its pods: those of other profiles may run
        if pods:
            say(f"apod: pods of {name} are running ({', '.join(pods)}): exit them, then run {again} again")
            return 2
    else:
        if broker_running(root):
            # Beside the new layout, `apod broker stop` would stop the new layout's broker.
            stop = "apod broker stop" if cur.legacy else f"APOD_ROOT={tilde(root)} apod broker stop"
            say(f"apod: a broker of the legacy layout is running: {stop} (and exit its pods), then run {again} "
                f"again (or migrate one profile at a time: apod migrate --layout PROFILE)")
            return 2
        pods = running_pods(root)
        if pods:
            say(f"apod: pods of the legacy layout are running ({', '.join(pods)}): exit them (and apod broker stop), "
                f"then run {again} again (or migrate one profile at a time: apod migrate --layout PROFILE)")
            return 2
    try:
        if name:
            (ops, notes), stays = plan_one(root, new, name), []
        else:
            (ops, stays), notes = plan(root, new), []
    except (profiles.ProfileError, paths.PathError) as e:
        say(f"apod: {e}")
        return 2
    todo, conflicts = [], []
    for src, dst, how in ops:
        if how[0] == "write" and same_text(dst, how[1]):
            continue  # written by an earlier run
        if how[0] == "move" and (dst.exists() or dst.is_symlink()):
            conflicts.append(dst)
        else:
            todo.append((src, dst, how))
    if conflicts:
        say("apod: these are there already, so nothing was moved (move them away, or merge them by hand, then run "
            f"{again} again):\n" + "".join(f"  {tilde(c)}\n" for c in conflicts))
        return 2
    left = root.with_name(root.name + ".migrated")  # what's left of the profiles moved one at a time
    dest = free(left)  # where the legacy folder goes, once all of it is migrated
    rest = ops[-1][1] if name else None  # where what's left of this profile's folder goes
    say(f"apod: {f'the profile {name}' if name else 'the legacy layout'}, in {tilde(root)}, to the new layout:")
    for k, d in (("config", new.config), ("data", new.data), ("state", new.state)):
        say(f"  {k:<7} {tilde(d)}")
    say("")
    for src, dst, how in todo:
        what = {"write": f" ({how[2] if how[0] == 'write' else ''})", "copy": " (copied: the old apod reads it too)",
                "history": f" ({name}'s launches, copied)" if name else " (merged)"}.get(how[0], "")
        if how[0] == "move" and name and src == root / name:
            what = " (the rest of it, out of the old apod's sight)"
        shown = f"{src.relative_to(root)}{'/' if src.is_dir() else ''}{what}"
        say(f"  {shown:<40} -> {tilde(dst)}")
        if how[0] == "write" and how[2].startswith("converted"):
            say("".join("        " + line + "\n" for line in how[1].splitlines()).rstrip("\n"))
    if not todo:
        say("  nothing left to move")
    for n in notes:
        say(f"\napod: note: {n}")
    if name:
        others = [d.name for d in sorted(root.iterdir()) if d.is_dir() and d.name not in paths.LEGACY_FOLDERS
                and d.name != name and not paths.name_error(d.name)]
        say(f"\n{tilde(root)} stays, for the old apod and the profiles still in it" + (f" ({', '.join(others)})" if others else "")
            + ": apod migrate --layout moves them all once its broker is stopped. Nothing is deleted.")
    else:
        if stays:
            say(f"\nThese stay in the legacy folder: {', '.join(stays)}.")
        say(f"Then {tilde(root)} becomes {tilde(dest)}: nothing is deleted.")
    # Mounts the new layout's guard refuses (a mount of ~/.config, say): say so before, not at launch.
    # Paths into the legacy folder (a key in <profile>/ssh/, say) point to where nothing is after.
    for src, dst, how in todo:
        if how[0] != "write":
            continue
        pname = dst.name[:-5]
        if any(f"{x}/{pname}/" in how[1] for x in (root, tilde(root))) and name:
            say(f"apod: careful: {dst.name} has paths in {tilde(root / pname)}/, which moves to "
                f"{tilde(rest)}/: move what they point to (and fix them in {tilde(dst)}) after")
        elif any(f"{x}/" in how[1] for x in (root, tilde(root))):
            later = "is renamed once every profile is migrated" if name else f"will be {tilde(dest)}/"
            say(f"apod: careful: {dst.name} has paths in {tilde(root)}/, which {later}: move what they point to "
                f"(and fix them in {tilde(dst)}) after")
        try:
            p = profiles.from_toml(profiles.tomllib.loads(how[1]), dst)
            for mode, path in p["pod"]["mounts"]:
                profiles.check_mount(mode, path, dst.name, lay=new)
        except profiles.ProfileError as e:
            say(f"apod: careful: {e}")
    if not ask("Move them?"):
        say("apod: nothing moved")
        return 1
    for d in (new.config, new.data, new.state):
        os.makedirs(d, mode=0o700, exist_ok=True)
    for src, dst, how in todo:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if how[0] == "write":
            # Not world-readable: [pod.env] may hold what an old env file kept.
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as out:
                out.write(how[1])
        elif how[0] == "copy":
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
        elif how[0] == "history":
            history(src, dst, how[1])
        else:
            shutil.move(str(src), str(dst))
    if name:
        say(f"apod: moved {name}; what's left of its folder is in {tilde(rest)}. The old apod no longer "
            f"has it: launch it with this apod.")
    else:
        os.rename(root, dest)
        say(f"apod: moved; {tilde(root)} is now {tilde(dest)}: delete it once your profiles work in the new layout"
            + (f" (and {tilde(left)}, from the profiles moved one at a time)" if dest != left and left.is_dir() else "")
            + ".")
        if os.environ.get("APOD_ROOT"):
            say("apod: unset APOD_ROOT: it means the legacy layout")
    say("apod: apod broker paths shows where things are now")
    return 0
