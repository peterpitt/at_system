"""
TMF 微型臺指期貨自動化交易系統 — 進入點。

用法：
    python main.py                # 依 .env 設定啟動（PAPER 或 LIVE）
    python main.py --once         # 只跑一輪（測試用）
    python main.py --dry-run      # 強制模擬，不論 .env 如何

啟動時會印出風險摘要並進行安全確認。實單需 .env 同時滿足：
    TRADING_MODE=LIVE
    CONFIRM_LIVE_TRADING=I_UNDERSTAND_THE_RISK
否則一律以模擬盤執行以保護本金。
"""
from __future__ import annotations

import argparse
import sys

from config import ConfigError, load_config
from src.broker import ShioajiBroker
from src.config_types import EngineConfig
from src.logger import get_logger
from src.notifier import Notifier
from src.risk_manager import RiskManager
from src.strategy import TrendStrategy
from src.supabase_recorder import SupabaseRecorder
from src.trader import Trader

log = get_logger("main")


def banner(cfg, effective_margin: float) -> None:
    mode = "★★★ 實單 LIVE（真實資金）★★★" if cfg.is_live else "模擬盤 PAPER（不動用真實資金）"
    affordable = int(cfg.account_capital // effective_margin)
    print("=" * 64)
    print("  微型臺指期貨 (TMF) 自動化交易系統")
    print("=" * 64)
    print(f"  交易模式        : {mode}")
    print(f"  商品            : {cfg.symbol}（每點 NT${cfg.point_value:.0f}）")
    print(f"  起始資金        : NT${cfg.account_capital:,.0f}")
    print(f"  每口保證金      : NT${effective_margin:,.0f}（可負擔 {affordable} 口）")
    print(f"  硬性口數上限    : {cfg.max_contracts} 口")
    print(f"  單筆風險        : {cfg.risk_per_trade_pct:.1%}  "
          f"(≈ NT${cfg.risk_amount_per_trade:,.0f})")
    print(f"  每日虧損上限    : {cfg.max_daily_loss_pct:.1%}  "
          f"(≈ NT${cfg.max_daily_loss:,.0f})")
    print(f"  總回撤上限      : {cfg.max_total_drawdown_pct:.1%}  "
          f"(≈ NT${cfg.max_total_drawdown:,.0f})")
    print(f"  連虧熔斷        : {cfg.max_consecutive_losses} 筆")
    print(f"  策略            : EMA({cfg.ema_fast}/{cfg.ema_slow}) + "
          f"ATR({cfg.atr_period}) 停損x{cfg.atr_stop_mult} 停利x{cfg.atr_tp_mult}")
    print(f"  交易時段        : {cfg.session_start}–{cfg.session_end}（收盤前強制平倉）")
    print("=" * 64)


def main() -> int:
    parser = argparse.ArgumentParser(description="TMF 自動化交易系統")
    parser.add_argument("--once", action="store_true", help="只跑一輪後結束")
    parser.add_argument("--dry-run", action="store_true", help="強制模擬模式")
    parser.add_argument("--poll", type=int, default=30, help="輪詢秒數（預設 30）")
    args = parser.parse_args()

    try:
        cfg = load_config()
        warnings = cfg.validate()
    except ConfigError as exc:
        log.error("設定錯誤，拒絕啟動：%s", exc)
        return 2

    for w in warnings:
        log.warning("設定提醒：%s", w)

    force_paper = args.dry_run

    notifier = Notifier(
        cfg.telegram_token,
        cfg.telegram_chat_id,
        discord_webhook_url=cfg.discord_webhook_url,
    )
    mode_label = "LIVE" if (not force_paper and cfg.is_live) else "PAPER"
    recorder = SupabaseRecorder(
        url=cfg.supabase_url,
        key=cfg.supabase_key,
        symbol=cfg.symbol,
        mode=mode_label,
    )
    simulation = True if force_paper else cfg.simulation
    broker = ShioajiBroker(simulation=simulation)

    effective_margin = cfg.margin_per_contract

    # 連線 + 解析合約 + 取得實際保證金
    try:
        broker.connect(
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
            pfx_path=cfg.pfx_path,
            pfx_password=cfg.pfx_password,
            person_id=cfg.person_id,
        )
        broker.resolve_contract(cfg.symbol)
        live_margin = broker.contract_margin()
        if live_margin and live_margin > 0:
            effective_margin = live_margin
            log.info("以 API 取得實際保證金：NT$%s", f"{live_margin:,.0f}")
    except Exception as exc:
        log.error("券商連線/合約解析失敗：%s", exc)
        log.error("請確認 API 金鑰、憑證與網路，並已安裝 shioaji。")
        return 3

    banner(cfg, effective_margin)

    # 實單最後確認閘門
    if simulation:
        log.info("以模擬盤執行，不會動用真實資金。")
    else:
        log.warning("★ 即將以『真實資金』自動交易。10 秒後開始，按 Ctrl+C 可中止。★")
        try:
            import time as _t

            for i in range(10, 0, -1):
                print(f"  ...{i}", end="\r", flush=True)
                _t.sleep(1)
            print()
        except KeyboardInterrupt:
            log.info("使用者中止啟動。")
            broker.disconnect()
            return 0

    risk = RiskManager(
        starting_capital=cfg.account_capital,
        point_value=cfg.point_value,
        margin_per_contract=effective_margin,
        risk_per_trade_pct=cfg.risk_per_trade_pct,
        max_daily_loss_pct=cfg.max_daily_loss_pct,
        max_total_drawdown_pct=cfg.max_total_drawdown_pct,
        max_contracts=cfg.max_contracts,
        max_consecutive_losses=cfg.max_consecutive_losses,
    )
    strategy = TrendStrategy(
        ema_fast=cfg.ema_fast,
        ema_slow=cfg.ema_slow,
        atr_period=cfg.atr_period,
        atr_stop_mult=cfg.atr_stop_mult,
        atr_tp_mult=cfg.atr_tp_mult,
    )
    engine_cfg = EngineConfig(
        symbol=cfg.symbol,
        point_value=cfg.point_value,
        bar_minutes=cfg.bar_minutes,
        session_start=cfg.session_start,
        session_end=cfg.session_end,
        enable_night_session=cfg.enable_night_session,
    )
    trader = Trader(
        cfg=engine_cfg,
        broker=broker,
        strategy=strategy,
        risk=risk,
        notifier=notifier,
        recorder=recorder,
    )

    try:
        trader.run(
            poll_seconds=args.poll,
            max_iterations=1 if args.once else None,
        )
    except KeyboardInterrupt:
        log.info("收到中斷訊號，安全關閉中…")
        trader.stop()
    finally:
        broker.disconnect()

    return 0


if __name__ == "__main__":
    sys.exit(main())
