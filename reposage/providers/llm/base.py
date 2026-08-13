"""LLM Provider 抽象导出（契约定义在 domain/protocols.py）。"""

from reposage.domain.protocols import LLMProvider, ModelResponse

__all__ = ["LLMProvider", "ModelResponse"]
