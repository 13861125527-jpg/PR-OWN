"""评测 Runner 骨架（evals/runner.py）。

Phase 0：加载数据集 → 运行审查（默认 Fake Provider）→ 汇总指标 → 报告。
V1 后接入真实 SinglePassReviewer；当前先打通数据与指标闭环。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from reposage.domain.models import (
    ContextChunk,
    ContextLayer,
    ContextSource,
    ContextSourceKind,
    ReviewContext,
)
from reposage.evals.dataset import EvalDataset, EvalSample
from reposage.evals.metrics import Metrics, compute_metrics
from reposage.providers.git.fake import FakeGitProvider


class EvalRunner:
    """最小评测 Runner。"""

    def __init__(self, dataset: EvalDataset) -> None:
        self.dataset = dataset
        self.results: dict[str, Metrics] = {}

    async def _review_sample(self, sample: EvalSample) -> list[Any]:
        """Phase 0：用 FakeGitProvider 取 diff 并装配最小上下文，返回空 findings 占位。

        V1 起替换为 ReviewService 真实链路（SinglePassReviewer + FindingPipeline）。
        """
        fake = FakeGitProvider()
        fake.add_snapshot("base", sample.base_files)
        fake.add_snapshot("head", sample.head_files)
        diff_text = await fake.get_diff("base", "head")
        ctx = ReviewContext(
            run_id=f"eval-{sample.id}",
            chunks=[
                ContextChunk(
                    layer=ContextLayer.L2,
                    source=ContextSource(kind=ContextSourceKind.DIFF, ref=sample.id),
                    content=diff_text,
                    tokens=len(diff_text) // 4,
                )
            ],
        )
        _ = ctx  # Phase 0 占位：后续交给 reviewer
        return []

    async def run(self) -> dict[str, Metrics]:
        for sample in self.dataset.samples:
            findings = await self._review_sample(sample)
            self.results[sample.id] = compute_metrics(
                sample.expected,
                findings,
                sample_kind=sample.kind,
            )
        return self.results

    def report(self) -> str:
        lines = [f"Dataset: {self.dataset.name}"]
        for sid, m in self.results.items():
            lines.append(f"  {sid}: {m.to_report()}")
        return "\n".join(lines)


def run_eval(dataset_path: Path) -> str:
    ds = EvalDataset.load_yaml(dataset_path)
    runner = EvalRunner(ds)
    asyncio.run(runner.run())
    return runner.report()
