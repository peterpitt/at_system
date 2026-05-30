"""
風險管理核心 —— 整個系統最重要的模組。

哲學：先求生存，再談獲利。任何一筆交易在送出前，都必須通過這裡的層層檢查；
任何時刻只要觸及防線，系統會主動平倉並停止交易。

主要防線：
  1. 部位大小計算      —— 依「單筆風險金額 / 停損距離」決定口數，再受保證金與硬上限夾擊取最小值
  2. 每日虧損上限      —— 當日已實現＋未實現虧損達上限 → 當日停止
  3. 總回撤上限        —— 帳戶自起始資金回撤達上限 → 永久停止（需人工檢視）
  4. 連續虧損熔斷      —— 連續 N 筆虧損 → 當日停止
  5. 保證金充足檢查    —— 下單前確認剩餘資金足以支應，並保留緩衝
  6. 強制停損          —— 每個部位都必須帶停損價
  7. Kill switch       —— 偵測到旗標檔即立刻停止

本模組不直接呼叫券商 API，只做「決策」，方便獨立測試。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .logger import get_logger

log = get_logger("risk")

KILL_SWITCH_FILE = "KILL_SWITCH"


@dataclass
class SizingResult:
    contracts: int
    reason: str
    risk_amount: float = 0.0
    stop_points: float = 0.0


@dataclass
class RiskState:
    """當日 / 帳戶層級的風險狀態。"""

    starting_capital: float
    equity: float                      # 目前權益（含未實現）
    realized_pnl_today: float = 0.0
    consecutive_losses: int = 0
    trades_today: int = 0
    trading_day: date = field(default_factory=date.today)
    halted: bool = False               # 當日停止
    locked: bool = False               # 永久停止（總回撤）
    halt_reason: str = ""


class RiskManager:
    def __init__(
        self,
        *,
        starting_capital: float,
        point_value: float,
        margin_per_contract: float,
        risk_per_trade_pct: float,
        max_daily_loss_pct: float,
        max_total_drawdown_pct: float,
        max_contracts: int,
        max_consecutive_losses: int,
        margin_buffer: float = 1.15,
    ) -> None:
        self.point_value = point_value
        self.margin_per_contract = margin_per_contract
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_total_drawdown_pct = max_total_drawdown_pct
        self.max_contracts = max_contracts
        self.max_consecutive_losses = max_consecutive_losses
        # 下單時要求「剩餘資金 ≥ 保證金 × buffer」，預留追繳緩衝
        self.margin_buffer = margin_buffer

        self.state = RiskState(
            starting_capital=starting_capital,
            equity=starting_capital,
        )

    # ---------- 每日重置 ----------
    def roll_day_if_needed(self, today: date | None = None) -> None:
        today = today or date.today()
        if today != self.state.trading_day:
            log.info("換日重置風險狀態：%s → %s", self.state.trading_day, today)
            self.state.trading_day = today
            self.state.realized_pnl_today = 0.0
            self.state.consecutive_losses = 0
            self.state.trades_today = 0
            # 永久鎖定（locked）不因換日解除；當日停止（halted）解除
            if not self.state.locked:
                self.state.halted = False
                self.state.halt_reason = ""

    # ---------- 部位大小 ----------
    def calc_position_size(
        self, *, entry_price: float, stop_price: float, available_funds: float
    ) -> SizingResult:
        """
        依風險決定口數：
            risk_amount = 起始資金 × risk_per_trade_pct
            stop_points = |進場 - 停損|
            每口風險   = stop_points × point_value
            口數       = floor(risk_amount / 每口風險)
        再以「保證金可負擔口數」與「硬上限」夾擊取最小值。
        """
        stop_points = abs(entry_price - stop_price)
        if stop_points <= 0:
            return SizingResult(0, "停損距離為 0，拒絕進場（必須有有效停損）")

        risk_amount = self.state.starting_capital * self.risk_per_trade_pct
        risk_per_contract = stop_points * self.point_value
        by_risk = math.floor(risk_amount / risk_per_contract)

        # 保證金可負擔（含緩衝）
        usable = available_funds / self.margin_buffer
        by_margin = math.floor(usable / self.margin_per_contract)

        contracts = max(0, min(by_risk, by_margin, self.max_contracts))

        if contracts < 1:
            reason = (
                f"計算口數為 0（風險允許 {by_risk}、保證金允許 {by_margin}、"
                f"上限 {self.max_contracts}）→ 不進場"
            )
            return SizingResult(0, reason, risk_amount, stop_points)

        reason = (
            f"口數={contracts}（風險允許 {by_risk}、保證金允許 {by_margin}、"
            f"上限 {self.max_contracts}），停損 {stop_points:.0f} 點，"
            f"每口風險 {risk_per_contract:,.0f} 元"
        )
        return SizingResult(contracts, reason, risk_amount, stop_points)

    # ---------- 進場前總體檢查 ----------
    def can_open(self, *, available_funds: float, contracts: int) -> tuple[bool, str]:
        if self.state.locked:
            return False, f"帳戶已永久鎖定：{self.state.halt_reason}"
        if self.state.halted:
            return False, f"當日已停止交易：{self.state.halt_reason}"
        if Path(KILL_SWITCH_FILE).exists():
            self.trigger_kill_switch("偵測到 KILL_SWITCH 旗標檔")
            return False, "Kill switch 已啟動"
        if contracts < 1:
            return False, "口數不足，不進場"
        need = self.margin_per_contract * contracts * self.margin_buffer
        if available_funds < need:
            return False, (
                f"保證金不足：需 {need:,.0f}（含緩衝），可用 {available_funds:,.0f}"
            )
        return True, "通過風險檢查"

    # ---------- 即時權益更新與防線觸發 ----------
    def update_equity(self, *, unrealized_pnl: float) -> None:
        """以『起始資金＋當日已實現＋目前未實現』估算即時權益。"""
        self.state.equity = (
            self.state.starting_capital
            + self.state.realized_pnl_today
            + unrealized_pnl
        )
        self._check_drawdown_lines(unrealized_pnl)

    def _check_drawdown_lines(self, unrealized_pnl: float) -> None:
        # 每日虧損上限（已實現＋未實現）
        daily_loss = -(self.state.realized_pnl_today + unrealized_pnl)
        max_daily = self.state.starting_capital * self.max_daily_loss_pct
        if daily_loss >= max_daily and not self.state.halted:
            self.halt(
                f"觸及每日虧損上限：當日虧損 {daily_loss:,.0f} ≥ {max_daily:,.0f}"
            )

        # 總回撤上限
        drawdown = self.state.starting_capital - self.state.equity
        max_dd = self.state.starting_capital * self.max_total_drawdown_pct
        if drawdown >= max_dd and not self.state.locked:
            self.lock(
                f"觸及總回撤上限：回撤 {drawdown:,.0f} ≥ {max_dd:,.0f}，永久停止"
            )

    # ---------- 平倉結果回報 ----------
    def record_closed_trade(self, *, realized_pnl: float) -> None:
        self.state.realized_pnl_today += realized_pnl
        self.state.trades_today += 1
        self.state.equity = self.state.starting_capital + self.state.realized_pnl_today

        if realized_pnl < 0:
            self.state.consecutive_losses += 1
            log.info(
                "虧損平倉 %s，連續虧損 %d 筆",
                f"{realized_pnl:,.0f}",
                self.state.consecutive_losses,
            )
            if self.state.consecutive_losses >= self.max_consecutive_losses:
                self.halt(
                    f"連續虧損達 {self.state.consecutive_losses} 筆，當日熔斷"
                )
        else:
            self.state.consecutive_losses = 0
            log.info("獲利平倉 +%s", f"{realized_pnl:,.0f}")

        # 平倉後再檢查每日/總回撤
        self._check_drawdown_lines(unrealized_pnl=0.0)

    # ---------- 狀態切換 ----------
    def halt(self, reason: str) -> None:
        if not self.state.halted:
            self.state.halted = True
            self.state.halt_reason = reason
            log.warning("【當日停止】%s", reason)

    def lock(self, reason: str) -> None:
        self.state.locked = True
        self.state.halted = True
        self.state.halt_reason = reason
        log.error("【永久鎖定】%s", reason)

    def trigger_kill_switch(self, reason: str) -> None:
        self.lock(f"Kill switch：{reason}")

    # ---------- 查詢 ----------
    @property
    def should_flatten(self) -> bool:
        """是否應立即平掉所有部位（停止狀態下）。"""
        return self.state.halted or self.state.locked

    def summary(self) -> str:
        s = self.state
        return (
            f"權益={s.equity:,.0f} | 當日已實現={s.realized_pnl_today:,.0f} | "
            f"連虧={s.consecutive_losses} | 筆數={s.trades_today} | "
            f"halted={s.halted} locked={s.locked}"
        )
