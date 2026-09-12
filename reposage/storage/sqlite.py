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
from datetime import datetime
from pathlib import Path
from typing import Any

from reposage.config.settings import Settings
from reposage.domain.enums import (
    CommentKind,
    CommentStatus,
    FeedbackKind,
    FindingCategory,
    FindingStatus,
    PublishOperationKind,
    PublishOperationStatus,
    PublishPlanStatus,
    RevokeFeedbackResult,
    Severity,
)
from reposage.domain.finding import Finding
from reposage.domain.models import (
    CoverageManifest,
    Evidence,
    FindingSource,
    ModelUsage,
    ToolCall,
    ToolResult,
    utcnow,
)
from reposage.domain.run import (
    CommentPlan,
    DeleteCommentRequest,
    FeedbackMemory,
    GateDecision,
    LeaseLostError,
    PublishCommentResult,
    PublishOperation,
    PublishPlan,
    ReviewRun,
    ReviewTask,
)
from reposage.observability.tool_trace import (
    canonicalize_args_json,
    canonicalize_data_json,
    canonicalize_error,
)

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

CREATE TABLE IF NOT EXISTS run_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    sequence INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 1,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    error TEXT,
    detail TEXT
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
    prompt_hash TEXT,
    schema_hash TEXT,
    schema_repairs INTEGER NOT NULL DEFAULT 0
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
    needs_evidence INTEGER NOT NULL DEFAULT 0,
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
    pr_identity TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'dry_run',
    target_head_sha TEXT,
    committed_watermark TEXT,
    lease_owner TEXT,
    lease_until TEXT
);

