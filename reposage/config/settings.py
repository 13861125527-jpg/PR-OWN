"""配置模型与加载（config/settings.py）。

层级：内置默认值 < 用户配置 ~/.reposage.yaml < 默认分支仓库配置 reposage.yaml < CLI/Action 显式参数。
铁律：PR 分支配置不读取（不提升权限、不关闭安全策略）。
契约来源：09 §6 + 附录 B。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class ProjectConfig(BaseModel):
    name: str = "RepoSage"
    mode: str = Field(default="local_cli", description="local_cli | github_action | webhook")


class GitConfig(BaseModel):
    provider: str = Field(default="github", description="github | local")
    token_env: str = "GITHUB_TOKEN"
    lock_head_sha: bool = True


class LLMConfig(BaseModel):
    provider: str = "openai_compatible"
    model: str = "DP-V4-PRO"
    api_key_env: str = "MODEL_API_KEY"
    base_url_env: str = "MODEL_BASE_URL"
    temperature: float = 0.1
    timeout_seconds: int = 60
    max_retries: int = 2
    structured_strategy: str = Field(default="schema_first", description="schema_first | json_repair")


class ReviewConfig(BaseModel):
    strategy: str = Field(default="single_pass", description="single_pass | multi_role | agentic")
    languages: list[str] = Field(default_factory=lambda: ["python"])
    min_confidence: float = 0.75
    max_files: int = 40
    roles: list[str] = Field(default_factory=lambda: ["general"])


class ContextConfig(BaseModel):
    token_budget: int = 32000
    diff_ratio: float = 0.40
    related_code_ratio: float = 0.25
    rules_ratio: float = 0.10
    reserve_ratio: float = 0.15
    symbol_retrieval: bool = False  # V2


class ConcurrencyConfig(BaseModel):
    file_tasks: int = 3
    model_requests: int = 3
    github_requests: int = 5
    role_tasks: int = 3  # V2


class AgentConfig(BaseModel):
    enabled: bool = False
    max_rounds: int = 8
    max_tool_calls: int = 12
    max_wallclock_s: int = 300
    reserved_finalize_ratio: float = 0.10  # 总预算预留比例（08 §5）
    grace_rounds: int = 2
    repeat_threshold: int = 3
    tool_protocol: str = Field(default="native", description="native | action_json（以评测为准）")


class BudgetConfig(BaseModel):
    max_total_tokens: int = 80000
    max_cost_usd: float = 2.0
    max_runtime_seconds: int = 600


class PublishingConfig(BaseModel):
    dry_run: bool = True
    request_changes: bool = False
    idempotent: bool = True
    body_threshold_severity: str = "medium"


class StorageConfig(BaseModel):
    backend: str = "sqlite"
    path: str = ".reposage/reposage.db"


class ObservabilityConfig(BaseModel):
    structured_logs: bool = True
    redact_secrets: bool = True
    save_raw_chain_of_thought: bool = False
    save_tool_trace: bool = True


class PrivacyConfig(BaseModel):
    retain_source_in_evals: bool = False
    log_level: str = "info"


class Settings(BaseModel):
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
