"""AgentMessage → OpenAI-compatible chat messages。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from reposage.domain.models import AgentMessage


def messages_to_chat(messages: Sequence[AgentMessage], *, protocol: str) -> list[dict[str, Any]]:
    """把会话消息转成 chat.completions 列表。"""
    out: list[dict[str, Any]] = []
    for msg in messages:
        if protocol == "native" and msg.role == "assistant" and msg.tool_calls:
            out.append(
                {
                    "role": "assistant",
                    "content": msg.content or None,
                    "tool_calls": [
                        {
                            "id": item.id,
                            "type": "function",
                            "function": {
                                "name": item.name,
                                "arguments": json.dumps(item.arguments, ensure_ascii=False),
                            },
                        }
                        for item in msg.tool_calls
                    ],
                    **(
                        {"reasoning_content": msg.reasoning_content}
                        if msg.reasoning_content is not None
                        else {}
                    ),
                }
            )
            continue
        if protocol == "native" and msg.role == "tool":
            item: dict[str, Any] = {
                "role": "tool",
                "content": msg.content,
                "tool_call_id": msg.tool_call_id or "",
            }
            if msg.name:
                item["name"] = msg.name
            out.append(item)
            continue
        item = {"role": msg.role, "content": msg.content}
        if msg.role == "assistant" and msg.reasoning_content is not None:
            item["reasoning_content"] = msg.reasoning_content
        out.append(item)
    return out
