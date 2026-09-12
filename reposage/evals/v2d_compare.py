"""聚类/去重/Judge 对照（21 §11.3 / T6）。

主验收是 duplicate_survival_rate 与 Fake 裁决正确性；Finding F1 不作为本里程碑质量证据。
V2-A/V2-B/V2-C 对照必须保持 judge.enabled=false。
V3-A：agent.enabled 默认 False，本对照不启用 Agent。
V2-E：本对照不传入 feedback（默认空列表），不启用增量。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from reposage.domain.diff import parse_unified_diff
from reposage.domain.enums import FindingCategory, FindingStatus, JudgeAction, Severity
from reposage.domain.finding import FindingCandidate
from reposage.domain.models import GlobalBudget
from reposage.review.judge import FakeAdjudicator, JudgeDecision
from reposage.review.pipeline import FindingPipeline

_DEFAULT_DATASET = "reposage/evals/datasets/v2d_dedup.yaml"
_DEFAULT_JSON = "docs/evidence/v2-d-compare.json"
_DEFAULT_MD = "docs/evidence/v2-d-compare.md"


class _CandSpec(BaseModel):
    title: str = "x"
    start_line: int = 1
    trigger: str = "x"
    category: str = "security"
    confidence: float = 0.9
    path: str | None = None


class DedupEvalCase(BaseModel):
    id: str
    kind: str
    diff: str
    candidates: list[_CandSpec] = Field(default_factory=list)
    judge_enabled: bool = False
    judge_action: str | None = None
    judge_fail: bool = False
    expect_merged: int | None = None
    expect_survival_max: float | None = None
    expect_accepted: int | None = None
    expect_downrank: int | None = None


class DedupEvalDataset(BaseModel):
    name: str
    cases: list[DedupEvalCase] = Field(default_factory=list)

    @classmethod
    def load_yaml(cls, path: Path) -> DedupEvalDataset:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)


def _file_map(diff: str) -> dict[str, Any]:
    files = parse_unified_diff(diff)
    return {f.path: f for f in files}


def _cand(spec: _CandSpec, default_path: str) -> FindingCandidate:
    return FindingCandidate(
        title=spec.title,
        severity=Severity.HIGH,
        confidence=spec.confidence,
        category=FindingCategory(spec.category),
        claimed_path=spec.path or default_path,
        claimed_start_line=spec.start_line,
        trigger_condition=spec.trigger,
        explanation=spec.title,
        impact="x",
        suggestion="x",
    )


async def _run_case(case: DedupEvalCase) -> dict[str, Any]:
    file_map = _file_map(case.diff)
    default_path = next(iter(file_map)) if file_map else "src/app.py"
    pipeline = FindingPipeline(
        repo="eval",
        head_sha="head",
        min_confidence=0.75,
        judge_enabled=case.judge_enabled,
        max_findings=32,
    )
    adjudicator = None
    budget = None
    sem = None
    if case.judge_enabled:
        if case.judge_fail:
            adjudicator = FakeAdjudicator(fail=True)
        else:
            adjudicator = _scripted_adjudicator(case.judge_action)
        budget = GlobalBudget()
        sem = asyncio.Semaphore(1)
    cands = [_cand(spec, default_path) for spec in case.candidates]
    pipe = await pipeline.process(
        run_id=f"eval-{case.id}",
        candidates=cands,
        file_map=file_map,
        adjudicator=adjudicator,
        budget=budget,
        model_semaphore=sem,
    )
    accepted = [f for f in pipe.findings if f.status is FindingStatus.ACCEPTED]
    survival = pipe.metrics.duplicate_survival_rate
    ok = True
    if case.expect_merged is not None:
        ok = ok and pipe.metrics.merged == case.expect_merged
    if case.expect_survival_max is not None and survival is not None:
        ok = ok and survival <= case.expect_survival_max
    if case.expect_accepted is not None:
        ok = ok and len(accepted) == case.expect_accepted
    if case.expect_downrank is not None:
        ok = ok and pipe.metrics.judge_downrank == case.expect_downrank
    return {
        "id": case.id,
        "kind": case.kind,
        "ok": ok,
        "n_raw": pipe.metrics.raw_candidates,
        "n_merged": pipe.metrics.merged,
        "duplicate_survival_rate": survival,
        "dedup_collapse_rate": pipe.metrics.dedup_collapse_rate,
        "n_accepted": len(accepted),
        "judge_keep": pipe.metrics.judge_keep,
        "judge_downrank": pipe.metrics.judge_downrank,
        "fingerprints": sorted({f.fingerprint for f in pipe.findings if f.fingerprint}),
    }


class _DeferredAdjudicator(FakeAdjudicator):
    def __init__(self, action: JudgeAction) -> None:
        super().__init__()
        self._action = action

    async def adjudicate(self, **kwargs):  # type: ignore[no-untyped-def]
        findings = kwargs["findings"]
        self.decisions = [
            JudgeDecision(
                finding_occurrence_id=f.finding_occurrence_id,
                action=self._action,
                reason="eval",
            )
            for f in findings
        ]
        return await super().adjudicate(**kwargs)


def _scripted_adjudicator(action: str | None) -> FakeAdjudicator:
    if action == "downrank":
        return _DeferredAdjudicator(JudgeAction.DOWNRANK)
    return _DeferredAdjudicator(JudgeAction.KEEP)


def run_compare(dataset_path: str | Path = _DEFAULT_DATASET) -> dict[str, Any]:
    return asyncio.run(_run_compare_async(dataset_path))


async def _run_compare_async(dataset_path: str | Path) -> dict[str, Any]:
    ds = DedupEvalDataset.load_yaml(Path(dataset_path))
    rows = [await _run_case(case) for case in ds.cases]
    dedup_rows = [r for r in rows if r["kind"] in {"dedup", "no_merge"}]
    judge_rows = [r for r in rows if r["kind"] == "judge"]
    return {
        "mode": "scripted_fake",
        "dataset": str(dataset_path),
        "samples": len(ds.cases),
        "real_api": "not_run",
        "dedup": {
            "cases": dedup_rows,
            "passed": sum(1 for r in dedup_rows if r["ok"]),
            "total": len(dedup_rows),
            "note": "重复候选压缩与不应合并的对；口径是 duplicate_survival_rate，不是真实重复占比。",
        },
        "judge": {
            "cases": judge_rows,
            "passed": sum(1 for r in judge_rows if r["ok"]),
            "total": len(judge_rows),
            "note": "Fake 裁决 keep/downrank/fail-open；不宣称真实模型质量。",
        },
        "cost": {
            "model_calls": {"off": 0, "on": 0},
            "note": "本对照 Fake Adjudicator 不经 LLM complete；真实 Judge 成本见 real_compare（非本里程碑 DoD）。",
        },
        "all_passed": all(r["ok"] for r in rows),
    }


def render_markdown(report: dict[str, Any]) -> str:
    dedup, judge, cost = report["dedup"], report["judge"], report["cost"]
    lines = [
        "# V2-D 去重 / Judge 对照（脚本化 Fake，非真实 API）",
        "",
        "> 由 `python -m reposage.evals.v2d_compare` 生成；原始 JSON：`docs/evidence/v2-d-compare.json`。",
        f"> dataset=`{report['dataset']}` samples={report['samples']} real_api={report['real_api']}",
        "> 脱敏：不含 API Key / 源码原文 / 密钥字面量。",
        "",
        "## 确定性去重",
        "",
        f"passed={dedup['passed']}/{dedup['total']}",
        "",
        "| id | kind | ok | n_raw | n_merged | survival | collapse |",
        "|----|------|----|-------|----------|----------|----------|",
    ]
    for row in dedup["cases"]:
        lines.append(
            f"| {row['id']} | {row['kind']} | {row['ok']} | {row.get('n_raw', '')} | "
            f"{row.get('n_merged', '')} | {row.get('duplicate_survival_rate', '')} | "
            f"{row.get('dedup_collapse_rate', '')} |"
        )
    lines.extend(["", dedup["note"], "", "## Judge", "", f"passed={judge['passed']}/{judge['total']}", ""])
    lines.extend(
        [
            "| id | ok | n_accepted | keep | downrank |",
            "|----|----|------------|------|----------|",
        ]
    )
    for row in judge["cases"]:
        lines.append(
            f"| {row['id']} | {row['ok']} | {row.get('n_accepted', '')} | "
            f"{row.get('judge_keep', '')} | {row.get('judge_downrank', '')} |"
        )
    lines.extend(
        [
            "",
            judge["note"],
            "",
            "## 成本",
            "",
            cost["note"],
            "",
            f"全部通过：{report['all_passed']}",
            "",
            "真实 API 对照：未运行。",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(
    report: dict[str, Any],
    *,
    json_path: str | Path = _DEFAULT_JSON,
    markdown_path: str | Path = _DEFAULT_MD,
) -> None:
    json_file = Path(json_path)
    md_file = Path(markdown_path)
    json_file.parent.mkdir(parents=True, exist_ok=True)
    json_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_file.write_text(render_markdown(report), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="V2-D 去重/Judge 对照")
    parser.add_argument("--dataset", default=_DEFAULT_DATASET)
    parser.add_argument("--output", default=_DEFAULT_JSON)
    parser.add_argument("--markdown", default=_DEFAULT_MD)
    args = parser.parse_args()
    report = run_compare(args.dataset)
    write_report(report, json_path=args.output, markdown_path=args.markdown)
    print(f"wrote {args.output} and {args.markdown}")
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
