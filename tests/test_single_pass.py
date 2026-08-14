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
from reposage.review.context import ContextAssembler, ContextBudget, unit_to_messages
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
    reviewer = SinglePassReviewer(fake, file_tasks=2, model_requests=2)
    units = _units()
    result = await reviewer.execute(units, _run(), GlobalBudget())
    # 每文件一次调用 → 2 次调用、usage 汇总
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

    reviewer = SinglePassReviewer(FlakyLLM(), file_tasks=2, model_requests=2)
    result = await reviewer.execute(_units(), _run(), GlobalBudget())
    assert len(result.candidates) == 1  # 第二个文件成功
    assert len(result.source_run.warnings) == 1
    failed = [t for t in result.source_run.tasks if t.status is ReviewTaskStatus.FAILED]
    assert len(failed) == 1
    assert "boom" in (failed[0].error or "")


@pytest.mark.asyncio
async def test_budget_exceeded_blocks_new_calls():
    fake = FakeLLMProvider(default_findings=[_cand()])
    reviewer = SinglePassReviewer(fake, file_tasks=1, model_requests=1)
    # 预算极小：第一个请求就被拒绝
    budget = GlobalBudget(max_total_tokens=10)
    result = await reviewer.execute(_units(), _run(), budget)
    assert all(t.status is ReviewTaskStatus.FAILED for t in result.source_run.tasks)
    assert any("预算不足" in (t.error or "") for t in result.source_run.tasks)


@pytest.mark.asyncio
async def test_budget_consumed_from_usage():
    fake = FakeLLMProvider(default_findings=[_cand()], input_tokens=100, output_tokens=50)
    reviewer = SinglePassReviewer(fake, file_tasks=2, model_requests=2)
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
# ================= V1-d 返工：P0-1 硬预算 / P1-1 调度时序 =================


def _big_units():
    """src/big.py 200 行 + 小预算 → 多个 unit（同文件）。"""
    assembler = ContextAssembler(budget=ContextBudget(total_tokens=400))
    big = "\n".join([f"+line {i} = 1" for i in range(200)])
    diff = (
        "diff --git a/src/big.py b/src/big.py\n"
        "--- a/src/big.py\n"
        "+++ b/src/big.py\n"
        f"@@ -0,0 +1,{len(big.splitlines())} @@\n"
        f"{big}\n"
    )
    return assembler.build_file_units(
        run_id="r1", change_request=_req(), file=parse_unified_diff(diff)[0]
    )


class _TimingLLM:
    """记录每次调用的开始/结束墙钟，用于时序断言。"""

    def __init__(self, candidates_per_call: int = 1) -> None:
        self.candidates_per_call = candidates_per_call
        self.intervals: list[tuple[float, float]] = []
        self.calls = 0

    async def structured(self, messages, **kwargs):
        import asyncio
        import time

        start = time.monotonic()
        self.calls += 1
        await asyncio.sleep(0.05)
        end = time.monotonic()
        self.intervals.append((start, end))
        from reposage.domain.models import ModelUsage

        return [_cand() for _ in range(self.candidates_per_call)], ModelUsage(
            model="m", role="smoke", input_tokens=10, output_tokens=5
        )

    async def complete(self, *a, **k):
        raise NotImplementedError

    async def tool_loop(self, *a, **k):
        raise NotImplementedError


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


@pytest.mark.asyncio
async def test_budget_atomic_reservation_concurrent():
    """P0-1：预算只够一个请求的预留时，两个并发文件只有一个放行。"""
    # _TimingLLM 在 structured 内 sleep，制造"预留后未结算"的并发窗口
    llm = _TimingLLM()
    reviewer = SinglePassReviewer(llm, file_tasks=2, model_requests=2)
    # 单个 unit 预留 ≈ input 110 + output_reserve 4800 = 4910；9000 只够一个在途
    budget = GlobalBudget(max_total_tokens=9000)
    result = await reviewer.execute(_units(), _run(), budget)
    statuses = [t.status for t in result.source_run.tasks]
    assert statuses.count(ReviewTaskStatus.COMPLETED) == 1
    assert statuses.count(ReviewTaskStatus.FAILED) == 1
    assert any("预算不足" in (t.error or "") for t in result.source_run.tasks if t.error)
    # 预留无泄漏：tokens_used 只含成功那次（10+5）
    assert budget.tokens_used == 15
    assert budget.remaining_tokens == budget.max_total_tokens - 15


@pytest.mark.asyncio
async def test_budget_released_on_failure():
    """P0-1：模型失败 → settle 释放预留，预算不泄漏。"""
    budget = GlobalBudget()

    class BoomLLM:
        async def structured(self, messages, **kwargs):
            raise RuntimeError("boom")

        async def complete(self, *a, **k):
            raise NotImplementedError

        async def tool_loop(self, *a, **k):
            raise NotImplementedError

    reviewer = SinglePassReviewer(BoomLLM(), file_tasks=1, model_requests=1)
    await reviewer.execute(_units(), _run(), budget)
    assert budget.tokens_used == 0
    assert budget.cost_used == 0.0
    assert budget.remaining_tokens == budget.max_total_tokens


