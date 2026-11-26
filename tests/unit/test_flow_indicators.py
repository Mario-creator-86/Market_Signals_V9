"""Flow indicators unit tests — fully offline (canned provider responses).

Covers: whale walls/tape/aggregation, provider fail-safe, persistence,
alignment mapping (CONFIRM/NEUTRAL/CONTRADICT/UNAVAILABLE), the institutional
proxy's honest-absence fields, and the decision-pipeline wiring
(build_signal_decision_context + evaluate_trade_decision evidence attach).
"""
import sys
import unittest
from unittest.mock import patch

import core.flow_indicators.whale_flow as wf
import core.flow_indicators.institutional_flow as inst
from core.flow_indicators.evidence import _alignment, flow_evidence_for_signal


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _book(mid, spread=0.0005, n=60, wall_side=None, wall_price=None, wall_usd=500_000.0):
    """Synthetic book: dense small levels + one obvious wall (when requested)."""
    levels = {"bids": [], "asks": []}
    step = mid * 0.0002
    for i in range(n):
        px = mid - spread / 2 - i * step
        levels["bids"].append([f"{px:.2f}", f"{10.0 + i:.2f}"])   # ~1k USD levels
        px = mid + spread / 2 + i * step
        levels["asks"].append([f"{px:.2f}", f"{10.0 + i:.2f}"])
    if wall_side and wall_price:
        wall_qty = wall_usd / wall_price
        levels[wall_side].append([f"{wall_price:.2f}", f"{wall_qty:.2f}"])
    return levels


def _trades(n_small=40, whales=(("BUY", 300_000.0), ("SELL", 100_000.0))):
    """aggTrade-like rows: many small + a few whale prints."""
    rows = []
    for i in range(n_small):
        rows.append({"p": "100.00", "q": "1.5", "m": i % 2 == 0})
    for side, usd in whales:
        rows.append({"p": "100.00", "q": f"{usd / 100.0:.2f}", "m": side == "SELL"})
    return rows


def _bybit_trades(whales=(("BUY", 300_000.0),)):
    rows = [{"price": "100.00", "size": "2.0", "side": "BUY" if i % 2 else "SELL"}
            for i in range(20)]
    for side, usd in whales:
        rows.append({"price": "100.00", "size": f"{usd / 100.0:.2f}", "side": side})
    return rows


class WhaleFlowTestBase(unittest.TestCase):
    def setUp(self):
        wf._HISTORY.clear()
        with wf._CACHE_LOCK:
            wf._CACHE.clear()
            wf._INFLIGHT.clear()

    def _serve(self, binance_ok=True, bybit_ok=True, okx_ok=True):
        def fake_http_get(url, provider=None, headers=None, timeout=None,
                          max_retries=0, params=None, **kw):
            if "binance" in url and binance_ok:
                if "depth" in url:
                    return _Resp({"bids": _book(100.0, wall_side="bids", wall_price=98.5)["bids"],
                                  "asks": _book(100.0, wall_side="asks", wall_price=101.5)["asks"]})
                return _Resp(_trades())
            if "bybit" in url and bybit_ok:
                if "orderbook" in url:
                    return _Resp({"retCode": 0, "result": {
                        "b": _book(99.9, wall_side="bids", wall_price=98.4)["bids"],
                        "a": _book(99.9, wall_side="asks", wall_price=101.4)["asks"]}})
                return _Resp({"result": {"list": _bybit_trades()}})
            if "okx" in url and okx_ok:
                if "books" in url:
                    return _Resp({"data": [{"bids": _book(100.1, wall_side="bids", wall_price=98.6)["bids"],
                                           "asks": _book(100.1, wall_side="asks", wall_price=101.6)["asks"]}]})
                return _Resp({"data": [{"px": "100.0", "sz": "3000", "side": "BUY"}]})
            return _Resp({}, status=503)
        return fake_http_get


