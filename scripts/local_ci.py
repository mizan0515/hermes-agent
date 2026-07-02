#!/usr/bin/env python3
"""Local required-gate runner for Hermes PR work.

This script is the local source of truth for required validation. It makes one
rule mechanical: a required gate that cannot run is BLOCKED, not
PASS-with-a-caveat. Remote services may provide extra signal, but PASS/PR_READY
must be based on local gates that actually executed.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
RUFF_VERSION = "0.15.10"
PYTHON_EXTENSIONS = {".py", ".pyi"}
FAIL_OPEN_PATH_PREFIXES = (".github/", ".githooks/")
FAIL_OPEN_FILES = {"AGENTS.md", "pyproject.toml", "scripts/local_ci.py", "uv.lock"}
PROSE_EXTENSIONS = {".md", ".mdx", ".txt", ".rst"}
PROSE_PATH_PREFIXES = ("docs/", "website/docs/", "website/i18n/")


class GateBlocked(RuntimeError):
    """Raised when a required gate cannot be executed at all."""


@dataclass(frozen=True)
class CommandResolution:
    command: list[str]
    source: str


@dataclass(frozen=True)
class GateResult:
    name: str
    command: str
    status: str  # passed | failed | blocked
    detail: str = ""

    def summary(self) -> str:
        label = {
            "passed": "PASS",
            "failed": "FAIL",
            "blocked": "BLOCKED",
            "skipped": "SKIP",
        }.get(self.status, self.status.upper())
        suffix = f" — {self.detail}" if self.detail else ""
        return f"{label}: {self.name} ({self.command}){suffix}"


def _run_probe(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(cmd),
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(list(cmd), 126, stdout="", stderr=str(exc))
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            list(cmd), 124, stdout=exc.stdout or "", stderr=exc.stderr or "timed out"
        )


def _format_probe_failure(label: str, result: subprocess.CompletedProcess[str]) -> str:
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    tail = stderr or stdout or f"exit {result.returncode}"
    return f"{label}: {tail}"


def resolve_ruff_command() -> CommandResolution:
    """Find an executable ruff command, or fail closed with GateBlocked.

    Resolution order intentionally prefers the current Python environment. On
    Windows enterprise/app-control setups, a standalone ``uv.exe`` may exist on
    PATH but be blocked by policy; that must not be treated as a passing local
    gate. We probe every candidate and include the failure evidence in the
    BLOCKED message.
    """

    failures: list[str] = []

    python_module = [sys.executable, "-m", "ruff"]
    result = _run_probe([*python_module, "--version"])
    if result.returncode == 0:
        return CommandResolution(python_module, "python -m ruff")
    failures.append(_format_probe_failure("python -m ruff", result))

    if shutil.which("ruff"):
        ruff_cmd = ["ruff"]
        result = _run_probe([*ruff_cmd, "--version"])
        if result.returncode == 0:
            return CommandResolution(ruff_cmd, "ruff on PATH")
        failures.append(_format_probe_failure("ruff", result))

    if shutil.which("uv"):
        uv_cmd = ["uv", "run", "--with", f"ruff=={RUFF_VERSION}", "ruff"]
        result = _run_probe([*uv_cmd, "--version"])
        if result.returncode == 0:
            return CommandResolution(uv_cmd, "uv run --with ruff")
        failures.append(_format_probe_failure("uv-provided ruff", result))
    else:
        failures.append("uv-provided ruff: uv not found on PATH")

    details = " | ".join(failures)
    raise GateBlocked(
        "Required gate unavailable: code style/basic mistake automatic check "
        f"(ruff) could not run. Evidence: {details}. A missing required gate "
        "must be BLOCKED, never PASS. Install the dev dependency "
        f"(`python -m pip install ruff=={RUFF_VERSION}` or a working uv) and rerun."
    )


def _is_prose_only(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized.endswith(tuple(PROSE_EXTENSIONS)) or normalized.startswith(
        PROSE_PATH_PREFIXES
    )


def _is_python_path(path: str) -> bool:
    return Path(path).suffix in PYTHON_EXTENSIONS


def _existing_targets(paths: Sequence[str]) -> list[str]:
    targets: list[str] = []
    for path in paths:
        normalized = path.replace("\\", "/")
        if (REPO_ROOT / normalized).exists():
            targets.append(normalized)
    return sorted(dict.fromkeys(targets))


def discover_changed_paths() -> list[str]:
    """Return local changed paths, fail-open to [] if git cannot answer."""

    commands = [
        ["git", "diff", "--name-only", "--cached"],
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]
    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if upstream.returncode == 0 and upstream.stdout.strip():
        commands.append(["git", "diff", "--name-only", f"{upstream.stdout.strip()}...HEAD"])

    changed: set[str] = set()
    for cmd in commands:
        result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
        if result.returncode != 0:
            return []
        changed.update(line.strip().replace("\\", "/") for line in result.stdout.splitlines())
    return sorted(path for path in changed if path)


def build_gate_targets(paths: Sequence[str], *, full: bool = False) -> tuple[list[str], list[str]]:
    """Return (ruff_targets, windows_footgun_targets) for a small local gate.

    Empty or local-automation/dependency edits fail open to full-tree checks.
    Plain prose edits skip code gates. Python edits run only the changed Python
    files. Unknown non-prose files fail open to full-tree checks rather than
    silently skipping a gate that might matter.
    """

    normalized = [p.strip().replace("\\", "/") for p in paths if p.strip()]
    if full or not normalized:
        return ["."], ["--all"]
    if any(p.startswith(FAIL_OPEN_PATH_PREFIXES) or p in FAIL_OPEN_FILES for p in normalized):
        return ["."], ["--all"]

    python_targets = _existing_targets(p for p in normalized if _is_python_path(p))
    unknown_non_prose = [
        p
        for p in normalized
        if not _is_python_path(p) and not _is_prose_only(p) and (REPO_ROOT / p).exists()
    ]
    if unknown_non_prose:
        return ["."], ["--all"]
    if not python_targets:
        return [], []
    return python_targets, python_targets


def run_command(name: str, cmd: Sequence[str]) -> GateResult:
    printable = shlex.join(list(cmd))
    print(f"▶ {name}: {printable}", flush=True)
    result = subprocess.run(list(cmd), cwd=REPO_ROOT)
    if result.returncode == 0:
        return GateResult(name=name, command=printable, status="passed")
    return GateResult(
        name=name,
        command=printable,
        status="failed",
        detail=f"exit {result.returncode}",
    )


def run_ruff(targets: Sequence[str]) -> GateResult:
    if not targets:
        return GateResult(
            name="code style/basic mistake automatic check",
            command="ruff check <changed-python-files>",
            status="skipped",
            detail="no changed Python files",
        )
    try:
        resolved = resolve_ruff_command()
    except GateBlocked as exc:
        target_text = shlex.join(["ruff", "check", *targets])
        return GateResult(
            name="code style/basic mistake automatic check",
            command=target_text,
            status="blocked",
            detail=str(exc),
        )
    return run_command(
        "code style/basic mistake automatic check",
        [*resolved.command, "check", *targets],
    )


def run_windows_footguns(targets: Sequence[str]) -> GateResult:
    if not targets:
        return GateResult(
            name="Windows unsafe-code guard",
            command="python scripts/check-windows-footguns.py <changed-python-files>",
            status="skipped",
            detail="no changed Python files",
        )
    return run_command(
        "Windows unsafe-code guard",
        [sys.executable, "scripts/check-windows-footguns.py", *targets],
    )


def has_blocking_failure(results: Sequence[GateResult]) -> bool:
    return any(result.status in {"failed", "blocked"} for result in results)


def install_git_hooks() -> int:
    hooks_dir = REPO_ROOT / ".githooks"
    hook_path = hooks_dir / "pre-push"
    hooks_dir.mkdir(exist_ok=True)
    hook_path.write_text(
        "#!/usr/bin/env bash\n"
        "# Generated by scripts/local_ci.py --install-git-hooks.\n"
        "# Required gates must run before pushing. If this blocks, fix the gate; do not report PASS.\n"
        "# Local validation is authoritative; do not defer PASS/PR_READY to remote CI.\n"
        "set -euo pipefail\n"
        "ROOT=\"$(git rev-parse --show-toplevel)\"\n"
        "exec python \"$ROOT/scripts/local_ci.py\" --skip-hook-install\n",
        encoding="utf-8",
    )
    try:
        hook_path.chmod(0o755)
    except OSError:
        pass
    result = subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"], cwd=REPO_ROOT
    )
    if result.returncode != 0:
        return result.returncode
    print("Installed local pre-push hook at .githooks/pre-push")
    return 0


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run required local CI gates. Unavailable required gates fail closed."
    )
    parser.add_argument(
        "--install-git-hooks",
        action="store_true",
        help="Install the tracked pre-push hook via git config core.hooksPath=.githooks.",
    )
    parser.add_argument(
        "--skip-hook-install",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-windows-footguns",
        action="store_true",
        help="Skip the Windows unsafe-code guard (not recommended; for narrow debugging only).",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run full-tree gates instead of changed-file scoped gates.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Explicit changed paths to gate. Defaults to local git changes; empty diff fails open to full-tree gates.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.install_git_hooks:
        hook_rc = install_git_hooks()
        if hook_rc != 0:
            return hook_rc

    changed_paths = args.paths or discover_changed_paths()
    ruff_targets, windows_targets = build_gate_targets(changed_paths, full=args.full)
    if changed_paths:
        print("Changed paths considered:")
        for path in changed_paths:
            print(f"- {path}")
    else:
        print("Changed paths considered: none detected; running full-tree gates (fail-open).")

    results = [run_ruff(ruff_targets)]
    if not args.no_windows_footguns:
        results.append(run_windows_footguns(windows_targets))

    print("\nRequired gate summary:")
    for result in results:
        print(f"- {result.summary()}")

    if has_blocking_failure(results):
        print(
            "\nRequired gate outcome: BLOCKED. Do not merge or report PASS until every "
            "required gate above is PASS.",
            file=sys.stderr,
        )
        return 1

    print("\nRequired gate outcome: PASS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
