# V2-B Implementation Review Round 3

> Review date: 2026-08-16
> Scope: verify Round 2 blockers/should-fixes for V2-B symbol/L3 retrieval.

## Verdict

**V2-B core is accepted.**

The previous blocking issues are closed:

- B1 closed: snapshot preparation no longer preloads every import target. It filters import targets through `used_import_targets()` and added-line used names.
- B2 closed: L3 collection failures are no longer silently erased. They are surfaced as `L3 extraction failed: <ExceptionType>` in warnings and coverage without leaking source text.
- B3 closed: local composition now injects `LocalGitSnapshotProvider` into `ReviewService`; the CLI path uses the real local Git provider path rather than only Fake snapshots.
- S1 closed: cap attribution now tracks changed-file owners through `SnapshotCandidate.owners` and `cap_affected_paths`.
- S2 closed: local Git path listing is cached per SHA, and diagnostics now use `blobs_kept` instead of the misleading `hits_kept`.

## Evidence

### Gates

Command results:

- `pytest`: passed, 421 tests
- `ruff check reposage tests --no-cache`: passed
- `mypy reposage --strict`: passed, 63 source files

### V2-B Compare

`reposage.evals.v2b_compare` was rerun successfully to a writable temp evidence path because overwriting the existing `docs/evidence/v2-b-compare.json` was denied by the OS.

Temp output:

- `C:\Users\Admin\Documents\Codex\v2-b-compare-r3.json`
- `C:\Users\Admin\Documents\Codex\v2-b-compare-r3.md`

Metrics:

| Metric | L3-off | L3-on |
|---|---:|---:|
| Precision | 0.75 | 1.0 |
| Recall | 0.75 | 1.0 |
| F1 | 0.75 | 1.0 |
| Position accuracy | 0.75 | 1.0 |
| Model calls | 4 | 4 |
| Estimated input tokens | 645 | 808 |
| L3 chunks | 0 | 4 |
| Budget rejects | 0 | 0 |

Retrieval metric embedded in compare:

- hit_rate = 1.0
- false_rate = 0.0
- TP/FN/FP = 9/0/0
- expected = 9
- forbidden = 6

Note: this remains a scripted Fake comparison. It proves the pipeline consumes L3 context and that retrieval returns the intended symbols; it does not prove real-model quality gain.

## Code Review Findings

### P2: run-level coverage `truncated` ignores skipped coverage items

`ReviewService._merge_coverage()` computes the aggregate `CoverageManifest.truncated` flag from unit coverage and strategy extra items, but it does not include `skipped_coverage`.

Impact:

- In the L3 capability-miss path, the service appends a `CoverageItem(reason=TRUNCATED, detail="capability miss: GitSnapshotProvider")` to `skipped_coverage`.
- The coverage item and warning are present, so the issue is disclosed.
- However, the aggregate `CoverageManifest.truncated` boolean can still be false, which can mislead storage or dashboards that rely on the top-level flag.

Suggested fix:

```python
truncated = (
    any(u.coverage.truncated for u in units)
    or any(i.reason.value == "truncated" for i in skipped_coverage)
    or any(i.reason.value == "truncated" for i in extra_items)
)
```

Suggested test:

- Extend `test_l3_capability_miss_is_disclosed()` to assert `run.coverage.truncated is True`.

This is **not blocking V2-B core acceptance**, because the primary local L3 path works and the capability miss is already surfaced as warning + coverage item.

## Line References

- `reposage/review/symbols/retrieve.py`: `used_import_targets()` filters unused imports.
- `reposage/review/symbols/snapshot.py`: `SnapshotCandidate.owners`, `cap_affected_paths`, `blobs_kept`.
- `reposage/review/context.py`: L3 extraction failures are converted into coverage/warnings rather than empty hits.
- `reposage/app/compose.py`: `compose_local_service()` injects `LocalGitSnapshotProvider`.
- `reposage/providers/git/local.py`: `list_paths()` cache and local Git snapshot provider.
- `tests/test_l3_snapshot.py`: unused import preload and cap owner coverage.
- `tests/test_local_git.py`: local provider cache and real local Git composition test.
- `tests/test_service.py`: L3 capability miss and extraction failure disclosure.

## Final Status

**V2-B: ACCEPTED for core implementation.**

Recommended next action:

- Apply the small P2 coverage aggregation fix above, then move on to the next planned phase.
