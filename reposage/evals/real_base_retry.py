"""Retry only the Base arm for samples whose structured output was truncated."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from reposage.config.settings import Settings
from reposage.evals.dataset import EvalDataset
from reposage.evals.real_v3_compare import _arm, _score_judges, _settings
from reposage.evals.semantic_judge import SemanticJudgeConfig
from reposage.providers.llm.openai_compat import OpenAICompatProvider, resolve_credentials


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    key, url = resolve_credentials(settings.llm)
    if not key or not url:
        return 1
    cfg = SemanticJudgeConfig.load(Path(args.judge_config))
    ds = EvalDataset.load_yaml(Path(args.dataset))
    wanted = set(args.sample_ids.split(","))
    samples = [sample for sample in ds.samples if sample.id in wanted]
    provider = OpenAICompatProvider.from_config(_settings(args.model, agentic=False).llm)
    judges = [
        OpenAICompatProvider(
            model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
            temperature=cfg.temperature, timeout_seconds=cfg.timeout_seconds,
            max_retries=cfg.max_retries, max_output_tokens=cfg.max_output_tokens,
        )
        for _ in range(2)
    ]
    rows: list[dict[str, Any]] = []
    try:
        for index, sample in enumerate(samples, 1):
            print(f"[{index}/{len(samples)}] {sample.id}", flush=True)
            arm = await _arm(sample, provider, model=args.model, agentic=False)
            findings = list(arm["finding_objects"])
            await _score_judges(sample, arm, findings, judges, cfg)
            rows.append({"id": sample.id, "base": arm})
    finally:
        await provider.aclose()
        for judge in judges:
            await judge.aclose()
    Path(args.output).write_text(json.dumps({"model": args.model, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps([{"id": row["id"], "task_failures": row["base"]["task_failures"], "accepted": row["base"]["accepted_count"]} for row in rows], ensure_ascii=False, indent=2))
    return 0 if all(not row["base"]["task_failures"] for row in rows) else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="reposage/evals/datasets/resume_l3_context_30.yaml")
    parser.add_argument("--sample-ids", required=True)
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--judge-config", default="config/semantic_judge.local.json")
    parser.add_argument("--output", default="docs/evidence/v3-live-base-cleanup.json")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
