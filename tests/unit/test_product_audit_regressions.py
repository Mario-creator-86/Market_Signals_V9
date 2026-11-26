from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest


def test_chart_interval_alias_selects_requested_history(monkeypatch):
    from core import stock_data_client as module
    calls = []
    frame = pd.DataFrame({'Open': [10.], 'High': [12.], 'Low': [9.],
                          'Close': [11.], 'Volume': [100]},
                         index=pd.date_range('2026-08-03', periods=1, tz='America/New_York'))
    def history(**kwargs):
        calls.append(kwargs)
        return frame.copy()
    monkeypatch.setattr(module.yf, 'Ticker', lambda _: SimpleNamespace(history=history))
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    assert module.StockDataClient.get_historical_chart('TEST', interval='1day')
    assert calls == [{'period': '1y', 'interval': '1d'}]


def test_chart_30minute_request_uses_30minute_bars(monkeypatch):
    from core import stock_data_client as module
    calls = []
    frame = pd.DataFrame({'Open': [10.], 'High': [12.], 'Low': [9.],
                          'Close': [11.], 'Volume': [100]},
                         index=pd.date_range('2026-08-03', periods=1))
    def history(**kwargs):
        calls.append(kwargs)
        return frame.copy()
    monkeypatch.setattr(module.yf, 'Ticker', lambda _: SimpleNamespace(history=history))
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    module.StockDataClient.get_historical_chart('TEST', timeframe='30min')
    assert calls[0]['interval'] == '30m'


def test_four_hour_chart_aggregates_ohlcv_within_each_session(monkeypatch):
    from core import stock_data_client as module
    index = pd.date_range('2026-08-03 09:30', periods=7, freq='h', tz='America/New_York')
    frame = pd.DataFrame({'Open': range(10,17), 'High': range(12,19),
                          'Low': range(9,16), 'Close': range(11,18), 'Volume': [100]*7}, index=index)
    monkeypatch.setattr(module.yf, 'Ticker', lambda _: SimpleNamespace(history=lambda **_: frame.copy()))
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    rows = module.StockDataClient.get_historical_chart('TEST', timeframe='4hour')
    assert len(rows) == 2
    assert (rows[0]['open'], rows[0]['high'], rows[0]['low'], rows[0]['close'], rows[0]['volume']) == (10,15,9,14,400)
    assert rows[1]['volume'] == 300


@pytest.mark.parametrize('prices', [(100,0), (100,-5), (-100,105), (0,105)])
def test_earnings_gap_rejects_nonpositive_equity_prices(monkeypatch, prices):
    from agents.pre_earnings_agent import PreEarningsAgent
    agent = PreEarningsAgent.__new__(PreEarningsAgent)
    monkeypatch.setattr(agent, '_get_event_reference_prices', lambda *a, **k: prices)
    assert agent._compute_event_gap_move_pct('TEST', datetime(2026,8,3,tzinfo=timezone.utc), 'BMO') is None


def test_backtest_cache_detects_correction_to_earlier_candle():
    from core.quick_backtester import QuickBacktester
    frame = pd.DataFrame({'open': [10.,11.], 'high': [12.,13.], 'low': [9.,10.],
                          'close': [11.,12.], 'volume': [100,100]},
                         index=pd.date_range('2026-08-03',periods=2))
    corrected = frame.copy()
    corrected.loc[corrected.index[0], 'high'] = 50.
    assert QuickBacktester._df_fingerprint(frame) != QuickBacktester._df_fingerprint(corrected)
    assert QuickBacktester._df_fingerprint(frame) == QuickBacktester._df_fingerprint(frame.copy())


def test_strategy_comparison_fetches_one_snapshot_for_all_variants(monkeypatch):
    from api import actions_service
    from core.quick_backtester import QuickBacktester
    frame = pd.DataFrame({'close': [10.,11.]})
    fetches, runs = [], []
    def fetch(self, *args, **kwargs):
        fetches.append((args,kwargs))
        return frame, []
    def run(self, **kwargs):
        runs.append(kwargs)
        return {'win_rate': 0, 'signals': []}
    monkeypatch.setattr(QuickBacktester, 'fetch_backtest_data', fetch)
    monkeypatch.setattr(QuickBacktester, 'quick_backtest', run)
    results = actions_service._get_strategy_comparison('TEST', custom_tp_pct=4, custom_sl_pct=2)
    assert len(results) == 12
    assert len(fetches) == 1
    assert len(runs) == 12
    assert all(call.get('df') is frame for call in runs)


