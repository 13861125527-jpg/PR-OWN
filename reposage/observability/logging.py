"""结构化 JSONL 日志（11 §2 / 10 §5，V1-f）。

字段：`ts, level, run_id, stage, task_id, tool_call_id, finding_occurrence_id,
fingerprint, event, detail`。

- 脱敏：token/key/secret/authorization 等凭证片段一律 `***`（`10` §5）；
- 禁止写入：prompt、完整源码、API 响应正文、raw CoT；
- 失败隔离：日志写入异常不得影响审查主流程（吞掉，不让日志故障拖垮 review）。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from typing import Any, TextIO


def redact_secrets(text: str) -> str:
    """凭证脱敏（10 §5 / observability.redact_secrets）。

    覆盖常见凭证前缀 + key=value 高熵值 + authorization/bearer + PEM 私钥块。
    与 publishing.publisher._redact 同源；日志侧覆盖范围一致，保证日志不会泄漏凭证。
    """
    cleaned = re.sub(
        r"(?i)(sk-[a-z0-9_-]{4,}|bearer\s+[a-z0-9._-]{4,}|"
        r"gh[pousr]_[a-z0-9]{8,}|github_pat_[a-z0-9_]{8,}|"
        r"akia[a-z0-9]{12,}|xox[baprs]-[a-z0-9-]{8,}|ai[za][a-z0-9_-]{8,})",
        "***",
        text,
    )
    cleaned = re.sub(
        r"(?i)(api[_-]?key|password|passwd|token|secret|authorization)\s*[=:]\s*[\"']?[^\s\"',;]{4,}[\"']?",
        r"\1=***",
        cleaned,
    )
    cleaned = re.sub(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        "***",
        cleaned,
        flags=re.DOTALL,
    )
    cleaned = re.sub(
        r"(?i)(postgres(?:ql)?|mysql|mongodb(\+srv)?)://[^:/\s]+:[^@/\s]+@",
        r"\1://***@",
        cleaned,
    )
    return cleaned


class StructuredLogger:
    """最小结构化 JSONL 日志器（V1-f）。

    每条日志一行 JSON，字段固定（11 §2）；detail 统一脱敏；写失败静默忽略。
    """

    def __init__(self, sink: TextIO | None = None) -> None:
        self._sink = sink if sink is not None else sys.stderr

    def log(
        self,
        *,
        level: str = "info",
        run_id: str | None = None,
        stage: str | None = None,
        task_id: str | None = None,
        tool_call_id: str | None = None,
        finding_occurrence_id: str | None = None,
        fingerprint: str | None = None,
        event: str,
        detail: str | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": level,
            "run_id": run_id,
            "stage": stage,
            "task_id": task_id,
            "tool_call_id": tool_call_id,
            "finding_occurrence_id": finding_occurrence_id,
            "fingerprint": fingerprint,
            "event": event,
            "detail": redact_secrets(detail) if detail else None,
        }
        try:
            self._sink.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._sink.flush()
        except Exception:  # noqa: BLE001 —— 日志失败不得影响审查主流程
            pass


__all__ = ["StructuredLogger", "redact_secrets"]
