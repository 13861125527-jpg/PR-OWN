"""Prompt 加载（V2-A：角色文件；V2-D：task 文件）。"""

from __future__ import annotations

import hashlib
from pathlib import Path

_ROLES_DIR = Path(__file__).resolve().parent / "roles"
_TASKS_DIR = Path(__file__).resolve().parent / "tasks"


def load_role_prompt(role_id: str) -> str:
    path = _ROLES_DIR / f"{role_id}.md"
    if not path.is_file():
        raise FileNotFoundError(f"缺少角色 prompt: {path}")
    return path.read_text(encoding="utf-8")


def role_prompt_hash(role_id: str) -> str:
    return hashlib.sha256(load_role_prompt(role_id).encode("utf-8")).hexdigest()


def load_task_prompt(task_id: str) -> str:
    path = _TASKS_DIR / f"{task_id}.md"
    if not path.is_file():
        raise FileNotFoundError(f"缺少任务 prompt: {path}")
    return path.read_text(encoding="utf-8")


def task_prompt_hash(task_id: str) -> str:
    return hashlib.sha256(load_task_prompt(task_id).encode("utf-8")).hexdigest()
