"""配置模型与加载（config/settings.py）。

层级：内置默认值 < 用户配置 ~/.reposage.yaml < 默认分支仓库配置 reposage.yaml < CLI/Action 显式参数。
铁律：PR 分支配置不读取（不提升权限、不关闭安全策略）。

严格校验（P1-7）：
- 所有子模型 extra="forbid"（未知字段拒绝）；
- 枚举字段用 Literal；
- 数值约束 gt/ge/le；上下文比例之和 ≤ 1；
- 跨字段：request_changes=true 与 dry_run=true 冲突拒绝。

契约来源：09 §6 + 附录 B。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

_MODE = Literal["local_cli", "github_action", "webhook"]
_PROVIDER = Literal["github", "local"]
_STRATEGY = Literal["single_pass", "multi_role", "agentic"]
_TOOL_PROTOCOL = Literal["native", "action_json"]
_STRUCTURED = Literal["schema_first", "json_repair"]
_BACKEND = Literal["sqlite"]
_SEVERITY = Literal["critical", "high", "medium", "low", "info"]


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "RepoSage"
    mode: _MODE = Field(default="local_cli")


class GitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: _PROVIDER = "github"
    token_env: str = "GITHUB_TOKEN"
    lock_head_sha: bool = True


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "openai_compatible"
    model: str = "DP-V4-PRO"
    api_key_env: str = "MODEL_API_KEY"
    base_url_env: str = "MODEL_BASE_URL"
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    timeout_seconds: int = Field(default=60, gt=0)
    max_retries: int = Field(default=2, ge=0)
    structured_strategy: _STRUCTURED = "schema_first"
    max_output_tokens: int = Field(
        default=3000, gt=0, description="结构化审查输出上限（09 §5：findings ~2k + summary ~1k）"
    )
    # 模型价格（美元 / 1k tokens；None = 未定价 → 费用门控标记 unknown，V1-d P0-1）
    input_price_per_1k: float | None = Field(default=None, ge=0.0)
    output_price_per_1k: float | None = Field(default=None, ge=0.0)


class ReviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: _STRATEGY = "single_pass"
    languages: list[str] = Field(default_factory=lambda: ["python"])
    min_confidence: float = Field(default=0.75, gt=0.0, le=1.0)
    max_files: int = Field(default=40, gt=0)
    roles: list[str] = Field(default_factory=lambda: ["general"])


class ContextConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_budget: int = Field(default=32000, gt=0)
    diff_ratio: float = Field(default=0.40, ge=0.0, le=1.0)
    related_code_ratio: float = Field(default=0.25, ge=0.0, le=1.0)
    rules_ratio: float = Field(default=0.10, ge=0.0, le=1.0)
    reserve_ratio: float = Field(default=0.15, ge=0.0, le=1.0)
    symbol_retrieval: bool = False  # V2

    @model_validator(mode="after")
    def _ratio_sum(self) -> ContextConfig:
        total = self.diff_ratio + self.related_code_ratio + self.rules_ratio + self.reserve_ratio
        if total > 1.0 + 1e-9:
            raise ValueError(f"上下文比例之和不能超过 1：{total:.3f}")
        return self


class ConcurrencyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_tasks: int = Field(default=3, gt=0)
    model_requests: int = Field(default=3, gt=0)
    github_requests: int = Field(default=5, gt=0)
    role_tasks: int = Field(default=3, gt=0)  # V2


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_rounds: int = Field(default=8, gt=0)
    max_tool_calls: int = Field(default=12, gt=0)
    max_wallclock_s: int = Field(default=300, gt=0)
    reserved_finalize_ratio: float = Field(default=0.10, ge=0.0, le=1.0)  # 总预算预留比例（08 §5）
    grace_rounds: int = Field(default=2, ge=0)
    repeat_threshold: int = Field(default=3, gt=0)
    tool_protocol: _TOOL_PROTOCOL = "native"  # 以评测为准


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_total_tokens: int = Field(default=80000, gt=0)
    max_cost_usd: float = Field(default=2.0, gt=0.0)
    max_runtime_seconds: int = Field(default=600, gt=0)


class PublishingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = True
    request_changes: bool = False
    idempotent: bool = True
    body_threshold_severity: _SEVERITY = "medium"

    @model_validator(mode="after")
    def _dry_run_conflict(self) -> PublishingConfig:
        # dry-run 不发布，request_changes 无意义且可能误导（P1-7）
        if self.dry_run and self.request_changes:
            raise ValueError("request_changes=true 与 dry_run=true 冲突：dry-run 不发布评论")
        return self


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: _BACKEND = "sqlite"
    path: str = ".reposage/reposage.db"


class ObservabilityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    structured_logs: bool = True
    redact_secrets: bool = True
    save_raw_chain_of_thought: bool = False
    save_tool_trace: bool = True


class PrivacyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retain_source_in_evals: bool = False
    log_level: Literal["debug", "info", "warning", "error"] = "info"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectConfig = Field(default_factory=ProjectConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    publishing: PublishingConfig = Field(default_factory=PublishingConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)

    def snapshot_hash(self) -> str:
        """配置快照哈希（复现与缓存键，06 §7）。"""
        import hashlib
        import json

        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _merge_into(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """浅层合并（嵌套 dict 递归合并）。"""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge_into(base[key], value)
        else:
            base[key] = value
    return base


def load_settings(
    user_config: Path | None = None,
    repo_config: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """按层级加载：内置默认 < 用户配置 < 仓库配置 < CLI 参数。

    overrides 来自 CLI/Action 显式参数（最高优先级）。
    """
    data: dict[str, Any] = {}
    if user_config is not None and user_config.exists():
        data = _merge_into(data, yaml.safe_load(user_config.read_text(encoding="utf-8")) or {})
    if repo_config is not None and repo_config.exists():
        data = _merge_into(data, yaml.safe_load(repo_config.read_text(encoding="utf-8")) or {})
    if overrides:
        data = _merge_into(data, overrides)
    return Settings.model_validate(data)
