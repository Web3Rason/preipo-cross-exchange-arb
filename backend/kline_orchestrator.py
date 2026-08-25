"""K 線 backfill + incremental updater — 全 9 家"""

import asyncio
import json
import logging
import time
from pathlib import Path

import aiohttp

import kline_fetchers as F
import kline_storage as ST
from normalizer import to_implied

logger = logging.getLogger(__name__)

ASSETS_FILE = Path(__file__).parent / "data" / "preipo_assets.json"
MAX_BACKFILL_DAYS = 30
INTERVALS = ["1m", "1h"]
INCREMENTAL_INTERVAL = 60


def _now_ms():
    return int(time.time() * 1000)


def _compute_implied(rows, pricing_basis, total_supply, multiplier, extra):
    # api_direct（PreStocks）: 用 extra['prestocks_factor'] = impliedValuation/tokenPrice 套到歷史 close
    # 假設 SPV 規模在 backfill 區間內未變動（短期觀察成立；長期 SPV rebalance 會漂移）
    if pricing_basis == "api_direct":
        factor = (extra or {}).get("prestocks_factor")
        for r in rows:
            c = r.get("c")
            r["implied_valuation"] = c * factor if (c and factor) else None
        return rows
    for r in rows:
        _, val = to_implied(r.get("c"), pricing_basis, total_supply, multiplier, extra)
        r["implied_valuation"] = val
    return rows


