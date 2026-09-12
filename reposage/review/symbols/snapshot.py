"""Head 快照：惰性读 blob，路径沙箱，单 run 内复用。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from reposage.domain.models import ChangedFile, is_safe_repo_path
from reposage.domain.protocols import GitSnapshotProvider

MAX_L3_FILES_READ = 40


@dataclass(frozen=True)
class PathFailure:
    path: str
    reason: str


@dataclass
class RetrievalDiagnostics:
    """L3 准备诊断：cap 与 IO 分开，不含源码。"""

    repository_id: str = ""
    head_sha: str = ""
    requests_attempted: int = 0
    blobs_read: int = 0
    candidates_considered: int = 0
    blobs_kept: int = 0
    skipped_by_cap: int = 0
    truncated_by_cap: bool = False
    capability_miss: bool = False
    io_failures: list[PathFailure] = field(default_factory=list)
    cap_affected_paths: set[str] = field(default_factory=set)


@dataclass
class HeadSnapshot:
    """某 SHA 的按需文件视图（不把全文写入 SQLite）。"""

    sha: str
    repository_id: str = ""
    paths: tuple[str, ...] = ()
    blobs: dict[str, str] = field(default_factory=dict)
    max_files_read: int = MAX_L3_FILES_READ
    diagnostics: RetrievalDiagnostics = field(default_factory=RetrievalDiagnostics)

    @property
    def path_set(self) -> set[str]:
        return set(self.paths)

    @property
    def files_read(self) -> int:
        return self.diagnostics.blobs_read

    @property
    def truncated(self) -> bool:
        return self.diagnostics.truncated_by_cap

    def get(self, path: str) -> str | None:
        return self.blobs.get(path.replace("\\", "/"))

    @classmethod
    def from_blobs(
        cls,
        sha: str,
        blobs: dict[str, str],
        *,
        repository_id: str = "eval",
        max_files_read: int = MAX_L3_FILES_READ,
    ) -> HeadSnapshot:
        safe = {p.replace("\\", "/"): c for p, c in blobs.items() if is_safe_repo_path(p)}
        diag = RetrievalDiagnostics(
            repository_id=repository_id,
            head_sha=sha,
            requests_attempted=len(safe),
            blobs_read=len(safe),
            blobs_kept=len(safe),
        )
        return cls(
            sha=sha,
            repository_id=repository_id,
            paths=tuple(sorted(safe)),
            blobs=dict(safe),
            max_files_read=max_files_read,
            diagnostics=diag,
        )

    async def fetch(
        self,
        git: GitSnapshotProvider,
        path: str,
        *,
        owners: set[str] | None = None,
    ) -> str | None:
        path = path.replace("\\", "/")
        if not is_safe_repo_path(path):
            return None
        if path in self.blobs:
            return self.blobs[path]
        if self.diagnostics.blobs_read >= self.max_files_read:
            self.diagnostics.truncated_by_cap = True
            self.diagnostics.skipped_by_cap += 1
            for owner in owners or ():
                self.diagnostics.cap_affected_paths.add(owner)
            return None
        self.diagnostics.requests_attempted += 1
        try:
            blob = await git.get_blob(self.sha, path)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.diagnostics.io_failures.append(
                PathFailure(path=path, reason=type(exc).__name__)
            )
            return None
        if blob is None:
            return None
        self.diagnostics.blobs_read += 1
        self.blobs[path] = blob
        return blob


@dataclass
class PreparedL3Snapshot:
    snapshot: HeadSnapshot
    diagnostics: RetrievalDiagnostics


def is_python_path(path: str) -> bool:
    return path.replace("\\", "/").endswith(".py")


def is_test_path(path: str) -> bool:
    norm = path.replace("\\", "/")
    if not norm.endswith(".py"):
        return False
    name = norm.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py") or "/tests/" in f"/{norm}"


def _test_path_priority(path: str, names: set[str]) -> int | None:
    """路径启发式：只给名字相关的测试打分；无关测试返回 None（不读）。"""
    norm = path.replace("\\", "/")
    base = norm.rsplit("/", 1)[-1]
    stem = base[:-3] if base.endswith(".py") else base
    for name in names:
        if stem in {f"test_{name}", f"{name}_test"}:
            return 0
        if name and name in norm:
            return 1
    return None


def _def_path_priority(path: str, names: set[str]) -> int | None:
    norm = path.replace("\\", "/")
    base = norm.rsplit("/", 1)[-1]
    for name in names:
        if base == f"{name}.py" or norm.endswith(f"/{name}/__init__.py"):
            return 0
    return None


@dataclass(frozen=True)
class SnapshotCandidate:
    path: str
    priority: int
    owners: frozenset[str]


def rank_snapshot_candidates(
    *,
    path_set: set[str],
    changed_paths: set[str],
    import_targets: dict[str, set[str]],
    name_owners: dict[str, set[str]],
) -> list[SnapshotCandidate]:
    """稳定排序：import 目标 → 同名模块 → 名字相关测试。候选携带 changed-file owners。"""
    names = set(name_owners)
    scored: dict[str, int] = {}
    owners: dict[str, set[str]] = {}

    def add(path: str, priority: int, path_owners: set[str]) -> None:
        scored[path] = min(scored.get(path, 99), priority)
        owners.setdefault(path, set()).update(path_owners)

    for target, target_owners in import_targets.items():
        if target in path_set and target not in changed_paths:
            add(target, 0, target_owners)
    for path in path_set:
        if path in changed_paths or not is_python_path(path):
            continue
        prio = _def_path_priority(path, names)
        if prio is not None:
            path_owners = set()
            for name, name_from in name_owners.items():
                if _def_path_priority(path, {name}) is not None:
                    path_owners |= name_from
            add(path, 1 + prio, path_owners or set(changed_paths))
            continue
        if is_test_path(path):
            tprio = _test_path_priority(path, names)
            if tprio is not None:
                path_owners = set()
                for name, name_from in name_owners.items():
                    if _test_path_priority(path, {name}) is not None:
                        path_owners |= name_from
                add(path, 10 + tprio, path_owners or set(changed_paths))
    return [
        SnapshotCandidate(path=p, priority=prio, owners=frozenset(owners.get(p, set())))
        for p, prio in sorted(scored.items(), key=lambda item: (item[1], item[0]))
    ]


def rank_candidate_paths(
    *,
    path_set: set[str],
    changed_paths: set[str],
    import_targets: list[str],
    names: set[str],
) -> list[str]:
    """测试兼容：只返回排序后的 path。"""
    owner = next(iter(changed_paths), "")
    ranked = rank_snapshot_candidates(
        path_set=path_set,
        changed_paths=changed_paths,
        import_targets={p: {owner} for p in import_targets},
        name_owners={n: {owner} for n in names},
    )
    return [c.path for c in ranked]


async def prepare_l3_snapshot(
    git: GitSnapshotProvider,
    sha: str,
    changed: list[ChangedFile],
    *,
    repository_id: str,
    max_files_read: int = MAX_L3_FILES_READ,
) -> PreparedL3Snapshot:
    """异步准备：先读 changed，再按稳定候选排序读取，cap 与 IO 分开。"""
    diag = RetrievalDiagnostics(repository_id=repository_id, head_sha=sha)
    try:
        listed = await git.list_paths(sha)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        diag.io_failures.append(PathFailure(path=".", reason=type(exc).__name__))
        snap = HeadSnapshot(
            sha=sha, repository_id=repository_id, diagnostics=diag, max_files_read=max_files_read
        )
        return PreparedL3Snapshot(snapshot=snap, diagnostics=diag)

    paths = tuple(sorted({p.replace("\\", "/") for p in listed if is_safe_repo_path(p)}))
    snap = HeadSnapshot(
        sha=sha,
        repository_id=repository_id,
        paths=paths,
        max_files_read=max_files_read,
        diagnostics=diag,
    )
    path_set = set(paths)

    changed_py = sorted(
        {
            f.path.replace("\\", "/")
            for f in changed
            if is_python_path(f.path) or f.language == "python"
        }
    )
    for path in changed_py:
        await snap.fetch(git, path, owners={path})

    from .extract import SymbolIndex, parse_imports
    from .retrieve import added_used_names, seed_names_for_file, used_import_targets

    index = SymbolIndex()
    name_owners: dict[str, set[str]] = {}
    import_targets: dict[str, set[str]] = {}
    changed_by_path = {f.path.replace("\\", "/"): f for f in changed}
    for path in changed_py:
        src = snap.get(path)
        if not src:
            continue
        try:
            defs = index.defs_for(path, src, repository_id=repository_id, head_sha=sha)
        except Exception:
            defs = []
        file = changed_by_path.get(path)
        used: set[str] = set()
        if file is not None:
            used = added_used_names(file, src)
            for name in seed_names_for_file(file, src, defs):
                name_owners.setdefault(name, set()).add(path)
        specs = parse_imports(src)
        for target in used_import_targets(path, specs, path_set, used):
            import_targets.setdefault(target, set()).add(path)

    ranked = rank_snapshot_candidates(
        path_set=path_set,
        changed_paths=set(changed_py),
        import_targets=import_targets,
        name_owners=name_owners,
    )
    diag.candidates_considered = len(ranked)
    for cand in ranked:
        if diag.blobs_read >= max_files_read:
            diag.truncated_by_cap = True
            diag.skipped_by_cap += 1
            diag.cap_affected_paths.update(cand.owners)
            continue
        await snap.fetch(git, cand.path, owners=set(cand.owners))
    diag.blobs_kept = len(snap.blobs)
    return PreparedL3Snapshot(snapshot=snap, diagnostics=diag)


async def hydrate_head_snapshot(
    git: GitSnapshotProvider,
    sha: str,
    changed: list[ChangedFile],
    *,
    repository_id: str = "unknown",
    max_files_read: int = MAX_L3_FILES_READ,
) -> HeadSnapshot:
    """兼容入口：返回 snapshot。CancelledError 原样传播。"""
    prepared = await prepare_l3_snapshot(
        git, sha, changed, repository_id=repository_id, max_files_read=max_files_read
    )
    return prepared.snapshot
