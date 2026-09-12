"""Python 符号抽取：ast 优先，缩进扫描回退。"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

from reposage.domain.enums import SymbolKind
from reposage.domain.models import SymbolDef

EXTRACTOR_ID = "python-ast+indent-v2"
_DEF_LINE = re.compile(r"^([ \t]*)(async\s+def|def|class)\s+([A-Za-z_]\w*)")
_IMPORT_LINE = re.compile(
    r"^(\s*)("
    r"from\s+(\.*)\s*([A-Za-z0-9_.]*)\s+import\s+(.+)"
    r"|import\s+([A-Za-z0-9_.]+)(?:\s+as\s+([A-Za-z_]\w*))?"
    r")"
)


@dataclass(frozen=True)
class ImportSpec:
    """一条 import：保留 module / 导出名 / 本地别名 / 相对层级。"""

    module: str
    imported_name: str | None
    local_alias: str
    level: int
    star: bool = False


@dataclass(frozen=True)
class SymbolCacheKey:
    """缓存键五元组：repo + SHA + path + content hash + extractor。"""

    repository_id: str
    head_sha: str
    canonical_path: str
    content_hash: str
    extractor_id: str

    def as_str(self) -> str:
        return (
            f"{self.repository_id}|{self.head_sha}|{self.canonical_path}|"
            f"{self.content_hash}|{self.extractor_id}"
        )


class SymbolExtractor(Protocol):
    id: str

    def extract(self, path: str, source: str) -> list[SymbolDef]: ...


class PythonAstExtractor:
    id = EXTRACTOR_ID

    def extract(self, path: str, source: str) -> list[SymbolDef]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return _from_indent(path, source)
        return _from_ast(path, source, tree)


class SymbolIndex:
    """单 run 抽取缓存。"""

    def __init__(self, extractor: SymbolExtractor | None = None) -> None:
        self.extractor: SymbolExtractor = extractor or PythonAstExtractor()
        self._cache: dict[str, list[SymbolDef]] = {}

    def cache_key(
        self,
        path: str,
        source: str,
        *,
        repository_id: str,
        head_sha: str,
    ) -> SymbolCacheKey:
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        canonical = path.replace("\\", "/")
        return SymbolCacheKey(
            repository_id=repository_id,
            head_sha=head_sha,
            canonical_path=canonical,
            content_hash=digest,
            extractor_id=self.extractor.id,
        )

    def defs_for(
        self,
        path: str,
        source: str,
        *,
        repository_id: str,
        head_sha: str,
    ) -> list[SymbolDef]:
        key = self.cache_key(
            path, source, repository_id=repository_id, head_sha=head_sha
        ).as_str()
        if key not in self._cache:
            self._cache[key] = self.extractor.extract(path.replace("\\", "/"), source)
        return self._cache[key]


def _slice_sig(lines: list[str], start: int) -> str:
    if 0 < start <= len(lines):
        return lines[start - 1].rstrip()[:200]
    return ""


def _from_ast(path: str, source: str, tree: ast.AST) -> list[SymbolDef]:
    lines = source.splitlines()
    defs: list[SymbolDef] = []
    class_stack: list[str] = []
    fn_stack: list[str] = []

    def add(node: ast.AST, name: str, kind: SymbolKind, qualname: str) -> None:
        start = int(getattr(node, "lineno", 1) or 1)
        end = int(getattr(node, "end_lineno", start) or start)
        defs.append(
            SymbolDef(
                name=name,
                qualname=qualname,
                kind=kind,
                path=path,
                start_line=start,
                end_line=max(end, start),
                signature=_slice_sig(lines, start),
            )
        )

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            qual = ".".join([*class_stack, node.name])
            add(node, node.name, SymbolKind.CLASS, qual)
            class_stack.append(node.name)
            self.generic_visit(node)
            class_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_fn(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_fn(node)

        def _visit_fn(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            parts = [*class_stack, *fn_stack, node.name]
            qual = ".".join(parts)
            kind = SymbolKind.METHOD if class_stack and not fn_stack else SymbolKind.FUNCTION
            add(node, node.name, kind, qual)
            fn_stack.append(node.name)
            self.generic_visit(node)
            fn_stack.pop()

    Visitor().visit(tree)
    return defs


def _from_indent(path: str, source: str) -> list[SymbolDef]:
    lines = source.splitlines()
    starts: list[tuple[int, int, str, SymbolKind, str]] = []
    class_at: list[tuple[int, str]] = []
    for i, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        matched = _DEF_LINE.match(line)
        if not matched:
            continue
        ind = len(matched.group(1).expandtabs(4))
        while class_at and class_at[-1][0] >= ind:
            class_at.pop()
        raw = matched.group(2)
        name = matched.group(3)
        if "class" in raw:
            kind = SymbolKind.CLASS
            qual = name
            class_at.append((ind, name))
        elif class_at and ind > class_at[-1][0]:
            kind = SymbolKind.METHOD
            qual = f"{class_at[-1][1]}.{name}"
        else:
            kind = SymbolKind.FUNCTION
            qual = name
        starts.append((i, ind, name, kind, qual))
    defs: list[SymbolDef] = []
    for idx, (start_ln, ind, name, kind, qual) in enumerate(starts):
        end = len(lines)
        for nline, nind, *_rest in starts[idx + 1 :]:
            if nind <= ind:
                end = nline - 1
                break
        defs.append(
            SymbolDef(
                name=name,
                qualname=qual,
                kind=kind,
                path=path,
                start_line=start_ln,
                end_line=max(end, start_ln),
                signature=_slice_sig(lines, start_ln),
            )
        )
    return defs


def parse_imports(source: str) -> list[ImportSpec]:
    """解析 import；`import *` 标记 star，不展开。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _parse_imports_regex(source)
    rows: list[ImportSpec] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                rows.append(
                    ImportSpec(
                        module=alias.name,
                        imported_name=None,
                        local_alias=local,
                        level=0,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            level = int(node.level or 0)
            for alias in node.names:
                if alias.name == "*":
                    rows.append(
                        ImportSpec(
                            module=module,
                            imported_name=None,
                            local_alias="*",
                            level=level,
                            star=True,
                        )
                    )
                    continue
                local = alias.asname or alias.name
                rows.append(
                    ImportSpec(
                        module=module,
                        imported_name=alias.name,
                        local_alias=local,
                        level=level,
                    )
                )
    return rows


def _parse_imports_regex(source: str) -> list[ImportSpec]:
    rows: list[ImportSpec] = []
    for line in source.splitlines():
        matched = _IMPORT_LINE.match(line.split("#")[0])
        if not matched:
            continue
        if matched.group(6):
            module = matched.group(6)
            alias = matched.group(7)
            rows.append(
                ImportSpec(
                    module=module,
                    imported_name=None,
                    local_alias=alias or module.split(".")[0],
                    level=0,
                )
            )
            continue
        dots = matched.group(3) or ""
        module = (matched.group(4) or "").strip()
        names = matched.group(5) or ""
        level = len(dots)
        for part in names.split(","):
            chunk = part.strip()
            if not chunk:
                continue
            if " as " in chunk:
                imported, local = (p.strip() for p in chunk.split(" as ", 1))
            else:
                imported, local = chunk, chunk
            if imported == "*":
                rows.append(
                    ImportSpec(
                        module=module,
                        imported_name=None,
                        local_alias="*",
                        level=level,
                        star=True,
                    )
                )
            else:
                rows.append(
                    ImportSpec(
                        module=module,
                        imported_name=imported,
                        local_alias=local,
                        level=level,
                    )
                )
    return rows
