"""评测 Runner（evals/runner.py）。

V1-d：真实链路接入——FakeGitProvider（样本 base/head 快照）→ diff 解析/过滤 →
ContextAssembler（per-file units）→ ReviewStrategy（默认 SinglePassReviewer，
LLM 由外部注入：FakeLLMProvider 脚本化或真实 Provider）→ FindingPipeline →
指标（compute_metrics）。策略可注入，便于按样本脚本化候选。
"""

from __future__ import annotations

from pathlib import Path

from reposage.domain.diff import filter_files, parse_unified_diff
from reposage.domain.enums import ChangeRequestSource, ReviewRunStatus, ReviewStrategyName
from reposage.domain.finding import Finding
from reposage.domain.models import ChangeRequest, CommitRef, GlobalBudget, ReviewUnit
from reposage.domain.run import ReviewRun
from reposage.domain.strategy import ReviewStrategy
from reposage.evals.dataset import EvalDataset, EvalSample
from reposage.evals.metrics import Metrics, compute_metrics
from reposage.providers.git.fake import FakeGitProvider
from reposage.review.context import ContextAssembler
from reposage.review.pipeline import FindingPipeline
from reposage.review.single_pass import SinglePassReviewer


class EvalRunner:
    """评测 Runner：样本 → 真实审查链路 → 指标。"""

    def __init__(
        self,
        dataset: EvalDataset,
        *,
        strategy: ReviewStrategy | None = None,
        repo: str = "eval-repo",
        min_confidence: float = 0.0,  # 评测默认不设门槛，观察原始命中
    ) -> None:
        self.dataset = dataset
        self.strategy = strategy  # None 时按样本惰性创建 SinglePassReviewer(FakeLLMProvider())
        self.repo = repo
        self.min_confidence = min_confidence
        self.results: dict[str, Metrics] = {}

    async def _review_sample(self, sample: EvalSample) -> list[Finding]:
        """FakeGitProvider 样本快照 → 真实审查链路（V1-d）。"""
        from reposage.providers.llm.fake import FakeLLMProvider

        fake = FakeGitProvider()
        fake.add_snapshot("base", sample.base_files)
        fake.add_snapshot("head", sample.head_files)
        req = ChangeRequest(
            source=ChangeRequestSource.LOCAL_RANGE,
            base=CommitRef(sha="base", label="base"),
            head=CommitRef(sha="head", label="head", locked=True),
            title=sample.pr_title or None,
            description=sample.pr_description or None,
        )
        diff_text = await fake.get_diff("base", "head")
        files = parse_unified_diff(diff_text)
        filtered = filter_files(files, languages=["python"], max_files=40)
        file_map = {f.path: f for f in filtered.kept}

        run = ReviewRun(
            run_id=f"eval-{sample.id}",
            strategy=ReviewStrategyName.SINGLE_PASS,
            status=ReviewRunStatus.RUNNING,
        )
        assembler = ContextAssembler()
        units: list[ReviewUnit] = []
        for f in filtered.kept:
            units.extend(assembler.build_file_units(run_id=run.run_id, change_request=req, file=f))

        strategy = self.strategy or SinglePassReviewer(FakeLLMProvider())
        result = await strategy.execute(units, run, GlobalBudget())

        pipeline = FindingPipeline(
            repo=self.repo, head_sha="head", min_confidence=self.min_confidence
        )
        return pipeline.process(
            run_id=run.run_id,
            candidates=result.candidates,
            file_map=file_map,
        )

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
        lines = [f"Evals: {len(self.results)} samples"]
        for sid, m in self.results.items():
            lines.append(f"  {sid}: {m.to_report()}")
        return "\n".join(lines)


def run_eval(dataset_path: Path) -> str:
    """命令行入口：加载数据集 → 真实链路评测 → 报告。"""
    import asyncio

    ds = EvalDataset.load_yaml(dataset_path)
    runner = EvalRunner(ds)
    asyncio.run(runner.run())
    return runner.report()
