from datetime import datetime, timedelta, timezone

from agents import pre_earnings_agent as pe


def test_scoring_scenario_high_gap_short_bias():
    gap_risk, contributors = pe.compute_gap_risk_score(
        historical_move_score=1.0,
        news_intensity_score=1.0,
        catalyst_severity_score=1.0,
        liquidity_risk_score=0.7,
        implied_move_score=1.0,
    )
    result = pe.compute_direction_bias_score(
        "TEST",
        enriched_data={"beat_rate_historical": 0.2, "beat_rate_samples": 4, "eps_estimate_revisions_30d": -0.5, "calls_vs_puts_ratio": 0.3},
        news_sentiment=-0.9,
    )
    bias = result["bias_score"]

    assert gap_risk >= 90
    assert bias <= -60
    assert len(contributors) == 5


def test_scoring_scenario_high_gap_long_bias():
    gap_risk, contributors = pe.compute_gap_risk_score(
        historical_move_score=0.7,
        news_intensity_score=0.7,
        catalyst_severity_score=0.9,
        liquidity_risk_score=0.5,
        implied_move_score=0.8,
    )
    result = pe.compute_direction_bias_score(
        "TEST",
        enriched_data={"beat_rate_historical": 0.8, "beat_rate_samples": 4, "eps_estimate_revisions_30d": 0.5, "calls_vs_puts_ratio": 1.8},
        news_sentiment=0.85,
    )
    bias = result["bias_score"]

    assert gap_risk >= 70
    assert bias >= 60
    assert len(contributors) == 5


def test_scoring_scenario_low_risk_neutral_bias_without_implied_move():
    gap_risk, contributors = pe.compute_gap_risk_score(
        historical_move_score=0.2,
        news_intensity_score=0.2,
        catalyst_severity_score=0.3,
        liquidity_risk_score=0.2,
        implied_move_score=None,
    )
    result = pe.compute_direction_bias_score(
        "TEST",
        enriched_data={},
        news_sentiment=0.1,
    )
    bias = result["bias_score"]

    assert gap_risk <= 35
    assert -20 <= bias <= 20
    assert len(contributors) == 4
    assert all(c["name"] != "implied_move" for c in contributors)


def test_direction_bias_helpers_support_normalized_and_legacy_payloads():
    assert pe.direction_label_from_score(25) == "LONG"
    assert pe.direction_label_from_score(-25) == "SHORT"
    assert pe.direction_label_from_score(0) == "NEUTRAL"

    assert pe.direction_bias_score_from_item({"direction_bias_score": 44}) == 44
    assert pe.direction_bias_score_from_item({"direction_bias": 0.52}) == 52
    assert pe.direction_bias_score_from_item({"direction_bias": -0.61}) == -61


def test_gap_risk_weight_overrides_are_applied_and_normalized():
    gap_default, _ = pe.compute_gap_risk_score(
        historical_move_score=0.9,
        news_intensity_score=0.2,
        catalyst_severity_score=0.2,
        liquidity_risk_score=0.2,
        implied_move_score=0.2,
    )
    gap_override, contributors = pe.compute_gap_risk_score(
        historical_move_score=0.9,
        news_intensity_score=0.2,
        catalyst_severity_score=0.2,
        liquidity_risk_score=0.2,
        implied_move_score=0.2,
        weight_overrides={
            "historical_move": 0.8,
            "implied_move": 0.05,
            "news_intensity": 0.05,
            "catalyst_severity": 0.05,
            "liquidity_risk": 0.05,
        },
    )

    assert gap_override > gap_default
    w_sum = sum(float(c.get("weight") or 0.0) for c in contributors)
    assert abs(w_sum - 1.0) < 0.01


def test_analysis_coverage_and_opportunity_scores_behave_as_expected():
    agent = pe.PreEarningsAgent()
    high_cov, high_lbl, high_parts = agent._compute_analysis_coverage(
        event_present=True,
        news_count=8,
        historical_samples=6,
        implied_available=True,
        liquidity_available=True,
        earnings_trend_available=True,
    )
    low_cov, low_lbl, low_parts = agent._compute_analysis_coverage(
        event_present=True,
        news_count=0,
        historical_samples=0,
        implied_available=False,
        liquidity_available=False,
        earnings_trend_available=False,
    )
    high_opp = agent._compute_opportunity_score(gap_risk=80, direction_bias=-60, coverage_score=high_cov)
    low_opp = agent._compute_opportunity_score(gap_risk=80, direction_bias=-60, coverage_score=low_cov)

    assert high_cov >= 70
    assert high_lbl == "HIGH"
    assert len(high_parts) == 6
    assert low_cov <= 45
    assert low_lbl == "LOW"
    assert len(low_parts) == 6
    assert high_opp > low_opp


