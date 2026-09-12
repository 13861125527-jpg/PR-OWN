"""V3-A 沙箱/schema 对照（23 §10.2 / T7）。

主验收是越界拒绝、合法读、截断、stub 不进 Pipeline、默认审查不写 tool_calls。
无真实模型。V2-A/B/C/D/E 对照钉死 agent.enabled=false。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from reposage.providers.git.fake import FakeGitProvider
from reposage.providers.llm.fake import FakeLLMProvider
from reposage.review.service import ReviewService
from reposage.storage.sqlite import SqliteStorage
from reposage.tools import builtin_registry, invoke
from reposage.tools.snapshot import MemoryToolSnapshot, ToolWorkspace

_DEFAULT_DATASET = "reposage/evals/datasets/v3a_sandbox.yaml"
_DEFAULT_JSON = "docs/evidence/v3-a-compare.json"
_DEFAULT_MD = "docs/evidence/v3-a-compare.md"

_BLOBS = {
    "src/a.py": "value = foo.bar()\nSNAP\n",
    "src/long.py": "\n".join(f"L{i}" for i in range(1, 250)) + "\n",
}


class SandboxEvalCase(BaseModel):
    id: str
    kind: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    expect_status: str
    expect_error: str | None = None
    expect_line: str | None = None
    expect_truncated: bool | None = None
    expect_hit_path: str | None = None
    expect_hits: int | None = None


class SandboxEvalDataset(BaseModel):
    name: str
    cases: list[SandboxEvalCase] = Field(default_factory=list)

    @classmethod
    def load_yaml(cls, path: Path) -> SandboxEvalDataset:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)


def _workspace() -> ToolWorkspace:
    return ToolWorkspace(
        repo_root=Path("missing-root"),
        snapshot=MemoryToolSnapshot(_BLOBS, head_sha="head", repository_id="eval"),
    )


def _eval_case(case: SandboxEvalCase) -> dict[str, Any]:
    registry = builtin_registry()
    call, result = asyncio.run(invoke(registry, _workspace(), case.tool, case.args))
    status = call.status.value
    ok = status == case.expect_status
    if case.expect_error is not None:
        ok = ok and result.error == case.expect_error
    payload: Any = None
    if result.data:
        payload = json.loads(result.data).get("payload")
    if case.expect_line is not None:
        lines = (payload or {}).get("lines") or []
        ok = ok and case.expect_line in lines
    if case.expect_truncated is not None:
        ok = ok and result.truncated is case.expect_truncated
    if case.expect_hit_path is not None:
        hits = (payload or {}).get("hits") or []
        ok = ok and any(h.get("path") == case.expect_hit_path for h in hits)
    if case.expect_hits is not None:
        hits = (payload or {}).get("hits") or []
        ok = ok and len(hits) == case.expect_hits
    return {
        "id": case.id,
        "kind": case.kind,
        "ok": ok,
        "status": status,
        "error": result.error,
        "truncated": result.truncated,
    }


def _isolation() -> dict[str, Any]:
    fake = FakeGitProvider()
    fake.add_snapshot("main", {"src/a.py": "def f():\n    return 1\n"})
    fake.add_snapshot("feat", {"src/a.py": "def f():\n    return 2\n"})
    fake.add_pr(1, base="main", head="feat")
    store = SqliteStorage(":memory:")
    service = ReviewService(fake, FakeLLMProvider(default_findings=[]), store)
    asyncio.run(service.review("1"))
    n = store._query("SELECT COUNT(*) AS n FROM tool_calls")[0]["n"]  # noqa: SLF001
    return {
        "id": "default-review-isolation",
        "kind": "isolate",
        "ok": n == 0 and service.settings.agent.enabled is False,
        "status": "ok" if n == 0 else "error",
        "error": None,
        "truncated": False,
    }


def run_compare(dataset_path: str | Path = _DEFAULT_DATASET) -> dict[str, Any]:
    dataset = SandboxEvalDataset.load_yaml(Path(dataset_path))
    rows = [_eval_case(case) for case in dataset.cases]
    rows.append(_isolation())
    passed = sum(1 for r in rows if r["ok"])
    return {
        "dataset": dataset.name,
        "samples": len(rows),
        "real_api": False,
        "sandbox": {
            "passed": passed,
            "total": len(rows),
            "cases": rows,
            "note": "脚本化 Fake 快照；无真实模型。不贴源码原文。",
        },
        "all_passed": passed == len(rows),
    }


def render_markdown(report: dict[str, Any]) -> str:
    box = report["sandbox"]
    lines = [
        "# V3-A 工具沙箱对照（脚本化 Fake，非真实 API）",
        "",
        "> 由 `python -m reposage.evals.v3a_compare` 生成；原始 JSON：`docs/evidence/v3-a-compare.json`。",
        f"> dataset=`{report['dataset']}` samples={report['samples']} real_api={report['real_api']}",
        "> 脱敏：不含 API Key / 源码原文 / 密钥字面量。",
        "",
        "## 越界 / 合法读 / 截断 / stub / 隔离",
        "",
        f"passed={box['passed']}/{box['total']}",
        "",
        "| id | kind | ok | status | truncated |",
        "|----|------|----|--------|-----------|",
    ]
    for row in box["cases"]:
        lines.append(
            f"| {row['id']} | {row['kind']} | {row['ok']} | {row['status']} | {row['truncated']} |"
        )
    lines.extend(
        [
            "",
            box["note"],
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
    parser = argparse.ArgumentParser(description="V3-A 工具沙箱对照")
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
