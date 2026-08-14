"""SinglePassReviewer 测试（V1-d，04 §1-§2 per-file map-reduce）。"""

import pytest
from reposage.domain.diff import parse_unified_diff
from reposage.domain.enums import (
    ChangeRequestSource,
    FindingCategory,
    ReviewStrategyName,
    ReviewTaskStatus,
    Severity,
)
from reposage.domain.finding import FindingCandidate
from reposage.domain.models import ChangeRequest, CommitRef, GlobalBudget
from reposage.domain.run import ReviewRun
from reposage.providers.llm.fake import FakeLLMProvider
from reposage.review.context import ContextAssembler, unit_to_messages
from reposage.review.single_pass import SinglePassReviewer

DIFF_A = """\
diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1,1 +1,3 @@
 x
+def g():
+    return eval(x)
"""

DIFF_B = """\
diff --git a/src/b.py b/src/b.py
--- a/src/b.py
+++ b/src/b.py
@@ -1,1 +1,2 @@
 y
+os.system(z)
"""


def _req() -> ChangeRequest:
    return ChangeRequest(
        source=ChangeRequestSource.LOCAL_RANGE,
        base=CommitRef(sha="base", label="base"),
        head=CommitRef(sha="head", label="head", locked=True),
        title="t",
    )


def _units():
    assembler = ContextAssembler()
    return [
        *assembler.build_file_units(run_id="r1", change_request=_req(), file=parse_unified_diff(DIFF_A)[0]),
        *assembler.build_file_units(run_id="r1", change_request=_req(), file=parse_unified_diff(DIFF_B)[0]),
    ]


def _cand(path="src/a.py", start=3) -> FindingCandidate:
    return FindingCandidate(
        title="t",
        severity=Severity.HIGH,
        confidence=0.9,
        category=FindingCategory.SECURITY,
        claimed_path=path,
        claimed_start_line=start,
        trigger_condition="eval(x)",
    )


def _run() -> ReviewRun:
    return ReviewRun(run_id="r1", strategy=ReviewStrategyName.SINGLE_PASS)


@pytest.mark.asyncio
async def test_execute_aggregates_candidates_and_usage():
    fake = FakeLLMProvider(default_findings=[_cand()])
    reviewer = SinglePassReviewer(fake, concurrency=2)
    units = _units()
    result = await reviewer.execute(units, _run(), GlobalBudget())
    # 每 unit 一次调用 → 2 次调用、usage 汇总
    assert len(result.candidates) == 2
    assert len(result.source_run.usages) == 2
    assert len(result.source_run.tasks) == 2
    assert all(t.status is ReviewTaskStatus.COMPLETED for t in result.source_run.tasks)
    assert result.source_run.strategy is ReviewStrategyName.SINGLE_PASS
    # map-reduce：每个文件一个任务
    assert {t.target for t in result.source_run.tasks} == {"src/a.py", "src/b.py"}


@pytest.mark.asyncio
async def test_execute_unit_failure_isolated():
    """单 unit 失败 → FAILED 任务 + warning，其他文件结果保留（04 §2 部分成功）。"""

    class FlakyLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def structured(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
            return [_cand()], FakeLLMProvider()._usage()

        async def complete(self, *a, **k):
            raise NotImplementedError

        async def tool_loop(self, *a, **k):
            raise NotImplementedError

    reviewer = SinglePassReviewer(FlakyLLM(), concurrency=2)
    result = await reviewer.execute(_units(), _run(), GlobalBudget())
    assert len(result.candidates) == 1  # 第二个文件成功
    assert len(result.source_run.warnings) == 1
    failed = [t for t in result.source_run.tasks if t.status is ReviewTaskStatus.FAILED]
    assert len(failed) == 1
    assert "boom" in (failed[0].error or "")


@pytest.mark.asyncio
async def test_budget_exceeded_blocks_new_calls():
    fake = FakeLLMProvider(default_findings=[_cand()])
    reviewer = SinglePassReviewer(fake, concurrency=1)
    # 预算极小：第一个 unit 就拒绝
    budget = GlobalBudget(max_total_tokens=10)
    result = await reviewer.execute(_units(), _run(), budget)
    assert all(t.status is ReviewTaskStatus.FAILED for t in result.source_run.tasks)
    assert any("预算不足" in (t.error or "") for t in result.source_run.tasks)


@pytest.mark.asyncio
async def test_budget_consumed_from_usage():
    fake = FakeLLMProvider(default_findings=[_cand()], input_tokens=100, output_tokens=50)
    reviewer = SinglePassReviewer(fake, concurrency=2)
    budget = GlobalBudget()
    await reviewer.execute(_units(), _run(), budget)
    assert budget.tokens_used == 2 * 150  # 两次调用各 100+50
    assert budget.remaining_tokens == budget.max_total_tokens - 300


def test_unit_to_messages_layers():
    """L0/L4 → system；L1/L2 → user（不可信内容已包装）。"""
    unit = _units()[0]
    messages = unit_to_messages(unit)
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "[UNTRUSTED_CONTENT]" in messages[1]["content"]


def test_supports_single_pass_only():
    reviewer = SinglePassReviewer(FakeLLMProvider())
    assert reviewer.supports(_run())
    from reposage.domain.enums import ReviewStrategyName as N

    assert reviewer.name == N.SINGLE_PASS.value
