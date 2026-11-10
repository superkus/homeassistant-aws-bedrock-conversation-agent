"""Tests for release safety checks."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tomllib

from PIL import Image

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


def test_brand_icon_pngs_match_release_contract() -> None:
    """Brand icons have the required PNG dimensions and reviewed bytes."""
    brand = REPOSITORY_ROOT / "custom_components" / "bedrock_conversation" / "brand"
    expected = {
        "icon.png": (
            (256, 256),
            "effc46a856d54c2071a982d6193d74508a229c7f133e7373cb435c2a2fe0858f",
        ),
        "icon@2x.png": (
            (512, 512),
            "1a2c4610f92b6685c822f1ac4af017ab613299bd3d6a069c4ef26d628c31feff",
        ),
    }
    for filename, (expected_size, expected_hash) in expected.items():
        path = brand / filename
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash
        with Image.open(path) as image:
            assert image.format == "PNG"
            assert image.size == expected_size
            image.verify()


def test_project_metadata_references_complete_license() -> None:
    """Published metadata references the complete license, including asset exceptions."""
    metadata = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["license"] == {"file": "LICENSE"}
