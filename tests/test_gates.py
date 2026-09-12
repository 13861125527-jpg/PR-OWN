"""门控纯函数测试（18 §5 / 复验 path_io 负例）。"""

from reposage.domain.diff import parse_unified_diff
from reposage.domain.enums import GateReason
from reposage.review.reviewers.roles.gates import evaluate_gate, extract_features
from reposage.review.reviewers.roles.registry import BUILTIN_ROLE_SPECS


def _spec(role_id: str):
    return next(s for s in BUILTIN_ROLE_SPECS if s.id == role_id)


def _file(diff: str):
    files = parse_unified_diff(diff)
    files[0].language = "python"
    return files[0]


EVAL_DIFF = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,1 +1,2 @@
 def f(x):
+    return eval(x)
"""

OPEN_ONLY = """\
diff --git a/src/io_util.py b/src/io_util.py
--- a/src/io_util.py
+++ b/src/io_util.py
@@ -1,1 +1,2 @@
 from pathlib import Path
+cfg = Path("config.json").read_text()
"""

COMMENT_EVAL = """\
diff --git a/src/docs.py b/src/docs.py
--- a/src/docs.py
+++ b/src/docs.py
@@ -1,1 +1,2 @@
 x = 1
+# never call eval(user)
"""

EXCEPT_PASS = """\
diff --git a/src/h.py b/src/h.py
--- a/src/h.py
+++ b/src/h.py
@@ -1,1 +1,4 @@
 def g():
+    try:
+        f()
+    except Exception: pass
"""

NESTED_LOOP = """\
diff --git a/src/batch/job.py b/src/batch/job.py
--- a/src/batch/job.py
+++ b/src/batch/job.py
@@ -1,1 +1,4 @@
 def run(items):
+    for a in items:
+        for b in a:
+            consume(b)
"""

UPLOAD_OPEN = """\
diff --git a/src/views.py b/src/views.py
--- a/src/views.py
+++ b/src/views.py
@@ -1,1 +1,3 @@
 def save(upload):
+    filename = upload.filename
+    open(filename, "wb").write(upload.read())
"""


def test_eval_enables_security_and_general():
    features = extract_features(_file(EVAL_DIFF))
    general = evaluate_gate(features, _spec("general"), ["python"])
    security = evaluate_gate(features, _spec("security"), ["python"])
    assert general.enabled and general.reason == GateReason.ALWAYS.value
    assert security.enabled
    assert "exec_dyn" in security.matched_features
    assert evaluate_gate(features, _spec("performance"), ["python"]).enabled is False


def test_path_io_alone_does_not_enable_security():
    features = extract_features(_file(OPEN_ONLY))
    security = evaluate_gate(features, _spec("security"), ["python"])
    assert security.enabled is False
    assert security.reason == GateReason.GATE_MISS.value
    assert "path_io" in features.keyword_hits


def test_comment_eval_does_not_enable_security():
    features = extract_features(_file(COMMENT_EVAL))
    security = evaluate_gate(features, _spec("security"), ["python"])
    assert security.enabled is False
    assert "exec_dyn" not in features.keyword_hits


def test_except_pass_enables_correctness():
    features = extract_features(_file(EXCEPT_PASS))
    decision = evaluate_gate(features, _spec("correctness"), ["python"])
    assert decision.enabled
    assert "except_swallow" in decision.matched_features


def test_added_lines_gate_enables_role_without_keyword_hit():
    features = extract_features(_file(OPEN_ONLY))
    spec = _spec("correctness").model_copy(update={"gate_id": "added_lines"})
    decision = evaluate_gate(features, spec, ["python"])
    assert decision.enabled
    assert decision.matched_features == ["has_added_lines"]


def test_nested_loop_enables_performance():
    features = extract_features(_file(NESTED_LOOP))
    decision = evaluate_gate(features, _spec("performance"), ["python"])
    assert decision.enabled
    assert "loop_nested" in decision.matched_features


def test_docstring_opener_in_context_does_not_enable_security():
    """B2 复现 A：docstring 开头是 context，added 正文不得命中 exec_dyn。"""
    diff = """\
diff --git a/src/doc.py b/src/doc.py
--- a/src/doc.py
+++ b/src/doc.py
@@ -1,4 +1,5 @@
 def f():
     \"\"\"docs
+    never call eval(user)
     \"\"\"
     return 1
"""
    features = extract_features(_file(diff))
    security = evaluate_gate(features, _spec("security"), ["python"])
    assert security.enabled is False
    assert "exec_dyn" not in features.keyword_hits


def test_nested_loop_outer_in_context_enables_performance():
    """B2 复现 B：外层循环是 context，added 内层循环必须命中 loop_nested。"""
    diff = """\
diff --git a/src/nest.py b/src/nest.py
--- a/src/nest.py
+++ b/src/nest.py
@@ -1,3 +1,4 @@
 def f(xs):
     for x in xs:
+        for y in x:
             use(y)
"""
    features = extract_features(_file(diff))
    decision = evaluate_gate(features, _spec("performance"), ["python"])
    assert "loop_nested" in features.keyword_hits
    assert decision.enabled
    assert "loop_nested" in decision.matched_features


def test_multiline_docstring_does_not_enable_security():
    diff = """\
diff --git a/src/doc.py b/src/doc.py
--- a/src/doc.py
+++ b/src/doc.py
@@ -1,2 +1,5 @@
 def f():
