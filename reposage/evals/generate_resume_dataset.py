"""Generate the synthetic V1/V2/V3 resume evaluation dataset.

The cases are intentionally small enough to audit by hand, while still including
repository context that fixed-pipeline and agentic strategies can use differently.
This generator is deterministic; edit the case definitions, not the generated YAML.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

import yaml

OUTPUT = Path("reposage/evals/datasets/resume_v123_40.yaml")
VALID_CATEGORIES = {
    "correctness",
    "security",
    "silent_failure",
    "concurrency",
    "edge_case",
    "test_gap",
    "performance",
    "maintainability",
}


def finding(category: str, path: str, line: int, severity: str, note: str) -> dict[str, Any]:
    return {"category": category, "path": path, "line": line, "severity": severity, "note": note}


def sample(
    id_: str,
    kind: str,
    title: str,
    base: dict[str, str],
    head: dict[str, str],
    expected: list[dict[str, Any]],
    notes: str,
) -> dict[str, Any]:
    return {
        "id": id_,
        "kind": kind,
        "pr_title": title,
        "pr_description": "",
        "base_files": base,
        "head_files": head,
        "expected": expected,
        "notes": notes,
    }


def build_samples() -> list[dict[str, Any]]:
    s: list[dict[str, Any]] = []
    add = s.append

    add(
        sample(
            "r01-sql-injection",
            "single_defect",
            "support user lookup",
            {
                "src/users.py": "def find(cur, name):\n    return cur.execute('SELECT * FROM users WHERE name=?', (name,))\n"
            },
            {
                "src/users.py": "def find(cur, name):\n    return cur.execute(f\"SELECT * FROM users WHERE name='{name}'\")\n"
            },
            [
                finding(
                    "security",
                    "src/users.py",
                    2,
                    "high",
                    "User-controlled name is interpolated into SQL; verify parameterization restores safety.",
                )
            ],
            "single-file; explicit attacker-controlled input; audit_priority=high",
        )
    )
    add(
        sample(
            "r02-path-traversal",
            "single_defect",
            "add report download",
            {
                "src/downloads.py": "from pathlib import Path\nROOT = Path('/srv/reports')\ndef load(name):\n    return (ROOT / 'default.pdf').read_bytes()\n"
            },
            {
                "src/downloads.py": "from pathlib import Path\nROOT = Path('/srv/reports')\ndef load(name):\n    return (ROOT / name).read_bytes()\n"
            },
            [
                finding(
                    "security",
                    "src/downloads.py",
                    4,
                    "high",
                    "Untrusted name can escape ROOT through ../; confirm containment after resolution.",
                )
            ],
            "single-file; path containment",
        )
    )
    add(
        sample(
            "r03-shell-injection",
            "single_defect",
            "add archive command",
            {
                "src/archive.py": "import subprocess\ndef pack(path):\n    return subprocess.run(['tar', '-czf', 'out.tgz', path], check=True)\n"
            },
            {
                "src/archive.py": "import subprocess\ndef pack(path):\n    return subprocess.run(f'tar -czf out.tgz {path}', shell=True, check=True)\n"
            },
            [
                finding(
                    "security",
                    "src/archive.py",
                    3,
                    "critical",
                    "Shell execution interpolates an untrusted path.",
                )
            ],
            "single-file; command injection",
        )
    )
    add(
        sample(
            "r04-unsafe-yaml",
            "single_defect",
            "accept yaml settings",
            {"src/settings.py": "import yaml\ndef parse(raw):\n    return yaml.safe_load(raw)\n"},
            {
                "src/settings.py": "import yaml\ndef parse(raw):\n    return yaml.load(raw, Loader=yaml.Loader)\n"
            },
            [
                finding(
                    "security",
                    "src/settings.py",
                    3,
                    "high",
                    "General YAML loader may construct arbitrary Python objects from untrusted input.",
                )
            ],
            "single-file; unsafe deserialization",
        )
    )
    add(
        sample(
            "r05-weak-token",
            "single_defect",
            "generate invitation token",
            {
                "src/tokens.py": "import secrets\ndef issue():\n    return secrets.token_urlsafe(24)\n"
            },
            {
                "src/tokens.py": "import random\ndef issue():\n    return str(random.randint(100000, 999999))\n"
            },
            [
                finding(
                    "security",
                    "src/tokens.py",
                    3,
                    "high",
                    "random and a six-digit space are unsuitable for security tokens.",
                )
            ],
            "single-file; predictable credential",
        )
    )
    add(
        sample(
            "r06-timing-compare",
            "single_defect",
            "simplify signature check",
            {
                "src/signing.py": "import hmac\ndef valid(got, expected):\n    return hmac.compare_digest(got, expected)\n"
            },
            {"src/signing.py": "def valid(got, expected):\n    return got == expected\n"},
            [
                finding(
                    "security",
                    "src/signing.py",
                    2,
                    "medium",
                    "Direct equality can leak signature prefix timing.",
                )
            ],
            "single-file; subtle security regression",
        )
    )
    add(
        sample(
            "r07-open-redirect",
            "single_defect",
            "support return url",
            {"src/web.py": "def after_login(request):\n    return redirect('/dashboard')\n"},
            {
                "src/web.py": "def after_login(request):\n    return redirect(request.args.get('next', '/dashboard'))\n"
            },
            [
                finding(
                    "security",
                    "src/web.py",
                    2,
                    "medium",
                    "Unvalidated external next URL enables open redirects.",
                )
            ],
            "single-file; framework-style input",
        )
    )
    add(
        sample(
            "r08-log-secret",
            "single_defect",
            "add request diagnostics",
            {"src/client.py": "def send(headers, log):\n    log.info('sending request')\n"},
            {
                "src/client.py": "def send(headers, log):\n    log.info('sending request headers=%s', headers)\n"
            },
            [
                finding(
                    "security",
                    "src/client.py",
                    2,
                    "medium",
                    "Logging all headers can expose Authorization and cookie credentials.",
                )
            ],
            "single-file; secret exposure",
        )
    )

    add(
        sample(
            "r09-zero-page-size",
            "single_defect",
            "add page count",
            {
                "src/paging.py": "def pages(total, size):\n    return 0 if size <= 0 else (total + size - 1) // size\n"
            },
            {"src/paging.py": "def pages(total, size):\n    return (total + size - 1) // size\n"},
            [
                finding(
                    "edge_case",
                    "src/paging.py",
                    2,
                    "medium",
                    "size=0 now raises ZeroDivisionError.",
                )
            ],
            "single-file; boundary input",
        )
    )
    add(
        sample(
            "r10-empty-average",
            "single_defect",
            "add latency average",
            {
                "src/stats.py": "def average(values):\n    return sum(values) / len(values) if values else 0.0\n"
            },
            {"src/stats.py": "def average(values):\n    return sum(values) / len(values)\n"},
            [finding("edge_case", "src/stats.py", 2, "medium", "Empty input divides by zero.")],
            "single-file; empty collection",
        )
    )
    add(
        sample(
            "r11-off-by-one",
            "single_defect",
            "iterate all retry slots",
            {"src/retry.py": "def attempts(limit):\n    return list(range(limit))\n"},
            {"src/retry.py": "def attempts(limit):\n    return list(range(limit + 1))\n"},
            [
                finding(
                    "correctness",
                    "src/retry.py",
                    2,
                    "medium",
                    "Runs limit+1 attempts although limit is the maximum count.",
                )
            ],
            "single-file; off-by-one",
        )
    )
    add(
        sample(
            "r12-wrong-sort",
            "single_defect",
            "rank newest events",
            {
                "src/events.py": "def newest(events):\n    return sorted(events, key=lambda e: e.created_at, reverse=True)\n"
            },
            {
                "src/events.py": "def newest(events):\n    return sorted(events, key=lambda e: e.created_at)\n"
            },
            [
                finding(
                    "correctness",
                    "src/events.py",
                    2,
                    "medium",
                    "Ascending order contradicts newest-first behavior.",
                )
            ],
            "single-file; semantic regression",
        )
    )
    add(
        sample(
            "r13-mutable-default",
            "single_defect",
            "collect validation errors",
            {
                "src/validation.py": "def collect(item, errors=None):\n    errors = [] if errors is None else errors\n    errors.append(item)\n    return errors\n"
            },
            {
                "src/validation.py": "def collect(item, errors=[]):\n    errors.append(item)\n    return errors\n"
            },
            [
                finding(
                    "correctness",
                    "src/validation.py",
                    1,
                    "medium",
                    "Default list is shared across calls and leaks prior results.",
                )
            ],
            "single-file; Python state lifetime",
        )
    )
    add(
        sample(
            "r14-finally-return",
            "single_defect",
            "ensure cleanup result",
            {
                "src/work.py": "def run(job):\n    try:\n        return job.execute()\n    finally:\n        job.close()\n"
            },
            {
                "src/work.py": "def run(job):\n    try:\n        return job.execute()\n    finally:\n        job.close()\n        return None\n"
            },
            [
                finding(
                    "silent_failure",
                    "src/work.py",
                    6,
                    "high",
                    "Return in finally suppresses both the real result and exceptions.",
                )
            ],
            "single-file; exception suppression",
        )
    )
    add(
        sample(
            "r15-swallow-timeout",
            "single_defect",
            "handle optional cache",
            {"src/cache.py": "def fetch(client, key):\n    return client.get(key)\n"},
            {
                "src/cache.py": "def fetch(client, key):\n    try:\n        return client.get(key)\n    except Exception:\n        return None\n"
            },
            [
                finding(
                    "silent_failure",
                    "src/cache.py",
                    4,
                    "medium",
                    "Catching every exception hides connectivity and programming failures as cache misses.",
                )
            ],
            "single-file; broad exception",
        )
    )
    add(
        sample(
            "r16-file-leak",
            "single_defect",
            "stream first line",
            {
                "src/files.py": "def first(path):\n    with open(path, encoding='utf-8') as fh:\n        return fh.readline()\n"
            },
            {
                "src/files.py": "def first(path):\n    fh = open(path, encoding='utf-8')\n    return fh.readline()\n"
            },
            [
                finding(
                    "correctness",
                    "src/files.py",
                    2,
                    "medium",
                    "Opened file is never closed on the new path.",
                )
            ],
            "single-file; resource lifecycle",
        )
    )
    add(
        sample(
            "r17-naive-quadratic",
            "single_defect",
            "deduplicate ids",
            {"src/ids.py": "def unique(values):\n    return list(dict.fromkeys(values))\n"},
            {
                "src/ids.py": "def unique(values):\n    out = []\n    for value in values:\n        if value not in out:\n            out.append(value)\n    return out\n"
            },
            [
                finding(
                    "performance",
                    "src/ids.py",
                    4,
                    "medium",
                    "List membership makes deduplication quadratic for large inputs.",
                )
            ],
            "single-file; scalable input",
        )
    )
    add(
        sample(
            "r18-blocking-async",
            "single_defect",
            "pace async worker",
            {"src/worker.py": "import asyncio\nasync def poll():\n    await asyncio.sleep(1)\n"},
            {"src/worker.py": "import time\nasync def poll():\n    time.sleep(1)\n"},
            [
                finding(
                    "performance",
                    "src/worker.py",
                    3,
                    "high",
                    "time.sleep blocks the event loop and stalls unrelated tasks.",
                )
            ],
            "single-file; async performance",
        )
    )

    add(
        sample(
            "r19-two-defects",
            "multi_defect",
            "extend account import",
            {
                "src/importer.py": "def load(cur, email, rows):\n    return cur.execute('SELECT id FROM users WHERE email=?', (email,))\n"
            },
            {
                "src/importer.py": "def load(cur, email, rows):\n    user = cur.execute(f\"SELECT id FROM users WHERE email='{email}'\")\n    average = sum(rows) / len(rows)\n    return user, average\n"
            },
            [
                finding(
                    "security", "src/importer.py", 2, "high", "Email is interpolated into SQL."
                ),
                finding("edge_case", "src/importer.py", 3, "medium", "Empty rows divides by zero."),
            ],
            "multi-defect; distinct categories",
        )
    )
    add(
        sample(
            "r20-two-defects",
            "multi_defect",
            "add batch export",
            {
                "src/export.py": "def export(items, sink):\n    for item in items:\n        sink.write(item)\n"
            },
            {
                "src/export.py": "def export(items, sink):\n    try:\n        for i in range(len(items) + 1):\n            sink.write(items[i])\n    except Exception:\n        pass\n"
            },
            [
                finding(
                    "correctness",
                    "src/export.py",
                    3,
                    "medium",
                    "Loop indexes one past the final item.",
                ),
                finding(
                    "silent_failure",
                    "src/export.py",
                    5,
                    "high",
                    "Broad catch silently returns a partial export.",
                ),
            ],
            "multi-defect; interaction between fault and masking",
        )
    )
    add(
        sample(
            "r21-lock-and-await",
            "multi_defect",
            "refresh shared cache",
            {
                "src/shared.py": "async def refresh(lock, client, cache):\n    data = await client.fetch()\n    async with lock:\n        cache.update(data)\n"
            },
            {
                "src/shared.py": "async def refresh(lock, client, cache):\n    async with lock:\n        data = await client.fetch()\n        cache.clear()\n        cache.update(data)\n"
            },
            [
                finding(
                    "concurrency",
                    "src/shared.py",
                    3,
                    "high",
                    "Network await while holding a shared lock can stall all users.",
                ),
                finding(
                    "correctness",
                    "src/shared.py",
                    4,
                    "medium",
                    "Clearing before update can drop valid cached keys absent from a partial response.",
                ),
            ],
            "multi-defect; concurrency and state semantics",
        )
    )
    add(
        sample(
            "r22-security-and-leak",
            "multi_defect",
            "run uploaded migration",
            {
                "src/migrate.py": "def run(path, log):\n    log.info('migration requested')\n    return path\n"
            },
            {
                "src/migrate.py": "import subprocess\ndef run(path, log, token):\n    log.info('token=%s', token)\n    return subprocess.run(f'python {path}', shell=True)\n"
            },
            [
                finding(
                    "security",
                    "src/migrate.py",
                    3,
                    "high",
                    "Authentication token is written to logs.",
                ),
                finding(
                    "security",
                    "src/migrate.py",
                    4,
                    "critical",
                    "Uploaded path is interpolated into a shell command.",
                ),
            ],
            "multi-defect; two independent security findings",
        )
    )

    add(
        sample(
            "r23-contract-none",
            "cross_file",
            "use repository lookup",
            {
                "src/service.py": "from src.repo import find_user\ndef greet(uid):\n    user = find_user(uid)\n    return user.name\n",
                "src/repo.py": "def find_user(uid):\n    return DB.get(uid)\n",
            },
            {
                "src/service.py": "from src.repo import find_user\ndef greet(uid):\n    user = find_user(uid)\n    return user.name.upper()\n",
                "src/repo.py": "def find_user(uid):\n    return DB.get(uid)\n",
            },
            [
                finding(
                    "correctness",
                    "src/service.py",
                    4,
                    "high",
                    "Repository contract permits None, so missing users raise AttributeError.",
                )
            ],
            "cross-file; evidence_path=src/repo.py; requires return-contract reasoning",
        )
    )
    add(
        sample(
            "r24-unit-mismatch",
            "cross_file",
            "show cache age",
            {
                "src/view.py": "from src.clock import age_seconds\ndef label(ts):\n    return f'{age_seconds(ts)}s'\n",
                "src/clock.py": "import time\ndef age_seconds(ts_ms):\n    return int(time.time() - ts_ms / 1000)\n",
            },
            {
                "src/view.py": "from src.clock import age_seconds\ndef label(ts):\n    return f'{age_seconds(ts * 1000)}s'\n",
                "src/clock.py": "import time\ndef age_seconds(ts_ms):\n    return int(time.time() - ts_ms / 1000)\n",
            },
            [
                finding(
                    "correctness",
                    "src/view.py",
                    3,
                    "high",
                    "Caller multiplies a timestamp that is already in milliseconds.",
                )
            ],
            "cross-file; evidence_path=src/clock.py; unit contract",
        )
    )
    add(
        sample(
            "r25-auth-default",
            "cross_file",
            "add admin endpoint",
            {
                "src/routes.py": "from src.auth import allowed\ndef remove(user, item):\n    if not allowed(user, 'delete'):\n        raise PermissionError\n    return item.delete()\n",
                "src/auth.py": "def allowed(user, action):\n    return action in user.permissions\n",
            },
            {
                "src/routes.py": "from src.auth import allowed\ndef remove(user, item):\n    if allowed(user, 'delete'):\n        return item.delete()\n    return item\n",
                "src/auth.py": "def allowed(user, action):\n    return action not in user.denied_permissions\n",
            },
            [
                finding(
                    "security",
                    "src/routes.py",
                    3,
                    "critical",
                    "New deny-list helper makes unspecified delete permission allowed by default.",
                )
            ],
            "cross-file; evidence_path=src/auth.py; authorization contract",
        )
    )
    add(
        sample(
            "r26-sentinel-conflict",
            "cross_file",
            "cache profile results",
            {
                "src/profile.py": "from src.cache import MISS\ndef get(uid, cache):\n    value = cache.get(uid, MISS)\n    return load(uid) if value is MISS else value\n",
                "src/cache.py": "MISS = object()\n",
            },
            {
                "src/profile.py": "from src.cache import MISS\ndef get(uid, cache):\n    value = cache.get(uid)\n    return load(uid) if value is MISS else value\n",
                "src/cache.py": "MISS = object()\n",
            },
            [
                finding(
                    "correctness",
                    "src/profile.py",
                    3,
                    "high",
                    "dict.get now returns None, which is never identical to the MISS sentinel.",
                )
            ],
            "cross-file; evidence_path=src/cache.py; sentinel semantics",
        )
    )
    add(
        sample(
            "r27-config-string-bool",
            "cross_file",
            "honor dry run setting",
            {
                "src/deploy.py": "from src.config import DRY_RUN\ndef deploy(client):\n    if DRY_RUN:\n        return 'skipped'\n    return client.push()\n",
                "src/config.py": "DRY_RUN = False\n",
            },
            {
                "src/deploy.py": "from src.config import DRY_RUN\ndef deploy(client):\n    if DRY_RUN:\n        return 'skipped'\n    return client.push()\n",
                "src/config.py": "import os\nDRY_RUN = os.getenv('DRY_RUN', 'false')\n",
            },
            [
                finding(
                    "correctness",
                    "src/config.py",
                    2,
                    "high",
                    "Non-empty string 'false' is truthy, so deployments are always skipped by default.",
                )
            ],
            "cross-file; evidence_path=src/deploy.py; configuration consumer establishes impact",
        )
    )
    add(
        sample(
            "r28-key-normalization",
            "cross_file",
            "normalize session keys",
            {
                "src/session.py": "from src.keys import key\ndef save(store, uid, value):\n    store[key(uid)] = value\n",
                "src/keys.py": "def key(uid):\n    return str(uid)\n",
            },
            {
                "src/session.py": "from src.keys import key\ndef save(store, uid, value):\n    store[key(uid)] = value\n",
                "src/keys.py": "def key(uid):\n    return str(uid).lower().strip()\n",
            },
            [
                finding(
                    "correctness",
                    "src/keys.py",
                    2,
                    "medium",
                    "Lowercasing case-sensitive user IDs aliases distinct sessions.",
                )
            ],
            "cross-file; evidence_path=src/session.py; identity contract",
        )
    )
    add(
        sample(
            "r29-lock-copy",
            "cross_file",
            "return account snapshot",
            {
                "src/accounts.py": "from src.state import STATE, LOCK\ndef snapshot():\n    with LOCK:\n        return dict(STATE)\n",
                "src/state.py": "from threading import Lock\nSTATE = {}\nLOCK = Lock()\n",
            },
            {
                "src/accounts.py": "from src.state import STATE\ndef snapshot():\n    return dict(STATE)\n",
                "src/state.py": "from threading import Lock\nSTATE = {}\nLOCK = Lock()\n",
            },
            [
                finding(
                    "concurrency",
                    "src/accounts.py",
                    3,
                    "high",
                    "Snapshot copy can race with concurrent mutation after the shared lock was removed.",
                )
            ],
            "cross-file; evidence_path=src/state.py; shared synchronization contract",
        )
    )
    add(
        sample(
            "r30-retry-non-idempotent",
            "cross_file",
            "retry payment request",
            {
                "src/checkout.py": "from src.retry import retry\ndef charge(gateway, order):\n    return gateway.charge(order.id, order.total)\n",
                "src/retry.py": "def retry(fn):\n    return fn()\n",
            },
            {
                "src/checkout.py": "from src.retry import retry\ndef charge(gateway, order):\n    return retry(lambda: gateway.charge(order.id, order.total))\n",
                "src/retry.py": "def retry(fn):\n    try:\n        return fn()\n    except TimeoutError:\n        return fn()\n",
            },
            [
                finding(
                    "correctness",
                    "src/checkout.py",
                    3,
                    "critical",
                    "Retrying an unkeyed charge after timeout can double-charge when the first request succeeded remotely.",
                )
            ],
            "cross-file; evidence_path=src/retry.py; non-idempotent side effect",
        )
    )

    negatives = [
        (
            "r31-negative-rename",
            "rename accumulator",
            {
                "src/math.py": "def total(xs):\n    value = 0\n    for x in xs:\n        value += x\n    return value\n"
            },
            {
                "src/math.py": "def total(xs):\n    result = 0\n    for x in xs:\n        result += x\n    return result\n"
            },
            "behavior-preserving local rename",
        ),
        (
            "r32-negative-context-manager",
            "close files reliably",
            {
                "src/read.py": "def read(path):\n    fh = open(path, encoding='utf-8')\n    try:\n        return fh.read()\n    finally:\n        fh.close()\n"
            },
            {
                "src/read.py": "def read(path):\n    with open(path, encoding='utf-8') as fh:\n        return fh.read()\n"
            },
            "safe resource-management refactor",
        ),
        (
            "r33-negative-parameterize",
            "parameterize lookup",
            {
                "src/db.py": "def get(cur, uid):\n    return cur.execute('SELECT * FROM users WHERE id=' + str(int(uid)))\n"
            },
            {
                "src/db.py": "def get(cur, uid):\n    return cur.execute('SELECT * FROM users WHERE id=?', (uid,))\n"
            },
            "security improvement; should not trigger stale-pattern false positive",
        ),
        (
            "r34-negative-empty-guard",
            "handle empty input",
            {"src/mean.py": "def mean(xs):\n    return sum(xs) / len(xs)\n"},
            {"src/mean.py": "def mean(xs):\n    return sum(xs) / len(xs) if xs else 0.0\n"},
            "correct boundary guard",
        ),
        (
            "r35-negative-async-sleep",
            "avoid blocking worker",
            {"src/poll.py": "import time\nasync def poll():\n    time.sleep(1)\n"},
            {"src/poll.py": "import asyncio\nasync def poll():\n    await asyncio.sleep(1)\n"},
            "async correctness improvement",
        ),
        (
            "r36-negative-doc-string",
            "document dangerous examples",
            {"src/docs.py": "GUIDE = 'examples'\n"},
            {
                "src/docs.py": "GUIDE = '''Never run eval(user_input) or subprocess with shell=True.'''\n"
            },
            "dangerous tokens occur only in documentation",
        ),
        (
            "r37-negative-test-fixture",
            "add fake credential fixture",
            {"tests/test_auth.py": "def test_login(client):\n    assert client.login('u', 'p')\n"},
            {
                "tests/test_auth.py": "FAKE_TOKEN = 'test-token-not-a-secret'\ndef test_login(client):\n    assert client.login('u', 'p', token=FAKE_TOKEN)\n"
            },
            "clearly fake test credential",
        ),
        (
            "r38-negative-lock",
            "synchronize shared counter",
            {"src/count.py": "COUNT = 0\ndef inc():\n    global COUNT\n    COUNT += 1\n"},
            {
                "src/count.py": "from threading import Lock\nCOUNT = 0\nLOCK = Lock()\ndef inc():\n    global COUNT\n    with LOCK:\n        COUNT += 1\n"
            },
            "adds synchronization",
        ),
        (
            "r39-negative-cache",
            "memoize pure formatter",
            {"src/format.py": "def label(code):\n    return code.strip().upper()\n"},
            {
                "src/format.py": "from functools import lru_cache\n@lru_cache(maxsize=128)\ndef label(code):\n    return code.strip().upper()\n"
            },
            "bounded cache on pure string function",
        ),
        (
            "r40-negative-cross-file",
            "rename shared helper",
            {
                "src/app.py": "from src.util import normalize\ndef run(v):\n    return normalize(v)\n",
                "src/util.py": "def normalize(v):\n    return v.strip()\n",
            },
            {
                "src/app.py": "from src.util import clean\ndef run(v):\n    return clean(v)\n",
                "src/util.py": "def clean(v):\n    return v.strip()\n",
            },
            "consistent cross-file rename",
        ),
    ]
    for id_, title, base, head, note in negatives:
        add(sample(id_, "negative", title, base, head, [], f"negative; {note}"))
    return s


def validate(samples: list[dict[str, Any]]) -> None:
    assert len(samples) == 40
    ids = [item["id"] for item in samples]
    assert len(ids) == len(set(ids))
    assert sum(not item["expected"] for item in samples) == 10
    assert sum(item["kind"] == "cross_file" for item in samples) == 8
    for item in samples:
        assert item["base_files"] != item["head_files"], item["id"]
        for expected in item["expected"]:
            assert expected["category"] in VALID_CATEGORIES
            content = item["head_files"].get(expected["path"])
            assert content is not None, (item["id"], expected["path"])
            lines = content.splitlines()
            assert 1 <= expected["line"] <= len(lines), (item["id"], expected["line"])
            base_lines = item["base_files"].get(expected["path"], "").splitlines()
            added: set[int] = set()
            for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(
                a=base_lines, b=lines, autojunk=False
            ).get_opcodes():
                if tag in {"insert", "replace"}:
                    added.update(range(j1 + 1, j2 + 1))
            assert expected["line"] in added, (
                item["id"],
                expected["path"],
                expected["line"],
                sorted(added),
            )


def main() -> None:
    samples = build_samples()
    validate(samples)
    payload = {
        "name": "resume-v123-synthetic-40",
        "samples": samples,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        "# Synthetic benchmark for an initial V1/V2/V3 comparison.\n"
        "# Gold labels require human review before results are used in a resume.\n"
        "# Generated by reposage/evals/generate_resume_dataset.py.\n"
        + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
    print(f"wrote {len(samples)} samples to {OUTPUT}")


if __name__ == "__main__":
    main()