def test_calendar_mismatch_detector_flags_earnings_like_news_without_calendar_event():
    now = datetime(2026, 2, 26, 12, 0, 0, tzinfo=timezone.utc)
    news_index = {
        "ODD": [
            {
                "title": "ODD reports Q4 results and updates guidance",
                "snippet": "EPS beats but outlook mixed",
                "published_utc": (now - timedelta(hours=3)).isoformat(),
            },
            {
                "title": "ODD earnings call preview",
                "snippet": "Street expects volatile move around earnings",
                "published_utc": (now - timedelta(hours=1)).isoformat(),
            },
        ]
    }
    earnings_index = {}

    mismatch = pe.detect_calendar_mismatch_symbols(
        news_index=news_index,
        earnings_index=earnings_index,
        now=now,
        min_hits=2,
    )

    assert "ODD" in mismatch
    assert mismatch["ODD"]["reason"] == "POSSIBLE_MISSING_EARNINGS_EVENT"
    assert mismatch["ODD"]["hits"] == 2


def test_calendar_mismatch_detector_ignores_when_calendar_event_exists_nearby():
    now = datetime(2026, 2, 26, 12, 0, 0, tzinfo=timezone.utc)
    news_index = {
        "AAPL": [
            {
                "title": "AAPL reports Q1 results",
                "snippet": "EPS and revenue beat",
                "published_utc": (now - timedelta(hours=2)).isoformat(),
            },
            {
                "title": "AAPL earnings preview",
                "snippet": "Analysts focus on services growth",
                "published_utc": (now - timedelta(hours=1)).isoformat(),
            },
        ]
    }
    earnings_index = {
        "AAPL": [
            {
                "symbol": "AAPL",
                "date": (now + timedelta(days=1)).isoformat(),
            }
        ]
    }

    mismatch = pe.detect_calendar_mismatch_symbols(
        news_index=news_index,
        earnings_index=earnings_index,
        now=now,
        min_hits=2,
    )
    assert mismatch == {}


def test_pick_primary_event_does_not_fall_back_to_past_event():
    agent = pe.PreEarningsAgent.__new__(pe.PreEarningsAgent)
    rows = [
        {
            "symbol": "AAPL",
            "date": "2026-02-20T09:00:00+00:00",
        }
    ]

    picked = agent._pick_primary_event(rows)

    assert picked == {}


def test_schedule_conflict_detects_recent_prior_earnings_without_news_support():
    agent = pe.PreEarningsAgent.__new__(pe.PreEarningsAgent)
    # Anchor to the real clock: _detect_schedule_conflict's past-event guard
    # (event_dt < now-1d => None) makes hardcoded dates rot as the wall clock
    # advances. Use relative dates so the spacing logic is what's exercised.
    now = datetime.now(timezone.utc)
    event_dt = now + timedelta(days=7)
    prior = event_dt - timedelta(days=7)
    older = event_dt - timedelta(days=84)
    # 2026-09-03: the conflict detector only treats rows with confirmed
    # reported actuals (eps_actual/revenue_actual) as prior earnings events —
    # future calendar rows without actuals must not suppress a candidate.
    # The stub mirrors that contract with confirmed rows.
    agent._get_cached_earnings_history = lambda symbol, max_events=16: [
        {"symbol": symbol, "date": prior.isoformat(), "eps_actual": 1.2},
        {"symbol": symbol, "date": older.isoformat(), "eps_actual": 0.9},
    ]
    cfg = {
        "schedule_conflict_min_spacing_days": 45,
        "schedule_conflict_max_news_rows": 0,
    }

    conflict = agent._detect_schedule_conflict(
        symbol="PEP",
        event_dt=event_dt,
        event={"symbol": "PEP", "date": event_dt.isoformat()},
        news_rows=[],
        cfg=cfg,
    )

    assert conflict is not None
    assert conflict["reason"] == "RECENT_PRIOR_EARNINGS_CONFLICT"
    assert conflict["delta_days"] == 7


def test_schedule_conflict_allows_recent_prior_earnings_when_news_support_exists():
    agent = pe.PreEarningsAgent.__new__(pe.PreEarningsAgent)
    # Relative dates (see test above). With news support present, the
    # news-support escape hatch (len(news_rows) > max_news_rows => None) fires
    # BEFORE the spacing check, so no conflict is reported.
    now = datetime.now(timezone.utc)
    event_dt = now + timedelta(days=7)
    prior = event_dt - timedelta(days=7)
    agent._get_cached_earnings_history = lambda symbol, max_events=16: [
        {"symbol": symbol, "date": prior.isoformat()},
    ]
    cfg = {
        "schedule_conflict_min_spacing_days": 45,
        "schedule_conflict_max_news_rows": 0,
    }

    conflict = agent._detect_schedule_conflict(
        symbol="PEP",
        event_dt=event_dt,
        event={"symbol": "PEP", "date": event_dt.isoformat()},
        news_rows=[{"title": "PEP earnings preview"}],
        cfg=cfg,
    )

    assert conflict is None


