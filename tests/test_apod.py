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
        cls.m = cls.tmp / "mounts"          # things to mount
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

    def test_python_3_11_is_required(self):
        old = self.tmp / "oldbin"
        old.mkdir()
        (old / "python3").write_text("#!/bin/sh\ncase $1 in --version) echo Python 3.9.2 ;; *) exit 1 ;; esac\n")
        (old / "python3").chmod(0o755)
        self.log.write_text("")
        for args in (["plain"], ["profiles"]):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
