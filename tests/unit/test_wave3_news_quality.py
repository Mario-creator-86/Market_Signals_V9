"""Wave 3 - news quality: impact calibration, breaking window, company names,
text cleaning, engine process role, briefing attach.

Live evidence 2026-11-15 (Sunday): 144 stories in 24 h from 11 sources and
"Market-moving: 0 - 0 high/critical impact - 0 breaking"; every market story
read "Low 15". The archive that day held a hot PPI print, Fed-rate fears,
Bitcoin at a 12-week low and the Nvidia CEO resignation.

The headline fixture below is REAL: every title is taken verbatim from
``data/news_backup.json`` as it stood on 2026-11-15 at 12:34 local time, before
this change was live (public headlines and public eToro posts; the social
titles are the raw ones, with the ".: " join and the hard cut). Each one
carries the band it must land in, so the scoring cannot drift back to all-low
or all-high.
"""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import agents.news_tab_agent as nta
from agents.news_tab_agent import (
    ACTION_SOCIAL_MAX,
    BREAKING_WINDOW_MINUTES,
    NewsArticle,
    NewsImpact,
    NewsSentiment,
    NewsTabAgent,
    get_breaking_window_minutes,
    set_breaking_window_minutes,
)
from core.news_v9 import company_names, impact_rules
from core.news_v9.finbert import FinancialSentimentResult
from core.news_v9.text import clean_news_text, social_post_title, truncate_at_word

pytestmark = pytest.mark.unit

LOW, MEDIUM, HIGH, CRITICAL = "low", "medium", "high", "critical"

