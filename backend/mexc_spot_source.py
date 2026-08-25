"""MEXC spot REST polling — V3 spot WS 已升級 protobuf-only，破例走 REST

實測（2026-05-24）：MEXC spot WS（wbs.mexc.com / wbs-api.mexc.com）對任何
`spot@public.bookTicker.v3.api@<symbol>` JSON 訂閱都回 `Reason: Blocked!`。
只有 `.pb` 後綴的 protobuf channel（如 `spot@public.aggre.deals.v3.api.pb@100ms@<sym>`）
會推資料，但是 binary protobuf 需要額外的 schema 解析。

對 SPACEX(PRE) 這種低頻 SPV 鏡像 token 來說 protobuf 投資成本不划算 →
破例走 REST `/api/v3/ticker/bookTicker` 10 秒輪詢。同 BingX funding 模式。
"""

import asyncio
import logging
import time
from typing import Optional
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)

REST_BOOKTICKER_URL = "https://api.mexc.com/api/v3/ticker/bookTicker"
REST_24HR_URL = "https://api.mexc.com/api/v3/ticker/24hr"
POLL_INTERVAL = 10  # 秒，spot SPV 不需要更快


class MexcSpotSource:
    def __init__(self, watch_symbols: set[str]):
        self.watch_symbols = watch_symbols  # e.g. {'SPACEX(PRE)USDT', ...}
        self._tickers: dict[str, dict] = {}
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def wait_ready(self, timeout: float = 10.0):
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[mexc-spot] wait_ready timeout")

    def snapshot(self, symbol: str) -> Optional[dict]:
        return self._tickers.get(symbol)

    async def _run(self):
        logger.info(f"[mexc-spot] REST polling {len(self.watch_symbols)} symbols every {POLL_INTERVAL}s")
        async with aiohttp.ClientSession() as sess:
            while True:
                try:
                    for sym in self.watch_symbols:
                        # symbol 含括號要 url-encode（() 在 query string 雖通常合法但保險起見）
                        params_sym = {"symbol": sym}
                        async with sess.get(REST_BOOKTICKER_URL, params=params_sym,
                                            timeout=aiohttp.ClientTimeout(total=8)) as r:
                            bt = await r.json()
                        async with sess.get(REST_24HR_URL, params=params_sym,
                                            timeout=aiohttp.ClientTimeout(total=8)) as r:
                            tk = await r.json()
                        if not isinstance(bt, dict) or not isinstance(tk, dict):
                            continue
                        last = _f(tk.get("lastPrice"))
                        bid = _f(bt.get("bidPrice"))
                        ask = _f(bt.get("askPrice"))
                        self._tickers[sym] = {
                            "last": last,
                            "bid": bid,
                            "ask": ask,
                            "mark": last,  # spot 無 mark；用 last 代替
                            "usdt_vol": _f(tk.get("quoteVolume")),
                            "ts": time.time(),
                        }
                        self._ready.set()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"[mexc-spot] REST poll 失敗: {e}")
                await asyncio.sleep(POLL_INTERVAL)


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
