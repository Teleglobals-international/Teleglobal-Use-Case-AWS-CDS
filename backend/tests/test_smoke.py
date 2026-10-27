"""
Offline smoke tests - no network required.

Covers the knowledge base, guardrails, tools (simulated), and the agent's
deterministic fallback path. The live Bedrock path is exercised manually via
the UI / API (see docs/API.md).

Run:  python -m pytest -q     (or)    python tests/test_smoke.py
"""
import os
import sys

# Make the `app` package importable when run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.knowledge_base import search_fund_docs
from app.guardrails import mask_pii, apply_disclaimer
from app.agent import _fallback, _parse_model_json
from app import tools


def test_kb_retrieval_finds_elss():
    results = search_fund_docs("tax saving fund with lock in", top_k=2)
    assert results, "expected at least one result"
    assert any("ELSS" in r["fund"] for r in results)


def test_pii_masking():
    raw = "My PAN is ABCDE1234F and phone +919876543210, email me at a@b.com"
    masked = mask_pii(raw)
    assert "ABCDE1234F" not in masked
    assert "9876543210" not in masked
    assert "a@b.com" not in masked
    assert "[PAN_MASKED]" in masked


def test_disclaimer_appended_once():
    once = apply_disclaimer("Here is some info.")
    twice = apply_disclaimer(once)
    assert once.count("subject to market risks") == 1
    assert once == twice  # idempotent


def test_agent_fallback_returns_answer_and_ctas():
    result = _fallback("how to start a sip")
    assert result["answer"]
    assert isinstance(result["ctas"], list)
    assert result["fallback"] is True


def test_parse_model_json_handles_unescaped_newlines():
    raw = '{"answer": "line one\nline two", "ctas": [], "intent": "information"}'
    parsed = _parse_model_json(raw)
    assert "line one" in parsed["answer"]
    assert parsed["intent"] == "information"


def test_parse_model_json_unwraps_nested():
    nested = '{"answer": "{\\"answer\\": \\"real text\\", \\"ctas\\": [], \\"intent\\": \\"service\\"}"}'
    parsed = _parse_model_json(nested)
    assert parsed["answer"] == "real text"


def test_handoff_tool_records_outbox():
    before = len(tools.OUTBOX)
    entry = tools.handoff_to_advisor("guest", "wants human", "summary here")
    assert entry["kind"] == "handoff"
    assert entry["status"] == "QUEUED"
    # handoff also sends an advisor alert email -> two new outbox entries
    assert len(tools.OUTBOX) >= before + 2


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
            passed += 1
        except Exception:  # noqa: BLE001
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(fns)} tests passed")
    sys.exit(0 if passed == len(fns) else 1)
