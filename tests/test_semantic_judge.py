from reposage.domain.enums import FindingCategory, Severity
from reposage.domain.finding import Finding
from reposage.domain.protocols import ModelResponse
from reposage.evals.dataset import ExpectedFinding
from reposage.evals.semantic_judge import compute_semantic_metrics


def _finding(occurrence_id: str, category: FindingCategory, explanation: str) -> Finding:
    return Finding(
        finding_occurrence_id=occurrence_id,
        run_id="run",
        fingerprint=f"fp-{occurrence_id}",
        cross_run_match_key=f"key-{occurrence_id}",
        title="blocking call",
        severity=Severity.MEDIUM,
        confidence=0.9,
        category=category,
        canonical_path="src/a.py",
        canonical_start_line=10,
        explanation=explanation,
    )


class FakeSemanticProvider:
    def __init__(self, decisions: list[dict[str, object]]) -> None:
        self.decisions = decisions

    async def complete(self, messages, **kwargs):
        del messages, kwargs
        return ModelResponse(data={"decisions": self.decisions})


async def test_category_mismatch_can_be_semantically_correct():
    expected = [
        ExpectedFinding(
            category="performance",
            path="src/a.py",
            line=10,
            note="A blocking helper stalls the async event loop.",
        )
    ]
    findings = [
        _finding(
            "one",
            FindingCategory.CONCURRENCY,
            "The synchronous helper blocks the event loop.",
        )
    ]
    provider = FakeSemanticProvider([
        {
            "pair_id": "e0-f0",
            "semantically_equivalent": True,
            "confidence": 0.98,
            "reason": "same defect",
        }
    ])

    metrics = await compute_semantic_metrics(expected, findings, provider)

    assert metrics.matched == 1
    assert metrics.precision == 1.0
    assert metrics.recall == 1.0
    assert metrics.category_agreement == 0.0


async def test_semantic_matching_is_one_to_one_for_duplicates():
    expected = [
        ExpectedFinding(category="correctness", path="src/a.py", line=10, note="wrong value")
    ]
    findings = [
        _finding("one", FindingCategory.CORRECTNESS, "wrong value"),
        _finding("two", FindingCategory.CORRECTNESS, "same wrong value"),
    ]
    provider = FakeSemanticProvider([
        {"pair_id": "e0-f0", "semantically_equivalent": True, "confidence": 0.9},
        {"pair_id": "e0-f1", "semantically_equivalent": True, "confidence": 0.8},
    ])

    metrics = await compute_semantic_metrics(expected, findings, provider)

    assert metrics.matched == 1
    assert metrics.precision == 1.0
    assert metrics.recall == 1.0
    assert metrics.raw_reported == 2
    assert metrics.reported == 1
    assert metrics.duplicates_suppressed == 1
