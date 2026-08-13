"""diff parser 与文件过滤测试（V1-a 返工：统计/quoted path/hunk 校验/删除过滤/可配置规则）。"""

from pathlib import Path

import pytest
from reposage.domain.diff import (
    DiffParseError,
    FilterRules,
    filter_files,
    parse_unified_diff,
)
from reposage.domain.enums import ChangedFileStatus, CoverageReason, DiffLineType
from reposage.domain.models import ChangedFile

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---- 解析：基础与行号映射 ----


def test_parse_modified_basic():
    files = parse_unified_diff(_fixture("modified_basic.diff"))
    assert len(files) == 1
    f = files[0]
    assert f.path == "src/main.py"
    assert f.status is ChangedFileStatus.MODIFIED
    assert len(f.hunks) == 1

    hunk = f.hunks[0]
    assert hunk.old_start == 1 and hunk.old_count == 3
    assert hunk.new_start == 1 and hunk.new_count == 4

    lines = hunk.lines
    assert lines[0].type is DiffLineType.CONTEXT and lines[0].old_ln == 1 and lines[0].new_ln == 1
    assert lines[1].type is DiffLineType.REMOVED and lines[1].old_ln == 2 and lines[1].new_ln is None
    assert lines[2].type is DiffLineType.ADDED and lines[2].new_ln == 2 and lines[2].old_ln is None
    assert lines[3].type is DiffLineType.ADDED and lines[3].new_ln == 3
    assert lines[4].type is DiffLineType.CONTEXT and lines[4].old_ln == 3 and lines[4].new_ln == 4
    assert hunk.added_lines == [2, 3]


# ---- P1-1：增删统计 ----


def test_parse_counts_additions_deletions_single_hunk():
    """P1-1：单 hunk 增删统计。"""
    f = parse_unified_diff(_fixture("modified_basic.diff"))[0]
    assert f.additions == 2
    assert f.deletions == 1


def test_parse_counts_added_file():
    f = parse_unified_diff(_fixture("added_file.diff"))[0]
    assert f.additions == 3
    assert f.deletions == 0


def test_parse_counts_deleted_file():
    f = parse_unified_diff(_fixture("deleted_file.diff"))[0]
    assert f.additions == 0
    assert f.deletions == 3


def test_parse_counts_multiple_hunks_sum():
    """P1-1：多 hunk 合计。"""
    diff = (
        "diff --git a/src/a.py b/src/a.py\n"
        "index 1..2 100644\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,1 +1,2 @@\n"
        " x\n"
        "+y\n"
        "@@ -5,2 +6,2 @@\n"
        " a\n"
        "-b\n"
        "+c\n"
    )
    f = parse_unified_diff(diff)[0]
    assert f.additions == 2  # +y, +c
    assert f.deletions == 1  # -b


def test_parse_counts_no_newline_marker_excluded():
    """P1-1：\\ No newline at end of file 标记不计入统计。"""
    diff = (
        "diff --git a/src/a.py b/src/a.py\n"
        "index 1..2 100644\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "\\ No newline at end of file\n"
        "+new\n"
        "\\ No newline at end of file\n"
    )
    f = parse_unified_diff(diff)[0]
    assert f.additions == 1
    assert f.deletions == 1


def test_parse_crlf_input():
    """测试清单 5：CRLF 输入。"""
    diff = "diff --git a/src/a.py b/src/a.py\r\nindex 1..2 100644\r\n--- a/src/a.py\r\n+++ b/src/a.py\r\n@@ -1,1 +1,2 @@\r\n x\r\n+y\r\n"
    f = parse_unified_diff(diff)[0]
    assert f.additions == 1
    assert f.path == "src/a.py"


def test_parse_added_file_anchor_lines():
    files = parse_unified_diff(_fixture("added_file.diff"))
    f = files[0]
    assert f.status is ChangedFileStatus.ADDED
    assert f.hunks[0].old_start == 0 and f.hunks[0].new_start == 1
    assert f.hunks[0].added_lines == [1, 2, 3]