def test_calendar_mismatch_detector_ignores_non_equity_symbols():
    now = datetime(2026, 2, 26, 12, 0, 0, tzinfo=timezone.utc)
    news_index = {
        "BTC-USD": [
            {
                "title": "BTC-USD reports Q4 results",
                "snippet": "EPS keyword present in syndicated text",
                "published_utc": (now - timedelta(hours=2)).isoformat(),
            },
            {
                "title": "BTC-USD earnings preview mention",
                "snippet": "another noisy article",
                "published_utc": (now - timedelta(hours=1)).isoformat(),
            },
        ]
    }
    mismatch = pe.detect_calendar_mismatch_symbols(
        news_index=news_index,
        earnings_index={},
        now=now,
        min_hits=2,
    )
    assert mismatch == {}


def test_earnings_eligible_symbol_excludes_crypto_and_gold_silver_commodities():
    assert pe._is_earnings_eligible_symbol("BTC-USD") is False
    assert pe._is_earnings_eligible_symbol("ETHUSDT") is False
    assert pe._is_earnings_eligible_symbol("GC=F") is False
    assert pe._is_earnings_eligible_symbol("SI=F") is False
    assert pe._is_earnings_eligible_symbol("GLD") is False
    assert pe._is_earnings_eligible_symbol("SLV") is False
    assert pe._is_earnings_eligible_symbol("AAPL") is True


def test_calendar_mismatch_detector_ignores_generic_earnings_wording_noise():
    now = datetime(2026, 2, 26, 12, 0, 0, tzinfo=timezone.utc)
    news_index = {
        "MOVE": [
            {
                "title": "Stocks surge ahead of Nvidia results; earnings week remains volatile",
                "snippet": "Macro digest only",
                "published_utc": (now - timedelta(hours=2)).isoformat(),
            },
            {
                "title": "Market wrap: software pressure continues into earnings week",
                "snippet": "No issuer earnings event statement",
                "published_utc": (now - timedelta(hours=1)).isoformat(),
            },
        ]
    }
    mismatch = pe.detect_calendar_mismatch_symbols(
        news_index=news_index,
        earnings_index={},
        now=now,
        min_hits=2,
    )
    assert mismatch == {}


def test_calendar_mismatch_detector_ignores_post_earnings_result_coverage():
    now = datetime(2026, 2, 26, 12, 0, 0, tzinfo=timezone.utc)
    news_index = {
        "RNG": [
            {
                "title": "RingCentral (RNG) Surpasses Q4 Earnings and Revenue Estimates",
                "snippet": "Post-results recap",
                "published_utc": (now - timedelta(hours=4)).isoformat(),
            },
            {
                "title": "RingCentral, Inc. (RNG) Q4 2025 Earnings Call Transcript",
                "snippet": "Call transcript after report",
                "published_utc": (now - timedelta(hours=2)).isoformat(),
            },
        ]
    }
    mismatch = pe.detect_calendar_mismatch_symbols(
        news_index=news_index,
        earnings_index={},
        now=now,
        min_hits=2,
    )
    assert mismatch == {}


def test_prune_mismatch_with_cached_history_suppresses_recent_event(monkeypatch):
    now = datetime(2026, 2, 26, 12, 0, 0, tzinfo=timezone.utc)
    agent = pe.PreEarningsAgent()
    mismatch = {"CIFR": {"symbol": "CIFR", "reason": "POSSIBLE_MISSING_EARNINGS_EVENT"}}
    cfg = agent._get_config()
    cfg["lookahead_days"] = 10
    cfg["mismatch_past_grace_days"] = 3

    monkeypatch.setattr(
        agent,
        "_get_cached_earnings_history",
        lambda symbol, max_events=16: [
            {"date": (now - timedelta(days=2)).isoformat(), "when": "bmo"}
        ] if symbol == "CIFR" else [],
    )
    pruned = agent._prune_mismatch_with_cached_history(mismatch, now=now, cfg=cfg)
    assert pruned == {}


def test_historical_earnings_gap_stats_uses_event_window_moves(monkeypatch):
    agent = pe.PreEarningsAgent()
    now = datetime(2026, 2, 26, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        {"date": (now - timedelta(days=90)).isoformat(), "when": "amc"},
        {"date": (now - timedelta(days=180)).isoformat(), "when": "bmo"},
        {"date": (now - timedelta(days=270)).isoformat(), "when": "amc"},
    ]
    moves = [10.0, -6.0, 8.0]

    monkeypatch.setattr(pe, "_utc_now", lambda: now)
    monkeypatch.setattr(agent, "_get_cached_earnings_history", lambda symbol, max_events=24: events)
    monkeypatch.setattr(agent, "_compute_event_gap_move_pct", lambda symbol, event_dt, session: moves.pop(0))
    monkeypatch.setattr(agent, "_load_symbol_daily_bars", lambda symbol: [])

    stats = agent._compute_historical_earnings_gap_stats("AAPL", max_events=8)
    assert stats["sample_size"] == 3
    # abs moves => [10,6,8] => median 8
    assert abs(float(stats["median_move_pct"]) - 8.0) < 1e-9


