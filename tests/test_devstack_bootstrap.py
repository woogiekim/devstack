"""Tests for the remote one-touch bootstrap installer.

The ``bootstrap`` script at the repo root is the curl-pipe target of the
remote install one-liner:

    curl -fsSL https://raw.githubusercontent.com/woogiekim/devstack/main/bootstrap | sh

These tests stub ``git`` via a temporary directory placed first on ``PATH``
so we can exercise the script's pure logic — env resolution, clone vs.
update branching, fail-fast guards, exec hand-off to the inner installer —
without touching the network. The inner installer is also stubbed so the
bootstrap's hand-off is observable as a recorded "INSTALL_RAN" sentinel
instead of a real symlink install.

Contract:

* ``bootstrap`` is POSIX sh and passes ``/bin/sh -n``.
* ``DEVSTACK_HOME`` defaults to ``${HOME}/.devstack/src`` and is honored
  when set.
* ``DEVSTACK_REPO`` defaults to ``https://github.com/woogiekim/devstack.git``
  and is honored when set.
* ``DEVSTACK_REF`` defaults to ``main`` and is honored when set.
* When ``DEVSTACK_HOME`` does not exist, the script runs ``git clone
  --depth 1 --branch <ref> <repo> <home>``.
* When ``DEVSTACK_HOME`` already contains a devstack git checkout, the
  script runs ``git -C <home> fetch --depth 1 origin <ref>`` then
  ``git -C <home> reset --hard FETCH_HEAD``.
* When ``DEVSTACK_HOME`` exists but is not a git checkout, the script
  aborts with a clear actionable message and non-zero exit code.
* When ``git`` is not on PATH, the script aborts with a clear message
  and non-zero exit code.
* After a successful clone/update, the script execs
  ``"$DEVSTACK_HOME/install"`` so the canonical installer continues to
  own symlinks, AI skills, and workspace registration.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "bootstrap"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class BootstrapSyntaxTests(unittest.TestCase):
    """The script must exist and be POSIX-sh parseable."""

    def test_bootstrap_file_exists(self):
        self.assertTrue(
            BOOTSTRAP.exists(),
            f"bootstrap script missing at {BOOTSTRAP}",
        )

    def test_bootstrap_is_executable(self):
        self.assertTrue(
            os.access(BOOTSTRAP, os.X_OK),
            f"bootstrap script must be executable: {BOOTSTRAP}",
        )

    def test_bootstrap_parses_under_posix_sh(self):
        result = subprocess.run(
            ["/bin/sh", "-n", str(BOOTSTRAP)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"sh -n bootstrap failed: stdout={result.stdout!r} stderr={result.stderr!r}",
        )

    def test_bootstrap_has_no_bashisms_in_shebang(self):
        head = BOOTSTRAP.read_text().splitlines()[0]
        self.assertIn(
            "/bin/sh",
            head,
            f"bootstrap must use /bin/sh shebang, got {head!r}",
        )


class BootstrapHelpTests(unittest.TestCase):
    """The bootstrap should advertise its own env knobs in --help."""

    def test_bootstrap_documents_env_overrides(self):
        # We do not require an actual --help flag; the env knob names just
        # need to be discoverable inside the script so operators can grep.
        text = BOOTSTRAP.read_text()
        self.assertIn("DEVSTACK_HOME", text)
        self.assertIn("DEVSTACK_REPO", text)
        self.assertIn("DEVSTACK_REF", text)
        self.assertIn("install", text)


class _StubEnv:
    """Build an isolated test environment with stub ``git`` and a stub
    ``install`` inner installer.
    """

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.home.mkdir()
        self.bin = tmp / "stub-bin"
        self.bin.mkdir()
        self.log = tmp / "stub.log"
        self.log.write_text("")

    def install_stub_git(self, *, clone_writes_install: bool = True,
                         clone_fail: bool = False,
                         fetch_fail: bool = False) -> None:
        """Drop a stub ``git`` on PATH that logs invocations.

        When ``clone_writes_install`` is True (default), a ``clone`` call
        creates the target directory with a ``.git`` marker AND copies the
        repo-local stub installer into it so the bootstrap's final ``exec``
        finds an executable to hand off to.
        """
        clone_fail_flag = "1" if clone_fail else "0"
        fetch_fail_flag = "1" if fetch_fail else "0"
        clone_install_flag = "1" if clone_writes_install else "0"
        body = textwrap.dedent(
            f"""\
            #!/bin/sh
            # Stub git used by test_devstack_bootstrap.py.
            echo "git $*" >> "{self.log}"

            cmd="$1"; shift || true

            if [ "$cmd" = "clone" ]; then
                if [ "{clone_fail_flag}" = "1" ]; then
                    echo "stub-git: clone failed" >&2
                    exit 17
                fi
                # last positional arg is the target directory
                target=""
                for a in "$@"; do target="$a"; done
                mkdir -p "$target/.git"
                if [ "{clone_install_flag}" = "1" ]; then
                    cp "{self.bin}/stub-install" "$target/install"
                    chmod +x "$target/install"
                fi
                exit 0
            fi

            if [ "$cmd" = "-C" ]; then
                target="$1"; shift
                sub="$1"; shift || true
                if [ "$sub" = "fetch" ]; then
                    if [ "{fetch_fail_flag}" = "1" ]; then
                        echo "stub-git: fetch failed" >&2
                        exit 23
                    fi
                    exit 0
                fi
                if [ "$sub" = "reset" ]; then
                    exit 0
                fi
                if [ "$sub" = "rev-parse" ]; then
                    if [ -d "$target/.git" ]; then
                        echo "$target/.git"; exit 0
                    fi
                    echo "stub-git: not a git checkout" >&2
                    exit 128
                fi
            fi
            echo "stub-git: unknown command: $cmd $*" >&2
            exit 99
            """
        )
        path = self.bin / "git"
        _write_executable(path, body)

    def install_stub_inner_installer(self) -> None:
        """Drop a stub ``install`` script that the bootstrap will exec
        AFTER a successful clone/update. It logs its invocation.
        """
        body = textwrap.dedent(
            f"""\
            #!/bin/sh
            echo "INSTALL_RAN args=$*" >> "{self.log}"
            echo "INSTALL_HOME=${{DEVSTACK_HOME:-unset}}" >> "{self.log}"
            exit 0
            """
        )
        path = self.bin / "stub-install"
        _write_executable(path, body)

    def env(self, **overrides) -> dict:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "DEVSTACK_REPO": "https://example.invalid/woogiekim/devstack.git",
            "DEVSTACK_REF": "main",
        }
        env.update(overrides)
        return env

    def log_text(self) -> str:
        return self.log.read_text()


class BootstrapCloneFlow(unittest.TestCase):
    """End-to-end clone flow against a stub git."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="devstack-bootstrap-")
        self.tmp = Path(self._tmp)
        self.stub = _StubEnv(self.tmp)
        self.stub.install_stub_inner_installer()
        self.stub.install_stub_git(clone_writes_install=True)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_default_devstack_home_is_under_home_dot_devstack_src(self):
        env = self.stub.env()
        # Do not set DEVSTACK_HOME — verify the default is honored.
        result = subprocess.run(
            ["/bin/sh", str(BOOTSTRAP)],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"bootstrap failed: stdout={result.stdout!r} stderr={result.stderr!r}",
        )
        expected_home = self.stub.home / ".devstack" / "src"
        self.assertTrue(
            expected_home.exists(),
            f"default DEVSTACK_HOME not created: {expected_home}",
        )
        log = self.stub.log_text()
        self.assertIn("git clone --depth 1", log)
        self.assertIn("--branch main", log)
        self.assertIn("INSTALL_RAN", log)
        self.assertIn(f"INSTALL_HOME={expected_home}", log)

    def test_explicit_devstack_home_is_honored(self):
        target = self.tmp / "explicit-home"
        env = self.stub.env(DEVSTACK_HOME=str(target))
        result = subprocess.run(
            ["/bin/sh", str(BOOTSTRAP)],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((target / ".git").exists())
        self.assertIn(f"INSTALL_HOME={target}", self.stub.log_text())

    def test_devstack_repo_and_ref_overrides_are_honored(self):
        env = self.stub.env(
            DEVSTACK_HOME=str(self.tmp / "h"),
            DEVSTACK_REPO="https://example.invalid/forked/devstack.git",
            DEVSTACK_REF="release/2026.06",
        )
        result = subprocess.run(
            ["/bin/sh", str(BOOTSTRAP)],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.stub.log_text()
        self.assertIn("https://example.invalid/forked/devstack.git", log)
        self.assertIn("--branch release/2026.06", log)


class BootstrapUpdateFlow(unittest.TestCase):
    """When DEVSTACK_HOME already contains a devstack checkout, the
    bootstrap updates in place via fetch + reset --hard.
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="devstack-bootstrap-")
        self.tmp = Path(self._tmp)
        self.stub = _StubEnv(self.tmp)
        self.stub.install_stub_inner_installer()
        self.stub.install_stub_git()
        # Seed an existing devstack checkout: a directory with a .git/
        # marker AND an executable install script (so the final exec
        # has a target to hand off to without a clone running).
        self.target = self.tmp / "existing"
        (self.target / ".git").mkdir(parents=True)
        _write_executable(
            self.target / "install",
            textwrap.dedent(
                f"""\
                #!/bin/sh
                echo "INSTALL_RAN args=$*" >> "{self.stub.log}"
                echo "INSTALL_HOME=${{DEVSTACK_HOME:-unset}}" >> "{self.stub.log}"
                exit 0
                """
            ),
        )

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_existing_checkout_is_updated_not_recloned(self):
        env = self.stub.env(DEVSTACK_HOME=str(self.target))
        result = subprocess.run(
            ["/bin/sh", str(BOOTSTRAP)],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.stub.log_text()
        self.assertNotIn("git clone", log,
                         "existing checkout should NOT trigger a clone")
        self.assertIn("git -C", log)
        self.assertIn("fetch --depth 1 origin main", log)
        self.assertIn("reset --hard FETCH_HEAD", log)
        self.assertIn("INSTALL_RAN", log)


class BootstrapFailFastGuards(unittest.TestCase):
    """Negative paths must fail loudly with actionable messages."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="devstack-bootstrap-")
        self.tmp = Path(self._tmp)
        self.stub = _StubEnv(self.tmp)
        self.stub.install_stub_inner_installer()

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_aborts_when_git_is_missing(self):
        # Note: no install_stub_git() — PATH has no git binary.
        env = self.stub.env(DEVSTACK_HOME=str(self.tmp / "x"))
        result = subprocess.run(
            ["/bin/sh", str(BOOTSTRAP)],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(
            result.returncode, 0,
            f"bootstrap must exit non-zero when git is missing; got rc=0\nstdout={result.stdout!r}\nstderr={result.stderr!r}",
        )
        combined = (result.stdout + "\n" + result.stderr).lower()
        self.assertIn("git", combined,
                      "error message must mention git")

    def test_aborts_when_devstack_home_is_a_non_git_directory(self):
        self.stub.install_stub_git()
        target = self.tmp / "stale"
        target.mkdir()
        (target / "some-other-file").write_text("legacy content")
        env = self.stub.env(DEVSTACK_HOME=str(target))
        result = subprocess.run(
            ["/bin/sh", str(BOOTSTRAP)],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(
            result.returncode, 0,
            "bootstrap must refuse to clobber a non-git DEVSTACK_HOME",
        )
        combined = (result.stdout + "\n" + result.stderr).lower()
        self.assertIn("devstack_home", combined)

    def test_aborts_when_clone_fails(self):
        self.stub.install_stub_git(clone_fail=True)
        env = self.stub.env(DEVSTACK_HOME=str(self.tmp / "fresh"))
        result = subprocess.run(
            ["/bin/sh", str(BOOTSTRAP)],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(
            result.returncode, 0,
            "bootstrap must propagate clone failure",
        )

    def test_aborts_when_fetch_fails_on_existing_checkout(self):
        self.stub.install_stub_git(fetch_fail=True)
        target = self.tmp / "existing"
        (target / ".git").mkdir(parents=True)
        _write_executable(target / "install", "#!/bin/sh\nexit 0\n")
        env = self.stub.env(DEVSTACK_HOME=str(target))
        result = subprocess.run(
            ["/bin/sh", str(BOOTSTRAP)],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(
            result.returncode, 0,
            "bootstrap must propagate fetch failure on existing checkouts",
        )


class BootstrapDocAlignment(unittest.TestCase):
    """The README must advertise the canonical one-liner and refresh-era
    feature set. These tests are documentation-correctness checks: cheap,
    grep-based, and break loudly when the wording drifts.
    """

    README = ROOT / "README.md"
    TEAM_ROLLOUT = ROOT / "TEAM_ROLLOUT.md"
    EXPECTED_ONE_LINER = (
        "curl -fsSL https://raw.githubusercontent.com/"
        "woogiekim/devstack/main/bootstrap | sh"
    )

    def test_readme_exists(self):
        self.assertTrue(self.README.exists())

    def test_readme_shows_remote_one_liner(self):
        text = self.README.read_text()
        self.assertIn(
            self.EXPECTED_ONE_LINER,
            text,
            "README must show the canonical curl-pipe one-liner verbatim",
        )

    def test_readme_does_not_lead_with_local_path(self):
        """The first ``$HOME/Developments/devstack/install`` reference, if
        present at all, must appear AFTER the curl one-liner.
        """
        text = self.README.read_text()
        local_marker = "~/Developments/devstack/install"
        one_liner_idx = text.find(self.EXPECTED_ONE_LINER)
        local_idx = text.find(local_marker)
        self.assertGreaterEqual(
            one_liner_idx, 0,
            "one-liner must appear in README",
        )
        if local_idx >= 0:
            self.assertLess(
                one_liner_idx, local_idx,
                "the remote one-liner must appear before any local "
                "checkout install reference",
            )

    def test_readme_mentions_current_feature_set(self):
        text = self.README.read_text().lower()
        # Refresh-era feature keywords that must appear somewhere.
        for keyword in (
            "devstack ui",
            "workspace",
            "doctor",
            "uninstall",
            ".devstack.json",
        ):
            self.assertIn(
                keyword, text,
                f"README missing refresh-era keyword: {keyword!r}",
            )

    def test_readme_compat_shim_note_present(self):
        text = self.README.read_text()
        # CCSTACK_ should appear ONLY inside a compat-shim paragraph; we
        # check that it is mentioned at least once so the deprecation note
        # is preserved.
        self.assertIn(
            "CCSTACK_",
            text,
            "README must keep a compat-shim note for CCSTACK_* env vars",
        )

    def test_team_rollout_points_at_one_liner(self):
        text = self.TEAM_ROLLOUT.read_text()
        self.assertIn(
            "bootstrap",
            text.lower(),
            "TEAM_ROLLOUT.md must reference the bootstrap one-liner",
        )


if __name__ == "__main__":
    unittest.main()
