"""通知模組：重要事件（進出場、觸發防線、錯誤）發送到 Telegram 與 Discord；
未設定時僅寫入日誌。任何通知失敗均不中斷交易主流程。
"""
from __future__ import annotations

from .logger import get_logger

log = get_logger("notify")

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class Notifier:
    def __init__(
        self,
        token: str = "",
        chat_id: str = "",
        discord_webhook_url: str = "",
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.discord_webhook_url = discord_webhook_url

        self._tg_enabled = bool(token and chat_id and requests is not None)
        self._discord_enabled = bool(discord_webhook_url and requests is not None)

        if (token or chat_id) and requests is None:
            log.warning("已設定 Telegram 但缺少 requests 套件，Telegram 通知停用。")
        if discord_webhook_url and requests is None:
            log.warning("已設定 Discord 但缺少 requests 套件，Discord 通知停用。")

    def send(self, message: str) -> None:
        log.info("通知：%s", message)
        self._send_telegram(message)
        self._send_discord(message)

    def _send_telegram(self, message: str) -> None:
        if not self._tg_enabled:
            return
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            requests.post(
                url,
                json={"chat_id": self.chat_id, "text": message},
                timeout=10,
            )
        except Exception as exc:
            log.warning("Telegram 通知失敗：%s", exc)

    def _send_discord(self, message: str) -> None:
        if not self._discord_enabled:
            return
        try:
            requests.post(
                self.discord_webhook_url,
                json={"content": message},
                timeout=10,
            )
        except Exception as exc:
            log.warning("Discord 通知失敗：%s", exc)
