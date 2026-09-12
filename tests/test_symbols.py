"""V2-B 符号抽取与 L3 检索。"""

from reposage.domain.diff import parse_unified_diff
from reposage.domain.enums import L3HitReason, SymbolKind
from reposage.domain.models import is_safe_repo_path
from reposage.review.symbols.extract import EXTRACTOR_ID, PythonAstExtractor, SymbolIndex
from reposage.review.symbols.retrieve import collect_l3_hits
from reposage.review.symbols.snapshot import HeadSnapshot


def test_safe_repo_path():
    assert is_safe_repo_path("src/a.py")
    assert not is_safe_repo_path("../etc/passwd")
    assert not is_safe_repo_path("/abs.py")
    assert not is_safe_repo_path("C:foo.py")


def test_ast_extracts_class_and_method():
    src = "class Foo:\n    def bar(self):\n        return 1\n"
    defs = PythonAstExtractor().extract("src/a.py", src)
    names = {(d.kind, d.qualname) for d in defs}
    assert (SymbolKind.CLASS, "Foo") in names
    assert (SymbolKind.METHOD, "Foo.bar") in names


def test_syntax_error_falls_back_to_indent():
    src = "def ok():\n    return 1\n\ndef broken(\n"
    defs = PythonAstExtractor().extract("src/a.py", src)
    assert any(d.name == "ok" for d in defs)


def test_cache_key_five_dimensions():
    index = SymbolIndex()
    src = "def f():\n    return 1\n"
    base = index.cache_key("a.py", src, repository_id="repo-a", head_sha="sha1")
    assert EXTRACTOR_ID in base.as_str()
    assert (
        index.cache_key("a.py", src + "\n", repository_id="repo-a", head_sha="sha1").as_str()
        != base.as_str()
    )
    assert (
        index.cache_key("b.py", src, repository_id="repo-a", head_sha="sha1").as_str()
        != base.as_str()
    )
    assert (
        index.cache_key("a.py", src, repository_id="repo-b", head_sha="sha1").as_str()
        != base.as_str()
    )
    assert (
        index.cache_key("a.py", src, repository_id="repo-a", head_sha="sha2").as_str()
        != base.as_str()
    )

    class Other:
        id = "other-extractor"

        def extract(self, path: str, source: str):
            return []

    other = SymbolIndex(extractor=Other())
    assert (
        other.cache_key("a.py", src, repository_id="repo-a", head_sha="sha1").as_str()
        != base.as_str()
    )
    assert index.defs_for("a.py", src, repository_id="repo-a", head_sha="sha1") == index.defs_for(
        "a.py", src, repository_id="repo-a", head_sha="sha1"
    )


def test_comment_added_does_not_create_modified_symbol():
    diff = """\
diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1,3 +1,4 @@
 def f():
     return 1
+# never call eval(user)
     x = 2
"""
    file = parse_unified_diff(diff)[0]
    file.language = "python"
    snap = HeadSnapshot.from_blobs(
        "head",
        {"src/a.py": "def f():\n    return 1\n# never call eval(user)\n    x = 2\n"},
    )
    hits = collect_l3_hits(file, snap).hits
    assert hits == []


def test_import_target_definition_is_retrieved():
    diff = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 from src.util import helper
 def run(xs):
     for x in xs:
+        helper(x)
"""
    file = parse_unified_diff(diff)[0]
    file.language = "python"
    snap = HeadSnapshot.from_blobs(
        "head",
        {
            "src/app.py": (
                "from src.util import helper\ndef run(xs):\n    for x in xs:\n        helper(x)\n"
            ),
            "src/util.py": "def helper(x):\n    return x\n",
        },
    )
    hits = collect_l3_hits(file, snap).hits
    assert any(h.symbol.name == "helper" and h.reason is L3HitReason.IMPORT for h in hits)


def test_cross_file_same_name_conflict_notes():
    diff = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 def run():
     a = 1
     b = 2
+    return 3
"""
    file = parse_unified_diff(diff)[0]
    file.language = "python"
    snap = HeadSnapshot.from_blobs(
        "head",
        {
            "src/app.py": "def run():\n    a = 1\n    b = 2\n    return 3\n",
            "src/a.py": "def run():\n    return 0\n",
            "src/b.py": "def run():\n    return 1\n",
        },
    )
    hits = collect_l3_hits(file, snap).hits
    run_hits = [h for h in hits if h.symbol.name == "run" and h.symbol.path != "src/app.py"]
    assert len(run_hits) >= 2
    assert any(h.notes and "证据冲突" in h.notes for h in run_hits)