+    \"\"\"
+    never call eval(user)
+    \"\"\"
     return 1
"""
    features = extract_features(_file(diff))
    security = evaluate_gate(features, _spec("security"), ["python"])
    assert security.enabled is False
    assert "exec_dyn" not in features.keyword_hits


def test_multiline_except_pass_enables_correctness():
    diff = """\
diff --git a/src/swallow.py b/src/swallow.py
--- a/src/swallow.py
+++ b/src/swallow.py
@@ -1,1 +1,5 @@
 def g():
+    try:
+        f()
+    except Exception:
+        pass
"""
    features = extract_features(_file(diff))
    decision = evaluate_gate(features, _spec("correctness"), ["python"])
    assert decision.enabled
    assert "except_swallow" in decision.matched_features


def test_sequential_loops_do_not_enable_performance():
    diff = """\
diff --git a/src/seq.py b/src/seq.py
--- a/src/seq.py
+++ b/src/seq.py
@@ -1,1 +1,5 @@
 def run(a, b):
+    for x in a:
+        consume(x)
+    for y in b:
+        consume(y)
"""
    features = extract_features(_file(diff))
    decision = evaluate_gate(features, _spec("performance"), ["python"])
    assert decision.enabled is False
    assert "loop_nested" not in features.keyword_hits


def test_itertools_product_enables_performance():
    diff = """\
diff --git a/src/prod.py b/src/prod.py
--- a/src/prod.py
+++ b/src/prod.py
@@ -1,1 +1,3 @@
 def run(a, b):
+    for x in itertools.product(a, b):
+        consume(x)
"""
    features = extract_features(_file(diff))
    decision = evaluate_gate(features, _spec("performance"), ["python"])
    assert decision.enabled
    assert "iter_product" in decision.matched_features


def test_gate_version_is_manifest_content_hash():
    from reposage.review.reviewers.roles.gates import (
        GATE_VERSION,
        compute_gate_version,
        gate_manifest,
    )

    manifest = gate_manifest()
    assert compute_gate_version(manifest) == GATE_VERSION
    assert "scan" in manifest
    scan_ids = {entry["id"] for entry in manifest["scan"]}
    assert {"import", "comment", "doc_open", "except_head", "pass_cont", "loop"} <= scan_ids
    mutated = dict(manifest)
    mutated["algo"] = "extract_v9"
    assert compute_gate_version(mutated) != GATE_VERSION
    mutated_kw = gate_manifest()
    mutated_kw["keywords"] = list(mutated_kw["keywords"]) + [
        {"id": "x", "pattern": "foo", "flags": 0}
    ]
    assert compute_gate_version(mutated_kw) != GATE_VERSION
    mutated_scan = gate_manifest()
    scan = [dict(entry) for entry in mutated_scan["scan"]]
    for entry in scan:
        if entry["id"] == "loop":
            entry["pattern"] = entry["pattern"] + "x"
    mutated_scan["scan"] = scan
    assert compute_gate_version(mutated_scan) != GATE_VERSION
    assert "import_security" in manifest
    assert manifest["import_security"]["pattern"]
    mutated_imp = gate_manifest()
    mutated_imp["import_security"] = dict(mutated_imp["import_security"])
    mutated_imp["import_security"]["pattern"] = mutated_imp["import_security"]["pattern"] + "x"
    assert compute_gate_version(mutated_imp) != GATE_VERSION


def test_upload_open_combo_enables_security():
    features = extract_features(_file(UPLOAD_OPEN))
    decision = evaluate_gate(features, _spec("security"), ["python"])
    assert decision.enabled
    assert decision.matched_features  # path_auth via views.py and/or combo / user_input


def test_matched_features_are_rule_ids_not_source():
    features = extract_features(_file(EVAL_DIFF))
    blob = " ".join(features.keyword_hits + features.combo_hits)
    assert "return eval" not in blob
    assert "sk-" not in blob


def test_lang_miss():
    file = _file(EVAL_DIFF)
    file.language = "go"
    features = extract_features(file)
    decision = evaluate_gate(features, _spec("general"), ["python"])
    assert decision.enabled is False
    assert decision.reason == GateReason.LANG_MISS.value


def test_single_loop_does_not_enable_performance():
    diff = """\
diff --git a/src/walk.py b/src/walk.py
--- a/src/walk.py
+++ b/src/walk.py
@@ -1,1 +1,3 @@
 def run(items):
+    for x in items:
+        consume(x)
"""
    features = extract_features(_file(diff))
    decision = evaluate_gate(features, _spec("performance"), ["python"])
    assert decision.enabled is False
    assert "loop_nested" not in features.keyword_hits


def test_gate_eval_dataset_meets_role_thresholds():
    from reposage.evals.gate import GATE_THRESHOLDS, evaluate_gate_dataset, load_gate_dataset

    samples = load_gate_dataset()
    assert len(samples) >= 20
    metrics = evaluate_gate_dataset(samples)
    for role_id, (min_p, min_r) in GATE_THRESHOLDS.items():
        m = metrics[role_id]
        assert m.precision >= min_p, f"{role_id} precision {m.precision:.3f} < {min_p} misses={m.misses}"
        assert m.recall >= min_r, f"{role_id} recall {m.recall:.3f} < {min_r} misses={m.misses}"
