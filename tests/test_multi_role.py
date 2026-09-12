"""MultiRoleReviewer 测试（V2-A：两阶段准入、role_id 盖戳、失败矩阵）。"""

from __future__ import annotations

import asyncio

import pytest
from reposage.config.settings import Settings
from reposage.domain.diff import parse_unified_diff
from reposage.domain.enums import (
    ChangeRequestSource,
    CoverageReason,
    FindingCategory,
    FindingSourceKind,
    ReviewRunStatus,
    ReviewStrategyName,
    ReviewTaskKind,
    ReviewTaskStatus,
    Severity,
)
from reposage.domain.finding import FindingCandidate
from reposage.domain.models import ChangeRequest, CommitRef, GlobalBudget
from reposage.domain.run import ReviewRun
from reposage.providers.git.fake import FakeGitProvider
from reposage.providers.llm.fake import FakeLLMProvider, _role_id_from_messages
from reposage.review.context import ContextAssembler
from reposage.review.pipeline import FindingPipeline
from reposage.review.reviewers.roles.multi_role import MultiRoleReviewer
from reposage.review.reviewers.roles.registry import RoleRegistry
from reposage.review.service import ReviewService
from reposage.storage.sqlite import SqliteStorage

EVAL_DIFF = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,1 +1,2 @@
 def f(x):
+    return eval(x)
"""

HARMLESS = """\
diff --git a/src/ok.py b/src/ok.py
--- a/src/ok.py
+++ b/src/ok.py
@@ -1,1 +1,2 @@
 def f(x):
