"""Bitget V2 spot ticker — WebSocket 推送（單一連線、mass subscribe）

訂閱 channel="ticker", instType="SPOT" 拿即時 last/bid/ask/24h vol。
Bitget V2 spot 公開 WS：wss://ws.bitget.com/v2/ws/public，無需 API key。

註：preSPAX / preOPAI 是 Republic SPV mirror token，不是 1:1 對應 underlying share。
正規化請參考 normalizer.py 的 subscription_proxy basis。
"""

import asyncio
import json
import logging
import time
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://ws.bitget.com/v2/ws/public"
PING_INTERVAL = 25  # Bitget 要求 < 30s


class BitgetSpotSource:
    """單一 WS 連線訂閱多個 spot symbol 的 ticker"""

    def __init__(self, watch_symbols: set[str]):
        self.watch_symbols = watch_symbols
        self._tickers: dict[str, dict] = {}  # symbol → {last, bid, ask, vol, ts}
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def wait_ready(self, timeout: float = 10.0):
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[bitget-ws] wait_ready timeout")

    def snapshot(self, symbol: str) -> Optional[dict]:
        return self._tickers.get(symbol)

    async def _run(self):
        backoff = 1
        while True:
            try:
                logger.info(f"[bitget-ws] 連線 {WS_URL}，預計訂閱 {len(self.watch_symbols)} symbols")
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    args = [
                        {"instType": "SPOT", "channel": "ticker", "instId": s}
                        for s in sorted(self.watch_symbols)
                    ]
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    logger.info(f"[bitget-ws] 已送出 {len(args)} 個 ticker 訂閱請求")

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
                                    logger.warning(f"[bitget-ws] error: {msg}")
                                continue
                            arg = msg.get("arg", {})
                            data = msg.get("data")
                            if arg.get("channel") != "ticker" or not data:
                                continue
                            for d in data:
                                sym = d.get("instId") or d.get("symbol")
                                if not sym:
                                    continue
                                self._tickers[sym] = {
                                    "last": _f(d.get("lastPr")),
                                    "bid": _f(d.get("bidPr")),
                                    "ask": _f(d.get("askPr")),
                                    "usdt_vol": _f(d.get("usdtVolume")) or _f(d.get("quoteVolume")),
                                    "ts": time.time(),
                                }
                            self._ready.set()
                            backoff = 1
                    finally:
                        ping_task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[bitget-ws] 連線中斷，{backoff}s 後重連: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
