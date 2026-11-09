"""Tests for release safety checks."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_release_rejects_untracked_files(tmp_path: Path) -> None:
    """The release target stops before tagging when an untracked file exists."""
    shutil.copy(REPOSITORY_ROOT / "Makefile", tmp_path / "Makefile")
    component = tmp_path / "custom_components" / "bedrock_conversation"
    component.mkdir(parents=True)
    (component / "manifest.json").write_text('{"version": "9.9.9"}', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("Test release\n", encoding="utf-8")
    (tmp_path / "test-overrides.mk").write_text(
        "venv:\n\t@:\n\ndeps:\n\t@:\n\ntest:\n\t@:\n\nlint:\n\t@:\n", encoding="utf-8"
    )

    real_git = shutil.which("git")
    assert real_git is not None
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$GIT_CALL_LOG"\n'
        'if [ "$1" = "tag" ] && [ "$2" = "-a" ]; then\n'
        '  : > "$TAG_ATTEMPT_MARKER"\n'
        "  exit 99\n"
        "fi\n"
        'exec "$REAL_GIT" "$@"\n',
        encoding="utf-8",
    )
    fake_git.chmod(0o755)

    assert _run(real_git, "init", cwd=tmp_path).returncode == 0
    tracked = [
        "Makefile",
        "CHANGELOG.md",
        "custom_components/bedrock_conversation/manifest.json",
        "test-overrides.mk",
        "bin/git",
    ]
    assert _run(real_git, "add", *tracked, cwd=tmp_path).returncode == 0
    commit = _run(
        real_git,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "fixture",
        cwd=tmp_path,
    )
    assert commit.returncode == 0, commit.stderr

    (tmp_path / "accidental-untracked.txt").write_text("must block release\n", encoding="utf-8")
    marker = tmp_path / ".tag-attempted"
    call_log = tmp_path / ".git-calls"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "REAL_GIT": real_git,
        "GIT_CALL_LOG": str(call_log),
        "TAG_ATTEMPT_MARKER": str(marker),
    }
    result = _run(
        "make",
        "-f",
        "Makefile",
        "-f",
        "test-overrides.mk",
        "release",
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode != 0
    assert call_log.exists(), f"release gate did not run:\n{result.stdout}\n{result.stderr}"
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert "diff-index --quiet HEAD --" in calls, result.stderr
    assert not marker.exists(), "release reached tag creation despite an untracked file"