# (headline, ticker or None, source provider, allowed bands)
ARCHIVE_HEADLINES = [
    # -- market-wide events and large moves: must rank high on the headline --
    ("Bitcoin Hits 12-Week Low as Fed Rate Fears Undo Most of Summer Rally", None, "decrypt", {HIGH}),
    ("Ethereum, XRP, PI fall as US PPI inflation reaches 6.5%", None, "crypto_news", {HIGH}),
    ("Crypto market sees brief relief after cooler US PPI data", None, "crypto_news", {HIGH}),
    ("Bitcoin falls to four-month low near $62K as crypto selloff deepens", None, "cointelegraph", {HIGH}),
    ("Bitcoin crashes to 3-month low below $63,000 as analyst flags fourth capitulation wave", None, "coindesk", {HIGH}),
    ("Bitcoin dips below $70,000 as Dollar Index hits 19-month high", None, "coindesk", {HIGH}),
    # -- company-specific material events ------------------------------------
    ("Jensen Huang Resigns as Nvidia CEO, With CFO Colette Kress Named Interim Chief", None, "decrypt", {HIGH}),
    ("Jensen Huang resigns as Nvidia CEO amid scandal", "NVDA", "yahoo_rss", {HIGH}),
    ("Nvidia stock suffers longest weekly losing streak since 2012 as market value sinks by $1.7 trillion",
     None, "cnbc_top", {HIGH}),
    ("MOD Stock Jumps 30% as Samsung Reportedly Explores Acquisition of Modine", "MOD", "yahoo_rss", {HIGH}),
    ("Samsung in talks to buy data center cooling firm Modine, Bloomberg reports", "MOD", "yahoo_rss", {HIGH}),
    ("Stantec (STN) Tops Q3 Earnings and Revenue Estimates", "STN", "yahoo_rss", {HIGH}),
    ("Sigma Lithium Corporation (SGML) Reports Q3 Loss, Misses Revenue Estimates", "SGML", "yahoo_rss", {HIGH}),
    ("Liftoff Mobile shares surge 25% as AI advertising growth drives earnings beat", "LFTO", "yahoo_rss", {HIGH}),
    ("Wolverine World Wide Raises FY2026 Guidance as Q3 Revenue Rises 7.5% to $505.4 Million",
     "WWW", "yahoo_rss", {HIGH}),
    ("Nebius Tumbles 14% on Soft Guidance and Bigger Spending Plans; CoreWeave Sinks 10%, IREN Drops 7%",
     "IREN", "yahoo_rss", {HIGH}),
    ("Capri Holdings (CPRI) Names Justin Boxford CEO In 2027 Leadership Handover", "CPRI", "yahoo_rss", {HIGH}),
    ("Intellia Therapeutics' One-Time HAE Therapy Cuts Attacks 87% in Trial as FDA Decision Nears",
     "NTLA", "yahoo_rss", {HIGH}),
    ("Stock Market Today, Nov. 13: AMD Sinks 8.75% on Sector AI-Chip Sell-Off After Early Gains Reverse",
     "AMD", "yahoo_rss", {HIGH}),
    ("Nu Holdings Stocks Jump 7.72% After Record $1.1 Billion Quarterly Profit", "NU", "yahoo_rss", {HIGH}),
    # -- routine company news, follow-up coverage, scheduling: medium ---------
    ("Stantec (STN) Q3 2026 Earnings Call Transcript", "STN", "yahoo_rss", {MEDIUM}),
    ("NU Q3 Earnings Call Highlights", "NU", "yahoo_rss", {MEDIUM}),
    ("Xcel Brands to Host Third Quarter 2026 Earnings Call on November 16, 2026", "XELB", "yahoo_rss", {MEDIUM}),
    ("PDD Holdings to Report Third Quarter 2026 Unaudited Financial Results on November 19, 2026",
     "PDD", "yahoo_rss", {MEDIUM}),
    ("Applied Digital (APLD) Uses Augmented Reality At Polaris Forge 1", "APLD", "yahoo_rss", {MEDIUM}),
    ("AT&T (T) Starts Work On 54 Acre Plano Headquarters Campus", "T", "yahoo_rss", {MEDIUM}),
    ("Klarna Names Chris Koegel as Chief Financial Officer", "KLAR", "yahoo_rss", {MEDIUM}),
    ("Super Micro Computer (SMCI) Under DOJ Scrutiny Over Alleged Server Smuggling To China",
     "SMCI", "yahoo_rss", {MEDIUM}),
    ("Tractor Supply partners with Uber to offer on-demand delivery", "UBER", "yahoo_rss", {MEDIUM}),
    ("Keurig Dr Pepper Announces Pricing of Public Offering of Common Stock", "KDP", "yahoo_rss", {MEDIUM}),
    ("Microsoft apologizes for massive outage that took down Outlook and other 365 services",
     None, "cnbc_top", {MEDIUM}),
    ("AI risks are the biggest threat to the financial system, Fed survey finds", None, "cnbc_top", {MEDIUM}),
    ("Kraken parent Payward’s Q3 earnings fall 67% as crypto trading slows", None, "crypto_news", {MEDIUM}),
    ("Bitcoin plunges to $68,000 as Anthropic IPO frenzy adds to market jitters", None, "coindesk", {MEDIUM}),
    # -- commentary, listicles, price-prediction filler: low ------------------
    ("XRP price prediction after the $1 break. Is $0.80 back in play?", None, "crypto_news", {LOW}),
    ("How Low Can Bitcoin Go by the End of 2026 After Losing $68K? 3 AIs Gave Us Brutal Targets",
     None, "cryptopotato", {LOW}),
    ("When Will the Next Crypto Bull Run Finally Begin? 3 AIs Reveal Their Predictions", None, "cryptopotato", {LOW}),
    ("Top 5 prediction market platforms to watch in 2026", None, "crypto_news", {LOW}),
    ("Ethereum and Layer 1s Explained", None, "theblock", {LOW}),
    ("Here’s what happened in crypto today", None, "cointelegraph", {LOW}),
    ("Meet the seven co-founders of Anthropic", None, "cnbc_top", {LOW}),
    ("Could Budapest be the next digital nomad hotspot?", None, "cnbc_top", {LOW}),
    ("I’m 63 and spending $95,000 on a roof deck for my $500,000 home. Is that a good investment?",
     None, "marketwatch", {LOW}),
    ("Whale Alert: Four Traders Liquidated for More Than $10M Apiece", None, "cryptopotato", {LOW}),
    ("Is Walmart (WMT) Stock Too Expensive to Buy Now?", "WMT", "yahoo_rss", {LOW}),
    ("Dutch Bros vs. Wendy's: Which Stock Is a Better Buy?", "BROS", "yahoo_rss", {LOW}),
    ("Where Will IonQ Stock Be in 5 Years?", "IONQ", "yahoo_rss", {LOW}),
    ("Nu Holdings: 3 Key Takeaways for Long-Term Investors", "NU", "yahoo_rss", {LOW}),
    ("Bull of the Day: Coherent (COHR)", "COHR", "yahoo_rss", {LOW}),
    ("Prediction: This Quantum Computing Stock Will Be Worth More Than IonQ 5 Years From Now",
     "IONQ", "yahoo_rss", {LOW}),
    ("Cipher Digital (CIFR) On Texas Approval Risks And Earnings, Is The Stock Fully Valued?",
     "CIFR", "yahoo_rss", {LOW}),
    ("The Home Depot, Inc. (HD) Is a Trending Stock: Facts to Know Before Betting on It", "HD", "yahoo_rss", {LOW}),
    ("Baidu to Report Q3 Earnings: What's in Store for the Stock?", "BIDU", "yahoo_rss", {LOW}),
    # -- social posts (eToro): always low -------------------------------------
    ("$NVDA Could this be a good time to buy ?: What are your thoughts", "NVDA", "etoro", {LOW}),
    ("9 Reasons #Bitcoin Crashed From $81K to $67K in Just 10 Days", "NVDA", "etoro", {LOW}),
    ("US Stocks Rise, But Late-Week Volatility Hits AI and Bonds.", "AI", "etoro", {LOW}),
    ("🗓️ WEEK IN REVIEW: Nvidia dominated headlines after CEO Jensen Huang's exit (Colette Kress now inte",
     None, "etoro", {LOW}),
    ("Sunday calm — weekend markets.: Flash: Nasdaq closed down 0.9% Friday as the 10-year yield topped 5% and Nvidia ",
     "NVDA", "etoro", {LOW}),
]