def test_live_calibration_metrics_include_brier_and_buckets():
    agent = pe.PreEarningsAgent()
    agent._feedback["outcomes"] = [
        {
            "evaluation_utc": "2026-02-20T10:00:00+00:00",
            "predicted_gap_risk": 80,
            "predicted_prob_high_gap": 0.8,
            "predicted_direction_bias": 45,
            "realized_high_gap": True,
            "realized_gap_move_pct": 9.0,
            "realized_signed_move_pct": 4.0,
        },
        {
            "evaluation_utc": "2026-02-21T10:00:00+00:00",
            "predicted_gap_risk": 30,
            "predicted_prob_high_gap": 0.3,
            "predicted_direction_bias": -50,
            "realized_high_gap": False,
            "realized_gap_move_pct": 2.0,
            "realized_signed_move_pct": -1.2,
        },
    ]
    cfg = agent._get_config()
    model = {"sample_size": 2, "global_factor": 1.0, "group_factors": {}, "group_samples": {}}
    metrics = agent._compute_live_calibration_metrics(cfg, model)

    assert metrics["sample_size"] == 2
    assert isinstance(metrics["brier"], float)
    assert isinstance(metrics["mae"], float)
    assert isinstance(metrics["buckets"], list)
    assert len(metrics["buckets"]) == 3


