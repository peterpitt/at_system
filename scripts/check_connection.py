"""
連線自我檢測工具 —— 只讀取，不下任何單。

驗證：
  1. .env 設定可正確載入
  2. 能登入 Shioaji
  3. 能解析 TMF 近月合約並取得保證金
  4. 能取得帳戶餘額與行情快照

請在第一次部署、或更換金鑰/憑證後執行：
    python scripts/check_connection.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import ConfigError, load_config  # noqa: E402
from src.broker import ShioajiBroker  # noqa: E402
from src.logger import get_logger  # noqa: E402

log = get_logger("check")


def main() -> int:
    try:
        cfg = load_config()
        cfg.validate()
    except ConfigError as exc:
        log.error("設定錯誤：%s", exc)
        return 2

    # 檢測一律以模擬連線，絕不觸碰實單
    broker = ShioajiBroker(simulation=True)
    try:
        broker.connect(
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
        )
        log.info("[OK] 登入成功")

        contract = broker.resolve_contract(cfg.symbol)
        log.info("[OK] 合約：%s", getattr(contract, "code", contract))

        margin = broker.contract_margin()
        log.info("[OK] 保證金（API）：%s", f"{margin:,.0f}" if margin else "無法取得，將用設定值")

        snap = broker.account_snapshot()
        log.info("[OK] 可用餘額：NT$%s", f"{snap.available_margin:,.0f}")

        price = broker.last_price()
        log.info("[OK] 最新價：%s", price)

        print("\n✅ 連線檢測全部通過。可進一步以 `python main.py --dry-run` 試跑模擬。")
    except Exception as exc:
        log.error("[FAIL] 連線檢測失敗：%s", exc)
        return 1
    finally:
        broker.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
