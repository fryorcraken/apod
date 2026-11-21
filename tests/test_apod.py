#!/usr/bin/env python3
"""Tests for the `apod` launcher (bash) with a stub `podman` that records its arguments, in
throwaway folders under tmp/ (never your real ones): the legacy layout (APOD_ROOT) or the new one
(APOD_CONFIG_DIR, APOD_DATA_DIR, APOD_STATE_DIR, APOD_RUNTIME_DIR).

The regression test: a set of profiles in the old file formats (env, mounts, image, network,
volumes, connectors, github, ssh-key, broker.toml) must launch with the podman command line
recorded in tests/golden/ before profile.toml existed, both as they are and after `apod migrate`
turned them into profile.toml; and so must the same profiles in the new layout. There, the folder
names are mapped to the legacy ones before the comparison (a profile's home, the work logs, the
broker's session folders), and a per-run ssh-agent's socket is mounted alone, where the legacy
layout mounts <profile>/agent/.

The comparison treats `--env-file F` as the `-e K=V` lines of F, and sorts the `-e` options: apod
passes a profile's environment and git identity as `-e` options now, not as an --env-file.

Run: python3 tests/test_apod.py   (RECORD=1 rewrites tests/golden/, from the apod you started from only; KEEP=1 keeps the
temp dir; APOD_TEST_TMP=DIR puts it there instead of tmp/, when the checkout's path is too long for the legacy layout's
unix sockets)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

# git in the tests never uses your identity or git config. In an apod pod they come in through
# GIT_CONFIG_* and GIT_COMMITTER_*, and would sign the tests' commits through your broker (asking
# you on the host).
for k in [k for k in os.environ if k.startswith(("GIT_CONFIG", "GIT_AUTHOR_", "GIT_COMMITTER_"))]:
    del os.environ[k]
os.environ.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "broker"))
import paths  # noqa: E402 (where apod's files are)

APOD = HERE / "apod"
LAYOUT_VARS = ("APOD_ROOT", "APOD_CONFIG_DIR", "APOD_DATA_DIR", "APOD_STATE_DIR", "APOD_RUNTIME_DIR",
               "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR")
GOLDEN = HERE / "tests/golden"
RECORD = bool(os.environ.get("RECORD"))

STUB_PODMAN = textwrap.dedent("""\
    #!/usr/bin/env bash
    # Records `podman run` arguments, one per line, to $STUB_LOG.
    case $1 in
      secret)
        case $2 in
          exists) [[ " $STUB_SECRETS " == *" $3 "* ]]; exit ;;
          ls) for s in $STUB_SECRETS; do echo "$s"; done; exit 0 ;;
        esac ;;
      run) { printf 'podman'; printf '\\n%s' "$@"; printf '\\n--end--\\n'; } >> "$STUB_LOG"; exit 0 ;;
      ps) [ -z "${STUB_PS:-}" ] || echo "$STUB_PS"; exit 0 ;;
      inspect) [ -z "${STUB_INSPECT:-}" ] || echo "$STUB_INSPECT"; exit 0 ;;
    esac
    exit 0
""")
STUB_UUIDGEN = textwrap.dedent("""\
    #!/usr/bin/env bash
    f=$STUB_DIR/uuid-count; n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$f"
    printf '00000000-0000-4000-8000-%012d\\n' "$n"
""")
STUB_GH = textwrap.dedent("""\
    #!/usr/bin/env bash
    if [ "$1 $2" = "auth token" ]; then echo tok-test; exit 0; fi
    if [ "$1 $2" = "api user" ]; then echo octo; exit 0; fi
    exit 0
""")


def env_file_pairs(path):
    """What apod's loader makes of an old env file: KEY=VALUE lines, outer quotes stripped."""
    pairs = []
    for line in Path(path).read_text().splitlines():
        k, eq, v = line.strip().partition("=")
        if eq and not k.startswith("#"):
            pairs.append(f"{k.strip()}={v.strip().strip(chr(39) + chr(34))}")
    return pairs