@pytest.fixture(autouse=True)
def _restore_breaking_window():
    yield
    set_breaking_window_minutes(BREAKING_WINDOW_MINUTES)


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.delenv("MS_PROCESS_ROLE", raising=False)
    instance = NewsTabAgent()
    instance.clear_cache()
    monkeypatch.setattr(
        instance,
        "_analyze_sentiment_result",
        lambda text: FinancialSentimentResult(label="negative", score=-0.9, confidence=0.9, source="finbert"),
    )
    return instance


def _article(title, *, ticker=None, provider="yahoo_rss", age_minutes=10, summary="", article_id=None, **extra):
    social = provider == "etoro"
    return NewsArticle(
        id=article_id or (("etoro_" if social else "a_") + str(abs(hash((title, ticker, age_minutes))) % 10**12)),
        title=title,
        url="https://example.com/story",
        source="eToro (@someuser)" if social else provider,
        published_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
        ticker=ticker,
        summary=summary,
        source_provider=provider,
        **extra,
    )


def _scored(agent, title, **kwargs):
    article = _article(title, **kwargs)
    agent._analyze_article(article)
    return article


# ── 1. impact calibration on real archive headlines ─────────────────────────


def test_archive_fixture_is_small_and_real():
    assert len(ARCHIVE_HEADLINES) < 60
    assert len({row[0] for row in ARCHIVE_HEADLINES}) == len(ARCHIVE_HEADLINES)


@pytest.mark.parametrize("title,ticker,provider,bands", ARCHIVE_HEADLINES, ids=[r[0][:48] for r in ARCHIVE_HEADLINES])
def test_archive_headline_lands_in_its_band(agent, title, ticker, provider, bands):
    article = _scored(agent, title, ticker=ticker, provider=provider)

    assert article.impact.value in bands, (article.impact_score, article.impact_reasons)
    # Explainable: the rules that fired travel with the article.
    assert article.impact_reasons, title
    assert all(isinstance(reason, str) and reason for reason in article.impact_reasons)


def test_archive_fixture_is_neither_all_low_nor_all_high(agent):
    publisher = [row for row in ARCHIVE_HEADLINES if row[2] != "etoro"]
    scored = [_scored(agent, title, ticker=ticker, provider=provider) for title, ticker, provider, _ in publisher]
    levels = [a.impact.value for a in scored]

    assert levels.count(HIGH) + levels.count(CRITICAL) == sum(
        1 for row in publisher if row[3] & {HIGH, CRITICAL}
    )
    assert levels.count(LOW) == sum(1 for row in publisher if row[3] == {LOW})
    assert levels.count(MEDIUM) == sum(1 for row in publisher if row[3] == {MEDIUM})
    # The fixture mixes the three bands on purpose; none may swallow the rest.
    assert 0.2 < (levels.count(HIGH) + levels.count(CRITICAL)) / len(levels) < 0.5
    assert 0.2 < levels.count(LOW) / len(levels) < 0.5
    # Social posts never leave LOW and never reach the actionable band.
    for title, ticker, provider, _ in ARCHIVE_HEADLINES:
        if provider == "etoro":
            post = _scored(agent, title, ticker=ticker, provider=provider)
            assert post.impact == NewsImpact.LOW
            assert post.action_score <= ACTION_SOCIAL_MAX
            assert post.is_breaking is False


def test_central_bank_decisions_and_leading_prints_are_critical(agent):
    for title in (
        "Fed holds rates steady, signals one cut in 2027",
        "ECB cuts interest rates by 25 basis points",
        "US PPI inflation reaches 6.5%, hottest since 2022",
        "Jobs report: US adds 250,000 jobs, unemployment rate falls to 3.9%",
    ):
        article = _scored(agent, title)
        assert article.impact == NewsImpact.CRITICAL, (title, article.impact_score, article.impact_reasons)
        assert article.is_breaking is True


def test_upcoming_or_passing_macro_mentions_are_not_the_event(agent):
    upcoming = _scored(agent, "Bitcoin volatility nears 2026 lows as traders brace for Fed decision and US midterms")
    assert upcoming.impact in (NewsImpact.LOW, NewsImpact.MEDIUM)
    assert upcoming.is_breaking is False
    assert any("upcoming macro event" in r for r in upcoming.impact_reasons)

    # A company whose name is a macro acronym is not an inflation print.
    company = _scored(agent, "CPI Aerostructures to Report Third Quarter 2026 Results on November 16, 2026",
                      ticker="CVU")
    assert company.impact == NewsImpact.MEDIUM
    assert company.is_breaking is False

    # A stock's move is not the index's move.
    assert impact_rules.evaluate_headline("Stocks slip as Nvidia falls 8%").market_points == 0
    # ... and a percentage that is not a price move is not a market move.
    assert impact_rules.evaluate_headline(
        "Ethereum scaling project MegaETH slashes staff by 20% as it pursues AI business"
    ).market_points == 0
    assert impact_rules.evaluate_headline("Bitcoin drops 9% in biggest one-day fall since March").market_points >= 35
    assert impact_rules.evaluate_headline("S&P 500 falls 2.3% as tech sells off").market_points >= 35
    assert impact_rules.evaluate_headline("Dow drops 900 points as bank fears spread").market_points >= 35


