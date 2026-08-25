"""Ourbit futures WS — sub.ticker（MEXC 白標格式）

wss://futures.ourbit.com/edge，無需 API key。
SPCX_USDT 為 per_share_usd（與 Aster 同價位區間）。
"""

import asyncio
import json
import logging
import time
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://futures.ourbit.com/edge"
PING_INTERVAL = 15  # MEXC 系列 WS 通常 20s timeout


class OurbitFuturesSource:
    def __init__(self, watch_symbols: set[str]):
        self.watch_symbols = watch_symbols  # e.g. {'SPCX_USDT'}
        self._tickers: dict[str, dict] = {}
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def wait_ready(self, timeout: float = 10.0):
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[ourbit-ws] wait_ready timeout")

    def snapshot(self, symbol: str) -> Optional[dict]:
        return self._tickers.get(symbol)

    async def _run(self):
        backoff = 1
        while True:
            try:
                logger.info(f"[ourbit-ws] 連線 {WS_URL}，預計訂閱 {len(self.watch_symbols)} symbols")
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    for sym in sorted(self.watch_symbols):
                        await ws.send(json.dumps({
                            "method": "sub.ticker",
                            "param": {"symbol": sym},
                        }))
                        await asyncio.sleep(0.05)
                    logger.info(f"[ourbit-ws] 已訂閱 {len(self.watch_symbols)} symbols")

                    async def _ping():
                        while True:
                            await asyncio.sleep(PING_INTERVAL)
                            try:
                                await ws.send(json.dumps({"method": "ping"}))
                            except Exception:
                                break
                    ping_task = asyncio.create_task(_ping())
                    backoff = 1

                    try:
                        async for raw in ws:
                            try:
                                d = json.loads(raw)
                            except Exception:
                                continue
                            channel = d.get("channel") or ""
                            # 跳過 subscribe ack（data 是字串 "success"）與 pong
                            if channel.startswith("rs.") or channel == "pong":
                                continue
                            data = d.get("data")
                            if not isinstance(data, dict):
                                continue
                            sym = d.get("symbol") or data.get("symbol")
                            if not sym:
                                continue
                            cur = self._tickers.setdefault(sym, {})
                            cur["last"] = _f(data.get("lastPrice")) or cur.get("last")
                            cur["bid"] = _f(data.get("bid1")) or cur.get("bid")
                            cur["ask"] = _f(data.get("ask1")) or cur.get("ask")
                            cur["mark"] = _f(data.get("fairPrice")) or _f(data.get("lastPrice")) or cur.get("mark")
                            cur["index"] = _f(data.get("indexPrice"))
                            cur["funding"] = _f(data.get("fundingRate"))
                            cur["usdt_vol"] = _f(data.get("amount24"))
                            cur["ts"] = time.time()
                            self._ready.set()
                    finally:
                        ping_task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[ourbit-ws] 中斷 {backoff}s 後重連: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
