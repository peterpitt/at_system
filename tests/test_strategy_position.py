"""策略訊號與部位損益計算測試。"""
import numpy as np
import pandas as pd

from src.position import Position, Side
from src.strategy import TrendStrategy


def _make_strategy():
    return TrendStrategy(
        ema_fast=5, ema_slow=10, atr_period=5, atr_stop_mult=2.0, atr_tp_mult=3.0
    )


def _df_from_closes(closes):
    n = len(closes)
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 5 for c in closes],
            "low": [c - 5 for c in closes],
            "close": closes,
            "volume": [100] * n,
        }
    )


def test_golden_cross_goes_long():
    # 先跌後升，製造黃金交叉
    closes = list(range(20100, 20000, -5)) + list(range(20000, 20200, 5))
    df = _df_from_closes(closes)
    strat = _make_strategy()
    d = strat.evaluate(df, Side.FLAT)
    assert d.signal.value in ("LONG", "NONE")
    if d.signal.value == "LONG":
        assert d.stop_price < d.entry_price < d.take_profit_price
        # 風報比應 = tp/sl 倍數比
        risk = d.entry_price - d.stop_price
        reward = d.take_profit_price - d.entry_price
        assert reward > risk


def test_insufficient_bars_returns_none():
    df = _df_from_closes([20000, 20010, 20020])
    strat = _make_strategy()
    d = strat.evaluate(df, Side.FLAT)
    assert d.signal.value == "NONE"


# ---------- 部位損益 ----------

def test_long_pnl_and_stops():
    pos = Position(point_value=10)
    pos.open(side=Side.LONG, contracts=1, entry_price=20000,
             stop_price=19960, take_profit_price=20060)
    assert pos.unrealized_pnl(20010) == 100   # +10 點 × 10 × 1
    assert pos.hit_stop(19960) is True
    assert pos.hit_take_profit(20060) is True
    assert pos.realized_pnl(20030) == 300


def test_short_pnl_and_stops():
    pos = Position(point_value=10)
    pos.open(side=Side.SHORT, contracts=2, entry_price=20000,
             stop_price=20040, take_profit_price=19940)
    assert pos.unrealized_pnl(19990) == 200   # +10 點 × 10 × 2
    assert pos.hit_stop(20040) is True
    assert pos.hit_take_profit(19940) is True


def test_flat_position_has_no_pnl():
    pos = Position(point_value=10)
    assert pos.unrealized_pnl(20000) == 0.0
    assert not pos.is_open
