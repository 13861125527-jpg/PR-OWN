# V2-C Implementation Review Round 1

> Review date: 2026-08-16
> Scope: V2-C static analyzer fusion implementation against `docs/architecture/20-v2c-alignment.md`.

## Verdict

**V2-C is implemented but not accepted.**

The base implementation is broad and most gates pass, but two behavior-level blockers remain:

1. `RuffAnalyzer` can silently treat missing/unusable ruff as "no diagnostics".
2. Pipeline cross-source fusion can indirectly merge multiple different static rules through one LLM finding, losing the static `rule_id`.

Both contradict the frozen V2-C alignment.

## Gates Run

All mechanical gates passed:

- `pytest`: passed, 437 tests
- `ruff check reposage tests --no-cache`: passed
- `mypy reposage --strict`: passed, 71 source files
- `v2c_compare`: passed 3/3 conversion and 4/4 dedup/fusion cases
- V2-A compare rerun: passed
- V2-B compare rerun: passed

Temp evidence generated during review:

- `C:\Users\Admin\Documents\Codex\v2-c-compare-review.md`
- `C:\Users\Admin\Documents\Codex\v2-a-compare-v2c-review.md`
- `C:\Users\Admin\Documents\Codex\v2-b-compare-v2c-review.md`

## Blocking Findings

### B1. Missing ruff can be misclassified as "no diagnostics"

Location: `reposage/review/static/ruff.py:83-89`

Current behavior:

- Exit code `>=2` is treated as failure.
- Exit code `1` is treated as successful diagnostics.
- `stdout.decode("utf-8") or "[]"` converts empty stdout into an empty diagnostics list.

Why this is wrong:

- `python -m ruff` can fail with exit code `1` when the module is missing/unusable, with the error on stderr and empty stdout.
- Current code can convert that into `AnalyzerRunResult(status="ok", diagnostics=[])`.
- That violates V2-C §9: missing ruff must be `capability miss + truncated`, never "zero issues".

Required fix:

- Do not coerce empty stdout to `[]`.
- For exit code `1`, require valid JSON stdout. If stdout is empty or JSON invalid, return failed with warning like `capability miss: ruff` or `ruff json invalid`.
- Keep `0` as success only when stdout is valid JSON, normally `[]`.

Required test:

- Add a unit test that simulates `python -m ruff` returning code `1`, empty stdout, stderr containing module/import error, and assert `AnalyzerRunResult.status == "failed"` plus capability/JSON warning.

### B2. Cross-source fusion can merge different static rules through one LLM finding

Location: `reposage/review/pipeline.py:242-264`, `reposage/review/pipeline.py:372-399`

Current behavior:

- `_fuse_cross_source()` repeatedly merges clusters if any pair is fuseable.
- A single LLM finding can fuse with static rule A, then the merged cluster can fuse again with static rule B.
- `_cluster_rule_key()` sees multiple static keys and falls back to `model`.

Observed repro:

- Inputs: one LLM security finding near two static security findings: `ruff:S307` and `ruff:S110`.
- Actual output: one accepted finding, sources include LLM + static, but `rule_id=None` / model key.
- Expected output: two findings, or at minimum no fusion that combines two different static `rule_id`s.

Why this is wrong:

- V2-C §7.4 says different static `rule_id` findings must not fuse due to same-line/near-line behavior.
- V2-C §7.3 / DP-9 says LLM+static fusion should keep the static `rule_id` for fingerprint and cross-run key.

Required fix:

- Prevent fusion if the resulting cluster would contain more than one distinct static `rule_id`.
- A clean implementation is to make `_clusters_fuseable()` compute the union of static rule keys from both clusters and return false if `len(static_rule_keys) > 1`.
- Keep current behavior for:
  - one LLM + one static rule;
  - multiple LLMs + one static rule;
  - duplicate static diagnostics with the same rule.

Required tests:

- Add a Pipeline test:
  - LLM security finding near `ruff:S307` and `ruff:S110`;
  - expected accepted count is `2`;
  - no accepted finding has both static rule sources merged into one `model` fingerprint.
- Add a `v2c_static.yaml` case for this transitive-fusion negative sample.

## Confirmed Good

- `FindingCandidate.source_kind/rule_id/analyzer_id` exists and LLM schema excludes these fields.
- `sanitize_llm_candidate()` / `prepare_llm_candidate()` strip model-supplied program fields.
- `StaticAnalyzer` protocol, Fake analyzer, converter, runner, and `RuffAnalyzer` are present.
- Static candidates use `STATIC_ANALYZER` + `STATIC_RESULT`.
- Pipeline verifies `STATIC_RESULT` only for authentic static sources.
- Static disabled path creates no static task/subprocess.
- Static capability miss through missing snapshot is warning + coverage truncated and does not fail the whole run.
- Static analysis runs in REVIEW alongside the LLM strategy and does not consume `GlobalBudget`.

## Next Acceptance Conditions

V2-C can be accepted after:

1. Fix B1 and B2.
2. Add the two missing tests above.
3. Rerun:
   - full pytest
   - Ruff
   - mypy strict
   - `v2c_compare`
   - V2-A and V2-B compare to confirm static remains disabled there.

## Status

**V2-C: IMPLEMENTED / NOT ACCEPTED**
