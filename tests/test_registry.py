"""RoleRegistry 配置合并测试（18 §3.2）。"""

import pytest
from pydantic import ValidationError
from reposage.config.settings import Settings
from reposage.review.reviewers.roles.registry import RoleRegistry, RoleRegistryError


def test_default_effective_is_general_required():
    registry = RoleRegistry(Settings())
    enabled = registry.effective_enabled()
    assert [s.id for s in enabled] == ["general"]
    assert enabled[0].required is True


def test_unknown_role_rejected():
    with pytest.raises(RoleRegistryError, match="未知角色"):
        RoleRegistry(Settings.model_validate({"review": {"roles": ["general", "wizard"]}}))


def test_required_downgrade_rejected():
    with pytest.raises(RoleRegistryError, match="禁止将 required"):
        RoleRegistry(
            Settings.model_validate(
                {"review": {"roles": ["general"], "role_overrides": {"general": {"required": False}}}}
            )
        )


def test_overlay_enable_false_removes_from_allowlist():
    settings = Settings.model_validate(
        {
            "review": {
                "roles": ["general", "security"],
                "role_overrides": {"security": {"enabled": False}},
            }
        }
    )
    registry = RoleRegistry(settings)
    assert [s.id for s in registry.effective_enabled()] == ["general"]


def test_roles_list_enables_optional():
    settings = Settings.model_validate(
        {"review": {"roles": ["general", "security", "correctness", "performance"]}}
    )
    registry = RoleRegistry(settings)
    assert [s.id for s in registry.effective_enabled()] == [
        "general",
        "security",
        "correctness",
        "performance",
    ]


def test_overlay_can_broaden_gate_without_changing_builtin():
    settings = Settings.model_validate(
        {
            "review": {
                "roles": ["general", "correctness"],
                "role_overrides": {"correctness": {"gate_id": "added_lines"}},
            }
        }
    )
    registry = RoleRegistry(settings)
    assert registry.get("correctness").gate_id == "added_lines"


def test_unknown_overlay_rejected():
    with pytest.raises(RoleRegistryError, match="未知角色覆盖"):
        RoleRegistry(Settings.model_validate({"review": {"role_overrides": {"nope": {"enabled": True}}}}))


def test_budget_weight_must_be_positive():
    with pytest.raises(ValidationError):
        Settings.model_validate({"review": {"role_overrides": {"general": {"budget_weight": 0}}}})


def test_unimplemented_role_cannot_be_enabled():
    with pytest.raises(RoleRegistryError, match="角色未实现"):
        RoleRegistry(Settings.model_validate({"review": {"roles": ["general", "silent-failure"]}}))


def test_no_required_enabled_rejected():
    with pytest.raises(RoleRegistryError, match="至少需要一个"):
        RoleRegistry(
            Settings.model_validate(
                {"review": {"roles": ["security"], "role_overrides": {"security": {"enabled": True}}}}
            )
        )