@pytest.mark.asyncio
async def test_same_file_units_serial_not_overlapping():
    """P1-1：同一大文件的多个 unit 串行执行（不重叠）。"""
    units = _big_units()
    assert len(units) > 1
    llm = _TimingLLM()
    reviewer = SinglePassReviewer(llm, file_tasks=3, model_requests=3)
    await reviewer.execute(units, _run(), GlobalBudget())
    assert llm.calls == len(units)
    for i in range(len(llm.intervals) - 1):
        assert not _overlap(llm.intervals[i], llm.intervals[i + 1])  # 同文件串行


@pytest.mark.asyncio
async def test_different_files_concurrent():
    """P1-1：不同文件并发（file_tasks>=2 时重叠）。"""
    llm = _TimingLLM()
    reviewer = SinglePassReviewer(llm, file_tasks=2, model_requests=2)
    await reviewer.execute(_units(), _run(), GlobalBudget())  # 两个文件
    assert len(llm.intervals) == 2
    assert _overlap(llm.intervals[0], llm.intervals[1])  # 并发执行


@pytest.mark.asyncio
async def test_model_requests_limits_despite_file_tasks():
    """P1-1：file_tasks 与 model_requests 独立限流（model_requests=1 强制全串行）。"""
    llm = _TimingLLM()
    reviewer = SinglePassReviewer(llm, file_tasks=3, model_requests=1)  # 文件可并发但模型请求串行
    await reviewer.execute(_units(), _run(), GlobalBudget())
    assert len(llm.intervals) == 2
    assert not _overlap(llm.intervals[0], llm.intervals[1])  # model_requests=1 → 不重叠


@pytest.mark.asyncio
async def test_file_task_per_file_with_partial_failure():
    """P1-1：每文件一个 ReviewTask；第二块失败保留第一块结果并标记 FAILED。"""
    units = _big_units()
    assert len(units) >= 2

    class FailSecondLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def structured(self, messages, **kwargs):
            from reposage.domain.models import ModelUsage

            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("block2 boom")
            return [_cand()], ModelUsage(model="m", role="s", input_tokens=5, output_tokens=3)

        async def complete(self, *a, **k):
            raise NotImplementedError

        async def tool_loop(self, *a, **k):
            raise NotImplementedError

    llm = FailSecondLLM()
    reviewer = SinglePassReviewer(llm, file_tasks=1, model_requests=1)
    result = await reviewer.execute(units, _run(), GlobalBudget())
    assert len(result.source_run.tasks) == 1  # 每文件一个任务
    task = result.source_run.tasks[0]
    assert task.status is ReviewTaskStatus.FAILED
    assert task.target == "src/big.py"
    assert "block2 boom" in (task.error or "")
    assert len(result.candidates) == 1  # 第一块结果保留
    assert any("部分块失败" in w for w in result.source_run.warnings)
# ================= V1-d 返工：P0-1 max_cost / max_runtime 门控 =================


@pytest.mark.asyncio
async def test_reserve_rejects_when_cost_exceeded():
    """max_cost_usd 门控：最坏费用超限 → reserve 拒绝。"""
    budget = GlobalBudget(max_cost_usd=1.0)
    r1 = await budget.reserve(input_tokens=100, max_output_tokens=50, est_cost_usd=0.6)
    assert r1 is not None
    r2 = await budget.reserve(input_tokens=100, max_output_tokens=50, est_cost_usd=0.6)
    assert r2 is None  # 0.6 + 0.6 > 1.0


@pytest.mark.asyncio
async def test_reserve_rejects_after_runtime_expired():
    """max_runtime_seconds 门控：墙钟到期 → 拒绝新请求。"""
    from datetime import timedelta

    budget = GlobalBudget(max_runtime_seconds=10)
    budget.started_at = budget.started_at - timedelta(seconds=60)  # 已过期
    r = await budget.reserve(input_tokens=100, max_output_tokens=50, est_cost_usd=0.0)
    assert r is None


@pytest.mark.asyncio
async def test_settle_releases_unused_reservation():
    """结算释放未用预留：预扣大于实际 → remaining 回升到实际。"""
    budget = GlobalBudget()
    r = await budget.reserve(input_tokens=1000, max_output_tokens=2000, est_cost_usd=0.0)
    assert budget.remaining_tokens == budget.max_total_tokens - 3000
    await budget.settle(r, actual_input=100, actual_output=50, actual_cost=0.0)
    assert budget.tokens_used == 150
    assert budget.remaining_tokens == budget.max_total_tokens - 150
