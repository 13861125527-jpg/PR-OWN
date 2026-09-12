"""RoleRegistry：内置 spec + Settings 唯一合并（18 §3.2）。"""

from __future__ import annotations

from reposage.config.settings import ReviewConfig, RoleOverlayConfig, Settings
from reposage.domain.run import RoleSpec

BUILTIN_ROLE_SPECS: tuple[RoleSpec, ...] = (
    RoleSpec(
        id="general",
        prompt_id="general",
        gate_id="always",
        required=True,
        enabled=True,
    ),
    RoleSpec(
        id="security",
        prompt_id="security",
        gate_id="security",
        required=False,
        enabled=True,
    ),
    RoleSpec(
        id="correctness",
        prompt_id="correctness",
        gate_id="correctness",
        required=False,
        enabled=True,
    ),
    RoleSpec(
        id="performance",
        prompt_id="performance",
        gate_id="performance",
        required=False,
        enabled=True,
    ),
    # 占位：默认不在 review.roles 中，不评估
    RoleSpec(id="silent-failure", prompt_id="silent-failure", gate_id="correctness", enabled=False, implemented=False),
    RoleSpec(id="edge-case", prompt_id="edge-case", gate_id="correctness", enabled=False, implemented=False),
    RoleSpec(id="concurrency", prompt_id="concurrency", gate_id="always", enabled=False, implemented=False),
    RoleSpec(id="test", prompt_id="test", gate_id="always", enabled=False, implemented=False),
)


class RoleRegistryError(ValueError):
    """角色配置非法（preflight FAILED）。"""


class RoleRegistry:
    """合并 builtin < user/repo/CLI（Settings 已合并）后的有效角色表。"""

    def __init__(self, settings: Settings | None = None, *, review: ReviewConfig | None = None) -> None:
        self._review = review if review is not None else (settings or Settings()).review
        self._by_id = {spec.id: spec for spec in BUILTIN_ROLE_SPECS}
        self._merged = self._merge()
        self._validate()

    def _overlay(self, role_id: str) -> RoleOverlayConfig:
        return self._review.role_overrides.get(role_id, RoleOverlayConfig())

    def _merge(self) -> dict[str, RoleSpec]:
        merged: dict[str, RoleSpec] = {sid: spec.model_copy() for sid, spec in self._by_id.items()}
        for role_id, overlay in self._review.role_overrides.items():
            if role_id not in merged:
                raise RoleRegistryError(f"未知角色覆盖: {role_id}")
            spec = merged[role_id]
            updates: dict[str, object] = {}
            if overlay.enabled is not None:
                updates["enabled"] = overlay.enabled
            if overlay.required is True:
                updates["required"] = True
            if overlay.required is False and spec.required:
                raise RoleRegistryError(f"禁止将 required 角色降级: {role_id}")
            if overlay.budget_weight is not None:
                updates["budget_weight"] = overlay.budget_weight
            if overlay.gate_id is not None:
                updates["gate_id"] = overlay.gate_id
            if updates:
                merged[role_id] = spec.model_copy(update=updates)
        return merged

    def _validate(self) -> None:
        unknown = [rid for rid in self._review.roles if rid not in self._by_id]
        if unknown:
            raise RoleRegistryError(f"未知角色: {unknown}")
        unimplemented = [
            rid
            for rid in self._review.roles
            if rid in self._by_id
            and not self._merged[rid].implemented
            and self._overlay(rid).enabled is not False
        ]
        if unimplemented:
            raise RoleRegistryError(f"角色未实现，禁止启用: {unimplemented}")
        if len(self._review.roles) != len(set(self._review.roles)):
            raise RoleRegistryError("review.roles 含重复 id")
        enabled = self.effective_enabled()
        if not any(spec.required for spec in enabled):
            raise RoleRegistryError("至少需要一个 enabled 且 required 的角色")

    def get(self, role_id: str) -> RoleSpec:
        return self._merged[role_id]

    def effective_enabled(self) -> list[RoleSpec]:
        """effective_enabled = id ∈ review.roles AND overlay.enabled is not False。"""
        result: list[RoleSpec] = []
        for role_id in self._review.roles:
            overlay = self._overlay(role_id)
            if overlay.enabled is False:
                continue
            result.append(self._merged[role_id])
        return result

    def registered_ids(self) -> frozenset[str]:
        return frozenset(self._by_id)
