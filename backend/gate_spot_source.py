"""Gate.io spot v4 WS — spot.book_ticker + spot.tickers

wss://api.gateio.ws/ws/v4/，無需 API key。

跟 gate_source.py（futures）格式幾乎相同，只是 channel 前綴 spot.* 而非 futures.*，
result 結構也稍有不同（spot.book_ticker 用 b/a 同 futures；spot.tickers 用
currency_pair / last / highest_bid / lowest_ask / quote_volume）。
"""

import asyncio
import json
import logging
import time
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://api.gateio.ws/ws/v4/"


class GateSpotSource:
    def __init__(self, watch_symbols: set[str]):
        self.watch_symbols = watch_symbols  # e.g. {'SPCX_USDT', ...}
        self._tickers: dict[str, dict] = {}
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def wait_ready(self, timeout: float = 10.0):
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[gate-spot] wait_ready timeout")

    def snapshot(self, symbol: str) -> Optional[dict]:
        return self._tickers.get(symbol)

    async def _run(self):
        backoff = 1
        while True:
            try:
                logger.info(f"[gate-spot] 連線 {WS_URL}，預計訂閱 {len(self.watch_symbols)} symbols")
                async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=15) as ws:
                    now = int(time.time())
                    syms = sorted(self.watch_symbols)
                    await ws.send(json.dumps({
                        "time": now, "channel": "spot.book_ticker",
                        "event": "subscribe", "payload": syms,
                    }))
                    await ws.send(json.dumps({
                        "time": now, "channel": "spot.tickers",
                        "event": "subscribe", "payload": syms,
                    }))
                    logger.info(f"[gate-spot] 已訂閱 {len(syms)} symbols")
                    backoff = 1

                    async for raw in ws:
                        try:
                            d = json.loads(raw)
                        except Exception:
                            continue
                        if d.get("event") != "update":
                            continue
                        channel = d.get("channel")
                        result = d.get("result")
                        if not result:
                            continue
                        if channel == "spot.book_ticker":
                            sym = result.get("s")
                            if not sym:
                                continue
                            cur = self._tickers.setdefault(sym, {})
                            cur["bid"] = _f(result.get("b"))
                            cur["ask"] = _f(result.get("a"))
                            cur["ts"] = time.time()
                        elif channel == "spot.tickers":
                            items = result if isinstance(result, list) else [result]
                            for item in items:
                                sym = item.get("currency_pair")
                                if not sym:
                                    continue
                                cur = self._tickers.setdefault(sym, {})
                                cur["last"] = _f(item.get("last"))
                                cur["mark"] = cur.get("last")  # spot 無 mark
                                cur["usdt_vol"] = _f(item.get("quote_volume"))
                                cur["ts"] = time.time()
                        self._ready.set()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[gate-spot] 中斷 {backoff}s 後重連: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
