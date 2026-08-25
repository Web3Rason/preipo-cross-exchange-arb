"""BingX swap — WS（bookTicker/ticker/markPrice）+ REST funding polling

WS: wss://open-api-swap.bingx.com/swap-market (GZIP 壓縮)
REST: /openApi/swap/v2/quote/premiumIndex 拉 funding rate
  → 規則破例：BingX 沒公開 WS funding stream（實測 @premiumIndex / @fundingRate 都 code=80015 不支援）
  → 走 REST polling
  → funding rate 8 小時結算一次，30s polling 足夠
"""

import asyncio
import gzip
import json
import logging
import time
import uuid
from typing import Optional

import aiohttp
import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://open-api-swap.bingx.com/swap-market"
REST_PREMIUM_URL = "https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex"
FUNDING_POLL_INTERVAL = 30  # 秒


class BingxSwapSource:
    def __init__(self, watch_symbols: set[str]):
        self.watch_symbols = watch_symbols  # e.g. {'NCSKSPACEXV2USD-USDT', ...}
        self._tickers: dict[str, dict] = {}
        self._task: Optional[asyncio.Task] = None
        self._funding_task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    async def start(self):
        self._task = asyncio.create_task(self._run())
        self._funding_task = asyncio.create_task(self._funding_loop())

    async def wait_ready(self, timeout: float = 10.0):
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[bingx-ws] wait_ready timeout")

    def snapshot(self, symbol: str) -> Optional[dict]:
        return self._tickers.get(symbol)

    async def _run(self):
        backoff = 1
        while True:
            try:
                logger.info(f"[bingx-ws] 連線 {WS_URL}，預計訂閱 {len(self.watch_symbols)} symbols")
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    for sym in sorted(self.watch_symbols):
                        await ws.send(json.dumps({
                            "id": str(uuid.uuid4()),
                            "reqType": "sub",
                            "dataType": f"{sym}@bookTicker",
                        }))
                        await asyncio.sleep(0.05)
                        await ws.send(json.dumps({
                            "id": str(uuid.uuid4()),
                            "reqType": "sub",
                            "dataType": f"{sym}@ticker",
                        }))
                        await asyncio.sleep(0.05)
                    logger.info(f"[bingx-ws] 已送出訂閱請求")
                    backoff = 1

                    async for raw in ws:
                        # BingX 推 GZIP binary
                        if isinstance(raw, bytes):
                            try:
                                text = gzip.decompress(raw).decode("utf-8")
                            except Exception:
                                continue
                        else:
                            text = raw
                        # BingX 用 "Ping" 字串 keepalive，回 "Pong"
                        if text == "Ping":
                            try:
                                await ws.send("Pong")
                            except Exception:
                                pass
                            continue
                        try:
                            d = json.loads(text)
                        except Exception:
                            continue
                        if d.get("code") is not None and d.get("data") is None:
                            # subscribe ack
                            continue
                        dtype = d.get("dataType") or ""
                        data = d.get("data") or {}
                        if not dtype or not data:
                            continue
                        sym = dtype.split("@", 1)[0]
                        cur = self._tickers.setdefault(sym, {})
                        if dtype.endswith("@bookTicker"):
                            cur["bid"] = _f(data.get("b"))
                            cur["ask"] = _f(data.get("a"))
                        elif dtype.endswith("@ticker"):
                            cur["last"] = _f(data.get("c") or data.get("lastPrice"))
                            cur["mark"] = cur.get("last") or cur.get("mark")
                            cur["usdt_vol"] = _f(data.get("q") or data.get("quoteVolume"))
                        cur["ts"] = time.time()
                        self._ready.set()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[bingx-ws] 中斷 {backoff}s 後重連: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


    async def _funding_loop(self):
        """BingX 沒 WS funding stream，30s 拉一次 REST premiumIndex（funding 8h 結算一次，足夠）"""
        async with aiohttp.ClientSession() as sess:
            while True:
                try:
                    for sym in self.watch_symbols:
                        async with sess.get(
                            REST_PREMIUM_URL,
                            params={"symbol": sym},
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            j = await resp.json()
                        data = j.get("data") or {}
                        if isinstance(data, dict):
                            cur = self._tickers.setdefault(sym, {})
                            cur["funding"] = _f(data.get("lastFundingRate"))
                            cur["next_funding_ms"] = int(data.get("nextFundingTime")) if data.get("nextFundingTime") else None
                            mark_px = _f(data.get("markPrice"))
                            if mark_px is not None:
                                cur["mark"] = mark_px
                except Exception as e:
                    logger.warning(f"[bingx] funding REST 失敗: {e}")
                await asyncio.sleep(FUNDING_POLL_INTERVAL)


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
