"""統一日誌設定。

同時輸出到 console 與檔案（logs/）。交易系統的每個決策都必須留痕，
出問題時才能事後追查。
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

_CONFIGURED = False


def setup_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"trade_{datetime.now():%Y%m%d}.log"

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-14s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
