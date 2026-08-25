"""PreStocks Solana SPL token price source

定價來源（2026-05-24 確認）：PreStocks 官方公開 API
  GET https://prestocks.com/api/<symbol>
  → 直接回 tokenPrice / markPrice / impliedValuation / markValuation / supply
  → 不需自己換算 pricing_basis（每個 mint 的 SPV 規模不同、conversion factor 不一致）

歷史背景：
  上一版用 DexScreener tokenPrice + 自己套 subscription_proxy / valuation_per_billion 公式，
  小幣（NEURALINK / KALSHI / POLYMARKET）算出來與官方 implied 差 4-27 倍。改打官方 API
  就不用維護每個 mint 的 subscription_price / initial_valuation_usd 等假設參數。

push 機制（純 event-driven，非 polling）：
  1. Solana mainnet WS `accountSubscribe` 訂閱 7 個 Meteora DLMM LB_PAIR account
  2. 收到 account 變動 push（=有 swap）→ 觸發一次 prestocks.com REST fetch
  3. 啟動時一次性 fetch 拿 initial state，之後純靠 WS trigger
"""

import asyncio
import json
import logging
import time
from typing import Optional

import aiohttp
import websockets

logger = logging.getLogger(__name__)

SOLANA_WS = "wss://api.mainnet-beta.solana.com"
PRESTOCKS_API = "https://prestocks.com/api"


class PreStocksSource:
    def __init__(self, pool_map: dict[str, str]):
        """pool_map: { 'SPACEX': '<pair_address>', 'OPENAI': '<pair_address>', ... }
        symbol → Meteora LB_PAIR address。pair_address 仍然用來訂閱鏈上事件作為「該幣有交易」訊號。
        """
        self.pool_map = pool_map
        self.pair_to_symbol = {v: k for k, v in pool_map.items()}
        self._tickers: dict[str, dict] = {}  # symbol → snapshot dict
        self._sess: Optional[aiohttp.ClientSession] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    async def start(self):
        self._sess = aiohttp.ClientSession()
        await asyncio.gather(*[
            self._fetch_api(sym) for sym in self.pool_map
        ])
        self._ready.set()
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def wait_ready(self, timeout: float = 15.0):
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("[prestocks] wait_ready timeout")

    def snapshot(self, symbol: str) -> Optional[dict]:
        return self._tickers.get(symbol)

    async def _fetch_api(self, sym: str):
        """打 prestocks.com 官方 API 拿即時 tokenPrice + impliedValuation"""
        url = f"{PRESTOCKS_API}/{sym.lower()}"
        try:
            async with self._sess.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (5032_PreIPO monitor)"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                data = await r.json()
        except Exception as e:
            logger.warning(f"[prestocks] {sym} API fetch failed: {e}")
            return
        token_px = _f(data.get("tokenPrice"))
        mark_px = _f(data.get("markPrice"))
        implied_val = _f(data.get("impliedValuation"))
        mark_val = _f(data.get("markValuation"))
        supply = _f(data.get("supply"))
        if token_px is None and mark_px is None:
            logger.warning(f"[prestocks] {sym} API 無 tokenPrice/markPrice，原始={data}")
            return
        self._tickers[sym] = {
            "last": token_px,
            "mark": mark_px or token_px,         # markPrice 是 oracle 計算的權威價，fallback tokenPrice
            "bid": token_px,                     # PreStocks API 無 bid/ask，DEX 流動性都用單一價
            "ask": token_px,
            "implied_valuation": implied_val,    # ★ 官方權威估值，aggregator 直接用、跳過 normalizer
            "mark_valuation": mark_val,
            "supply": supply,
            "usdt_vol": None,                    # PreStocks API 沒提供 24h vol，要的話另外抓 DexScreener
            "liquidity_usd": None,
            "ts": time.time(),
        }

    async def _ws_loop(self):
        """訂閱 7 個 LB_PAIR account 變動，收到 push 就觸發 fetch"""
        backoff = 1
        sub_id_to_pair: dict[int, str] = {}
        req_id_to_pair: dict[int, str] = {}

        while True:
            try:
                logger.info(f"[prestocks-ws] 連線 {SOLANA_WS}，訂閱 {len(self.pool_map)} 個 Meteora LB_PAIR（trigger 用）")
                async with websockets.connect(SOLANA_WS, ping_interval=20, ping_timeout=15) as ws:
                    for i, (sym, pair_addr) in enumerate(self.pool_map.items()):
                        req_id = i + 1
                        req_id_to_pair[req_id] = pair_addr
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "method": "accountSubscribe",
                            "params": [pair_addr, {"encoding": "base64", "commitment": "confirmed"}],
                        }))
                        await asyncio.sleep(0.05)
                    backoff = 1

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        if "result" in msg and "id" in msg and isinstance(msg["result"], int):
                            sub_id = msg["result"]
                            pair = req_id_to_pair.get(msg["id"])
                            if pair:
                                sub_id_to_pair[sub_id] = pair
                            continue
                        if msg.get("method") == "accountNotification":
                            sub_id = (msg.get("params") or {}).get("subscription")
                            pair = sub_id_to_pair.get(sub_id)
                            if not pair:
                                continue
                            sym = self.pair_to_symbol.get(pair)
                            if sym:
                                asyncio.create_task(self._fetch_api(sym))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[prestocks-ws] 中斷 {backoff}s 後重連: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


def _f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