+    return x + 1
"""


def _req() -> ChangeRequest:
    return ChangeRequest(
        source=ChangeRequestSource.LOCAL_RANGE,
        base=CommitRef(sha="base", label="base"),
        head=CommitRef(sha="head", label="head", locked=True),
        title="t",
    )


def _cand(*, role_id: str | None = None) -> FindingCandidate:
    return FindingCandidate(
        title="eval",
        severity=Severity.HIGH,
        confidence=0.9,
        category=FindingCategory.SECURITY,
        claimed_path="src/app.py",
        claimed_start_line=2,
        trigger_condition="eval(x)",
        role_id=role_id,
    )


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "review": {
                "strategy": "multi_role",
                "roles": ["general", "security", "correctness", "performance"],
            }
        }
    )


def _reviewer(llm: FakeLLMProvider) -> MultiRoleReviewer:
    return MultiRoleReviewer(
        llm,
        RoleRegistry(_settings()),
        languages=["python"],
        file_tasks=3,
        role_tasks=3,
        model_requests=2,
    )


def _units(diff: str = EVAL_DIFF):
    file = parse_unified_diff(diff)[0]
    return ContextAssembler().build_file_units(run_id="r1", change_request=_req(), file=file)


def _run() -> ReviewRun:
    return ReviewRun(run_id="r1", strategy=ReviewStrategyName.MULTI_ROLE)


@pytest.mark.asyncio
async def test_gate_miss_has_decision_but_no_task():
    llm = FakeLLMProvider(default_findings=[])
    result = await _reviewer(llm).execute(_units(HARMLESS), _run(), GlobalBudget())
    roles_called = {t.target.split("::")[-1] for t in result.source_run.tasks}
    assert roles_called == {"general"}
    reasons = {(d.role_id, d.enabled) for d in result.source_run.gate_decisions}
    assert ("security", False) in reasons
    assert ("general", True) in reasons
    assert all(t.kind is ReviewTaskKind.ROLE_REVIEW for t in result.source_run.tasks)


@pytest.mark.asyncio
async def test_two_phase_required_finishes_before_optional_starts():
    order: list[tuple[str, str]] = []

    class Tracking(FakeLLMProvider):
        async def structured(self, messages, **kwargs):
            role = _role_id_from_messages(messages)
            order.append(("start", role))
            await asyncio.sleep(0.02)
            try:
                return await super().structured(messages, **kwargs)
            finally:
                order.append(("end", role))

    llm = Tracking(default_findings=[])
    await _reviewer(llm).execute(_units(EVAL_DIFF), _run(), GlobalBudget())
    last_general_end = max(i for i, (p, r) in enumerate(order) if p == "end" and r == "general")
    first_optional_start = min(
        i for i, (p, r) in enumerate(order) if p == "start" and r != "general"
    )
    assert last_general_end < first_optional_start


@pytest.mark.asyncio
async def test_remaining_budget_allows_optional_after_phase1():
    llm = FakeLLMProvider(input_tokens=10, output_tokens=5, default_findings=[])
    result = await _reviewer(llm).execute(
        _units(EVAL_DIFF), _run(), GlobalBudget(max_total_tokens=80000)
    )
    roles = {t.target.split("::")[-1] for t in result.source_run.tasks}
    assert "general" in roles
    assert "security" in roles
    assert result.source_run.health.required_failed is False


@pytest.mark.asyncio
async def test_exhausted_budget_rejects_optional_without_overrun():
    llm = FakeLLMProvider(input_tokens=30000, output_tokens=10000, default_findings=[])
    budget = GlobalBudget(max_total_tokens=40000, max_cost_usd=99.0, max_runtime_seconds=60)
    result = await _reviewer(llm).execute(_units(EVAL_DIFF), _run(), budget)
    tasks = {t.target.split("::")[-1]: t for t in result.source_run.tasks}
    assert tasks["general"].status is ReviewTaskStatus.COMPLETED
    assert "security" in tasks
    assert tasks["security"].status is ReviewTaskStatus.FAILED
    assert result.source_run.health.required_failed is False
    assert result.source_run.health.optional_failure_count >= 1
    cov = {c.target.split("::")[-1]: c.reason for c in result.source_run.coverage_items}
    assert cov["security"] is CoverageReason.TRUNCATED
    assert budget.tokens_used <= budget.max_total_tokens or budget.overrun


@pytest.mark.asyncio
async def test_program_stamps_role_id_over_model_forgery():
    forged = _cand(role_id="forged")
    llm = FakeLLMProvider(role_findings={"security": [forged], "general": []})
    result = await _reviewer(llm).execute(_units(EVAL_DIFF), _run(), GlobalBudget())
    assert result.candidates
    assert all(c.role_id != "forged" for c in result.candidates)
    assert all(c.role_id in {"general", "security", "correctness", "performance"} for c in result.candidates)
    file_map = {u.file_path: parse_unified_diff(EVAL_DIFF)[0] for u in _units(EVAL_DIFF)}
    findings = (await FindingPipeline(repo="r", head_sha="head").process(
        run_id="r1", candidates=result.candidates, file_map=file_map
    )).findings
    sources = [s for f in findings for s in f.sources]
    assert any(s.kind is FindingSourceKind.LLM_ROLE and s.role_id == "security" for s in sources)
    assert all(s.role_id != "forged" for s in sources)


@pytest.mark.asyncio
async def test_model_semaphore_caps_in_flight():
    class Slow(FakeLLMProvider):
        async def structured(self, messages, **kwargs):
            await asyncio.sleep(0.05)
            return await super().structured(messages, **kwargs)

    llm = Slow(default_findings=[])
    reviewer = MultiRoleReviewer(
        llm,
        RoleRegistry(_settings()),
        languages=["python"],
        file_tasks=3,
        role_tasks=3,
        model_requests=1,
    )
    await reviewer.execute(_units(EVAL_DIFF), _run(), GlobalBudget())
    assert llm.max_in_flight <= 1


@pytest.mark.asyncio
async def test_optional_failure_keeps_run_completed():
    class BoomSecurity(FakeLLMProvider):
        async def structured(self, messages, **kwargs):
            role = _role_id_from_messages(messages)
            if role == "security":
                raise RuntimeError("security boom")
            return await super().structured(messages, **kwargs)

    fake_git = FakeGitProvider()
    fake_git.add_snapshot("base", {"src/app.py": "def f(x):\n    return x\n"})
    fake_git.add_snapshot("head", {"src/app.py": "def f(x):\n    return eval(x)\n"})
    settings = _settings()
    store = SqliteStorage(":memory:")
    service = ReviewService(
        fake_git,
        BoomSecurity(default_findings=[]),
        store,
        settings=settings,
    )
    run, _ = await service.review("base..head")
    assert run.status is ReviewRunStatus.COMPLETED
    assert run.warnings
    rows = store._query("SELECT role_id, enabled, reason FROM gate_decisions")
    assert any(r["role_id"] == "security" and r["enabled"] == 1 for r in rows)
    tasks = store._query("SELECT target, status FROM tasks")
    general = [t for t in tasks if str(t["target"]).endswith("::general")]
    assert general and all(t["status"] == "completed" for t in general)


@pytest.mark.asyncio
async def test_cancelled_run_is_persisted():
    class CancelStrategy:
        name = "single_pass"

        def supports(self, run):
            return True

        async def execute(self, units, run, budget):
            raise asyncio.CancelledError()

    fake_git = FakeGitProvider()
    fake_git.add_snapshot("base", {"src/a.py": "x=1\n"})
    fake_git.add_snapshot("head", {"src/a.py": "x=2\n"})
    store = SqliteStorage(":memory:")
    service = ReviewService(fake_git, FakeLLMProvider(), store)
    service.strategy = CancelStrategy()  # type: ignore[assignment]
    with pytest.raises(asyncio.CancelledError):
        await service.review("base..head")
    row = store._query("SELECT status FROM runs")[0]
    assert row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_assembler_stamps_gate_features():
    units = _units(EVAL_DIFF)
    assert units[0].gate_features is not None
    assert units[0].gate_features.path == "src/app.py"
    assert "exec_dyn" in units[0].gate_features.keyword_hits


@pytest.mark.asyncio
async def test_phase1_structural_error_does_not_start_optional():
    llm = FakeLLMProvider(default_findings=[])
    reviewer = _reviewer(llm)
    orig = reviewer._run_file_roles

    async def boom(run, path, file_units, specs, *args, **kwargs):
        if any(s.required for s in specs):
            raise RuntimeError("phase1 boom")
        return await orig(run, path, file_units, specs, *args, **kwargs)

    reviewer._run_file_roles = boom  # type: ignore[method-assign]
    result = await reviewer.execute(_units(EVAL_DIFF), _run(), GlobalBudget())
    roles = [_role_id_from_messages(c["messages"]) for c in llm.calls]
    assert "security" not in roles
    assert llm.calls == []
    assert result.source_run.health.required_failed is True
    assert all(t.status is ReviewTaskStatus.FAILED for t in result.source_run.tasks if "::general" in t.target)


@pytest.mark.asyncio
async def test_service_emits_gate_decision_logs():
    import io
    import json

    from reposage.observability.logging import StructuredLogger

    fake_git = FakeGitProvider()
    fake_git.add_snapshot("base", {"src/app.py": "def f(x):\n    return x\n"})
    fake_git.add_snapshot("head", {"src/app.py": "def f(x):\n    return eval(x)\n"})
    sink = io.StringIO()
    service = ReviewService(
        fake_git,
        FakeLLMProvider(default_findings=[]),
        SqliteStorage(":memory:"),
        settings=_settings(),
        logger=StructuredLogger(sink=sink),
    )
    await service.review("base..head")
    events = [json.loads(line) for line in sink.getvalue().splitlines() if line.strip()]
    gates = [e for e in events if e["event"] == "gate_decision"]
    assert gates
    assert any("role=security" in (e["detail"] or "") for e in gates)
    assert all("return eval" not in (e["detail"] or "") for e in gates)


@pytest.mark.asyncio
async def test_scripted_v1_vs_v2a_call_counts():
    from reposage.review.single_pass import SinglePassReviewer

    units = _units(EVAL_DIFF)
    v1_llm = FakeLLMProvider(default_findings=[])
    v2_llm = FakeLLMProvider(default_findings=[])
    v1 = await SinglePassReviewer(v1_llm).execute(units, _run(), GlobalBudget())
    v2 = await _reviewer(v2_llm).execute(units, _run(), GlobalBudget())
    assert len(v1_llm.calls) == 1
    assert len(v2_llm.calls) >= 2
    assert "security" in {t.target.split("::")[-1] for t in v2.source_run.tasks}
    assert v1.source_run.health.required_failed is False
    assert v2.source_run.health.required_failed is False
