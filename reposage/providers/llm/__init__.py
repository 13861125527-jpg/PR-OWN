"""LLM Provider 抽象导出（契约定义在 domain/protocols.py）。

- LLMProvider / ModelResponse：抽象协议（V1 边界）
- FakeLLMProvider：脚本化提供者（测试/评测）
- OpenAICompatProvider：OpenAI-compatible 实现（V1-c，09 §3）
"""

from reposage.domain.protocols import LLMProvider, ModelResponse
from reposage.providers.llm.fake import FakeLLMProvider
from reposage.providers.llm.openai_compat import (
    LLMRequestError,
    OpenAICompatProvider,
    StructuredOutputError,
)

__all__ = [
    "FakeLLMProvider",
    "LLMProvider",
    "LLMRequestError",
    "ModelResponse",
    "OpenAICompatProvider",
    "StructuredOutputError",
]
