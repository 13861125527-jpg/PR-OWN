"""RepoSage CLI for local ranges and GitHub Actions PR reviews."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated, Any

import typer

from reposage.app.compose import compose_service
from reposage.app.feedback_cli import feedback_app
from reposage.config.settings import load_settings
from reposage.domain.finding import Finding
from reposage.providers.git.github_action import GitHubActionProvider
from reposage.providers.git.local import LocalGitProvider, LocalGitSnapshotProvider
from reposage.providers.llm.openai_compat import OpenAICompatProvider
from reposage.storage.sqlite import SqliteStorage

app = typer.Typer(add_completion=False, no_args_is_help=True)
app.add_typer(feedback_app, name="feedback")


def _result_payload(run: Any, findings: list[Finding]) -> dict[str, Any]:
    return {
        "run": run.model_dump(mode="json"),
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }


def _markdown(payload: dict[str, Any]) -> str:
    run = payload["run"]
    findings = payload["findings"]
    lines = [
        "# RepoSage review",
        "",
        f"- Status: `{run['status']}`",
        f"- Strategy: `{run['strategy']}`",
        f"- Findings: **{len(findings)}**",
        "",
    ]
    for finding in findings:
        path = finding.get("canonical_path") or finding.get("claimed_path") or "unknown"
        line = finding.get("canonical_start_line") or finding.get("claimed_start_line") or "?"
        lines.extend(
            [
                f"## [{finding['severity']}] {finding['title']}",
                "",
                f"`{path}:{line}` · `{finding['category']}`",
                "",
                str(finding.get("explanation") or ""),
                "",
                f"建议：{finding.get('suggestion') or '无'}",
                "",
            ]
        )
    if run.get("warnings"):
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {warning}" for warning in run["warnings"])
    return "\n".join(lines)


async def _review(
    *,
    repo: Path,
    base: str | None,
    head: str | None,
    pull_request: int | None,
    publish: bool,
    strategy: str,
    model: str | None,
    output: Path,
) -> int:
    if pull_request is None and (not base or not head):
        raise typer.BadParameter("本地模式必须同时提供 --base 和 --head")
    if pull_request is not None and (base or head):
        raise typer.BadParameter("--pr 与 --base/--head 不能同时使用")
    repository = os.environ.get("GITHUB_REPOSITORY", "") if pull_request is not None else ""
    overrides: dict[str, Any] = {
        "project": {
            "mode": "github_action" if pull_request is not None else "local_cli",
            "name": repository if repository else "RepoSage",
        },
        "review": {"strategy": strategy},
        "agent": {"enabled": strategy == "agentic"},
        "publishing": {"dry_run": not publish, "request_changes": False},
    }
    if model:
        overrides["llm"] = {"model": model}
    settings = load_settings(repo_config=repo / "reposage.yaml", overrides=overrides)
    llm = OpenAICompatProvider.from_config(settings.llm)
    git: LocalGitProvider
    if pull_request is None:
        snapshot = LocalGitSnapshotProvider(repo)
        git = LocalGitProvider(repo, snapshot=snapshot)
        ref = f"{base}..{head}"
    else:
        token = os.environ.get(settings.git.token_env, "")
        api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        snapshot = LocalGitSnapshotProvider(repo)
        git = GitHubActionProvider(
            str(repo),
            repository=repository,
            pull_number=pull_request,
            token=token,
            api_url=api_url,
            snapshot=snapshot,
        )
        ref = str(pull_request)
    storage = SqliteStorage(settings.storage.path)
    service = compose_service(git, snapshot, llm=llm, storage=storage, settings=settings)
    try:
        run, findings = await service.review(ref, dry_run=not publish)
    finally:
        await llm.aclose()
        close_git = getattr(git, "aclose", None)
        if callable(close_git):
            await close_git()
    payload = _result_payload(run, findings)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    rendered = _markdown(payload)
    typer.echo(rendered)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as stream:
            stream.write(rendered + "\n")
    return 0 if run.status.value in {"completed", "partial"} else 1


@app.command()
def review(
    repo: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)] = Path("."),
    base: Annotated[str | None, typer.Option(help="本地审查 base ref")] = None,
    head: Annotated[str | None, typer.Option(help="本地审查 head ref")] = None,
    pull_request: Annotated[int | None, typer.Option("--pr", help="GitHub PR number")] = None,
    publish: Annotated[bool, typer.Option(help="发布 GitHub 评论；省略时 dry-run")] = False,
    strategy: Annotated[str, typer.Option(help="single_pass | multi_role | agentic")] = "agentic",
    model: Annotated[str | None, typer.Option(help="覆盖配置中的模型名")] = None,
    output: Annotated[Path, typer.Option(help="JSON 结果路径")] = Path("reposage-review.json"),
) -> None:
    """Review a local commit range or the current GitHub Actions PR."""
    if strategy not in {"single_pass", "multi_role", "agentic"}:
        raise typer.BadParameter("strategy 必须是 single_pass、multi_role 或 agentic")
    raise typer.Exit(
        asyncio.run(
            _review(
                repo=repo,
                base=base,
                head=head,
                pull_request=pull_request,
                publish=publish,
                strategy=strategy,
                model=model,
                output=output,
            )
        )
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
