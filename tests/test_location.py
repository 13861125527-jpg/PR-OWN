"""claimed → canonical 重定位测试（V1-d，05 §3 / 07 §5 location 校验）。"""


from reposage.domain.diff import parse_unified_diff
from reposage.domain.enums import FindingCategory, Severity
from reposage.domain.finding import FindingCandidate
from reposage.review.location import (
    BODY_ONLY,
    LOCATION_VALID,
    UNKNOWN_PATH,
    added_line_numbers,
    resolve_location,
)


def _file(diff: str):
    return parse_unified_diff(diff)[0]


MODIFY_DIFF = """\
diff --git a/src/a.py b/src/a.py
index 1..2 100644
--- a/src/a.py
+++ b/src/a.py
@@ -1,2 +1,6 @@
 def f():
     return 1
+
+def g():
+    return eval(x)
+    return 2
"""

DELETE_DIFF = """\
diff --git a/src/old.py b/src/old.py
deleted file mode 100644
--- a/src/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-import os
-print(1)
"""


def _cand(*, path="src/a.py", start=3, end=None, category=FindingCategory.SECURITY):
    return FindingCandidate(
        title="t",
        severity=Severity.HIGH,
        confidence=0.9,
        category=category,
        claimed_path=path,
        claimed_start_line=start,
        claimed_end_line=end,
        trigger_condition="eval(x)",
        explanation="x",
    )


def test_added_line_numbers():
    file = _file(MODIFY_DIFF)
    assert added_line_numbers(file) == {3, 4, 5, 6}  # 新增行（含空行）


def test_resolve_within_added_lines():
    file_map = {"src/a.py": _file(MODIFY_DIFF)}
    res = resolve_location(_cand(start=5), file_map)
    assert res.status == LOCATION_VALID
    assert res.path == "src/a.py"
    assert res.start_line == 5


def test_resolve_with_drift_tolerance():
    """claimed 指向新增行附近（hunk 上下文行）→ 容差内锚定最近新增行。"""
    file_map = {"src/a.py": _file(MODIFY_DIFF)}
    res = resolve_location(_cand(start=2), file_map)  # 2 是上下文行，最近新增 3
    assert res.status == LOCATION_VALID
    assert res.start_line == 3


def test_resolve_outside_tolerance_body_only():
    file_map = {"src/a.py": _file(MODIFY_DIFF)}
    res = resolve_location(_cand(start=30), file_map)  # 远离新增行
    assert res.status == BODY_ONLY
    assert res.start_line is None
    assert "不在新增行" in res.reason


def test_resolve_unknown_path_is_unknown():
    """P1-3：claimed_path 不在变更文件 → UNKNOWN_PATH（pipeline 转 SUPPRESSED）。"""
    file_map = {"src/a.py": _file(MODIFY_DIFF)}
    res = resolve_location(_cand(path="src/zzz.py"), file_map)
    assert res.status == UNKNOWN_PATH
    assert "不在本次变更文件" in res.reason


def test_resolve_missing_line_body_only():
    file_map = {"src/a.py": _file(MODIFY_DIFF)}
    res = resolve_location(_cand(start=None), file_map)
    assert res.status == BODY_ONLY
    assert "claimed_start_line" in res.reason


def test_resolve_deleted_file_body_only():
    file_map = {"src/old.py": _file(DELETE_DIFF)}
    res = resolve_location(_cand(path="src/old.py"), file_map)
    assert res.status == BODY_ONLY
    assert "无新增行" in res.reason


def test_resolve_end_line_within_region():
    file_map = {"src/a.py": _file(MODIFY_DIFF)}
    res = resolve_location(_cand(start=4, end=6), file_map)
    assert res.status == LOCATION_VALID
    assert res.start_line == 4
    assert res.end_line == 6