def test_parse_deleted_file_no_anchors():
    f = parse_unified_diff(_fixture("deleted_file.diff"))[0]
    assert f.status is ChangedFileStatus.DELETED
    hunk = f.hunks[0]
    removed = [ln.old_ln for ln in hunk.lines if ln.type is DiffLineType.REMOVED]
    assert removed == [1, 2, 3]
    assert hunk.added_lines == []


def test_parse_rename_pure():
    f = parse_unified_diff(_fixture("rename.diff"))[0]
    assert f.status is ChangedFileStatus.RENAMED
    assert f.old_path == "src/old_name.py"
    assert f.path == "src/new_name.py"
    assert f.hunks == []


def test_parse_binary():
    f = parse_unified_diff(_fixture("binary.diff"))[0]
    assert f.is_binary is True


def test_parse_multi_file():
    files = parse_unified_diff(_fixture("multi_file.diff"))
    assert [f.path for f in files] == ["src/app.py", "config/app.yaml"]
    assert files[0].status is ChangedFileStatus.MODIFIED
    assert files[1].status is ChangedFileStatus.ADDED


def test_parse_empty():
    assert parse_unified_diff("") == []
    assert parse_unified_diff("   \n") == []


# ---- P1-2：quoted / escaped path ----


def test_parse_path_with_space_unquoted():
    diff = "diff --git a/src/my file.py b/src/my file.py\nindex 1..2 100644\n--- a/src/my file.py\n+++ b/src/my file.py\n@@ -1,1 +1,2 @@\n x\n+y\n"
    f = parse_unified_diff(diff)[0]
    assert f.path == "src/my file.py"


def test_parse_path_with_space_quoted():
    """测试清单 7：空格文件名（Git quoted 形式）。"""
    diff = (
        'diff --git "a/src/my file.py" "b/src/my file.py"\n'
        '--- "a/src/my file.py"\n'
        '+++ "b/src/my file.py"\n'
        "@@ -1,1 +1,2 @@\n x\n+y\n"
    )
    f = parse_unified_diff(diff)[0]
    assert f.path == "src/my file.py"


def test_parse_quoted_escaped_path():
    """测试清单 8：\\t \\" 转义还原。"""
    diff = (
        'diff --git "a/src/we\\tird\\"f.py" "b/src/we\\tird\\"f.py"\n'
        "@@ -1,1 +1,2 @@\n x\n+y\n"
    )
    f = parse_unified_diff(diff)[0]
    assert f.path == 'src/we\tird"f.py'


def test_parse_unicode_path():
    """测试清单 9：Unicode 路径（八进制转义 \344\270\255 = 中）。"""
    diff = (
        'diff --git "a/src/\\344\\270\\255\\346\\226\\207.py" "b/src/\\344\\270\\255\\346\\226\\207.py"\n'
        "@@ -1,1 +1,2 @@\n x\n+y\n"
    )
    f = parse_unified_diff(diff)[0]
    assert f.path == "src/中文.py"


def test_parse_illegal_escape_raises():
    """测试清单 14：非法转义明确失败，不静默。"""
    diff = 'diff --git "a/src/bad\\q.py" "b/src/bad\\q.py"\n'
    with pytest.raises(DiffParseError):
        parse_unified_diff(diff)


def test_parse_octal_with_8_or_9_raises_diff_parse_error():
    """P2（复验）：\\128 含 8/9，必须抛 DiffParseError 而非 int() 的 ValueError。"""
    diff = 'diff --git "a/src/\\128.py" "b/src/\\128.py"\n'
    with pytest.raises(DiffParseError):
        parse_unified_diff(diff)


def test_parse_rename_unsafe_old_path_rejected():
    """P1（复验）：old_path 与 path 共用路径安全校验，../ 必须被拒绝。"""
    diff = (
        "diff --git a/../secret.py b/src/safe.py\n"
        "similarity index 100%\n"
        "rename from ../secret.py\n"
        "rename to src/safe.py\n"
    )
    with pytest.raises(ValueError):
        parse_unified_diff(diff)


