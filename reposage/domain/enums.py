"""领域枚举（domain/enums.py）。

纯枚举，无任何 IO / SDK 依赖。契约来源：05-domain-data-design.md。
"""

from __future__ import annotations

from enum import StrEnum


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingCategory(StrEnum):
    CORRECTNESS = "correctness"
    SECURITY = "security"
    SILENT_FAILURE = "silent_failure"
    CONCURRENCY = "concurrency"
    EDGE_CASE = "edge_case"
    TEST_GAP = "test_gap"
    PERFORMANCE = "performance"
    MAINTAINABILITY = "maintainability"


class FindingStatus(StrEnum):
    """Finding 生命周期（程序驱动状态机，见 05 §3）。"""

    CANDIDATE = "candidate"
    SCHEMA_VALID = "schema_valid"
    LOCATION_VALID = "location_valid"
    EVIDENCE_VALID = "evidence_valid"
    MERGED = "merged"
    ACCEPTED = "accepted"
    SUPPRESSED = "suppressed"
    BODY_ONLY = "body_only"
    PUBLISHED = "published"
    PUBLISH_FAILED = "publish_failed"


class ReviewRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReviewTaskKind(StrEnum):
    FILE_REVIEW = "file_review"
    ROLE_REVIEW = "role_review"
    AGENT_TASK = "agent_task"


class ReviewTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_TOOL = "waiting_tool"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StageName(StrEnum):
    PREFLIGHT = "preflight"
    FETCH = "fetch"
    CONTEXT = "context"
    REVIEW = "review"
    PIPELINE = "pipeline"
    PUBLISH = "publish"


class StageStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class ChangedFileStatus(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


class DiffLineType(StrEnum):
    CONTEXT = "context"
    ADDED = "added"
    REMOVED = "removed"


class ContextLayer(StrEnum):
    L0 = "L0"  # 系统治理/协议
    L1 = "L1"  # PR 元数据
    L2 = "L2"  # 当前文件 diff hunk
    L3 = "L3"  # 相关代码
    L4 = "L4"  # 规则与反馈记忆


class ContextSourceKind(StrEnum):
    DIFF = "diff"
    FILE = "file"
    SYMBOL = "symbol"
    RULES = "rules"
    FEEDBACK = "feedback"
    ISSUE = "issue"
    SYSTEM = "system"


class EvidenceKind(StrEnum):
    DIFF_LINE = "diff_line"
    FILE_REGION = "file_region"
    SYMBOL_DEF = "symbol_def"
    SYMBOL_REF = "symbol_ref"
    TOOL_RESULT = "tool_result"
    STATIC_RESULT = "static_result"


class FindingSourceKind(StrEnum):
    LLM_GENERAL = "llm_general"
    LLM_ROLE = "llm_role"
    STATIC_ANALYZER = "static_analyzer"
    TOOL_AGENT = "tool_agent"
    JUDGE = "judge"


class PublishPlanStatus(StrEnum):
    PREPARED = "prepared"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    PARTIAL = "partial"
    FAILED = "failed"
    CLEANUP_PENDING = "cleanup_pending"
    COMPLETED = "completed"


class CommentStatus(StrEnum):
    PREPARED = "prepared"
    PUBLISHED = "published"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class CommentKind(StrEnum):
    INLINE = "inline"
    BODY = "body"
    SUMMARY = "summary"


class PublishOperationKind(StrEnum):
    SUPERSEDE_CLEANUP = "supersede_cleanup"


class PublishOperationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class ModelUsageOutcome(StrEnum):
    COMPLETED = "completed"
    LATE_CANCELLED = "late_cancelled"
    FAILED = "failed"


class ChangeRequestSource(StrEnum):
    LOCAL_RANGE = "local_range"
    GITHUB_PR = "github_pr"


class ReviewStrategyName(StrEnum):
    SINGLE_PASS = "single_pass"
    MULTI_ROLE = "multi_role"
    AGENTIC = "agentic"


class ToolCallStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    INVALID_ARGS = "invalid_args"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class FeedbackKind(StrEnum):
    FALSE_POSITIVE = "false_positive"
    WONT_FIX = "wont_fix"
    RULE = "rule"


class ToolPermission(StrEnum):
    READ_ONLY = "read_only"


class CoverageReason(StrEnum):
    COVERED = "covered"
    SKIPPED_SIZE = "skipped_size"
    SKIPPED_LANG = "skipped_lang"
    SKIPPED_GENERATED = "skipped_generated"
    ROLE_FAILED = "role_failed"
    TASK_FAILED = "task_failed"
    TRUNCATED = "truncated"


class RuleScope(StrEnum):
    GLOBAL = "global"
    REPO = "repo"
