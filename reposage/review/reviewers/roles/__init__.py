"""V2 角色注册表、门控与 MultiRoleReviewer。"""

from .gates import GATE_VERSION, evaluate_gate, extract_features
from .registry import RoleRegistry, RoleRegistryError

__all__ = [
    "GATE_VERSION",
    "RoleRegistry",
    "RoleRegistryError",
    "evaluate_gate",
    "extract_features",
]
