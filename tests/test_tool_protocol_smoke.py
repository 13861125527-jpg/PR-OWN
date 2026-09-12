"""OQ-11 deferred probe。"""

from reposage.providers.llm.tool_protocol_smoke import run_probe


def test_probe_is_deferred_and_does_not_claim_live():
    report = run_probe(live=True)
    assert report["status"] == "DEFERRED_BY_USER"
    assert report["real_api"] is False
    assert report["protocols"]["native"]["ran"] is False
