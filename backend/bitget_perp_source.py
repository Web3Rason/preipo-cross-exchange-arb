"""Bitget V2 USDT-FUTURES ticker — WebSocket

跟 bitget_source.py（spot）用同一個 WS endpoint，但 instType 換成 USDT-FUTURES，
ticker payload 含 markPrice / indexPrice / fundingRate 等永續特有欄位。

SPCXUSDT 是 RWA 永續（isRwa=YES, maxLever=5），跟 preSPAX/preOPAI 訂閱式現貨完全
不同來源，所以需要獨立 connector。
"""

import asyncio
import json
import logging
import time
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://ws.bitget.com/v2/ws/public"
PING_INTERVAL = 25


class BitgetPerpSource:
    def __init__(self, watch_symbols: set[str]):
        self.watch_symbols = watch_symbols  # e.g. {'SPCXUSDT', ...}
        self._tickers: dict[str, dict] = {}
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def wait_ready(self, timeout: float = 10.0):
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[bitget-perp] wait_ready timeout")

    def snapshot(self, symbol: str) -> Optional[dict]:
        return self._tickers.get(symbol)

    async def _run(self):
        backoff = 1
        while True:
            try:
                logger.info(f"[bitget-perp] 連線 {WS_URL}，預計訂閱 {len(self.watch_symbols)} symbols")
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    args = [
                        {"instType": "USDT-FUTURES", "channel": "ticker", "instId": s}
                        for s in sorted(self.watch_symbols)
                    ]
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    logger.info(f"[bitget-perp] 已送出 {len(args)} 個 ticker 訂閱請求")

                    async def _ping():
                        while True:
                            await asyncio.sleep(PING_INTERVAL)
                            try:
                                await ws.send("ping")
                            except Exception:
                                break
                    ping_task = asyncio.create_task(_ping())

                    try:
                        async for raw in ws:
                            if raw == "pong":
                                continue
                            try:
                                msg = json.loads(raw)
                            except Exception:
                                continue
                            if msg.get("event"):
                                if msg.get("event") == "error":
                                    logger.warning(f"[bitget-perp] error: {msg}")
                                continue
                            arg = msg.get("arg", {})
                            data = msg.get("data")
                            if arg.get("channel") != "ticker" or not data:
                                continue
                            for d in data:
                                sym = d.get("instId")
                                if not sym:
                                    continue
                                self._tickers[sym] = {
                                    "last": _f(d.get("lastPr")),
                                    "bid": _f(d.get("bidPr")),
                                    "ask": _f(d.get("askPr")),
                                    "mark": _f(d.get("markPrice")) or _f(d.get("lastPr")),
                                    "index": _f(d.get("indexPrice")),
                                    "funding": _f(d.get("fundingRate")),
                                    "usdt_vol": _f(d.get("quoteVolume")) or _f(d.get("usdtVolume")),
                                    "ts": time.time(),
                                }
                            self._ready.set()
                            backoff = 1
                    finally:
                        ping_task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[bitget-perp] 連線中斷，{backoff}s 後重連: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
