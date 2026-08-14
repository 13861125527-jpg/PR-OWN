"""审查策略接口（domain/strategy.py）。

ReviewStrategy 是版本演进的核心（03 §5）：任何 Strategy 只返回 CandidateFinding + SourceRunResult；
正式 Finding 生命周期由统一 FindingPipeline（ReviewService 编排）执行，Strategy 不得推进 Finding 状态。

V1-d 契约：execute 接收装配好的 per-file ReviewUnit 列表（04 §1 map-reduce 的
原子粒度；ReviewUnit 定义在 domain/models.py）；SinglePass 按 file 分组并发，
MultiRole（V2）/Agentic（V3）在此基础上扩展。
"""

from __future__ import annotations

from typing import Protocol

from .finding import FindingCandidate
from .models import GlobalBudget, ReviewUnit
from .run import ReviewRun, SourceRunResult


class StrategyResult:
    """Strategy 的唯一产出。"""

    def __init__(self, candidates: list[FindingCandidate], source_run: SourceRunResult) -> None:
        self.candidates = candidates
        self.source_run = source_run


class ReviewStrategy(Protocol):
    name: str

    def supports(self, run: ReviewRun) -> bool: ...

    async def execute(
        self,
        units: list[ReviewUnit],
        run: ReviewRun,
        budget: GlobalBudget,
    ) -> StrategyResult: ...
