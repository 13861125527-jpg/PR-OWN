"""反馈记忆 CLI（V2-E）：mark / list / revoke。审查逻辑不在这里。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from reposage.config.settings import Settings, load_settings
from reposage.domain.enums import FeedbackKind, RevokeFeedbackResult
from reposage.domain.run import FeedbackMemory
from reposage.review.feedback import (
    FeedbackConfigError,
    memory_from_finding,
    validate_feedback_memory,
)
from reposage.storage.sqlite import SqliteStorage

feedback_app = typer.Typer(add_completion=False, no_args_is_help=True, help="仓库反馈记忆")

_REPO_DEFAULT = Path(".")


def _settings_and_store(repo: Path) -> tuple[Settings, SqliteStorage]:
    settings = load_settings(
        user_config=Path.home() / ".reposage.yaml",
        repo_config=repo / "reposage.yaml",
    )
    raw = Path(settings.storage.path)
    db_path = raw if raw.is_absolute() else repo / raw
    return settings, SqliteStorage(db_path)


def _echo_memory(memory: FeedbackMemory, *, verbose: bool) -> None:
    parts = [
        f"id={memory.id}",
        f"kind={memory.kind.value}",
        f"active={int(memory.active)}",
        f"path={memory.path or '-'}",
        f"scope={memory.scope or '-'}",
        f"category={memory.category or '-'}",
        f"rule_key={memory.rule_key or '-'}",
        f"symbol={memory.symbol or '-'}",
        f"pattern={memory.pattern or '-'}",
    ]
    if verbose:
        parts.append(f"rationale={memory.rationale}")
    typer.echo(" ".join(parts))


@feedback_app.command("mark")
def mark(
    kind: Annotated[FeedbackKind, typer.Option(help="false_positive | wont_fix | rule")],
    repo: Annotated[
        Path,
        typer.Option(
            "--repo",
            exists=True,
            file_okay=False,
            resolve_path=True,
            help="已解析的仓库根目录",
        ),
    ] = _REPO_DEFAULT,
    path: Annotated[str | None, typer.Option(help="精确相对路径")] = None,
    scope: Annotated[str | None, typer.Option(help="glob，如 src/**/*.py")] = None,
    symbol: Annotated[str | None, typer.Option(help="title/trigger 子串")] = None,
    category: Annotated[str | None, typer.Option(help="FindingCategory")] = None,
    rule_key: Annotated[str | None, typer.Option("--rule-key")] = None,
    pattern: Annotated[str | None, typer.Option(help="正则，匹配 title/trigger/explanation")] = None,
    cross_run_key: Annotated[
        str | None,
        typer.Option("--cross-run-key", help="精确 cross_run_match_key（行号锚，代码移动会失配）"),
    ] = None,
    rationale: Annotated[str, typer.Option(help="用户说明")] = "",
    from_finding: Annotated[
        str | None,
        typer.Option("--from-finding", help="从 finding_occurrence_id 抄 path/category/rule_id"),
    ] = None,
    with_cross_run_key: Annotated[
        bool,
        typer.Option("--with-cross-run-key", help="与 --from-finding 一起：额外抄 cross_run_match_key"),
    ] = False,
) -> None:
    settings, store = _settings_and_store(repo)
    try:
        memory = asyncio.run(
            _mark_async(
                store,
                settings,
                kind=kind,
                path=path,
                scope=scope,
                symbol=symbol,
                category=category,
                rule_key=rule_key,
                pattern=pattern,
                cross_run_key=cross_run_key,
                rationale=rationale,
                from_finding=from_finding,
                with_cross_run_key=with_cross_run_key,
            )
        )
    except FeedbackConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    finally:
        store.close()
    typer.echo(f"marked id={memory.id} kind={memory.kind.value}")


async def _mark_async(
    store: SqliteStorage,
    settings: Settings,
    *,
    kind: FeedbackKind,
    path: str | None,
    scope: str | None,
    symbol: str | None,
    category: str | None,
    rule_key: str | None,
    pattern: str | None,
    cross_run_key: str | None,
    rationale: str,
    from_finding: str | None,
    with_cross_run_key: bool,
) -> FeedbackMemory:
    repo_key = settings.project.name
    if from_finding:
        finding = await store.get_finding(from_finding)
        if finding is None:
            raise ValueError(f"finding 不存在: {from_finding}")
        memory = memory_from_finding(
            finding,
            kind=kind,
            repo=repo_key,
            rationale=rationale,
            with_cross_run_key=with_cross_run_key,
            path=path,
            scope=scope,
            symbol=symbol,
            category=category,
            rule_key=rule_key,
            pattern=pattern,
            cross_run_match_key=cross_run_key,
        )
    else:
        memory = FeedbackMemory(
            repo=repo_key,
            kind=kind,
            path=path,
            scope=scope,
            symbol=symbol,
            category=category,
            rule_key=rule_key,
            pattern=pattern,
            cross_run_match_key=cross_run_key,
            rationale=rationale,
        )
    validate_feedback_memory(memory)
    memory.id = await store.record_feedback(memory)
    return memory


@feedback_app.command("list")
def list_cmd(
    repo: Annotated[
        Path,
        typer.Option(
            "--repo",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ] = _REPO_DEFAULT,
    include_revoked: Annotated[bool, typer.Option("--include-revoked")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", help="打印 rationale")] = False,
) -> None:
    settings, store = _settings_and_store(repo)
    try:
        rows = asyncio.run(
            store.list_feedback(settings.project.name, include_revoked=include_revoked)
        )
    finally:
        store.close()
    if not rows:
        typer.echo("0")
        return
    for memory in rows:
        _echo_memory(memory, verbose=verbose)


@feedback_app.command("revoke")
def revoke(
    feedback_id: Annotated[int, typer.Option("--id", help="feedback 表主键")],
    repo: Annotated[
        Path,
        typer.Option(
            "--repo",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ] = _REPO_DEFAULT,
) -> None:
    settings, store = _settings_and_store(repo)
    try:
        result = asyncio.run(store.revoke_feedback(feedback_id, repo=settings.project.name))
    finally:
        store.close()
    if result in (RevokeFeedbackResult.REVOKED, RevokeFeedbackResult.ALREADY_REVOKED):
        typer.echo(f"revoked id={feedback_id}")
        return
    if result == RevokeFeedbackResult.WRONG_REPO:
        typer.echo(f"feedback id={feedback_id} does not belong to this repo", err=True)
    else:
        typer.echo(f"feedback id={feedback_id} not found", err=True)
    raise typer.Exit(code=1)