def test_non_python_skips_l3():
    diff = """\
diff --git a/src/a.go b/src/a.go
--- a/src/a.go
+++ b/src/a.go
@@ -1,1 +1,2 @@
 package main
+func F() {}
"""
    file = parse_unified_diff(diff)[0]
    file.language = "go"
    snap = HeadSnapshot.from_blobs("head", {"src/a.go": "package main\nfunc F() {}\n"})
    assert collect_l3_hits(file, snap).hits == []


def test_alias_and_relative_import():
    from reposage.review.symbols.extract import parse_imports

    specs = parse_imports("import pkg.mod as pm\nfrom .mod import Foo as F\n")
    assert any(s.module == "pkg.mod" and s.local_alias == "pm" for s in specs)
    assert any(s.imported_name == "Foo" and s.local_alias == "F" and s.level == 1 for s in specs)


def test_star_import_not_expanded():
    diff = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 from src.util import *
 def run():
+    return helper(1)
"""
    file = parse_unified_diff(diff)[0]
    file.language = "python"
    snap = HeadSnapshot.from_blobs(
        "head",
        {
            "src/app.py": "from src.util import *\ndef run():\n    return helper(1)\n",
            "src/util.py": "def helper(x):\n    return x\n",
        },
    )
    hits = collect_l3_hits(file, snap).hits
    assert not any(h.symbol.name == "helper" and h.reason is L3HitReason.IMPORT for h in hits)


def test_unused_import_not_retrieved():
    diff = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 from src.util import helper, unused
 def run():
+    return helper(1)
"""
    file = parse_unified_diff(diff)[0]
    file.language = "python"
    snap = HeadSnapshot.from_blobs(
        "head",
        {
            "src/app.py": (
                "from src.util import helper, unused\ndef run():\n    return helper(1)\n"
            ),
            "src/util.py": "def helper(x):\n    return x\ndef unused(x):\n    return x\n",
        },
    )
    hits = collect_l3_hits(file, snap).hits
    assert any(h.symbol.name == "helper" for h in hits)
    assert not any(h.symbol.name == "unused" for h in hits)


def test_toplevel_added_uses_module_and_import():
    diff = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 from src.util import helper
 x = 1
+helper(x)
"""
    file = parse_unified_diff(diff)[0]
    file.language = "python"
    snap = HeadSnapshot.from_blobs(
        "head",
        {
            "src/app.py": "from src.util import helper\nx = 1\nhelper(x)\n",
            "src/util.py": "def helper(x):\n    return x\n",
        },
    )
    hits = collect_l3_hits(file, snap).hits
    assert any(h.symbol.name == "helper" and h.reason is L3HitReason.IMPORT for h in hits)


def test_per_unit_l2_ranges_keep_uncovered_def():
    src = "def big():\n" + "    x = 1\n" * 20 + "    return 1\n"
    diff = """\
diff --git a/src/big.py b/src/big.py
--- a/src/big.py
+++ b/src/big.py
@@ -10,3 +10,4 @@
     x = 1
     x = 1
+    y = 2
     x = 1
"""
    file = parse_unified_diff(diff)[0]
    file.language = "python"
    snap = HeadSnapshot.from_blobs("head", {"src/big.py": src})
    partial = collect_l3_hits(file, snap, l2_ranges=[(10, 13)]).hits
    full = collect_l3_hits(file, snap, l2_ranges=[(1, 30)]).hits
    assert any(h.symbol.name == "big" for h in partial)
    assert not any(h.symbol.name == "big" and h.reason is L3HitReason.DEFINITION for h in full)
