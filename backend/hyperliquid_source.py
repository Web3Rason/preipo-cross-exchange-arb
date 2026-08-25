"""Hyperliquid HIP-3 multi-perpDEX 即時報價來源（純 WS）

- meta：啟動時 REST 拉一次（szDecimals/maxLeverage 等不變化，不算輪詢）
- l2Book：WS 訂閱 → bid/ask top-of-book
- activeAssetCtx：WS 訂閱 → markPx/funding/oraclePx/openInterest/dayNtlVlm

無任何 REST polling。
"""

import asyncio
import json
import logging
import time
from typing import Optional

import aiohttp
import websockets

logger = logging.getLogger(__name__)

INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"


class HyperliquidSource:
    """負責一個 HIP-3 perpDEX（如 xyz、vntl）的即時報價快取"""

    def __init__(self, dex: str, watch_symbols: set[str]):
        self.dex = dex
        self.watch_symbols = watch_symbols  # 'xyz:SPCX' / 'vntl:SPACEX' 等全名

        self._ctx: dict[str, dict] = {}   # markPx/funding/oraclePx/OI/dayNtlVlm（WS push）
        self._meta: dict[str, dict] = {}  # szDecimals/maxLeverage（一次性 REST）
        self._book: dict[str, dict] = {}  # bid/ask/ts（WS push）

        self._ws_task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    async def start(self):
        await self._fetch_meta_once()
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def wait_ready(self, timeout: float = 10.0):
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[hl:{self.dex}] wait_ready timeout，繼續以部分資料運作")

    def snapshot(self, symbol: str) -> Optional[dict]:
        if symbol not in self._meta and symbol not in self._ctx:
            return None
        return {
            "meta": self._meta.get(symbol, {}),
            "ctx": self._ctx.get(symbol, {}),
            "book": self._book.get(symbol, {}),
        }

    # ──── 啟動時一次性 REST：拉 meta（szDecimals/maxLeverage 等）────
    async def _fetch_meta_once(self):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    INFO_URL,
                    json={"type": "meta", "dex": self.dex},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
            universe = data.get("universe", [])
            for u in universe:
                name = u["name"]
                self._meta[name] = {
                    "sz_decimals": u.get("szDecimals"),
                    "max_leverage": u.get("maxLeverage"),
                    "is_delisted": u.get("isDelisted", False),
                }
            logger.info(f"[hl:{self.dex}] meta 載入 {len(self._meta)} 個 asset（一次性 REST）")
        except Exception as e:
            logger.warning(f"[hl:{self.dex}] meta 載入失敗: {e}")

    # ──── WS 迴圈：l2Book + activeAssetCtx 兩個 channel 同一條連線 ────
    async def _ws_loop(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=15) as ws:
                    # 對每個 symbol 訂閱兩個 channel
                    for sym in self.watch_symbols:
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "l2Book", "coin": sym},
                        }))
                        await asyncio.sleep(0.04)
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "activeAssetCtx", "coin": sym},
                        }))
                        await asyncio.sleep(0.04)
                    logger.info(
                        f"[hl:{self.dex}] WS 訂閱 {len(self.watch_symbols)} symbol × (l2Book + activeAssetCtx)"
                    )
                    backoff = 1

                    async for msg in ws:
                        try:
                            data = json.loads(msg)
                        except Exception:
                            continue
                        channel = data.get("channel")
                        body = data.get("data") or {}

                        if channel == "l2Book":
                            coin = body.get("coin")
                            levels = body.get("levels", [[], []])
                            bids, asks = levels[0], levels[1]
                            bid = _f(bids[0]["px"]) if bids else None
                            ask = _f(asks[0]["px"]) if asks else None
                            self._book[coin] = {"bid": bid, "ask": ask, "ts": time.time()}
                            self._ready.set()

                        elif channel == "activeAssetCtx":
                            coin = body.get("coin")
                            ctx = body.get("ctx") or {}
                            if coin:
                                self._ctx[coin] = {
                                    "mark_px": _f(ctx.get("markPx")),
                                    "oracle_px": _f(ctx.get("oraclePx")),
                                    "mid_px": _f(ctx.get("midPx")),
                                    "funding": _f(ctx.get("funding")),
                                    "open_interest": _f(ctx.get("openInterest")),
                                    "day_ntl_vlm": _f(ctx.get("dayNtlVlm")),
                                    "prev_day_px": _f(ctx.get("prevDayPx")),
                                }
                                self._ready.set()
            except Exception as e:
                logger.warning(f"[hl:{self.dex}] WS 中斷: {e}，{backoff}s 後重連")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None
