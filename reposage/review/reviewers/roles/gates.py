"""确定性门控：从 ChangedFile 抽取 GateFeatures，按 RoleSpec.gate_id 判定（18 §5）。

matched_features 只保存规则 ID / combo 标签，不保存源码或密钥字面量。
gate_version = 规则集 content hash（regex/路径/组合/角色映射/提取算法）。
扫描每个 hunk 的新文件侧（context + added）；deleted 忽略。状态不跨 hunk。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from reposage.domain.enums import DiffLineType, GateReason
from reposage.domain.models import ChangedFile, DiffHunk, GateFeatures
from reposage.domain.run import GateDecision, RoleSpec

EXTRACT_ALGO = "v2-indent-scan"

# 词族：只匹配 added 行正文（非注释、非 docstring）
_KEYWORD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("exec_dyn", re.compile(r"\b(eval|exec|compile)\s*\(")),
    ("cmd", re.compile(r"\b(os\.system|os\.popen|subprocess\.)|shell\s*=\s*True")),
    ("deser", re.compile(r"\b(pickle|cloudpickle|marshal)\s*\.\s*loads?\s*\(|\byaml\.load\s*\(")),
    ("web_io", re.compile(r"\b(requests|httpx|urllib)\b|\burlopen\s*\(")),
    ("auth", re.compile(r"\b(jwt|oauth|password|api_key|os\.environ)\b", re.I)),
    ("sql", re.compile(r"\b(execute|executemany)\s*\(|f[\"'].*\bSELECT\b", re.I)),
    (
        "secret_lit",
        re.compile(
            r"(api[_-]?key|secret|password|passwd|token)\s*=\s*[\"'][A-Za-z0-9_\-\.]{8,}[\"']",
            re.I,
        ),
    ),
    (
        "user_input",
        re.compile(
            r"\b(request\.(args|files|form|json)|UploadFile|werkzeug|filename)\b"
        ),
    ),
    ("path_io", re.compile(r"\bopen\s*\(|\bPath\s*\(|\bos\.path\b|\bpathlib\b")),
    (
        "except_swallow",
        re.compile(
            r"except\s*:|except\s+Exception(\s+as\s+\w+)?\s*:\s*(pass|continue)\b"
        ),
    ),
    (
        "async_fire",
        re.compile(r"\basyncio\.create_task\b|\.add_done_callback\b"),
    ),
    ("none_index", re.compile(r"^\s*assert\s+")),
    ("heavy_collect", re.compile(r"\.read\(\)\s*\.split\b")),
    ("iter_product", re.compile(r"\bitertools\.product\s*\(")),
)

# 词法/结构扫描与 runtime 共用，全部进入 gate_manifest
_SCAN_PATTERNS: dict[str, re.Pattern[str]] = {
    "import": re.compile(r"^\s*(from\s+\S+\s+import|import\s+\S+)"),
    "comment": re.compile(r"^\s*#"),
    "doc_open": re.compile(r"^(\s*)[rRuUfF]*(?P<q>'''|\"\"\")(?P<rest>.*)$"),
    "except_head": re.compile(r"^(\s*)except(\s+Exception(\s+as\s+\w+)?)?\s*:\s*(?:#.*)?$"),
    "pass_cont": re.compile(r"^(\s*)(pass|continue)\b"),
    "loop": re.compile(r"^(\s*)(for|while)\b"),
}
# 新增 import 行映射到 security 规则；与 _security_hit 共用
_IMPORT_SECURITY_RE = re.compile(r"\b(pickle|subprocess|jwt)\b")
_PATH_AUTH_PARTS = ("auth", "security")
_PATH_AUTH_SUFFIXES = ("_view.py", "views.py")
_PATH_PERF_PARTS = ("cache", "batch", "worker")
_UPLOAD_PATH_PARTS = ("upload", "uploads", "extract", "tmp", "temp")
_SECURITY_HITS = (
    "exec_dyn",
    "cmd",
    "deser",
    "web_io",
    "auth",
    "sql",
    "secret_lit",
)
_CORRECTNESS_HITS = ("except_swallow", "async_fire", "none_index")
_PERFORMANCE_HITS = ("loop_nested", "heavy_collect", "iter_product")
_COMBO_SECURITY = ("path_auth", "path_io+user_input")


def _indent(text: str) -> int:
    expanded = text.expandtabs(4)
    return len(expanded) - len(expanded.lstrip(" "))


def _pattern_entry(name: str, pat: re.Pattern[str]) -> dict[str, Any]:
    return {"id": name, "pattern": pat.pattern, "flags": int(pat.flags)}


def _scan_hunk(hunk: DiffHunk) -> tuple[list[str], list[str], bool]:
    """新文件侧视图：context + added；deleted 忽略。状态只在本 hunk 内有效。

    仅 added 行贡献 keyword/import；context 只维护 docstring / 循环 / except 结构。
    """
    keyword_hits: list[str] = []
    imports: list[str] = []
    has_code_added = False
    in_doc = False
    quote = ""
    loop_stack: list[int] = []
    pending_except: tuple[int, bool] | None = None
    comment = _SCAN_PATTERNS["comment"]
    doc_open = _SCAN_PATTERNS["doc_open"]
    except_head = _SCAN_PATTERNS["except_head"]
    pass_cont = _SCAN_PATTERNS["pass_cont"]
    loop = _SCAN_PATTERNS["loop"]
    import_re = _SCAN_PATTERNS["import"]

    for line in hunk.lines:
        if line.type is DiffLineType.REMOVED:
            continue
        if line.type not in (DiffLineType.CONTEXT, DiffLineType.ADDED):
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

        ind = _indent(text)
        while loop_stack and ind <= loop_stack[-1]:
            loop_stack.pop()
        if pending_except is not None:
            base, head_added = pending_except
            if ind <= base:
                pending_except = None
            elif pass_cont.match(text):
                if (is_added or head_added) and "except_swallow" not in keyword_hits:
                    keyword_hits.append("except_swallow")
                pending_except = None
            else:
                pending_except = None
        if except_head.match(text):
            pending_except = (ind, is_added)
        is_loop = bool(loop.match(text))
        nested_here = bool(is_loop and loop_stack)
        if is_loop:
            loop_stack.append(ind)
        if not is_added:
            continue
        has_code_added = True
        if import_re.match(text):
            imports.append(text.strip())
        if nested_here and "loop_nested" not in keyword_hits:
            keyword_hits.append("loop_nested")
        for name, pattern in _KEYWORD_PATTERNS:
            if pattern.search(text) and name not in keyword_hits:
                keyword_hits.append(name)
    return keyword_hits, imports, has_code_added


def gate_manifest() -> dict[str, Any]:
    """决定 Gate 行为的规范化规则集（与 runtime 同一组 Pattern 对象）。"""
    return {
        "algo": EXTRACT_ALGO,
        "keywords": [_pattern_entry(name, pat) for name, pat in _KEYWORD_PATTERNS],
        "scan": [_pattern_entry(name, pat) for name, pat in _SCAN_PATTERNS.items()],
        "path_auth_parts": list(_PATH_AUTH_PARTS),
        "path_auth_suffixes": list(_PATH_AUTH_SUFFIXES),
        "path_perf_parts": list(_PATH_PERF_PARTS),
        "upload_path_parts": list(_UPLOAD_PATH_PARTS),
        "security_hits": list(_SECURITY_HITS),
        "correctness_hits": list(_CORRECTNESS_HITS),
        "performance_hits": list(_PERFORMANCE_HITS),
        "combo_security": list(_COMBO_SECURITY),
        "combo_rules": ["path_io+user_input", "path_auth", "path_perf"],
        "import_security": _pattern_entry("import_security", _IMPORT_SECURITY_RE),
    }


def compute_gate_version(manifest: dict[str, Any] | None = None) -> str:
    blob = json.dumps(manifest or gate_manifest(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


GATE_VERSION = compute_gate_version()


def extract_features(file: ChangedFile) -> GateFeatures:
    """从 ChangedFile 抽取门控事实（纯函数；不读盘、不解析 prompt）。"""
    keyword_hits: list[str] = []
    added_imports: list[str] = []
    has_code_added = False
    for hunk in file.hunks:
        hits, imports, added = _scan_hunk(hunk)
        has_code_added = has_code_added or added
        for name in hits:
            if name not in keyword_hits:
                keyword_hits.append(name)
        for item in imports:
            if item not in added_imports:
                added_imports.append(item)

    normalized = file.path.replace("\\", "/")
    parts = [p.lower() for p in normalized.split("/") if p]
    combo_hits: list[str] = []
    path_auth = any(p in _PATH_AUTH_PARTS for p in parts) or any(
        normalized.endswith(suf) for suf in _PATH_AUTH_SUFFIXES
    )
    path_perf = any(p in _PATH_PERF_PARTS for p in parts)
    upload_path = any(p in _UPLOAD_PATH_PARTS for p in parts)
    if path_auth:
        combo_hits.append("path_auth")
    if path_perf:
        combo_hits.append("path_perf")
    path_io = "path_io" in keyword_hits
    combo_partner = (
        "user_input" in keyword_hits
        or "web_io" in keyword_hits
        or "auth" in keyword_hits
        or path_auth
        or upload_path
    )
    if path_io and combo_partner:
        combo_hits.append("path_io+user_input")

    return GateFeatures(
        path=file.path,
        language=file.language,
        status=file.status,
        path_parts=parts,
        added_imports=added_imports,
        keyword_hits=keyword_hits,
        combo_hits=combo_hits,
        has_added_lines=has_code_added,
    )


def _language_ok(features: GateFeatures, spec: RoleSpec, languages: list[str]) -> bool:
    allowed = spec.languages or languages
    if features.language is None:
        return False
    return features.language in allowed


def _security_hit(features: GateFeatures) -> list[str]:
    hits: list[str] = []
    for name in _SECURITY_HITS:
        if name in features.keyword_hits:
            hits.append(name)
    for combo in features.combo_hits:
        if combo in _COMBO_SECURITY:
            hits.append(combo)
    joined = " ".join(features.added_imports)
    if _IMPORT_SECURITY_RE.search(joined):
        if "pickle" in joined and "deser" not in hits:
            hits.append("deser")
        if "subprocess" in joined and "cmd" not in hits:
            hits.append("cmd")
        if "jwt" in joined and "auth" not in hits:
            hits.append("auth")
    return hits


def _correctness_hit(features: GateFeatures) -> list[str]:
    return [name for name in _CORRECTNESS_HITS if name in features.keyword_hits]


def _performance_hit(features: GateFeatures) -> list[str]:
    hits: list[str] = [name for name in _PERFORMANCE_HITS if name in features.keyword_hits]
    if "path_perf" in features.combo_hits:
        hits.append("path_perf")
    return hits


def evaluate_gate(
    features: GateFeatures,
    spec: RoleSpec,
    languages: list[str],
) -> GateDecision:
    """(features, spec, languages) → GateDecision；同一输入永远同一输出。"""
    if not _language_ok(features, spec, languages):
        return GateDecision(
            role_id=spec.id,
            file_path=features.path,
            enabled=False,
            reason=GateReason.LANG_MISS.value,
            gate_version=GATE_VERSION,
        )
    gate_id = spec.gate_id
    if gate_id == "always":
        return GateDecision(
            role_id=spec.id,
            file_path=features.path,
            enabled=True,
            reason=GateReason.ALWAYS.value,
            gate_version=GATE_VERSION,
        )
    if not features.has_added_lines:
        return GateDecision(
            role_id=spec.id,
            file_path=features.path,
            enabled=False,
            reason=GateReason.NO_ADDED_LINES.value,
            gate_version=GATE_VERSION,
        )
    if gate_id == "added_lines":
        return GateDecision(
            role_id=spec.id,
            file_path=features.path,
            enabled=True,
            matched_features=["has_added_lines"],
            reason=GateReason.GATE_HIT.value,
            gate_version=GATE_VERSION,
        )
    if gate_id == "security":
        matched = _security_hit(features)
    elif gate_id == "correctness":
        matched = _correctness_hit(features)
    elif gate_id == "performance":
        matched = _performance_hit(features)
    else:
        matched = []
    enabled = bool(matched)
    return GateDecision(
        role_id=spec.id,
        file_path=features.path,
        enabled=enabled,
        matched_features=matched,
        reason=GateReason.GATE_HIT.value if enabled else GateReason.GATE_MISS.value,
        gate_version=GATE_VERSION,
    )


__all__ = [
    "EXTRACT_ALGO",
    "GATE_VERSION",
    "compute_gate_version",
    "evaluate_gate",
    "extract_features",
    "gate_manifest",
]
