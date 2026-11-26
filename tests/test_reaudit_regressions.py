import json
import math
import pytest

from core.alerts import AlertManager, AlertType
from core.config import ConfigManager
from core.backtest_summary import summarize_trades


@pytest.fixture
def config_factory(tmp_path, monkeypatch):
    import core.config as module
    path = tmp_path / 'watchlist.json'
    path.write_text(json.dumps({'stocks': ['AAPL', 'NVDA'], 'alerts': []}))
    monkeypatch.setattr(module, 'WATCHLIST_PATH', path)
    def factory():
        cfg = ConfigManager(path=tmp_path / 'config.json')
        cfg._store = None
        return cfg
    return factory, path


def test_alert_created_during_scan_survives(config_factory):
    make, path = config_factory
    api = AlertManager(make())
    api.add_alert('AAPL', AlertType.PRICE_UP, 100)
    engine = AlertManager(make())
    added = api.add_alert('NVDA', AlertType.PRICE_UP, 200)
    assert len(engine.check_alerts({'AAPL': {'price': 101}})) == 1
    assert added.id in {a['id'] for a in json.loads(path.read_text())['alerts']}


def test_alert_deleted_during_scan_not_resurrected(config_factory):
    make, path = config_factory
    api = AlertManager(make())
    alert = api.add_alert('AAPL', AlertType.PRICE_UP, 100)
    engine = AlertManager(make())
    api.remove_alert(alert.id)
    assert engine.check_alerts({'AAPL': {'price': 101}}) == []
    assert json.loads(path.read_text())['alerts'] == []


def test_two_engines_emit_once(config_factory):
    make, _ = config_factory
    AlertManager(make()).add_alert('AAPL', AlertType.PRICE_UP, 100)
    one, two = AlertManager(make()), AlertManager(make())
    assert len(one.check_alerts({'AAPL': {'price': 101}})) == 1
    assert two.check_alerts({'AAPL': {'price': 101}}) == []


def test_watchlist_save_preserves_concurrent_alert(config_factory):
    make, path = config_factory
    stale = make()
    alert = AlertManager(make()).add_alert('NVDA', AlertType.PRICE_UP, 200)
    stale._save_watchlist()
    assert json.loads(path.read_text())['alerts'][0]['id'] == alert.id


def test_migration_survives_second_load(config_factory):
    make, path = config_factory
    row = {'id': 'legacy', 'ticker': 'AAPL', 'type': 'PRICE_UP', 'target': 100, 'active': True}
    path.write_text(json.dumps({'stocks': ['AAPL'], 'alerts': [row], 'extension': 'keep'}))
    make()
    assert make().watchlist['alerts'] == [row]
    assert json.loads(path.read_text())['extension'] == 'keep'


@pytest.mark.parametrize('quote', [ {'price': None}, {'price': float('nan')}, {'price': 101, 'stale': True}, {'price': 101, 'freshness': 'stale'} ])
def test_invalid_quotes_do_not_trigger(config_factory, quote):
    make, _ = config_factory
    manager = AlertManager(make())
    manager.add_alert('AAPL', AlertType.PRICE_UP, 100)
    assert manager.check_alerts({'AAPL': quote}) == []


def test_missing_volatility_does_not_crash(config_factory):
    make, _ = config_factory
    manager = AlertManager(make())
    manager.add_alert('AAPL', AlertType.VOLATILITY, 3)
    assert manager.check_alerts({'AAPL': {'price': 100, 'change_pct': None}}) == []


def test_pooled_expectancy_and_ratios():
    rows = [{'entry': 100, 'sl': 99, 'qty': 1, 'net_pnl': v} for v in [10] + [-1] * 99]
    result = summarize_trades(rows)
    assert result['expectancy_r'] == -.89
    assert result['profit_factor'] == pytest.approx(10 / 99, abs=.0001)
    assert result['payoff_ratio'] == 10
    assert result['total_trades'] == 100
    assert result['max_drawdown_pct'] is None


def test_incomplete_r_is_unknown_not_zero():
    result = summarize_trades([{'r_net': 2}, {'entry': 100}])
    assert result['expectancy_r'] is None
    assert result['metrics_complete'] is False


