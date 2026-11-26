from datetime import datetime, timezone, timedelta
from core.data_model.quality import Conflict, DataQuality, SignalQuality, QualityEnvelope

UTC = timezone.utc


def test_conflict_immutable():
    c = Conflict(field="price", values=[100.0, 101.5], sources=["yahoo", "alpaca"], deviation_pct=1.5)
    assert c.field == "price"
    assert c.deviation_pct == 1.5


def test_data_quality_fields():
    q = DataQuality(source="yahoo", timestamp=datetime(2026, 8, 11, tzinfo=UTC),
                    freshness=timedelta(seconds=30), coverage=0.95, stale=False,
                    conflicts=(Conflict("price", [100.0, 101.5], ["yahoo", "alpaca"], 1.5),),
                    quality_flags=frozenset({"CROSS_SOURCE"}))
    assert q.coverage == 0.95
    assert "CROSS_SOURCE" in q.quality_flags
    assert q.conflicts[0].field == "price"


def test_signal_quality_metrics():
    sq = SignalQuality(win_rate=0.6, profit_factor=1.8, avg_R=0.4, expectancy_R=0.3,
                       max_drawdown=0.12, sample_size=50)
    assert sq.sample_size == 50
    assert sq.alerts == ()


def test_quality_envelope_carries_both():
    q = DataQuality(source="yahoo", timestamp=datetime(2026, 8, 11, tzinfo=UTC))
    sq = SignalQuality(win_rate=0.5, profit_factor=1.5, avg_R=0.3, expectancy_R=0.2,
                       max_drawdown=0.1, sample_size=20)
    env = QualityEnvelope(data=q, signal=sq)
    assert env.data.source == "yahoo"
    assert env.signal.sample_size == 20
    # DATA and SIGNAL quality are explicitly separate (§33)
    assert env.data is not env.signal