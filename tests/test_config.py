"""配置加载测试（09 §6 层级 + P1-7 严格校验）。"""

from pathlib import Path

import pytest
from pydantic import ValidationError
from reposage.config.settings import Settings, load_settings


def test_defaults():
    s = Settings()
    assert s.project.mode == "local_cli"
    assert s.review.strategy == "single_pass"
    assert s.publishing.dry_run is True
    assert s.agent.reserved_finalize_ratio == 0.10
    assert s.context.symbol_retrieval is True
    assert s.review.static.enabled is False
    assert s.review.judge.enabled is False
    assert s.review.judge.max_findings == 32
    assert s.review.feedback.enabled is True
    assert s.review.feedback.max_items_per_file == 5
    assert s.review.incremental.enabled is False
    assert s.review.static.analyzers == ["ruff"]


def test_load_settings_merge(tmp_path: Path):
    user = tmp_path / "user.yaml"
    repo = tmp_path / "repo.yaml"
    user.write_text("review:\n  strategy: multi_role\n  max_files: 10\n", encoding="utf-8")
    repo.write_text("review:\n  max_files: 99\npublishing:\n  dry_run: false\n", encoding="utf-8")

    s = load_settings(user_config=user, repo_config=repo, overrides={"review": {"max_files": 5}})
    assert s.review.strategy == "multi_role"  # 用户配置生效（仓库未覆盖）
    assert s.review.max_files == 5  # CLI 覆盖
    assert s.publishing.dry_run is False  # 仓库配置生效


def test_snapshot_hash_changes_with_config():
    a = Settings()
    b = Settings.model_validate({"review": {"max_files": 3}})
    assert a.snapshot_hash() != b.snapshot_hash()


def test_snapshot_hash_stable_for_same_config():
    """V1-f：相同配置快照哈希稳定。"""
    a = Settings()
    b = Settings()
    assert a.snapshot_hash() == b.snapshot_hash()


def test_snapshot_payload_excludes_secrets():
    """V1-f：config 快照剥离 secret 字段（只存 xxx_env 名），密钥不进快照内容。"""
    payload = Settings().snapshot_payload()
    # 环境变量名保留（可复现），但不得出现任何高熵 secret 值
    assert "MODEL_API_KEY" in payload or '"api_key_env": "***"' in payload
    assert "GITHUB_TOKEN" in payload
    # 不泄漏任何 secret 形态
    assert "sk-" not in payload
    assert "ghp_" not in payload


def test_unknown_field_rejected():
    """P1-7：extra=forbid——未知字段拒绝。"""
    with pytest.raises(ValidationError):
        Settings.model_validate({"review": {"unknown_option": 1}})


def test_negative_concurrency_rejected():
    with pytest.raises(ValidationError):
        Settings.model_validate({"concurrency": {"file_tasks": -1}})


def test_invalid_strategy_rejected():
    with pytest.raises(ValidationError):
        Settings.model_validate({"review": {"strategy": "magic"}})


def test_ratio_over_one_rejected():
    with pytest.raises(ValidationError):
        Settings.model_validate({"context": {"diff_ratio": 0.6, "related_code_ratio": 0.5}})


def test_request_changes_with_dry_run_rejected():
    """P1-7：request_changes=true 与 dry_run=true 冲突。"""
    with pytest.raises(ValidationError):
        Settings.model_validate({"publishing": {"dry_run": True, "request_changes": True}})
    # dry_run=false 时允许
    Settings.model_validate({"publishing": {"dry_run": False, "request_changes": True}})


def test_unknown_static_analyzer_rejected():
    with pytest.raises(ValidationError, match="未知静态分析器"):
        Settings.model_validate({"review": {"static": {"analyzers": ["bandit"]}}})


def test_negative_budget_rejected():
    with pytest.raises(ValidationError):
        Settings.model_validate({"budget": {"max_total_tokens": 0}})
