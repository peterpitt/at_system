"""風控單元測試 —— 守護本金的最後一道驗證。

這些測試確保：資金永遠不會因為部位過大、虧損失控或防線失效而崩盤。
若任何一條失敗，系統就不該碰真實資金。
"""
from datetime import date, timedelta

import pytest

from src.risk_manager import RiskManager


def make_rm(**overrides):
    params = dict(
        starting_capital=72000,
        point_value=10,
        margin_per_contract=26300,
        risk_per_trade_pct=0.01,
        max_daily_loss_pct=0.03,
        max_total_drawdown_pct=0.15,
        max_contracts=1,
        max_consecutive_losses=3,
    )
    params.update(overrides)
    return RiskManager(**params)


# ---------- 部位大小 ----------

def test_size_respects_hard_cap():
    """即使風險與保證金都允許更多，也不得超過硬上限。"""
    rm = make_rm(max_contracts=1, risk_per_trade_pct=0.5, margin_per_contract=1000)
    res = rm.calc_position_size(entry_price=20000, stop_price=19990, available_funds=72000)
    assert res.contracts == 1


def test_size_zero_when_stop_too_wide():
    """停損太寬導致單口風險超過預算 → 0 口，不進場。"""
    rm = make_rm()
    # 停損 100 點 → 單口風險 1000 元 > 預算 720 元
    res = rm.calc_position_size(entry_price=20000, stop_price=19900, available_funds=72000)
    assert res.contracts == 0


def test_size_zero_when_no_stop_distance():
    rm = make_rm()
    res = rm.calc_position_size(entry_price=20000, stop_price=20000, available_funds=72000)
    assert res.contracts == 0
    assert "停損" in res.reason


def test_size_limited_by_margin():
    """資金不足以負擔保證金（含緩衝）時，口數受限。"""
    rm = make_rm(max_contracts=10, risk_per_trade_pct=0.5, margin_per_contract=26300)
    # 72000 / 1.15 / 26300 ≈ 2.38 → 2 口
    res = rm.calc_position_size(entry_price=20000, stop_price=19999, available_funds=72000)
    assert res.contracts == 2


def test_size_limited_by_risk_budget():
    """風險預算才是綁定條件時，以風險為準。"""
    rm = make_rm(max_contracts=10, margin_per_contract=100, risk_per_trade_pct=0.01)
    # 預算 720 元，停損 10 點 → 單口風險 100 元 → 7 口
    res = rm.calc_position_size(entry_price=20000, stop_price=19990, available_funds=72000)
    assert res.contracts == 7


# ---------- 進場閘門 ----------

def test_cannot_open_when_margin_insufficient():
    rm = make_rm()
    ok, why = rm.can_open(available_funds=20000, contracts=1)
    assert not ok
    assert "保證金不足" in why


def test_can_open_normal():
    rm = make_rm()
    ok, why = rm.can_open(available_funds=72000, contracts=1)
    assert ok


def test_cannot_open_when_halted():
    rm = make_rm()
    rm.halt("測試停止")
    ok, _ = rm.can_open(available_funds=72000, contracts=1)
    assert not ok


# ---------- 每日虧損上限 ----------

def test_daily_loss_limit_halts():
    rm = make_rm()  # 上限 3% = 2160 元
    rm.update_equity(unrealized_pnl=-2200)
    assert rm.state.halted
    assert not rm.state.locked  # 尚未到總回撤


def test_daily_loss_not_triggered_below_limit():
    rm = make_rm()
    rm.update_equity(unrealized_pnl=-2000)
    assert not rm.state.halted


# ---------- 總回撤永久鎖定 ----------

def test_total_drawdown_locks():
    rm = make_rm()  # 15% = 10800 元
    rm.update_equity(unrealized_pnl=-11000)
    assert rm.state.locked
    assert rm.state.halted


def test_lock_survives_day_roll():
    """永久鎖定不因換日解除。"""
    rm = make_rm()
    rm.lock("總回撤")
    rm.roll_day_if_needed(date.today() + timedelta(days=1))
    assert rm.state.locked


def test_halt_clears_on_day_roll():
    """當日停止（非鎖定）換日後解除。"""
    rm = make_rm()
    rm.halt("每日虧損")
    assert rm.state.halted
    rm.roll_day_if_needed(date.today() + timedelta(days=1))
    assert not rm.state.halted


# ---------- 連續虧損熔斷 ----------

def test_consecutive_losses_halt():
    rm = make_rm(max_consecutive_losses=3)
    for _ in range(3):
        rm.record_closed_trade(realized_pnl=-200)
    assert rm.state.halted


def test_win_resets_consecutive_losses():
    rm = make_rm(max_consecutive_losses=3)
    rm.record_closed_trade(realized_pnl=-200)
    rm.record_closed_trade(realized_pnl=-200)
    rm.record_closed_trade(realized_pnl=+500)
    assert rm.state.consecutive_losses == 0
    assert not rm.state.halted


# ---------- 已實現損益累積 ----------

def test_realized_pnl_accumulates():
    rm = make_rm()
    rm.record_closed_trade(realized_pnl=+300)
    rm.record_closed_trade(realized_pnl=-100)
    assert rm.state.realized_pnl_today == 200
    assert rm.state.trades_today == 2


def test_realized_losses_can_trigger_daily_halt():
    """純已實現虧損累積也應觸發每日停止。"""
    rm = make_rm(max_consecutive_losses=99)  # 排除連虧熔斷干擾
    rm.record_closed_trade(realized_pnl=-1500)
    rm.record_closed_trade(realized_pnl=-800)  # 累計 -2300 > 2160
    assert rm.state.halted


# ---------- should_flatten ----------

def test_should_flatten_when_halted():
    rm = make_rm()
    assert not rm.should_flatten
    rm.halt("x")
    assert rm.should_flatten