CREATE TABLE IF NOT EXISTS publish_operations (
    op_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES publish_plans(plan_id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS published_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES publish_plans(plan_id),
    comment_id TEXT NOT NULL,
    required INTEGER NOT NULL DEFAULT 1,
    finding_occurrence_id TEXT REFERENCES findings(finding_occurrence_id),
    fingerprint TEXT,
    stable_key TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    path TEXT,
    line INTEGER,
    body TEXT NOT NULL,
    marker TEXT NOT NULL DEFAULT '',
    remote_comment_id INTEGER,
    status TEXT NOT NULL DEFAULT 'prepared',
    UNIQUE (plan_id, comment_id)
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
CREATE INDEX IF NOT EXISTS idx_feedback_repo_active ON feedback(repo, active);

CREATE TABLE IF NOT EXISTS pr_caches (
    pr_key TEXT PRIMARY KEY,
    base_sha TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    diff_hash TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_decisions (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    file_path TEXT NOT NULL,
    role_id TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    reason TEXT NOT NULL,
    matched_features_json TEXT NOT NULL DEFAULT '[]',
    gate_version TEXT NOT NULL,
    PRIMARY KEY (run_id, file_path, role_id)
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
        self._migrate()
        self._ensure_feedback_index()
        self._conn.commit()

    def _migrate(self) -> None:
        """版本化迁移（PRAGMA user_version，V1-e 返工 P1 / 四轮 P1 / V1-f）。

        v0→v1（SCHEMA 基线，含 pr_identity/stable_key 列）。
        v1→v2（四轮 P1）：watermark 拆为 target_head_sha + committed_watermark，
            并新增 lease_owner/lease_until 并发租约列。
        v2→v3（V1-f）：新增 run_stages 表（随 SCHEMA IF NOT EXISTS 自动补建，旧库安全升级）。
        v3→v4（V2-A）：新增 gate_decisions 表。
        v4→v5（V2-D）：findings.needs_evidence。
        幂等：新库 executescript 后 user_version 设为 5；旧库按列是否存在补列并建唯一索引。
        V2-E：feedback 表已在 SCHEMA；idx_feedback_repo_active 在初始化时 IF NOT EXISTS，不升版本。
        """
        cur = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if cur >= 1:
            # 增量迁移（旧库可能已有 v1 结构）
            if cur == 1:
                self._migrate_v1_to_v2()
            if cur <= 2:
                self._migrate_v2_to_v3()
            if cur <= 3:
                self._migrate_v3_to_v4()
            if cur <= 4:
                self._migrate_v4_to_v5()
            return
        # 旧库可能缺列：按需 ALTER TABLE 补列（幂等：先查列是否存在）
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(publish_plans)").fetchall()}
        if "pr_identity" not in cols:
            self._conn.execute("ALTER TABLE publish_plans ADD COLUMN pr_identity TEXT NOT NULL DEFAULT ''")
        ccols = {r["name"] for r in self._conn.execute("PRAGMA table_info(published_comments)").fetchall()}
        if "stable_key" not in ccols:
            self._conn.execute("ALTER TABLE published_comments ADD COLUMN stable_key TEXT NOT NULL DEFAULT ''")
        # 幂等唯一约束：旧库（HEAD 版）无 UNIQUE(plan_id, comment_id)，record_publish_plan 的
        # ON CONFLICT(plan_id, comment_id) 依赖它。先去重（保留有效 published 优先），再建唯一索引。
        self._dedup_published_comments()
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_published_comments_plan_comment "
            "ON published_comments(plan_id, comment_id)"
        )
        self._migrate_v1_to_v2()
        self._migrate_v2_to_v3()
        self._migrate_v3_to_v4()
        self._migrate_v4_to_v5()

    def _migrate_v1_to_v2(self) -> None:
        """v1→v2：watermark → target_head_sha（旧值=候选 head SHA），补 committed_watermark 与租约列。"""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(publish_plans)").fetchall()}
        if "target_head_sha" not in cols:
            self._conn.execute("ALTER TABLE publish_plans ADD COLUMN target_head_sha TEXT")
            # 旧 watermark 语义即「候选 head SHA」（build_plan 就写入），搬运为 target_head_sha
            if "watermark" in cols:
                self._conn.execute("UPDATE publish_plans SET target_head_sha = watermark")
        if "committed_watermark" not in cols:
            self._conn.execute("ALTER TABLE publish_plans ADD COLUMN committed_watermark TEXT")
        if "lease_owner" not in cols:
            self._conn.execute("ALTER TABLE publish_plans ADD COLUMN lease_owner TEXT")
        if "lease_until" not in cols:
            self._conn.execute("ALTER TABLE publish_plans ADD COLUMN lease_until TEXT")
        self._conn.execute("PRAGMA user_version = 2")

    def _migrate_v2_to_v3(self) -> None:
        """v2→v3（V1-f）：run_stages 表随 SCHEMA executescript 的 IF NOT EXISTS 自动补建。"""
        # 旧库升级时 executescript 已建 run_stages 表，无需 ALTER；仅推进版本号
        self._conn.execute("PRAGMA user_version = 3")

    def _migrate_v3_to_v4(self) -> None:
        """v3→v4（V2-A）：gate_decisions 表随 SCHEMA IF NOT EXISTS 自动补建。"""
        self._conn.execute("PRAGMA user_version = 4")

    def _migrate_v4_to_v5(self) -> None:
        """v4→v5（V2-D）：findings.needs_evidence 内部标记。"""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(findings)").fetchall()}
        if "needs_evidence" not in cols:
            self._conn.execute(
                "ALTER TABLE findings ADD COLUMN needs_evidence INTEGER NOT NULL DEFAULT 0"
            )
        self._conn.execute("PRAGMA user_version = 5")

    def _ensure_feedback_index(self) -> None:
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_repo_active ON feedback(repo, active)"
        )

    def _dedup_published_comments(self) -> None:
        """去重 published_comments（保留有效 published + remote_id 优先，其次最新 id，V1-e 四轮 P2）。"""
        self._conn.execute(
            """
            DELETE FROM published_comments
            WHERE id NOT IN (
                SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY plan_id, comment_id
                               ORDER BY
                                   CASE WHEN status = 'published' AND remote_comment_id IS NOT NULL THEN 0 ELSE 1 END,
                                   id DESC
                           ) AS rn
                    FROM published_comments
                ) WHERE rn = 1
            )
            """
        )

    def close(self) -> None:
        self._conn.close()

    @classmethod
    def from_settings(cls, settings: Settings) -> SqliteStorage:
        return cls(settings.storage.path)

    # ---- 内部同步实现（在 to_thread 中运行） ----

    def _record_run(self, run: ReviewRun) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO runs
                    (run_id, external_ref, base_sha, head_sha, strategy, status,
                     publish_status, warnings_json, config_hash, started_at, finished_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        external_ref=excluded.external_ref,
                        base_sha=excluded.base_sha,
                        head_sha=excluded.head_sha,
                        strategy=excluded.strategy,
                        status=excluded.status,
                        publish_status=excluded.publish_status,
                        warnings_json=excluded.warnings_json,
                        config_hash=excluded.config_hash,
                        finished_at=excluded.finished_at
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
                # V1-f：阶段记录落库。同一 run 重写（第一次落库 + finish 后定稿）不产生
                # 无界重复——先清空再全量写入，保留 sequence 顺序，同 stage 可多次出现。
                self._conn.execute("DELETE FROM run_stages WHERE run_id = ?", (run.run_id,))
                for seq, s in enumerate(run.stages):
                    self._conn.execute(
                        """
                        INSERT INTO run_stages
                        (run_id, sequence, stage, status, required, duration_ms, tokens, cost_usd, error, detail)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run.run_id,
                            seq,
                            s.stage.value,
                            s.status.value,
                            int(s.required),
                            s.duration_ms,
                            s.tokens,
                            s.cost_usd,
                            s.error,
                            s.detail,
                        ),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _record_findings(self, findings: list[Finding]) -> None:
        with self._lock:
            try:
                for f in findings:
                    self._conn.execute(
                        """
                        INSERT INTO findings
                        (finding_occurrence_id, run_id, fingerprint, cross_run_match_key, cluster_id,
                         title, severity, confidence, category, claimed_path, claimed_start, claimed_end,
                         canonical_path, canonical_start, canonical_end, status, evidence_json, sources_json,
                         is_outside_diff, needs_evidence)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            int(f.needs_evidence),
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
            except Exception:
                # P1（复验）：批量写入中途失败必须 rollback，防止半批数据在后续操作中意外提交
                self._conn.rollback()
                raise

    def _record_usage(self, run_id: str, usage: ModelUsage) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO usages
                    (run_id, model, role, in_tokens, out_tokens, cost_usd, outcome,
                     latency_ms, retries, prompt_hash, schema_hash, schema_repairs)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        usage.schema_hash,
                        usage.schema_repairs,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _record_tasks(self, tasks: list[ReviewTask]) -> None:
        """落库 ReviewTask（V1-f：成本/Token 指标，tasks 表 in_tokens/out_tokens）。"""
        with self._lock:
            try:
                for t in tasks:
                    self._conn.execute(
                        """
                        INSERT INTO tasks
                        (task_id, run_id, kind, target, status, in_tokens, out_tokens, cost_usd, error)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(task_id) DO UPDATE SET
                            run_id=excluded.run_id,
                            kind=excluded.kind,
                            target=excluded.target,
                            status=excluded.status,
                            in_tokens=excluded.in_tokens,
                            out_tokens=excluded.out_tokens,
                            cost_usd=excluded.cost_usd,
                            error=excluded.error
                        """,
                        (
                            t.task_id,
                            t.run_id,
                            t.kind.value,
                            t.target,
                            t.status.value,
                            t.input_tokens,
                            t.output_tokens,
                            t.cost_usd,
                            t.error,
                        ),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _record_coverage(self, run_id: str, coverage: CoverageManifest) -> None:
        """落库覆盖清单（V1-f：CoverageItem 全指标，items 序列化为 JSON）。"""
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO coverages
                    (run_id, items_json, truncated)
                    VALUES (?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        items_json=excluded.items_json,
                        truncated=excluded.truncated
                    """,
                    (
                        run_id,
                        json.dumps([i.model_dump() for i in coverage.items], ensure_ascii=False),
                        int(coverage.truncated),
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _record_gate_decisions(self, run_id: str, decisions: list[GateDecision]) -> None:
        """幂等 upsert 门控决策（不含源码；matched_features 仅为规则 ID）。"""
        if not decisions:
            return
        with self._lock:
            try:
                for d in decisions:
                    self._conn.execute(
                        """
                        INSERT INTO gate_decisions
                        (run_id, file_path, role_id, enabled, reason, matched_features_json, gate_version)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(run_id, file_path, role_id) DO UPDATE SET
                            enabled=excluded.enabled,
                            reason=excluded.reason,
                            matched_features_json=excluded.matched_features_json,
                            gate_version=excluded.gate_version
                        """,
                        (
                            run_id,
                            d.file_path,
                            d.role_id,
                            int(d.enabled),
                            d.reason,
                            json.dumps(d.matched_features, ensure_ascii=False),
                            d.gate_version,
                        ),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ---- async 公开接口（满足 Storage 协议，P0-1） ----

    async def record_run(self, run: ReviewRun) -> None:
        await asyncio.to_thread(self._record_run, run)

    async def record_findings(self, findings: list[Finding]) -> None:
        await asyncio.to_thread(self._record_findings, findings)

    async def record_usage(self, run_id: str, usage: ModelUsage) -> None:
        await asyncio.to_thread(self._record_usage, run_id, usage)

    async def record_tasks(self, tasks: list[ReviewTask]) -> None:
        await asyncio.to_thread(self._record_tasks, tasks)

    async def record_coverage(self, run_id: str, coverage: CoverageManifest) -> None:
        await asyncio.to_thread(self._record_coverage, run_id, coverage)

    async def record_gate_decisions(self, run_id: str, decisions: list[GateDecision]) -> None:
        await asyncio.to_thread(self._record_gate_decisions, run_id, decisions)

    # ---- V1-e：发布（Saga/Outbox）CRUD ----

    def _record_publish_plan(self, plan: PublishPlan) -> None:
        """落库 plan（create-if-absent，V1-e 五轮 P0：不 UPSERT 回写状态）。

        已存在的 plan（恢复路径）不更新 status/committed_watermark，避免把另一个
        Worker 已推进的状态回写成旧状态。comments/operations 同样 create-if-absent。
        """
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO publish_plans
                    (plan_id, run_id, pr_identity, status, mode, target_head_sha, committed_watermark)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(plan_id) DO NOTHING
                    """,
                    (
                        plan.plan_id,
                        plan.run_id,
                        plan.pr_identity,
                        plan.status.value,
                        plan.mode,
                        plan.target_head_sha,
                        plan.committed_watermark,
                    ),
                )
                for c in plan.comments:
                    self._conn.execute(
                        """
                        INSERT INTO published_comments
                        (plan_id, comment_id, required, finding_occurrence_id, fingerprint,
                         stable_key, kind, path, line, body, marker, remote_comment_id, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(plan_id, comment_id) DO NOTHING
                        """,
                        (
                            plan.plan_id,
                            c.comment_id,
                            int(c.required),
                            c.finding_occurrence_id,
                            c.fingerprint,
                            c.stable_key,
                            c.kind.value,
                            c.path,
                            c.line,
                            c.body,
                            c.marker,
                            c.remote_comment_id,
                            c.status.value,
                        ),
                    )
                for op in plan.operations:
                    self._conn.execute(
                        """
                        INSERT INTO publish_operations
                        (op_id, plan_id, kind, status, detail)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(op_id) DO NOTHING
                        """,
                        (op.op_id, op.plan_id, op.kind.value, op.status.value, op.detail),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _assert_lease_locked(self, plan_id: str, lease_owner: str) -> None:
        """锁内校验租约（V1-e 五轮 fencing）：不匹配抛 LeaseLostError。"""
        row = self._conn.execute(
            "SELECT lease_owner FROM publish_plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if row is None or row["lease_owner"] != lease_owner:
            raise LeaseLostError(f"lease lost for plan {plan_id}")

    def _record_operation_locked(self, op: PublishOperation) -> None:
        self._conn.execute(
            """
            INSERT INTO publish_operations
            (op_id, plan_id, kind, status, detail)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(op_id) DO UPDATE SET
                plan_id=excluded.plan_id,
                kind=excluded.kind,
                status=excluded.status,
                detail=excluded.detail
            """,
            (op.op_id, op.plan_id, op.kind.value, op.status.value, op.detail),
        )

    def _record_operation(self, op: PublishOperation, lease_owner: str) -> None:
        """记录 operation（带 fencing：V1-e 五轮，租约丢失抛 LeaseLostError）。"""
        with self._lock:
            try:
                self._assert_lease_locked(op.plan_id, lease_owner)
                self._record_operation_locked(op)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _record_comment_results(
        self, plan_id: str, results: dict[str, PublishCommentResult], lease_owner: str
    ) -> None:
        with self._lock:
            try:
                # fencing：同一事务内先校验租约再写（V1-e 五轮）
                self._assert_lease_locked(plan_id, lease_owner)
                for r in results.values():
                    self._conn.execute(
                        """
                        UPDATE published_comments
                        SET status = ?, remote_comment_id = ?
                        WHERE plan_id = ? AND comment_id = ?
                        """,
                        (r.status.value, r.remote_comment_id, plan_id, r.comment_id),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _update_plan_status(
        self, plan_id: str, status: PublishPlanStatus, lease_owner: str
    ) -> None:
        """fencing：只有持有当前租约的 owner 能推进状态（V1-e 五轮 P0）。

        rowcount==0 抛 LeaseLostError，调用方立即停止后续写入与远端调用。
        """
        with self._lock:
            try:
                cur = self._conn.execute(
                    "UPDATE publish_plans SET status = ? WHERE plan_id = ? AND lease_owner = ?",
                    (status.value, plan_id, lease_owner),
                )
                if cur.rowcount != 1:
                    raise LeaseLostError(f"lease lost for plan {plan_id}")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _load_publish_plan(self, plan_id: str) -> PublishPlan | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT plan_id, run_id, pr_identity, status, mode, target_head_sha, committed_watermark "
                "FROM publish_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                return None
            comments = self._conn.execute(
                """
                SELECT comment_id, required, finding_occurrence_id, fingerprint, stable_key,
                       kind, path, line, body, marker, remote_comment_id, status
                FROM published_comments WHERE plan_id = ? ORDER BY id
                """,
                (plan_id,),
            ).fetchall()
            operations = self._conn.execute(
                "SELECT op_id, kind, status, detail FROM publish_operations WHERE plan_id = ?",
                (plan_id,),
            ).fetchall()
        plan = PublishPlan(
            plan_id=row["plan_id"],
            run_id=row["run_id"],
            pr_identity=row["pr_identity"],
            mode=row["mode"],
            status=PublishPlanStatus(row["status"]),
            target_head_sha=row["target_head_sha"],
            committed_watermark=row["committed_watermark"],
            comments=[
                CommentPlan(
                    comment_id=c["comment_id"],
                    required=bool(c["required"]),
                    finding_occurrence_id=c["finding_occurrence_id"],
                    fingerprint=c["fingerprint"],
                    stable_key=c["stable_key"],
                    kind=CommentKind(c["kind"]),
                    path=c["path"],
                    line=c["line"],
                    body=c["body"],
                    marker=c["marker"],
                    remote_comment_id=c["remote_comment_id"],
                    status=CommentStatus(c["status"]),
                )
                for c in comments
            ],
            operations=[
                PublishOperation(
                    op_id=o["op_id"],
                    plan_id=plan_id,
                    kind=PublishOperationKind(o["kind"]),
                    status=PublishOperationStatus(o["status"]),
                    detail=o["detail"],
                )
                for o in operations
            ],
        )
        return plan

    def _load_recoverable_plans(self, run_id: str) -> list[PublishPlan]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT plan_id FROM publish_plans
                WHERE run_id = ? AND status IN ('prepared', 'publishing', 'partial', 'cleanup_pending')
                """,
                (run_id,),
            ).fetchall()
        plans = [self._load_publish_plan(r["plan_id"]) for r in rows]
        return [p for p in plans if p is not None]

    def _record_plan_published_with_cleanup(
        self, plan_id: str, committed_watermark: str, op: PublishOperation, lease_owner: str
    ) -> None:
        """同事务：plan → published + committed_watermark + cleanup op PENDING（V1-e 四轮 P0）。

        带 fencing（V1-e 五轮 P0）：先 UPDATE plan（WHERE lease_owner），rowcount==0 则
        整个事务回滚（禁止写入 operation）并抛 LeaseLostError。
        """
        with self._lock:
            try:
                cur = self._conn.execute(
                    "UPDATE publish_plans SET status = 'published', committed_watermark = ? "
                    "WHERE plan_id = ? AND lease_owner = ?",
                    (committed_watermark, plan_id, lease_owner),
                )
                if cur.rowcount != 1:
                    raise LeaseLostError(f"lease lost for plan {plan_id}")
                self._record_operation_locked(op)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _finish_cleanup(self, plan_id: str, *, success: bool, lease_owner: str) -> None:
        """收敛 plan 状态（V1-e 四轮复验 should-fix：不覆盖 op 状态）。带 fencing。

        rowcount==0 抛 LeaseLostError（V1-e 五轮 P0）。
        """
        plan_status = PublishPlanStatus.COMPLETED if success else PublishPlanStatus.CLEANUP_PENDING
        with self._lock:
            try:
                cur = self._conn.execute(
                    "UPDATE publish_plans SET status = ? WHERE plan_id = ? AND lease_owner = ?",
                    (plan_status.value, plan_id, lease_owner),
                )
                if cur.rowcount != 1:
                    raise LeaseLostError(f"lease lost for plan {plan_id}")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _claim_plan(self, plan_id: str, lease_owner: str, lease_until: str, now: str) -> bool:
        """原子租约 claim（V1-e 四轮 P1 / 五轮 P0）：CAS 检查状态与 lease 过期。

        now 是当前时间，用于比较 lease_until 是否过期；不得用新到期时间代替（五轮 P0）。
        """
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    UPDATE publish_plans
                    SET lease_owner = ?, lease_until = ?
                    WHERE plan_id = ?
                      AND status IN ('prepared', 'publishing', 'partial', 'cleanup_pending', 'published')
                      AND (lease_until IS NULL OR lease_until < ?)
                    """,
                    (lease_owner, lease_until, plan_id, now),
                )
                self._conn.commit()
                return cur.rowcount == 1
            except Exception:
                self._conn.rollback()
                raise

    def _release_lease(self, plan_id: str, lease_owner: str) -> None:
        """释放租约（V1-e 五轮 P0）：正常结束路径清空 lease，带 fencing。"""
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE publish_plans SET lease_owner = NULL, lease_until = NULL "
                    "WHERE plan_id = ? AND lease_owner = ?",
                    (plan_id, lease_owner),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _retire_remote_id(
        self, plan_id: str, pr_identity: str, marker: str, stale_remote_id: int, lease_owner: str
    ) -> None:
        """外部删除收敛（V1-e 四轮 P0）：同 PR 同 marker 的旧 active 映射置 superseded。

        带 fencing（V1-e 五轮）：锁内先校验租约再写。
        """
        with self._lock:
            try:
                self._assert_lease_locked(plan_id, lease_owner)
                self._conn.execute(
                    """
                    UPDATE published_comments
                    SET status = 'superseded'
                    WHERE marker = ?
                      AND remote_comment_id = ?
                      AND status = 'published'
                      AND plan_id IN (
                          SELECT plan_id FROM publish_plans WHERE pr_identity = ?
                      )
                    """,
                    (marker, stale_remote_id, pr_identity),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _mark_plan_obsolete(self, plan_id: str) -> None:
        """旧 head 的 plan 被新 head 取代时标记 OBSOLETE。

        只废弃无活跃租约（lease_owner IS NULL）的 plan（V1-e 六轮复验 should-fix）：
        若旧 plan 正被另一 Worker 持租约恢复中，则跳过废弃，避免与持有者互相覆盖。
        """
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE publish_plans SET status = 'obsolete' "
                    "WHERE plan_id = ? AND lease_owner IS NULL",
                    (plan_id,),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    async def record_publish_plan(self, plan: PublishPlan) -> None:
        await asyncio.to_thread(self._record_publish_plan, plan)

    async def record_comment_results(
        self, plan_id: str, results: dict[str, PublishCommentResult], lease_owner: str
    ) -> None:
        await asyncio.to_thread(self._record_comment_results, plan_id, results, lease_owner)

    async def record_operation(self, op: PublishOperation, lease_owner: str) -> None:
        await asyncio.to_thread(self._record_operation, op, lease_owner)

    async def update_plan_status(
        self, plan_id: str, status: PublishPlanStatus, lease_owner: str
    ) -> None:
        await asyncio.to_thread(self._update_plan_status, plan_id, status, lease_owner)

    async def assert_lease(self, plan_id: str, lease_owner: str) -> None:
        await asyncio.to_thread(self._assert_lease, plan_id, lease_owner)

    def _assert_lease(self, plan_id: str, lease_owner: str) -> None:
        with self._lock:
            self._assert_lease_locked(plan_id, lease_owner)

    async def record_plan_published_with_cleanup(
        self, plan_id: str, committed_watermark: str, op: PublishOperation, lease_owner: str
    ) -> None:
        await asyncio.to_thread(
            self._record_plan_published_with_cleanup, plan_id, committed_watermark, op, lease_owner
        )

    async def finish_cleanup(self, plan_id: str, *, success: bool, lease_owner: str) -> None:
        await asyncio.to_thread(self._finish_cleanup, plan_id, success=success, lease_owner=lease_owner)

    async def claim_plan(self, plan_id: str, lease_owner: str, lease_until: str, now: str) -> bool:
        return await asyncio.to_thread(self._claim_plan, plan_id, lease_owner, lease_until, now)

    async def release_lease(self, plan_id: str, lease_owner: str) -> None:
        await asyncio.to_thread(self._release_lease, plan_id, lease_owner)

    async def retire_remote_id(
        self, plan_id: str, pr_identity: str, marker: str, stale_remote_id: int, lease_owner: str
    ) -> None:
        await asyncio.to_thread(
            self._retire_remote_id, plan_id, pr_identity, marker, stale_remote_id, lease_owner
        )

    async def mark_plan_obsolete(self, plan_id: str) -> None:
        await asyncio.to_thread(self._mark_plan_obsolete, plan_id)

    async def load_publish_plan(self, plan_id: str) -> PublishPlan | None:
        return await asyncio.to_thread(self._load_publish_plan, plan_id)

    async def load_recoverable_plans(self, run_id: str) -> list[PublishPlan]:
        return await asyncio.to_thread(self._load_recoverable_plans, run_id)

    def _update_finding_status(
        self, finding_occurrence_id: str, status: str, plan_id: str, lease_owner: str
    ) -> None:
        with self._lock:
            try:
                # fencing：同一事务内先校验租约再写（V1-e 五轮）
                self._assert_lease_locked(plan_id, lease_owner)
                self._conn.execute(
                    "UPDATE findings SET status = ? WHERE finding_occurrence_id = ?",
                    (status, finding_occurrence_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    async def update_finding_status(
        self, finding_occurrence_id: str, status: str, plan_id: str, lease_owner: str
    ) -> None:
        await asyncio.to_thread(
            self._update_finding_status, finding_occurrence_id, status, plan_id, lease_owner
        )

    # ---- V1-e 返工：跨 run 幂等查询 ----

    def _load_published_remote_ids(self, pr_identity: str) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT marker, remote_comment_id FROM published_comments
                JOIN publish_plans ON published_comments.plan_id = publish_plans.plan_id
                WHERE publish_plans.pr_identity = ?
                  AND remote_comment_id IS NOT NULL
                  AND published_comments.status = 'published'
                ORDER BY published_comments.id ASC
                """,
                (pr_identity,),
            ).fetchall()
        # 确定性规则（V1-e 四轮 P0）：同一 marker 多条 active 时取最新 id（后写覆盖先写）
        mapping: dict[str, int] = {}
        for r in rows:
            mapping[r["marker"]] = r["remote_comment_id"]
        return mapping

    def _load_superseded_comment_ids(
        self, pr_identity: str, exclude_plan_id: str
    ) -> list[DeleteCommentRequest]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT remote_comment_id, marker FROM published_comments
                JOIN publish_plans ON published_comments.plan_id = publish_plans.plan_id
                WHERE publish_plans.pr_identity = ?
                  AND publish_plans.plan_id != ?
                  AND remote_comment_id IS NOT NULL
                  AND published_comments.status = 'published'
                """,
                (pr_identity, exclude_plan_id),
            ).fetchall()
        return [
            DeleteCommentRequest(
                remote_comment_id=r["remote_comment_id"],
                pr_identity=pr_identity,
                expected_marker=r["marker"],
            )
            for r in rows
        ]

    def _mark_comments_superseded(
        self, plan_id: str, remote_comment_ids: list[int], lease_owner: str
    ) -> None:
        if not remote_comment_ids:
            return
        with self._lock:
            try:
                # fencing：同一事务内先校验租约再写（V1-e 五轮）
                self._assert_lease_locked(plan_id, lease_owner)
                placeholders = ",".join("?" for _ in remote_comment_ids)
                self._conn.execute(
                    f"UPDATE published_comments SET status = 'superseded' "
                    f"WHERE remote_comment_id IN ({placeholders})",
                    tuple(remote_comment_ids),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _load_latest_recoverable_publish_plan(self, pr_identity: str) -> PublishPlan | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT plan_id FROM publish_plans
                WHERE pr_identity = ? AND mode = 'publish'
                  AND (
                    status IN ('prepared', 'publishing', 'partial', 'cleanup_pending')
                    OR (
                      status = 'published' AND EXISTS (
                        SELECT 1 FROM publish_operations o
                        WHERE o.plan_id = publish_plans.plan_id
                          AND o.kind = 'supersede_cleanup'
                          AND o.status IN ('pending', 'running', 'failed')
                      )
                    )
                  )
                ORDER BY rowid DESC LIMIT 1
                """,
                (pr_identity,),
            ).fetchone()
        if row is None:
            return None
        return self._load_publish_plan(row["plan_id"])

    def _load_latest_plan(self, pr_identity: str) -> PublishPlan | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT plan_id FROM publish_plans WHERE pr_identity = ? ORDER BY rowid DESC LIMIT 1",
                (pr_identity,),
            ).fetchone()
        if row is None:
            return None
        return self._load_publish_plan(row["plan_id"])

    async def load_published_remote_ids(self, pr_identity: str) -> dict[str, int]:
        return await asyncio.to_thread(self._load_published_remote_ids, pr_identity)

    async def load_superseded_comment_ids(
        self, pr_identity: str, exclude_plan_id: str
    ) -> list[DeleteCommentRequest]:
        return await asyncio.to_thread(
            self._load_superseded_comment_ids, pr_identity, exclude_plan_id
        )

    async def load_latest_plan(self, pr_identity: str) -> PublishPlan | None:
        return await asyncio.to_thread(self._load_latest_plan, pr_identity)

    async def load_latest_recoverable_publish_plan(self, pr_identity: str) -> PublishPlan | None:
        return await asyncio.to_thread(self._load_latest_recoverable_publish_plan, pr_identity)

    async def mark_comments_superseded(
        self, plan_id: str, remote_comment_ids: list[int], lease_owner: str
    ) -> None:
        await asyncio.to_thread(self._mark_comments_superseded, plan_id, remote_comment_ids, lease_owner)

    # ---- V2-E：反馈记忆 + 增量 watermark ----

    def _row_to_feedback(self, row: sqlite3.Row) -> FeedbackMemory:
        revoked = row["revoked_at"]
        created = row["created_at"]
        return FeedbackMemory(
            id=int(row["id"]),
            repo=row["repo"],
            kind=FeedbackKind(row["kind"]),
            scope=row["scope"],
            path=row["path"],
            symbol=row["symbol"],
            category=row["category"],
            rule_key=row["rule_key"],
            pattern=row["pattern"],
            cross_run_match_key=row["cross_run_match_key"],
            rationale=row["rationale"] or "",
            active=bool(row["active"]),
            created_at=datetime.fromisoformat(created) if created else utcnow(),
            revoked_at=datetime.fromisoformat(revoked) if revoked else None,
        )

    def _record_feedback(self, memory: FeedbackMemory) -> int:
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO feedback
                    (repo, kind, scope, path, symbol, category, rule_key, pattern,
                     cross_run_match_key, rationale, active, created_at, revoked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory.repo,
                        memory.kind.value,
                        memory.scope,
                        memory.path,
                        memory.symbol,
                        memory.category,
                        memory.rule_key,
                        memory.pattern,
                        memory.cross_run_match_key,
                        memory.rationale,
                        int(memory.active),
                        memory.created_at.isoformat(),
                        memory.revoked_at.isoformat() if memory.revoked_at else None,
                    ),
                )
                self._conn.commit()
                row_id = cur.lastrowid
                if row_id is None:
                    raise RuntimeError("feedback insert 未返回 id")
                return int(row_id)
            except Exception:
                self._conn.rollback()
                raise

    def _list_feedback(self, repo: str | None, include_revoked: bool) -> list[FeedbackMemory]:
        sql = "SELECT * FROM feedback"
        params: list[Any] = []
        clauses: list[str] = []
        if repo is not None:
            clauses.append("repo = ?")
            params.append(repo)
        if not include_revoked:
            clauses.append("active = 1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_feedback(r) for r in rows]

    def _revoke_feedback(self, feedback_id: int, repo: str) -> RevokeFeedbackResult:
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT repo, active FROM feedback WHERE id = ?", (feedback_id,)
                ).fetchone()
                if row is None:
                    return RevokeFeedbackResult.NOT_FOUND
                if row["repo"] != repo:
                    return RevokeFeedbackResult.WRONG_REPO
                if not row["active"]:
                    return RevokeFeedbackResult.ALREADY_REVOKED
                self._conn.execute(
                    """
                    UPDATE feedback SET active = 0, revoked_at = ?
                    WHERE id = ? AND repo = ? AND active = 1
                    """,
                    (utcnow().isoformat(), feedback_id, repo),
                )
                self._conn.commit()
                return RevokeFeedbackResult.REVOKED
            except Exception:
                self._conn.rollback()
                raise

    def _get_finding(self, finding_occurrence_id: str) -> Finding | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM findings WHERE finding_occurrence_id = ?",
                (finding_occurrence_id,),
            ).fetchone()
        if row is None:
            return None
        evidence_raw = json.loads(row["evidence_json"] or "[]")
        sources_raw = json.loads(row["sources_json"] or "[]")
        return Finding(
            finding_occurrence_id=row["finding_occurrence_id"],
            run_id=row["run_id"],
            fingerprint=row["fingerprint"],
            cross_run_match_key=row["cross_run_match_key"],
            cluster_id=row["cluster_id"],
            title=row["title"],
            severity=Severity(row["severity"]),
            confidence=row["confidence"],
            category=FindingCategory(row["category"]),
            claimed_path=row["claimed_path"],
            claimed_start_line=row["claimed_start"],
            claimed_end_line=row["claimed_end"],
            canonical_path=row["canonical_path"],
            canonical_start_line=row["canonical_start"],
            canonical_end_line=row["canonical_end"],
            status=FindingStatus(row["status"]),
            evidence=[Evidence.model_validate(item) for item in evidence_raw],
            sources=[FindingSource.model_validate(item) for item in sources_raw],
            is_outside_diff=bool(row["is_outside_diff"]),
            needs_evidence=bool(row["needs_evidence"]),
        )

    def _load_committed_watermark(self, pr_identity: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT committed_watermark FROM publish_plans
                WHERE pr_identity = ?
                  AND committed_watermark IS NOT NULL
                  AND committed_watermark != ''
                  AND mode = 'publish'
                  AND status IN ('published', 'cleanup_pending', 'completed')
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (pr_identity,),
            ).fetchone()
        if row is None:
            return None
        return str(row["committed_watermark"])

    def _record_tool_invocation(self, call: ToolCall, result: ToolResult) -> None:
        if not call.task_id:
            raise ValueError("record_tool_invocation requires task_id")
        args_json, args_trunc = canonicalize_args_json(call.arguments)
        data, data_trunc = canonicalize_data_json(result.data)
        error = canonicalize_error(result.error)
        truncated = result.truncated or args_trunc or data_trunc
        with self._lock:
            try:
                task = self._conn.execute(
                    "SELECT task_id FROM tasks WHERE task_id = ?", (call.task_id,)
                ).fetchone()
                if task is None:
                    raise ValueError("task_id not found")
                existing = self._conn.execute(
                    """
                    SELECT c.task_id, c.name, c.args_json, c.status, c.repeat_of,
                           c.tokens_used, c.attempt_count, r.data, r.error, r.truncated, r.tokens
                    FROM tool_calls c
                    LEFT JOIN tool_results r ON r.tool_call_id = c.tool_call_id
                    WHERE c.tool_call_id = ?
                    """,
                    (call.tool_call_id,),
                ).fetchone()
                if existing is not None:
                    same = (
                        existing["task_id"] == call.task_id
                        and existing["name"] == call.name
                        and existing["args_json"] == args_json
                        and existing["status"] == call.status.value
                        and (existing["repeat_of"] or None) == call.repeat_of
                        and existing["tokens_used"] == call.tokens_used
                        and existing["attempt_count"] == call.attempt_count
                        and (existing["data"] or None) == data
                        and (existing["error"] or None) == error
                        and bool(existing["truncated"]) == truncated
                        and existing["tokens"] == result.tokens
                    )
                    if same:
                        return
                    raise ValueError("tool_call_id conflict")
                self._conn.execute(
                    """
                    INSERT INTO tool_calls
                    (tool_call_id, task_id, name, args_json, status, repeat_of, tokens_used, attempt_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        call.tool_call_id,
                        call.task_id,
                        call.name,
                        args_json,
                        call.status.value,
                        call.repeat_of,
                        call.tokens_used,
                        call.attempt_count,
                    ),
                )
                self._conn.execute(
                    """
                    INSERT INTO tool_results (tool_call_id, data, error, truncated, tokens)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (call.tool_call_id, data, error, int(truncated), result.tokens),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    async def record_feedback(self, memory: FeedbackMemory) -> int:
        return await asyncio.to_thread(self._record_feedback, memory)

    async def list_feedback(
        self, repo: str | None, *, include_revoked: bool
    ) -> list[FeedbackMemory]:
        return await asyncio.to_thread(self._list_feedback, repo, include_revoked)

    async def revoke_feedback(self, feedback_id: int, *, repo: str) -> RevokeFeedbackResult:
        return await asyncio.to_thread(self._revoke_feedback, feedback_id, repo)

    async def load_active_feedback(self, repo: str) -> list[FeedbackMemory]:
        return await asyncio.to_thread(self._list_feedback, repo, False)

    async def get_finding(self, finding_occurrence_id: str) -> Finding | None:
        return await asyncio.to_thread(self._get_finding, finding_occurrence_id)

    async def load_committed_watermark(self, pr_identity: str) -> str | None:
        return await asyncio.to_thread(self._load_committed_watermark, pr_identity)

    async def record_tool_invocation(self, call: ToolCall, result: ToolResult) -> None:
        await asyncio.to_thread(self._record_tool_invocation, call, result)

    # ---- 测试/运维辅助 ----

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()
