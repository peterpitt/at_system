"""
交易引擎：串接行情、策略、風控、券商與部位管理的主迴圈。

每一輪 (tick) 的流程：
  1. 換日重置風控 → 檢查 kill switch / 是否已停止
  2. 取得最新價與 K 棒
  3. 更新未實現損益 → 觸發每日虧損 / 總回撤防線
  4. 若需停止 → 立即平倉並結束當輪
  5. 若有部位：檢查停損 / 停利 / 策略反轉 → 平倉
  6. 若無部位且在可交易時段：評估進場訊號 → 風控核可 → 下單
  7. 收盤前強制平倉（不留倉過夜，避免跳空風險）

此設計刻意保守：同時只持一個方向、單一部位、強制停損、收盤平倉。
"""
from __future__ import annotations

import time as _time
from datetime import datetime, time

from .broker import ShioajiBroker
from .config_types import EngineConfig
from .logger import get_logger
from .notifier import Notifier
from .position import Position, Side
from .risk_manager import RiskManager
from .strategy import Signal, StrategyDecision, TrendStrategy

log = get_logger("trader")


class Trader:
    def __init__(
        self,
        *,
        cfg: EngineConfig,
        broker: ShioajiBroker,
        strategy: TrendStrategy,
        risk: RiskManager,
        notifier: Notifier,
    ) -> None:
        self.cfg = cfg
        self.broker = broker
        self.strategy = strategy
        self.risk = risk
        self.notifier = notifier
        self.position = Position(point_value=cfg.point_value)
        self._running = False

    # ---------- 時段判斷 ----------
    def _in_session(self, now: time | None = None) -> bool:
        now = now or datetime.now().time()
        return self.cfg.session_start <= now <= self.cfg.session_end

    def _past_forced_close(self, now: time | None = None) -> bool:
        """收盤前緩衝：到達 session_end 即強制平倉。"""
        now = now or datetime.now().time()
        return now >= self.cfg.session_end

    # ---------- 平倉 ----------
    def _flatten(self, reason: str) -> None:
        if not self.position.is_open:
            return
        try:
            exit_price = self.broker.last_price()
        except Exception as exc:  # pragma: no cover
            log.error("平倉前取價失敗，仍嘗試市價平倉：%s", exc)
            exit_price = self.position.entry_price

        self.broker.close_position_market(
            position_side=self.position.side, quantity=self.position.contracts
        )
        pnl = self.position.realized_pnl(exit_price)
        msg = f"平倉（{reason}）：{self.position} 出場 {exit_price:.0f}，損益 {pnl:,.0f}"
        log.info(msg)
        self.notifier.send(msg)
        self.risk.record_closed_trade(realized_pnl=pnl)
        self.position.flatten()

    # ---------- 進場 ----------
    def _try_enter(self, decision: StrategyDecision, available_funds: float) -> None:
        sizing = self.risk.calc_position_size(
            entry_price=decision.entry_price,
            stop_price=decision.stop_price,
            available_funds=available_funds,
        )
        log.info("部位試算：%s", sizing.reason)
        if sizing.contracts < 1:
            return

        ok, why = self.risk.can_open(
            available_funds=available_funds, contracts=sizing.contracts
        )
        if not ok:
            log.info("風控否決進場：%s", why)
            return

        side = Side.LONG if decision.signal == Signal.LONG else Side.SHORT
        self.broker.place_market_order(side=side, quantity=sizing.contracts)
        self.position.open(
            side=side,
            contracts=sizing.contracts,
            entry_price=decision.entry_price,
            stop_price=decision.stop_price,
            take_profit_price=decision.take_profit_price,
        )
        msg = f"進場：{self.position}｜{decision.note}"
        log.info(msg)
        self.notifier.send(msg)

    # ---------- 單輪 ----------
    def tick(self) -> None:
        self.risk.roll_day_if_needed()

        # 取價與帳務
        try:
            last_price = self.broker.last_price()
            snap = self.broker.account_snapshot()
        except Exception as exc:
            log.error("取得行情/帳務失敗，本輪跳過：%s", exc)
            return

        available_funds = snap.available_margin or self.risk.state.equity

        # 更新未實現損益 → 觸發防線
        upnl = self.position.unrealized_pnl(last_price)
        self.risk.update_equity(unrealized_pnl=upnl)

        # 停止狀態 → 平倉並結束
        if self.risk.should_flatten:
            if self.position.is_open:
                self._flatten(f"風控停止：{self.risk.state.halt_reason}")
            return

        # 持有部位：即時停損 / 停利
        if self.position.is_open:
            if self.position.hit_stop(last_price):
                self._flatten("觸及停損")
                return
            if self.position.hit_take_profit(last_price):
                self._flatten("觸及停利")
                return

        # 收盤前強制平倉，不留倉
        if self._past_forced_close():
            if self.position.is_open:
                self._flatten("收盤前強制平倉")
            return

        # 取 K 棒評估策略（剔除最後一根未完成 K 棒）
        try:
            bars = self.broker.get_kbars(minutes=self.cfg.bar_minutes)
        except Exception as exc:
            log.error("取得 K 棒失敗，本輪跳過：%s", exc)
            return
        if bars.empty or len(bars) < 2:
            return
        closed_bars = bars.iloc[:-1]

        decision = self.strategy.evaluate(closed_bars, self.position.side)

        # 策略反轉 → 平倉
        if decision.signal == Signal.CLOSE and self.position.is_open:
            self._flatten(decision.note)
            return

        # 進場（限可交易時段）
        if decision.signal in (Signal.LONG, Signal.SHORT) and not self.position.is_open:
            if self._in_session():
                self._try_enter(decision, available_funds)
            else:
                log.debug("有訊號但非交易時段，略過：%s", decision.note)

    # ---------- 主迴圈 ----------
    def run(self, *, poll_seconds: int = 30, max_iterations: int | None = None) -> None:
        self._running = True
        log.info("交易引擎啟動。%s", self.risk.summary())
        self.notifier.send("TMF 自動交易引擎已啟動。")
        n = 0
        try:
            while self._running:
                try:
                    self.tick()
                except Exception as exc:  # 單輪錯誤不應拖垮整個引擎
                    log.exception("tick 發生未預期例外：%s", exc)
                if self.risk.state.locked:
                    log.error("帳戶永久鎖定，引擎停止。%s", self.risk.summary())
                    self.notifier.send(f"帳戶鎖定，引擎停止：{self.risk.state.halt_reason}")
                    break
                n += 1
                if max_iterations is not None and n >= max_iterations:
                    break
                _time.sleep(poll_seconds)
        finally:
            self._running = False
            if self.position.is_open:
                self._flatten("引擎關閉，清空部位")
            log.info("交易引擎結束。%s", self.risk.summary())

    def stop(self) -> None:
        self._running = False
