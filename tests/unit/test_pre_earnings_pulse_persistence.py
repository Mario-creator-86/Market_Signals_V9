import json
from datetime import timedelta

import pytest

from agents import pre_earnings_agent as pe
from agents.pre_earnings_agent import PreEarningsAgent


def _item(symbol, event_utc, label="LONG", score=40):
    return {
        "symbol": symbol,
        "earnings_time_utc": pe._iso(event_utc),
        "session": "AMC",
        "gap_risk_numeric": 80,
        "direction_label": label,
        "direction_bias_score": score,
        "top_news": [],
    }


def _pulse(generated_at, *, longs, shorts, sentiment="bullish", summary="Slate"):
    return {
        "summary": summary,
        "sentiment": sentiment,
        "top_long": [
            {"symbol": symbol, "reason": reason}
            for symbol, reason in longs
        ],
        "top_short": [
            {"symbol": symbol, "reason": reason}
            for symbol, reason in shorts
        ],
        "catalysts": "Earnings and guidance.",
        "risks": "Post-report volatility.",
        "model": "glm-5.2",
        "generated_at": pe._iso(generated_at),
    }


@pytest.fixture
def isolated_pulse_agent(tmp_path, monkeypatch):
    output_path = tmp_path / "pre_earnings.json"
    history_path = tmp_path / "pre_earnings_pulse_history.json"
    monkeypatch.setattr(pe, "PRE_EARNINGS_OUTPUT_PATH", output_path)
    monkeypatch.setattr(
        pe,
        "PRE_EARNINGS_FEEDBACK_PATH",
        tmp_path / "pre_earnings_feedback.json",
    )
    monkeypatch.setattr(
        pe,
        "PRE_EARNINGS_AI_CACHE_PATH",
        tmp_path / "pre_earnings_ai_cache.json",
    )
    monkeypatch.setattr(pe, "PRE_EARNINGS_PULSE_HISTORY_PATH", history_path)
    monkeypatch.setattr(
        PreEarningsAgent,
        "_filter_schedule_conflict_items",
        lambda self, items, cfg: list(items),
    )
    return PreEarningsAgent(), output_path, history_path


@pytest.mark.unit
def test_pulse_active_union_survives_restart_and_clsk_is_updated_once(
    isolated_pulse_agent,
):
    agent, output_path, history_path = isolated_pulse_agent
    event_utc = pe._utc_now() + timedelta(days=5)
    # Labels must be coherent with the sides the model picks (the audit P0-1
    # guard drops contradictory picks): shorts are picked on CLSK, INSP, ACMR,
    # BTDR, so those rows carry SHORT labels here.
    items = [
        _item(symbol, event_utc, label=label, score=score)
        for symbol, label, score in [
            ("VCYT", "LONG", 60),
            ("CIFR", "LONG", 55),
            ("FORM", "LONG", 50),
            ("CNO", "LONG", 45),
            ("UFCS", "LONG", 40),
            ("CLSK", "SHORT", -40),
            ("INSP", "SHORT", -35),
            ("ACMR", "SHORT", -30),
            ("BTDR", "SHORT", -45),
        ]
    ]
    agent._state["items"] = items
    older_at = pe._utc_now() - timedelta(minutes=10)
    newer_at = pe._utc_now()

    first = _pulse(
        older_at,
        longs=[
            ("VCYT", "Highest gap risk."),
            ("CIFR", "Strong long bias."),
            ("FORM", "Semiconductor expansion."),
        ],
        shorts=[
            ("CLSK", "Earlier downside rationale."),
            ("INSP", "Weak setup."),
        ],
    )
    second = _pulse(
        newer_at,
        longs=[
            ("CNO", "Positive call highlights."),
            ("UFCS", "Favorable momentum."),
        ],
        shorts=[
            ("CLSK", "Latest relative weakness."),
            ("ACMR", "Market-relative weakness."),
            ("BTDR", "Bearish expectations."),
        ],
        sentiment="bearish",
        summary="Later slate",
    )

    agent.import_pulse_snapshot(first, items, source="screenshot_recovery")
    merged = agent.import_pulse_snapshot(second, items, source="llm")

    assert [pick["symbol"] for pick in merged["top_long"]] == [
        "VCYT",
        "CIFR",
        "FORM",
        "CNO",
        "UFCS",
    ]
    assert [pick["symbol"] for pick in merged["top_short"]] == [
        "CLSK",
        "INSP",
        "ACMR",
        "BTDR",
    ]
    assert [pick["symbol"] for pick in merged["current_top_long"]] == [
        "CNO",
        "UFCS",
    ]
    assert [pick["symbol"] for pick in merged["current_top_short"]] == [
        "CLSK",
        "ACMR",
        "BTDR",
    ]
    clsk = next(pick for pick in merged["top_short"] if pick["symbol"] == "CLSK")
    assert clsk["reason"] == "Latest relative weakness."
    assert clsk["occurrences"] == 2
    assert clsk["sources"] == ["screenshot_recovery", "llm"]
    assert len(merged["history"]) == 2
    assert merged["history"][0]["summary"] == "Later slate"
    assert merged["history"][1]["summary"] == "Slate"
    assert output_path.exists()
    assert history_path.exists()

    restarted = PreEarningsAgent()
    restarted._compute_pulse = lambda *_args, **_kwargs: pytest.fail(
        "fresh persisted Pulse must not request the LLM after restart"
    )
    restored = restarted.get_pulse(
        restarted._get_config(),
        items,
        ttl_seconds=300,
    )
    assert [pick["symbol"] for pick in restored["top_long"]] == [
        "VCYT",
        "CIFR",
        "FORM",
        "CNO",
        "UFCS",
    ]
    assert [pick["symbol"] for pick in restored["current_top_long"]] == [
        "CNO",
        "UFCS",
    ]
    assert [pick["symbol"] for pick in restored["current_top_short"]] == [
        "CLSK",
        "ACMR",
        "BTDR",
    ]
    assert restored["history_meta"]["snapshot_count"] == 2
    assert restored["persisted"] is True


