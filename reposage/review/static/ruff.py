"""ruff 子进程适配器（V2-C §5.3）。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from ...domain.models import ChangedFile, is_safe_repo_path
from .protocol import AnalyzerDiagnostic, AnalyzerRunResult

_MAX_BLOB_BYTES = 1_048_576


class RuffAnalyzer:
    id = "ruff"

    async def analyze(
        self,
        files: list[ChangedFile],
        blobs: Mapping[str, str],
        *,
        timeout_s: float,
        select: list[str],
        denylist: list[str],
    ) -> AnalyzerRunResult:
        kept_paths = {f.path.replace("\\", "/") for f in files}
        written: list[str] = []
        try:
            with tempfile.TemporaryDirectory(prefix="reposage-ruff-") as tmp_name:
                tmp = Path(tmp_name)
                for path, content in blobs.items():
                    rel = path.replace("\\", "/")
                    if rel not in kept_paths or not is_safe_repo_path(rel):
                        continue
                    encoded = content.encode("utf-8")
                    if len(encoded) > _MAX_BLOB_BYTES:
                        continue
                    dest = tmp.joinpath(*rel.split("/"))
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(encoded)
                    written.append(rel)
                if not written:
                    return AnalyzerRunResult(status="ok")
                args = [
                    sys.executable,
                    "-m",
                    "ruff",
                    "check",
                    "--isolated",
                    "--output-format",
                    "json",
                    "--select",
                    ",".join(select) if select else "B,S",
                ]
                if denylist:
                    args.extend(["--ignore", ",".join(denylist)])
                args.extend(written)
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=str(tmp),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, _stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout_s
                    )
                except TimeoutError:
                    await _kill(proc)
                    return AnalyzerRunResult(
                        status="failed",
                        warnings=["ruff timeout"],
                    )
                except asyncio.CancelledError:
                    await _kill(proc)
                    raise
                code = proc.returncode if proc.returncode is not None else 2
                payload, error = _ruff_json_payload(code, stdout, _stderr)
                if error is not None or payload is None:
                    return AnalyzerRunResult(status="failed", warnings=[error or "ruff json invalid"])
                diagnostics: list[AnalyzerDiagnostic] = []
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    mapped = _map_filename(item.get("filename"), tmp, kept_paths)
                    if mapped is None:
                        continue
                    raw_rule = str(item.get("code") or "")
                    if not raw_rule:
                        continue
                    loc = item.get("location") if isinstance(item.get("location"), dict) else {}
                    end_loc = item.get("end_location") if isinstance(item.get("end_location"), dict) else {}
                    row = loc.get("row") if isinstance(loc, dict) else None
                    if row is None:
                        continue
                    try:
                        start_line = int(row)
                    except (TypeError, ValueError):
                        continue
                    end_line = start_line
                    end_row = end_loc.get("row") if isinstance(end_loc, dict) else None
                    if end_row is not None:
                        try:
                            end_line = int(end_row)
                        except (TypeError, ValueError):
                            end_line = start_line
                    if denylist and raw_rule.upper() in {c.upper() for c in denylist}:
                        continue
                    diagnostics.append(
                        AnalyzerDiagnostic(
                            analyzer_id=self.id,
                            rule_id=raw_rule,
                            path=mapped,
                            start_line=start_line,
                            end_line=end_line,
                            message=str(item.get("message") or ""),
                            severity_hint=str(item.get("severity") or "") or None,
                        )
                    )
                return AnalyzerRunResult(diagnostics=diagnostics, status="ok")
        except FileNotFoundError:
            return AnalyzerRunResult(
                status="failed",
                warnings=["capability miss: ruff"],
            )
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            return AnalyzerRunResult(
                status="failed",
                warnings=[f"capability miss: ruff ({type(exc).__name__})"],
            )


def _ruff_empty_json_warning(stderr: bytes) -> str:
    err = stderr.decode("utf-8", errors="replace").lower()
    if "no module named" in err or "modulenotfounderror" in err:
        return "capability miss: ruff"
    return "ruff json invalid"


def _ruff_json_payload(
    code: int, stdout: bytes, stderr: bytes
) -> tuple[list[object] | None, str | None]:
    """解释 ruff 退出码与 stdout。空 stdout 不得当成零诊断。"""
    if code >= 2:
        return None, f"ruff exit {code}"
    text = stdout.decode("utf-8", errors="replace")
    if not text.strip():
        return None, _ruff_empty_json_warning(stderr)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, _ruff_empty_json_warning(stderr)
    if not isinstance(payload, list):
        return None, "ruff json not a list"
    return payload, None


def _map_filename(filename: object, tmp: Path, kept: set[str]) -> str | None:
    if not isinstance(filename, str) or not filename:
        return None
    raw = Path(filename)
    try:
        resolved_tmp = tmp.resolve()
        if raw.is_absolute():
            rel = raw.resolve().relative_to(resolved_tmp)
        else:
            rel = Path(filename.replace("\\", "/"))
            if rel.is_absolute():
                return None
            # 防止目录穿越
            candidate = (resolved_tmp / rel).resolve()
            rel = candidate.relative_to(resolved_tmp)
    except ValueError:
        return None
    posix = rel.as_posix()
    if ".." in Path(posix).parts:
        return None
    if not is_safe_repo_path(posix):
        return None
    if posix not in kept:
        return None
    return posix


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    proc.kill()
    with contextlib.suppress(Exception):
        await proc.communicate()
