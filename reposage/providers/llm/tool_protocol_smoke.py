"""OQ-11 协议探针（24 §5.4）。本轮 DEFERRED_BY_USER：不读取 Key、不调用 API。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

_DEFAULT_JSON = "docs/evidence/v3-b-oq11-protocol.json"
_DEFAULT_MD = "docs/evidence/v3-b-oq11-protocol.md"

_NOTE = (
    "用户已书面选择本阶段不运行真实模型。脚本可运行但不读取 MODEL_API_KEY、"
    "不发起 HTTP。恢复 live 验证前不得宣称 OQ-11 已由真实模型关闭。"
)


def run_probe(*, live: bool = False) -> dict[str, Any]:
    """本轮强制 deferred；``live=True`` 仍拒绝真实调用。"""
    del live
    return {
        "status": "DEFERRED_BY_USER",
        "real_api": False,
        "protocols": {
            "native": {"ran": False, "parse_ok_rate": None},
            "action_json": {"ran": False, "parse_ok_rate": None},
        },
        "decision": "keep agent.enabled=false; tool_protocol remains native (experimental)",
        "note": _NOTE,
    }


def render_markdown(report: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# V3-B OQ-11 协议探针",
            "",
            f"> 状态：**{report['status']}**",
            f"> real_api={report['real_api']}",
            "",
            "## 本轮裁决",
            "",
            report["decision"],
            "",
            report["note"],
            "",
            "| protocol | ran | parse_ok_rate |",
            "|----------|-----|---------------|",
            f"| native | {report['protocols']['native']['ran']} | {report['protocols']['native']['parse_ok_rate']} |",
            f"| action_json | {report['protocols']['action_json']['ran']} | {report['protocols']['action_json']['parse_ok_rate']} |",
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OQ-11 tool protocol probe (deferred)")
    parser.add_argument("--json-out", default=_DEFAULT_JSON)
    parser.add_argument("--md-out", default=_DEFAULT_MD)
    args = parser.parse_args(argv)
    report = run_probe()
    json_path = Path(args.json_out)
    md_path = Path(args.md_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"status={report['status']} json={json_path} md={md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
