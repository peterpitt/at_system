"""部位與交易狀態追蹤。

單一部位模型（系統設計為同時只持有一個方向的部位，符合小資金保守原則）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


@dataclass
class Position:
    side: Side = Side.FLAT
    contracts: int = 0
    entry_price: float = 0.0
    stop_price: float = 0.0
    take_profit_price: float = 0.0
    entry_time: datetime | None = None
    point_value: float = 10.0

    @property
    def is_open(self) -> bool:
        return self.side != Side.FLAT and self.contracts > 0

    def unrealized_pnl(self, last_price: float) -> float:
        """以最新價估算未實現損益（新台幣）。"""
        if not self.is_open:
            return 0.0
        diff = last_price - self.entry_price
        if self.side == Side.SHORT:
            diff = -diff
        return diff * self.point_value * self.contracts

    def realized_pnl(self, exit_price: float) -> float:
        diff = exit_price - self.entry_price
        if self.side == Side.SHORT:
            diff = -diff
        return diff * self.point_value * self.contracts

    def hit_stop(self, last_price: float) -> bool:
        if not self.is_open:
            return False
        if self.side == Side.LONG:
            return last_price <= self.stop_price
        return last_price >= self.stop_price

    def hit_take_profit(self, last_price: float) -> bool:
        if not self.is_open or self.take_profit_price <= 0:
            return False
        if self.side == Side.LONG:
            return last_price >= self.take_profit_price
        return last_price <= self.take_profit_price

    def open(
        self,
        *,
        side: Side,
        contracts: int,
        entry_price: float,
        stop_price: float,
        take_profit_price: float,
    ) -> None:
        self.side = side
        self.contracts = contracts
        self.entry_price = entry_price
        self.stop_price = stop_price
        self.take_profit_price = take_profit_price
        self.entry_time = datetime.now()

    def flatten(self) -> None:
        self.side = Side.FLAT
        self.contracts = 0
        self.entry_price = 0.0
        self.stop_price = 0.0
        self.take_profit_price = 0.0
        self.entry_time = None

    def __str__(self) -> str:
        if not self.is_open:
            return "FLAT（無部位）"
        return (
            f"{self.side.value} {self.contracts} 口 @ {self.entry_price:.0f} "
            f"停損 {self.stop_price:.0f} 停利 {self.take_profit_price:.0f}"
        )
