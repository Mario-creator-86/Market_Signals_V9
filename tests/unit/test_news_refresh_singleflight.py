import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading

from agents.news_tab_agent import NewsArticle, NewsTabAgent, NewsTabState
from core.request_context import ExternalCallBudget, get_request_context, request_context


def _feed_cache_agent():
    agent = NewsTabAgent.__new__(NewsTabAgent)
    agent._feed_cache_ttl = 300
    agent._feed_cache_lock = threading.RLock()
    agent._feed_cache = {}
    agent._feed_cache_inflight = {}
    return agent


def test_overlapping_news_refresh_returns_cached_state_without_provider_work(monkeypatch):
    agent = NewsTabAgent.__new__(NewsTabAgent)
    agent._news_refresh_lock = threading.RLock()
    agent._news_refresh_inflight = True
    agent._news_refresh_collapsed_total = 0
    agent._cache_lock = threading.RLock()
    agent._state = NewsTabState()

    monkeypatch.setattr(
        agent,
        "_fetch_market_news",
        lambda: (_ for _ in ()).throw(AssertionError("provider work must be collapsed")),
    )

    state, articles = agent.refresh_all_news(["AAPL"])

    assert articles == []
    assert state["refresh_collapsed"] is True
    assert agent._news_refresh_inflight is True
    assert agent._news_refresh_collapsed_total == 1


def test_global_and_targeted_refresh_commit_newest_merged_snapshot(monkeypatch):
    agent = NewsTabAgent.__new__(NewsTabAgent)
    agent._news_refresh_lock = threading.RLock()
    agent._news_state_commit_lock = threading.RLock()
    agent._news_refresh_inflight = False
    agent._news_refresh_collapsed_total = 0
    agent._cache_lock = threading.RLock()
    agent._state = NewsTabState()
    agent._seen_ids = set()
    agent._last_reset_time = datetime.min.replace(tzinfo=timezone.utc)
    agent._last_market_refresh_ts = time.time()
    agent._market_refresh_ttl = 900
    agent._ticker_refresh_meta = {}
    agent._ticker_refresh_ttl = 1800
    agent._ticker_refresh_inflight = {}
    agent._enable_yahoo_per_ticker = False
    agent._news_cfg = {
        "ticker_empty_refresh_ttl_seconds": 0,
        "ticker_stagger_batch_size": 5,
        "ticker_on_demand_days_back": 7,
    }

    global_article = NewsArticle(
        id="global-aapl",
        title="AAPL global refresh",
        url="https://example.com/global",
        source="Global",
        published_at=datetime.now(timezone.utc),
        ticker="AAPL",
        source_scope="ticker",
    )
    targeted_article = NewsArticle(
        id="targeted-aapl",
        title="AAPL targeted refresh",
        url="https://example.com/targeted",
        source="Targeted",
        published_at=datetime.now(timezone.utc),
        ticker="AAPL",
        source_scope="ticker",
    )
    global_save_entered = threading.Event()
    allow_global_save = threading.Event()
    targeted_fetch_started = threading.Event()
    persisted_snapshots = []

    monkeypatch.setattr("agents.news_tab_agent.is_news_tab_enabled", lambda: True)
    monkeypatch.setattr(agent, "_populate_company_names", lambda _tickers: None)
    monkeypatch.setattr(agent, "_get_watchlist_context", lambda include=None: list(include or []))
    monkeypatch.setattr(agent, "_article_matches_suppression", lambda _article: False)
    monkeypatch.setattr(agent, "_analyze_article", lambda _article: None)
    monkeypatch.setattr(agent, "_attach_cached_ai", lambda _article: None)
    monkeypatch.setattr(agent, "_calculate_action_score", lambda _article: 1)
    monkeypatch.setattr(agent, "_apply_cross_source_dedup", lambda articles: list(articles))
    monkeypatch.setattr(agent, "_queue_auto_micro_summaries", lambda: None)
    monkeypatch.setattr(agent, "_queue_market_narrative_refresh", lambda: None)

    def fake_fetch(_ticker, *_args, **_kwargs):
        if threading.current_thread().name == "GlobalNewsRefreshTest":
            return [global_article]
        targeted_fetch_started.set()
        return [targeted_article]

    def organize(articles, watchlist=None):
        del watchlist
        bucket = agent._state.ticker_news.setdefault("AAPL", [])
        ids = {article.id for article in bucket}
        bucket.extend(article for article in articles if article.id not in ids)

    def save_snapshot(snapshot):
        ids = {
            article["id"]
            for rows in (snapshot.get("ticker_news") or {}).values()
            for article in rows
        }
        if threading.current_thread().name == "GlobalNewsRefreshTest":
            global_save_entered.set()
            assert allow_global_save.wait(timeout=2)
        persisted_snapshots.append((threading.current_thread().name, ids))

    monkeypatch.setattr(agent, "_fetch_ticker_news", fake_fetch)
    monkeypatch.setattr(agent, "_organize_state", organize)
    monkeypatch.setattr(agent, "_save_backup_from_snapshot", save_snapshot)

    global_thread = threading.Thread(
        target=lambda: agent.refresh_all_news(["AAPL"], include_market=False),
        name="GlobalNewsRefreshTest",
    )
    global_thread.start()
    assert global_save_entered.wait(timeout=2)

    targeted_thread = threading.Thread(
        target=lambda: agent.refresh_single_ticker_news("AAPL"),
        name="TargetedNewsRefreshTest",
    )
    targeted_thread.start()
    assert targeted_fetch_started.wait(timeout=2)
    time.sleep(0.03)
    assert not any(name == "TargetedNewsRefreshTest" for name, _ids in persisted_snapshots)

    allow_global_save.set()
    global_thread.join(timeout=2)
    targeted_thread.join(timeout=2)

    assert not global_thread.is_alive()
    assert not targeted_thread.is_alive()
    assert persisted_snapshots[-1] == (
        "TargetedNewsRefreshTest",
        {"global-aapl", "targeted-aapl"},
    )
    assert {article.id for article in agent._state.ticker_news["AAPL"]} == {
        "global-aapl",
        "targeted-aapl",
    }