@pytest.mark.unit
def test_identical_material_snapshot_is_compacted_without_losing_occurrences(
    isolated_pulse_agent,
):
    agent, _, history_path = isolated_pulse_agent
    event_utc = pe._utc_now() + timedelta(days=4)
    items = [
        _item("VCYT", event_utc),
        _item("CLSK", event_utc, label="SHORT", score=-40),
    ]
    first_at = pe._utc_now() - timedelta(minutes=6)
    second_at = pe._utc_now()
    first = _pulse(
        first_at,
        longs=[("VCYT", "Same reason.")],
        shorts=[("CLSK", "Same downside.")],
    )
    second = _pulse(
        second_at,
        longs=[("VCYT", "Same reason.")],
        shorts=[("CLSK", "Same downside.")],
    )

    agent.import_pulse_snapshot(first, items, source="llm")
    merged = agent.import_pulse_snapshot(second, items, source="llm")
    sidecar = json.loads(history_path.read_text(encoding="utf-8"))

    assert len(sidecar["snapshots"]) == 1
    assert sidecar["snapshots"][0]["occurrences"] == 2
    assert sidecar["snapshots"][0]["first_generated_at"] == pe._iso(first_at)
    assert sidecar["snapshots"][0]["last_generated_at"] == pe._iso(second_at)
    assert merged["history_meta"]["snapshot_count"] == 1
    assert merged["history_meta"]["generation_count"] == 2
    assert merged["history"][0]["occurrences"] == 2


@pytest.mark.unit
def test_older_import_adds_missing_picks_without_overwriting_newer_reason(
    isolated_pulse_agent,
):
    agent, _, _ = isolated_pulse_agent
    event_utc = pe._utc_now() + timedelta(days=6)
    items = [
        _item("CLSK", event_utc, label="SHORT", score=-40),
        _item("INSP", event_utc, label="SHORT", score=-35),
    ]
    newer_at = pe._utc_now()
    older_at = newer_at - timedelta(days=1)

    agent.import_pulse_snapshot(
        _pulse(
            newer_at,
            longs=[],
            shorts=[("CLSK", "Newest rationale.")],
        ),
        items,
        source="current_state",
    )
    merged = agent.import_pulse_snapshot(
        _pulse(
            older_at,
            longs=[],
            shorts=[
                ("CLSK", "Old rationale."),
                ("INSP", "Recovered historical pick."),
            ],
        ),
        items,
        source="screenshot_recovery",
    )

    assert [pick["symbol"] for pick in merged["top_short"]] == ["CLSK", "INSP"]
    clsk = merged["top_short"][0]
    assert clsk["reason"] == "Newest rationale."
    assert clsk["source"] == "current_state"
    assert clsk["first_seen_at"] == pe._iso(older_at)
    assert clsk["last_seen_at"] == pe._iso(newer_at)


