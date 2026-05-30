"""
交易策略：EMA 趨勢跟隨 + ATR 動態停損 / 停利。

選擇理由（針對小資金、保守取向）：
  * 趨勢跟隨：順勢操作，避免在震盪中頻繁進出累積手續費與滑價。
  * EMA 快慢線交叉：定義方向，邏輯簡單、不易過度擬合。
  * ATR 決定停損距離：依市場波動自適應，波動大時停損放寬、口數自動縮小。
  * 風報比 > 1（停利倍數 > 停損倍數）：長期維持正期望值的必要條件之一。

本模組只負責「產生訊號與計算停損停利」，不下單、不管錢。
所有指標以純 pandas/numpy 實作，不依賴額外套件。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd

from .logger import get_logger
from .position import Side

log = get_logger("strategy")


class Signal(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"
    CLOSE = "CLOSE"  # 趨勢反轉，平掉現有部位


@dataclass
class StrategyDecision:
    signal: Signal
    entry_price: float = 0.0
    stop_price: float = 0.0
    take_profit_price: float = 0.0
    atr: float = 0.0
    note: str = ""


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Average True Range。df 需含 high/low/close 欄位。"""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


class TrendStrategy:
    def __init__(
        self,
        *,
        ema_fast: int,
        ema_slow: int,
        atr_period: int,
        atr_stop_mult: float,
        atr_tp_mult: float,
    ) -> None:
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.atr_period = atr_period
        self.atr_stop_mult = atr_stop_mult
        self.atr_tp_mult = atr_tp_mult

    @property
    def min_bars(self) -> int:
        """產生穩定訊號所需的最少 K 棒數。"""
        return max(self.ema_slow, self.atr_period) * 3

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema_fast"] = ema(df["close"], self.ema_fast)
        df["ema_slow"] = ema(df["close"], self.ema_slow)
        df["atr"] = atr(df, self.atr_period)
        return df

    def evaluate(self, df: pd.DataFrame, current_side: Side) -> StrategyDecision:
        """
        以「已收盤」的 K 棒評估（呼叫端應傳入不含當前未完成 K 棒的資料）。

        進場：
            黃金交叉（fast 由下往上穿越 slow）→ 做多
            死亡交叉（fast 由上往下穿越 slow）→ 做空
        出場：
            趨勢反轉（出現反向交叉）→ 平倉（停損/停利由部位層另行即時監控）
        """
        if len(df) < self.min_bars:
            return StrategyDecision(Signal.NONE, note=f"K棒不足（{len(df)}/{self.min_bars}）")

        d = self.compute_indicators(df)
        last = d.iloc[-1]
        prev = d.iloc[-2]

        if np.isnan(last["atr"]) or last["atr"] <= 0:
            return StrategyDecision(Signal.NONE, note="ATR 無效")

        golden = prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]
        death = prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]

        price = float(last["close"])
        a = float(last["atr"])
        stop_dist = a * self.atr_stop_mult
        tp_dist = a * self.atr_tp_mult

        # 已有部位時，反向交叉 → 平倉
        if current_side == Side.LONG and death:
            return StrategyDecision(Signal.CLOSE, entry_price=price, atr=a, note="死亡交叉，平多")
        if current_side == Side.SHORT and golden:
            return StrategyDecision(Signal.CLOSE, entry_price=price, atr=a, note="黃金交叉，平空")

        # 無部位時尋找進場
        if current_side == Side.FLAT:
            if golden:
                return StrategyDecision(
                    Signal.LONG,
                    entry_price=price,
                    stop_price=price - stop_dist,
                    take_profit_price=price + tp_dist,
                    atr=a,
                    note=f"黃金交叉做多 (ATR={a:.1f})",
                )
            if death:
                return StrategyDecision(
                    Signal.SHORT,
                    entry_price=price,
                    stop_price=price + stop_dist,
                    take_profit_price=price - tp_dist,
                    atr=a,
                    note=f"死亡交叉做空 (ATR={a:.1f})",
                )

        return StrategyDecision(Signal.NONE, atr=a, note="無訊號")
