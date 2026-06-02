"""
集中設定載入與驗證。

所有參數來自環境變數（.env）。本模組負責：
  1. 載入 .env
  2. 型別轉換與合理性驗證
  3. 對危險設定（如 LIVE 模式、過大部位）發出明確警告或直接拒絕

設計原則：寧可在啟動時因設定不合理而拒絕執行，
也不要帶著錯誤設定去碰真實資金。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv 應已安裝
    pass


def _get(key: str, default: str | None = None) -> str | None:
    val = os.getenv(key, default)
    if val is not None:
        val = val.strip()
    return val or default


def _get_float(key: str, default: float) -> float:
    raw = _get(key)
    if raw is None:
        return default
    return float(raw)


def _get_int(key: str, default: int) -> int:
    raw = _get(key)
    if raw is None:
        return default
    return int(raw)


def _get_bool(key: str, default: bool) -> bool:
    raw = _get(key)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "y", "on")


def _parse_time(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


class ConfigError(Exception):
    """設定不合理時拋出，阻止系統啟動。"""


@dataclass(frozen=True)
class Config:
    # --- 認證 ---
    api_key: str
    api_secret: str
    account_id: str
    pfx_path: str
    pfx_password: str
    person_id: str

    # --- 模式 ---
    trading_mode: str  # PAPER / LIVE
    confirm_live: str

    # --- 資金與風險 ---
    account_capital: float
    symbol: str
    margin_per_contract: float
    risk_per_trade_pct: float
    max_daily_loss_pct: float
    max_total_drawdown_pct: float
    max_contracts: int
    max_consecutive_losses: int

    # --- 策略 ---
    bar_minutes: int
    ema_fast: int
    ema_slow: int
    atr_period: int
    atr_stop_mult: float
    atr_tp_mult: float
    session_start: time
    session_end: time
    enable_night_session: bool

    # --- 通知 ---
    telegram_token: str
    telegram_chat_id: str
    discord_webhook_url: str

    # --- Supabase ---
    supabase_url: str
    supabase_key: str

    # ----- 衍生屬性 -----
    @property
    def is_live(self) -> bool:
        """唯有 LIVE 模式 + 第二道確認鎖通過，才視為真正實單。"""
        return (
            self.trading_mode.upper() == "LIVE"
            and self.confirm_live == "I_UNDERSTAND_THE_RISK"
        )

    @property
    def simulation(self) -> bool:
        """傳給 Shioaji 的 simulation 參數。非 live 即模擬。"""
        return not self.is_live

    @property
    def point_value(self) -> float:
        """微型臺指每點價值（新台幣）。"""
        return 10.0

    @property
    def risk_amount_per_trade(self) -> float:
        return self.account_capital * self.risk_per_trade_pct

    @property
    def max_daily_loss(self) -> float:
        return self.account_capital * self.max_daily_loss_pct

    @property
    def max_total_drawdown(self) -> float:
        return self.account_capital * self.max_total_drawdown_pct

    def validate(self) -> list[str]:
        """回傳警告清單；遇到致命問題則拋出 ConfigError。"""
        warnings: list[str] = []

        if self.trading_mode.upper() not in ("PAPER", "LIVE"):
            raise ConfigError(f"TRADING_MODE 必須為 PAPER 或 LIVE，目前為 {self.trading_mode!r}")

        # 實單需金鑰與憑證
        if self.is_live:
            missing = [
                name
                for name, val in (
                    ("SINOPAC_API_KEY", self.api_key),
                    ("SINOPAC_API_SECRET", self.api_secret),
                    ("SINOPAC_PFX_PATH", self.pfx_path),
                    ("SINOPAC_PFX_PASSWORD", self.pfx_password),
                    ("SINOPAC_PERSON_ID", self.person_id),
                )
                if not val
            ]
            if missing:
                raise ConfigError(
                    "LIVE 模式缺少必要設定：" + ", ".join(missing)
                )
            if self.pfx_path and not Path(self.pfx_path).exists():
                raise ConfigError(f"找不到憑證檔：{self.pfx_path}")

        if self.trading_mode.upper() == "LIVE" and not self.is_live:
            warnings.append(
                "TRADING_MODE=LIVE 但 CONFIRM_LIVE_TRADING 未設為 "
                "I_UNDERSTAND_THE_RISK → 系統將以『模擬盤』執行以保護資金。"
            )

        # 風險合理性
        if self.risk_per_trade_pct <= 0 or self.risk_per_trade_pct > 0.05:
            raise ConfigError(
                f"RISK_PER_TRADE_PCT={self.risk_per_trade_pct} 不在 (0, 0.05] 合理範圍。"
                "單筆風險超過 5% 對小資金而言過高。"
            )
        if self.max_daily_loss_pct <= 0 or self.max_daily_loss_pct > 0.10:
            raise ConfigError(
                f"MAX_DAILY_LOSS_PCT={self.max_daily_loss_pct} 不在 (0, 0.10] 合理範圍。"
            )
        if self.max_contracts < 1:
            raise ConfigError("MAX_CONTRACTS 至少為 1。")

        # 資金 vs 保證金安全檢查
        if self.margin_per_contract <= 0:
            raise ConfigError("MARGIN_PER_CONTRACT 必須為正數。")
        affordable = int(self.account_capital // self.margin_per_contract)
        if affordable < 1:
            raise ConfigError(
                f"可用資金 {self.account_capital:,.0f} 元 不足以支應一口保證金 "
                f"{self.margin_per_contract:,.0f} 元。無法安全交易。"
            )
        if self.max_contracts > affordable:
            warnings.append(
                f"MAX_CONTRACTS={self.max_contracts} 超過資金可負擔口數 {affordable}，"
                f"系統會自動下修。"
            )
        # 對小資金提醒：建議僅 1 口
        if self.max_contracts > 1 and self.account_capital < self.margin_per_contract * 3:
            warnings.append(
                "資金低於 3 口保證金，建議 MAX_CONTRACTS=1 以保留足夠緩衝，"
                "避免盤中波動觸及追繳。"
            )

        if self.ema_fast >= self.ema_slow:
            raise ConfigError("EMA_FAST 必須小於 EMA_SLOW。")
        if self.atr_tp_mult <= self.atr_stop_mult:
            warnings.append(
                "ATR_TP_MULT 未大於 ATR_STOP_MULT，風報比 < 1，長期期望值偏低。"
            )

        return warnings


def load_config() -> Config:
    cfg = Config(
        api_key=_get("SINOPAC_API_KEY", "") or "",
        api_secret=_get("SINOPAC_API_SECRET", "") or "",
        account_id=_get("SINOPAC_ACCOUNT_ID", "") or "",
        pfx_path=_get("SINOPAC_PFX_PATH", "./Sinopac.pfx") or "",
        pfx_password=_get("SINOPAC_PFX_PASSWORD", "") or "",
        person_id=_get("SINOPAC_PERSON_ID", "") or "",
        trading_mode=_get("TRADING_MODE", "PAPER") or "PAPER",
        confirm_live=_get("CONFIRM_LIVE_TRADING", "") or "",
        account_capital=_get_float("ACCOUNT_CAPITAL", 72000),
        symbol=_get("SYMBOL", "TMF") or "TMF",
        margin_per_contract=_get_float("MARGIN_PER_CONTRACT", 26300),
        risk_per_trade_pct=_get_float("RISK_PER_TRADE_PCT", 0.01),
        max_daily_loss_pct=_get_float("MAX_DAILY_LOSS_PCT", 0.03),
        max_total_drawdown_pct=_get_float("MAX_TOTAL_DRAWDOWN_PCT", 0.15),
        max_contracts=_get_int("MAX_CONTRACTS", 1),
        max_consecutive_losses=_get_int("MAX_CONSECUTIVE_LOSSES", 3),
        bar_minutes=_get_int("BAR_MINUTES", 5),
        ema_fast=_get_int("EMA_FAST", 12),
        ema_slow=_get_int("EMA_SLOW", 26),
        atr_period=_get_int("ATR_PERIOD", 14),
        atr_stop_mult=_get_float("ATR_STOP_MULT", 2.0),
        atr_tp_mult=_get_float("ATR_TP_MULT", 3.0),
        session_start=_parse_time(_get("SESSION_START", "09:00")),
        session_end=_parse_time(_get("SESSION_END", "13:25")),
        enable_night_session=_get_bool("ENABLE_NIGHT_SESSION", False),
        telegram_token=_get("TELEGRAM_BOT_TOKEN", "") or "",
        telegram_chat_id=_get("TELEGRAM_CHAT_ID", "") or "",
        discord_webhook_url=_get("DISCORD_WEBHOOK_URL", "") or "",
        supabase_url=_get("SUPABASE_URL", "") or "",
        supabase_key=_get("SUPABASE_KEY", "") or "",
    )
    return cfg