@pytest.mark.unit
def test_new_quarter_event_archives_old_pick_but_retains_both_snapshots(
    isolated_pulse_agent,
):
    agent, _, history_path = isolated_pulse_agent
    old_event = pe._utc_now() + timedelta(days=3)
    new_event = old_event + timedelta(days=91)
    old_items = [_item("VCYT", old_event)]
    new_items = [_item("VCYT", new_event)]

    agent.import_pulse_snapshot(
        _pulse(
            pe._utc_now() - timedelta(days=1),
            longs=[("VCYT", "Prior-quarter setup.")],
            shorts=[],
        ),
        old_items,
        source="llm",
    )
    merged = agent.import_pulse_snapshot(
        _pulse(
            pe._utc_now(),
            longs=[("VCYT", "New-quarter setup.")],
            shorts=[],
            summary="New quarter",
        ),
        new_items,
        source="llm",
    )
    sidecar = json.loads(history_path.read_text(encoding="utf-8"))

    assert len(merged["top_long"]) == 1
    assert merged["top_long"][0]["reason"] == "New-quarter setup."
    assert merged["top_long"][0]["event_date"] == new_event.date().isoformat()
    assert len(sidecar["snapshots"]) == 2
    assert len(sidecar["archive"]) == 1
    assert sidecar["archive"][0]["reason"] == "Prior-quarter setup."
    assert sidecar["archive"][0]["archive_reason"] == "event_replaced"


@pytest.mark.unit
def test_reported_event_leaves_active_card_but_remains_in_archive_and_history(
    isolated_pulse_agent,
    monkeypatch,
):
    agent, _, history_path = isolated_pulse_agent
    event_utc = pe._utc_now() + timedelta(days=1)
    items = [_item("VCYT", event_utc)]
    agent.import_pulse_snapshot(
        _pulse(
            pe._utc_now(),
            longs=[("VCYT", "Upcoming setup.")],
            shorts=[],
        ),
        items,
        source="llm",
    )
    after_report = event_utc + timedelta(days=1)
    monkeypatch.setattr(pe, "_utc_now", lambda: after_report)

    reconciled = agent._reconcile_and_get_persisted_pulse(
        agent._get_config(),
        [],
    )
    sidecar = json.loads(history_path.read_text(encoding="utf-8"))

    assert reconciled["top_long"] == []
    assert reconciled["history_meta"]["snapshot_count"] == 1
    assert len(sidecar["archive"]) == 1
    assert sidecar["archive"][0]["archive_reason"] == "event_reported"


