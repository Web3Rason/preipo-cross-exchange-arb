"""Gate.io futures USDT WS — book_ticker + tickers

wss://fx-ws.gateio.ws/v4/ws/usdt，無需 API key。
SPACEX_USDT / OPENAI_USDT / ANTHROPIC_USDT 為 valuation_per_billion_usd。
"""

import asyncio
import json
import logging
import time
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://fx-ws.gateio.ws/v4/ws/usdt"


class GateFuturesSource:
    def __init__(self, watch_symbols: set[str]):
        self.watch_symbols = watch_symbols  # e.g. {'SPACEX_USDT', ...}
        self._tickers: dict[str, dict] = {}
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def wait_ready(self, timeout: float = 10.0):
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[gate-ws] wait_ready timeout")

    def snapshot(self, symbol: str) -> Optional[dict]:
        return self._tickers.get(symbol)

    async def _run(self):
        backoff = 1
        while True:
            try:
                logger.info(f"[gate-ws] 連線 {WS_URL}，預計訂閱 {len(self.watch_symbols)} symbols")
                async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=15) as ws:
                    now = int(time.time())
                    syms = sorted(self.watch_symbols)
                    await ws.send(json.dumps({
                        "time": now, "channel": "futures.book_ticker",
                        "event": "subscribe", "payload": syms,
                    }))
                    await ws.send(json.dumps({
                        "time": now, "channel": "futures.tickers",
                        "event": "subscribe", "payload": syms,
                    }))
                    logger.info(f"[gate-ws] 已訂閱 {len(syms)} symbols (book_ticker + tickers)")
                    backoff = 1

                    async for raw in ws:
                        try:
                            d = json.loads(raw)
                        except Exception:
                            continue
                        event = d.get("event")
                        if event != "update":
                            continue
                        channel = d.get("channel")
                        result = d.get("result")
                        if not result:
                            continue
                        if channel == "futures.book_ticker":
                            sym = result.get("s") or result.get("contract")
                            if not sym:
                                continue
                            cur = self._tickers.setdefault(sym, {})
                            cur["bid"] = _f(result.get("b"))
                            cur["ask"] = _f(result.get("a"))
                            cur["ts"] = time.time()
                        elif channel == "futures.tickers":
                            # result 可能是 list
                            items = result if isinstance(result, list) else [result]
                            for item in items:
                                sym = item.get("contract")
                                if not sym:
                                    continue
                                cur = self._tickers.setdefault(sym, {})
                                cur["last"] = _f(item.get("last"))
                                cur["mark"] = _f(item.get("mark_price")) or _f(item.get("last"))
                                cur["usdt_vol"] = _f(item.get("volume_24h_quote"))
                                cur["funding"] = _f(item.get("funding_rate"))
                                cur["ts"] = time.time()
                        self._ready.set()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[gate-ws] 中斷 {backoff}s 後重連: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
