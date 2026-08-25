"""Aster futures WS — 雙連線 pattern（markPrice array + bookTicker）

- markPrice/funding: wss://fstream.asterdex.com/market/stream?streams=!markPrice@arr@1s
- bookTicker:       wss://fstream.asterdex.com/ws + SUBSCRIBE
"""

import asyncio
import json
import logging
import time
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

WS_MARK_URL = "wss://fstream.asterdex.com/market/stream?streams=!markPrice@arr@1s"
WS_BOOK_URL = "wss://fstream.asterdex.com/ws"


class AsterFuturesSource:
    def __init__(self, watch_symbols: set[str]):
        self.watch_symbols = {s.upper() for s in watch_symbols}
        self._mark: dict[str, dict] = {}
        self._book: dict[str, dict] = {}
        self._mark_task: Optional[asyncio.Task] = None
        self._book_task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    async def start(self):
        self._mark_task = asyncio.create_task(self._run_mark())
        self._book_task = asyncio.create_task(self._run_book())

    async def wait_ready(self, timeout: float = 10.0):
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[aster-ws] wait_ready timeout")

    def snapshot(self, symbol: str) -> Optional[dict]:
        sym = symbol.upper()
        m = self._mark.get(sym, {})
        b = self._book.get(sym, {})
        if not m and not b:
            return None
        return {
            "mark": m.get("mark") or b.get("last"),
            "last": b.get("last"),
            "bid": b.get("bid"),
            "ask": b.get("ask"),
            "index": m.get("index"),
            "funding": m.get("funding_rate"),
            "next_funding_ms": m.get("funding_time_ms"),
            "usdt_vol": b.get("usdt_vol"),
        }

    async def _run_mark(self):
        backoff = 1
        while True:
            try:
                logger.info(f"[aster-ws] markPrice 連線 {WS_MARK_URL}")
                async with websockets.connect(WS_MARK_URL, ping_interval=20, ping_timeout=15) as ws:
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        data = msg.get("data") if isinstance(msg, dict) else None
                        if not isinstance(data, list):
                            continue
                        ts = time.time()
                        for item in data:
                            if not isinstance(item, dict):
                                continue
                            s = item.get("s") or ""
                            if s not in self.watch_symbols:
                                continue
                            try:
                                self._mark[s] = {
                                    "mark": _f(item.get("p")),
                                    "index": _f(item.get("i")),
                                    "funding_rate": _f(item.get("r")),
                                    "funding_time_ms": int(item.get("T")) if item.get("T") else None,
                                    "ts": ts,
                                }
                            except (TypeError, ValueError):
                                continue
                        if self._mark:
                            self._ready.set()
                            backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[aster-ws] markPrice 中斷 {backoff}s 後重連: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _run_book(self):
        backoff = 1
        while True:
            try:
                streams = []
                for s in sorted(self.watch_symbols):
                    sl = s.lower()
                    streams += [f"{sl}@bookTicker", f"{sl}@ticker"]
                logger.info(f"[aster-ws] bookTicker 連線，訂閱 {len(streams)} streams")
                async with websockets.connect(WS_BOOK_URL, ping_interval=20, ping_timeout=15) as ws:
                    await ws.send(json.dumps({"method": "SUBSCRIBE", "params": streams, "id": 1}))
                    backoff = 1
                    async for raw in ws:
                        try:
                            d = json.loads(raw)
                        except Exception:
                            continue
                        if "id" in d and d.get("result") is None:
                            continue
                        event = d.get("e")
                        sym = (d.get("s") or "").upper()
                        if not sym:
                            continue
                        cur = self._book.setdefault(sym, {})
                        if event == "bookTicker":
                            cur["bid"] = _f(d.get("b"))
                            cur["ask"] = _f(d.get("a"))
                        elif event == "24hrTicker":
                            cur["last"] = _f(d.get("c"))
                            cur["usdt_vol"] = _f(d.get("q"))
                        cur["ts"] = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[aster-ws] bookTicker 中斷 {backoff}s 後重連: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