@pytest.mark.unit
def test_news_reported_picks_are_archived_but_history_and_new_quarter_survive(
    isolated_pulse_agent,
):
    agent, _, history_path = isolated_pulse_agent
    event_utc = pe._utc_now() + timedelta(days=5)
    items = [
        _item("VCYT", event_utc),
        _item("FORM", event_utc),
        _item("CNO", event_utc),
    ]
    agent.import_pulse_snapshot(
        _pulse(
            pe._utc_now(),
            longs=[
                ("VCYT", "Original VCYT recommendation."),
                ("FORM", "Original FORM recommendation."),
                ("CNO", "Still upcoming."),
            ],
            shorts=[],
        ),
        items,
        source="llm",
    )
    before = json.loads(history_path.read_text(encoding="utf-8"))
    reported_items = [
        {
            **_item("VCYT", event_utc),
            "flags": {
                "already_reported": True,
                "already_reported_evidence": ["VCYT reported results"],
            },
        },
        {
            **_item("FORM", event_utc),
            "flags": {
                "already_reported": True,
                "already_reported_evidence": ["FORM earnings call highlights"],
            },
        },
        _item("CNO", event_utc),
    ]

    descriptors = agent._pulse_descriptors_by_symbol(reported_items)
    reconciled = agent._reconcile_and_get_persisted_pulse(
        agent._get_config(),
        reported_items,
    )
    after = json.loads(history_path.read_text(encoding="utf-8"))

    assert set(descriptors) == {"CNO"}
    assert [pick["symbol"] for pick in reconciled["top_long"]] == ["CNO"]
    assert all(
        pick["symbol"] not in {"VCYT", "FORM"}
        for pick in reconciled["top_long"] + reconciled["top_short"]
    )
    assert after["snapshots"] == before["snapshots"]
    assert reconciled["history"][0]["top_long"][0]["symbol"] == "VCYT"
    assert {pick["symbol"] for pick in after["archive"]} == {"VCYT", "FORM"}
    assert {
        pick["archive_reason"] for pick in after["archive"]
    } == {"already_reported_news"}

    # Even a recovered/hallucinated snapshot cannot re-activate a news-confirmed
    # reported event; it stays available only in immutable history.
    still_filtered = agent.import_pulse_snapshot(
        _pulse(
            pe._utc_now() + timedelta(minutes=1),
            longs=[
                ("VCYT", "Must not become active."),
                ("FORM", "Must not become active."),
                ("CNO", "Still upcoming."),
            ],
            shorts=[],
            summary="Recovered snapshot",
        ),
        reported_items,
        source="screenshot_recovery",
    )
    assert [pick["symbol"] for pick in still_filtered["top_long"]] == ["CNO"]
    recovered_history = still_filtered["history"][0]
    assert [
        (pick["symbol"], pick["eligible"])
        for pick in recovered_history["top_long"]
    ] == [("VCYT", False), ("FORM", False), ("CNO", True)]

    # A later quarterly event without the flag is a new event key and can
    # qualify normally; the old archive is not a permanent ticker blacklist.
    next_quarter = event_utc + timedelta(days=91)
    new_items = [_item("VCYT", next_quarter), _item("CNO", event_utc)]
    next_cycle = agent.import_pulse_snapshot(
        _pulse(
            pe._utc_now() + timedelta(minutes=2),
            longs=[("VCYT", "New-quarter setup.")],
            shorts=[],
            summary="New quarter",
        ),
        new_items,
        source="llm",
    )
    assert [pick["symbol"] for pick in next_cycle["top_long"]] == ["CNO", "VCYT"]
    vcyt = next(pick for pick in next_cycle["top_long"] if pick["symbol"] == "VCYT")
    assert vcyt["event_date"] == next_quarter.date().isoformat()


@pytest.mark.unit
def test_restart_removes_news_reported_recommendations_from_memory_and_disk(
    isolated_pulse_agent,
    monkeypatch,
):
    agent, output_path, history_path = isolated_pulse_agent
    event_utc = pe._utc_now() + timedelta(days=5)
    original_items = [_item("VCYT", event_utc), _item("FORM", event_utc)]
    agent._state["items"] = original_items
    agent.import_pulse_snapshot(
        _pulse(
            pe._utc_now(),
            longs=[
                ("VCYT", "Old active recommendation."),
                ("FORM", "Old active recommendation."),
            ],
            shorts=[],
        ),
        original_items,
        source="llm",
    )
    reported_items = [
        {
            **item,
            "flags": {
                "already_reported": True,
                "already_reported_evidence": [f"{item['symbol']} reported"],
            },
        }
        for item in original_items
    ]
    agent._state["items"] = reported_items
    with agent._lock:
        agent._persist_locked()
    history_before = json.loads(
        history_path.read_text(encoding="utf-8")
    )["snapshots"]

    def forbidden_llm(*args, **kwargs):
        raise AssertionError("restart reconciliation must not call the LLM")

    monkeypatch.setattr(pe.ollama, "generate", forbidden_llm)
    restarted = PreEarningsAgent()

    assert restarted._state["pulse"]["top_long"] == []
    assert restarted._state["pulse"]["top_short"] == []
    assert restarted._state["pulse"]["history"][0]["top_long"][0]["symbol"] == "VCYT"
    persisted_main = json.loads(output_path.read_text(encoding="utf-8"))
    assert persisted_main["pulse"]["top_long"] == []
    assert persisted_main["pulse"]["top_short"] == []
    assert persisted_main["pulse"]["history"][0]["top_long"][1]["symbol"] == "FORM"
    persisted_sidecar = json.loads(history_path.read_text(encoding="utf-8"))
    assert persisted_sidecar["snapshots"] == history_before
    assert {pick["symbol"] for pick in persisted_sidecar["archive"]} == {
        "VCYT",
        "FORM",
    }
    assert {
        pick["archive_reason"] for pick in persisted_sidecar["archive"]
    } == {"already_reported_news"}