class Launcher:
    """What the tests of the launcher share: throwaway apod folders (LAYOUT: "legacy", APOD_ROOT;
    "xdg", the new layout) with profiles (made by the subclass's make_profiles), the stub
    programs, and launch() with the podman arguments."""
    LAYOUT = "legacy"
    # The new layout's folder names, mapped to the legacy ones that tests/golden/ has.
    TO_LEGACY = [(r"@TMP@/data/profiles/([^/:\n]+)/home", r"@TMP@/apod/\1/home"),
                 (r"@TMP@/data/worklog/", "@TMP@/apod/worklog/"),
                 (r"@RUN@/sessions/", "@TMP@/apod/broker/sessions/"),
                 (r"@RUN@/ssh-([^/:\n]+)-@PID@\.sock:@RUN@/ssh-\1-@PID@\.sock", r"@TMP@/apod/\1/agent:@TMP@/apod/\1/agent"),
                 (r"SSH_AUTH_SOCK=@RUN@/ssh-([^/:\n]+)-@PID@\.sock", r"SSH_AUTH_SOCK=@TMP@/apod/\1/agent/ssh-@PID@.sock")]

    @classmethod
    def setUpClass(cls):
        base = Path(os.environ.get("APOD_TEST_TMP") or HERE / "tmp")  # short enough for unix socket paths
        base.mkdir(exist_ok=True)
        cls.tmp = Path(tempfile.mkdtemp(dir=base, prefix="apod-test-"))
        cls.addClassCleanup(lambda: None if os.environ.get("KEEP") else shutil.rmtree(cls.tmp, ignore_errors=True))
        cls.root = cls.tmp / "apod"  # the legacy layout's folder
        cls.rundir = Path(tempfile.mkdtemp(prefix="apod-run-"))  # the new layout's runtime folder: short, 0700
        cls.addClassCleanup(lambda: None if os.environ.get("KEEP") else shutil.rmtree(cls.rundir, ignore_errors=True))
        if cls.LAYOUT == "legacy":
            cls.root.mkdir()
            cls.layout_env = {"APOD_ROOT": str(cls.root)}
        else:
            cls.layout_env = {"APOD_CONFIG_DIR": str(cls.tmp / "config"), "APOD_DATA_DIR": str(cls.tmp / "data"),
                              "APOD_STATE_DIR": str(cls.tmp / "state"), "APOD_RUNTIME_DIR": str(cls.rundir)}
        cls.home = cls.tmp / "home"
        cls.home.mkdir()
        (cls.home / ".gitconfig").write_text("[user]\n\tname = Host User\n\temail = host@example.com\n")
        cls.bin = cls.tmp / "bin"
        cls.bin.mkdir()
        for name, text in (("podman", STUB_PODMAN), ("uuidgen", STUB_UUIDGEN), ("gh", STUB_GH)):
            (cls.bin / name).write_text(text)
            (cls.bin / name).chmod(0o755)
        cls.log = cls.tmp / "podman.log"
        cls.log.write_text("")
        cls.proj = cls.tmp / "proj"
        (cls.proj).mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", cls.proj], check=True)
        subprocess.run(["git", "-C", cls.proj, "remote", "add", "origin", "git@github.com:acme/widget.git"], check=True)
        # Things to mount: not under tmp/, which may be inside the checkout, a mount the guard refuses.
        cls.m = Path(tempfile.mkdtemp(prefix="apod-mounts-"))
        cls.addClassCleanup(lambda: None if os.environ.get("KEEP") else shutil.rmtree(cls.m, ignore_errors=True))
        for d in ("ro1", "rw1", "cli-ro", "cli-rw"):
            (cls.m / d).mkdir(parents=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", cls.tmp / "key"], check=True)
        cls.env = dict({k: v for k, v in os.environ.items() if k not in LAYOUT_VARS}, **cls.layout_env,
                       HOME=str(cls.home), STUB_DIR=str(cls.tmp),
                       STUB_LOG=str(cls.log), STUB_SECRETS="gh-plain gh-other", NO_COLOR="1",
                       APOD_BROKER_ASK_TIMEOUT="3", PATH=f"{cls.bin}:{os.environ['PATH']}")
        cls.P = paths.Layout(cls.env)
        for k in list(cls.env):
            if k in ("GH_REPO", "SSH_AUTH_SOCK", "HERDR_PANE_ID", "HC_SCOPE", "APOD_IMAGE") or k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG")):
                cls.env.pop(k)
        cls.addClassCleanup(lambda: subprocess.run([sys.executable, HERE / "apod-broker", "stop"], env=cls.env,
                                                   capture_output=True))
        cls.make_profiles()

    # -- profiles ---------------------------------------------------------------------------

    @classmethod
    def profile(cls, name, **files):
        """A profile in the old files, in the legacy folder (the legacy layout's only)."""
        d = cls.root / name
        (d / "home").mkdir(parents=True)
        for f, text in files.items():
            (d / {"broker_toml": "broker.toml"}.get(f, f.replace("_", "-"))).write_text(textwrap.dedent(text))
        return d

    @classmethod
    def toml_profile(cls, name, text, **files):
        """A profile in a profile.toml: <root>/NAME/profile.toml, or profiles/NAME.toml."""
        p = cls.P.profile(name)
        Path(p["home"]).mkdir(parents=True)
        Path(p["file"]).parent.mkdir(parents=True, exist_ok=True)
        Path(p["file"]).write_text(textwrap.dedent(text))
        return Path(p["dir"] or p["file"])

    @classmethod
    def make_profiles(cls):
        raise NotImplementedError

    @classmethod
    def make_old_profiles(cls):
        m, key = cls.m, cls.tmp / "key"
        # No broker: a token secret, quoted env values, every optional file.
        cls.profile("plain",
                    env=f"""\
                        GIT_AUTHOR_NAME="Bot Agent"
                        GIT_AUTHOR_EMAIL=bot@example.com
                        # a comment
                        SOME_VAR='a b'
                        PLAIN=value
                        """,
                    mounts=f"""\
                        # extra mounts
                        {m}/ro1
                        rw {m}/rw1
                        {m}/missing
                        """,
                    image="myimage\n", network="host\n",
                    volumes="# volumes\napod-plain-nix /nix\n",
                    connectors="# connectors\ngdoc, todoist\n")
        cls.profile("tokenother", github="other\n")
        cls.profile("notoken")
        cls.profile("sshkey", ssh_key=f"{key}\n", ssh_confirm="")
        # The broker: its role, scope, allow and deny, the GitHub account; the identity in env.
        cls.profile("brk",
                    broker_toml="""\
                        role = "dev"
                        scope = ["gh:acme/*"]
                        allow = ["clipboard:image"]
                        deny = ["gh:pr merge"]

                        [gh]
                        user = "agentbot"
                        """,
                    env="GIT_AUTHOR_NAME=Agent\nGIT_AUTHOR_EMAIL=agent@example.com\nGIT_COMMITTER_NAME=Committer\n"
                        "GIT_COMMITTER_EMAIL=committer@example.com\n",
                    mounts=f"{m}/ro1\nrw {m}/rw1\n",
                    image="brkimage\n")
        # The broker, with the identity from the host's git config; a token file the broker ignores;
        # an ssh-key file that the broker's ssh extension doesn't replace.
        cls.profile("brkhost", broker_toml='[gh]\nuser = "agentbot"\n', github="other\n", ssh_key=f"{key}\n")

    # -- running apod ---------------------------------------------------------------------

    def launch(self, profile, *args, expect=0, extra_env=None, proj=None):
        """Run apod; returns (podman argv groups, stderr). The podman log is the stub's."""
        self.log.write_text("")
        (self.tmp / "uuid-count").unlink(missing_ok=True)  # the same ids in every launch
        env = dict(self.env, **(extra_env or {}))
        r = subprocess.run([APOD, profile, *args, str(proj or self.proj)], env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=120, cwd=self.tmp)
        self.assertEqual(r.returncode, expect, r.stderr)
        return self.argv(), r.stderr

    def argv(self):
        """The recorded `podman run` arguments, normalised: paths of this test replaced, process
        ids dropped, `--env-file F` expanded to `-e` options, and the `-e` options sorted."""
        text = self.log.read_text()
        if not text:
            return []
        args = text.split("\n--end--\n")[0].split("\n")[1:]
        out, envs, i = [], [], 0
        while i < len(args):
            a = args[i]
            if a == "--env-file":
                envs += env_file_pairs(args[i + 1])
                i += 2
            elif a == "-e":
                envs.append(args[i + 1])
                i += 2
            else:
                out.append(a)
                i += 1
        out += [f"-e {e}" for e in sorted(envs)]
        text = "\n".join(out)
        text = text.replace(str(self.m), "@TMP@/mounts")
        for real, name in ((self.rundir, "@RUN@"), (self.tmp, "@TMP@"), (HERE, "@REPO@")):
            text = text.replace(str(real), name)
        text = re.sub(r"(apod-[A-Za-z0-9._-]+-proj)-\d+", r"\1-@PID@", text)
        text = re.sub(r"ssh-\d+\.sock", "ssh-@PID@.sock", text)
        text = re.sub(r"ssh-([A-Za-z0-9._-]+)-\d+\.sock", r"ssh-\1-@PID@.sock", text)
        if self.LAYOUT != "legacy":
            for pat, rep in self.TO_LEGACY:
                text = re.sub(pat, rep, text)
        return text.split("\n")

    def check_golden(self, name, argv):
        f = GOLDEN / f"{name}.argv"
        if RECORD:
            GOLDEN.mkdir(exist_ok=True)
            f.write_text("\n".join(argv) + "\n")
            return
        self.assertEqual("\n".join(argv) + "\n", f.read_text(), f"podman arguments of {name} changed")

    def test_profiles_listing(self):
        """apod profiles: the same answer whichever way a profile is written."""
        if "plain" not in self.P.profile_names():
            self.skipTest("no profiles")
        r = subprocess.run([APOD, "profiles"], env=self.env, cwd=self.tmp, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = {l.split()[0]: " ".join(l.split()[1:]) for l in r.stdout.splitlines() if l.startswith(("plain", "tokenother", "notoken", "sshkey", "brk"))}
        self.assertEqual(rows, {
            "plain": "token: set google: no broker: -", "tokenother": "token: gh-other google: no broker: -",
            "notoken": "token: - google: no broker: -", "sshkey": "token: - google: no broker: -",
            "brk": "token: - google: no broker: dev", "brkhost": "token: unused google: no broker: reader"})
        self.assertNotIn("no profile launches with", r.stdout)

    # (golden name, profile, apod options, extra env). Options with paths use @M@ for the mounts dir.
    CASES = [
        ("plain", "plain", [], {}),
        ("plain-cli-mounts", "plain", ["--ro", "@M@/cli-ro", "--rw", "@M@/cli-rw", "--rw", "@M@/ro1", "-i", "other-img", "-c", "todoist"], {}),
        ("plain-role-flag", "plain", ["--role", "dev", "--gh-user", "bot", "--scope", "gh:acme/widget"], {}),
        ("tokenother", "tokenother", [], {}),
        ("notoken", "notoken", [], {}),
        ("sshkey", "sshkey", ["-a", "bash"], {}),
        ("brk", "brk", [], {}),
        ("brk-no-broker", "brk", ["--no-broker"], {}),
        ("brkhost", "brkhost", [], {}),
    ]

    def run_cases(self, label):
        for name, profile, opts, extra in self.CASES:
            with self.subTest(case=name, phase=label):
                argv, _ = self.launch(profile, *[o.replace("@M@", str(self.m)) for o in opts], extra_env=extra)
                self.check_golden(name, argv)



class OldFilesTest(Launcher, unittest.TestCase):
    """Profiles in the old files: broker.toml, env, mounts, image, ... Same command lines as ever."""

    @classmethod
    def make_profiles(cls):
        cls.make_old_profiles()

    def test_old_profiles_launch_as_before(self):
        self.run_cases("old files")

    def test_presets_need_the_new_layout(self):
        self.log.write_text("")
        r = subprocess.run([APOD, "plain", "-p", "tools", str(self.proj)], env=self.env, cwd=self.tmp,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("preset tools: presets are in the new layout only (apod migrate --layout)", r.stderr)
        self.assertEqual(self.log.read_text(), "", "no pod")
        r = subprocess.run([APOD, "presets"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("presets are in the new layout only", r.stderr)

    def test_old_files_are_announced(self):
        _, err = self.launch("plain")
        self.assertEqual(err.count("apod: legacy layout: apod migrate --layout, then unset APOD_ROOT"), 1, err)
        self.assertIn("apod: plain: old profile files (env, mounts, image, network, volumes, connectors): apod migrate plain", err)
        _, err = self.launch("brk")  # the broker's session says it
        self.assertIn("apod broker: brk: old profile files (broker.toml, env, mounts, image): apod migrate brk", err)

    def test_mounts_that_expose_apods_files_are_refused(self):
        for opts in (["--rw", str(self.root)], ["--ro", str(self.root / "plain")], ["--ro", str(self.root.parent)],
                     ["--rw", str(HERE)], ["--ro", str(self.root / "worklog/../broker")]):
            self.log.write_text("")
            r = subprocess.run([APOD, "plain", *opts, str(self.proj)], env=self.env, cwd=self.tmp,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, opts)
            self.assertIn("is refused", r.stderr)
            self.assertEqual(self.log.read_text(), "", "no pod")
        # The worklog, read-only, is fine.
        (self.root / "worklog").mkdir(exist_ok=True)
        argv, _ = self.launch("plain", "--ro", str(self.root / "worklog"))
        self.assertIn("@TMP@/apod/worklog:@TMP@/apod/worklog:ro,z", argv)
        # So is a profile's own mount list.
        (self.root / "plain/mounts").write_text(f"rw {self.root}\n")
        r = subprocess.run([APOD, "plain", str(self.proj)], env=self.env, cwd=self.tmp, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"{self.root}/plain/mounts: rw mount {self.root} is refused", r.stderr)
        (self.root / "plain/mounts").write_text(f"{self.m}/ro1\nrw {self.m}/rw1\n{self.m}/missing\n")

    def test_a_project_in_apods_folder_is_refused(self):
        for proj in (self.root, self.root / "plain", self.root / "broker", self.root.parent):
            self.log.write_text("")
            r = subprocess.run([APOD, "plain", str(proj)], env=self.env, cwd=self.tmp, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, proj)
            self.assertIn(f"the project {proj} is refused: it ", r.stderr)
            self.assertEqual(self.log.read_text(), "", "no pod")

    def test_python_3_11_is_required(self):
        old = self.tmp / "oldbin"
        old.mkdir()
        (old / "python3").write_text("#!/bin/sh\ncase $1 in --version) echo Python 3.9.2 ;; *) exit 1 ;; esac\n")
        (old / "python3").chmod(0o755)
        self.log.write_text("")
        for args in (["plain"], ["profiles"], ["new", "x"], ["migrate", "--all"]):
            r = subprocess.run([APOD, *args, str(self.proj)] if args[0] == "plain" else [APOD, *args],
                               env=dict(self.env, PATH=f"{old}:{self.env['PATH']}"), cwd=self.tmp,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("Python 3.11 or newer is required to read profiles (python3 here: Python 3.9.2)", r.stderr)
        self.assertEqual(self.log.read_text(), "")  # no pod

    def test_a_broken_profile_stops_the_launch(self):
        self.profile("broken")
        (self.root / "broken/profile.toml").write_text('[gh]\nuser = "x"\nscope = ["gh:a/b"]\n')
        r = subprocess.run([APOD, "broken", str(self.proj)], env=self.env, cwd=self.tmp, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("[gh] has scope = ...: move it above the first [table]", r.stderr)
        self.assertEqual(self.log.read_text(), "")


class ProfileTomlTest(Launcher, unittest.TestCase):
    """The same profiles written as profile.toml: the same command lines."""

    @classmethod
    def make_profiles(cls):
        m, key = cls.m, cls.tmp / "key"
        cls.toml_profile("plain", f"""\
            broker = false
            [pod]
            image = "myimage"
            network = "host"
            ro = ["{m}/ro1", "{m}/missing"]
            rw = ["{m}/rw1"]
            volumes = {{ "apod-plain-nix" = "/nix" }}
            connectors = ["gdoc", "todoist"]
            [pod.env]
            SOME_VAR = "a b"
            PLAIN = "value"
            [git]
            name = "Bot Agent"
            email = "bot@example.com"
            """)
        cls.toml_profile("tokenother", 'broker = false\n[token]\nprofile = "other"\n')
        cls.toml_profile("notoken", "broker = false\n")
        cls.toml_profile("sshkey", f'broker = false\n[ssh-agent]\nkey = "{key}"\nconfirm = true\n')
        cls.toml_profile("brk", f"""\
            role = "dev"
            scope = ["gh:acme/*"]
            allow = ["clipboard:image"]
            deny = ["gh:pr merge"]
            [pod]
            image = "brkimage"
            ro = ["{m}/ro1"]
            rw = ["{m}/rw1"]
            [git]
            name = "Agent"
            email = "agent@example.com"
            committer_name = "Committer"
            committer_email = "committer@example.com"
            [gh]
            user = "agentbot"
            """)
        # An [ssh-agent] key beside the broker: used when the broker's ssh extension isn't on.
        cls.toml_profile("brkhost", f"""\
            [token]
            profile = "other"
            [ssh-agent]
            key = "{key}"
            [gh]
            user = "agentbot"
            """)

    def test_profile_toml_launches_as_the_old_files_did(self):
        self.run_cases("profile.toml")

    def test_no_warning_without_old_files(self):
        _, err = self.launch("plain")
        self.assertNotIn("old profile files", err)
        _, err = self.launch("brk")
        self.assertNotIn("old profile files", err)

    def test_env_values_are_passed_as_written(self):
        self.toml_profile("quotes", """\
            broker = false
            [pod.env]
            QUOTED = "\\"x\\" 'y' $HOME"
            EMPTY = ""
            MULTI = "a\\nb"
            [git]
            name = "N"
            email = "n@example.org"
            """)
        argv, _ = self.launch("quotes")
        self.assertIn('-e QUOTED="x" \'y\' $HOME', argv)
        self.assertIn("-e EMPTY=", argv)
        self.assertIn("-e MULTI=a", argv)  # the newline is inside the argument
        self.assertNotIn("--env-file", Path(self.log).read_text().split("\n"))
        self.assertIn("-e GIT_AUTHOR_NAME=N", argv)

    def test_env_from_passes_names_not_values(self):
        """[pod] env_from: -e NAME, and podman reads the value from its environment (podman-run(1),
        --env: a variable given without a value is set from the host's, if it's set there)."""
        self.toml_profile("envfrom", """\
            broker = false
            [pod]
            env_from = ["APOD_TEST_SECRET", "APOD_TEST_EMPTY", "APOD_TEST_UNSET", "proj"]
            [pod.env]
            PLAIN = "v"
            [git]
            name = "N"
            email = "n@example.org"
            """)
        env = {k: v for k, v in self.env.items() if k != "APOD_TEST_UNSET"}
        self.log.write_text("")
        r = subprocess.run([APOD, "envfrom", str(self.proj)], env=dict(env, APOD_TEST_SECRET="s3cret-value", APOD_TEST_EMPTY=""),
                           cwd=self.tmp, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        argv, raw = self.argv(), self.log.read_text()
        self.assertIn("-e APOD_TEST_SECRET", argv)
        self.assertIn("-e APOD_TEST_EMPTY", argv)  # set, if empty: podman passes it
        self.assertIn("-e PLAIN=v", argv)
        self.assertNotIn("s3cret-value", raw)
        self.assertNotIn("APOD_TEST_SECRET=", raw)
        # Not set: left out, with a warning; nor is one of apod's own shell variables (not the environment).
        self.assertNotIn("-e APOD_TEST_UNSET", argv)
        self.assertIn("apod: APOD_TEST_UNSET ([pod] env_from) isn't set in your environment: not passed to the pod", r.stderr)
        self.assertIn("apod: proj ([pod] env_from) isn't set in your environment", r.stderr)
        self.assertNotIn("-e proj", argv)
        self.assertNotIn("s3cret", self.P.history.read_text())

    def test_a_mount_that_is_missing_is_skipped_with_a_warning(self):
        _, err = self.launch("plain")
        self.assertIn(f"apod: skipping missing mount {self.m}/missing", err)

    def worktree_repo(self, name, *worktrees):
        """A repository under the mounts dir, with linked worktrees at these paths (relative to it)."""
        repo = self.m / name
        git = lambda *a: subprocess.run(["git", "-c", "commit.gpgsign=false", *map(str, a)], env=self.env,
                                        check=True, capture_output=True)
        git("init", "-q", "-b", "main", repo)
        git("-C", repo, "commit", "-q", "--allow-empty", "-m", "init")
        for i, w in enumerate(worktrees):
            git("-C", repo, "worktree", "add", "-q", "-b", f"b{i}", repo / w)
        return repo

    def test_the_other_worktrees_records_are_read_only(self):
        """A repository with linked worktrees: its .git/worktrees is read-only in the pod (the
        records of the worktrees in the pod excepted), so git there can't delete the records of
        those that aren't mounted, and the pod's git gc skips the reflog step that would fail on them."""
        M = "@TMP@/mounts"
        reflog_keys = lambda argv: {a.split("=", 1)[1] for a in argv if a.startswith("-e GIT_CONFIG_KEY_")} & \
            {"gc.reflogExpire", "gc.reflogExpireUnreachable"}
        notes = lambda argv: [a for a in argv if "git worktree" in a]
        unshared = "Other worktrees of this repository aren't mounted, so git worktree list shows them as prunable: they are in use on the host."
        ro_note = lambda recs: (f"The worktrees' records ({recs}) are read-only here, but for those of the worktrees in the pod: "
                                "git worktree prune, remove and add fail. Don't rename a branch that another worktree has "
                                "checked out: git renames it, then can't update that worktree's HEAD.")
        mounts = lambda argv, part: [a for a in argv if part in a and ":" in a and " " not in a and not a.startswith("-")]

        repo = self.worktree_repo("wt1", "../wt1-mine", "../wt1-other")
        recs = f"{M}/wt1/.git/worktrees"
        with self.subTest("a linked worktree"):
            argv, _ = self.launch("plain", proj=self.m / "wt1-mine")
            order = [argv.index(f"{p}:{p}:{o}") for p, o in ((f"{M}/wt1/.git", "z"), (f"{M}/wt1-mine", "z"),
                                                             (recs, "ro,z"), (f"{recs}/wt1-mine", "z"))]
            self.assertEqual(order, sorted(order), "parents first")
            self.assertEqual(mounts(argv, recs), [f"{recs}:{recs}:ro,z", f"{recs}/wt1-mine:{recs}/wt1-mine:z"])
            self.assertEqual(reflog_keys(argv), {"gc.reflogExpire", "gc.reflogExpireUnreachable"})
            self.assertEqual(notes(argv), [f"{unshared} {ro_note(recs)}"])
        with self.subTest("another worktree mounted read-write: its record too"):
            argv, _ = self.launch("plain", "--rw", str(self.m / "wt1-other"), proj=self.m / "wt1-mine")
            self.assertEqual(mounts(argv, recs), [f"{recs}:{recs}:ro,z", f"{recs}/wt1-mine:{recs}/wt1-mine:z",
                                                  f"{recs}/wt1-other:{recs}/wt1-other:z"])
            self.assertEqual(notes(argv), [ro_note(recs)])
        with self.subTest("the repository's .git mounted read-only: nothing to add"):
            argv, _ = self.launch("plain", "--ro", str(repo / ".git"), proj=self.m / "wt1-mine")
            self.assertEqual(mounts(argv, recs), [])
            self.assertEqual(reflog_keys(argv), set())
            self.assertEqual(notes(argv), [f"{unshared} Never run git worktree prune, nor git worktree remove on them."])
        with self.subTest("the mounts name .git/worktrees: their mode stands"):
            argv, err = self.launch("plain", "--rw", str(repo / ".git/worktrees"), proj=self.m / "wt1-mine")
            self.assertEqual(mounts(argv, recs), [f"{recs}:{recs}:z"])
            self.assertEqual(reflog_keys(argv), set())
            self.assertIn(f"apod: {repo}/.git/worktrees is mounted read-write, as the mounts say", err)

        repo = self.worktree_repo("wt2", ".claude/worktrees/inside", "../wt2-out")
        recs = f"{M}/wt2/.git/worktrees"
        with self.subTest("the main checkout, with a worktree inside it"):
            argv, _ = self.launch("plain", proj=repo)
            self.assertEqual(mounts(argv, recs), [f"{recs}:{recs}:ro,z", f"{recs}/inside:{recs}/inside:z"])
            self.assertEqual(len(reflog_keys(argv)), 2)
            self.assertEqual(notes(argv), [f"{unshared} {ro_note(recs)}"])
        with self.subTest("the main checkout's .git mounted read-only: nothing to add"):
            argv, _ = self.launch("plain", "--ro", str(repo / ".git"), proj=repo)
            self.assertEqual(mounts(argv, recs), [])
            self.assertEqual(reflog_keys(argv), set())

        # A clone for worktree flows: every worktree is in the pod, so git worktree add works.
        repo = self.worktree_repo("wt6", ".claude/worktrees/a", ".claude/worktrees/b")
        with self.subTest("the main checkout, its worktrees all inside it: nothing read-only"):
            argv, _ = self.launch("plain", proj=repo)
            self.assertEqual(mounts(argv, "/.git/worktrees"), [])
            self.assertEqual(reflog_keys(argv), set())
            self.assertEqual(notes(argv), [])

        # The pod can write these folders: a symlink in them mustn't get a host folder mounted.
        elsewhere = self.m / "elsewhere"
        (elsewhere / "a").mkdir(parents=True)
        (elsewhere / "b").mkdir()
        repo = self.worktree_repo("wt3", "../wt3-out")
        shutil.rmtree(repo / ".git/worktrees")
        (repo / ".git/worktrees").symlink_to(elsewhere)
        with self.subTest("a main checkout's .git/worktrees, a symlink"):
            argv, err = self.launch("plain", proj=repo)
            self.assertEqual(mounts(argv, "elsewhere") + mounts(argv, "/.git/worktrees"), [])
            self.assertEqual(reflog_keys(argv), set())
            self.assertIn(f"apod: {repo}/.git/worktrees is a symlink, which git doesn't make", err)
        other = self.worktree_repo("wt4", "../wt4-out")
        proj = self.m / "wt4-proj"
        proj.mkdir()
        (proj / ".git").symlink_to(other / ".git")
        with self.subTest("a .git, a symlink to a repository outside the pod"):
            argv, _ = self.launch("plain", proj=proj)
            self.assertEqual(mounts(argv, f"{M}/wt4/"), [])
            self.assertEqual(reflog_keys(argv), set())
        proj = self.m / "wt5-proj"
        proj.mkdir()
        (proj / ".git").write_text(f"gitdir: {other}/.git\n")
        with self.subTest("a .git file to a repository outside the pod"):
            argv, err = self.launch("plain", proj=proj)
            self.assertIn("git won't work in the pod", err)
            self.assertEqual(mounts(argv, f"{M}/wt4/"), [])
            self.assertEqual(reflog_keys(argv), set())

        with self.subTest("no worktrees: as before"):
            argv, _ = self.launch("plain")
            self.assertEqual(mounts(argv, "/.git/worktrees"), [])
            self.assertEqual(reflog_keys(argv), set())
            self.assertEqual(notes(argv), [])

    def test_the_old_files_are_ignored_beside_a_profile_toml(self):
        self.toml_profile("both", 'broker = false\n[git]\nname = "New"\nemail = "new@example.org"\n')
        (self.root / "both/env").write_text("GIT_AUTHOR_NAME=Old\nGIT_AUTHOR_EMAIL=old@example.org\n")
        argv, err = self.launch("both")
        self.assertIn("-e GIT_AUTHOR_NAME=New", argv)
        self.assertIn("apod: both: profile.toml is used; the old files (env) are ignored (apod migrate both moves them away)", err)


class NewLayoutTest(ProfileTomlTest):
    """The same profile.toml files in the new layout, profiles/NAME.toml: the same command lines
    (the folder names mapped)."""
    LAYOUT = "xdg"

    def test_the_old_files_are_ignored_beside_a_profile_toml(self):
        self.skipTest("the old files are the legacy layout's only")

    def test_where_things_are(self):
        argv, err = self.launch("brk")
        self.assertNotIn("legacy layout", err)
        files = {f.name for f in (self.tmp / "config/profiles").iterdir()}  # files only, one per profile
        self.assertLessEqual({"brk.toml", "brkhost.toml", "notoken.toml", "plain.toml", "sshkey.toml", "tokenother.toml"}, files)
        self.assertTrue(all(f.endswith((".toml", ".google-credentials.json")) for f in files), files)
        raw = self.log.read_text().split("\n")
        self.assertIn(f"{self.tmp}/data/profiles/brk/home:/home/agent", raw)
        self.assertTrue(any(a.startswith(f"{self.rundir}/sessions/") and a.endswith(":/run/apod-broker") for a in raw), raw)
        self.assertTrue((self.tmp / "state/history.jsonl").is_file())
        self.assertFalse(self.root.exists())
        # The per-run ssh-agent's socket, alone, from the runtime folder.
        self.launch("sshkey", "-a", "bash")
        raw = self.log.read_text().split("\n")
        socks = [a for a in raw if re.fullmatch(rf"{self.rundir}/ssh-sshkey-\d+\.sock:{self.rundir}/ssh-sshkey-\d+\.sock", a)]
        self.assertEqual(len(socks), 1, raw)

    def test_nothing_from_the_config_folder_is_mounted_but_the_google_client(self):
        gd = Path(tempfile.mkdtemp(prefix="apod-gdocs-"))  # a google-docs-mcp checkout, outside tmp/
        self.addCleanup(shutil.rmtree, gd, ignore_errors=True)
        (gd / "dist").mkdir()
        (gd / "dist/index.js").write_text("")
        self.toml_profile("gdocs", "broker = false\n[git]\nname = \"N\"\nemail = \"n@example.org\"\n")
        tok = self.tmp / "data/profiles/gdocs/home/.config/google-docs-mcp"
        tok.mkdir(parents=True)
        (tok / "token.json").write_text("{}")
        cred = self.tmp / "config/profiles/gdocs.google-credentials.json"
        cred.write_text('{"installed": {"client_id": "x", "client_secret": "y"}}')
        self.launch("gdocs", "-c", "google-docs-mcp", extra_env={"APOD_GDOCS_MCP": str(gd)})
        raw = self.log.read_text().split("--end--")[0].split("\n")
        mounted = [a for a in raw if a.startswith(str(self.tmp / "config"))]
        self.assertEqual(mounted, [f"{cred}:{gd}/credentials.json:ro,z"])

    def test_mounts_that_expose_apods_files_are_refused(self):
        (self.tmp / "data/worklog").mkdir(parents=True, exist_ok=True)
        (self.tmp / "state").mkdir(exist_ok=True)
        (self.tmp / "state/history.jsonl").touch()
        for opts in (["--rw", str(self.tmp / "config")], ["--ro", str(self.tmp / "config/profiles/plain.toml")],
                     ["--ro", str(self.tmp / "data/profiles/brk/home")], ["--ro", str(self.tmp / "state")],
                     ["--ro", str(self.rundir)], ["--ro", str(self.tmp)], ["--rw", str(self.tmp / "data/worklog")],
                     ["--ro", str(self.tmp / "data/worklog/../profiles")]):
            self.log.write_text("")
            r = subprocess.run([APOD, "plain", *opts, str(self.proj)], env=self.env, cwd=self.tmp,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, opts)
            self.assertIn("is refused", r.stderr)
            self.assertEqual(self.log.read_text(), "", "no pod")
        # Read-only, the work logs and the history are fine; the old apod's too, while it's beside this one.
        old = self.home / ".agent-pod"
        (old / "worklog").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, old)  # a profile left there is announced at each launch
        argv, _ = self.launch("plain", "--ro", str(self.tmp / "data/worklog"), "--ro", str(self.tmp / "state/history.jsonl"),
                              "--ro", str(old / "worklog"))
        self.assertIn("@TMP@/data/worklog:@TMP@/data/worklog:ro,z", argv)
        self.assertIn("@TMP@/state/history.jsonl:@TMP@/state/history.jsonl:ro,z", argv)
        self.assertIn("@TMP@/home/.agent-pod/worklog:@TMP@/home/.agent-pod/worklog:ro,z", argv)
        for opts in (["--rw", str(old / "worklog")], ["--ro", str(old)]):
            r = subprocess.run([APOD, "plain", *opts, str(self.proj)], env=self.env, cwd=self.tmp,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, opts)
            self.assertIn("apod's legacy folder", r.stderr)
        # Another profile's transcripts, read-only, for a pod that reviews its sessions; not read-write,
        # nor the home around them.
        tr = self.tmp / "data/profiles/brk/home/.claude/projects"
        tr.mkdir(parents=True)
        self.launch("plain", "--ro", str(tr))
        self.assertIn(f"{tr}:{tr}:ro,z", self.log.read_text().split("\n"))
        for opts in (["--rw", str(tr)], ["--ro", str(tr.parent)]):
            r = subprocess.run([APOD, "plain", *opts, str(self.proj)], env=self.env, cwd=self.tmp,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, opts)
            self.assertIn("is inside apod's data folder", r.stderr)
        # Nor one its pod turned into a symlink, to somewhere else on the host.
        evil = self.tmp / "data/profiles/evil/home/.claude"
        evil.mkdir(parents=True)
        (evil / "projects").symlink_to(self.m)
        self.log.write_text("")
        r = subprocess.run([APOD, "plain", "--ro", str(evil / "projects"), str(self.proj)], env=self.env, cwd=self.tmp,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"goes through {evil}/projects, a symlink in apod's data folder", r.stderr)
        self.assertEqual(self.log.read_text(), "", "no pod")
        # So is a profile's own mount list.
        f = self.tmp / "config/profiles/plain.toml"
        text = f.read_text()
        self.addCleanup(f.write_text, text)
        f.write_text(text.replace('rw = [', f'rw = ["{self.tmp}/state", '))
        r = subprocess.run([APOD, "plain", str(self.proj)], env=self.env, cwd=self.tmp, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"{f}: rw mount {self.tmp}/state is refused: it is apod's state folder", r.stderr)

    def test_a_project_in_apods_folders_is_refused(self):
        (self.tmp / "state").mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, self.home / ".agent-pod")  # a profile left there is announced at each launch
        for proj, what in ((self.tmp / "config", "is apod's config folder"), (self.tmp / "config/profiles", "is inside"),
                           (self.tmp / "data", "is apod's data folder"), (self.tmp / "state", "is apod's state folder"),
                           (self.rundir, "is apod's runtime folder"), (self.tmp, "contains apod's config folder"),
                           (self.home / ".agent-pod/old", "is inside apod's legacy folder")):
            proj.mkdir(parents=True, exist_ok=True)
            self.log.write_text("")
            r = subprocess.run([APOD, "plain", str(proj)], env=self.env, cwd=self.tmp, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, proj)
            self.assertIn(f"the project {proj} is refused: it {what}", r.stderr)
            self.assertEqual(self.log.read_text(), "", "no pod")
        # The apod checkout may be the project (a pod working on apod), unlike a --rw mount of it. A
        # copy of it, next to this test's apod folders: this checkout contains them, in its tmp/.
        checkout = Path(tempfile.mkdtemp(dir=self.tmp.parent, prefix="apod-checkout-"))
        self.addCleanup(shutil.rmtree, checkout)
        shutil.copytree(HERE, checkout, dirs_exist_ok=True, symlinks=True,
                        ignore=lambda d, names: {"tmp", ".git"} & set(names) if Path(d) == HERE else ())
        self.log.write_text("")
        r = subprocess.run([checkout / "apod", "plain", str(checkout)], env=self.env, cwd=self.tmp,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"{checkout}:{checkout}:z", self.log.read_text())

    def test_names_a_profile_cant_have(self):
        for name, msg in (("x.google-credentials", "ends in .google-credentials"), ("worklog", "is an apod command or folder")):
            r = subprocess.run([APOD, name, str(self.proj)], env=self.env, cwd=self.tmp, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)
            self.assertIn(msg, r.stderr)
        r = subprocess.run([APOD, "nonesuch", str(self.proj)], env=self.env, cwd=self.tmp, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True)
        self.assertIn("no profile named nonesuch (profiles: brk brkhost", r.stderr)
        r = subprocess.run([APOD, "--list-profiles"], env=self.env, capture_output=True, text=True)  # for _apod
        self.assertLessEqual({"brk", "brkhost", "notoken", "plain", "sshkey", "tokenother"}, set(r.stdout.split()))
        self.assertEqual(r.stderr, "")

    def test_a_profile_left_in_the_legacy_folder(self):
        old = self.home / ".agent-pod"
        (old / "leftover").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, old)
        r = subprocess.run([APOD, "leftover", str(self.proj)], env=self.env, cwd=self.tmp, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"{old} still has profiles of the legacy layout (leftover), which only the old apod launches: "
                      f"apod migrate --layout PROFILE moves one", r.stderr)
        self.assertIn(f"{old}/leftover is a profile of the legacy layout, which isn't used once one of the new "
                      f"layout's folders is there (apod broker paths): apod migrate --layout leftover moves it", r.stderr)

    def test_new_writes_one_file(self):
        r = subprocess.run([APOD, "new", "wiz2"], env=self.env, cwd=self.tmp, capture_output=True, text=True,
                           input="\n".join(["reader", "", "n", "", "", "n", "n", "n", "", "", "", "y"]) + "\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.tmp / "config/profiles/wiz2.toml").read_text(), "broker = false\n")
        self.assertTrue((self.tmp / "data/profiles/wiz2/home").is_dir())
        self.assertIn(f"Created {self.tmp}/config/profiles/wiz2.toml", r.stderr)
        r = subprocess.run([APOD, "new", "wiz2"], env=self.env, cwd=self.tmp, capture_output=True, text=True, input="")
        self.assertIn("wiz2.toml already exists", r.stderr)


class PresetTest(Launcher, unittest.TestCase):
    """Presets, presets/NAME.toml in the config folder: pod settings, a role and a scope any profile
    launches with (-p), over the profile's own and under the command line's."""
    LAYOUT = "xdg"

    @classmethod
    def make_profiles(cls):
        m = cls.m
        cls.toml_profile("who", f"""\
            broker = false
            [pod]
            image = "whoimage"
            ro = ["{m}/ro1"]
            volumes = {{ "apod-who-nix" = "/nix" }}
            env_from = ["APOD_TEST_FROM"]
            [pod.env]
            A = "1"
            OVER = "profile"
            [git]
            name = "Who"
            email = "who@example.org"
            """)
        cls.toml_profile("bot", '[gh]\nuser = "agentbot"\n[git]\nname = "Bot"\nemail = "bot@example.org"\n')
        cls.preset("tools", f"""\
            description = "the tools"
            [pod]
            image = "toolsimage"
            network = "host"
            ro = ["{m}/cli-ro"]
            rw = ["{m}/ro1"]
            volumes = {{ "apod-tools-nix" = "/nix" }}
            [pod.env]
            B = "2"
            OVER = "tools"
            APOD_TEST_FROM = "a value"
            """)
        cls.preset("second", '[pod]\nimage = "secondimage"\nenv_from = ["OVER"]\n[pod.env]\nB = "3"\n')
        cls.preset("job", 'description = "a job"\nrole = "pm"\nscope = ["gh:acme/gadget"]\n')

    @classmethod
    def preset(cls, name, text):
        f = cls.P.preset_file(name)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(textwrap.dedent(text))
        return f

    def refused(self, *args):
        """apod's stderr for a launch that must fail before any pod starts."""
        self.log.write_text("")
        r = subprocess.run([APOD, *args, str(self.proj)], env=self.env, cwd=self.tmp,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.log.read_text(), "", "no pod")
        return r.stderr

    def test_a_preset_over_the_profile(self):
        argv, _ = self.launch("who", "-p", "tools", extra_env={"APOD_TEST_FROM": "host value"})
        self.assertIn("toolsimage", argv)
        self.assertNotIn("whoimage", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "host")
        # The profile's mount, read-write as the preset says, and the preset's own.
        self.assertIn("@TMP@/mounts/ro1:@TMP@/mounts/ro1:z", argv)
        self.assertNotIn("@TMP@/mounts/ro1:@TMP@/mounts/ro1:ro,z", argv)
        self.assertIn("@TMP@/mounts/cli-ro:@TMP@/mounts/cli-ro:ro,z", argv)
        # One volume at /nix: the preset's.
        self.assertIn("apod-tools-nix:/nix", argv)
        self.assertNotIn("apod-who-nix:/nix", argv)
        # The environment: both, the preset's value over the profile's, a value over a name.
        for e in ("-e A=1", "-e B=2", "-e OVER=tools", "-e APOD_TEST_FROM=a value"):
            self.assertIn(e, argv)
        self.assertNotIn("-e APOD_TEST_FROM", argv)
        self.assertIn("apod.preset=tools", argv)
        raw = self.log.read_text()
        self.assertIn('profile "who", preset tools. Only these host paths are mounted', raw)
        # Recorded with the launch, so that apod resume launches with it again.
        last = json.loads(self.P.history.read_text().splitlines()[-1])
        self.assertEqual(last["opts"], ["who", "-p", "tools"])
        self.assertEqual(last["profile"], "who")

    def test_without_a_preset_the_profile_alone(self):
        argv, _ = self.launch("who", extra_env={"APOD_TEST_FROM": "host value"})
        self.assertIn("whoimage", argv)
        self.assertIn("@TMP@/mounts/ro1:@TMP@/mounts/ro1:ro,z", argv)
        self.assertIn("apod-who-nix:/nix", argv)
        self.assertIn("-e APOD_TEST_FROM", argv)
        self.assertNotIn("--network", argv)
        self.assertFalse(any(a.startswith("apod.preset=") for a in argv), argv)
        self.assertIn('profile "who". Only these host paths are mounted', self.log.read_text())

    def test_the_command_line_over_the_presets(self):
        argv, _ = self.launch("who", "-p", "tools", "-i", "cliimage", "--ro", str(self.m / "ro1"))
        self.assertIn("cliimage", argv)
        self.assertNotIn("toolsimage", argv)
        self.assertIn("@TMP@/mounts/ro1:@TMP@/mounts/ro1:ro,z", argv)

    def test_presets_apply_in_order(self):
        for opts in (["-p", "tools,second"], ["-p", "tools", "--preset", "second"]):
            with self.subTest(opts=opts):
                argv, _ = self.launch("who", *opts, extra_env={"OVER": "host"})
                self.assertIn("secondimage", argv)
                self.assertIn("-e B=3", argv)
                self.assertIn("-e OVER", argv)  # second passes it by name, over tools' value
                self.assertNotIn("-e OVER=tools", argv)
                self.assertIn("apod-tools-nix:/nix", argv)  # what second doesn't set stays
                self.assertIn("apod.preset=tools,second", argv)
                self.assertIn('profile "who", presets tools,second.', self.log.read_text())
        argv, _ = self.launch("who", "-p", "second,tools")
        self.assertIn("toolsimage", argv)

    def test_a_broker_profile_with_a_preset(self):
        argv, _ = self.launch("bot", "-p", "tools", "-a", "bash")
        self.assertIn("toolsimage", argv)
        self.assertTrue(any(a.endswith(":/run/apod-broker") for a in argv), argv)

    def broker_note(self):
        """The broker's role and scope lines in the agent's note."""
        raw = self.log.read_text()
        return (re.search(r"apod broker: role ([^\s,.]+)", raw).group(1),
                re.search(r"Scope \(the repositories[^)]*\): (.*)\.\n", raw).group(1).split(", "))

    def test_a_presets_role_and_scope(self):
        """As --role and --scope: the role over the profile's, the scope added to the launch's."""
        self.launch("bot", "-p", "job")
        role, scope = self.broker_note()
        self.assertEqual(role, "pm")
        self.assertIn("gh:acme/gadget", scope)
        self.assertIn("gh:acme/widget", scope)  # the project's remote, as ever
        last = json.loads(self.P.history.read_text().splitlines()[-1])
        self.assertEqual(last["opts"], ["bot", "-p", "job"])  # resume reads the preset again
        # The command line's role over the preset's; its scope with the preset's.
        self.launch("bot", "-p", "job", "--role", "dev", "--scope", "gh:acme/other")
        role, scope = self.broker_note()
        self.assertEqual(role, "dev")
        self.assertIn("gh:acme/gadget", scope)
        self.assertIn("gh:acme/other", scope)
        # The last preset's role.
        f = self.preset("reader", 'role = "reader"\n')
        self.addCleanup(f.unlink)
        self.launch("bot", "-p", "job,reader")
        self.assertEqual(self.broker_note()[0], "reader")
        self.launch("bot", "-p", "reader,job")
        self.assertEqual(self.broker_note()[0], "pm")

    def test_a_presets_role_turns_the_broker_on(self):
        """who has broker = false: a preset's role or scope turns the broker on, as --role does,
        and --no-broker refuses it."""
        argv, _ = self.launch("who", "-p", "job")
        self.assertTrue(any(a.endswith(":/run/apod-broker") for a in argv), argv)
        self.assertEqual(self.broker_note()[0], "pm")
        err = self.refused("who", "-p", "tools,job", "--no-broker")
        self.assertIn("apod: preset job sets a role or a scope, which need the broker, which --no-broker turns off",
                      err)
        argv, _ = self.launch("who", "-p", "tools", "--no-broker")  # pod settings alone: no broker needed
        self.assertFalse(any(a.endswith(":/run/apod-broker") for a in argv), argv)

    def test_a_presets_mounts_are_guarded(self):
        f = self.preset("leaky", f'[pod]\nrw = ["{self.tmp}/config"]\n')
        self.addCleanup(f.unlink)
        err = self.refused("who", "-p", "leaky")
        self.assertIn(f"{f}: rw mount {self.tmp}/config is refused: it is apod's config folder", err)

    def test_what_a_preset_cant_set(self):
        for text, msg in (
                ('[pod]\nconnectors = ["gdoc"]\n', "[pod] connectors: a preset has none, since connectors act as the "
                                                    "profile's own Claude and Google logins"),
                ('allow = ["gh:issue edit"]\n', "a preset has description, role, scope and [pod] only, not allow"),
                ('broker = false\n', "a preset has description, role, scope and [pod] only, not broker"),
                ('[gh]\nuser = "x"\n', "a preset has description, role, scope and [pod] only, not gh"),
                ('[git]\nname = "x"\n', "a preset has description, role, scope and [pod] only, not git"),
                ('role = "none"\n', 'role = "none": a preset doesn\'t turn the broker off (--no-broker does)'),
                ('role = "a b"\n', "role is a string without spaces, not empty"),
                ('scope = "gh:acme/*"\n', "scope is a list of strings"),
                ('[pod]\ndescription = "x"\n', "[pod] has description = ...: move it above [pod]"),
                ('[pod]\nrole = "pm"\n', "[pod] has role = ...: move it above [pod]"),
                ('[pod]\nscope = ["gh:acme/*"]\n', "[pod] has scope = ...: move it above [pod]"),
                ('[pod]\nimages = "x"\n', "[pod] has no key images"),
                ('[pod.env]\nGIT_AUTHOR_NAME = "x"\n', "[pod.env] GIT_AUTHOR_NAME: the git identity is [git] name and email"),
                ('[pod]\nro = "x"\n', "[pod]: ro is a list of strings")):
            with self.subTest(text=text):
                f = self.preset("bad", text)
                self.assertIn(f"{f}: {msg}", self.refused("who", "-p", "bad"))
        f.unlink()

    def test_names(self):
        self.assertIn("'../x' isn't a preset name", self.refused("who", "-p", "../x"))
        err = self.refused("who", "-p", "nope")
        self.assertIn(f"no preset nope: {self.tmp}/config/presets/nope.toml doesn't exist (presets: job second tools)",
                      err)
        r = subprocess.run([APOD, "--list-presets"], env=self.env, capture_output=True, text=True)  # for _apod
        self.assertEqual(r.stdout.split(), ["job", "second", "tools"])
        # presets is a command, not a profile's name.
        r = subprocess.run([APOD, "presets", str(self.proj)], env=self.env, capture_output=True, text=True)
        self.assertIn("usage: apod presets", r.stderr)
        self.assertIn("'presets' is an apod command or folder", paths.name_error("presets"))

    def test_apod_presets_lists_them(self):
        f = self.preset("broken", '[pod]\nconnectors = ["gdoc"]\n')
        self.addCleanup(f.unlink)
        r = subprocess.run([APOD, "presets"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual([l.split()[0] for l in lines], ["broken", "job", "second", "tools"])
        self.assertIn("invalid: ", lines[0])
        self.assertEqual(lines[1].split(None, 1)[1], "role pm, scope gh:acme/gadget  (a job)")
        self.assertEqual(lines[2].split(None, 1)[1], "image secondimage, 2 variables")
        self.assertEqual(lines[3].split(None, 1)[1], "image toolsimage, network host, 2 mounts, 1 volume, 3 variables  "
                                                     "(the tools)")


class MigratedLayoutTest(Launcher, unittest.TestCase):
    """A legacy folder (the old profiles, a profile.toml, the broker's state, work logs...) after
    `apod migrate --layout`: the same command lines in the new layout, and nothing deleted."""
    LAYOUT = "xdg"

    @classmethod
    def make_profiles(cls):
        cls.root.mkdir()
        cls.make_old_profiles()
        r = cls.root
        (r / "tomlp/home").mkdir(parents=True)
        (r / "tomlp/profile.toml").write_text('# kept\nbroker = false\n[git]\nname = "T"\nemail = "t@example.org"\n')
        (r / "bare/home").mkdir(parents=True)  # no settings: no broker
        (r / "plain/google-credentials.json").write_text('{"installed": {}}')
        (r / "plain/home/.claude").mkdir(parents=True)
        (r / "plain/home/.claude/x.json").write_text("{}")
        (r / "plain/agent").mkdir()  # a per-run ssh-agent's folder, from a run long gone
        (r / "broker.toml").write_text('[notify]\ncommand = ["true"]\n')
        (r / "roles.toml").write_text('[roles.mine]\nextends = ["reader"]\n')
        (r / "broker-extensions").mkdir()
        (r / "broker-extensions/mine.py").write_text("")
        (r / "worklog/w1").mkdir(parents=True)
        (r / "worklog/w1/worklog.md").write_text("# log\n")
        (r / "history.jsonl").write_text('{"ts": 1, "profile": "plain", "agent": "bash", "project": "/x", "opts": ["plain"]}\n')
        (r / "broker/sessions/s1").mkdir(parents=True)
        (r / "broker/sessions/s1/allowed_signers").write_text("")
        (r / "broker/sessions/s1.json").write_text('{"id": "s1", "profile": "brk"}\n')
        for f in ("log.jsonl", "grants.json", "broker.log"):
            (r / "broker" / f).write_text("{}\n")
        (r / "broker/gh-api").mkdir()
        (r / "broker/gh-api/ca.pem").write_text("pem")
        (r / "notes.txt").write_text("mine")
        cls.files_before = sorted(str(f.relative_to(r)) for f in r.rglob("*") if f.is_file())
        res = subprocess.run([APOD, "migrate", "--layout", "--yes"], env=dict(cls.env, APOD_ROOT=str(r)), cwd=cls.tmp,
                             capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        cls.migrated = res.stderr

    def test_migrated_layout_launches_as_before(self):
        self.run_cases("migrated layout")
        _, err = self.launch("plain")
        self.assertNotIn("legacy layout", err)
        self.assertNotIn("old profile files", err)

    def test_where_everything_went(self):
        t = self.tmp
        for f in ("config/profiles/plain.toml", "config/profiles/brk.toml", "config/profiles/tomlp.toml",
                  "config/profiles/bare.toml", "config/profiles/plain.google-credentials.json", "config/config.toml",
                  "config/roles.toml", "config/extensions/mine.py", "data/profiles/plain/home/.claude/x.json",
                  "data/worklog/w1/worklog.md", "state/history.jsonl", "state/broker/log.jsonl", "state/broker/grants.json",
                  "state/broker/broker.log", "state/broker/sessions/s1.json", "state/broker/gh-api/ca.pem"):
            self.assertTrue((t / f).is_file(), f)
        self.assertEqual((t / "config/profiles/tomlp.toml").read_text().splitlines()[0], "# kept")
        self.assertTrue((t / "config/profiles/bare.toml").read_text().startswith("broker = false"))
        self.assertEqual((t / "config/profiles/plain.toml").stat().st_mode & 0o777, 0o600)
        self.assertIn("plain/ (converted from env, mounts, image, network, volumes, connectors)", self.migrated)
        self.assertIn("broker = false", self.migrated)  # it shows the files it writes
        self.assertIn("moved;", self.migrated)
        self.assertIn("unset APOD_ROOT", self.migrated)
        # A bare folder launches without the broker, as before.
        argv, _ = self.launch("bare")
        self.assertFalse(any(a.startswith("apod.broker=") for a in argv))
        r = subprocess.run([APOD, "history"], env=self.env, capture_output=True, text=True)
        self.assertIn("plain", r.stdout)

    def test_nothing_is_deleted(self):
        self.assertFalse(self.root.exists())
        old = self.tmp / "apod.migrated"
        left = sorted(str(f.relative_to(old)) for f in old.rglob("*") if f.is_file())
        for f in ("plain/env", "plain/mounts", "brk/broker.toml", "sshkey/ssh-key", "tomlp/profile.toml",
                  "broker/sessions/s1/allowed_signers", "notes.txt"):
            self.assertIn(f, left)
        moved = {"broker.toml": "config/config.toml", "roles.toml": "config/roles.toml",
                 "history.jsonl": "state/history.jsonl"}
        for f in self.files_before:  # every file is still somewhere
            here = (old / f).exists() or (self.tmp / moved.get(f, "-")).exists()
            if not here and "/" in f:
                top, rest = f.split("/", 1)
                here = any((self.tmp / p).exists() for p in (f"data/profiles/{top}/{rest}", f"data/{f}",
                                                             f"config/extensions/{rest}", f"state/{f}",
                                                             f"config/profiles/{top}.google-credentials.json"))
            self.assertTrue(here, f)

    def test_it_is_idempotent(self):
        for env in (dict(self.env, APOD_ROOT=str(self.root)), self.env):
            r = subprocess.run([APOD, "migrate", "--layout", "--yes"], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("nothing to migrate", r.stderr)


class MigrateLayoutChecksTest(unittest.TestCase):
    """apod migrate --layout: when it refuses, what it asks, and a second run after a first that
    didn't finish."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apod-ml-"))  # short: a unix socket goes in it
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "old"
        (self.root / "p/home").mkdir(parents=True)
        (self.root / "p/env").write_text("GIT_AUTHOR_NAME=A\nGIT_AUTHOR_EMAIL=a@example.org\n")
        (self.root / "selinux").write_text("apod\n")
        b = self.tmp / "bin"
        b.mkdir()
        (b / "podman").write_text(STUB_PODMAN)
        (b / "podman").chmod(0o755)
        self.env = dict({k: v for k, v in os.environ.items() if k not in LAYOUT_VARS}, HOME=str(self.tmp / "home"),
                        APOD_ROOT=str(self.root), APOD_CONFIG_DIR=str(self.tmp / "c"), APOD_DATA_DIR=str(self.tmp / "d"),
                        APOD_STATE_DIR=str(self.tmp / "s"), PATH=f"{b}:{os.environ['PATH']}", STUB_LOG=os.devnull)

    def migrate(self, *args, input="", **env):
        return subprocess.run([APOD, "migrate", "--layout", *args], env=dict(self.env, **env), capture_output=True,
                              text=True, input=input)

    def test_refused_while_a_broker_or_a_pod_runs(self):
        import socket
        (self.root / "broker").mkdir()
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(self.root / "broker/admin.sock"))
        s.listen()
        r = self.migrate("--yes")
        s.close()
        self.assertEqual(r.returncode, 2)
        self.assertIn("a broker of the legacy layout is running: apod broker stop", r.stderr)
        (self.root / "broker/admin.sock").unlink()
        # A pod that mounts the legacy folder's files (a profile's home): refused; one that doesn't
        # (a pod of another apod folder, a copy) doesn't count.
        r = self.migrate("--yes", STUB_PS="c1", STUB_INSPECT=f"apod-p-proj-1 /x/proj {self.root}/p/home")
        self.assertEqual(r.returncode, 2)
        self.assertIn("pods of the legacy layout are running (apod-p-proj-1)", r.stderr)
        r = self.migrate(input="n\n", STUB_PS="c1", STUB_INSPECT="apod-other-proj-1 /x/proj /elsewhere/home")
        self.assertEqual(r.returncode, 1, r.stderr)  # it went on, to the question
        self.assertTrue((self.root / "p/home").is_dir() and not (self.tmp / "c").exists())
        # Beside the new layout (one of its folders is there), its own `apod broker stop` won't do.
        (self.tmp / "h").mkdir()
        (self.tmp / "h/.agent-pod").symlink_to(self.root)
        (self.tmp / "c").mkdir()
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(self.root / "broker/admin.sock"))
        s.listen()
        r = self.migrate("--yes", APOD_ROOT="", HOME=str(self.tmp / "h"))
        s.close()
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("a broker of the legacy layout is running: APOD_ROOT=~/.agent-pod apod broker stop", r.stderr)

    def test_paths_into_the_legacy_folder(self):
        (self.root / "p/ssh").mkdir()
        (self.root / "p/profile.toml").write_text(
            f'broker = false\n# a comment on {self.root}/p/ isn\'t a path\n[ssh-agent]\nkey = "{self.root}/p/ssh/id"\n'
            f'[pod.env]\nKEYS = "{self.root}/keys"\n')
        r = self.migrate(input="n\n")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn(f"careful: p.toml has paths in {self.root}/, which will be {self.root}.migrated/: move what "
                      f"they point to (and fix them in {self.tmp}/c/profiles/p.toml) after:\n"
                      f'  4: key = "{self.root}/p/ssh/id"\n  6: KEYS = "{self.root}/keys"\n', r.stderr)
        self.assertIn("p/ssh", r.stderr)  # it stays behind, and the list says so
        # One profile at a time: its folder moves at once; the rest of the old folder, later.
        r = self.migrate("p", input="n\n")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn(f"careful: p.toml has paths in {self.root}/p/, which moves to {self.tmp}/old.migrated/p/: move "
                      f"what they point to (and fix them in {self.tmp}/c/profiles/p.toml) after:\n"
                      f'  4: key = "{self.root}/p/ssh/id"\n', r.stderr)
        self.assertIn(f"careful: p.toml has paths in {self.root}/, which is renamed once every profile is migrated: "
                      f"move what they point to (and fix them in {self.tmp}/c/profiles/p.toml) after:\n"
                      f'  6: KEYS = "{self.root}/keys"\n', r.stderr)
        self.assertNotIn("a comment", r.stderr)

    def test_one_profile_at_a_time(self):
        import socket
        root, c, d, st = self.root, self.tmp / "c", self.tmp / "d", self.tmp / "s"
        (root / "q/home").mkdir(parents=True)
        (root / "q/home/f").write_text("q's")
        (root / "q/profile.toml").write_text("broker = false\n")
        (root / "q/google-credentials.json").write_text("{}")
        (root / "q/.old").mkdir()
        (root / "roles.toml").write_text("[roles.x]\n")
        (root / "broker.toml").write_text('[gh]\nuser = "u"\n')
        (root / "broker-extensions").mkdir()
        (root / "broker-extensions/e.py").write_text("# e\n")
        (root / "broker").mkdir()
        (root / "broker/grants.json").write_text("{}")
        (root / "history.jsonl").write_text('{"ts": 1, "profile": "p"}\n{"ts": 2, "profile": "q"}\n{"ts": 3, "profile": "p"}\n')
        # The old broker runs, and so does a pod of p: q moves all the same.
        s = socket.socket(socket.AF_UNIX)
        s.bind(str(root / "broker/admin.sock"))
        s.listen()
        self.addCleanup(s.close)
        running = dict(STUB_PS="c1", STUB_INSPECT=f"apod-p-proj-1 /x/proj {root}/p/home")
        r = self.migrate("q", "--yes", **running)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((c / "profiles/q.toml").read_text(), "broker = false\n")
        self.assertEqual((d / "profiles/q/home/f").read_text(), "q's")
        self.assertTrue((c / "profiles/q.google-credentials.json").is_file())
        # What the profiles share is copied (the old apod still reads it), and q's launches.
        for old, copy in (("roles.toml", c / "roles.toml"), ("broker.toml", c / "config.toml"),
                          ("broker-extensions/e.py", c / "extensions/e.py"), ("selinux", st / "selinux"),
                          ("broker/grants.json", st / "broker/grants.json")):
            self.assertEqual(copy.read_text(), (root / old).read_text(), old)
        self.assertEqual([json.loads(l)["ts"] for l in (st / "history.jsonl").read_text().splitlines()], [2])
        # The rest of q's folder is out of the old apod's sight; p is as it was.
        self.assertFalse((root / "q").exists())
        self.assertTrue((self.tmp / "old.migrated/q/profile.toml").is_file())
        self.assertTrue((self.tmp / "old.migrated/q/.old").is_dir())
        self.assertTrue((root / "p/home").is_dir() and (root / "p/env").is_file())
        self.assertIn(f"{root} stays, for the old apod and the profiles still in it (p)", r.stderr)
        # A pod of p stops p's migration; q is done.
        r = self.migrate("p", "--yes", **running)
        self.assertEqual(r.returncode, 2)
        self.assertIn("pods of p are running (apod-p-proj-1)", r.stderr)
        r = self.migrate("q", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("q is in the new layout already: nothing to migrate", r.stderr)
        for bad, msg in (("nope", f"no profile nope in {root}"), ("worklog", "is an apod command or folder")):
            r = self.migrate(bad, "--yes")
            self.assertEqual(r.returncode, 2)
            self.assertIn(msg, r.stderr)
        # All the rest, once the old broker is stopped: what's the same stays, the history is merged.
        r = self.migrate("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("or migrate one profile at a time: apod migrate --layout PROFILE", r.stderr)
        s.close()
        (root / "broker/admin.sock").unlink()
        r = self.migrate("--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((d / "profiles/p/home").is_dir())
        self.assertEqual([json.loads(l)["ts"] for l in (st / "history.jsonl").read_text().splitlines()], [1, 2, 3])
        self.assertIn("roles.toml (the same in the new layout)", r.stderr)
        self.assertIn("broker/grants.json (the new layout's broker has its own)", r.stderr)
        self.assertFalse(root.exists())
        self.assertTrue((self.tmp / "old.migrated.1/roles.toml").is_file())
        self.assertIn(f"(and {self.tmp}/old.migrated, from the profiles moved one at a time)", r.stderr)

    def test_one_profile_and_a_shared_file_already_there(self):
        (self.root / "q").mkdir()
        (self.root / "q/profile.toml").write_text("broker = false\n")
        (self.root / "roles.toml").write_text("[roles.x]\n")
        (self.tmp / "c").mkdir()
        (self.tmp / "c/roles.toml").write_text("[roles.y]\n")
        r = self.migrate("q", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"note: {self.tmp}/c/roles.toml is the new layout's, and isn't the same as {self.root}/roles.toml, "
                      f"which the old apod reads", r.stderr)
        self.assertEqual((self.tmp / "c/roles.toml").read_text(), "[roles.y]\n")
        # All at once, a different one is a conflict.
        r = self.migrate("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"  {self.tmp}/c/roles.toml\n", r.stderr)

    def test_it_asks_first(self):
        r = self.migrate(input="n\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"p/home/", r.stderr)
        self.assertIn(f"-> {self.tmp}/d/profiles/p/home", r.stderr)
        self.assertIn("selinux", r.stderr)
        self.assertIn("Move them? [Y/n]", r.stderr)
        self.assertIn("nothing moved", r.stderr)
        self.assertTrue((self.root / "p/home").is_dir() and not (self.tmp / "c").exists())
        self.assertEqual(self.migrate(input="").returncode, 1)  # no answer is a no
        r = self.migrate(input="y\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.tmp / "s/selinux").read_text(), "apod\n")

    def test_what_is_there_already_stops_it(self):
        (self.tmp / "d/profiles/p/home").mkdir(parents=True)
        r = self.migrate("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"{self.tmp}/d/profiles/p/home", r.stderr)
        self.assertTrue(self.root.exists() and not (self.tmp / "c/profiles/p.toml").exists())

    def test_a_second_run_finishes_the_first(self):
        # As if a first run had written the profile's file and moved its home, then stopped.
        text = 'broker = false\n\n[git]   # the identity (GIT_AUTHOR_* and GIT_COMMITTER_* in the old env file)\nname = "A"\nemail = "a@example.org"\n'
        (self.tmp / "c/profiles").mkdir(parents=True)
        (self.tmp / "c/profiles/p.toml").write_text(text)
        (self.tmp / "d/profiles/p").mkdir(parents=True)
        os.rename(self.root / "p/home", self.tmp / "d/profiles/p/home")
        r = self.migrate("--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.tmp / "c/profiles/p.toml").read_text(), text)
        self.assertTrue((self.tmp / "s/selinux").exists() and (self.tmp / "old.migrated/p/env").exists())

    def test_new_folders_inside_the_old_one_are_refused(self):
        r = self.migrate("--yes", APOD_STATE_DIR=str(self.root / "state"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("which is renamed at the end", r.stderr)
        self.assertTrue((self.root / "p/home").is_dir())

    def test_mounts_the_new_layout_refuses_are_pointed_out(self):
        (self.root / "p/mounts").write_text(f"{self.tmp}\n")  # contains the new folders
        r = self.migrate(input="n\n")
        self.assertIn(f"careful: p.toml: ro mount {self.tmp} is refused: it contains apod's config folder", r.stderr)


class MigratedTest(Launcher, unittest.TestCase):
    """The old profiles after `apod migrate`: the same command lines again."""

    @classmethod
    def make_profiles(cls):
        cls.make_old_profiles()
        r = cls.migrate("--all", "--yes")
        assert r.returncode == 0, r.stderr

    @classmethod
    def migrate(cls, *args, input=""):
        return subprocess.run([APOD, "migrate", *args], env=cls.env, cwd=cls.tmp, input=input, capture_output=True, text=True)

    def test_migrated_profiles_launch_as_before(self):
        self.run_cases("migrated")
        # And without a warning about old files.
        _, err = self.launch("plain")
        self.assertNotIn("old profile files", err)

    def test_the_old_files_are_moved_not_deleted(self):
        d = self.root / "plain"
        self.assertEqual(sorted(f.name for f in (d / ".old").iterdir()),
                         ["connectors", "env", "image", "mounts", "network", "volumes"])
        self.assertEqual(sorted(f.name for f in d.iterdir()), [".old", "home", "profile.toml"])
        self.assertEqual((d / ".old/env").read_text().splitlines()[0], 'GIT_AUTHOR_NAME="Bot Agent"')
        self.assertTrue((self.root / "sshkey/.old/ssh-key").exists() and (self.root / "sshkey/.old/ssh-confirm").exists())

    def test_the_profile_toml_written(self):
        m = self.m
        self.assertEqual((self.root / "plain/profile.toml").read_text(), textwrap.dedent(f"""\
            broker = false

            [pod]
            image = "myimage"
            network = "host"
            ro = ["{m}/ro1", "{m}/missing"]
            rw = ["{m}/rw1"]
            volumes = {{ "apod-plain-nix" = "/nix" }}
            connectors = ["gdoc", "todoist"]

            [pod.env]
            SOME_VAR = "a b"
            PLAIN = "value"

            [git]   # the identity (GIT_AUTHOR_* and GIT_COMMITTER_* in the old env file)
            name = "Bot Agent"
            email = "bot@example.com"
            """))
        brk = (self.root / "brk/profile.toml").read_text()
        self.assertTrue(brk.startswith('role = "dev"\nscope = ["gh:acme/*"]'), brk)  # broker.toml first, as it was
        self.assertNotIn("broker = ", brk)
        self.assertIn('[gh]\nuser = "agentbot"', brk)
        self.assertIn('committer_name = "Committer"', brk)
        self.assertIn('[token]   # without the broker: another profile\'s GitHub token\nprofile = "other"', (self.root / "tokenother/profile.toml").read_text())
        self.assertIn("confirm = true", (self.root / "sshkey/profile.toml").read_text())

    def test_it_is_idempotent(self):
        before = {f: f.read_text() for f in self.root.glob("*/profile.toml")}
        r = self.migrate("--all", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("plain: already migrated: nothing to do", r.stderr)
        self.assertNotIn("would be", r.stderr)
        self.assertEqual({f: f.read_text() for f in self.root.glob("*/profile.toml")}, before)

    def test_it_shows_the_file_and_asks_first(self):
        self.profile("asked", env="GIT_AUTHOR_NAME=A\nGIT_AUTHOR_EMAIL=a@example.org\n", image="img\n")
        r = self.migrate("asked", input="n\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"asked: {self.root}/asked/profile.toml would be:", r.stderr)
        self.assertIn('    image = "img"', r.stderr)
        self.assertIn("Write it? [Y/n]", r.stderr)
        self.assertIn("asked: left as it was", r.stderr)
        self.assertFalse((self.root / "asked/profile.toml").exists())
        self.assertTrue((self.root / "asked/env").exists())
        r = self.migrate("asked", input="y\n")
        self.assertTrue((self.root / "asked/profile.toml").exists())
        self.assertFalse((self.root / "asked/env").exists())
        self.assertEqual(self.migrate("asked", input="").returncode, 0)
        # No answer (stdin closed) is a no.
        self.profile("silent", image="img\n")
        self.migrate("silent", input="")
        self.assertTrue((self.root / "silent/image").exists() and not (self.root / "silent/profile.toml").exists())

    def test_the_profile_toml_is_private(self):
        self.profile("secretive", env="GIT_AUTHOR_NAME=A\nGIT_AUTHOR_EMAIL=a@example.org\nAPI_KEY=hunter2\n")
        (self.root / "secretive/env").chmod(0o600)
        self.assertEqual(self.migrate("secretive", "--yes").returncode, 0)
        self.assertEqual((self.root / "secretive/profile.toml").stat().st_mode & 0o777, 0o600)

    def test_persona_is_renamed_only_at_the_top(self):
        self.profile("pers", broker_toml='persona = "dev"\n[x]\npersona = "keep"\n')
        r = self.migrate("pers", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.root / "pers/profile.toml").read_text(), 'role = "dev"\n[x]\npersona = "keep"\n')

    def test_old_files_beside_a_profile_toml_are_moved_away(self):
        self.toml_profile("both", 'broker = false\n[git]\nname = "New"\nemail = "new@example.org"\n')
        (self.root / "both/env").write_text("GIT_AUTHOR_NAME=Old\n")
        r = self.migrate("both", input="y\n")
        self.assertIn("both: profile.toml exists, so the old files (env) are ignored.", r.stderr)
        self.assertTrue((self.root / "both/.old/env").exists())
        self.assertIn('name = "New"', (self.root / "both/profile.toml").read_text())  # untouched

    def test_comments_and_persona_survive(self):
        self.profile("old", broker_toml='# my broker\npersona = "maintainer"   # was persona\nscope = ["gh:a/b"]\n\n[gh]\nuser = "x"  # the bot\n')
        r = self.migrate("old", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (self.root / "old/profile.toml").read_text()
        self.assertTrue(text.startswith('# my broker\nrole = "maintainer"   # was persona\n'), text)
        self.assertIn('user = "x"  # the bot', text)

    def test_what_does_not_convert_is_reported_and_nothing_changes(self):
        # A key after a [table] line was gh.scope for the old reading; profile.toml refuses it.
        self.profile("odd", broker_toml='[gh]\nuser = "x"\nscope = ["gh:a/b"]\n', image="img\n")
        r = self.migrate("odd", "--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("odd: the old files don't convert to a profile.toml", r.stderr)
        self.assertIn("move it above the first [table]", r.stderr)
        self.assertTrue((self.root / "odd/broker.toml").exists() and not (self.root / "odd/profile.toml").exists())
        r = self.migrate("nonesuch")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no profile named nonesuch", r.stderr)
        self.assertEqual(self.migrate().returncode, 2)  # a profile or --all

    def test_migrate_is_not_a_profile_name(self):
        r = subprocess.run([APOD, "migrate-me"], env=self.env, cwd=self.tmp, capture_output=True, text=True)
        self.assertIn("no profile named migrate-me", r.stderr)


class WizardTest(Launcher, unittest.TestCase):
    """apod new: answers on stdin; it writes one profile.toml."""

    @classmethod
    def make_profiles(cls):
        (cls.bin / "rad").write_text("#!/bin/sh\nexit 0\n")  # there's a rad, so the wizard asks about it
        (cls.bin / "rad").chmod(0o755)

    def new(self, name, *answers):
        return subprocess.run([APOD, "new", name], env=self.env, cwd=self.tmp, input="\n".join(answers) + "\n",
                              capture_output=True, text=True)

    def test_a_broker_profile(self):
        m = self.m
        r = self.new("wiz",
                     "dev",                       # role
                     "agentbot",                  # GitHub account
                     "y", "Bot Agent", "bot@example.org",   # another git identity
                     "",                          # signing key
                     "",                          # ssh hosts
                     "n",                         # radicle
                     "n",                         # notifications
                     "y", "y",                    # paste images; allow it for this profile
                     "gh:acme/*",                 # more scope
                     f"{m}/nonesuch", str(self.root), f"{m}/ro1", "",   # read-only mounts: missing, refused, fine, done
                     f"{m}/rw1", "",              # read-write mounts
                     "myimage",                   # image
                     "y")                         # create
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"No such path: {m}/nonesuch", r.stderr)
        self.assertIn("is refused", r.stderr)
        self.assertIn(f"{self.root}/wiz/profile.toml:", r.stderr)
        text = (self.root / "wiz/profile.toml").read_text()
        self.assertEqual(text, textwrap.dedent(f"""\
            role = "dev"
            allow = ["clipboard:image"]   # on top of the role, for this profile
            scope = ["gh:acme/*"]

            [pod]
            image = "myimage"
            ro = ["{m}/ro1"]
            rw = ["{m}/rw1"]

            [git]
            name = "Bot Agent"
            email = "bot@example.org"

            [gh]
            user = "agentbot"

            [clipboard]
            enabled = true
            """))
        self.assertEqual(sorted(f.name for f in (self.root / "wiz").iterdir()), ["home", "profile.toml"])
        # It's a profile apod can launch, with what was asked for.
        argv, err = self.launch("wiz")
        self.assertNotIn("old profile files", err)
        self.assertIn("-e GIT_AUTHOR_NAME=Bot Agent", argv)
        self.assertIn("@TMP@/mounts/ro1:@TMP@/mounts/ro1:ro", argv)
        self.assertIn("@TMP@/mounts/rw1:@TMP@/mounts/rw1", argv)
        self.assertIn("myimage", argv)

    def test_paths_are_stored_absolute(self):
        rel = os.path.relpath(self.m / "ro1", self.tmp)  # as typed, from the folder apod new runs in
        r = self.new("relwiz", "reader", "", "n", "", "", "n", "n", "n", rel, "", "", "", "y")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f'ro = ["{self.m}/ro1"]', (self.root / "relwiz/profile.toml").read_text())

    def test_a_profile_without_the_broker(self):
        r = self.new("plainwiz", "reader", "", "n", "", "", "n", "n", "n", "", "", "", "y")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("No broker: the agent works on the launch folder", r.stderr)
        self.assertEqual((self.root / "plainwiz/profile.toml").read_text(), "broker = false\n")

    def test_nothing_is_written_without_a_yes(self):
        r = self.new("nope", "reader", "", "n", "", "", "n", "n", "n", "", "", "", "n")
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.root / "nope").exists())

    def test_names_that_are_taken(self):
        for name in ("migrate", "broker"):
            self.assertIn("is an apod command or folder", self.new(name, "").stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
