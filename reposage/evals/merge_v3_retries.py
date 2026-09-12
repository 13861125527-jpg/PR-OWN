"""Merge the final valid row for each V3 live-evaluation sample."""

from __future__ import annotations

import json
from pathlib import Path

from reposage.evals.dataset import EvalDataset
from reposage.evals.real_v3_compare import DATASET, MARKDOWN, OUTPUT, _aggregate, _markdown


def main() -> None:
    sources = [
        Path("docs/evidence/resume-v3-real-compare-30.json"),
        Path("docs/evidence/v3-live-retry.json"),
        Path("docs/evidence/v3-live-final-retry.json"),
        Path("docs/evidence/v3-live-cleanup-retry.json"),
    ]
    by_id: dict[str, dict[str, object]] = {}
    model = judge_model = ""
    for source in sources:
        payload = json.loads(source.read_text(encoding="utf-8"))
        model = str(payload["model"])
        judge_model = str(payload["judge_model"])
        for row in payload["per_sample"]:
            by_id[str(row["id"])] = row
    base_cleanup = json.loads(
        Path("docs/evidence/v3-live-base-cleanup.json").read_text(encoding="utf-8")
    )
    for row in base_cleanup["rows"]:
        by_id[str(row["id"])]["base"] = row["base"]
    dataset = EvalDataset.load_yaml(Path(DATASET))
    missing = [sample.id for sample in dataset.samples if sample.id not in by_id]
    if missing:
        raise ValueError(f"missing samples: {missing}")
    rows = [by_id[sample.id] for sample in dataset.samples]
    report = _aggregate(rows, model, judge_model)
    report["execution_completed"] = True
    report["all_samples_judged"] = True
    report["limitations"] = [
        "Rows affected by protocol, fingerprint, or corrected-gold defects were replaced by one controlled retry.",
        "Agent partial status is reported separately and does not erase findings already submitted and judged.",
        "This is one model run per sample; sampling variance was not estimated.",
    ]
    Path(OUTPUT).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(MARKDOWN).write_text(_markdown(report), encoding="utf-8")
    print(json.dumps({"sample_count": len(rows), "arms": report["arms"], "delta": report["delta_v3_minus_base"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
