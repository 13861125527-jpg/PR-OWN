"""配置加载测试（09 §6 层级）。"""

from pathlib import Path

from reposage.config.settings import Settings, load_settings


def test_defaults():
    s = Settings()
    assert s.project.mode == "local_cli"
    assert s.review.strategy == "single_pass"
    assert s.publishing.dry_run is True
    assert s.agent.reserved_finalize_ratio == 0.10


def test_load_settings_merge(tmp_path: Path):
    user = tmp_path / "user.yaml"
    repo = tmp_path / "repo.yaml"
    user.write_text("review:\n  strategy: multi_role\n  max_files: 10\n", encoding="utf-8")
    repo.write_text("review:\n  max_files: 99\npublishing:\n  dry_run: false\n", encoding="utf-8")

    s = load_settings(user_config=user, repo_config=repo, overrides={"review": {"max_files": 5}})
    # 层级：CLI 覆盖 > 仓库 > 用户
    assert s.review.strategy == "multi_role"  # 用户配置生效（仓库未覆盖）
    assert s.review.max_files == 5  # CLI 覆盖
    assert s.publishing.dry_run is False  # 仓库配置生效


def test_snapshot_hash_changes_with_config():
    a = Settings(review=Settings.model_fields["review"].default)
    b = Settings.model_validate({"review": {"max_files": 3}})
    assert a.snapshot_hash() != b.snapshot_hash()
