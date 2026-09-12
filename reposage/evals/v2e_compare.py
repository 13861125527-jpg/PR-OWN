"""反馈记忆对照（22 §7.2 / T7）。

主验收是 mark 抑制、撤销恢复、代码移动命中、开关隔离；Finding F1 不是本里程碑质量证据。
V2-A/B/C/D 对照必须保持 review.feedback.enabled=false 与 incremental.enabled=false。
V3-A：agent.enabled 默认 False，本对照不启用 Agent。
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
from reposage.domain.enums import FeedbackKind, FindingCategory, FindingStatus, Severity
from reposage.domain.finding import FindingCandidate
from reposage.domain.run import FeedbackMemory
from reposage.review.feedback import FeedbackConfigError, validate_feedback_memory
from reposage.review.pipeline import FindingPipeline

_DEFAULT_DATASET = "reposage/evals/datasets/v2e_feedback.yaml"
_DEFAULT_JSON = "docs/evidence/v2-e-compare.json"
_DEFAULT_MD = "docs/evidence/v2-e-compare.md"


class _CandSpec(BaseModel):
    title: str = "x"
    start_line: int = 1
    trigger: str = "x"
    category: str = "security"
    path: str | None = None


class FeedbackEvalCase(BaseModel):
    id: str
    kind: str
    diff: str
    path: str
    category: str
    candidates: list[_CandSpec] = Field(default_factory=list)
    revoked: bool = False
    feedback_enabled: bool = True
    start_line: int | None = None
    expect_accepted: int | None = None
    expect_suppressed: int | None = None


class FeedbackEvalDataset(BaseModel):
    name: str
    cases: list[FeedbackEvalCase] = Field(default_factory=list)

    @classmethod
    def load_yaml(cls, path: Path) -> FeedbackEvalDataset:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)


def _file_map(diff: str) -> dict[str, Any]:
    files = parse_unified_diff(diff)
    return {f.path: f for f in files}


def _cand(spec: _CandSpec, default_path: str) -> FindingCandidate:
    return FindingCandidate(
        title=spec.title,
        severity=Severity.HIGH,
        confidence=0.9,
        category=FindingCategory(spec.category),
        claimed_path=spec.path or default_path,
        claimed_start_line=spec.start_line,
        trigger_condition=spec.trigger,
        explanation=spec.title,
        impact="x",
        suggestion="x",
    )


async def _run_case(case: FeedbackEvalCase) -> dict[str, Any]:
    file_map = _file_map(case.diff)
    default_path = next(iter(file_map)) if file_map else case.path
    memory = FeedbackMemory(
        id=1,
        repo="eval",
        kind=FeedbackKind.FALSE_POSITIVE,
        path=case.path,
        category=case.category,
    )
    if case.revoked:
        memory.revoke()
    feedback = [memory] if case.feedback_enabled else []
    pipeline = FindingPipeline(repo="eval", head_sha="head", min_confidence=0.75)
    pipe = await pipeline.process(
        run_id=f"eval-{case.id}",
        candidates=[_cand(spec, default_path) for spec in case.candidates],
        file_map=file_map,
        feedback=feedback,
    )
    accepted = [f for f in pipe.findings if f.status is FindingStatus.ACCEPTED]
    suppressed = [
        f
        for f in pipe.findings
        if f.status is FindingStatus.SUPPRESSED
        and any(v.actor == "user" for v in f.versions)
    ]
    ok = True
    if case.expect_accepted is not None:
        ok = ok and len(accepted) == case.expect_accepted
    if case.expect_suppressed is not None:
        ok = ok and len(suppressed) == case.expect_suppressed
    return {
        "id": case.id,
        "kind": case.kind,
        "ok": ok,
        "n_accepted": len(accepted),
        "n_suppressed": len(suppressed),
        "feedback_suppressed": pipe.metrics.feedback_suppressed,
    }


def run_compare(dataset_path: str | Path = _DEFAULT_DATASET) -> dict[str, Any]:
    return asyncio.run(_run_compare_async(dataset_path))


async def _run_compare_async(dataset_path: str | Path) -> dict[str, Any]:
    ds = FeedbackEvalDataset.load_yaml(Path(dataset_path))
    rows = [await _run_case(case) for case in ds.cases]
    global_ok = False
    try:
        validate_feedback_memory(
            FeedbackMemory(repo="eval", kind=FeedbackKind.FALSE_POSITIVE)
        )
    except FeedbackConfigError:
        global_ok = True
    return {
        "mode": "scripted_fake",
        "dataset": str(dataset_path),
        "samples": len(ds.cases),
        "real_api": "not_run",
        "feedback": {
            "cases": rows,
            "passed": sum(1 for r in rows if r["ok"]),
            "total": len(rows),
            "global_reject": global_ok,
            "note": "mark 抑制 / 撤销恢复 / 代码移动 / 开关隔离；口径是程序状态，不是模型质量。",
        },
        "all_passed": global_ok and all(r["ok"] for r in rows),
    }


def render_markdown(report: dict[str, Any]) -> str:
    fb = report["feedback"]
    lines = [
        "# V2-E 反馈记忆对照（脚本化 Fake，非真实 API）",
        "",
        "> 由 `python -m reposage.evals.v2e_compare` 生成；原始 JSON：`docs/evidence/v2-e-compare.json`。",
        f"> dataset=`{report['dataset']}` samples={report['samples']} real_api={report['real_api']}",
        "> 脱敏：不含 API Key / 源码原文 / 密钥字面量。",
        "",
        "## 反馈抑制 / 撤销 / 移动 / 隔离",
        "",
        f"passed={fb['passed']}/{fb['total']} global_reject={fb['global_reject']}",
        "",
        "| id | kind | ok | accepted | suppressed |",
        "|----|------|----|----------|------------|",
    ]
    for row in fb["cases"]:
        lines.append(
            f"| {row['id']} | {row['kind']} | {row['ok']} | {row['n_accepted']} | {row['n_suppressed']} |"
        )
    lines.extend(
        [
            "",
            fb["note"],
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
    parser = argparse.ArgumentParser(description="V2-E 反馈记忆对照")
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
