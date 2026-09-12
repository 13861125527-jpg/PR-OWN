"""Generate a frozen 30-sample holdout set with unseen cross-file contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

OUTPUT = Path("reposage/evals/datasets/holdout_cross_file_30.yaml")


def _case(index: int, slug: str, helper: str, before: str, after: str, category: str | None, note: str) -> dict[str, Any]:
    changed = f"app/review_{index:02d}.py"
    related = f"contracts/rule_{index:02d}.py"
    module = f"contracts.rule_{index:02d}"
    base = f"from {module} import apply\n\ndef transform(value):\n    return {before}\n"
    head = f"from {module} import apply\n\ndef transform(value):\n    return {after}\n"
    expected = [] if category is None else [{"category": category, "path": changed, "line": 4, "severity": "medium", "note": note}]
    return {
        "id": f"holdout-{index:02d}-{slug}",
        "kind": "negative" if category is None else "cross_file",
        "pr_title": f"Refine {slug.replace('-', ' ')} handling",
        "pr_description": "Simplify the transformation while preserving externally observable behavior.",
        "base_files": {changed: base, related: helper, "contracts/common.py": "DEFAULT_LIMIT = 100\n"},
        "head_files": {changed: head, related: helper, "contracts/common.py": "DEFAULT_LIMIT = 100\n"},
        "expected": expected,
        "notes": f"frozen_holdout=true; expected_context={related}; audit={note}",
    }


def build_samples() -> list[dict[str, Any]]:
    rows = [
        ("decimal-money", "from decimal import Decimal\ndef apply(value):\n    return Decimal(value).quantize(Decimal('0.01'))\n", "str(apply(value))", "str(float(apply(value)))", "correctness", "Converting the normalized Decimal through float can change exact monetary output."),
        ("safe-path", "from pathlib import Path\ndef apply(value):\n    root = Path('/srv/files').resolve()\n    target = (root / value).resolve()\n    if root not in target.parents:\n        raise ValueError('outside root')\n    return target\n", "apply(value)", "(apply(value), '/srv/files/' + value)[1]", "security", "The validated resolved path is discarded and traversal text is returned."),
        ("enum-wire-value", "from enum import Enum\nclass State(Enum):\n    OPEN = 'open'\n    CLOSED = 'closed'\ndef apply(value):\n    return State(value)\n", "apply(value).value", "apply(value)", "correctness", "The public string result changes to an Enum instance."),
        ("timezone-aware", "from datetime import datetime, timezone\ndef apply(value):\n    return datetime.fromtimestamp(value, tz=timezone.utc)\n", "apply(value)", "apply(value).replace(tzinfo=None)", "correctness", "The result silently loses UTC timezone awareness."),
        ("permission-check", "def apply(value):\n    return 'write' in value.permissions\n", "apply(value)", "bool(value.permissions)", "security", "Any permission now authorizes an operation that requires write access."),
        ("byte-order", "def apply(value):\n    return value.to_bytes(4, byteorder='little', signed=False)\n", "int.from_bytes(apply(value), 'little')", "int.from_bytes(apply(value), 'big')", "correctness", "The unchanged little-endian payload is decoded as big-endian."),
        ("digest-format", "import hashlib\ndef apply(value):\n    return hashlib.sha256(value)\n", "apply(value).hexdigest()", "apply(value).digest()", "correctness", "The public hexadecimal digest changes to raw bytes."),
        ("account-pattern", "import re\ndef apply(value):\n    return re.fullmatch(r'[a-z][a-z0-9_]{2,15}', value) is not None\n", "apply(value)", "bool(value)", "security", "Non-empty invalid account names bypass full validation."),
        ("opaque-cursor", "def apply(value):\n    return f'cursor:{value}:next'\n", "apply(value)", "int(apply(value)) + 1", "correctness", "An opaque cursor is parsed as an integer and raises ValueError."),
        ("retry-delay", "def apply(value):\n    return max(0, int(value.headers['Retry-After']))\n", "apply(value)", "apply(value) * 1000", "correctness", "A seconds delay is exposed as milliseconds without migrating callers."),
        ("deep-copy", "import copy\ndef apply(value):\n    return copy.deepcopy(value)\n", "apply(value)", "(apply(value), value)[1]", "correctness", "The defensive copy is discarded and mutable caller state leaks out."),
        ("unicode-normalization", "import unicodedata\ndef apply(value):\n    return unicodedata.normalize('NFC', value)\n", "apply(value)", "(apply(value), value)[1]", "correctness", "The normalized identifier is discarded, allowing canonically equivalent duplicates."),
        ("constant-time-token", "import hmac\ndef apply(value):\n    supplied, expected = value\n    return hmac.compare_digest(supplied, expected)\n", "apply(value)", "value[0] == value[1]", "security", "The constant-time secret comparison is replaced by ordinary equality."),
        ("bounded-limit", "def apply(value):\n    return min(max(int(value), 1), 100)\n", "apply(value)", "int(value)", "edge_case", "Out-of-range limits are no longer bounded before downstream use."),
        ("canonical-host", "from urllib.parse import urlsplit\ndef apply(value):\n    host = urlsplit(value).hostname\n    return host.lower() if host else None\n", "apply(value)", "value.split('/')[2]", "security", "Manual URL splitting preserves user-info and port text instead of the canonical hostname."),
        ("tenant-scope", "def apply(value):\n    query, tenant_id = value\n    return query.where(tenant_id=tenant_id)\n", "apply(value).all()", "value[0].all()", "security", "The tenant predicate is removed from the executed query."),
        ("cache-namespace", "def apply(value):\n    tenant, key = value\n    return f'{tenant}:{key}'.lower()\n", "apply(value)", "value[1].lower()", "correctness", "Removing the tenant namespace causes cross-tenant cache collisions."),
        ("optional-presence", "def apply(value):\n    return value.HasField('count')\n", "apply(value)", "value.count != 0", "correctness", "A present zero value is confused with an absent optional field."),
        ("stream-position", "def apply(value):\n    value.seek(0)\n    return value\n", "apply(value).read()", "value.read()", "correctness", "The stream is read from its current position instead of rewinding first."),
        ("compressed-bytes", "import gzip\ndef apply(value):\n    return gzip.compress(value.encode('utf-8'))\n", "apply(value)", "apply(value).decode('utf-8')", "correctness", "Arbitrary gzip bytes are decoded as UTF-8 and can raise UnicodeDecodeError."),
        ("idempotency-key", "import hashlib\ndef apply(value):\n    return hashlib.sha256(value.encode()).hexdigest()\n", "send(value, idempotency_key=apply(value))", "send(value)", "correctness", "Removing the deterministic idempotency key permits duplicate side effects on retries."),
        ("csrf-binding", "def apply(value):\n    token, session_token = value\n    return token == session_token and bool(token)\n", "apply(value)", "bool(value[0])", "security", "Any non-empty CSRF token is accepted without binding it to the session."),
        ("sequence-window", "def apply(value):\n    start, end = value\n    if end < start:\n        raise ValueError('reversed')\n    return range(start, end + 1)\n", "list(apply(value))", "list(range(value[0], value[1]))", "correctness", "The inclusive end becomes exclusive and reversed ranges stop raising."),
        ("resource-owner", "def apply(value):\n    resource, actor = value\n    return resource.owner_id == actor.id\n", "apply(value)", "value[0].owner_id is not None", "security", "Ownership authorization is replaced with a non-null owner check."),
        ("negative-decimal", "from decimal import Decimal\ndef apply(value):\n    return Decimal(value).quantize(Decimal('0.01'))\n", "str(value)", "str(apply(value))", None, "The helper correctly introduces deterministic money normalization."),
        ("negative-path", "from pathlib import Path\ndef apply(value):\n    root = Path('/srv/files').resolve()\n    target = (root / value).resolve()\n    if root not in target.parents:\n        raise ValueError('outside root')\n    return target\n", "'/srv/files/' + value", "apply(value)", None, "The changed code correctly adopts containment validation."),
        ("negative-permission", "def apply(value):\n    return 'write' in value.permissions\n", "bool(value.permissions)", "apply(value)", None, "The change correctly checks the required permission."),
        ("negative-tenant", "def apply(value):\n    query, tenant_id = value\n    return query.where(tenant_id=tenant_id)\n", "value[0].all()", "apply(value).all()", None, "The query is correctly scoped to the tenant."),
        ("negative-optional", "def apply(value):\n    return value.HasField('count')\n", "value.count != 0", "apply(value)", None, "Presence is correctly distinguished from a present zero."),
        ("negative-token", "import hmac\ndef apply(value):\n    supplied, expected = value\n    return hmac.compare_digest(supplied, expected)\n", "value[0] == value[1]", "apply(value)", None, "The change correctly adopts constant-time secret comparison."),
    ]
    return [_case(i, *row) for i, row in enumerate(rows, 1)]


def main() -> None:
    samples = build_samples()
    assert len(samples) == 30
    assert sum(bool(sample["expected"]) for sample in samples) == 24
    assert len({sample["id"] for sample in samples}) == 30
    OUTPUT.write_text(yaml.safe_dump({"name": "holdout-cross-file-30", "samples": samples}, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"wrote {len(samples)} frozen samples to {OUTPUT}")


if __name__ == "__main__":
    main()
