"""End-to-end tests for safe Git-backed installer updates."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _copy_source(source: Path, destination: Path) -> None:
    excluded = shutil.ignore_patterns(
        ".git",
        ".venv",
        ".worktrees",
        ".superpowers",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "*.pyc",
    )
    shutil.copytree(source, destination, ignore=excluded)


def make_remote_fixture(root: Path) -> tuple[Path, Path, Path]:
    remote = root / "remote.git"
    seed = root / "seed"
    checkout = root / "checkout"
    _git("init", "--bare", str(remote), cwd=root)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=remote)
    _copy_source(ROOT_DIR, seed)
    _git("init", "-b", "main", cwd=seed)
    _git("config", "user.name", "Installer Test", cwd=seed)
    _git("config", "user.email", "installer@example.invalid", cwd=seed)
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "initial", cwd=seed)
    _git("remote", "add", "origin", str(remote), cwd=seed)
    _git("push", "-u", "origin", "main", cwd=seed)
    _git("clone", str(remote), str(checkout), cwd=root)
    _git("config", "user.name", "Installer Test", cwd=checkout)
    _git("config", "user.email", "installer@example.invalid", cwd=checkout)
    return remote, seed, checkout


def _make_fake_python(fake_bin: Path) -> Path:
    stub = fake_bin / "python3"
    stub.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = -m ] && [ \"${2:-}\" = venv ]; then\n"
        "    mkdir -p \"$3/bin\"\n"
        "    printf '#!/bin/sh\\nexit 0\\n' > \"$3/bin/python\"\n"
        "    cat > \"$3/bin/pip\" <<'EOF'\n"
        "#!/bin/sh\n"
        "[ -z \"${PIP_LOG:-}\" ] || printf '%s\\n' \"$*\" >> \"$PIP_LOG\"\n"
        "exit \"${PIP_STATUS:-0}\"\n"
        "EOF\n"
        "    printf '#!/bin/sh\\nprintf '\"'\"'RCLIPBOARD_XSEL=\"0\"\\n'\"'\"'\\n' "
        "> \"$3/bin/rclipboard\"\n"
        "    chmod 0755 \"$3/bin/python\" \"$3/bin/pip\" "
        "\"$3/bin/rclipboard\"\n"
        "    exit 0\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n'
    )
    stub.chmod(0o755)
    return stub


def _make_fake_systemctl(fake_bin: Path, log: Path) -> None:
    stub = fake_bin / "systemctl"
    stub.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n"
        "case \"$*\" in\n"
        "  '--user restart rclipboard.service') "
        "exit \"${SYSTEMCTL_RESTART_STATUS:-0}\" ;;\n"
        "esac\n"
        "exit \"${SYSTEMCTL_STATUS:-0}\"\n"
    )
    stub.chmod(0o755)
    log.touch()


def _read_metadata(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


class InstallerUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.fake_bin = self.root / "fake-bin"
        self.home.mkdir()
        self.fake_bin.mkdir()
        self.remote, self.seed, self.checkout = make_remote_fixture(self.root)
        self.systemctl_log = self.root / "systemctl.log"
        _make_fake_python(self.fake_bin)
        _make_fake_systemctl(self.fake_bin, self.systemctl_log)
        self.app_dir = self.home / ".config/rclipboard"
        installed = self._run_source_installer("--no-start")
        self.assertEqual(installed.returncode, 0, installed.stderr)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _env(self, **overrides: str | None) -> dict[str, str]:
        env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
            "PYTHON_BIN": str(self.fake_bin / "python3"),
            "SYSTEMCTL_BIN": str(self.fake_bin / "systemctl"),
            "SYSTEMCTL_LOG": str(self.systemctl_log),
            "RCLIPBOARD_INSTALL_UNAME": "Linux",
            "RCLIPBOARD_INSTALL_SKIP_PIP": "1",
        }
        env.pop("XDG_CONFIG_HOME", None)
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def _run_source_installer(
        self, *args: str, **env_overrides: str | None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.checkout / "scripts/install.sh"), *args],
            cwd=self.checkout,
            env=self._env(**env_overrides),
            capture_output=True,
            text=True,
        )

    def _run_update(
        self, *args: str, **env_overrides: str | None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.app_dir / "bin/rclipboard-update"), *args],
            cwd=self.root,
            env=self._env(**env_overrides),
            capture_output=True,
            text=True,
        )

    def _push_marker(
        self,
        marker: str,
        *,
        branch: str = "main",
        remote: str = "origin",
    ) -> str:
        _git("checkout", branch, cwd=self.seed)
        installer = self.seed / "scripts/install.sh"
        installer.write_text(installer.read_text() + f"\n# {marker}\n")
        _git("add", "scripts/install.sh", cwd=self.seed)
        _git("commit", "-m", marker, cwd=self.seed)
        _git("push", remote, branch, cwd=self.seed)
        return _git("rev-parse", "HEAD", cwd=self.seed)

    def _before(self) -> tuple[str, bytes, bytes]:
        return (
            _git("rev-parse", "HEAD", cwd=self.checkout),
            (self.app_dir / "install.conf").read_bytes(),
            self.systemctl_log.read_bytes(),
        )

    def _assert_unchanged(self, before: tuple[str, bytes, bytes]) -> None:
        head, metadata, service_log = before
        self.assertEqual(_git("rev-parse", "HEAD", cwd=self.checkout), head)
        self.assertEqual((self.app_dir / "install.conf").read_bytes(), metadata)
        self.assertEqual(self.systemctl_log.read_bytes(), service_log)

    def _configure_mirror_stable(self) -> Path:
        mirror = self.root / "mirror.git"
        _git("init", "--bare", str(mirror), cwd=self.root)
        _git("symbolic-ref", "HEAD", "refs/heads/stable", cwd=mirror)
        _git("checkout", "-b", "stable", cwd=self.seed)
        _git("remote", "add", "mirror", str(mirror), cwd=self.seed)
        _git("push", "-u", "mirror", "stable", cwd=self.seed)
        _git("remote", "add", "mirror", str(mirror), cwd=self.checkout)
        _git("fetch", "mirror", "stable", cwd=self.checkout)
        _git("checkout", "-b", "stable", "--track", "mirror/stable", cwd=self.checkout)
        return mirror

    def test_successful_update_fast_forwards_refreshes_and_restarts(self) -> None:
        user_data = {
            "config.toml": (self.app_dir / "config.toml").read_bytes(),
            "age_key.txt": b"private key\n",
            "age_key.pub": b"public key\n",
            "known_keys": b"known peer\n",
        }
        for name, content in user_data.items():
            (self.app_dir / name).write_bytes(content)
        expected_head = self._push_marker("update-version-one")
        trace = self.root / "git.trace"

        result = self._run_update(GIT_TRACE=str(trace))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(_git("rev-parse", "HEAD", cwd=self.checkout), expected_head)
        self.assertEqual(_git("status", "--porcelain", cwd=self.checkout), "")
        self.assertIn(
            "# update-version-one",
            (self.app_dir / "installer/install.sh").read_text(),
        )
        self.assertIn(
            "--user restart rclipboard.service",
            self.systemctl_log.read_text(),
        )
        for name, content in user_data.items():
            self.assertEqual((self.app_dir / name).read_bytes(), content)
        trace_text = trace.read_text()
        self.assertIn("fetch -- origin main", trace_text)
        self.assertIn("merge --ff-only", trace_text)
        self.assertIsNone(
            re.search(r"(?:^|[ /])(?:stash|checkout|switch|rebase|reset)(?: |$)", trace_text),
            trace_text,
        )

    def test_untracked_file_is_rejected_before_mutation(self) -> None:
        (self.checkout / "untracked.txt").write_text("dirty\n")
        before = self._before()
        result = self._run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("clean worktree", result.stderr)
        self._assert_unchanged(before)

    def test_tracked_modification_is_rejected_before_mutation(self) -> None:
        readme = self.checkout / "README.md"
        readme.write_text(readme.read_text() + "\ndirty\n")
        before = self._before()
        result = self._run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("clean worktree", result.stderr)
        self._assert_unchanged(before)

    def test_detached_head_is_rejected_before_mutation(self) -> None:
        _git("checkout", "--detach", "HEAD", cwd=self.checkout)
        before = self._before()
        result = self._run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("branch main", result.stderr)
        self._assert_unchanged(before)

    def test_wrong_checked_out_branch_is_rejected_before_mutation(self) -> None:
        _git("checkout", "-b", "other", cwd=self.checkout)
        before = self._before()
        result = self._run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("branch main", result.stderr)
        self._assert_unchanged(before)

    def test_missing_remote_is_rejected_before_mutation(self) -> None:
        before = self._before()
        result = self._run_update("--remote", "missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("remote does not exist", result.stderr)
        self._assert_unchanged(before)

    def test_option_like_git_remote_name_is_passed_as_data(self) -> None:
        _git("remote", "add", "--", "-mirror", str(self.remote), cwd=self.checkout)
        expected_head = self._push_marker("option-like-remote")

        result = self._run_update("--remote", "-mirror")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(_git("rev-parse", "HEAD", cwd=self.checkout), expected_head)
        self.assertEqual(
            _read_metadata(self.app_dir / "install.conf")["remote"],
            "-mirror",
        )

    def test_missing_remote_branch_is_rejected_without_service_mutation(self) -> None:
        _git("checkout", "-b", "missing", cwd=self.checkout)
        before = self._before()
        result = self._run_update("--branch", "missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fetch", result.stderr.lower())
        self._assert_unchanged(before)

    def test_diverged_history_is_rejected_without_advancing_head_or_service(self) -> None:
        self._push_marker("remote-change")
        local = self.checkout / "local-change"
        local.write_text("local\n")
        _git("add", "local-change", cwd=self.checkout)
        _git("commit", "-m", "local change", cwd=self.checkout)
        before = self._before()
        result = self._run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fast-forward", result.stderr)
        self._assert_unchanged(before)

    def test_package_failure_keeps_advanced_checkout_and_old_metadata(self) -> None:
        expected_head = self._push_marker("package-failure-version")
        metadata_before = (self.app_dir / "install.conf").read_bytes()
        log_before = self.systemctl_log.read_bytes()

        result = self._run_update(
            RCLIPBOARD_INSTALL_SKIP_PIP=None,
            PIP_STATUS="23",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(_git("rev-parse", "HEAD", cwd=self.checkout), expected_head)
        self.assertEqual((self.app_dir / "install.conf").read_bytes(), metadata_before)
        self.assertEqual(self.systemctl_log.read_bytes(), log_before)
        self.assertIn("checkout", result.stderr.lower())
        self.assertIn("retry", result.stderr.lower())

    def test_restart_failure_keeps_advanced_checkout_and_old_metadata(self) -> None:
        expected_head = self._push_marker("restart-failure-version")
        metadata_before = (self.app_dir / "install.conf").read_bytes()

        result = self._run_update(SYSTEMCTL_RESTART_STATUS="29")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(_git("rev-parse", "HEAD", cwd=self.checkout), expected_head)
        self.assertEqual((self.app_dir / "install.conf").read_bytes(), metadata_before)
        self.assertIn("--user restart rclipboard.service", self.systemctl_log.read_text())
        self.assertIn("retry", result.stderr.lower())

    def test_explicit_target_persists_and_next_update_uses_it(self) -> None:
        self._configure_mirror_stable()
        first_head = self._push_marker(
            "mirror-version-one", branch="stable", remote="mirror"
        )

        first = self._run_update("--remote", "mirror", "--branch", "stable")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(_git("rev-parse", "HEAD", cwd=self.checkout), first_head)
        metadata = _read_metadata(self.app_dir / "install.conf")
        self.assertEqual(metadata["remote"], "mirror")
        self.assertEqual(metadata["branch"], "stable")

        second_head = self._push_marker(
            "mirror-version-two", branch="stable", remote="mirror"
        )
        trace = self.root / "second-update.trace"
        second = self._run_update(GIT_TRACE=str(trace))

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(_git("rev-parse", "HEAD", cwd=self.checkout), second_head)
        self.assertIn("fetch -- mirror stable", trace.read_text())

    def test_failed_explicit_target_does_not_replace_recorded_values(self) -> None:
        self._configure_mirror_stable()
        expected_head = self._push_marker(
            "mirror-package-failure", branch="stable", remote="mirror"
        )
        metadata_before = (self.app_dir / "install.conf").read_bytes()

        result = self._run_update(
            "--remote",
            "mirror",
            "--branch",
            "stable",
            RCLIPBOARD_INSTALL_SKIP_PIP=None,
            PIP_STATUS="31",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(_git("rev-parse", "HEAD", cwd=self.checkout), expected_head)
        self.assertEqual((self.app_dir / "install.conf").read_bytes(), metadata_before)

    def test_manual_internal_refresh_rejects_different_recorded_checkout(self) -> None:
        metadata = self.app_dir / "install.conf"
        metadata.write_text(
            f"repo_dir={self.seed}\nremote=origin\nbranch=main\n"
        )
        log_before = self.systemctl_log.read_bytes()

        result = self._run_source_installer(
            "--internal-refresh", "--remote", "origin", "--branch", "main"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("recorded source checkout", result.stderr)
        self.assertEqual(self.systemctl_log.read_bytes(), log_before)


if __name__ == "__main__":
    unittest.main()
