"""SQLite 基础存储（storage/sqlite.py）。

Phase 0：连接管理 + schema 初始化 + 最小 CRUD。
概念模型见 05 §6（最终字段名：finding_occurrence_id / fingerprint / cross_run_match_key）。

- 公开方法为 async，内部用 asyncio.to_thread 包装阻塞 IO，避免阻塞 async 事件循环（P0-1）；
  连接以 check_same_thread=False 创建，并以 threading.Lock 串行化并发写入。
- 连接后强制 PRAGMA foreign_keys=ON（P0-2），孤儿引用抛 IntegrityError。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from reposage.config.settings import Settings
from reposage.domain.finding import Finding
from reposage.domain.models import ModelUsage
from reposage.domain.run import ReviewRun

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    external_ref TEXT,
    base_sha TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    strategy TEXT NOT NULL,
    status TEXT NOT NULL,
    publish_status TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    config_hash TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    in_tokens INTEGER NOT NULL DEFAULT 0,
    out_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS usages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    model TEXT NOT NULL,
    role TEXT NOT NULL,
    in_tokens INTEGER NOT NULL DEFAULT 0,
    out_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT 'completed',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    prompt_hash TEXT
);

CREATE TABLE IF NOT EXISTS findings (
    finding_occurrence_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    fingerprint TEXT NOT NULL,
    cross_run_match_key TEXT NOT NULL,
    cluster_id TEXT,
    title TEXT NOT NULL,
    severity TEXT NOT NULL,
    confidence REAL NOT NULL,
    category TEXT NOT NULL,
    claimed_path TEXT,
    claimed_start INTEGER,
    claimed_end INTEGER,
    canonical_path TEXT,
    canonical_start INTEGER,
    canonical_end INTEGER,
    status TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    sources_json TEXT NOT NULL DEFAULT '[]',
    is_outside_diff INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS finding_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_occurrence_id TEXT NOT NULL REFERENCES findings(finding_occurrence_id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL,
    at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS coverages (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    items_json TEXT NOT NULL DEFAULT '[]',
    truncated INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tool_calls (
    tool_call_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    name TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    repeat_of TEXT,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tool_results (
    tool_call_id TEXT PRIMARY KEY REFERENCES tool_calls(tool_call_id),
    data TEXT,
    error TEXT,
    truncated INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS publish_plans (
    plan_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    status TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'dry_run',
    watermark TEXT
);

CREATE TABLE IF NOT EXISTS published_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES publish_plans(plan_id),
    finding_occurrence_id TEXT,
    fingerprint TEXT,
    kind TEXT NOT NULL,
    path TEXT,
    line INTEGER,
    body TEXT NOT NULL,
    marker TEXT NOT NULL DEFAULT '',
    remote_comment_id INTEGER,
    status TEXT NOT NULL DEFAULT 'prepared'
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope TEXT,
    path TEXT,
    symbol TEXT,
    category TEXT,
    rule_key TEXT,
    pattern TEXT,
    cross_run_match_key TEXT,
    rationale TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS pr_caches (
    pr_key TEXT PRIMARY KEY,
    base_sha TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    diff_hash TEXT,
    fetched_at TEXT NOT NULL
);
"""


class SqliteStorage:
    """SQLite 存储（async 接口 + to_thread 包装，满足 Storage 协议）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：连接供 to_thread 线程池使用；并发写由 self._lock 串行化
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.row_factory = sqlite3.Row
        # P0-2：SQLite 默认外键关闭，必须显式开启并断言
        self._conn.execute("PRAGMA foreign_keys = ON")
        row = self._conn.execute("PRAGMA foreign_keys").fetchone()
        if row is None or row[0] != 1:
            raise RuntimeError("SQLite 外键未启用")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @classmethod
    def from_settings(cls, settings: Settings) -> SqliteStorage:
        return cls(settings.storage.path)

    # ---- 内部同步实现（在 to_thread 中运行） ----

    def _record_run(self, run: ReviewRun) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO runs
                (run_id, external_ref, base_sha, head_sha, strategy, status,
                 publish_status, warnings_json, config_hash, started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.external_ref,
                    run.base_sha,
                    run.head_sha,
                    run.strategy.value,
                    run.status.value,
                    run.publish_status,
                    json.dumps(run.warnings, ensure_ascii=False),  # P1-4：只存 warnings，不存整个 run
                    run.config_snapshot_hash,
                    run.started_at.isoformat(),
                    run.finished_at.isoformat() if run.finished_at else None,
                ),
            )
            self._conn.commit()

    def _record_findings(self, findings: list[Finding]) -> None:
        with self._lock:
            for f in findings:
                self._conn.execute(
                    """
                    INSERT INTO findings
                    (finding_occurrence_id, run_id, fingerprint, cross_run_match_key, cluster_id,
                     title, severity, confidence, category, claimed_path, claimed_start, claimed_end,
                     canonical_path, canonical_start, canonical_end, status, evidence_json, sources_json,
                     is_outside_diff)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f.finding_occurrence_id,
                        f.run_id,
                        f.fingerprint,
                        f.cross_run_match_key,
                        f.cluster_id,
                        f.title,
                        f.severity.value,
                        f.confidence,
                        f.category.value,
                        f.claimed_path,
                        f.claimed_start_line,
                        f.claimed_end_line,
                        f.canonical_path,
                        f.canonical_start_line,
                        f.canonical_end_line,
                        f.status.value,
                        json.dumps([e.model_dump() for e in f.evidence], ensure_ascii=False),  # P1-5
                        json.dumps([s.model_dump() for s in f.sources], ensure_ascii=False),  # P1-5
                        int(f.is_outside_diff),
                    ),
                )
                for v in f.versions:
                    self._conn.execute(
                        """
                        INSERT INTO finding_versions
                        (finding_occurrence_id, from_status, to_status, actor, at, reason)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f.finding_occurrence_id,
                            v.from_status.value,
                            v.to_status.value,
                            v.actor,
                            v.at.isoformat(),
                            v.reason,
                        ),
                    )
            self._conn.commit()

    def _record_usage(self, run_id: str, usage: ModelUsage) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO usages
                (run_id, model, role, in_tokens, out_tokens, cost_usd, outcome, latency_ms, retries, prompt_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    usage.model,
                    usage.role,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cost_usd,
                    usage.outcome.value,
                    usage.latency_ms,
                    usage.retries,
                    usage.prompt_hash,
                ),
            )
            self._conn.commit()

    # ---- async 公开接口（满足 Storage 协议，P0-1） ----

    async def record_run(self, run: ReviewRun) -> None:
        await asyncio.to_thread(self._record_run, run)

    async def record_findings(self, findings: list[Finding]) -> None:
        await asyncio.to_thread(self._record_findings, findings)

    async def record_usage(self, run_id: str, usage: ModelUsage) -> None:
        await asyncio.to_thread(self._record_usage, run_id, usage)

    # ---- 测试/运维辅助 ----

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()