@pytest.mark.parametrize('direction,entry,tp,sl', [('LONG', 100, 100, 90), ('LONG',100,90,110), ('SHORT',100,110,90), ('LONG',100,float('nan'),90)])
def test_invalid_trade_geometry_blocked(direction, entry, tp, sl):
    from agents.trade_radar_agent import TradeRadarAgent
    assert TradeRadarAgent._build_readiness(direction, 80, entry, tp, sl, [])['can_open'] is False


def test_missing_atr_is_unknown():
    from api.state_service import _compute_atr_percentile_and_regime
    assert _compute_atr_percentile_and_regime([], 0, 100) == (None, 'unknown')


def test_regular_quote_is_not_premarket():
    from agents.premarket_scanner_agent import PremarketScannerAgent
    assert PremarketScannerAgent._normalize_mover(None, {'ticker': 'AAPL', 'price':100, 'previous_close':95, 'source':'regular_session'}) is None


def test_shared_config_concurrent_creates(config_factory):
    from concurrent.futures import ThreadPoolExecutor
    make, path = config_factory
    config = make()
    managers = [AlertManager(config) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda manager: manager.add_alert('AAPL', AlertType.PRICE_UP, 100), managers))
    assert len(json.loads(path.read_text())['alerts']) == 8


def test_actions_watchlist_writer_preserves_alerts(config_factory, monkeypatch):
    import api.actions_service as actions
    make, path = config_factory
    monkeypatch.setattr(actions, 'WATCHLIST_PATH', path)
    monkeypatch.setattr(actions, 'get_shared_config', make)
    alert = AlertManager(make()).add_alert('AAPL', AlertType.PRICE_UP, 100)
    actions._save_watchlist_payload({'stocks': ['NVDA'], 'alerts': []})
    assert json.loads(path.read_text())['alerts'][0]['id'] == alert.id


def test_initial_watchlist_creation_does_not_reenter_lock(config_factory):
    make, path = config_factory
    path.unlink()
    # Use a fresh store-free object so no previous snapshot can mask startup.
    cfg = object.__new__(ConfigManager)
    cfg._store = None
    cfg._load_watchlist()
    assert path.exists()
    assert json.loads(path.read_text())['alerts'] == []


def test_legacy_backtest_aggregates_recomputed_on_read():
    from core.backtest_summary import normalize_history_record
    record = {'mode': 'standard', 'expectancy_r': 4.5, 'signals': [
        {'ticker': 'AAPL', 'trades': [{'r_net': 10}]},
        {'ticker': 'NVDA', 'trades': [{'r_net': -1}] * 99}]}
    result = normalize_history_record(record)
    assert result['expectancy_r'] == -.89
    assert len(result['trade_list']) == 100
    assert record['expectancy_r'] == 4.5  # original evidence is not rewritten


def test_macro_timezone_winter_and_summer():
    from datetime import datetime
    from api.main import _merge_macro_calendar_events
    events = []
    _merge_macro_calendar_events(datetime(2026, 1, 1), datetime(2026, 12, 31), events)
    dates = {e['id']: e['date'] for e in events}
    assert dates['macro_cpi_20260113'].endswith('13:30:00+00:00')
    assert dates['macro_cpi_20260911'].endswith('12:30:00+00:00')
    assert all(e['source_url'].startswith('https://') for e in events)


def test_unverified_provider_bucket_not_company_evidence(monkeypatch):
    import agents.pre_earnings_agent as module
    from types import SimpleNamespace
    monkeypatch.setattr(module, 'get_news_tab_agent_if_ready', lambda: SimpleNamespace(_company_names_cache={'JBL': 'Jabil'}))
    agent = object.__new__(module.PreEarningsAgent)
    rows = [
        {'symbol': 'JBL', 'title': 'Jubilee shares rise after a waste project sale', 'summary': 'Jubilee Metals Group PLC (AIM:JLP, JSE:JBL, OTC:JUBPF) develops copper in Zambia'},
        {'symbol': 'JBL', 'title': 'Jabil reports quarterly earnings'},
    ]
    result = agent._build_news_index(rows, ['JBL'])
    assert [r['title'] for r in result['JBL']] == ['Jabil reports quarterly earnings']
