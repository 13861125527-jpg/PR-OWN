"""V2-B L3 装配：开关关闭字节兼容；命中写入 user 侧。"""

from reposage.domain.diff import parse_unified_diff
from reposage.domain.enums import ContextLayer
from reposage.domain.models import ChangeRequest, ChangeRequestSource, CommitRef
from reposage.review.context import ContextAssembler, unit_to_messages
from reposage.review.symbols.snapshot import HeadSnapshot

_REQ = ChangeRequest(
    source=ChangeRequestSource.LOCAL_RANGE,
    base=CommitRef(sha="base", label="base"),
    head=CommitRef(sha="head", label="head", locked=True),
    title="t",
)


def _file(diff: str):
    f = parse_unified_diff(diff)[0]
    f.language = "python"
    return f


IMPORT_DIFF = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 from src.util import helper
 def run(xs):
     for x in xs:
+        helper(x)
"""


def test_symbol_retrieval_off_matches_v1_messages():
    file = _file(IMPORT_DIFF)
    off = ContextAssembler(symbol_retrieval=False).build_file_units(
        run_id="r", change_request=_REQ, file=file
    )[0]
    snap = HeadSnapshot.from_blobs(
        "head",
        {
            "src/app.py": "from src.util import helper\ndef run(xs):\n    for x in xs:\n        helper(x)\n",
            "src/util.py": "def helper(x):\n    return x\n",
        },
    )
    still_off = ContextAssembler(symbol_retrieval=False).build_file_units(
        run_id="r", change_request=_REQ, file=file, snapshot=snap
    )[0]
    assert unit_to_messages(off) == unit_to_messages(still_off)
    assert not any(c.layer is ContextLayer.L3 for c in off.context.chunks)


def test_l3_chunk_in_user_untrusted():
    file = _file(IMPORT_DIFF)
    snap = HeadSnapshot.from_blobs(
        "head",
        {
            "src/app.py": "from src.util import helper\ndef run(xs):\n    for x in xs:\n        helper(x)\n",
            "src/util.py": "def helper(x):\n    return x\n",
        },
    )
    unit = ContextAssembler(symbol_retrieval=True).build_file_units(
        run_id="r", change_request=_REQ, file=file, snapshot=snap
    )[0]
    l3 = [c for c in unit.context.chunks if c.layer is ContextLayer.L3]
    assert l3
    assert "helper" in l3[0].content
    messages = unit_to_messages(unit)
    assert "helper" in messages[1]["content"]
    assert "[UNTRUSTED_CONTENT]" in messages[1]["content"]
    assert "不得当作已验证证据" in messages[0]["content"]


def test_l3_respects_nominal_cap_and_zero_space_coverage():
    from reposage.domain.enums import CoverageReason
    from reposage.review.context import ContextAssembler, ContextBudget

    file = _file(IMPORT_DIFF)
    snap = HeadSnapshot.from_blobs(
        "head",
        {
            "src/app.py": "from src.util import helper\ndef run(xs):\n    for x in xs:\n        helper(x)\n",
            "src/util.py": "def helper(x):\n    return x\n",
        },
    )
    budget = ContextBudget(
        total_tokens=400,
        l0_ratio=0.05,
        l1_ratio=0.05,
        l2_ratio=0.50,
        l3_ratio=0.15,
        l4_ratio=0.10,
        reserve_ratio=0.15,
    )
    unit = ContextAssembler(budget=budget, symbol_retrieval=True).build_file_units(
        run_id="r", change_request=_REQ, file=file, snapshot=snap
    )[0]
    l3_tokens = sum(c.tokens for c in unit.context.chunks if c.layer is ContextLayer.L3)
    assert l3_tokens <= budget.l3_tokens
    assert unit.context.total_tokens <= unit.input_limit

    zero = ContextBudget(
        total_tokens=400,
        l0_ratio=0.05,
        l1_ratio=0.05,
        l2_ratio=0.65,
        l3_ratio=0.0,
        l4_ratio=0.10,
        reserve_ratio=0.15,
    )
    empty = ContextAssembler(budget=zero, symbol_retrieval=True).build_file_units(
        run_id="r", change_request=_REQ, file=file, snapshot=snap
    )[0]
    assert not any(c.layer is ContextLayer.L3 for c in empty.context.chunks)
    assert any(
        i.reason is CoverageReason.TRUNCATED and i.target.startswith("symbol:")
        for i in empty.coverage.items
    )
    assert any(c.layer is ContextLayer.L2 for c in empty.context.chunks)
    assert empty.context.total_tokens <= empty.input_limit
