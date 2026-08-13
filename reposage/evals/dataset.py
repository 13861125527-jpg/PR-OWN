"""评测数据集模型（evals/dataset.py）。

评测集构成见 11 §5：人工单缺陷 PR、真实修复反向样本、无缺陷负样本、
跨文件样本、提示注入、稳健性样本。Phase 0 只定义格式与加载骨架。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ExpectedFinding(BaseModel):
    """一条标注期望命中的缺陷。"""

    category: str
    path: str | None = None
    line: int | None = None
    severity: str | None = None
    note: str = ""


class EvalSample(BaseModel):
    """评测样本。"""

    id: str
    kind: str = Field(default="single_defect", description="single_defect|reversed_fix|negative|cross_file|injection|robustness")
    base_files: dict[str, str] = Field(default_factory=dict, description="{path: content}@base")
    head_files: dict[str, str] = Field(default_factory=dict, description="{path: content}@head")
    pr_title: str = ""
    pr_description: str = ""
    expected: list[ExpectedFinding] = Field(default_factory=list)
    notes: str = ""


class EvalDataset(BaseModel):
    """评测集。"""

    name: str
    samples: list[EvalSample] = Field(default_factory=list)

    @classmethod
    def load_yaml(cls, path: Path) -> EvalDataset:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)
