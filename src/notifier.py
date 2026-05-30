"""通知模組：重要事件（進出場、觸發防線、錯誤）發送到 Telegram；
未設定 token 時僅寫入日誌。
"""
from __future__ import annotations

from .logger import get_logger

log = get_logger("notify")

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class Notifier:
    def __init__(self, token: str = "", chat_id: str = "") -> None:
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id and requests is not None)
        if token and chat_id and requests is None:
            log.warning("已設定 Telegram 但缺少 requests 套件，通知停用。")

    def send(self, message: str) -> None:
        log.info("通知：%s", message)
        if not self.enabled:
            return
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            requests.post(
                url,
                json={"chat_id": self.chat_id, "text": message},
                timeout=10,
            )
        except Exception as exc:  # 通知失敗絕不可中斷交易主流程
            log.warning("Telegram 通知失敗：%s", exc)
