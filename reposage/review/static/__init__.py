"""V2-C 静态分析器适配。"""

from .convert import convert_diagnostics
from .fake import FakeStaticAnalyzer
from .protocol import AnalyzerDiagnostic, AnalyzerRunResult, StaticAnalyzer
from .ruff import RuffAnalyzer
from .runner import run_static_analysis

__all__ = [
    "AnalyzerDiagnostic",
    "AnalyzerRunResult",
    "FakeStaticAnalyzer",
    "RuffAnalyzer",
    "StaticAnalyzer",
    "convert_diagnostics",
    "run_static_analysis",
]