class TestWhaleFlow(WhaleFlowTestBase):
    @patch.object(wf, "http_get")
    def test_walls_tape_and_bias(self, mock_get):
        mock_get.side_effect = self._serve()
        result = wf.get_whale_flow("BTC-USD", force=True)
        self.assertEqual(result["bias"], "BULLISH")
        self.assertIsNotNone(result["score"])
        self.assertGreater(result["score"], 20)
        self.assertLessEqual(result["score"], 100)
        self.assertGreater(result["buy_pressure"], result["sell_pressure"])
        self.assertIsNotNone(result["nearest_buy_wall"])
        self.assertIsNotNone(result["nearest_sell_wall"])
        self.assertIsNotNone(result["strongest_buy_wall"])
        self.assertIn("binance", result["sources_ok"])
        # all requested output fields present
        for field in ("bias", "score", "confidence", "buy_pressure", "sell_pressure",
                      "nearest_buy_wall", "nearest_sell_wall", "strongest_buy_wall",
                      "strongest_sell_wall", "persistence", "cancellation_activity",
                      "spoofing_risk", "data_freshness"):
            self.assertIn(field, result)

    @patch.object(wf, "http_get")
    def test_one_provider_down_others_carry(self, mock_get):
        mock_get.side_effect = self._serve(okx_ok=False)
        result = wf.get_whale_flow("ETH-USD", force=True)
        self.assertNotEqual(result["bias"], "UNAVAILABLE")
        self.assertEqual(sorted(result["sources_ok"]), ["binance", "bybit"])
        self.assertIn("okx", result["data_freshness"].get("errors", {}))

    @patch.object(wf, "http_get")
    def test_all_providers_down_unavailable(self, mock_get):
        mock_get.side_effect = self._serve(binance_ok=False, bybit_ok=False, okx_ok=False)
        result = wf.get_whale_flow("BTC-USD", force=True)
        self.assertEqual(result["bias"], "UNAVAILABLE")
        self.assertIsNone(result["score"])
        self.assertIsNone(result["buy_pressure"])
        self.assertIsNone(result["nearest_buy_wall"])
        self.assertEqual(result["sources_ok"], [])

    @patch.object(wf, "http_get")
    def test_persistence_after_two_snapshots(self, mock_get):
        mock_get.side_effect = self._serve()
        wf.get_whale_flow("BTC-USD", force=True)
        result = wf.get_whale_flow("BTC-USD", force=True)
        self.assertIsNotNone(result["persistence"])
        self.assertIsNotNone(result["cancellation_activity"])
        self.assertIsNotNone(result["spoofing_risk"])
        self.assertGreaterEqual(result["spoofing_risk"], 0.0)
        self.assertLessEqual(result["spoofing_risk"], 1.0)

    @patch.object(wf, "http_get")
    def test_cached_second_call_hits_cache(self, mock_get):
        mock_get.side_effect = self._serve()
        first = wf.get_whale_flow("BTC-USD", force=True)
        calls_after_first = mock_get.call_count
        second = wf.get_whale_flow("BTC-USD")  # no force -> cache
        self.assertEqual(mock_get.call_count, calls_after_first)
        self.assertEqual(first["bias"], second["bias"])

    def test_unsupported_symbol(self):
        result = wf.get_whale_flow("NOTACOIN-USD")
        self.assertEqual(result["bias"], "UNAVAILABLE")
        self.assertIn("unsupported", result["data_freshness"]["reason"])


