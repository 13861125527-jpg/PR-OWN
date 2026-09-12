"""从 changed file + HeadSnapshot 收集最小 L3 命中。"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from reposage.domain.enums import DiffLineType, L3HitReason, SymbolKind
from reposage.domain.models import ChangedFile, L3Hit, SymbolDef
from reposage.review.reviewers.roles.gates import _SCAN_PATTERNS

from .extract import ImportSpec, SymbolIndex, parse_imports
from .snapshot import HeadSnapshot, PathFailure, is_python_path, is_test_path

_TEST_RADIUS = 15
_MAX_IMPORT_TARGETS = 8
_MAX_CROSS_PATHS = 2
_MAX_TEST_FILES = 3
_NAME_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")


@dataclass
class L3CollectionResult:
    """检索结果：命中 + 结构化失败（无源码）。"""

    hits: list[L3Hit] = field(default_factory=list)
    failures: list[PathFailure] = field(default_factory=list)


def added_used_names(file: ChangedFile, source: str) -> set[str]:
    """changed hunk added 行上的标识符，供 prepare 过滤 import。"""
    return _used_names(source, set(_added_code_lines(file)))


def resolve_import_paths(
    module: str,
    level: int,
    from_path: str,
    path_set: set[str],
) -> list[str]:
    """把 import 解析为快照内候选 path（不联网）。"""
    base = PurePosixPath(from_path.replace("\\", "/")).parent
    if level:
        pkg = base
        for _ in range(level - 1):
            pkg = pkg.parent
        rel = str(pkg / module.replace(".", "/")) if module else str(pkg)
    else:
        rel = module.replace(".", "/")
    rel = rel.replace("\\", "/").strip("/")
    candidates = [f"{rel}.py", f"{rel}/__init__.py"]
    return [c for c in candidates if c in path_set]


def _hunk_new_ranges(file: ChangedFile) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for hunk in file.hunks:
        nums = [ln.new_ln for ln in hunk.lines if ln.new_ln is not None]
        if nums:
            ranges.append((min(nums), max(nums)))
    return ranges


def _fully_covered(defn: SymbolDef, ranges: list[tuple[int, int]]) -> bool:
    return any(lo <= defn.start_line and defn.end_line <= hi for lo, hi in ranges)


def _added_code_lines(file: ChangedFile) -> list[int]:
    """added 且非注释/docstring/空行的新行号。"""
    comment = _SCAN_PATTERNS["comment"]
    doc_open = _SCAN_PATTERNS["doc_open"]
    lines: list[int] = []
    for hunk in file.hunks:
        in_doc = False
        quote = ""
        for line in hunk.lines:
            if line.type is DiffLineType.REMOVED:
                continue
            text = line.content
            is_added = line.type is DiffLineType.ADDED
            if in_doc:
                if quote and quote in text:
                    in_doc = False
                    quote = ""
                continue
            stripped = text.strip()
            if not stripped:
                continue
            if comment.match(text):
                continue
            opened = doc_open.match(text)
            if opened:
                q = opened.group("q")
                rest = opened.group("rest")
                if q not in rest:
                    in_doc = True
                    quote = q
                continue
            if is_added and line.new_ln is not None:
                lines.append(line.new_ln)
    return lines


def _enclosing(defs: list[SymbolDef], line: int) -> SymbolDef | None:
    covering = [d for d in defs if d.start_line <= line <= d.end_line]
    if not covering:
        return None
    covering.sort(key=lambda d: (d.end_line - d.start_line, -d.start_line))
    return covering[0]


def _module_symbol(path: str, source: str) -> SymbolDef:
    lines = source.splitlines() or [""]
    name = PurePosixPath(path.replace("\\", "/")).stem
    return SymbolDef(
        name=name,
        qualname=name,
        kind=SymbolKind.MODULE,
        path=path.replace("\\", "/"),
        start_line=1,
        end_line=max(1, len(lines)),
        signature=f"module:{path}",
    )


def _used_names(source: str, line_nos: set[int]) -> set[str]:
    names: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        for i, line in enumerate(source.splitlines(), start=1):
            if i in line_nos:
                names.update(_NAME_RE.findall(line))
        return names

    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", None)
        if lineno not in line_nos:
            continue
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
            cur: ast.AST = node.value
            while isinstance(cur, ast.Attribute):
                names.add(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                names.add(cur.id)
    return names


def _modified_symbols(file: ChangedFile, defs: list[SymbolDef], source: str) -> list[SymbolDef]:
    seen: set[tuple[str, int, int]] = set()
    out: list[SymbolDef] = []
    path = file.path.replace("\\", "/")
    for new_ln in _added_code_lines(file):
        hit = _enclosing(defs, new_ln)
        if hit is None:
            hit = _module_symbol(path, source)
        key = (hit.qualname, hit.start_line, hit.end_line)
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out


def seed_names_for_file(file: ChangedFile, source: str, defs: list[SymbolDef]) -> set[str]:
    """modified symbols + added 行用到的名字；供 snapshot 路径启发式。"""
    modified = _modified_symbols(file, defs, source)
    names = {d.name for d in modified}
    names |= _used_names(source, set(_added_code_lines(file)))
    return {n for n in names if n}


def used_import_targets(
    path: str,
    specs: list[ImportSpec],
    path_set: set[str],
    used: set[str],
) -> list[str]:
    """只解析 added 行实际用到的 import 别名对应的快照 path。"""
    targets: list[str] = []
    for spec in specs:
        if spec.star or spec.local_alias not in used:
            continue
        targets.extend(resolve_import_paths(spec.module, spec.level, path, path_set))
    return targets


def _snippet(source: str, start: int, end: int) -> str:
    lines = source.splitlines()
    lo = max(1, start)
    hi = min(len(lines), max(end, lo))
    return "\n".join(lines[lo - 1 : hi])


def _test_window(source: str, name: str, radius: int = _TEST_RADIUS) -> str | None:
    lines = source.splitlines()
    pat = re.compile(rf"\b{re.escape(name)}\b")
    for i, line in enumerate(lines):
        if pat.search(line):
            lo = max(0, i - radius)
            hi = min(len(lines), i + radius + 1)
            return "\n".join(lines[lo:hi])
    return None


def _wanted_from_spec(spec: ImportSpec, used: set[str]) -> set[str]:
    if spec.star or spec.local_alias not in used:
        return set()
    wanted: set[str] = set()
    if spec.imported_name:
        wanted.add(spec.imported_name)
    else:
        wanted.update(n for n in used if n != spec.local_alias)
    return wanted


def collect_l3_hits(
    file: ChangedFile,
    snapshot: HeadSnapshot,
    *,
    index: SymbolIndex | None = None,
    l2_ranges: list[tuple[int, int]] | None = None,
) -> L3CollectionResult:
    """确定性最小 L3：定义 / 直接 import / 相关测试。"""
    failures: list[PathFailure] = []
    if file.language not in (None, "python") and not is_python_path(file.path):
        return L3CollectionResult()
    if file.language and file.language != "python":
        return L3CollectionResult()
    index = index or SymbolIndex()
    path = file.path.replace("\\", "/")
    source = snapshot.get(path)
    if not source:
        return L3CollectionResult()
    repo = snapshot.repository_id
    sha = snapshot.sha

    def defs_or_fail(target: str, src: str) -> list[SymbolDef]:
        try:
            return index.defs_for(target, src, repository_id=repo, head_sha=sha)
        except Exception as exc:
            failures.append(PathFailure(path=target, reason=type(exc).__name__))
            return []

    defs = defs_or_fail(path, source)
    modified = _modified_symbols(file, defs, source)
    ranges = l2_ranges if l2_ranges is not None else _hunk_new_ranges(file)
    added_lines = set(_added_code_lines(file))
    used = _used_names(source, added_lines)
    hits: list[L3Hit] = []
    seen: set[tuple[str, int, int, str]] = set()

    def add(hit: L3Hit) -> None:
        key = (hit.symbol.path, hit.symbol.start_line, hit.symbol.end_line, hit.reason.value)
        if key in seen:
            return
        seen.add(key)
        hits.append(hit)

    for defn in modified:
        if defn.kind is SymbolKind.MODULE:
            continue
        src = snapshot.get(defn.path) or source
        if not _fully_covered(defn, ranges):
            add(
                L3Hit(
                    symbol=defn,
                    reason=L3HitReason.DEFINITION,
                    snippet=_snippet(src, defn.start_line, defn.end_line),
                )
            )

    names = {d.name for d in modified if d.kind is not SymbolKind.MODULE}
    specs = parse_imports(source)
    imported_used: set[str] = set()
    for spec in specs:
        imported_used |= _wanted_from_spec(spec, used)
    qualnames = {d.qualname for d in modified if d.kind is not SymbolKind.MODULE}
    grouped: dict[str, list[tuple[SymbolDef, str]]] = {}
    for other in snapshot.blobs:
        if other == path or not is_python_path(other):
            continue
        other_src = snapshot.get(other)
        if not other_src:
            continue
        for defn in defs_or_fail(other, other_src):
            if defn.qualname in qualnames or defn.name in names:
                grouped.setdefault(defn.name, []).append((defn, other_src))
    for _name, group in grouped.items():
        notes = None
        paths = {d.path for d, _src in group}
        if len(paths) > 1:
            notes = "证据冲突: " + " 与 ".join(f"{d.path}:{d.qualname}" for d, _src in group)
        for defn, src in group[:_MAX_CROSS_PATHS]:
            add(
                L3Hit(
                    symbol=defn,
                    reason=L3HitReason.DEFINITION,
                    snippet=_snippet(src, defn.start_line, defn.end_line),
                    notes=notes,
                )
            )

    import_added = 0
    for spec in specs:
        if import_added >= _MAX_IMPORT_TARGETS:
            break
        if spec.star:
            continue
        wanted = _wanted_from_spec(spec, used)
        if not wanted:
            continue
        targets = resolve_import_paths(spec.module, spec.level, path, snapshot.path_set)
        for target in targets:
            tsrc = snapshot.get(target)
            if not tsrc:
                continue
            tdefs = defs_or_fail(target, tsrc)
            chosen = [d for d in tdefs if d.name in wanted or d.qualname in wanted]
            for defn in chosen:
                add(
                    L3Hit(
                        symbol=defn,
                        reason=L3HitReason.IMPORT,
                        snippet=_snippet(tsrc, defn.start_line, defn.end_line),
                    )
                )
                import_added += 1
                if import_added >= _MAX_IMPORT_TARGETS:
                    break

    test_files = 0
    for tpath in sorted(snapshot.blobs):
        if test_files >= _MAX_TEST_FILES:
            break
        if not is_test_path(tpath):
            continue
        tsrc = snapshot.blobs[tpath]
        for name in sorted(names | imported_used):
            window = _test_window(tsrc, name)
            if not window:
                continue
            add(
                L3Hit(
                    symbol=SymbolDef(
                        name=name,
                        qualname=name,
                        kind=SymbolKind.FUNCTION,
                        path=tpath,
                        start_line=1,
                        end_line=window.count("\n") + 1,
                        signature=f"test:{name}",
                    ),
                    reason=L3HitReason.TEST,
                    snippet=window,
                )
            )
            test_files += 1
            break
    return L3CollectionResult(hits=hits, failures=failures)