def test_changed_file_old_path_safe_direct():
    """P1（复验）：模型层直接构造不安全 old_path 被拒；合法 old_path 通过。"""
    with pytest.raises(ValueError):
        ChangedFile(path="src/safe.py", status=ChangedFileStatus.RENAMED, old_path="../secret.py")
    with pytest.raises(ValueError):
        ChangedFile(path="src/safe.py", status=ChangedFileStatus.RENAMED, old_path="C:/secret.py")
    ChangedFile(path="src/safe.py", status=ChangedFileStatus.RENAMED, old_path="src/old.py")  # ok


def test_parse_rename_with_content_change():
    """测试清单 10：rename 且同时包含内容修改。"""
    diff = (
        "diff --git a/src/old.py b/src/new.py\n"
        "similarity index 80%\n"
        "rename from src/old.py\n"
        "rename to src/new.py\n"
        "index 1..2 100644\n"
        "--- a/src/old.py\n"
        "+++ b/src/new.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-a\n"
        "+b\n"
        " c\n"
    )
    f = parse_unified_diff(diff)[0]
    assert f.status is ChangedFileStatus.RENAMED
    assert f.old_path == "src/old.py"
    assert f.path == "src/new.py"
    assert f.additions == 1 and f.deletions == 1


def test_parse_mode_only_change():
    """测试清单 11：mode-only change 无 hunk，文件保留。"""
    diff = (
        "diff --git a/run.sh b/run.sh\n"
        "old mode 100644\n"
        "new mode 100755\n"
    )
    f = parse_unified_diff(diff)[0]
    assert f.path == "run.sh"
    assert f.hunks == []


# ---- P2-1：hunk 完整性校验 ----


def test_parse_hunk_count_mismatch_raises():
    """测试清单 4：hunk 声明行数与实际不一致必须失败。"""
    diff = (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,9 +1,9 @@\n"
        " x\n"
    )
    with pytest.raises(DiffParseError, match="hunk 行数不一致"):
        parse_unified_diff(diff)


def test_parse_multi_file_one_corrupt_raises_not_silent():
    """测试清单 15：多文件 diff 中一个文件异常 → 明确失败，不静默成功。"""
    diff = (
        "diff --git a/src/ok.py b/src/ok.py\n"
        "--- a/src/ok.py\n"
        "+++ b/src/ok.py\n"
        "@@ -1,1 +1,2 @@\n"
        " x\n"
        "+y\n"
        "diff --git a/src/bad.py b/src/bad.py\n"
        "--- a/src/bad.py\n"
        "+++ b/src/bad.py\n"
        "@@ -1,9 +1,9 @@\n"
        " only-one-line\n"
    )
    with pytest.raises(DiffParseError, match="bad.py"):
        parse_unified_diff(diff)


def test_parse_truncated_diff_raises():
    """测试清单 14：截断 diff 不得静默成功。"""
    diff = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,3 +1,3 @@\n x\n+y\n"  # 缺 2 行
    with pytest.raises(DiffParseError):
        parse_unified_diff(diff)


# ---- P2-2：删除文件不绕过基础过滤 ----


def _file(path: str, *, status=ChangedFileStatus.MODIFIED, additions=10, deletions=2) -> ChangedFile:
    return ChangedFile(path=path, status=status, additions=additions, deletions=deletions)


def test_filter_deleted_still_skips_generated():
    """P2-2：删除的 lock/生成文件仍被过滤。"""
    files = [
        _file("poetry.lock", status=ChangedFileStatus.DELETED),
        _file("src/__pycache__/x.pyc", status=ChangedFileStatus.DELETED),
    ]
    result = filter_files(files)
    assert result.kept == []
    assert {r for _, r, _ in result.skipped} == {CoverageReason.SKIPPED_GENERATED}


def test_filter_deleted_still_skips_unsupported_language():
    files = [_file("frontend/app.ts", status=ChangedFileStatus.DELETED)]
    result = filter_files(files, languages=["python"])
    assert result.kept == []
    assert result.skipped[0][1] is CoverageReason.SKIPPED_LANG


def test_filter_deleted_still_skips_oversize():
    files = [_file("src/huge.py", status=ChangedFileStatus.DELETED, deletions=5000)]
    result = filter_files(files, max_diff_lines=2000)
    assert result.kept == []
    assert result.skipped[0][1] is CoverageReason.SKIPPED_SIZE


