"""Generate 30 auditable cross-file samples that deterministically trigger L3."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

OUTPUT = Path("reposage/evals/datasets/resume_l3_context_30.yaml")


def _case(
    index: int,
    slug: str,
    contract: str,
    before: str,
    after: str,
    *,
    category: str | None,
    note: str,
) -> dict[str, Any]:
    helper = f"contract_{index:02d}"
    changed = f"src/case_{index:02d}.py"
    related = f"lib/{helper}.py"
    import_line = f"from lib.{helper} import check\n"
    base = import_line + "\ndef process(value):\n" + f"    return {before}\n"
    head = import_line + "\ndef process(value):\n" + f"    return {after}\n"
    expected = []
    if category is not None:
        expected.append(
            {
                "category": category,
                "path": changed,
                "line": 4,
                "severity": "medium",
                "note": note,
            }
        )
    return {
        "id": f"l3-{index:02d}-{slug}",
        "kind": "cross_file" if expected else "negative",
        "pr_title": slug.replace("-", " "),
        "pr_description": "The imported helper contract is defined in an unchanged module.",
        "base_files": {changed: base, related: contract},
        "head_files": {changed: head, related: contract},
        "expected": expected,
        "notes": (
            f"requires_l3=true; expected_l3_path={related}; "
            f"expected_l3_symbol=check; audit={note}"
        ),
    }


def build_samples() -> list[dict[str, Any]]:
    raw = [
        # correctness / contract mismatches
        ("none-sentinel", "def check(value):\n    \"\"\"Return None when value is absent; zero is a valid result.\"\"\"\n    return None if value is None else value\n", "check(value) is None", "not check(value)", "correctness", "Falsy zero is confused with the documented None sentinel."),
        ("milliseconds", "def check(value):\n    \"\"\"Accept a timeout in milliseconds.\"\"\"\n    return value / 1000\n", "check(value * 1000)", "check(value)", "correctness", "The caller now passes seconds where the unchanged helper expects milliseconds."),
        ("inclusive-bound", "def check(value):\n    \"\"\"Return True for values in the inclusive range 0..100.\"\"\"\n    return 0 <= value <= 100\n", "check(value)", "check(value - 1)", "correctness", "Subtracting one violates the helper's inclusive-bound contract."),
        ("tuple-order", "def check(value):\n    \"\"\"Return (payload, error); error is None on success.\"\"\"\n    return (value, None)\n", "check(value)[0]", "check(value)[1]", "correctness", "The new code returns the error slot instead of the payload slot."),
        ("mutates-input", "def check(value):\n    \"\"\"Sort value in place and return None.\"\"\"\n    value.sort()\n", "(check(value), value)[1]", "check(value)", "correctness", "The helper mutates in place and returns None, which is now returned to callers."),
        ("bytes-output", "def check(value):\n    \"\"\"Encode text and return bytes.\"\"\"\n    return value.encode('utf-8')\n", "check(value).decode('utf-8')", "check(value) + '!'", "correctness", "The unchanged helper returns bytes, so concatenating a string raises TypeError."),
        ("raises-missing", "def check(value):\n    \"\"\"Raise KeyError when value is absent.\"\"\"\n    if value is None:\n        raise KeyError('missing')\n    return value\n", "check(value) if value is not None else 'default'", "check(value)", "edge_case", "The removed guard exposes the helper's documented missing-value exception."),
        ("empty-rejected", "def check(value):\n    \"\"\"Require a non-empty sequence.\"\"\"\n    if not value:\n        raise ValueError('empty')\n    return value[0]\n", "check(value) if value else None", "check(value)", "edge_case", "The caller no longer handles the helper's empty-input precondition."),
        ("one-based", "def check(value):\n    \"\"\"Accept a one-based item number.\"\"\"\n    if value < 1:\n        raise ValueError('one based')\n    return value - 1\n", "check(value + 1)", "check(value)", "correctness", "A zero-based caller value is passed to a one-based helper."),
        ("negative-invalid", "def check(value):\n    \"\"\"Reject negative amounts.\"\"\"\n    if value < 0:\n        raise ValueError('negative')\n    return value\n", "check(max(value, 0))", "check(value)", "edge_case", "Negative values now reach a helper that explicitly rejects them."),
        # security contracts
        ("raw-html", "def check(value):\n    \"\"\"Return HTML-escaped untrusted text.\"\"\"\n    import html\n    return html.escape(value)\n", "check(value)", "(check(value), value)[1]", "security", "The caller discards the unchanged escaping helper's result before rendering text."),
        ("path-containment", "def check(value):\n    \"\"\"Resolve a relative name and reject paths outside /srv/data.\"\"\"\n    from pathlib import Path\n    root = Path('/srv/data').resolve()\n    target = (root / value).resolve()\n    if root not in target.parents:\n        raise ValueError('escape')\n    return target\n", "check(value)", "(check(value), '/srv/data/' + value)[1]", "security", "The caller discards the containment helper's safe path, allowing traversal through an untrusted name."),
        ("auth-result", "def check(value):\n    \"\"\"Return True only when the token is authenticated.\"\"\"\n    return bool(value and value.startswith('signed:'))\n", "check(value)", "(check(value), value is not None)[1]", "security", "Presence is substituted for the unchanged authentication result."),
        ("sql-identifier", "def check(value):\n    \"\"\"Allow only known SQL column identifiers.\"\"\"\n    if value not in {'name', 'created_at'}:\n        raise ValueError('column')\n    return value\n", "check(value)", "(check(value), value)[1]", "security", "The validated SQL identifier is discarded in favor of the raw value."),
        ("redaction", "def check(value):\n    \"\"\"Redact all but the final four characters of a secret.\"\"\"\n    return '*' * max(0, len(value) - 4) + value[-4:]\n", "check(value)", "(check(value), value)[1]", "security", "The caller discards the helper's redacted result and exposes the raw secret."),
        ("signature-bool", "def check(value):\n    \"\"\"Verify a signed token and return a boolean.\"\"\"\n    return value == 'signed:ok'\n", "check(value)", "(check(value), bool(value))[1]", "security", "Any non-empty token is accepted after discarding the signature verification result."),
        # async/performance contracts
        ("async-required", "async def check(value):\n    \"\"\"Asynchronously validate and return a boolean.\"\"\"\n    return value > 0\n", "await check(value)", "check(value)", "correctness", "The coroutine is returned without awaiting it."),
        ("blocking-helper", "def check(value):\n    \"\"\"Blocking network operation; callers in async code must offload it.\"\"\"\n    return value.read()\n", "await asyncio.to_thread(check, value)", "check(value)", "performance", "A documented blocking helper is called directly on the event loop."),
        ("iterator-once", "def check(value):\n    \"\"\"Return a single-pass iterator over matching records.\"\"\"\n    return (x for x in value if x)\n", "list(check(value))", "len(check(value))", "correctness", "The helper returns an iterator, which has no length."),
        ("lock-required", "def check(value):\n    \"\"\"Mutate shared state; caller must hold STATE_LOCK.\"\"\"\n    value['count'] += 1\n    return value['count']\n", "check(value)  # called while STATE_LOCK is held", "check(value)  # lock no longer held", "concurrency", "The unchanged helper requires the caller to hold STATE_LOCK."),
        ("transaction-required", "def check(value):\n    \"\"\"Persist value; caller must execute inside an active transaction.\"\"\"\n    value.save()\n", "transaction.run(lambda: check(value))", "check(value)", "correctness", "The persistence helper is now called outside its required transaction."),
        ("consumes-stream", "def check(value):\n    \"\"\"Consume and close the input stream.\"\"\"\n    try:\n        return value.read()\n    finally:\n        value.close()\n", "check(value)", "check(value) + value.read()", "correctness", "The caller reads the stream after the helper has closed it."),
        ("cache-key", "def check(value):\n    \"\"\"Normalize cache keys to lowercase strings.\"\"\"\n    return str(value).lower()\n", "check(value)", "(check(value), str(value))[1]", "correctness", "Discarding normalization creates case-sensitive duplicate cache entries."),
        ("retry-safety", "def check(value):\n    \"\"\"Perform a non-idempotent charge exactly once.\"\"\"\n    return value.charge()\n", "check(value)", "retry(lambda: check(value), attempts=3)", "correctness", "A non-idempotent helper is placed inside a retry loop."),
        # negatives: L3 proves the changed call is valid
        ("negative-none", "def check(value):\n    \"\"\"Return None for absent input.\"\"\"\n    return None if value is None else value\n", "value is None", "check(value) is None", None, "Correct use of the helper's None sentinel."),
        ("negative-ms", "def check(value):\n    \"\"\"Accept timeout in milliseconds.\"\"\"\n    return value / 1000\n", "value", "check(value * 1000)", None, "Correct seconds-to-milliseconds conversion."),
        ("negative-escape", "def check(value):\n    \"\"\"Return escaped HTML safe for rendering.\"\"\"\n    import html\n    return html.escape(value)\n", "value", "check(value)", None, "Untrusted text is correctly escaped through the helper."),
        ("negative-await", "async def check(value):\n    \"\"\"Asynchronously return the validated value.\"\"\"\n    return value\n", "value", "await check(value)", None, "Coroutine is correctly awaited."),
        ("negative-iterator", "def check(value):\n    \"\"\"Return a single-pass iterator.\"\"\"\n    return iter(value)\n", "list(value)", "list(check(value))", None, "Iterator is deliberately materialized once."),
        ("negative-lock", "def check(value):\n    \"\"\"Increment shared state while the caller holds STATE_LOCK.\"\"\"\n    value['count'] += 1\n    return value['count']\n", "value['count']", "check(value)  # STATE_LOCK held by caller", None, "Call respects the helper's locking precondition."),
    ]
    return [
        _case(i, slug, contract, before, after, category=category, note=note)
        for i, (slug, contract, before, after, category, note) in enumerate(raw, 1)
    ]


def validate(samples: list[dict[str, Any]]) -> None:
    assert len(samples) == 30
    assert len({s["id"] for s in samples}) == 30
    assert sum(bool(s["expected"]) for s in samples) == 24
    assert sum(not s["expected"] for s in samples) == 6
    for item in samples:
        changed = next(path for path in item["head_files"] if path.startswith("src/"))
        lines = item["head_files"][changed].splitlines()
        for expected in item["expected"]:
            assert expected["path"] == changed
            assert expected["line"] == 4
            assert len(lines) >= expected["line"]


def main() -> None:
    samples = build_samples()
    validate(samples)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        yaml.safe_dump({"name": "resume-l3-context-30", "samples": samples}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(f"wrote {len(samples)} samples to {OUTPUT}")


if __name__ == "__main__":
    main()