class TestAlignmentMapping(unittest.TestCase):
    def test_confirm_contradict_neutral_unavailable(self):
        self.assertEqual(_alignment("LONG", "BULLISH", 60.0, 0.6), "CONFIRM")
        self.assertEqual(_alignment("SHORT", "BULLISH", 60.0, 0.6), "CONTRADICT")
        self.assertEqual(_alignment("SELL", "BEARISH", -60.0, 0.6), "CONFIRM")
        self.assertEqual(_alignment("BUY", "BEARISH", -60.0, 0.6), "CONTRADICT")
        self.assertEqual(_alignment("LONG", "BULLISH", 24.0, 0.6), "NEUTRAL")   # weak score
        self.assertEqual(_alignment("LONG", "BULLISH", 60.0, 0.30), "NEUTRAL")  # low conf
        self.assertEqual(_alignment("LONG", "NEUTRAL", 0.0, 0.9), "NEUTRAL")
        self.assertEqual(_alignment("LONG", "UNAVAILABLE", None, None), "UNAVAILABLE")
        self.assertEqual(_alignment("HOLD", "BULLISH", 60.0, 0.6), "NEUTRAL")

    @patch.object(wf, "get_whale_flow")
    def test_evidence_wraps_indicator(self, mock_whale):
        mock_whale.return_value = {"bias": "BEARISH", "score": -55.0, "confidence": 0.7,
                                   "data_freshness": {"stale": False}}
        ev = flow_evidence_for_signal({"ticker": "BTC-USD", "direction": "SHORT"})
        self.assertEqual(ev["alignment"], "CONFIRM")
        self.assertEqual(ev["asset_class"], "crypto")
        self.assertEqual(ev["summary"]["bias"], "BEARISH")

    @patch.object(wf, "get_whale_flow")
    def test_evidence_unavailable_never_raises(self, mock_whale):
        mock_whale.side_effect = RuntimeError("boom")
        ev = flow_evidence_for_signal({"ticker": "BTC-USD", "direction": "LONG"})
        self.assertEqual(ev["alignment"], "UNAVAILABLE")
        self.assertIn("error", ev["note"])

    def test_evidence_no_direction_setup(self):
        with patch.object(wf, "get_whale_flow", return_value={"bias": "BULLISH", "score": 50, "confidence": 0.8}):
            ev = flow_evidence_for_signal({"ticker": "BTC-USD", "direction": ""})
        self.assertEqual(ev["alignment"], "NEUTRAL")


