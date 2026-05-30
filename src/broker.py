"""
永豐 Shioaji 券商封裝層。

對外提供乾淨的介面（登入、取得合約、報價、下單、查詢部位/保證金），
對內隔離 Shioaji SDK 的細節與版本差異。

重要安全設計：
  * simulation 旗標由 config.is_live 推導；只有「LIVE + 第二道確認鎖」才會 simulation=False。
  * 實單必須先 activate_ca 活化憑證，否則 Shioaji 不允許送單。
  * 下單一律帶 octype=Auto，由券商判斷新倉/平倉，避免方向錯置。

Shioaji 並非總是安裝（例如在 CI 或本機驗證風控邏輯時），
因此採延遲匯入；未安裝時呼叫需要 SDK 的方法才會報錯。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from .logger import get_logger
from .position import Side

log = get_logger("broker")


@dataclass
class AccountSnapshot:
    available_margin: float   # 可用（權益）餘額
    equity: float
    positions: list


class ShioajiBroker:
    def __init__(self, *, simulation: bool) -> None:
        self.simulation = simulation
        self._api = None
        self._contract = None
        self._sj = None  # shioaji module
        self._fop_account = None

    # ---------- 連線 / 登入 ----------
    def connect(
        self,
        *,
        api_key: str,
        api_secret: str,
        pfx_path: str = "",
        pfx_password: str = "",
        person_id: str = "",
    ) -> None:
        import shioaji as sj  # 延遲匯入

        self._sj = sj
        mode = "模擬盤 (simulation)" if self.simulation else "★實單 (LIVE)★"
        log.info("建立 Shioaji 連線，模式：%s", mode)

        self._api = sj.Shioaji(simulation=self.simulation)
        accounts = self._api.login(api_key=api_key, secret_key=api_secret)
        log.info("登入成功，帳戶數：%d", len(accounts) if accounts else 0)

        self._fop_account = self._api.futopt_account

        # 實單需活化憑證才能下單
        if not self.simulation:
            if not (pfx_path and pfx_password and person_id):
                raise RuntimeError("實單模式必須提供憑證路徑、密碼與身分證字號以活化 CA。")
            ok = self._api.activate_ca(
                ca_path=pfx_path,
                ca_passwd=pfx_password,
                person_id=person_id,
            )
            if not ok:
                raise RuntimeError("憑證活化 (activate_ca) 失敗，無法進行實單。")
            log.info("憑證活化成功。")

    def disconnect(self) -> None:
        if self._api is not None:
            try:
                self._api.logout()
                log.info("已登出 Shioaji。")
            except Exception as exc:  # pragma: no cover
                log.warning("登出時發生例外：%s", exc)

    # ---------- 合約 ----------
    def resolve_contract(self, symbol: str):
        """
        取得近月合約。對微型臺指 (TMF)，Shioaji 提供連續近月代碼（R1）。
        優先使用 R1（近月連續），找不到再退而選最近到期的月合約。
        """
        category = getattr(self._api.Contracts.Futures, symbol, None)
        if category is None:
            raise RuntimeError(f"找不到期貨商品類別：{symbol}")

        # 連續近月：多為 {symbol}R1
        r1 = getattr(category, f"{symbol}R1", None)
        if r1 is not None:
            self._contract = r1
            log.info("使用近月連續合約：%s (%s)", r1.code, getattr(r1, "name", ""))
            return r1

        # 後備：挑選最近且尚未到期的月合約
        candidates = [c for c in category if getattr(c, "delivery_date", "")]
        candidates.sort(key=lambda c: c.delivery_date)
        today = datetime.now().strftime("%Y/%m/%d")
        for c in candidates:
            if c.delivery_date >= today:
                self._contract = c
                log.info("使用近月合約：%s 到期 %s", c.code, c.delivery_date)
                return c
        raise RuntimeError(f"無法解析 {symbol} 的近月合約。")

    @property
    def contract(self):
        if self._contract is None:
            raise RuntimeError("尚未解析合約，請先呼叫 resolve_contract。")
        return self._contract

    def contract_margin(self) -> float | None:
        """嘗試取得該合約原始保證金；失敗回傳 None（由 config 值兜底）。"""
        try:
            margin = getattr(self._contract, "margin_trading_floor", None)
            if margin:
                return float(margin)
        except Exception:  # pragma: no cover
            pass
        return None

    # ---------- 行情 ----------
    def get_kbars(self, *, minutes: int, lookback_days: int = 5) -> pd.DataFrame:
        """
        取得歷史 K 棒並重採樣為指定分鐘週期。
        回傳含 open/high/low/close/volume 欄位、以時間為索引的 DataFrame。
        """
        end = datetime.now()
        start = end - timedelta(days=lookback_days)
        kb = self._api.kbars(
            self._contract,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
        )
        df = pd.DataFrame({**kb})
        if df.empty:
            return df
        df["ts"] = pd.to_datetime(df["ts"])
        df = df.set_index("ts").sort_index()
        df = df.rename(
            columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )
        rule = f"{minutes}min"
        agg = (
            df.resample(rule)
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .dropna()
        )
        return agg

    def last_price(self) -> float:
        """以快照取得最新成交價。"""
        snaps = self._api.snapshots([self._contract])
        if not snaps:
            raise RuntimeError("取得快照失敗。")
        return float(snaps[0].close)

    # ---------- 帳務 ----------
    def account_snapshot(self) -> AccountSnapshot:
        margin = self._api.margin(self._fop_account)
        positions = self._api.list_positions(self._fop_account)
        # available_margin / equity_amount 視 SDK 版本而定，取常見欄位
        available = getattr(margin, "available_margin", None)
        equity = getattr(margin, "equity_amount", None)
        if available is None:
            available = getattr(margin, "available_balance", 0.0)
        if equity is None:
            equity = available
        return AccountSnapshot(
            available_margin=float(available or 0.0),
            equity=float(equity or 0.0),
            positions=list(positions or []),
        )

    # ---------- 下單 ----------
    def place_market_order(self, *, side: Side, quantity: int):
        """以市價單送出（IOC）。side 決定買賣，octype=Auto 由券商判斷新平倉。"""
        sj = self._sj
        action = sj.constant.Action.Buy if side == Side.LONG else sj.constant.Action.Sell
        order = self._api.Order(
            action=action,
            price=0,
            quantity=quantity,
            price_type=sj.constant.FuturesPriceType.MKP,  # 市價
            order_type=sj.constant.OrderType.IOC,
            octype=sj.constant.FuturesOCType.Auto,
            account=self._fop_account,
        )
        trade = self._api.place_order(self._contract, order)
        log.info("送出市價單：%s x%d → %s", side.value, quantity, getattr(trade, "status", ""))
        return trade

    def close_position_market(self, *, position_side: Side, quantity: int):
        """市價平倉：與持倉方向相反送單。"""
        opposite = Side.SHORT if position_side == Side.LONG else Side.LONG
        return self.place_market_order(side=opposite, quantity=quantity)

    def update_status(self) -> None:
        try:
            self._api.update_status(self._fop_account)
        except Exception as exc:  # pragma: no cover
            log.warning("更新委託狀態失敗：%s", exc)