def test_headline_evidence_decides_not_the_word_earnings(agent):
    result = _scored(agent, "Almonty Industries Inc. (ALM) Misses Q3 Earnings and Revenue Estimates", ticker="ALM")
    preview = _scored(agent, "AECOM Q4 Earnings Ahead: What Should Investors Expect?", ticker="ACM")
    valuation = _scored(agent, "Intuit (INTU) Could Be 39% Undervalued On Earnings Focus", ticker="INTU")

    assert result.impact == NewsImpact.HIGH and any("earnings result" in r for r in result.impact_reasons)
    assert preview.impact == NewsImpact.LOW  # was "High 65" on the word alone
    assert valuation.impact == NewsImpact.LOW
    assert any("earnings preview" in r for r in preview.impact_reasons)
    assert any("valuation commentary" in r for r in valuation.impact_reasons)


def test_impact_reasons_are_serialised_and_restored(agent):
    article = _scored(agent, "Capri Holdings (CPRI) Names Justin Boxford CEO In 2027 Leadership Handover",
                      ticker="CPRI")
    payload = article.to_dict()

    assert payload["impact_reasons"][0] == "covered ticker CPRI (base 40)"
    assert "CEO change (+25)" in payload["impact_reasons"]
    assert payload["impact_score"] == 65
    restored = NewsArticle.from_dict(json.loads(json.dumps(payload)))
    assert restored.impact_reasons == payload["impact_reasons"]


def test_impact_does_not_depend_on_the_age_of_the_story(agent):
    fresh = _scored(agent, "Fed holds rates steady, signals one cut in 2027", age_minutes=5)
    old = _scored(agent, "Fed holds rates steady, signals one cut in 2027", age_minutes=3 * 24 * 60)

    assert old.impact_score == fresh.impact_score
    assert fresh.is_breaking is True and old.is_breaking is False


# ── 1b. action score separates stories ──────────────────────────────────────


def test_action_score_no_longer_saturates(agent):
    routine = _scored(agent, "Applied Digital (APLD) Uses Augmented Reality At Polaris Forge 1", ticker="APLD")
    material = _scored(agent, "Stantec (STN) Tops Q3 Earnings and Revenue Estimates", ticker="STN")
    stale = _scored(agent, "Stantec (STN) Tops Q3 Earnings and Revenue Estimates", ticker="STN",
                    age_minutes=30 * 60)
    filler = _scored(agent, "Is Walmart (WMT) Stock Too Expensive to Buy Now?", ticker="WMT")
    threshold = agent.actionability_threshold

    # Old formula: impact + confidence x 50 + 10 put all four at 80-100.
    assert routine.action_score < threshold
    assert filler.action_score < routine.action_score
    assert material.action_score >= threshold
    assert stale.action_score < threshold < material.action_score
    assert 0 <= filler.action_score <= 100

    scores = [
        _scored(agent, title, ticker=ticker, provider=provider).action_score
        for title, ticker, provider, _ in ARCHIVE_HEADLINES
    ]
    actionable = sum(1 for s in scores if s >= threshold)
    assert 0 < actionable < len(scores) * 0.4  # was: most ticker stories


def test_neutral_sentiment_adds_no_direction_points(agent, monkeypatch):
    monkeypatch.setattr(
        agent, "_analyze_sentiment_result",
        lambda text: FinancialSentimentResult(label="neutral", score=0.0, confidence=0.95, source="finbert"),
    )
    neutral = _scored(agent, "Stantec (STN) Tops Q3 Earnings and Revenue Estimates", ticker="STN")
    assert neutral.sentiment == NewsSentiment.NEUTRAL
    assert neutral.action_score == int(65 * nta.ACTION_IMPACT_WEIGHT + nta.ACTION_TICKER_BONUS)


# ── 2. breaking: 180 minutes and high/critical impact only ──────────────────


def test_breaking_window_default_and_config_key(monkeypatch):
    assert BREAKING_WINDOW_MINUTES == 180
    monkeypatch.delenv("MS_PROCESS_ROLE", raising=False)
    NewsTabAgent()
    assert get_breaking_window_minutes() == 180
    NewsTabAgent({"news": {"breaking_window_minutes": 45}})
    assert get_breaking_window_minutes() == 45


def test_breaking_expires_after_the_window_and_needs_high_impact(agent):
    title = "Fed holds rates steady, signals one cut in 2027"
    inside = _scored(agent, title, age_minutes=170)
    outside = _scored(agent, title, age_minutes=200)
    assert inside.to_dict()["is_breaking"] is True and inside.to_dict()["freshness"] == "BREAKING"
    assert outside.impact == NewsImpact.CRITICAL  # still a high-impact story
    assert outside.to_dict()["is_breaking"] is False
    assert outside.to_dict()["freshness"] != "BREAKING"

    # Urgent wording without impact is not breaking.
    minor = _scored(agent, "Unocoin keeps deposits and withdrawals halted over suspect transfers")
    assert minor.impact in (NewsImpact.LOW, NewsImpact.MEDIUM)
    assert minor.is_breaking is False and minor.to_dict()["is_breaking"] is False

    # The flag is evaluated at read time: the same object ages out.
    inside.published_at = datetime.now(timezone.utc) - timedelta(minutes=181)
    assert inside.to_dict()["is_breaking"] is False


