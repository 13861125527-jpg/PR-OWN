"""路径沙箱（V3-A，10 §4）。错误文本不回显仓库绝对路径。"""

from __future__ import annotations

import re
from pathlib import Path

from reposage.domain.models import is_safe_repo_path


class SandboxError(Exception):
    """路径未通过沙箱。"""

    def __init__(self, code: str = "invalid_path") -> None:
        self.code = code
        super().__init__(code)


def normalize_repo_path(path: str) -> str:
    """字符串级规范化；失败抛 SandboxError。"""
    if not path or not isinstance(path, str):
        raise SandboxError("invalid_path")
    posix = path.replace("\\", "/")
    if posix.startswith(("/", "\\")):
        raise SandboxError("invalid_path")
    if len(posix) >= 2 and posix[1] == ":":
        raise SandboxError("invalid_path")
    parts = [p for p in posix.split("/") if p not in ("", ".")]
    if not parts:
        raise SandboxError("invalid_path")
    candidate = "/".join(parts)
    if not is_safe_repo_path(candidate):
        raise SandboxError("invalid_path")
    return candidate


def glob_is_safe(pattern: str, *, max_len: int = 256) -> str:
    """校验 glob：拒绝对路径、`..`、过长。返回 posix pattern。"""
    if not pattern or len(pattern) > max_len:
        raise SandboxError("invalid_path")
    posix = pattern.replace("\\", "/")
    if posix.startswith("/") or (len(posix) >= 2 and posix[1] == ":"):
        raise SandboxError("invalid_path")
    for part in posix.split("/"):
        if part == "..":
            raise SandboxError("invalid_path")
    return posix


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """把受控 glob（*, ?, **）编译为正则；不含用户字符类。"""
    i = 0
    out: list[str] = []
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*" and i + 1 < len(pattern) and pattern[i + 1] == "*":
            if i + 2 < len(pattern) and pattern[i + 2] == "/":
                out.append("(?:.*/)?")
                i += 3
                continue
            out.append(".*")
            i += 2
            continue
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch in r".+^$()[]{}|\\":
            out.append("\\" + ch)
        else:
            out.append(ch)
        i += 1
    return re.compile("^" + "".join(out) + "$")


def match_repo_glob(path: str, pattern: str) -> bool:
    safe = glob_is_safe(pattern)
    return glob_to_regex(safe).fullmatch(path) is not None


def resolve_in_workspace(repo_root: Path | None, path: str) -> str:
    """规范化 + 可选落盘 resolve，必须仍在 workspace 内。

    ``repo_root is None``：远程/Fake 模式，只做字符串级校验，路径权威在 snapshot。
    """
    normalized = normalize_repo_path(path)
    if repo_root is None:
        return normalized
    root = repo_root.resolve()
    if not root.exists():
        return normalized
    resolved = (root / normalized).resolve()
    if not resolved.is_relative_to(root):
        raise SandboxError("invalid_path")
    return normalized
