"""静态分析转换/去重对照（20 §11.3 / T6）。

主验收是转换与去重正确性；Finding F1 不作为本里程碑质量证据。
V2-A/V2-B 对照必须保持 static.enabled=false。
本对照不启用 Judge（FindingPipeline.judge_enabled 默认 False）。
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
from reposage.domain.enums import FindingCategory, FindingStatus, Severity
from reposage.domain.finding import FindingCandidate
from reposage.review.pipeline import FindingPipeline
from reposage.review.static.convert import convert_diagnostics
from reposage.review.static.protocol import AnalyzerDiagnostic

_DEFAULT_DATASET = "reposage/evals/datasets/v2c_static.yaml"
_DEFAULT_JSON = "docs/evidence/v2-c-compare.json"
_DEFAULT_MD = "docs/evidence/v2-c-compare.md"


class _DiagSpec(BaseModel):
    analyzer_id: str = "ruff"
    rule_id: str
    path: str
    start_line: int
    message: str = ""


class _LlmSpec(BaseModel):
    title: str = "llm"
    category: str = "security"
    start_line: int = 1
    trigger: str = "x"
    confidence: float = 1.0
    path: str | None = None


class StaticEvalCase(BaseModel):
    id: str
    kind: str
    diff: str
    diagnostic: _DiagSpec | None = None
    diagnostics: list[_DiagSpec] = Field(default_factory=list)
    llm: _LlmSpec | None = None
    expect_candidate: bool | None = None
    expect_rule_id: str | None = None
    expect_category: str | None = None
    expect_findings: int | None = None
    expect_source_kinds: list[str] = Field(default_factory=list)


class StaticEvalDataset(BaseModel):
    name: str
    cases: list[StaticEvalCase] = Field(default_factory=list)

    @classmethod
    def load_yaml(cls, path: Path) -> StaticEvalDataset:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)


def _file_map(diff: str) -> dict[str, Any]:
    files = parse_unified_diff(diff)
    return {f.path: f for f in files}


def _diag(spec: _DiagSpec) -> AnalyzerDiagnostic:
    return AnalyzerDiagnostic(
        analyzer_id=spec.analyzer_id,
        rule_id=spec.rule_id,
        path=spec.path,
        start_line=spec.start_line,
        message=spec.message,
    )


def _llm_cand(spec: _LlmSpec, default_path: str) -> FindingCandidate:
    return FindingCandidate(
        title=spec.title,
        severity=Severity.HIGH,
        confidence=spec.confidence,
        category=FindingCategory(spec.category),
        claimed_path=spec.path or default_path,
        claimed_start_line=spec.start_line,
        trigger_condition=spec.trigger,
        explanation="llm",
    )


async def _run_case(case: StaticEvalCase) -> dict[str, Any]:
    file_map = _file_map(case.diff)
    default_path = next(iter(file_map)) if file_map else "src/app.py"
    pipeline = FindingPipeline(repo="eval", head_sha="head", min_confidence=0.0)
    if case.kind == "convert":
        assert case.diagnostic is not None
        cands, _skipped = convert_diagnostics([_diag(case.diagnostic)], file_map)
        ok = (len(cands) == 1) if case.expect_candidate else (len(cands) == 0)
        if cands and case.expect_rule_id:
            ok = ok and cands[0].rule_id == case.expect_rule_id
        if cands and case.expect_category:
            ok = ok and cands[0].category.value == case.expect_category
        return {"id": case.id, "kind": case.kind, "ok": ok, "n_candidates": len(cands)}
    specs = list(case.diagnostics)
    if case.diagnostic is not None:
        specs = [case.diagnostic, *specs]
    static_cands, _ = convert_diagnostics([_diag(s) for s in specs], file_map)
    llm_cands: list[FindingCandidate] = []
    if case.llm is not None:
        llm_cands = [_llm_cand(case.llm, default_path)]
    findings = (await pipeline.process(
        run_id=f"eval-{case.id}",
        candidates=[*llm_cands, *static_cands],
        file_map=file_map,
        adjudicator=None,
    )).findings
    accepted = [f for f in findings if f.status is FindingStatus.ACCEPTED]
    ok = case.expect_findings is None or len(accepted) == case.expect_findings
    if case.expect_source_kinds:
        kinds = {s.kind.value for f in accepted for s in f.sources}
        ok = ok and set(case.expect_source_kinds) <= kinds
    return {
        "id": case.id,
        "kind": case.kind,
        "ok": ok,
        "n_accepted": len(accepted),
        "source_kinds": sorted({s.kind.value for f in accepted for s in f.sources}),
    }


def run_compare(dataset_path: str | Path = _DEFAULT_DATASET) -> dict[str, Any]:
    return asyncio.run(_run_compare_async(dataset_path))


async def _run_compare_async(dataset_path: str | Path) -> dict[str, Any]:
    ds = StaticEvalDataset.load_yaml(Path(dataset_path))
    rows = [await _run_case(case) for case in ds.cases]
    convert_rows = [r for r in rows if r["kind"] == "convert"]
    dedup_rows = [r for r in rows if r["kind"] in {"dedup", "fuse", "no_fuse"}]
    return {
        "mode": "scripted_fake",
        "dataset": str(dataset_path),
        "samples": len(ds.cases),
        "real_api": "not_run",
        "conversion": {
            "cases": convert_rows,
            "passed": sum(1 for r in convert_rows if r["ok"]),
            "total": len(convert_rows),
            "note": "诊断 → Candidate 的路径/新增行过滤；不跑真实模型。",
        },
        "dedup": {
            "cases": dedup_rows,
            "passed": sum(1 for r in dedup_rows if r["ok"]),
            "total": len(dedup_rows),
            "note": "同码去重、异码并存、LLM+静态融合；fingerprint 用静态 rule_id。",
        },
        "cost": {
            "model_calls": {"off": 0, "on": 0},
            "note": "本对照不经 LLM；模型 calls 相对 V2-B 不变由单测/Service 开关保证。静态分析不占 GlobalBudget。",
        },
        "all_passed": all(r["ok"] for r in rows),
    }


def render_markdown(report: dict[str, Any]) -> str:
    conv, dedup, cost = report["conversion"], report["dedup"], report["cost"]
    lines = [
        "# V2-C 转换/去重对照（脚本化 Fake，非真实 API）",
        "",
        "> 由 `python -m reposage.evals.v2c_compare` 生成；原始 JSON：`docs/evidence/v2-c-compare.json`。",
        f"> dataset=`{report['dataset']}` samples={report['samples']} real_api={report['real_api']}",
        "> 脱敏：不含 API Key / 源码原文 / 密钥字面量。",
        "",
        "## 转换",
        "",
        f"passed={conv['passed']}/{conv['total']}",
        "",
        "| id | ok | n_candidates |",
        "|----|----|--------------|",
    ]
    for row in conv["cases"]:
        lines.append(f"| {row['id']} | {row['ok']} | {row.get('n_candidates', '')} |")
    lines.extend(["", conv["note"], "", "## 去重 / 融合", "", f"passed={dedup['passed']}/{dedup['total']}", ""])
    lines.extend(
        [
            "| id | kind | ok | n_accepted | sources |",
            "|----|------|----|------------|---------|",
        ]
    )
    for row in dedup["cases"]:
        kinds = ",".join(row.get("source_kinds") or [])
        lines.append(
            f"| {row['id']} | {row['kind']} | {row['ok']} | {row.get('n_accepted', '')} | {kinds} |"
        )
    lines.extend(
        [
            "",
            dedup["note"],
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
    parser = argparse.ArgumentParser(description="V2-C 转换/去重对照")
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