def test_breaking_bucket_and_getters_honour_the_window(agent):
    fresh = _scored(agent, "Fed holds rates steady, signals one cut in 2027", age_minutes=20, article_id="fresh")
    aged = _scored(agent, "ECB cuts interest rates by 25 basis points", age_minutes=20, article_id="aged")
    agent._organize_state([fresh, aged], watchlist=[])
    assert {a.id for a in agent._state.breaking_news} == {"fresh", "aged"}

    aged.published_at = datetime.now(timezone.utc) - timedelta(hours=6)
    # A caller asking for a week still gets only what is breaking now.
    assert [a["id"] for a in agent.get_breaking_news(max_age_minutes=7 * 24 * 60)] == ["fresh"]
    assert [a["id"] for a in agent.try_get_breaking_news(max_age_minutes=7 * 24 * 60)] == ["fresh"]
    state = agent.get_news_tab_state()
    assert [a["id"] for a in state["breaking_news"]] == ["fresh"]
    by_id = {a["id"]: a for a in state["market_news"]}
    assert by_id["aged"]["is_breaking"] is False and by_id["aged"]["impact"] == "critical"


def test_stock_alert_column_is_not_urgency(agent):
    alert = _scored(agent, "KO Stock Alert: Coca-Cola Just Hit a New All-Time High", ticker="KO")
    assert alert.is_breaking is False
    assert not any("urgent" in r for r in alert.impact_reasons)


# ── 3. company names ────────────────────────────────────────────────────────


def _write_local_sources(data_dir):
    (data_dir / "calendar_events_cache.json").write_text(json.dumps({
        "earnings": {
            "TEAM": [{"symbol": "TEAM", "company_name": "Atlassian Corporation"}],
            "AMD": [{"symbol": "AMD", "company_name": "Advanced Micro Devices, Inc."}],
            "NBIS": [{"symbol": "NBIS", "company_name": "Nebius Group N.V."}],
            "ODD": [{"symbol": "ODD", "company_name": "ODDITY Tech Ltd."}],
        }
    }), encoding="utf-8")
    (data_dir / "scanner_state.json").write_text(json.dumps({
        "hot_tickers": [{"symbol": "VEON", "name": "VEON Ltd."}],
        "watchlist_stock_tickers": [{"symbol": "IONQ", "name": "IonQ, Inc."}],
    }), encoding="utf-8")
    (data_dir / "etoro_instruments_map.json").write_text(json.dumps({
        "CBRS": {"symbol": "CBRS", "name": "Cerebras Systems, Inc", "asset_class": "Stocks"},
        # Same symbol as AT&T, but a crypto asset: must not name the stock.
        "T": {"symbol": "T", "name": "Threshold Network", "asset_class": "Crypto"},
    }), encoding="utf-8")


def test_names_are_populated_from_local_files_with_bundled_fallback(agent, tmp_path, monkeypatch):
    _write_local_sources(tmp_path)
    monkeypatch.setattr(nta, "DATA_DIR", tmp_path)

    def _no_network(*args, **kwargs):
        raise AssertionError("company names must not use the network")

    monkeypatch.setattr(nta, "http_get", _no_network)
    monkeypatch.setattr(nta.yf, "Ticker", _no_network)

    agent._populate_company_names(["TEAM", "AMD", "NBIS", "CBRS", "T", "NVDA", "IONQ", "BTC-USD", "ZZZZ"])

    assert agent._company_full_names["TEAM"] == "ATLASSIAN CORPORATION"   # earnings calendar
    assert agent._company_full_names["CBRS"] == "CEREBRAS SYSTEMS, INC"   # eToro instrument map
    assert agent._company_full_names["IONQ"] == "IONQ, INC."              # scanner state
    assert agent._company_full_names["NVDA"] == "NVIDIA"                  # bundled fallback
    assert agent._company_full_names["T"] == "AT&T"                       # crypto row ignored -> bundled
    assert agent._company_full_names["ZZZZ"] == ""
    assert "BTC-USD" not in agent._company_full_names
    # The public cache (other modules match on its first word) holds only
    # names whose first word identifies the company.
    assert agent._company_names_cache["TEAM"] == "ATLASSIAN CORPORATION"
    assert agent._company_names_cache["AMD"] == ""  # "ADVANCED ..." is generic


def test_name_mention_is_relevance_evidence_with_word_boundaries(agent, tmp_path, monkeypatch):
    _write_local_sources(tmp_path)
    monkeypatch.setattr(nta, "DATA_DIR", tmp_path)
    watch = ["TEAM", "AAPL", "NVDA", "AMD", "NBIS"]

    def relevant(title, ticker, summary=""):
        return agent._is_article_relevant_to_ticker(_article(title, summary=summary), ticker, watch)

    assert relevant("Atlassian raises outlook after strong cloud quarter", "TEAM")
    assert relevant("Nvidia's board urged to consider return of Jensen Huang", "NVDA")
    assert relevant("Chipmaker update", "AMD", summary="Advanced Micro Devices said demand is higher than supply.")
    assert relevant("Nebius says customers will cover 75-85% of 2027 CapEx", "NBIS")
    assert relevant("Apple unveils new MacBook line", "AAPL")
    # Whole words only, and a one-word name is a proper noun.
    assert not relevant("Snapple maker reports record pineapple juice sales", "AAPL")
    assert not relevant("How do you like them apples: an apple a day for traders", "AAPL")
    assert not relevant("The stUSDT team did not disclose the wind-down terms", "TEAM")
    assert not relevant("Advanced analytics startup raises seed round", "AMD")
    assert agent._is_ticker_mentioned_in_article("NVDA", _article("Jensen Huang Resigns as Nvidia CEO"))