def test_hydrate_restores_provider_refresh_timestamps():
    agent = NewsTabAgent.__new__(NewsTabAgent)
    agent._cache_lock = threading.RLock()
    agent._state = NewsTabState()
    agent._seen_ids = set()
    agent._last_market_refresh_ts = 0.0
    agent._ticker_refresh_meta = {}

    agent.hydrate_state(
        {
            "ticker_news": {},
            "market_news": [],
            "breaking_news": [],
            "last_update": "2026-07-31T08:00:00+00:00",
            "_refresh_meta": {
                "market_ts": 1234.5,
                "ticker_ts": {"aapl": 1200, "MSFT": "1190.25", "BAD": "invalid"},
            },
        },
        reanalyze_existing=False,
    )

    assert agent._last_market_refresh_ts == 1234.5
    assert agent._ticker_refresh_meta == {"AAPL": 1200.0, "MSFT": 1190.25}


def test_feed_cache_collapses_concurrent_consumers(monkeypatch):
    agent = _feed_cache_agent()
    parsed = object()
    calls = {"count": 0}

    def slow_parse(_url):
        calls["count"] += 1
        time.sleep(0.03)
        return parsed

    monkeypatch.setattr(agent, "_parse_feed", slow_parse)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: agent._parse_feed_cached("https://example.com/feed"),
                range(8),
            )
        )

    assert results == [parsed] * 8
    assert calls["count"] == 1


def test_feed_cache_negative_result_is_short_lived_and_shared(monkeypatch):
    agent = _feed_cache_agent()
    calls = {"count": 0}

    def fail_parse(_url):
        calls["count"] += 1
        raise RuntimeError("upstream denied")

    monkeypatch.setattr(agent, "_parse_feed", fail_parse)

    for _ in range(2):
        try:
            agent._parse_feed_cached("https://example.com/denied")
        except RuntimeError as exc:
            assert "upstream denied" in str(exc)
        else:
            raise AssertionError("negative feed cache must preserve the error")

    assert calls["count"] == 1


def test_auto_refresh_runs_inside_news_agent_budget():
    agent = NewsTabAgent.__new__(NewsTabAgent)
    agent._update_thread = None
    agent._stop_event = threading.Event()
    agent._external_call_budget = 7
    observed = {}

    def capture_context(_tickers):
        ctx = get_request_context()
        observed["agent_name"] = ctx.agent_name
        observed["budget"] = ctx.external_budget.snapshot()
        agent._stop_event.set()

    agent.refresh_all_news = capture_context
    agent.start_auto_refresh(["AAPL"], interval_seconds=1)
    agent._update_thread.join(timeout=2)

    assert not agent._update_thread.is_alive()
    assert observed["agent_name"] == "news_tab"
    assert observed["budget"]["limit"] == 7


def test_refresh_defers_tickers_before_exhausting_run_budget(monkeypatch):
    agent = NewsTabAgent.__new__(NewsTabAgent)
    agent._news_refresh_lock = threading.RLock()
    agent._news_state_commit_lock = threading.RLock()
    agent._news_refresh_inflight = False
    agent._news_refresh_collapsed_total = 0
    agent._cache_lock = threading.RLock()
    agent._state = NewsTabState()
    agent._seen_ids = set()
    agent._last_reset_time = datetime.min.replace(tzinfo=timezone.utc)
    agent._last_market_refresh_ts = 0.0
    agent._market_refresh_ttl = 900
    agent._ticker_refresh_meta = {}
    agent._ticker_refresh_ttl = 0
    agent._enable_yahoo_per_ticker = False
    agent._news_cfg = {"ticker_empty_refresh_ttl_seconds": 0, "ticker_stagger_batch_size": 5}
    agent._feed_cache_ttl = 300
    agent._feed_cache_lock = threading.RLock()
    agent._feed_cache = {}
    agent._feed_cache_inflight = {}
    agent._yahoo_news_cache = {}
    calls = []

    monkeypatch.setattr("agents.news_tab_agent.is_news_tab_enabled", lambda: True)
    monkeypatch.setattr(
        "agents.news_tab_agent.get_finnhub_client",
        lambda: type("UnavailableFinnhub", (), {"is_available": lambda self: False})(),
    )
    monkeypatch.setattr(agent, "_populate_company_names", lambda _tickers: None)
    monkeypatch.setattr(agent, "_apply_cross_source_dedup", lambda articles: list(articles))
    monkeypatch.setattr(agent, "_organize_state", lambda articles, watchlist=None: None)
    monkeypatch.setattr(agent, "_save_backup_from_snapshot", lambda snapshot: None)
    monkeypatch.setattr(agent, "_queue_auto_micro_summaries", lambda: None)
    monkeypatch.setattr(agent, "_queue_market_narrative_refresh", lambda: None)

    def budgeted_fetch(ticker, *_args, **_kwargs):
        calls.append(ticker)
        assert get_request_context().external_budget.reserve("test_news")
        return []

    monkeypatch.setattr(agent, "_fetch_ticker_news", budgeted_fetch)
    budget = ExternalCallBudget(limit=2)
    with request_context(reason="TEST", agent_name="news_tab", external_budget=budget):
        agent.refresh_all_news(["AAA", "BBB", "CCC"], include_market=False)

    assert calls == ["AAA", "BBB"]
    assert budget.snapshot()["used"] == 2
    assert budget.snapshot()["blocked"] == 0