@pytest.mark.unit
def test_restart_repairs_stale_main_history_from_sidecar_then_is_byte_idempotent(
    isolated_pulse_agent,
    monkeypatch,
):
    agent, output_path, history_path = isolated_pulse_agent
    event_utc = pe._utc_now() + timedelta(days=7)
    symbols = ["VCYT", "FORM", "CNO", "UFCS"]
    items = [_item(symbol, event_utc) for symbol in symbols]
    agent._state["items"] = items
    base_time = pe._utc_now() - timedelta(minutes=4)
    for offset, symbol in enumerate(symbols):
        agent.import_pulse_snapshot(
            _pulse(
                base_time + timedelta(minutes=offset),
                longs=[(symbol, f"{symbol} material setup.")],
                shorts=[],
                summary=f"Snapshot {offset + 1}",
            ),
            items,
            source="llm",
        )

    canonical_sidecar_bytes = history_path.read_bytes()
    canonical_sidecar = json.loads(canonical_sidecar_bytes.decode("utf-8"))
    assert len(canonical_sidecar["snapshots"]) == 4
    assert [
        pick["symbol"]
        for pick in canonical_sidecar["active"]["top_long"]
    ] == symbols

    # Reproduce the live divergence: active union is already correct in both
    # files, but the cache-only main snapshot exposes only half the history.
    stale_main = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(stale_main["pulse"]["history"]) == 4
    stale_main["pulse"]["history"] = stale_main["pulse"]["history"][:2]
    stale_main["pulse"]["history_meta"]["snapshot_count"] = 2
    stale_main["pulse"]["history_meta"]["generation_count"] = 2
    output_path.write_text(
        json.dumps(stale_main, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    def forbidden_llm(*args, **kwargs):
        raise AssertionError("restart canonicalization must not call the LLM")

    monkeypatch.setattr(pe.ollama, "generate", forbidden_llm)
    restarted = PreEarningsAgent()

    assert len(restarted._state["pulse"]["history"]) == 4
    assert restarted._state["pulse"]["history_meta"]["snapshot_count"] == 4
    assert [pick["symbol"] for pick in restarted._state["pulse"]["top_long"]] == symbols
    repaired_disk = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(repaired_disk["pulse"]["history"]) == 4
    assert repaired_disk["pulse"] == restarted._state["pulse"]
    assert history_path.read_bytes() == canonical_sidecar_bytes

    # Once main and sidecar agree, another restart performs no write at all.
    repaired_main_bytes = output_path.read_bytes()
    repaired_main_mtime = output_path.stat().st_mtime_ns
    second_restart = PreEarningsAgent()

    assert second_restart._state["pulse"] == restarted._state["pulse"]
    assert output_path.read_bytes() == repaired_main_bytes
    assert output_path.stat().st_mtime_ns == repaired_main_mtime
    assert history_path.read_bytes() == canonical_sidecar_bytes


@pytest.mark.unit
def test_pulse_history_path_defaults_to_ms_data_directory():
    if "PRE_EARNINGS_PULSE_HISTORY_PATH" not in pe.os.environ:
        assert pe.PRE_EARNINGS_PULSE_HISTORY_PATH.parent == pe.DATA_DIR
    assert pe.PRE_EARNINGS_PULSE_HISTORY_PATH.name == (
        "pre_earnings_pulse_history.json"
    )