def test_api_pre_earnings_state_returns_expected_schema(client, monkeypatch):
    class DummyAgent:
        def get_state(self):
            return {
                "as_of_utc": "2026-02-26T10:00:00+00:00",
                "lookahead_days": 7,
                "flags": {
                    "news_available": True,
                    "earnings_available": True,
                    "ollama_used": False,
                },
                "inputs_used": {
                    "news_cache_age_seconds": 40,
                    "earnings_cache_age_seconds": 120,
                },
                "items": [
                    {
                        "symbol": "AAPL",
                        "earnings_time_utc": "2026-02-27T21:00:00+00:00",
                        "session": "AMC",
                        "confirmed": True,
                        "gap_risk": 81,
                        "direction_bias": 46,
                        "expected_move_pct": 6.2,
                        "historical_median_move_pct": 5.8,
                        "badges": ["HIGH_GAP_RISK"],
                        "why": ["Deterministic score from cached data"],
                        "what_to_watch": ["Guidance tone vs consensus"],
                        "top_news": [
                            {
                                "published_utc": "2026-02-26T08:00:00+00:00",
                                "source": "FMP",
                                "title": "AAPL pre-earnings preview",
                                "url": "https://example.com/aapl",
                            }
                        ],
                        "contributors": [
                            {
                                "name": "historical_move",
                                "value": 0.7,
                                "weight": 0.25,
                                "note": "Median prior earnings move proxy",
                            }
                        ],
                        "confidence": {
                            "gap_risk": {"low": 72, "high": 88, "score": 0.7},
                            "direction_bias": {"low": 30, "high": 62, "score": 0.65},
                        },
                    }
                ],
                "status": {"next_run_in": 500, "last_recalibration_quarter": "2026Q1"},
                "calibration": {"sample_size": 12, "brier": 0.18, "mae": 0.29, "buckets": []},
            }

    monkeypatch.setattr("api.main._agent_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr("agents.pre_earnings_agent.get_pre_earnings_agent", lambda: DummyAgent())

    response = client.get("/api/pre-earnings")
    assert response.status_code == 200
    data = response.json()

    assert data["disabled"] is False
    assert {"as_of_utc", "lookahead_days", "flags", "inputs_used", "items", "status", "calibration"} <= set(data.keys())
    assert isinstance(data["items"], list) and data["items"]
    first = data["items"][0]
    expected_item_keys = {
        "symbol",
        "earnings_time_utc",
        "session",
        "confirmed",
        "gap_risk",
        "direction_bias",
        "expected_move_pct",
        "historical_median_move_pct",
        "badges",
        "why",
        "what_to_watch",
        "top_news",
        "contributors",
        "confidence",
    }
    assert expected_item_keys <= set(first.keys())


def test_api_pre_earnings_refresh_returns_409_when_disabled(client, monkeypatch):
    monkeypatch.setattr("api.main._agent_enabled", lambda *args, **kwargs: False)

    response = client.post("/api/pre-earnings/refresh")
    assert response.status_code == 409
    assert response.json()["detail"] == "agent disabled"


def test_api_pre_earnings_refresh_runs_when_enabled(client, monkeypatch):
    class DummyAgent:
        def run_once(self):
            return {"as_of_utc": "2026-02-26T10:05:00+00:00", "items": []}

    monkeypatch.setattr("api.main._agent_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr("agents.pre_earnings_agent.get_pre_earnings_agent", lambda: DummyAgent())

    response = client.post("/api/pre-earnings/refresh")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["data"]["as_of_utc"] == "2026-02-26T10:05:00+00:00"


def test_api_pre_earnings_refresh_prefers_async_request_when_available(client, monkeypatch):
    class DummyAgent:
        def __init__(self):
            self.coverage_called = None

        def start(self):
            return None

        def request_refresh(self):
            return {"status": "accepted"}

        def request_coverage_warmup(self, **kwargs):
            self.coverage_called = kwargs
            return {"status": "accepted", "total": 5, "candidate_total": 181}

        def run_once(self):
            raise AssertionError("run_once should not be used when request_refresh exists")

    agent = DummyAgent()
    monkeypatch.setattr("api.main._agent_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr("agents.pre_earnings_agent.get_pre_earnings_agent", lambda: agent)

    response = client.post("/api/pre-earnings/refresh")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    assert data["async"] is True
    assert data["coverage"]["status"] == "accepted"
    assert agent.coverage_called == {
        "symbols": [],
        "batch_size": 5,
        "only_needs_coverage": True,
        "allow_remote_on_miss": True,
    }


def test_api_pre_earnings_symbol_refresh_enriches_all_inputs(client, monkeypatch):
    class DummyAgent:
        def __init__(self):
            self.symbol = None
            self.started = False

        def start(self):
            self.started = True

        def refresh_symbol_analysis(self, symbol):
            self.symbol = symbol
            return {
                "status": "ok",
                "symbol": symbol,
                "calendar": {"status": "ok"},
                "coverage": {"status": "accepted", "total": 1},
            }

        def refresh_symbol_calendar(self, _symbol):
            raise AssertionError("analysis refresh must be preferred")

    agent = DummyAgent()
    monkeypatch.setattr("api.main._agent_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr("agents.pre_earnings_agent.get_pre_earnings_agent", lambda: agent)

    response = client.post("/api/pre-earnings/refresh-symbol?symbol=gct")

    assert response.status_code == 200
    payload = response.json()
    assert agent.started is True
    assert agent.symbol == "GCT"
    assert payload["result"]["calendar"]["status"] == "ok"
    assert payload["result"]["coverage"]["status"] == "accepted"


def test_api_pre_earnings_symbol_refresh_rejects_malformed_symbol(client, monkeypatch):
    monkeypatch.setattr("api.main._agent_enabled", lambda *args, **kwargs: True)

    response = client.post(
        "/api/pre-earnings/refresh-symbol",
        params={"symbol": "AAPL<script>"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid symbol"


def test_api_pre_earnings_refresh_status_is_lightweight(client, monkeypatch):
    class DummyAgent:
        def get_refresh_status(self):
            return {
                "state_revision": 42,
                "status": {
                    "last_success_time": "2026-08-10T12:00:00+00:00",
                    "next_run_in": 120,
                },
                "disabled": False,
            }

        def get_coverage_warmup_status(self):
            return {"running": False, "processed": 5, "total": 5}

    monkeypatch.setattr(
        "agents.pre_earnings_agent.get_pre_earnings_agent",
        lambda: DummyAgent(),
    )

    response = client.get("/api/pre-earnings/refresh-status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["state_revision"] == 42
    assert payload["status"]["last_success_time"] == "2026-08-10T12:00:00+00:00"
    assert payload["status"]["coverage_warmup"]["running"] is False
    assert "items" not in payload
    assert len(response.content) < 2048


def test_api_pre_earnings_coverage_warmup_returns_409_when_disabled(client, monkeypatch):
    monkeypatch.setattr("api.main._agent_enabled", lambda *args, **kwargs: False)
    response = client.post("/api/pre-earnings/coverage/warmup", json={})
    assert response.status_code == 409
    assert response.json()["detail"] == "agent disabled"


def test_api_pre_earnings_coverage_warmup_accepts_with_batch_5(client, monkeypatch):
    class DummyAgent:
        def __init__(self):
            self.called = None

        def start(self):
            return None

        def request_coverage_warmup(self, symbols=None, batch_size=5, only_needs_coverage=True, allow_remote_on_miss=True):
            self.called = {
                "symbols": list(symbols or []),
                "batch_size": batch_size,
                "only_needs_coverage": only_needs_coverage,
                "allow_remote_on_miss": allow_remote_on_miss,
            }
            return {"status": "accepted", "total": 2, "batch_size": batch_size}

    agent = DummyAgent()
    monkeypatch.setattr("api.main._agent_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr("agents.pre_earnings_agent.get_pre_earnings_agent", lambda: agent)

    response = client.post(
        "/api/pre-earnings/coverage/warmup",
        json={
            "symbols": ["ODD", "RCKY"],
            "batch_size": 50,  # should be clamped to 5
            "only_needs_coverage": True,
            "allow_remote_on_miss": True,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    assert agent.called is not None
    assert agent.called["batch_size"] == 5
    assert agent.called["symbols"] == ["ODD", "RCKY"]


def test_api_pre_earnings_coverage_warmup_status_reads_agent(client, monkeypatch):
    class DummyAgent:
        def get_coverage_warmup_status(self):
            return {
                "running": True,
                "job_id": "cov-1",
                "total": 10,
                "processed": 3,
                "success": 3,
                "failed": 0,
                "batch_size": 5,
            }

    monkeypatch.setattr("agents.pre_earnings_agent.get_pre_earnings_agent", lambda: DummyAgent())
    response = client.get("/api/pre-earnings/coverage/warmup")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["coverage_warmup"]["running"] is True
    assert data["coverage_warmup"]["processed"] == 3


# ---- detect_already_reported_symbols (news-based "already reported" detection) ----

def test_already_reported_detector_flags_post_earnings_news_with_near_term_calendar_event():
    """CORT-shaped: ≥2 recent post-earnings headlines + a near-term calendar event
    (the wrong-future-date case the date-based stale filter can't catch) → flagged."""
    now = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
    news_index = {
        "CORT": [
            {
                "title": "CORT Q2 Earnings & Revenues Beat Estimates, 2026 Outlook Raised",
                "snippet": "Results surpass estimates; guidance raised",
                "published_utc": (now - timedelta(hours=6)).isoformat(),
            },
            {
                "title": "Earnings Call Highlights: CORT Record Revenue",
                "snippet": "Call recap after the report",
                "published_utc": (now - timedelta(hours=4)).isoformat(),
            },
        ]
    }
    earnings_index = {"CORT": [{"date": (now + timedelta(days=3)).isoformat(), "when": "amc"}]}

    flagged = pe.detect_already_reported_symbols(
        news_index=news_index, earnings_index=earnings_index, now=now, min_hits=2
    )

    assert "CORT" in flagged
    assert flagged["CORT"]["reason"] == "ALREADY_REPORTED"
    assert flagged["CORT"]["hits"] >= 2
    assert flagged["CORT"]["headlines"]


def test_already_reported_detector_ignores_when_no_near_term_calendar_event():
    """Post-earnings news but the only calendar event is a far-future next-quarter
    print → NOT flagged (legitimate future earnings, not a stale 'this' earnings)."""
    now = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
    news_index = {
        "CORT": [
            {
                "title": "CORT Q2 Earnings & Revenues Beat Estimates, 2026 Outlook Raised",
                "snippet": "Results beat",
                "published_utc": (now - timedelta(hours=6)).isoformat(),
            },
            {
                "title": "Earnings Call Highlights: CORT Record Revenue",
                "snippet": "Call recap",
                "published_utc": (now - timedelta(hours=4)).isoformat(),
            },
        ]
    }
    earnings_index = {"CORT": [{"date": (now + timedelta(days=60)).isoformat(), "when": "amc"}]}

    flagged = pe.detect_already_reported_symbols(
        news_index=news_index, earnings_index=earnings_index, now=now, min_hits=2
    )

    assert flagged == {}


def test_already_reported_detector_ignores_future_earnings_preview_news():
    """Genuine upcoming earnings: 'earnings preview' headlines (future cues) + a
    near-term event → NOT flagged (the earnings has NOT happened yet)."""
    now = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
    news_index = {
        "AAPL": [
            {
                "title": "AAPL earnings preview: what to expect ahead of earnings",
                "snippet": "Upcoming earnings outlook",
                "published_utc": (now - timedelta(hours=3)).isoformat(),
            },
            {
                "title": "Apple (AAPL) expected to report Q3 earnings next week",
                "snippet": "Ahead of earnings",
                "published_utc": (now - timedelta(hours=2)).isoformat(),
            },
        ]
    }
    earnings_index = {"AAPL": [{"date": (now + timedelta(days=5)).isoformat(), "when": "amc"}]}

    flagged = pe.detect_already_reported_symbols(
        news_index=news_index, earnings_index=earnings_index, now=now, min_hits=2
    )

    assert flagged == {}


def test_already_reported_detector_does_not_expire_expected_growth_preview_from_prior_quarter_summary():
    now = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
    news_index = {
        "DELL": [
            {
                "ticker": "DELL",
                "title": "Dell Technologies (DELL) Earnings Expected to Grow: Should You Buy?",
                "summary": "Dell beat revenue estimates in the prior quarter and reported stronger margins.",
                "published_utc": (now - timedelta(hours=2)).isoformat(),
                "_pe_relevance": 1.0,
            }
        ],
        "HPE": [
            {
                "ticker": "HPE",
                "title": "Is Hewlett Packard Enterprise (HPE) Undervalued As Analyst Bets Build On An Earnings Beat?",
                "summary": "The company reported results last quarter; the next earnings event is approaching.",
                "published_utc": (now - timedelta(hours=3)).isoformat(),
                "_pe_relevance": 1.0,
            }
        ],
    }
    earnings_index = {
        "DELL": [{"date": (now + timedelta(days=2)).isoformat(), "when": "unknown"}],
        "HPE": [{"date": (now + timedelta(days=3)).isoformat(), "when": "amc"}],
    }

    flagged = pe.detect_already_reported_symbols(
        news_index=news_index,
        earnings_index=earnings_index,
        now=now,
        min_hits=1,
    )

    assert flagged == {}


def test_already_reported_detector_does_not_expire_results_date_announcement():
    now = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
    news_index = {
        "GASS": [{
            "ticker": "GASS",
            "title": "StealthGas Announces the Date for the Release of Second Quarter 2026 Results",
            "summary": "The conference call is scheduled for next week.",
            "published_utc": (now - timedelta(hours=2)).isoformat(),
            "_pe_relevance": 1.0,
        }]
    }
    earnings_index = {
        "GASS": [{"date": (now + timedelta(days=1)).isoformat(), "when": "bmo"}]
    }

    flagged = pe.detect_already_reported_symbols(
        news_index=news_index,
        earnings_index=earnings_index,
        now=now,
        min_hits=1,
    )

    assert flagged == {}


def test_already_reported_detector_requires_min_hits():
    """A single post-earnings headline is not enough evidence → NOT flagged."""
    now = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
    news_index = {
        "CORT": [
            {
                "title": "CORT Q2 Earnings & Revenues Beat Estimates",
                "snippet": "Results beat",
                "published_utc": (now - timedelta(hours=6)).isoformat(),
            },
        ]
    }
    earnings_index = {"CORT": [{"date": (now + timedelta(days=3)).isoformat(), "when": "amc"}]}

    flagged = pe.detect_already_reported_symbols(
        news_index=news_index, earnings_index=earnings_index, now=now, min_hits=2
    )

    assert flagged == {}


def test_already_reported_detector_ignores_old_post_earnings_news():
    """Post-earnings headlines published long ago (> recent_hours) are stale evidence
    and may refer to a prior quarter → NOT flagged."""
    now = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
    news_index = {
        "CORT": [
            {
                "title": "CORT Q1 Earnings & Revenues Beat Estimates, Outlook Raised",
                "snippet": "Results beat",
                "published_utc": (now - timedelta(days=20)).isoformat(),
            },
            {
                "title": "Earnings Call Highlights: CORT Record Revenue",
                "snippet": "Call recap",
                "published_utc": (now - timedelta(days=19)).isoformat(),
            },
        ]
    }
    earnings_index = {"CORT": [{"date": (now + timedelta(days=3)).isoformat(), "when": "amc"}]}

    flagged = pe.detect_already_reported_symbols(
        news_index=news_index, earnings_index=earnings_index, now=now, min_hits=2, recent_hours=72
    )

    assert flagged == {}


# ─── Preselect session-aware ranking (2026-08-03) ────────────────────────────
# Calendar rows are date-only (midnight UTC). A raw |event_dt - now| proximity
# rank makes a TODAY AMC ticker (reporting ~20:00 UTC) look ~18h in the past at
# 18:00 UTC, scoring it BELOW a tomorrow reporter and getting it evicted by the
# 400-candidate preselect cap before it is ever built (PLTR was rank #639/3181).
# The session-aware effective delta mirrors `_earnings_reported_status`.


def test_nasdaq_discovery_date_remains_low_confidence_until_verified():
    now = datetime.now(timezone.utc)
    rows = pe.PreEarningsAgent._canonicalize_symbol_events(
        "AMAT",
        [{
            "symbol": "AMAT",
            "date": (now + timedelta(days=2)).isoformat(),
            "source": "nasdaq_zacks_estimate",
            "confirmed": False,
            "when": "amc",
        }],
        now=now,
    )

    assert len(rows) == 1
    assert rows[0]["event_status"] == "UPCOMING"
    assert rows[0]["source_priority"] == 50
    assert rows[0]["date_confidence"] == "low"


def test_effective_event_delta_today_amc_upcoming_is_zero():
    """A today-AMC event before 22:00 UTC is imminent -> delta 0 (top score)."""
    now = datetime.now(timezone.utc).replace(hour=18, minute=0, second=0, microsecond=0)
    event_dt = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc)
    delta = pe.PreEarningsAgent._effective_event_delta_days(event_dt, "AMC", now)
    assert delta == 0.0


def test_effective_event_delta_today_bmo_reported_is_far():
    """A today-BMO event after 14:30 UTC has already reported -> delta 10d."""
    now = datetime.now(timezone.utc).replace(hour=15, minute=0, second=0, microsecond=0)
    event_dt = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc)
    delta = pe.PreEarningsAgent._effective_event_delta_days(event_dt, "BMO", now)
    assert delta == 10.0


def test_effective_event_delta_future_is_real_distance():
    """A future event keeps its real (positive) distance."""
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    event_dt = now + timedelta(days=3)
    delta = pe.PreEarningsAgent._effective_event_delta_days(event_dt, "AMC", now)
    assert delta is not None
    assert 2.9 <= delta <= 3.1


def _today_amc_event(symbol, now):
    return {
        "symbol": symbol,
        "date": datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc).isoformat(),
        "when": "amc",
    }


def _future_event(symbol, now, days_ahead):
    return {
        "symbol": symbol,
        # midnight UTC of a future day — the real calendar storage shape
        "date": datetime.combine(
            now.date() + timedelta(days=days_ahead), datetime.min.time(), tzinfo=timezone.utc
        ).isoformat(),
        "when": "amc",
    }


def test_preselect_keeps_today_amc_when_over_cap():
    """Core regression: with >400 candidates, a today-AMC ticker (PLTR) must
    survive the preselect cap. Before the fix, tomorrow tickers outranked it
    and it was dropped at rank #639."""
    agent = pe.PreEarningsAgent.__new__(pe.PreEarningsAgent)
    now = datetime.now(timezone.utc).replace(hour=18, minute=0, second=0, microsecond=0)
    earnings_index = {"PLTR": [_today_amc_event("PLTR", now)]}
    # 499 tickers ALL reporting tomorrow (day-1). Under the old raw-midnight
    # rank, tomorrow's midnight (6h ahead) outscores today-AMC's midnight (18h
    # behind), so all 499 outrank PLTR -> PLTR ranks #500 -> evicted at cap 400.
    for i in range(499):
        sym = f"FUT{i:03d}"
        earnings_index[sym] = [_future_event(sym, now, days_ahead=1)]
    candidates = list(earnings_index.keys())
    selected = agent._preselect_candidate_symbols(
        candidate_symbols=candidates,
        earnings_index=earnings_index,
        news_index={},
        mismatch={},
        now=now,
        max_results=200,  # limit = max(300, 200*2) = 400
    )
    assert "PLTR" in selected, "today-AMC ticker was evicted by the preselect cap"


def test_preselect_ranks_today_amc_above_tomorrow():
    """A today-AMC ticker must rank ahead of a tomorrow ticker so it is never
    demoted below tomorrow by the midnight-UTC delta. Uses >400 candidates so
    the ranking path actually runs (preselect short-circuits at <=limit)."""
    agent = pe.PreEarningsAgent.__new__(pe.PreEarningsAgent)
    now = datetime.now(timezone.utc).replace(hour=18, minute=0, second=0, microsecond=0)
    earnings_index = {"PLTR": [_today_amc_event("PLTR", now)]}
    for i in range(499):
        sym = f"FUT{i:03d}"
        earnings_index[sym] = [_future_event(sym, now, days_ahead=1)]
    selected = agent._preselect_candidate_symbols(
        candidate_symbols=list(earnings_index.keys()),
        earnings_index=earnings_index,
        news_index={},
        mismatch={},
        now=now,
        max_results=200,
    )
    assert "PLTR" in selected
    # PLTR (today-AMC, imminent) outranks every tomorrow ticker -> ranks #1.
    assert selected[0] == "PLTR", (
        "today-AMC must outrank tomorrow; the midnight-UTC penalty is gone"
    )


def test_preselect_stages_nearest_first_even_when_batch_is_below_cap():
    agent = pe.PreEarningsAgent.__new__(pe.PreEarningsAgent)
    now = datetime(2026, 8, 30, 12, tzinfo=timezone.utc)
    earnings_index = {
        "BB": [_future_event("BB", now, days_ahead=25)],
        "DELL": [_future_event("DELL", now, days_ahead=2)],
        "NIO": [_future_event("NIO", now, days_ahead=1)],
    }

    selected = agent._preselect_candidate_symbols(
        candidate_symbols=["BB", "DELL", "NIO"],
        earnings_index=earnings_index,
        news_index={"BB": [{"title": "many"}] * 15},
        mismatch={"BB": {"reason": "news_calendar_mismatch"}},
        now=now,
        max_results=200,
    )

    assert selected == ["NIO", "DELL", "BB"]


def test_direction_bias_includes_etoro_retail_component():
    result = pe.compute_direction_bias_score(
        "TEST",
        enriched_data={"beat_rate_historical": 0.5, "beat_rate_samples": 4},
        news_sentiment=0.0,
        retail={"buy_pct": 90.0, "sell_pct": 10.0, "popularity_rank": 500},
    )
    comp = result["components"]["retail_positioning"]
    assert comp is not None and comp > 70
    assert "c6_retail" not in result["missing_components"]


def test_direction_bias_retail_absent_is_excluded():
    result = pe.compute_direction_bias_score("TEST", enriched_data={}, news_sentiment=0.0)
    assert result["components"]["retail_positioning"] is None
    assert "c6_retail" in result["missing_components"]


def test_direction_bias_retail_damped_by_rank_and_sign_flip():
    popular = pe.compute_direction_bias_score(
        "TEST", enriched_data={}, news_sentiment=0.0,
        retail={"buy_pct": 90, "sell_pct": 10, "popularity_rank": 500},
    )
    unpopular = pe.compute_direction_bias_score(
        "TEST", enriched_data={}, news_sentiment=0.0,
        retail={"buy_pct": 90, "sell_pct": 10, "popularity_rank": 25000},
    )
    assert unpopular["components"]["retail_positioning"] < popular["components"]["retail_positioning"]
    contrarian = pe.compute_direction_bias_score(
        "TEST", enriched_data={}, news_sentiment=0.0,
        retail={"buy_pct": 90, "sell_pct": 10, "popularity_rank": 500},
        retail_sign=-1.0,
    )
    assert contrarian["components"]["retail_positioning"] < 50


def test_direction_bias_retail_weight_zero_disables_component():
    result = pe.compute_direction_bias_score(
        "TEST", enriched_data={}, news_sentiment=0.0,
        retail={"buy_pct": 90.0, "sell_pct": 10.0, "popularity_rank": 500},
        retail_weight=0.0,
    )
    assert result["components"]["retail_positioning"] is None