def test_filter_deleted_python_kept():
    """删除的 Python 文件保留（由下游 body_only/摘要处理，04 §7b）。"""
    files = [_file("src/gone.py", status=ChangedFileStatus.DELETED)]
    result = filter_files(files)
    assert [f.path for f in result.kept] == ["src/gone.py"]


# ---- 过滤：其余与集成 ----


def test_filter_keeps_python():
    files = [_file("src/main.py")]
    result = filter_files(files)
    assert [f.path for f in result.kept] == ["src/main.py"]


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
    assert {r for _, r, _ in result.skipped} == {CoverageReason.SKIPPED_GENERATED}


def test_filter_skips_binary_and_wrong_language():
    files = [_file("static/logo.png"), _file("frontend/app.ts")]
    result = filter_files(files, languages=["python"])
    assert {r for _, r, _ in result.skipped} == {CoverageReason.SKIPPED_LANG}
    assert result.kept == []


def test_filter_skips_oversize():
    files = [_file("src/huge.py", additions=5000)]
    result = filter_files(files, max_diff_lines=2000)
    assert result.kept == []
    assert result.skipped[0][1] is CoverageReason.SKIPPED_SIZE


def test_filter_max_files_truncation():
    files = [_file(f"src/m{i}.py") for i in range(5)]
    result = filter_files(files, max_files=2)
    assert len(result.kept) == 2
    assert len(result.skipped) == 3
    assert all(r is CoverageReason.SKIPPED_SIZE for _, r, _ in result.skipped)


def test_parse_then_filter_oversize_integration():
    """测试清单 2：解析结果直接进入 size filter。"""
    big = "\n".join([f"+line {i}" for i in range(50)])
    diff = (
        "diff --git a/src/big.py b/src/big.py\n"
        "--- a/src/big.py\n"
        "+++ b/src/big.py\n"
        f"@@ -0,0 +1,{len(big.splitlines())} @@\n"
        f"{big}\n"
    )
    parsed = parse_unified_diff(diff)
    assert parsed[0].additions == 50
    result = filter_files(parsed, max_diff_lines=10)
    assert result.kept == []
    assert result.skipped[0][1] is CoverageReason.SKIPPED_SIZE


# ---- P2-3：可配置过滤规则 ----


def test_filter_custom_rules_language():
    """测试清单 13：自定义语言扩展。"""
    rules = FilterRules(language_extensions={"python": [".py"], "typescript": [".ts"]})
    files = [_file("frontend/app.ts")]
    result = filter_files(files, rules=rules, languages=["python"])
    assert result.kept == []
    result2 = filter_files(files, rules=rules, languages=["python", "typescript"])
    assert [f.path for f in result2.kept] == ["frontend/app.ts"]


def test_filter_custom_rules_generated():
    rules = FilterRules(generated_patterns=[r"^gen/", r"\.tmp$"])
    files = [_file("gen/out.py"), _file("data.tmp"), _file("src/normal.py")]
    result = filter_files(files, rules=rules)
    assert [f.path for f in result.kept] == ["src/normal.py"]
    assert len(result.skipped) == 2


def test_filter_custom_rules_binary_extension():
    rules = FilterRules(binary_extensions={".py", ".bin"})
    files = [_file("src/weird.py")]  # 被自定义规则判为二进制
    result = filter_files(files, rules=rules)
    assert result.kept == []
    assert result.skipped[0][1] is CoverageReason.SKIPPED_LANG


def test_filter_custom_rules_lock_files():
    rules = FilterRules(lock_files={"custom.lock"})
    files = [_file("custom.lock")]
    result = filter_files(files, rules=rules)
    assert result.kept == []
    assert result.skipped[0][1] is CoverageReason.SKIPPED_GENERATED


# ---- P3-1：元数据回填 ----


def test_filter_enriches_metadata():
    """P3-1：保留的文件回填 language/is_generated/is_binary。"""
    files = [_file("src/main.py")]
    result = filter_files(files)
    kept = result.kept[0]
    assert kept.language == "python"
    assert kept.is_generated is False
    assert kept.is_binary is False


def test_parse_sets_language():
    f = parse_unified_diff(_fixture("modified_basic.diff"))[0]
    assert f.language == "python"
