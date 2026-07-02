from __future__ import annotations

import os
import subprocess
import sys

import pytest

from scripts import local_ci


class TestRuffCommandResolution:
    def test_python_module_ruff_is_preferred(self, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="ruff 0.15.10\n", stderr="")

        monkeypatch.setattr(local_ci.subprocess, "run", fake_run)
        monkeypatch.setattr(local_ci.shutil, "which", lambda name: None)

        resolved = local_ci.resolve_ruff_command()

        assert resolved.command == [sys.executable, "-m", "ruff"]
        assert resolved.source == "python -m ruff"
        assert calls[0] == [sys.executable, "-m", "ruff", "--version"]

    def test_unusable_uv_does_not_count_as_available(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            if cmd[:3] == [sys.executable, "-m", "ruff"]:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No module named ruff")
            if cmd[0] == "ruff":
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found")
            if cmd[0] == "uv":
                return subprocess.CompletedProcess(
                    cmd,
                    126,
                    stdout="",
                    stderr="Permission denied: application control policy blocked uv.exe",
                )
            raise AssertionError(cmd)

        monkeypatch.setattr(local_ci.subprocess, "run", fake_run)
        monkeypatch.setattr(local_ci.shutil, "which", lambda name: "uv" if name == "uv" else None)

        with pytest.raises(local_ci.GateBlocked) as exc:
            local_ci.resolve_ruff_command()

        message = str(exc.value)
        assert "Required gate unavailable" in message
        assert "application control policy" in message
        assert "must be BLOCKED" in message


class TestRequiredGateResults:
    def test_missing_required_gate_forces_blocked_exit(self):
        result = local_ci.GateResult(
            name="code style/basic mistake check",
            command="ruff check .",
            status="blocked",
            detail="tool unavailable",
        )

        assert local_ci.has_blocking_failure([result]) is True
        assert "BLOCKED" in result.summary()

    def test_all_passed_has_no_blocking_failure(self):
        result = local_ci.GateResult(
            name="windows footguns",
            command="python scripts/check-windows-footguns.py --all",
            status="passed",
            detail="ok",
        )

        assert local_ci.has_blocking_failure([result]) is False
        assert "PASS" in result.summary()

    def test_skipped_gate_is_not_blocking_for_prose_only_change(self):
        result = local_ci.GateResult(
            name="code style/basic mistake check",
            command="ruff check <changed-python-files>",
            status="skipped",
            detail="no changed Python files",
        )

        assert local_ci.has_blocking_failure([result]) is False
        assert "SKIP" in result.summary()


class TestScopedGateTargets:
    def test_python_change_gets_file_scoped_gates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(local_ci, "REPO_ROOT", tmp_path)
        changed = tmp_path / "agent" / "verify_hooks.py"
        changed.parent.mkdir()
        changed.write_text("print('x')\n", encoding="utf-8")

        assert local_ci.build_gate_targets(["agent/verify_hooks.py"]) == (
            ["agent/verify_hooks.py"],
            ["agent/verify_hooks.py"],
        )

    def test_prose_only_change_skips_code_gates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(local_ci, "REPO_ROOT", tmp_path)
        readme = tmp_path / "README.md"
        readme.write_text("docs\n", encoding="utf-8")

        assert local_ci.build_gate_targets(["README.md"]) == ([], [])

    def test_ci_or_dependency_change_fails_open_to_full_tree(self, tmp_path, monkeypatch):
        monkeypatch.setattr(local_ci, "REPO_ROOT", tmp_path)

        assert local_ci.build_gate_targets([".github/workflows/ci.yml"]) == (
            ["."],
            ["--all"],
        )
        assert local_ci.build_gate_targets(["pyproject.toml"]) == (["."], ["--all"])

    def test_unknown_non_prose_change_fails_open_to_full_tree(self, tmp_path, monkeypatch):
        monkeypatch.setattr(local_ci, "REPO_ROOT", tmp_path)
        makefile = tmp_path / "Makefile"
        makefile.write_text("test:\n\tpytest\n", encoding="utf-8")

        assert local_ci.build_gate_targets(["Makefile"]) == (["."], ["--all"])

    def test_discover_changed_paths_includes_untracked_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(local_ci, "REPO_ROOT", tmp_path)
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        tracked = tmp_path / "tracked.py"
        tracked.write_text("print('tracked')\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
            },
        )
        untracked = tmp_path / "new_gate.py"
        untracked.write_text("print('new')\n", encoding="utf-8")

        assert "new_gate.py" in local_ci.discover_changed_paths()