def test_alias_rules():
    assert company_names.derive_aliases("NBIS", "Nebius Group N.V.") == ("NEBIUS GROUP", "NEBIUS")
    assert company_names.derive_aliases("BIDU", "Baidu, Inc.-ADR") == ("BAIDU",)
    assert company_names.derive_aliases("MSTR", "Strategy Inc") == ("MICROSTRATEGY",)  # "Strategy" is a word
    assert company_names.derive_aliases("FOSL", "Fossil Group, Inc.") == ("FOSSIL GROUP",)
    assert company_names.derive_aliases("IONQ", "IonQ, Inc.") == ("IONQ",)
    assert "PALO ALTO" not in company_names.derive_aliases("PANW", "Palo Alto Networks")
    matcher = company_names.compile_alias_matcher(("UBER", "UBER TECHNOLOGIES"))
    assert matcher.search("Tractor Supply partners with Uber to offer delivery")
    assert not matcher.search("an uber-cheap valuation")


def test_ambiguous_ticker_is_confirmed_by_the_company_name(agent, tmp_path, monkeypatch):
    _write_local_sources(tmp_path)
    monkeypatch.setattr(nta, "DATA_DIR", tmp_path)
    named = _article("Oddity Tech lifts full-year sales view", ticker="ODD", provider="finnhub_company_news",
                     source_scope="ticker")
    unnamed = _article("An odd week for beauty stocks", ticker="ODD", provider="finnhub_company_news",
                       source_scope="ticker")

    assert agent._is_scoped_ticker_fallback_match(named, "ODD", ["ODD"]) is True
    assert agent._is_scoped_ticker_fallback_match(unnamed, "ODD", ["ODD"]) is False


def test_market_feed_story_naming_a_watchlist_company_is_attributed_and_rescored(agent):
    story = _scored(
        agent,
        "Atlassian Names New CEO After Founder Steps Aside",
        provider="cnbc_top",
        is_market_wide=True,
        source_scope="market",
    )
    assert story.ticker is None and story.impact != NewsImpact.HIGH  # scored before attribution

    agent._organize_state([story], watchlist=["TEAM", "AAPL"])

    assert story.ticker == "TEAM"
    assert [a.id for a in agent._state.ticker_news["TEAM"]] == [story.id]
    assert story.impact == NewsImpact.HIGH and story.impact_score == 65
    assert story.impact_reasons[0] == "covered ticker TEAM (base 40)"
    assert "CEO change (+25)" in story.impact_reasons


# ── 4. text cleaning ────────────────────────────────────────────────────────


def test_entities_and_markup_are_cleaned_once_at_ingestion(agent):
    article = _scored(
        agent,
        "Bitcoin ETFs shed $2.35B in 2 weeks, BlackRock&#8217;s losing streak hits 11",
        provider="cointelegraph",
        summary=(
            '<p style="float: right; margin: 0 0 10px 15px; width: 240px;"><img alt="Bitcoin" src="x.jpg"></p>'
            "Despite the turbulence, the S&amp;P 500 secured   a weekly gain of 2.5%,\n while it didn&#39;t&#8230;"
        ),
    )

    assert article.title == "Bitcoin ETFs shed $2.35B in 2 weeks, BlackRock’s losing streak hits 11"
    assert article.summary == "Despite the turbulence, the S&P 500 secured a weekly gain of 2.5%, while it didn't…"
    # Idempotent: a second pass (restart, rescore) changes nothing.
    before = (article.title, article.summary)
    agent._rescore_hydrated_article(article)
    assert (article.title, article.summary) == before
    # An escaped tag stays text; it is not turned into markup and stripped.
    assert clean_news_text("Use &lt;b&gt; for bold &amp; more") == "Use <b> for bold & more"
    assert clean_news_text("AT&T and S&P stay as they are") == "AT&T and S&P stay as they are"
    assert clean_news_text("price < 100 and > 50 is the range") == "price < 100 and > 50 is the range"


def test_etoro_titles_are_cut_at_a_word_boundary_without_the_join(agent):
    body = (
        "Sunday calm — weekend markets.\n"
        "Flash: Nasdaq closed down 0.9% Friday as the 10-year yield topped 5% and Nvidia fell again."
    )
    joined = "Sunday calm — weekend markets.: Flash: Nasdaq closed down 0.9% Friday as the 10-year yield topped 5% and Nvidia "
    post = _scored(agent, joined, provider="etoro", summary=body)
    assert post.title == "Sunday calm — weekend markets."
    assert ".: " not in post.title

    long_line = (
        "November has been modestly positive for the portfolio, with a +0.91% month-to-date return amid "
        "heightened volatility across every sector we hold"
    )
    hard_cut = long_line[:140]
    post = _scored(agent, hard_cut, provider="etoro", summary=long_line + "\nSecond line.")
    assert len(post.title) <= 110 and post.title.endswith("…")
    assert long_line.startswith(post.title[:-1])
    assert long_line[len(post.title) - 1] == " "  # cut between words, not inside one
    assert post.summary.split("\n")[1] == "Second line."  # post structure is kept

    assert truncate_at_word("short title") == "short title"
    assert social_post_title("Custom headline", "") == "Custom headline"


