"""结构化 JSONL 日志测试（11 §2 / 10 §5，V1-f）。"""

import io
import json

from reposage.observability.logging import StructuredLogger, redact_secrets


def test_log_emits_jsonl_line_with_ids():
    sink = io.StringIO()
    logger = StructuredLogger(sink=sink)
    logger.log(
        level="info",
        run_id="run-1",
        stage="review",
        task_id="run-1:src/a.py",
        event="stage_completed",
        detail="units=1 candidates=1",
    )
    lines = [line for line in sink.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    # 字段齐全（11 §2）
    for field in (
        "ts", "level", "run_id", "stage", "task_id", "tool_call_id",
        "finding_occurrence_id", "fingerprint", "event", "detail",
    ):
        assert field in record
    assert record["run_id"] == "run-1"
    assert record["stage"] == "review"
    assert record["event"] == "stage_completed"


def test_log_redacts_secrets_in_detail():
    sink = io.StringIO()
    logger = StructuredLogger(sink=sink)
    logger.log(
        level="error", run_id="run-1", stage="fetch", event="stage_failed",
        detail="failed with api_key=sk-abcdef1234567890 token=ghp_1234567890abcdef",
    )
    record = json.loads(sink.getvalue().splitlines()[0])
    detail = record["detail"]
    assert "sk-abcdef1234567890" not in detail
    assert "ghp_1234567890abcdef" not in detail
    assert "***" in detail


def test_log_failure_does_not_raise():
    """V1-f：日志写入失败不得影响主流程。"""

    class _BoomSink:
        def write(self, _data: str) -> None:
            raise OSError("disk full")

        def flush(self) -> None:
            raise OSError("disk full")

    logger = StructuredLogger(sink=_BoomSink())  # type: ignore[arg-type]
    # 不应抛出异常
    logger.log(level="info", run_id="run-1", stage="preflight", event="run_start")


def test_gate_decision_log_has_no_source_snippet():
    sink = io.StringIO()
    logger = StructuredLogger(sink=sink)
    logger.log(
        level="info",
        run_id="run-1",
        stage="review",
        event="gate_decision",
        detail="path=src/app.py role=security enabled=True reason=gate_hit features=['exec_dyn']",
    )
    record = json.loads(sink.getvalue().splitlines()[0])
    assert record["event"] == "gate_decision"
    assert "return eval" not in record["detail"]
    assert "exec_dyn" in record["detail"]


def test_redact_secrets_covers_common_forms():
    cases = [
        ("sk-abcdef1234567890", "sk-abcdef1234567890"),
        ("ghp_1234567890abcdef", "ghp_1234567890abcdef"),
        ('authorization: Bearer abcdef123456', "abcdef123456"),
        ('api_key="supersecretvalue"', "supersecretvalue"),
        ("password=hardcoded123", "hardcoded123"),
        ("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----", "abc"),
    ]
    for text, secret in cases:
        out = redact_secrets(text)
        assert secret not in out, f"{text!r} 未脱敏：{out!r}"
