"""Agent 事件循环（24 §4）。"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from pydantic import ValidationError

from reposage.domain.enums import FindingSourceKind, ReviewTaskStatus, ToolCallStatus
from reposage.domain.finding import FindingCandidate
from reposage.domain.models import (
    AgentToolRequest,
    BudgetReservation,
    Evidence,
    GlobalBudget,
    ModelUsage,
    ModelUsageOutcome,
    utcnow,
)
from reposage.domain.protocols import LLMProvider, ModelResponse, Storage
from reposage.observability.logging import StructuredLogger, redact_secrets
from reposage.review.agent.budget import FinalizeBudgetCoordinator
from reposage.review.agent.compact import JSON_REPAIR_HINT, CompactResult, maybe_compact
from reposage.review.agent.session import AgentSession, AgentStateError
from reposage.review.candidates import sanitize_llm_candidate
from reposage.review.context import wrap_untrusted
from reposage.review.single_pass import estimate_cost, estimate_worst_cost
from reposage.tools.finish_review import FinishReviewArgs
from reposage.tools.invoke import invoke, is_repeat, new_tool_call_id
from reposage.tools.registry import ToolRegistry
from reposage.tools.snapshot import ToolWorkspace
from reposage.tools.submit_finding import SubmitFindingArgs

_CONTROL = frozenset({"submit_finding", "finish_review"})
_IDLE_LIMIT = 2
_NOT_EXECUTED = "not_executed_multiple_calls"
_JSON_REPAIR_USER = (
    f"你上次的输出{JSON_REPAIR_HINT}。错误：{{detail}}\n"
    "请只输出一个修正后的 JSON 对象，不要附加说明。"
)


class AgentLoopError(RuntimeError):
    """不可恢复的 loop 失败。"""


def _estimate_tokens(session: AgentSession) -> int:
    return max(1, session.message_chars() // 4)


def _observe_json(payload: dict[str, Any]) -> str:
    return wrap_untrusted(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _provider_failure_reason(exc: BaseException) -> str:
    summary = redact_secrets(str(exc)).replace("\n", " ").strip()[:160]
    if isinstance(exc, TimeoutError):
        return "model_timeout"
    status = getattr(exc, "status_code", None)
    body = str(getattr(exc, "error_body", "") or exc).lower()
    protocol_hints = ("tool", "tools", "tool_choice", "json_schema", "response_format", "protocol")
    if status == 400 and any(hint in body for hint in protocol_hints):
        return f"protocol_error:{summary}" if summary else "protocol_error"
    return f"model_error:{summary}" if summary else "model_error"


async def _persist_task(store: Storage, session: AgentSession) -> None:
    await store.record_tasks([session.task])


def _first_request(resp_tool_requests: list[AgentToolRequest], data: dict[str, Any] | None) -> AgentToolRequest:
    if resp_tool_requests:
        return resp_tool_requests[0]
    name = str((data or {}).get("name") or "")
    args = (data or {}).get("arguments")
    if not isinstance(args, dict):
        args = {}
    return AgentToolRequest(id=new_tool_call_id(), name=name, arguments=args)


async def run_agent_loop(
    session: AgentSession,
    *,
    llm: LLMProvider,
    registry: ToolRegistry,
    workspace: ToolWorkspace,
    global_budget: GlobalBudget,
    coordinator: FinalizeBudgetCoordinator,
    store: Storage,
    protocol: str,
    save_tool_trace: bool,
    logger: StructuredLogger | None,
    model_sem: asyncio.Semaphore,
    max_output_tokens: int,
    input_price_per_1k: float | None = None,
    output_price_per_1k: float | None = None,
) -> list[ModelUsage]:
    """跑到终态。取消时尽力落 CANCELLED 再重抛。返回本会话 usages。"""
    usages: list[ModelUsage] = []
    log = logger or StructuredLogger()

    async def stop(status: ReviewTaskStatus, reason: str) -> None:
        session.stop_reason = reason
        if session.status is ReviewTaskStatus.WAITING_TOOL and status is ReviewTaskStatus.COMPLETED:
            raise AgentStateError("WAITING_TOOL cannot complete")
        if session.status is not status:
            if session.status is ReviewTaskStatus.WAITING_TOOL and status in {
                ReviewTaskStatus.PARTIAL,
                ReviewTaskStatus.COMPLETED,
            }:
                session.transition(ReviewTaskStatus.RUNNING)
            session.transition(status, error=reason if status is not ReviewTaskStatus.COMPLETED else None)
        await _persist_task(store, session)
        log.log(
            level="info",
            run_id=session.run_id,
            stage="review",
            event="agent.stop",
            task_id=session.task_id,
            detail=f"status={status.value} reason={reason}",
        )

    def _hard_cap() -> int:
        return max(1, session.budget.max_session_chars)

    def _compact_and_log() -> CompactResult:
        result = maybe_compact(session)
        if result.did_compact:
            log.log(
                level="info",
                run_id=session.run_id,
                stage="review",
                event="agent.compact",
                task_id=session.task_id,
                detail=(
                    f"before_chars={result.before_chars} after_chars={result.after_chars} "
                    f"folded_rounds={result.folded_rounds} stubbed={result.stubbed} "
                    f"index_size={len(session.evidence_index)} "
                    f"summary_ref={result.summary_ref or '-'}"
                ),
            )
        return result

    async def _compact_or_overflow() -> bool:
        try:
            result = _compact_and_log()
        except asyncio.CancelledError:
            raise
        except Exception:
            result = CompactResult(
                did_compact=False,
                overflow=session.message_chars() > _hard_cap(),
                before_chars=session.message_chars(),
                after_chars=session.message_chars(),
                folded_rounds=0,
                summary_ref=None,
            )
        if result.overflow or session.message_chars() > _hard_cap():
            await stop(ReviewTaskStatus.PARTIAL, "context_overflow")
            return True
        return False

    open_reservation: BudgetReservation | None = None
    open_mode = "exploration"
    try:
        if session.status is ReviewTaskStatus.PENDING:
            session.transition(ReviewTaskStatus.RUNNING)
            await _persist_task(store, session)

        while session.status is ReviewTaskStatus.RUNNING:
            if session.wallclock_expired(global_remaining_s=global_budget.remaining_runtime_seconds):
                await stop(ReviewTaskStatus.PARTIAL, "wallclock")
                break
            if await _compact_or_overflow():
                break
            if session.json_repair_pending:
                if session.rounds_used >= session.budget.max_rounds:
                    session.json_repair_pending = False
                    session.idle_count += 1
                    await stop(ReviewTaskStatus.PARTIAL, "budget")
                    break
            else:
                if session.should_enter_grace() and session.enter_grace():
                    log.log(
                        level="info",
                        run_id=session.run_id,
                        stage="review",
                        event="agent.grace",
                        task_id=session.task_id,
                        detail="enter_grace",
                    )
                if session.grace_rounds_exhausted():
                    await stop(ReviewTaskStatus.PARTIAL, "budget")
                    break
                if session.rounds_used >= session.budget.max_rounds:
                    await stop(ReviewTaskStatus.PARTIAL, "budget")
                    break

            input_est = _estimate_tokens(session)
            est_cost = estimate_worst_cost(
                input_est, max_output_tokens, input_price_per_1k, output_price_per_1k
            )
            reservation = await coordinator.reserve(
                mode=session.mode,
                input_tokens=input_est,
                max_output_tokens=max_output_tokens,
                est_cost_usd=est_cost,
            )
            if reservation is None:
                if session.json_repair_pending or session.mode == "grace":
                    await stop(ReviewTaskStatus.PARTIAL, "budget")
                    break
                if session.enter_grace():
                    log.log(
                        level="info",
                        run_id=session.run_id,
                        stage="review",
                        event="agent.grace",
                        task_id=session.task_id,
                        detail="enter_grace",
                    )
                if session.grace_rounds_exhausted():
                    await stop(ReviewTaskStatus.PARTIAL, "budget")
                    break
                reservation = await coordinator.reserve(
                    mode="grace",
                    input_tokens=input_est,
                    max_output_tokens=max_output_tokens,
                    est_cost_usd=est_cost,
                )
                if reservation is None:
                    await stop(ReviewTaskStatus.PARTIAL, "budget")
                    break

            session.rounds_used += 1
            if session.mode == "grace":
                session.grace_rounds_used += 1
            if session.json_repair_pending:
                session.json_repair_used = True
                session.json_repair_pending = False
            log.log(
                level="info",
                run_id=session.run_id,
                stage="review",
                event="agent.round",
                task_id=session.task_id,
                detail=f"round={session.rounds_used} mode={session.mode}",
            )
            open_reservation = reservation
            open_mode = session.mode
            actual_input = 0
            actual_output = 0
            actual_cost = 0.0
            stop_after: tuple[ReviewTaskStatus, str] | None = None
            resp: ModelResponse | None = None
            try:
                elapsed = (utcnow() - session.started_at).total_seconds()
                remaining = min(
                    global_budget.remaining_runtime_seconds,
                    max(0.0, session.budget.max_wallclock_s - elapsed),
                )
                if remaining <= 0:
                    stop_after = (ReviewTaskStatus.PARTIAL, "wallclock")
                else:
                    async with model_sem:
                        async with asyncio.timeout(remaining):
                            resp = await llm.tool_loop(
                                session.view(),
                                registry.schemas(),
                                budget=global_budget,
                                protocol=protocol,
                            )
                    usage = resp.usage
                    if usage is not None:
                        priced = estimate_cost(
                            usage.input_tokens,
                            usage.output_tokens,
                            input_price_per_1k,
                            output_price_per_1k,
                        )
                        actual_cost = priced if priced is not None else (reservation.est_cost_usd or 0.0)
                        usage = usage.model_copy(update={"role": "agent", "cost_usd": actual_cost})
                        usages.append(usage)
                        actual_input = usage.input_tokens
                        actual_output = usage.output_tokens
                    else:
                        actual_input = reservation.input_tokens
                        actual_output = 0
                        actual_cost = reservation.est_cost_usd or 0.0
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                stop_after = (ReviewTaskStatus.PARTIAL, "model_timeout")
            except Exception as exc:
                failed_usage = getattr(exc, "usage", None)
                if isinstance(failed_usage, ModelUsage):
                    usages.append(
                        failed_usage.model_copy(update={"role": "agent", "outcome": ModelUsageOutcome.FAILED})
                    )
                    actual_input = failed_usage.input_tokens
                    actual_output = failed_usage.output_tokens
                    actual_cost = failed_usage.cost_usd
                stop_after = (ReviewTaskStatus.PARTIAL, _provider_failure_reason(exc))
            finally:
                try:
                    await coordinator.settle(
                        reservation,
                        mode=open_mode,
                        actual_input=actual_input,
                        actual_output=actual_output,
                        actual_cost=actual_cost,
                    )
                    open_reservation = None
                except asyncio.CancelledError:
                    raise

            if stop_after is not None:
                await stop(*stop_after)
                break
            if resp is None:
                await stop(ReviewTaskStatus.PARTIAL, "model_error")
                break

            action = resp.action
            data = resp.data or {}
            requests = list(resp.tool_requests)
            if action in _CONTROL or action == "tool_call":
                first = _first_request(requests, data)
                if not requests:
                    requests = [first]
                session.add_assistant(
                    resp.text or "",
                    tool_calls=requests,
                    reasoning_content=resp.reasoning_content,
                )
                terminal = await _handle_action(
                    session,
                    action=action if action in _CONTROL else "tool_call",
                    first=first,
                    rest=requests[1:],
                    registry=registry,
                    workspace=workspace,
                    store=store,
                    save_tool_trace=save_tool_trace,
                    logger=log,
                )
                if terminal:
                    break
                if await _compact_or_overflow():
                    break
                continue
            session.add_assistant(
                resp.text or str(data.get("summary") or ""),
                reasoning_content=resp.reasoning_content,
            )
            summary = str(data.get("summary") or "").strip()
            parse_error = data.get("parse_error")
            protocol_error = data.get("protocol_error")
            if parse_error:
                detail = redact_secrets(str(parse_error)).replace("\n", " ").strip()[:200]
                if protocol == "action_json" and not session.json_repair_used:
                    session.json_repair_pending = True
                    session.add_user(_JSON_REPAIR_USER.format(detail=detail or "parse_error"))
                    continue
                session.json_repair_pending = False
                session.idle_count += 1
                session.add_user(_observe_json({"error": "invalid_action", "detail": detail}))
                if session.idle_count > _IDLE_LIMIT:
                    await stop(ReviewTaskStatus.PARTIAL, "no_progress")
                    break
                continue
            if protocol_error:
                session.idle_count += 1
                session.add_user(
                    _observe_json({"error": "invalid_action", "detail": redact_secrets(str(protocol_error))[:200]})
                )
                if session.idle_count > _IDLE_LIMIT:
                    await stop(ReviewTaskStatus.PARTIAL, "no_progress")
                    break
                continue
            if not summary:
                session.idle_count += 1
                if session.idle_count > _IDLE_LIMIT:
                    await stop(ReviewTaskStatus.PARTIAL, "no_progress")
                    break
            else:
                session.idle_count = 0
        return usages
    except asyncio.CancelledError:
        log.log(
            level="info",
            run_id=session.run_id,
            stage="review",
            event="agent.cancel",
            task_id=session.task_id,
            detail="cancelled",
        )
        if open_reservation is not None:
            with contextlib.suppress(Exception):
                await coordinator.settle(
                    open_reservation, mode=open_mode, actual_input=0, actual_output=0, actual_cost=0.0
                )
        try:
            if session.status not in {
                ReviewTaskStatus.COMPLETED,
                ReviewTaskStatus.PARTIAL,
                ReviewTaskStatus.FAILED,
                ReviewTaskStatus.CANCELLED,
            }:
                session.transition(ReviewTaskStatus.CANCELLED, error="cancelled")
            await _persist_task(store, session)
        except Exception:
            pass
        raise


async def _handle_action(
    session: AgentSession,
    *,
    action: str,
    first: AgentToolRequest,
    rest: list[AgentToolRequest],
    registry: ToolRegistry,
    workspace: ToolWorkspace,
    store: Storage,
    save_tool_trace: bool,
    logger: StructuredLogger,
) -> bool:
    """处理当前动作。返回 True 表示 loop 应结束。"""
    del logger
    if action == "submit_finding":
        await _submit_finding(session, first)
        await _observe_skipped(session, rest)
        return False
    if action == "finish_review":
        await _finish_review(session, first)
        if session.status is ReviewTaskStatus.COMPLETED:
            await _observe_skipped(session, rest)
            await _persist_task(store, session)
            return True
        await _observe_skipped(session, rest)
        return False

    if first.name in _CONTROL:
        if first.name == "submit_finding":
            await _submit_finding(session, first)
        else:
            await _finish_review(session, first)
        await _observe_skipped(session, rest)
        return session.status in {
            ReviewTaskStatus.COMPLETED,
            ReviewTaskStatus.PARTIAL,
            ReviewTaskStatus.FAILED,
            ReviewTaskStatus.CANCELLED,
        }
    session.tool_attempts += 1
    if session.mode == "grace":
        session.add_tool_observation(
            tool_call_id=first.id,
            name=first.name,
            content=_observe_json({"error": "grace_no_new_tools"}),
        )
        session.index_excluded(name=first.name, arguments=first.arguments, error="grace_no_new_tools")
        await _observe_skipped(session, rest)
        return False
    if registry.get(first.name) is None:
        session.add_tool_observation(
            tool_call_id=first.id,
            name=first.name,
            content=_observe_json({"error": "unknown_tool"}),
        )
        session.index_excluded(name=first.name, arguments=first.arguments, error="unknown_tool")
        await _observe_skipped(session, rest)
        return False
    dup = is_repeat(first.name, first.arguments, session.calls)
    if dup is not None:
        session.repeat_count += 1
        session.add_tool_observation(
            tool_call_id=first.id,
            name=first.name,
            content=_observe_json({"error": "repeat", "repeat_of": dup}),
        )
        session.index_excluded(name=first.name, arguments=first.arguments, error="repeat")
        await _observe_skipped(session, rest)
        if session.repeat_count > session.budget.repeat_threshold:
            session.stop_reason = "repeat_loop"
            session.transition(ReviewTaskStatus.PARTIAL, error="repeat_loop")
            await _persist_task(store, session)
            return True
        return False
    session.repeat_count = 0
    session.transition(ReviewTaskStatus.WAITING_TOOL)
    await _persist_task(store, session)
    try:
        call, result = await invoke(
            registry,
            workspace,
            first.name,
            first.arguments,
            tool_call_id=first.id,
            store=store,
            save_tool_trace=save_tool_trace,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        session.transition(ReviewTaskStatus.FAILED, error=str(exc)[:300])
        await _persist_task(store, session)
        return True
    session.calls.append(call)
    session.transition(ReviewTaskStatus.RUNNING)
    await _persist_task(store, session)
    payload = result.data or _observe_json({"error": result.error or "error"})
    session.add_tool_observation(tool_call_id=first.id, name=first.name, content=payload)
    if call.status is ToolCallStatus.OK:
        session.successful_tools += 1
        if result.source is not None:
            session.index_success(
                tool_call_id=first.id,
                name=first.name,
                arguments=first.arguments,
                location=result.source.ref,
            )
    else:
        session.index_excluded(
            name=first.name,
            arguments=first.arguments,
            error=result.error or call.status.value,
        )
    await _observe_skipped(session, rest)
    return False


async def _observe_skipped(session: AgentSession, rest: list[AgentToolRequest]) -> None:
    for extra in rest:
        if extra.name not in _CONTROL:
            session.tool_attempts += 1
            session.index_pending(name=extra.name, tool_call_id=extra.id)
        session.add_tool_observation(
            tool_call_id=extra.id,
            name=extra.name,
            content=_observe_json({"error": _NOT_EXECUTED}),
        )


async def _submit_finding(session: AgentSession, req: AgentToolRequest) -> None:
    try:
        parsed = SubmitFindingArgs.model_validate(req.arguments)
    except ValidationError:
        session.add_tool_observation(
            tool_call_id=req.id,
            name="submit_finding",
            content=_observe_json({"error": "invalid_args"}),
        )
        return
    ids = list(parsed.evidence_tool_call_ids)
    if session.mode == "grace" and not ids:
        session.add_tool_observation(
            tool_call_id=req.id,
            name="submit_finding",
            content=_observe_json({"error": "invalid_args", "detail": "grace_requires_evidence"}),
        )
        return
    evidence: list[Evidence] = []
    for tool_id in ids:
        indexed = session.evidence_index.get(tool_id)
        if indexed is None:
            session.add_tool_observation(
                tool_call_id=req.id,
                name="submit_finding",
                content=_observe_json({"error": "invalid_args", "detail": "unknown_tool_call_id"}),
            )
            return
        evidence.append(indexed)
    raw = FindingCandidate(
        title=parsed.title,
        severity=parsed.severity,
        confidence=parsed.confidence,
        category=parsed.category,
        claimed_path=parsed.claimed_path or session.file_path,
        claimed_start_line=parsed.claimed_start_line,
        claimed_end_line=parsed.claimed_end_line,
        explanation=parsed.explanation,
        suggestion=parsed.suggestion,
        trigger_condition=parsed.trigger_condition,
        evidence=evidence,
    )
    cand = sanitize_llm_candidate(raw).model_copy(update={"source_kind": FindingSourceKind.TOOL_AGENT})
    session.candidates.append(cand)
    session.idle_count = 0
    session.add_tool_observation(
        tool_call_id=req.id,
        name="submit_finding",
        content=_observe_json({"ack": True, "candidate_index": len(session.candidates) - 1}),
    )


async def _finish_review(session: AgentSession, req: AgentToolRequest) -> None:
    try:
        FinishReviewArgs.model_validate(req.arguments or {"reason": "done"})
    except ValidationError:
        session.add_tool_observation(
            tool_call_id=req.id,
            name="finish_review",
            content=_observe_json({"error": "invalid_args"}),
        )
        return
    if session.status is ReviewTaskStatus.WAITING_TOOL:
        session.add_tool_observation(
            tool_call_id=req.id,
            name="finish_review",
            content=_observe_json({"error": "waiting_tool"}),
        )
        return
    session.stop_reason = str((req.arguments or {}).get("reason") or "finish_review")
    session.transition(ReviewTaskStatus.COMPLETED)
    session.add_tool_observation(
        tool_call_id=req.id,
        name="finish_review",
        content=_observe_json({"ack": True}),
    )
