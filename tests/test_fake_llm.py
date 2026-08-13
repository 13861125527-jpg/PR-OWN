"""FakeLLMProvider 测试。"""

import pytest
from reposage.domain.enums import FindingCategory, Severity
from reposage.domain.finding import FindingCandidate
from reposage.providers.llm.fake import FakeLLMProvider


@pytest.mark.asyncio
async def test_structured_returns_scripted_findings():
    candidate = FindingCandidate(
        title="t",
        severity=Severity.HIGH,
        confidence=0.9,
        category=FindingCategory.SECURITY,
        claimed_path="src/a.py",
        claimed_start_line=5,
    )
    fake = FakeLLMProvider(default_findings=[candidate], input_tokens=10, output_tokens=5, cost_usd=0.01)

    findings, usage = await fake.structured([{"role": "user", "content": "review"}])
    assert len(findings) == 1
    assert findings[0].claimed_path == "src/a.py"
    assert usage.input_tokens == 10
    assert usage.output_tokens == 5
    assert usage.cost_usd == 0.01
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_tool_loop_returns_finish():
    fake = FakeLLMProvider()
    resp = await fake.tool_loop(object(), [], budget={"max_rounds": 8})
    assert resp.action == "finish_review"


@pytest.mark.asyncio
async def test_late_cancelled_usage_still_recorded():
    """P0-R2-3：迟到响应不入审查，但 usage 仍记账。"""
    from reposage.domain.models import ModelUsage, ModelUsageOutcome

    fake = FakeLLMProvider()
    usage = fake._usage(outcome=ModelUsageOutcome.LATE_CANCELLED.value)  # noqa: SLF001
    assert usage.outcome is ModelUsageOutcome.LATE_CANCELLED
    assert isinstance(usage, ModelUsage)
