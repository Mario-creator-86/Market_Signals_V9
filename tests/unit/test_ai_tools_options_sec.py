"""Options + SEC tools are honest net-new stubs (§4 — no forbidden language)."""
from core.ai.tools.options import make_get_options_analysis_tool, options_tools
from core.ai.tools.sec import make_get_sec_context_tool, sec_tools


def test_get_options_analysis_is_honest_phase5_stub():
    t = make_get_options_analysis_tool()
    r = t.execute(ticker="NVDA")
    assert r.ok is False
    assert "Phase 5" in r.error


def test_get_sec_context_delegates_to_phase10_service_and_marks_external_data():
    t = make_get_sec_context_tool(get_context=lambda ticker: {
        "status": "available", "ticker": ticker, "filings": [{"form": "10-K"}],
    })
    r = t.execute(ticker="AAPL")
    assert r.ok is True
    assert r.data["trust"] == "UNTRUSTED_EXTERNAL_DATA"
    assert r.data["payload"]["filings"][0]["form"] == "10-K"


def test_get_sec_context_fails_closed_when_service_is_unavailable():
    t = make_get_sec_context_tool(get_context=lambda ticker: {
        "status": "unavailable", "ticker": ticker, "error": "identity missing", "filings": [],
    })
    r = t.execute(ticker="AAPL")
    assert r.ok is False
    assert "identity missing" in r.error


def test_options_and_sec_tools_lists():
    assert [t.name for t in options_tools()] == ["get_options_analysis"]
    assert [t.name for t in sec_tools()] == ["get_sec_context"]
