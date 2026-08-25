"""Ourbit spot WS — MEXC V3 spot JSON 格式（Ourbit 還沒升級 protobuf）

實測（2026-05-24）：wbs.ourbit.com/ws 收
  {'method':'SUBSCRIPTION','params':['spot@public.bookTicker.v3.api@SPAXUSDT']}
正常推 bookTicker（a/b/A/B 欄位）；MEXC 同樣訂閱回 Blocked。

跟 mexc_source.py（futures）的 sub.ticker 格式完全不同 — 這個是 V3 spot 的
SUBSCRIPTION 格式（capital S）。也跟 mexc_spot_source.py 不一樣，因為 Ourbit
還能用 JSON、MEXC 必須 protobuf。
"""

import asyncio
import json
import logging
import time
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://wbs.ourbit.com/ws"
PING_INTERVAL = 25  # MEXC V3 spec 30s timeout


class OurbitSpotSource:
    def __init__(self, watch_symbols: set[str]):
        self.watch_symbols = watch_symbols  # e.g. {'SPAXUSDT', ...}
        self._tickers: dict[str, dict] = {}
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def wait_ready(self, timeout: float = 10.0):
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[ourbit-spot] wait_ready timeout")

    def snapshot(self, symbol: str) -> Optional[dict]:
        return self._tickers.get(symbol)

    async def _run(self):
        backoff = 1
        while True:
            try:
                logger.info(f"[ourbit-spot] 連線 {WS_URL}，預計訂閱 {len(self.watch_symbols)} symbols")
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    # bookTicker 拿 bid/ask；miniTicker 拿 last/vol
                    for sym in sorted(self.watch_symbols):
                        await ws.send(json.dumps({
                            "method": "SUBSCRIPTION",
                            "params": [f"spot@public.bookTicker.v3.api@{sym}"],
                        }))
                        await ws.send(json.dumps({
                            "method": "SUBSCRIPTION",
                            "params": [f"spot@public.miniTicker.v3.api@UTC+8@{sym}"],
                        }))
                        await asyncio.sleep(0.05)
                    logger.info(f"[ourbit-spot] 已送出訂閱")

                    async def _ping():
                        while True:
                            await asyncio.sleep(PING_INTERVAL)
                            try:
                                await ws.send(json.dumps({"method": "PING"}))
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
                            # subscribe ack / pong
                            if "msg" in d and "c" not in d:
                                continue
                            channel = d.get("c") or ""
                            data = d.get("d") or {}
                            sym = d.get("s") or ""
                            if not channel or not sym or not isinstance(data, dict):
                                continue
                            cur = self._tickers.setdefault(sym, {})
                            if "bookTicker" in channel:
                                cur["bid"] = _f(data.get("b"))
                                cur["ask"] = _f(data.get("a"))
                            elif "miniTicker" in channel:
                                cur["last"] = _f(data.get("p")) or cur.get("last")
                                cur["mark"] = cur.get("last")  # spot 無 mark
                                cur["usdt_vol"] = _f(data.get("qv")) or cur.get("usdt_vol")
                            cur["ts"] = time.time()
                            self._ready.set()
                    finally:
                        ping_task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[ourbit-spot] 中斷 {backoff}s 後重連: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