@pytest.fixture
def controlled_quick_backtest(monkeypatch):
    from core import backtester as module
    from core.quick_backtester import QuickBacktester
    config = SimpleNamespace(signal={
        'rsi_period': 14, 'rsi_oversold': 30, 'rsi_overbought': 70,
        'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 9,
        'exit_mode': 'ATR', 'atr_multiplier_tp': 2., 'atr_multiplier_sl': 1.5,
        'profit_target_pct': 4., 'stop_loss_pct': 2.,
    }, signal_variants={}, strategy_v2={'costs_by_asset': {'US_STOCKS': {
        'commission_bps': 1., 'entry_slippage_bps': 1.,
        'slippage_sl_bps': 1., 'slippage_tp_bps': 1.,
    }}}, backtest={'cache_ttl_seconds': 900})
    frame = pd.DataFrame({'open': [100.]*120, 'high': [120.]*120,
                          'low': [99.]*120, 'close': [100.]*120,
                          'volume': [1000]*120})
    monkeypatch.setattr(module.TechnicalAnalysis, 'calc_rsi', lambda series, *a: series * 0 + 20.)
    monkeypatch.setattr(module.TechnicalAnalysis, 'calc_macd',
                        lambda series, *a: (series*0+2., series*0+1., series*0+1.))
    monkeypatch.setattr(module, 'calculate_atr', lambda df, **kw: df['close']*0+2.)
    return QuickBacktester(config), config, frame


def test_quick_backtest_atr_modes_change_actual_trade_levels(controlled_quick_backtest):
    runner, config, frame = controlled_quick_backtest
    medium = runner.quick_backtest('TEST', df=frame, risk_mode='MEDIUM')['signals'][0]
    strong = runner.quick_backtest('TEST', df=frame, risk_mode='STRONG')['signals'][0]
    assert strong['tp'] - strong['entry'] == pytest.approx((medium['tp'] - medium['entry']) * 1.5)
    assert strong['entry'] - strong['sl'] == pytest.approx((medium['entry'] - medium['sl']) * 1.25)
    assert config.signal['atr_multiplier_tp'] == 2.


def test_quick_backtest_explicit_exit_mode_is_applied_and_cached_separately(controlled_quick_backtest):
    runner, _, frame = controlled_quick_backtest
    atr = runner.quick_backtest('TEST', df=frame, exit_mode='ATR', custom_tp_pct=8.)['signals'][0]
    fixed = runner.quick_backtest('TEST', df=frame, exit_mode='FIXED_PCT', custom_tp_pct=8.)['signals'][0]
    assert fixed['tp'] == pytest.approx(fixed['entry'] * 1.08)
    assert atr['tp'] - atr['entry'] == pytest.approx(4.)


def test_quick_backtest_config_change_invalidates_cached_trades(controlled_quick_backtest):
    runner, config, frame = controlled_quick_backtest
    assert runner.quick_backtest('TEST', df=frame)['signals']
    config.signal['rsi_oversold'] = 10
    assert runner.quick_backtest('TEST', df=frame)['signals'] == []


def test_cockpit_range_proxy_preserves_zero_and_explicit_provenance():
    from api.cockpit_service import _pre_earnings_row
    row = _pre_earnings_row({'symbol': 'TEST', 'range_proxy_pct': 0.,
                             'expected_move_pct': 8., 'range_proxy_source': 'quote_range'})
    assert row['range_proxy_pct'] == 0.
    assert row['range_proxy_source'] == 'quote_range'
    assert row['expected_move_pct'] == 0.  # compatible alias, same measure


@pytest.mark.parametrize('price', [0.00000073, 0.00073, 0.0021, 100.])
def test_radar_payload_preserves_low_price_trade_levels(price):
    from agents.trade_radar_agent import TradeRadarAgent
    agent = TradeRadarAgent.__new__(TradeRadarAgent)
    agent._get_ohlc_cached = lambda **kw: (None, None, ['ohlc_local_cache_miss'])
    agent._build_news_pack = lambda **kw: ({'available': False, 'top': []}, {'news_ts': None})
    agent._build_calendar_pack = lambda **kw: ({'available': False, 'earnings': {}, 'macro_next_72h': []}, {'calendar_ts': None})
    candidate = TradeRadarAgent._new_candidate('TESTUSDT')
    candidate.update({'price': price, 'change_pct': 6.})
    result = agent._enrich_candidate(
        symbol='TESTUSDT', candidate=candidate, watchlist_item={},
        cfg={**TradeRadarAgent.DEFAULT_CONFIG, 'enable_backtest': False},
        now=datetime.now(timezone.utc), force_refresh=False,
    )
    levels = result['analysis']
    assert levels['entry'] == pytest.approx(price, rel=1e-10, abs=0)
    assert levels['tp'] / levels['entry'] == pytest.approx(1.04)
    assert levels['sl'] / levels['entry'] == pytest.approx(.97)
