#!/usr/bin/env python3
"""Tests for the `apod` launcher (bash) with a stub `podman` that records its arguments, in a
throwaway APOD_ROOT under tmp/ (never your real ~/.agent-pod).

The regression test: a set of profiles in the old file formats (env, mounts, image, network,
volumes, connectors, github, ssh-key, broker.toml) must launch with the podman command line
recorded in tests/golden/ before profile.toml existed, both as they are and after `apod migrate`
turned them into profile.toml.

The comparison treats `--env-file F` as the `-e K=V` lines of F, and sorts the `-e` options: apod
passes a profile's environment and git identity as `-e` options now, not as an --env-file.

Run: python3 tests/test_apod.py   (RECORD=1 rewrites tests/golden/; KEEP=1 keeps the temp dir; APOD_TEST_TMP=DIR puts it
there instead of tmp/, when the checkout's path is too long for a unix socket)
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

HERE = Path(__file__).resolve().parent.parent
APOD = HERE / "apod"
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
    """What the tests of the launcher share: a throwaway APOD_ROOT with profiles (made by the
    subclass's make_profiles), the stub programs, and launch() with the podman arguments."""

    @classmethod
    def setUpClass(cls):
        base = Path(os.environ.get("APOD_TEST_TMP") or HERE / "tmp")  # short enough for unix socket paths
        base.mkdir(exist_ok=True)
        cls.tmp = Path(tempfile.mkdtemp(dir=base, prefix="apod-test-"))
        cls.addClassCleanup(lambda: None if os.environ.get("KEEP") else shutil.rmtree(cls.tmp, ignore_errors=True))
        cls.root = cls.tmp / "apod"
        cls.root.mkdir()
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
        cls.env = dict(os.environ, APOD_ROOT=str(cls.root), HOME=str(cls.home), STUB_DIR=str(cls.tmp),
                       STUB_LOG=str(cls.log), STUB_SECRETS="gh-plain gh-other", NO_COLOR="1",
                       APOD_BROKER_ASK_TIMEOUT="3", PATH=f"{cls.bin}:{os.environ['PATH']}")
        for k in list(cls.env):
            if k in ("GH_REPO", "SSH_AUTH_SOCK", "HERDR_PANE_ID", "HC_SCOPE", "APOD_IMAGE") or k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG")):
                cls.env.pop(k)
        cls.addClassCleanup(lambda: subprocess.run([sys.executable, HERE / "apod-broker", "stop"], env=cls.env,
                                                   capture_output=True))
        cls.make_profiles()

    # -- profiles ---------------------------------------------------------------------------

    @classmethod
    def profile(cls, name, **files):
        d = cls.root / name
        (d / "home").mkdir(parents=True)
        for f, text in files.items():
            (d / {"broker_toml": "broker.toml"}.get(f, f.replace("_", "-"))).write_text(textwrap.dedent(text))
        return d

    @classmethod
    def toml_profile(cls, name, text, **files):
        d = cls.root / name
        (d / "home").mkdir(parents=True)
        (d / "profile.toml").write_text(textwrap.dedent(text))
        return d

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

    def launch(self, profile, *args, expect=0, extra_env=None):
        """Run apod; returns (podman argv groups, stderr). The podman log is the stub's."""
        self.log.write_text("")
        (self.tmp / "uuid-count").unlink(missing_ok=True)  # the same ids in every launch
        env = dict(self.env, **(extra_env or {}))
        r = subprocess.run([APOD, profile, *args, str(self.proj)], env=env, stdin=subprocess.DEVNULL,
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
        for real, name in ((self.tmp, "@TMP@"), (HERE, "@REPO@")):
            text = text.replace(str(real), name)
        text = re.sub(r"(apod-[A-Za-z0-9._-]+-proj)-\d+", r"\1-@PID@", text)
        text = re.sub(r"ssh-\d+\.sock", "ssh-@PID@.sock", text)
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
        if not (self.root / "plain").exists():
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

    def test_old_files_are_announced(self):
        _, err = self.launch("plain")
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

    def test_a_mount_that_is_missing_is_skipped_with_a_warning(self):
        _, err = self.launch("plain")
        self.assertIn(f"apod: skipping missing mount {self.m}/missing", err)

    def test_the_old_files_are_ignored_beside_a_profile_toml(self):
        self.toml_profile("both", 'broker = false\n[git]\nname = "New"\nemail = "new@example.org"\n')
        (self.root / "both/env").write_text("GIT_AUTHOR_NAME=Old\nGIT_AUTHOR_EMAIL=old@example.org\n")
        argv, err = self.launch("both")
        self.assertIn("-e GIT_AUTHOR_NAME=New", argv)
        self.assertIn("apod: both: profile.toml is used; the old files (env) are ignored (apod migrate both moves them away)", err)


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
