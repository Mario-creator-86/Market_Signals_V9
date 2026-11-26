"""Regressions from the 2026-09-15 audit of the new summary/evidence modules."""


def test_legacy_standard_record_without_trade_rows_keeps_counts():
    from core.backtest_summary import normalize_history_record

    legacy = {"mode": "standard", "total_trades": 12, "profit_factor": 1.4,
              "signals": [{"ticker": "AAPL", "wins": 7}]}
    out = normalize_history_record(legacy)
    assert out["total_trades"] == 12
    assert out["profit_factor"] == 1.4
    assert out["metrics_complete"] is False


def test_standard_record_with_trades_is_resummarized():
    from core.backtest_summary import normalize_history_record

    rec = {"mode": "standard", "total_trades": 99,
           "signals": [{"ticker": "AAPL", "trades": [{"r_multiple": 2.0}, {"r_multiple": -1.0}]}]}
    out = normalize_history_record(rec)
    assert out["total_trades"] == 2
    assert out["profit_factor"] == 2.0


def test_zero_news_minimums_disable_thin_news_gaps():
    from core.pre_earnings_evidence import build_verification_dossier

    item = {"news_digest": {"article_count": 0, "unique_sources": 0}}
    cfg = {"top_prediction_min_news_articles": 0, "top_prediction_min_news_sources": 0}
    codes = {g["code"] for g in build_verification_dossier(item, cfg)["gaps"]}
    assert "news_thin" not in codes and "news_sources_thin" not in codes
    codes_default = {g["code"] for g in build_verification_dossier(item, {})["gaps"]}
    assert "news_thin" in codes_default
