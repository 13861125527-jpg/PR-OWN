"""脚本化候选生成（evals/scripted.py，V1-d DoD：确定性 Fake 评测）。

按 sample.id 注入确定性 FindingCandidate（模拟模型输出），故意包含：
- 行号漂移（s11：claimed 指向上下文行，pipeline 容差锚定真实新增行）；
- 幻觉路径（s12：claimed_path 不在变更文件 → suppressed）；
- 重复候选（s13：同簇两条 → fingerprint 去重为一条）；
- 部分失败（s14：b.py 文件缺陷缺失，仅 a.py 候选保留）。

供 CI 用 ScriptedStrategy 验证 Pipeline 指标计算与门槛（不访问模型）。
"""

from __future__ import annotations

from collections.abc import Callable

from reposage.domain.enums import FindingCategory, Severity
from reposage.domain.finding import FindingCandidate
from reposage.domain.models import GlobalBudget, ModelUsage, ReviewUnit
from reposage.domain.run import ReviewRun, SourceRunResult
from reposage.domain.strategy import StrategyResult


def _cand(
    path: str,
    line: int,
    category: FindingCategory,
    trigger: str,
    *,
    confidence: float = 0.9,
    severity: Severity = Severity.HIGH,
) -> FindingCandidate:
    return FindingCandidate(
        title=trigger,
        severity=severity,
        confidence=confidence,
        category=category,
        claimed_path=path,
        claimed_start_line=line,
        trigger_condition=trigger,
        explanation="scripted eval candidate",
        impact="unknown",
        suggestion="fix",
    )


def scripted_candidates(sample_id: str) -> list[FindingCandidate]:
    """按样本注入确定性候选。"""
    table: dict[str, list[FindingCandidate]] = {
        "s01-eval": [_cand("src/app.py", 2, FindingCategory.SECURITY, "eval(x)")],
        "s02-os-system": [_cand("src/runner.py", 4, FindingCategory.SECURITY, "os.system(cmd)")],
        "s03-pickle": [_cand("src/io_util.py", 4, FindingCategory.SECURITY, "pickle.loads(raw)")],
        "s04-assert": [_cand("src/validate.py", 2, FindingCategory.CORRECTNESS, "assert v > 0")],
        "s05-hardcoded-secret": [_cand("src/config.py", 2, FindingCategory.SECURITY, "hardcoded API_KEY")],
        "s06-sql-fstring": [_cand("src/db.py", 2, FindingCategory.SECURITY, "f-string SQL")],
        "s07-subprocess-shell": [_cand("src/exec.py", 3, FindingCategory.SECURITY, "shell=True")],
        "s08-multi-defect": [
            _cand("src/handler.py", 2, FindingCategory.SECURITY, "eval(d)"),
            _cand("src/handler.py", 3, FindingCategory.SECURITY, "os.system(d)"),
        ],
        "s09-negative-clean": [],
        "s10-negative-refactor": [],
        # 行号漂移：真实缺陷在行 6，claimed 行 3（上下文行）→ 容差锚定 6
        "s11-drift-line": [_cand("src/drift.py", 3, FindingCategory.SECURITY, "eval(x)")],
        # 幻觉路径：claimed_path 不存在 → suppressed
        "s12-hallucinated-path": [_cand("src/ghost.py", 1, FindingCategory.SECURITY, "hallucinated")],
        # 重复候选：同文件同簇两条 → 去重为一条
        "s13-duplicate-candidates": [
            _cand("src/dup.py", 2, FindingCategory.SECURITY, "eval(v)"),
            _cand("src/dup.py", 2, FindingCategory.SECURITY, "eval(v)"),
        ],
        # 部分失败：b.py 文件缺陷缺失（模拟该文件 LLM 失败），仅 a.py 候选保留
        "s14-partial-failure": [_cand("src/a.py", 2, FindingCategory.SECURITY, "eval(1)")],
        "s15-yaml-load": [_cand("src/loader.py", 4, FindingCategory.SECURITY, "yaml.load")],
        "s16-weak-crypto": [_cand("src/pw.py", 4, FindingCategory.SECURITY, "md5")],
        "s17-unbound-variable": [_cand("src/loop.py", 4, FindingCategory.CORRECTNESS, "return t")],
        "s18-invalid-command": [],  # 无缺陷 → 空候选（noise=0）
        "s19-deadlock-risk": [_cand("src/lock.py", 4, FindingCategory.CONCURRENCY, "lock.acquire()")],
        "s20-wide-format": [_cand("src/out.py", 2, FindingCategory.SECURITY, "secret in f-string")],
        "v2b-cross-eval": [_cand("src/app.py", 4, FindingCategory.SECURITY, "eval(x)")],
        "v2b-consume": [_cand("src/app.py", 3, FindingCategory.CORRECTNESS, "helper(x)")],
        "v2b-alias": [_cand("src/app.py", 3, FindingCategory.CORRECTNESS, "u.helper")],
        "v2b-relative": [_cand("src/pkg/app.py", 3, FindingCategory.CORRECTNESS, "Foo()")],
    }
    return [c for c in table.get(sample_id, [])]


class ScriptedStrategy:
    """评测用 Strategy：按样本注入确定性候选（不访问模型）。"""

    name = "scripted"

    def __init__(self, scripted: Callable[[str], list[FindingCandidate]]) -> None:
        self.scripted = scripted

    def supports(self, run: ReviewRun) -> bool:
        return True

    async def execute(
        self,
        units: list[ReviewUnit],
        run: ReviewRun,
        budget: GlobalBudget,
    ) -> StrategyResult:
        sample_id = run.run_id.split("eval-", 1)[-1] if "eval-" in run.run_id else run.run_id
        return StrategyResult(
            self.scripted(sample_id),
            SourceRunResult(
                strategy=run.strategy,
                tasks=[],
                usages=[ModelUsage(model="scripted", role="eval")],
                warnings=[],
            ),
        )


class _NeverUsedLLM:
    """脚本化策略不需要 LLM；占位满足类型。"""

    async def complete(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("scripted strategy 不应调用 LLM")

    async def structured(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("scripted strategy 不应调用 LLM")

    async def tool_loop(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("scripted strategy 不应调用 LLM")


__all__ = ["ScriptedStrategy", "scripted_candidates"]