class TestInstitutionalFlow(unittest.TestCase):
    def setUp(self):
        with inst._CACHE_LOCK:
            inst._CACHE.clear()
            inst._INFLIGHT.clear()

    @staticmethod
    def _chart(bars):
        # full Yahoo v8 chart envelope: .chart.result[0]
        return {"chart": {"result": [{
            "timestamp": [b[0] for b in bars],
            "indicators": {"quote": [{"close": [b[1] for b in bars],
                                      "volume": [b[2] for b in bars]}]},
        }]}}

    def _intraday_bars(self, n=40, bull=True):
        bars = []
        price = 100.0
        vol = 50_000.0
        for i in range(n):
            price *= 1.0008 if bull else 0.9992
            bars.append((1_700_000_000 + i * 300, round(price, 2),
                         vol * (2.5 if i >= n - 6 else 1.0)))
        return bars

    def _serve(self, intraday, daily, finra_ok=False):
        def fake_http_get(url, provider=None, headers=None, timeout=None,
                          max_retries=0, params=None, **kw):
            if provider == "yahoo" and "chart" in url:
                rng = params.get("range") if params else None
                return _Resp(self._chart(intraday if rng == "1d" else daily))
            if provider == "finra":
                if not finra_ok:
                    return _Resp({}, status=404)
                return _Resp([{"issueSymbolIdentifier": "AAPL",
                               "weekStartDate": "2026-08-25",
                               "totalWeeklyShareQuantity": 1_234_567.0,
                               "totalWeeklyTradeCount": 4_321.0}])
            return _Resp({}, status=503)
        return fake_http_get

    @patch.object(inst, "http_get")
    def test_bullish_tape_and_proxy_label(self, mock_get):
        import time as _time
        now = _time.time()
        daily = [(now - 4 * 86400, 99.0, 1_000_000), (now - 3 * 86400, 99.5, 1_100_000),
                 (now - 2 * 86400, 100.0, 900_000), (now - 3600, 101.0, 800_000)]
        mock_get.side_effect = self._serve(self._intraday_bars(bull=True), daily)
        result = inst.get_institutional_flow("AAPL", force=True)
        self.assertTrue(result["proxy"])
        self.assertIn("PROXY", result["indicator"].upper() or result["proxy_scope"])
        self.assertIn("does NOT represent complete", result["proxy_scope"])
        self.assertEqual(result["bias"], "BULLISH")
        self.assertGreater(result["buy_pressure"], result["sell_pressure"])
        self.assertGreater(result["tape"]["up_volume"], result["tape"]["down_volume"])
        self.assertGreater(result["tape"]["volume_acceleration"], 1.0)
        self.assertIsNotNone(result["relative_volume"])
        # honest absence: free tape can't see print sizes
        self.assertIsNone(result["large_trade_bias"])
        self.assertIn("yahoo_chart", result["sources_ok"])

    @patch.object(inst, "http_get")
    def test_bearish_tape(self, mock_get):
        import time as _time
        now = _time.time()
        daily = [(now - 4 * 86400, 99.0, 1_000_000), (now - 3 * 86400, 99.5, 1_100_000),
                 (now - 2 * 86400, 100.0, 900_000), (now - 3600, 101.0, 800_000)]
        mock_get.side_effect = self._serve(self._intraday_bars(bull=False), daily)
        result = inst.get_institutional_flow("MSFT", force=True)
        self.assertEqual(result["bias"], "BEARISH")
        self.assertGreater(result["sell_pressure"], result["buy_pressure"])

    @patch.object(inst, "http_get")
    def test_no_bars_unavailable(self, mock_get):
        mock_get.side_effect = self._serve([], [])
        result = inst.get_institutional_flow("AAPL", force=True)
        self.assertEqual(result["bias"], "UNAVAILABLE")
        self.assertIsNone(result["score"])

    @patch("core.http_utils.http_post")
    @patch.object(inst, "http_get")
    def test_finra_context_when_available(self, mock_get, mock_post):
        import time as _time
        now = _time.time()
        daily = [(now - 4 * 86400, 99.0, 1_000_000), (now - 3 * 86400, 99.5, 1_100_000),
                 (now - 2 * 86400, 100.0, 900_000), (now - 3600, 101.0, 800_000)]
        mock_get.side_effect = self._serve(self._intraday_bars(bull=True), daily)
        # per-venue rows for two weeks; the NEWEST week must win after summing
        mock_post.return_value = _Resp([
            {"issueSymbolIdentifier": "AAPL", "weekStartDate": "2026-08-03",
             "totalWeeklyShareQuantity": 1_000_000.0, "totalWeeklyTradeCount": 5_000.0},
            {"issueSymbolIdentifier": "AAPL", "weekStartDate": "2026-08-03",
             "totalWeeklyShareQuantity": 234_567.0, "totalWeeklyTradeCount": 321.0},
            {"issueSymbolIdentifier": "AAPL", "weekStartDate": "2026-07-27",
             "totalWeeklyShareQuantity": 9_999_999.0, "totalWeeklyTradeCount": 1.0},
        ])
        result = inst.get_institutional_flow("AAPL", force=True)
        ctx = result["off_exchange_context"]
        self.assertTrue(ctx["available"])
        self.assertEqual(ctx["week_start"], "2026-08-03")
        self.assertEqual(ctx["weekly_otc_shares"], 1_234_567.0)
        self.assertEqual(ctx["weekly_otc_trades"], 5_321.0)
        self.assertIn("finra_otc_weekly", result["sources_ok"])

    def test_non_stock_symbol(self):
        result = inst.get_institutional_flow("BTC-USD")
        self.assertEqual(result["bias"], "UNAVAILABLE")


