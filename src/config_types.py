"""引擎層使用的精簡設定型別，與根目錄 config.Config 解耦，方便測試。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class EngineConfig:
    symbol: str
    point_value: float
    bar_minutes: int
    session_start: time
    session_end: time
    enable_night_session: bool