class KlineOrchestrator:
    def __init__(self):
        with open(ASSETS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        self.assets = {k: v for k, v in data.items() if not k.startswith("_")}
        self._backfill_task = None
        self._incr_task = None

    async def start(self):
        await ST.init_db()
        # 啟動時刷新 PreStocks 每個 mint 的 conversion factor（impliedValuation/tokenPrice）
        await self._refresh_prestocks_factors()
        # reprice 所有歷史 row（PreStocks 改 api_direct 後，舊 row 的 implied_val 是錯的，重算）
        await self._reprice_all()
        self._backfill_task = asyncio.create_task(self._backfill_all())
        self._incr_task = asyncio.create_task(self._incremental_loop())

    async def _refresh_prestocks_factors(self):
        """打 prestocks.com/api/<symbol> 拿每個 mint 的 impliedValuation/tokenPrice = factor，
        寫回 self.assets 對應 contract 的 'prestocks_factor' 欄位（in-memory）。
        Why: 歷史 K 沒法呼叫實時 API，用 startup 當下的 factor 對 close 做 scaling 即可"""
        prestocks_unders = [u for u, info in self.assets.items()
                            if (info.get("exchanges", {}).get("prestocks") or [])]
        if not prestocks_unders:
            return
        async with aiohttp.ClientSession() as sess:
            for und in prestocks_unders:
                try:
                    async with sess.get(
                        f"https://prestocks.com/api/{und.lower()}",
                        headers={"User-Agent": "Mozilla/5.0 (5032_PreIPO)"},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as r:
                        d = await r.json()
                    tp = d.get("tokenPrice")
                    iv = d.get("impliedValuation")
                    factor = (iv / tp) if (tp and iv) else None
                    if factor:
                        for c in self.assets[und]["exchanges"]["prestocks"]:
                            c["prestocks_factor"] = factor
                        logger.info(f"[kline] PreStocks {und} factor = ${factor/1e9:.2f}B/token")
                except Exception as e:
                    logger.warning(f"[kline] PreStocks {und} factor 抓取失敗: {e}")

    async def _reprice_all(self):
        """用當前 JSON 設定重算所有歷史 K 的 implied_valuation 並 upsert 回去"""
        total = 0
        for und, info in self.assets.items():
            total_supply = info.get("total_supply_shares", 0)
            for ex, contracts in info.get("exchanges", {}).items():
                for c in contracts:
                    key = ST.make_key(ex, c.get("deployer"), c["symbol"], c.get("settlement", "perpetual"))
                    pricing_basis = c["pricing_basis"]
                    multiplier = c.get("multiplier", 1) or 1
                    extra = {"subscription_price": c.get("subscription_price"),
                             "initial_valuation_usd": c.get("initial_valuation_usd"),
                             "prestocks_factor": c.get("prestocks_factor")}
                    for iv in INTERVALS:
                        rows = await ST.get_klines(key, iv, from_ts=0, limit=200000)
                        if not rows: continue
                        for r in rows:
                            _, val = to_implied(r.get("c"), pricing_basis, total_supply, multiplier, extra)
                            r["implied_valuation"] = val
                        n = await ST.upsert(key, und, iv, rows)
                        total += n
        if total:
            logger.info(f"[kline] reprice 完成，更新 {total} 個 row 的 implied_valuation")

    async def _backfill_all(self):
        async with aiohttp.ClientSession() as sess:
            for und, info in self.assets.items():
                total_supply = info.get("total_supply_shares", 0)
                for ex, contracts in info.get("exchanges", {}).items():
                    for c in contracts:
                        for iv in INTERVALS:
                            try:
                                await self._backfill_contract(sess, und, ex, c, iv, total_supply)
                            except Exception as e:
                                logger.warning(f"[kline] backfill {ex}/{c.get('symbol')} {iv} 失敗: {e}")
        logger.info("[kline] backfill all done")

    async def _backfill_contract(self, sess, underlying, exchange, contract, interval, total_supply):
        key = ST.make_key(exchange, contract.get("deployer"), contract["symbol"], contract.get("settlement", "perpetual"))
        latest = await ST.get_latest_ts(key, interval)
        if latest:
            start_ms = latest + 1
        else:
            start_ms = _now_ms() - MAX_BACKFILL_DAYS * 86400 * 1000

        rows = await self._fetch_dispatch(sess, exchange, contract, interval, start_ms)
        if not rows:
            return

        pricing_basis = contract["pricing_basis"]
        multiplier = contract.get("multiplier", 1) or 1
        extra = {"subscription_price": contract.get("subscription_price"),
                 "initial_valuation_usd": contract.get("initial_valuation_usd"),
                 "prestocks_factor": contract.get("prestocks_factor")}
        _compute_implied(rows, pricing_basis, total_supply, multiplier, extra)
        n = await ST.upsert(key, underlying, interval, rows)
        if n > 50:  # 只 log 大批 backfill
            logger.info(f"[kline] {key} {interval}: +{n} rows")

    async def _fetch_dispatch(self, sess, exchange, contract, interval, start_ms):
        end_ms = _now_ms()
        start_s = start_ms // 1000
        end_s = end_ms // 1000

        if exchange == "okx":
            inst = contract["ws_symbol"]
            all_rows, cursor = [], end_ms
            for _ in range(60):
                page = await F.fetch_okx(sess, inst, interval, end_ts_ms=cursor)
                if not page: break
                page = [r for r in page if r["ts"] >= start_ms]
                if not page: break
                all_rows = page + all_rows
                cursor = page[0]["ts"] - 1
                if page[0]["ts"] <= start_ms: break
                await asyncio.sleep(0.15)
            return all_rows

        if exchange == "hyperliquid":
            coin = contract["symbol"]
            all_rows, cur = [], start_ms
            for _ in range(30):
                page = await F.fetch_hyperliquid(sess, coin, interval, cur, end_ms)
                if not page: break
                all_rows.extend(page)
                cur = page[-1]["ts"] + 1
                if len(page) < 1000: break
                await asyncio.sleep(0.15)
            seen, dedup = set(), []
            for r in all_rows:
                if r["ts"] in seen: continue
                seen.add(r["ts"]); dedup.append(r)
            return dedup

        if exchange == "binance":
            sym = contract["ws_symbol"]
            all_rows, cursor = [], end_ms
            for _ in range(30):
                page = await F.fetch_binance(sess, sym, interval, end_ts_ms=cursor)
                if not page: break
                page = [r for r in page if r["ts"] >= start_ms]
                if not page: break
                all_rows = page + all_rows
                cursor = page[0]["ts"] - 1
                if page[0]["ts"] <= start_ms: break
                await asyncio.sleep(0.15)
            return all_rows

        if exchange == "aster":
            sym = contract["ws_symbol"]
            all_rows, cursor = [], end_ms
            for _ in range(30):
                page = await F.fetch_aster(sess, sym, interval, end_ts_ms=cursor)
                if not page: break
                page = [r for r in page if r["ts"] >= start_ms]
                if not page: break
                all_rows = page + all_rows
                cursor = page[0]["ts"] - 1
                if page[0]["ts"] <= start_ms: break
                await asyncio.sleep(0.15)
            return all_rows

        if exchange == "gateio":
            sym = contract["ws_symbol"]
            # spot 走現貨 candlesticks 端點（欄位順序與 futures 不同），perp 走 futures
            gate_fetch = F.fetch_gate_spot if contract.get("settlement") == "spot" else F.fetch_gate
            # Gate 不帶 limit 預設只回 100 點（limit 跟 from/to 互斥），用較小 step + 多 iterations
            step_s = 100 * (60 if interval == "1m" else 3600)
            max_iters = 800 if interval == "1m" else 100
            all_rows = []
            cur_s = start_s
            for _ in range(max_iters):
                if cur_s >= end_s: break
                to_s = min(cur_s + step_s, end_s)
                page = await gate_fetch(sess, sym, interval, cur_s, to_s)
                if page:
                    all_rows.extend(page)
                    cur_s = page[-1]["ts"] // 1000 + 1
                else:
                    cur_s = to_s + 1
                await asyncio.sleep(0.05)
            seen, dedup = set(), []
            for r in all_rows:
                if r["ts"] in seen: continue
                seen.add(r["ts"]); dedup.append(r)
            return dedup

        if exchange == "bingx":
            sym = contract["ws_symbol"]
            all_rows, cursor = [], end_ms
            for _ in range(20):
                page = await F.fetch_bingx(sess, sym, interval, end_ts_ms=cursor)
                if not page: break
                page = [r for r in page if r["ts"] >= start_ms]
                if not page: break
                all_rows = page + all_rows
                cursor = page[0]["ts"] - 1
                if page[0]["ts"] <= start_ms: break
                await asyncio.sleep(0.2)
            return all_rows

        if exchange == "bitget":
            # perp（USDT-FUTURES RWA 永續，如 SPCXUSDT）走 mix history-candles；
            # spot（IPO Prime preSPAX/preOPAI）走 spot history-candles（用 bitget_spot_symbol）
            if contract.get("settlement") == "perpetual":
                sym = contract["ws_symbol"]
                all_rows, cursor = [], end_ms
                for _ in range(250):  # mix history-candles 每頁 200，30 天 1m 需 ~216 頁
                    page = await F.fetch_bitget_perp(sess, sym, interval, end_ts_ms=cursor)
                    if not page: break
                    page = [r for r in page if r["ts"] >= start_ms]
                    if not page: break
                    all_rows = page + all_rows
                    cursor = page[0]["ts"] - 1
                    if page[0]["ts"] <= start_ms: break
                    await asyncio.sleep(0.15)
                return all_rows
            sym = contract.get("bitget_spot_symbol")
            if not sym: return []
            all_rows, cursor = [], end_ms
            for _ in range(60):
                page = await F.fetch_bitget_spot(sess, sym, interval, end_ts_ms=cursor)
                if not page: break
                page = [r for r in page if r["ts"] >= start_ms]
                if not page: break
                all_rows = page + all_rows
                cursor = page[0]["ts"] - 1
                if page[0]["ts"] <= start_ms: break
                await asyncio.sleep(0.15)
            return all_rows

        if exchange == "mexc":
            sym = contract["ws_symbol"]
            # spot（SPACEX(PRE)USDT 等）走 v3 spot klines，perp 走合約端點
            is_spot = contract.get("settlement") == "spot"
            step_s = 2000 * (60 if interval == "1m" else 3600)
            all_rows, cur_s = [], start_s
            for _ in range(30):
                if cur_s >= end_s: break
                to_s = min(cur_s + step_s, end_s)
                try:
                    if is_spot:
                        page = await F.fetch_mexc_spot(sess, sym, interval, cur_s, to_s)
                    else:
                        page = await F.fetch_mexc(sess, sym, interval, cur_s, to_s)
                except Exception:
                    page = []
                all_rows.extend(page)
                cur_s = to_s + 1
                await asyncio.sleep(0.15)
            seen, dedup = set(), []
            for r in all_rows:
                if r["ts"] in seen: continue
                seen.add(r["ts"]); dedup.append(r)
            return dedup

        if exchange == "ourbit":
            sym = contract["ws_symbol"]
            # spot（SPAXUSDT）走 v3 spot klines，perp 走合約 index_price 端點
            is_spot = contract.get("settlement") == "spot"
            step_s = 2000 * (60 if interval == "1m" else 3600)
            all_rows, cur_s = [], start_s
            for _ in range(30):
                if cur_s >= end_s: break
                to_s = min(cur_s + step_s, end_s)
                try:
                    if is_spot:
                        page = await F.fetch_ourbit_spot(sess, sym, interval, cur_s, to_s)
                    else:
                        page = await F.fetch_ourbit(sess, sym, interval, cur_s, to_s)
                except Exception:
                    page = []
                all_rows.extend(page)
                cur_s = to_s + 1
                await asyncio.sleep(0.15)
            seen, dedup = set(), []
            for r in all_rows:
                if r["ts"] in seen: continue
                seen.add(r["ts"]); dedup.append(r)
            return dedup

        if exchange == "prestocks":
            # PreStocks 鏈上歷史 K 沒公開 API，forward-only：當前 snapshot 累積
            pool = contract.get("prestocks_pool")
            if not pool: return []
            return await F.fetch_prestocks(sess, pool, interval)

        return []

    async def _incremental_loop(self):
        while True:
            try:
                await asyncio.sleep(INCREMENTAL_INTERVAL)
                async with aiohttp.ClientSession() as sess:
                    for und, info in self.assets.items():
                        total_supply = info.get("total_supply_shares", 0)
                        for ex, contracts in info.get("exchanges", {}).items():
                            for c in contracts:
                                for iv in INTERVALS:
                                    try:
                                        await self._backfill_contract(sess, und, ex, c, iv, total_supply)
                                    except Exception:
                                        pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[kline] incremental loop error: {e}")