class TestDecisionPipelineWiring(unittest.TestCase):
    """The evidence must reach the existing decision systems, not just the UI."""

    def test_decision_context_carries_flow_evidence(self):
        from core.trade_decision import build_signal_decision_context
        canned = {"alignment": "CONFIRM", "asset_class": "crypto",
                  "note": "whale flow bias BULLISH", "summary": {"bias": "BULLISH"}}
        with patch("core.flow_indicators.flow_evidence_for_signal", return_value=canned):
            ctx = build_signal_decision_context({
                "ticker": "BTC-USD", "action": "LONG", "entry": 100.0,
                "tp": 110.0, "sl": 95.0, "signal_score": 80.0,
            })
        self.assertEqual(ctx["flow_evidence"]["alignment"], "CONFIRM")
        self.assertEqual(ctx["ticker"], "BTC-USD")

    def test_decision_context_fail_safe(self):
        from core.trade_decision import build_signal_decision_context
        with patch("core.flow_indicators.flow_evidence_for_signal",
                   side_effect=RuntimeError("network down")):
            ctx = build_signal_decision_context({"ticker": "AAPL", "action": "LONG"})
        self.assertEqual(ctx["flow_evidence"]["alignment"], "UNAVAILABLE")

    def test_evaluate_attaches_evidence_to_signal(self):
        from core.ai import decision_engine

        signal = {"ticker": "BTC-USD", "action": "LONG", "entry": 100.0,
                  "tp": 110.0, "sl": 95.0}
        captured = {}

        class _BoomProvider:
            name = "boom"

            def generate(self, *args, **kwargs):
                raise RuntimeError("no network in test")

        # The §41 prompt must see the attached evidence; the caller-owned
        # signal dict must NOT be mutated (audit fix: the old in-place attach
        # leaked evidence into module-level scenario dicts and froze stale
        # values on reused signals).
        with patch("core.flow_indicators.flow_evidence_for_signal",
                   return_value={"alignment": "NEUTRAL", "note": "x"}), \
             patch("core.ai.decision_engine.build_trade_review_prompt",
                   side_effect=lambda sig, risk: (captured.update(sig), "p")[1]):
            decision_engine.evaluate_trade_decision(
                signal, {"accepted": True},
                config={"ai": {}},
                services={"primary_provider": _BoomProvider(), "primary_model": "m",
                          "fallback_chain": []},
            )
        self.assertIn("flow_evidence", captured)
        self.assertEqual(captured["flow_evidence"]["alignment"], "NEUTRAL")
        self.assertNotIn("flow_evidence", signal)

    def test_with_flow_evidence_hermetic_skip(self):
        from core.ai import decision_engine

        signal = {"ticker": "BTC-USD", "action": "LONG"}
        # Hermetic scenario runners declare the skip explicitly.
        self.assertIs(
            decision_engine._with_flow_evidence(signal, services={"skip_flow_evidence": True}),
            signal,
        )

    def test_critic_compact_signal_shows_alignment(self):
        from core.ai.critic import _compact_signal
        out = _compact_signal({"ticker": "BTC-USD", "signal_score": 80,
                               "flow_evidence": {"alignment": "CONFIRM",
                                                 "note": "whale flow bias BULLISH"}})
        self.assertIn("flow_alignment=CONFIRM", out)

    def test_critic_compact_signal_without_evidence_unchanged(self):
        from core.ai.critic import _compact_signal
        out = _compact_signal({"ticker": "AAPL", "signal_score": 80})
        self.assertNotIn("flow_alignment", out)


class TestFlowIndicatorNumericSafety(unittest.TestCase):
    """Audit 2026-09-02: provider NaN/inf must stay neutral, never bullish."""

    def test_clamp_nan_is_neutral_floor(self):
        from core.flow_indicators import institutional_flow, whale_flow
        self.assertEqual(institutional_flow._clamp(float("nan"), -1.0, 1.0), -1.0)
        self.assertEqual(whale_flow._clamp(float("nan"), -1.0, 1.0), -1.0)

    def test_bars_drop_non_finite_cells(self):
        from core.flow_indicators.institutional_flow import _bars
        chart = {
            "timestamp": [1, 2, 3],
            "indicators": {"quote": [{
                "close": [10.0, float("nan"), 12.0],
                "volume": [1000, 2000, float("inf")],
            }]},
        }
        bars = _bars(chart)
        self.assertEqual([b["close"] for b in bars], [10.0])
        self.assertEqual([b["volume"] for b in bars], [1000.0])

    def test_base_asset_accepts_concatenated_quote_forms(self):
        from core.flow_indicators.whale_flow import _base_asset, _is_crypto_symbol
        # _is_crypto_symbol accepts the concatenated forms; base extraction
        # must agree, otherwise classified-crypto tickers fail as unsupported.
        for ticker in ("BTC-USD", "BTCUSDT", "BTCUSD", "BTC/USDT"):
            self.assertTrue(_is_crypto_symbol(ticker), ticker)
            self.assertEqual(_base_asset(ticker), "BTC", ticker)
        self.assertIsNone(_base_asset("NOTACOIN"))


if __name__ == "__main__":
    unittest.main()
