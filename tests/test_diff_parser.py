"""diff parser 与文件过滤测试（V1-a，本地 fixture 样本）。"""

from pathlib import Path

import pytest
from reposage.domain.diff import DiffParseError, filter_files, parse_unified_diff
from reposage.domain.enums import ChangedFileStatus, CoverageReason, DiffLineType
from reposage.domain.models import ChangedFile

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---- 解析 ----

def test_parse_modified_basic():
    files = parse_unified_diff(_fixture("modified_basic.diff"))
    assert len(files) == 1
    f = files[0]
    assert f.path == "src/main.py"
    assert f.status is ChangedFileStatus.MODIFIED
    assert len(f.hunks) == 1

    hunk = f.hunks[0]
    assert hunk.old_start == 1 and hunk.old_count == 3
    assert hunk.new_start == 1 and hunk.new_count == 5

    # 行号映射（V1-a 核心）：context 双号、removed 只 old、added 只 new
    lines = hunk.lines
    assert lines[0].type is DiffLineType.CONTEXT and lines[0].old_ln == 1 and lines[0].new_ln == 1
    assert lines[1].type is DiffLineType.REMOVED and lines[1].old_ln == 2 and lines[1].new_ln is None
    assert lines[2].type is DiffLineType.ADDED and lines[2].new_ln == 2 and lines[2].old_ln is None
    assert lines[3].type is DiffLineType.ADDED and lines[3].new_ln == 3
    assert lines[4].type is DiffLineType.CONTEXT and lines[4].old_ln == 3 and lines[4].new_ln == 4

    assert hunk.added_lines == [2, 3]  # 评论候选锚点


def test_parse_added_file():
    files = parse_unified_diff(_fixture("added_file.diff"))
    assert len(files) == 1
    f = files[0]
    assert f.status is ChangedFileStatus.ADDED
    assert f.hunks[0].old_start == 0 and f.hunks[0].new_start == 1
    added = f.hunks[0].added_lines
    assert added == [1, 2, 3]


def test_parse_deleted_file():
    files = parse_unified_diff(_fixture("deleted_file.diff"))
    assert len(files) == 1
    f = files[0]
    assert f.status is ChangedFileStatus.DELETED
    hunk = f.hunks[0]
    assert hunk.new_start == 0 and hunk.new_count == 0
    removed = [ln.old_ln for ln in hunk.lines if ln.type is DiffLineType.REMOVED]
    assert removed == [1, 2, 3]
    assert hunk.added_lines == []  # 删除无新增行 → 无行内锚点（04 §7b）


def test_parse_rename():
    files = parse_unified_diff(_fixture("rename.diff"))
    assert len(files) == 1
    f = files[0]
    assert f.status is ChangedFileStatus.RENAMED
    assert f.old_path == "src/old_name.py"
    assert f.path == "src/new_name.py"
    assert f.hunks == []  # pure rename 无 hunk


def test_parse_binary():
    files = parse_unified_diff(_fixture("binary.diff"))
    assert len(files) == 1
    assert files[0].is_binary is True


def test_parse_multi_file():
    files = parse_unified_diff(_fixture("multi_file.diff"))
    assert len(files) == 2
    assert [f.path for f in files] == ["src/app.py", "config/app.yaml"]
    assert files[0].status is ChangedFileStatus.MODIFIED
    assert files[1].status is ChangedFileStatus.ADDED


def test_parse_empty():
    assert parse_unified_diff("") == []
    assert parse_unified_diff("   \n") == []


def test_parse_malformed_hunk_line_raises():
    bad = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n????\n"
    with pytest.raises(DiffParseError):
        parse_unified_diff(bad)


# ---- 过滤 ----

def _file(path: str, *, status=ChangedFileStatus.MODIFIED, additions=10, deletions=2) -> ChangedFile:
    return ChangedFile(path=path, status=status, additions=additions, deletions=deletions)


def test_filter_keeps_python_and_delete():
    files = [_file("src/main.py"), _file("src/gone.py", status=ChangedFileStatus.DELETED)]
    result = filter_files(files)
    assert [f.path for f in result.kept] == ["src/main.py", "src/gone.py"]  # deleted 保留


def test_filter_skips_generated_and_lockfile():
    files = [
        _file("src/__pycache__/main.cpython-312.pyc"),
        _file("poetry.lock"),
        _file("src/generated_pb2.py"),
        _file("dist/bundle.min.js"),
    ]
    result = filter_files(files)
    assert result.kept == []
    assert len(result.skipped) == 4
    reasons = {r for _, r, _ in result.skipped}
    assert reasons == {CoverageReason.SKIPPED_GENERATED}


def test_filter_skips_binary_and_wrong_language():
    files = [_file("static/logo.png"), _file("frontend/app.ts")]
    result = filter_files(files, languages=["python"])
    reasons = {r for _, r, _ in result.skipped}
    assert reasons == {CoverageReason.SKIPPED_LANG}
    assert result.kept == []


def test_filter_skips_oversize():
    files = [_file("src/huge.py", additions=5000)]
    result = filter_files(files, max_added_lines=2000)
    assert result.kept == []
    assert result.skipped[0][1] is CoverageReason.SKIPPED_SIZE


def test_filter_max_files_truncation():
    files = [_file(f"src/m{i}.py") for i in range(5)]
    result = filter_files(files, max_files=2)
    assert len(result.kept) == 2
    assert len(result.skipped) == 3
    assert all(r is CoverageReason.SKIPPED_SIZE for _, r, _ in result.skipped)