# ── 5. engine process: cache-only, no model, no archive writes ──────────────


def _seed_ticker_bucket(agent, ticker="AAPL"):
    article = _article(f"{ticker} supplier update", ticker=ticker, article_id=f"seed-{ticker}",
                       summary=f"News about {ticker}.")
    article.impact_score = 40
    agent._state.ticker_news[ticker] = [article]
    return article


def test_engine_role_serves_cache_only_and_never_writes_or_loads_the_model(agent, monkeypatch, tmp_path):
    backup = tmp_path / "news_backup.json"
    monkeypatch.setattr(nta, "NEWS_BACKUP_PATH", str(backup))
    monkeypatch.setenv("MS_PROCESS_ROLE", "engine")
    fetched = []
    monkeypatch.setattr(agent, "_fetch_ticker_news", lambda *a, **k: fetched.append(a) or [])

    def _model_must_not_load(*args, **kwargs):
        raise AssertionError("the sentiment model must not load in the engine process")

    monkeypatch.setattr(nta, "get_financial_sentiment_service", _model_must_not_load)
    _seed_ticker_bucket(agent, "AAPL")

    hit = agent.ensure_ticker_news_cached("AAPL", force_refresh=True, allow_remote=True)
    miss = agent.ensure_ticker_news_cached("MSFT", force_refresh=True, allow_remote=True)
    direct = agent.refresh_single_ticker_news("MSFT")

    assert hit["status"] == "hit" and hit["fetched"] is False
    assert miss == {"status": "miss_cache_only", "reason": "engine_process_cache_only", "ticker": "MSFT",
                    "count": 0, "fetched": False}
    assert direct["status"] == "cache_only" and direct["reason"] == "engine_process_cache_only"
    assert direct["added"] == 0
    assert fetched == []

    agent._save_backup_from_snapshot(agent._state.to_dict())
    agent._save_backup()
    assert not backup.exists()

    # If the engine ever has to score a headline it uses the lexicon.
    engine_agent = NewsTabAgent()
    scored = _article("Apple beats quarterly earnings estimates", ticker="AAPL")
    engine_agent._analyze_article(scored)
    assert scored.sentiment_source == "sentiment_lexicon"
    assert scored.sentiment_fallback_used is True
    assert "engine process" in scored.sentiment_error


def test_api_role_still_fetches_and_saves(agent, monkeypatch, tmp_path):
    backup = tmp_path / "news_backup.json"
    monkeypatch.setattr(nta, "NEWS_BACKUP_PATH", str(backup))
    monkeypatch.setenv("MS_PROCESS_ROLE", "api")
    calls = []
    fresh = _article("Microsoft expands AI data center spending", ticker="MSFT", article_id="msft-1",
                     summary="MSFT announced another program.")
    monkeypatch.setattr(agent, "_fetch_ticker_news", lambda *a, **k: calls.append(a[0]) or [fresh])
    monkeypatch.setattr(agent, "_queue_auto_micro_summaries", lambda: None)
    monkeypatch.setattr(agent, "_queue_market_narrative_refresh", lambda: None)

    result = agent.ensure_ticker_news_cached("MSFT", allow_remote=True)

    assert calls == ["MSFT"]
    assert result["status"] == "refreshed" and result["new_items"] == 1 and result["fetched"] is True
    assert backup.exists()
    saved = json.loads(backup.read_text(encoding="utf-8"))
    assert [a["id"] for a in saved["ticker_news"]["MSFT"]] == ["msft-1"]


