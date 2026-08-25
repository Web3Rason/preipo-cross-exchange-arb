"""OKX V5 public WS — tickers channel（單一連線、多 symbol）

wss://ws.okx.com:8443/ws/v5/public，無需 API key。
SPACEX-USDT-SWAP / OPENAI-USDT-SWAP / ANTHROPIC-USDT-SWAP 為 valuation_per_billion_usd 制。
"""

import asyncio
import json
import logging
import time
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
PING_INTERVAL = 25


class OkxSwapSource:
    def __init__(self, watch_symbols: set[str]):
        self.watch_symbols = watch_symbols
        self._tickers: dict[str, dict] = {}
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def wait_ready(self, timeout: float = 10.0):
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[okx-ws] wait_ready timeout")

    def snapshot(self, symbol: str) -> Optional[dict]:
        return self._tickers.get(symbol)

    async def _run(self):
        backoff = 1
        while True:
            try:
                logger.info(f"[okx-ws] 連線 {WS_URL}，預計訂閱 {len(self.watch_symbols)} symbols")
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    args = []
                    for s in sorted(self.watch_symbols):
                        args.append({"channel": "tickers", "instId": s})
                        args.append({"channel": "mark-price", "instId": s})
                        args.append({"channel": "funding-rate", "instId": s})
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    logger.info(f"[okx-ws] 已送出 {len(args)} 個訂閱請求（tickers + mark-price + funding-rate）")

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
                                    logger.warning(f"[okx-ws] error: {msg}")
                                continue
                            arg = msg.get("arg") or {}
                            channel = arg.get("channel")
                            for d in msg.get("data") or []:
                                inst = d.get("instId")
                                if not inst:
                                    continue
                                cur = self._tickers.setdefault(inst, {})
                                if channel == "tickers":
                                    cur["last"] = _f(d.get("last"))
                                    cur["bid"] = _f(d.get("bidPx"))
                                    cur["ask"] = _f(d.get("askPx"))
                                    cur["usdt_vol"] = _f(d.get("volCcy24h"))
                                    if not cur.get("mark"):
                                        cur["mark"] = _f(d.get("last"))
                                elif channel == "mark-price":
                                    cur["mark"] = _f(d.get("markPx"))
                                elif channel == "funding-rate":
                                    cur["funding"] = _f(d.get("fundingRate"))
                                    cur["next_funding_ms"] = int(d.get("fundingTime")) if d.get("fundingTime") else None
                                cur["ts"] = time.time()
                            self._ready.set()
                            backoff = 1
                    finally:
                        ping_task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[okx-ws] 中斷 {backoff}s 後重連: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
