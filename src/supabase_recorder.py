"""Supabase 交易紀錄模組：每筆交易平倉後寫入 Supabase。

未設定 SUPABASE_URL / SUPABASE_KEY 時為 no-op，不影響交易主流程。
任何寫入錯誤均只寫日誌，絕不拋出例外中斷交易。

建議在 Supabase 先建好以下資料表：

  create table if not exists tmf_trades (
    id                bigserial primary key,
    created_at        timestamptz default now(),
    trade_date        date        not null,
    mode              text        not null,   -- PAPER / LIVE
    symbol            text        not null,
    side              text        not null,   -- LONG / SHORT
    contracts         int         not null,
    entry_price       numeric     not null,
    exit_price        numeric     not null,
    stop_price        numeric,
    take_profit_price numeric,
    realized_pnl      numeric     not null,
    close_reason      text
  );
"""
from __future__ import annotations

from datetime import date, datetime

from .logger import get_logger

log = get_logger("supabase")


class SupabaseRecorder:
    def __init__(self, url: str = "", key: str = "", symbol: str = "TMF", mode: str = "PAPER") -> None:
        self._client = None
        self.symbol = symbol
        self.mode = mode

        if not (url and key):
            log.info("未設定 Supabase，交易紀錄功能停用。")
            return

        try:
            from supabase import create_client  # lazy import

            self._client = create_client(url, key)
            log.info("Supabase 連線初始化完成。")
        except ImportError:
            log.warning("supabase 套件未安裝（pip install supabase），紀錄功能停用。")
        except Exception as exc:
            log.warning("Supabase 初始化失敗：%s", exc)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def record_closed_trade(
        self,
        *,
        side: str,
        contracts: int,
        entry_price: float,
        exit_price: float,
        stop_price: float | None = None,
        take_profit_price: float | None = None,
        realized_pnl: float,
        close_reason: str = "",
    ) -> None:
        if not self.enabled:
            return
        row = {
            "trade_date": date.today().isoformat(),
            "mode": self.mode,
            "symbol": self.symbol,
            "side": side,
            "contracts": contracts,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "stop_price": stop_price,
            "take_profit_price": take_profit_price,
            "realized_pnl": realized_pnl,
            "close_reason": close_reason,
        }
        try:
            self._client.table("tmf_trades").insert(row).execute()
            log.info(
                "Supabase 寫入成功：%s %s口 進%.0f 出%.0f 損益%+.0f",
                side, contracts, entry_price, exit_price, realized_pnl,
            )
        except Exception as exc:
            log.warning("Supabase 寫入失敗（不影響交易）：%s", exc)