def test_engine_copy_follows_the_archive_written_by_the_api(agent, monkeypatch, tmp_path):
    backup = tmp_path / "news_backup.json"
    monkeypatch.setattr(nta, "NEWS_BACKUP_PATH", str(backup))
    seeded = _article("Apple supplier update", ticker="AAPL", article_id="from-api", summary="News about AAPL.")
    agent._analyze_article(seeded)
    backup.write_text(json.dumps({
        "ticker_news": {"AAPL": [seeded.to_dict()]},
        "market_news": [],
        "breaking_news": [],
        "last_update": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")

    # API role: never reloads behind the owner's back.
    monkeypatch.setenv("MS_PROCESS_ROLE", "api")
    assert agent._engine_sync_from_backup() is False

    monkeypatch.setenv("MS_PROCESS_ROLE", "engine")
    agent._engine_backup_mtime = 0.0
    agent._engine_sync_checked_ts = 0.0
    assert "AAPL" not in agent._state.ticker_news
    assert agent._engine_sync_from_backup() is True
    deadline = time.time() + 10
    while time.time() < deadline and agent._engine_sync_inflight:
        time.sleep(0.05)
    assert [a.id for a in agent._state.ticker_news.get("AAPL", [])] == ["from-api"]
    # Throttled: an unchanged file is not reloaded.
    assert agent._engine_sync_from_backup() is False
    assert not any(t.name == "NewsEngineBackupSync" and t.is_alive() for t in threading.enumerate())


def test_finbert_loader_refuses_the_engine_process(monkeypatch):
    from core.news_v9.finbert import FinBERTAnalyzer

    imported = []

    class _Transformers:
        def __getattr__(self, name):
            imported.append(name)
            raise AssertionError("transformers must not be touched in the engine process")

    monkeypatch.setitem(__import__("sys").modules, "transformers", _Transformers())
    monkeypatch.setenv("MS_PROCESS_ROLE", "engine")
    result = FinBERTAnalyzer(model="ProsusAI/finbert", revision="abc").analyze("Apple beats estimates")

    assert result.available is False
    assert result.error == "FinBERT is not loaded in the engine process"
    assert imported == []


# ── 6. attach a validated briefing to the in-memory article ─────────────────


def _briefing(**overrides):
    payload = {
        "briefing_version": nta.NEWS_BRIEFING_VERSION,
        "ai_status": "ok",
        "tldr": "Stantec beat Q3 estimates",
        "article_explainer": "Stantec reported third-quarter earnings above estimates.",
        "market_implication": "Limited read-through for the wider market.",
        "market_direction": "neutral",
        "ticker_implication": {"ticker": "STN", "direction": "up", "confidence": 60},
        "time_horizon": "intraday",
    }
    payload.update(overrides)
    return payload


def test_attach_briefing_updates_every_copy_and_persists(agent, monkeypatch):
    saved = []

    class _Store:
        def get_article_analysis(self, article_id, analysis_type=None, **kwargs):
            return None

        def save_article_analysis(self, **kwargs):
            saved.append(kwargs)

    import core.analysis_store as store_module

    monkeypatch.setattr(store_module, "analysis_store", _Store())
    primary = _scored(agent, "Stantec (STN) Tops Q3 Earnings and Revenue Estimates", ticker="STN",
                      article_id="stn-1")
    twin = NewsArticle.from_dict(primary.to_dict())  # same id, separate object (as after a restart)
    agent._state.ticker_news["STN"] = [primary]
    agent._state.market_news = [twin]
    before = primary.action_score

    assert agent.attach_briefing("stn-1", _briefing(), analysis_type="micro", model="deepseek-v4-pro:cloud") is True

    assert primary.ai_micro_summary["tldr"] == "Stantec beat Q3 estimates"
    assert twin.ai_micro_summary["tldr"] == "Stantec beat Q3 estimates"
    assert primary.action_score > before  # briefing + intraday horizon count
    # The next state poll still carries it.
    polled = agent.get_news_tab_state(active_tickers=["STN"])["ticker_news"]["STN"][0]
    assert polled["ai_micro_summary"]["article_explainer"].startswith("Stantec reported")
    assert saved and saved[0]["article_id"] == "stn-1" and saved[0]["analysis_type"] == "micro"
    assert saved[0]["model"] == "deepseek-v4-pro:cloud"

    assert agent.attach_briefing("stn-1", {"summary": "long form"}, analysis_type="deep") is True
    assert primary.ai_deep_analysis == {"summary": "long form"} and twin.ai_deep_analysis == {"summary": "long form"}


def test_attach_briefing_refuses_placeholders_and_unknown_articles(agent, monkeypatch):
    import core.analysis_store as store_module

    class _Store:
        def get_article_analysis(self, *args, **kwargs):
            return None

        def save_article_analysis(self, **kwargs):
            raise AssertionError("nothing may be stored for a refused briefing")

    monkeypatch.setattr(store_module, "analysis_store", _Store())
    article = _scored(agent, "Stantec (STN) Tops Q3 Earnings and Revenue Estimates", ticker="STN",
                      article_id="stn-2")
    agent._state.ticker_news["STN"] = [article]

    assert agent.attach_briefing("stn-2", _briefing(ai_status="unavailable")) is False
    assert agent.attach_briefing("stn-2", {"tldr": "old schema"}) is False
    assert agent.attach_briefing("stn-2", None) is False
    assert agent.attach_briefing("missing-id", _briefing()) is False
    assert agent.attach_briefing("stn-2", {"error": "model timeout"}, analysis_type="deep") is False
    assert article.ai_micro_summary is None and article.ai_deep_analysis is None


def test_stored_briefing_is_reattached_when_the_archive_is_reloaded(agent, monkeypatch):
    import core.analysis_store as store_module

    class _Store:
        def get_article_analysis(self, article_id, analysis_type=None, **kwargs):
            if article_id == "stn-3" and analysis_type == "micro":
                return {"analysis": _briefing()}
            return None

    monkeypatch.setattr(store_module, "analysis_store", _Store())
    article = _article("Stantec (STN) Tops Q3 Earnings and Revenue Estimates", ticker="STN", article_id="stn-3")
    article.sentiment = NewsSentiment.BULLISH

    agent._rescore_hydrated_article(article)

    assert article.ai_micro_summary["ai_status"] == "ok"
