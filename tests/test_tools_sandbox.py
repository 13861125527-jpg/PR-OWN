"""V3-A 沙箱纯函数。"""

from pathlib import Path

import pytest
from reposage.tools.sandbox import (
    SandboxError,
    glob_is_safe,
    match_repo_glob,
    normalize_repo_path,
    resolve_in_workspace,
)


def test_normalize_rejects_traversal_and_absolute():
    for raw in ("", "..", "../x", "/etc/passwd", "C:/Windows", "\\\\unc\\x", "//unc/x"):
        with pytest.raises(SandboxError):
            normalize_repo_path(raw)


def test_normalize_accepts_relative():
    assert normalize_repo_path("src/a.py") == "src/a.py"
    assert normalize_repo_path("src\\a.py") == "src/a.py"


def test_resolve_remote_mode_skips_filesystem():
    assert resolve_in_workspace(None, "src/a.py") == "src/a.py"
    with pytest.raises(SandboxError):
        resolve_in_workspace(None, "../secret")


def test_resolve_escape_via_dotdot(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    with pytest.raises(SandboxError):
        resolve_in_workspace(repo, "../secret.txt")


def test_resolve_symlink_escape(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = repo / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("no symlink privilege")
    with pytest.raises(SandboxError):
        resolve_in_workspace(repo, "link.txt")


def test_glob_rejects_dotdot_and_absolute():
    with pytest.raises(SandboxError):
        glob_is_safe("../x")
    with pytest.raises(SandboxError):
        glob_is_safe("/tmp/*")
    assert match_repo_glob("src/a.py", "src/**/*.py")
    assert not match_repo_glob("docs/a.py", "src/**/*.py")
