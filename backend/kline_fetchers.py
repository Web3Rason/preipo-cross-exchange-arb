"""各家交易所 REST K-line 抓取（分頁拉全歷史）

注意：歷史 K 必須 REST（沒 WS 推歷史），這個檔案是「啟動時 backfill + 定期 incremental」用，
不違反 WS-only 規則（規則是「即時數據用 WS」，歷史 K 本來就只能 REST）。
"""

import asyncio
import logging
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# 各家對 interval 的命名不同，統一 5032 內部用 '1m' / '1h'
INTERVAL_OKX = {"1m": "1m", "1h": "1H"}
INTERVAL_HL = {"1m": "1m", "1h": "1h"}
INTERVAL_BINANCE = {"1m": "1m", "1h": "1h"}
INTERVAL_GATE = {"1m": "1m", "1h": "1h"}
INTERVAL_BITGET = {"1m": "1min", "1h": "1h"}


def _f(v):
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


# ──── OKX ────
async def fetch_okx(sess: aiohttp.ClientSession, inst_id: str, interval: str, end_ts_ms: Optional[int] = None) -> list[dict]:
    """OKX history candles：bar=1m/1H、ms timestamps、回最新→最舊"""
    url = "https://www.okx.com/api/v5/market/history-candles"
    params = {"instId": inst_id, "bar": INTERVAL_OKX[interval], "limit": "300"}
    if end_ts_ms:
        params["after"] = str(end_ts_ms)  # after = 取此 ts 之前的（更舊）
    async with sess.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        d = await r.json()
    rows = d.get("data") or []
    out = []
    for row in rows:
        # [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        ts = int(row[0])
        out.append({"ts": ts, "o": _f(row[1]), "h": _f(row[2]), "l": _f(row[3]), "c": _f(row[4]), "v": _f(row[5])})
    out.sort(key=lambda x: x["ts"])
    return out


# ──── Hyperliquid（含 HIP-3 perpDEX 如 xyz/vntl） ────
async def fetch_hyperliquid(sess: aiohttp.ClientSession, coin: str, interval: str, start_ts_ms: int, end_ts_ms: int) -> list[dict]:
    """HL candleSnapshot：interval=1m/1h、coin 含 perpDEX prefix 如 'xyz:SPCX'"""
    url = "https://api.hyperliquid.xyz/info"
    body = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": INTERVAL_HL[interval], "startTime": start_ts_ms, "endTime": end_ts_ms}
    }
    async with sess.post(url, json=body, timeout=aiohttp.ClientTimeout(total=20)) as r:
        rows = await r.json()
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        # {t, T, s, i, o, c, h, l, v, n}
        out.append({"ts": int(row["t"]), "o": _f(row["o"]), "h": _f(row["h"]), "l": _f(row["l"]), "c": _f(row["c"]), "v": _f(row["v"])})
    out.sort(key=lambda x: x["ts"])
    return out


# ──── Binance Futures ────
async def fetch_binance(sess: aiohttp.ClientSession, symbol: str, interval: str, end_ts_ms: Optional[int] = None) -> list[dict]:
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {"symbol": symbol, "interval": INTERVAL_BINANCE[interval], "limit": 1500}
    if end_ts_ms:
        params["endTime"] = end_ts_ms
    async with sess.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        rows = await r.json()
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        # [openTime, o, h, l, c, v, closeTime, quoteVol, trades, ...]
        out.append({"ts": int(row[0]), "o": _f(row[1]), "h": _f(row[2]), "l": _f(row[3]), "c": _f(row[4]), "v": _f(row[5])})
    out.sort(key=lambda x: x["ts"])
    return out


# ──── Aster (Binance Futures 同款 API) ────
async def fetch_aster(sess: aiohttp.ClientSession, symbol: str, interval: str, end_ts_ms: Optional[int] = None) -> list[dict]:
    url = "https://fapi.asterdex.com/fapi/v1/klines"
    params = {"symbol": symbol, "interval": INTERVAL_BINANCE[interval], "limit": 1500}
    if end_ts_ms:
        params["endTime"] = end_ts_ms
    async with sess.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        rows = await r.json()
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        out.append({"ts": int(row[0]), "o": _f(row[1]), "h": _f(row[2]), "l": _f(row[3]), "c": _f(row[4]), "v": _f(row[5])})
    out.sort(key=lambda x: x["ts"])
    return out


# ──── Gate.io USDT-Futures ────
async def fetch_gate(sess: aiohttp.ClientSession, contract: str, interval: str, from_ts_s: int, to_ts_s: int) -> list[dict]:
    """Gate candlesticks：ts 秒、限制 from/to range、最多 2000 點"""
    url = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
    params = {"contract": contract, "interval": INTERVAL_GATE[interval], "from": from_ts_s, "to": to_ts_s}
    async with sess.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        rows = await r.json()
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        # {t, v, c, h, l, o}
        out.append({"ts": int(row["t"]) * 1000, "o": _f(row["o"]), "h": _f(row["h"]), "l": _f(row["l"]), "c": _f(row["c"]), "v": _f(row.get("v"))})
    out.sort(key=lambda x: x["ts"])
    return out


# ──── Bitget spot ────
async def fetch_bitget_spot(sess: aiohttp.ClientSession, symbol: str, interval: str, end_ts_ms: Optional[int] = None) -> list[dict]:
    url = "https://api.bitget.com/api/v2/spot/market/history-candles"
    params = {"symbol": symbol, "granularity": INTERVAL_BITGET[interval], "limit": "200"}
    if end_ts_ms:
        params["endTime"] = str(end_ts_ms)
    async with sess.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        d = await r.json()
    rows = d.get("data") or []
    out = []
    for row in rows:
        # [ts, o, h, l, c, baseVol, usdtVol]
        out.append({"ts": int(row[0]), "o": _f(row[1]), "h": _f(row[2]), "l": _f(row[3]), "c": _f(row[4]), "v": _f(row[6]) if len(row) > 6 else _f(row[5])})
    out.sort(key=lambda x: x["ts"])
    return out


# ──── Bitget USDT-FUTURES (mix) ────
# 實測（2026-06-05）：SPCXUSDT RWA 永續歷史在 mix history-candles，
# 與 spot 端點不同：路徑 /api/v2/mix/market/history-candles，多帶 productType=USDT-FUTURES，
# granularity 用 1m / 1H（**不是 spot 的 1min / 1h**）。回 array [ts_ms, o, h, l, c, baseVol, quoteVol]。
INTERVAL_BITGET_MIX = {"1m": "1m", "1h": "1H"}


async def fetch_bitget_perp(sess: aiohttp.ClientSession, symbol: str, interval: str, end_ts_ms: Optional[int] = None, product_type: str = "USDT-FUTURES") -> list[dict]:
    url = "https://api.bitget.com/api/v2/mix/market/history-candles"
    params = {"symbol": symbol, "productType": product_type, "granularity": INTERVAL_BITGET_MIX[interval], "limit": "200"}
    if end_ts_ms:
        params["endTime"] = str(end_ts_ms)
    async with sess.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        d = await r.json()
    rows = d.get("data") or []
    out = []
    for row in rows:
        # [ts, o, h, l, c, baseVol, quoteVol]
        out.append({"ts": int(row[0]), "o": _f(row[1]), "h": _f(row[2]), "l": _f(row[3]), "c": _f(row[4]), "v": _f(row[6]) if len(row) > 6 else _f(row[5])})
    out.sort(key=lambda x: x["ts"])
    return out


# ──── BingX swap ────
async def fetch_bingx(sess: aiohttp.ClientSession, symbol: str, interval: str, end_ts_ms: Optional[int] = None) -> list[dict]:
    url = "https://open-api.bingx.com/openApi/swap/v3/quote/klines"
    params = {"symbol": symbol, "interval": INTERVAL_BINANCE[interval], "limit": 1000}
    if end_ts_ms:
        params["endTime"] = str(end_ts_ms)
    async with sess.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        d = await r.json()
    rows = d.get("data") or []
    out = []
    for row in rows:
        # {open, close, high, low, volume, time}
        out.append({"ts": int(row["time"]), "o": _f(row["open"]), "h": _f(row["high"]), "l": _f(row["low"]), "c": _f(row["close"]), "v": _f(row.get("volume"))})
    out.sort(key=lambda x: x["ts"])
    return out


# ──── MEXC / Ourbit (MEXC 白標) 共用 kline format ────
async def _fetch_mexc_style(sess: aiohttp.ClientSession, base_url: str, symbol: str, interval: str, start_ts_s: int, end_ts_s: int) -> list[dict]:
    iv = {"1m": "Min1", "1h": "Min60"}[interval]
    url = f"{base_url}/api/v1/contract/kline/{symbol}"
    params = {"interval": iv, "start": start_ts_s, "end": end_ts_s}
    async with sess.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        d = await r.json()
    data = d.get("data") or {}
    times = data.get("time") or []
    opens = data.get("open") or []
    highs = data.get("high") or []
    lows = data.get("low") or []
    closes = data.get("close") or []
    vols = data.get("vol") or []
    out = []
    for i, t in enumerate(times):
        out.append({"ts": int(t) * 1000,
                    "o": _f(opens[i]) if i < len(opens) else None,
                    "h": _f(highs[i]) if i < len(highs) else None,
                    "l": _f(lows[i]) if i < len(lows) else None,
                    "c": _f(closes[i]) if i < len(closes) else None,
                    "v": _f(vols[i]) if i < len(vols) else None})
    out.sort(key=lambda x: x["ts"])
    return out


async def fetch_mexc(sess: aiohttp.ClientSession, symbol: str, interval: str, start_ts_s: int, end_ts_s: int) -> list[dict]:
    return await _fetch_mexc_style(sess, "https://contract.mexc.com", symbol, interval, start_ts_s, end_ts_s)


# ──── MEXC / Ourbit SPOT (v3 spot klines，與合約端點不同) ────
# 實測（2026-06-05）：MEXC 現貨 SPACEX(PRE)USDT、Ourbit 現貨 SPAXUSDT 的歷史 K 線只在
# 各自的 spot v3 REST `/api/v3/klines`，合約端點 `/api/v1/contract/kline/` 對現貨 symbol 回空。
# interval enum：1m / 5m / 15m / 30m / 60m / 4h / 1d…（**注意 1h 無效，要用 60m**）
# 回 array：[openTime_ms, o, h, l, c, vol(base), closeTime_ms, quoteVol]
INTERVAL_MEXC_SPOT = {"1m": "1m", "1h": "60m"}
_IV_MS = {"1m": 60_000, "1h": 3_600_000}
_MEXC_SPOT_LIMIT = 500  # 實測 v3 spot：limit 上限 500（給 1000 仍只回 500）


async def _fetch_mexc_v3_spot_style(sess: aiohttp.ClientSession, base_url: str, symbol: str, interval: str, start_ts_s: int, end_ts_s: int) -> list[dict]:
    """固定窗口滑動分頁。
    實測（2026-06-05）MEXC v3 spot klines 行為：同時帶 startTime+endTime 時，
    只回 [startTime, startTime + limit×interval] 這個窗口（limit 上限 500），
    不會自動往 endTime 收斂；且若窗口落在上市前會回空（不能用「回空就停」當終止條件）。
    → 用固定窗口寬度 limit×interval 從 start 滑到 end，逐窗收集。"""
    iv = INTERVAL_MEXC_SPOT[interval]
    url = f"{base_url}/api/v3/klines"
    win_ms = _MEXC_SPOT_LIMIT * _IV_MS[interval]
    out: list[dict] = []
    cur_ms, end_ms = start_ts_s * 1000, end_ts_s * 1000
    max_iters = (end_ms - cur_ms) // win_ms + 2
    for _ in range(int(max_iters)):
        if cur_ms >= end_ms:
            break
        params = {"symbol": symbol, "interval": iv, "startTime": cur_ms,
                  "endTime": min(cur_ms + win_ms, end_ms), "limit": _MEXC_SPOT_LIMIT}
        async with sess.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
            rows = await r.json()
        if isinstance(rows, list):
            for row in rows:
                out.append({"ts": int(row[0]), "o": _f(row[1]), "h": _f(row[2]), "l": _f(row[3]), "c": _f(row[4]), "v": _f(row[5])})
        cur_ms += win_ms  # 固定步進，空窗（上市前）也照樣往後滑
        await asyncio.sleep(0.08)
    # 去重 + 排序（相鄰窗邊界可能重疊一筆）
    dedup = {r["ts"]: r for r in out}
    return sorted(dedup.values(), key=lambda x: x["ts"])


async def fetch_mexc_spot(sess: aiohttp.ClientSession, symbol: str, interval: str, start_ts_s: int, end_ts_s: int) -> list[dict]:
    return await _fetch_mexc_v3_spot_style(sess, "https://api.mexc.com", symbol, interval, start_ts_s, end_ts_s)


async def fetch_ourbit_spot(sess: aiohttp.ClientSession, symbol: str, interval: str, start_ts_s: int, end_ts_s: int) -> list[dict]:
    return await _fetch_mexc_v3_spot_style(sess, "https://api.ourbit.com", symbol, interval, start_ts_s, end_ts_s)


# ──── Gate.io SPOT candlesticks（欄位順序與 futures 完全不同！） ────
# 實測（2026-06-05）：GET /api/v4/spot/candlesticks?currency_pair=&interval=&from=&to=
# 回 array：[t_s(str), quoteVolume, close, high, low, open, baseVolume, windowClosed]
# 對比 futures 是 dict {t,v,c,h,l,o} — 不能共用 fetch_gate parser。
async def fetch_gate_spot(sess: aiohttp.ClientSession, currency_pair: str, interval: str, from_ts_s: int, to_ts_s: int) -> list[dict]:
    url = "https://api.gateio.ws/api/v4/spot/candlesticks"
    params = {"currency_pair": currency_pair, "interval": INTERVAL_GATE[interval], "from": from_ts_s, "to": to_ts_s}
    async with sess.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        rows = await r.json()
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        # [t, quoteVol, close, high, low, open, baseVol, windowClosed]
        out.append({"ts": int(row[0]) * 1000, "o": _f(row[5]), "h": _f(row[3]), "l": _f(row[4]), "c": _f(row[2]), "v": _f(row[6])})
    out.sort(key=lambda x: x["ts"])
    return out


async def fetch_ourbit(sess: aiohttp.ClientSession, symbol: str, interval: str, start_ts_s: int, end_ts_s: int) -> list[dict]:
    iv = {"1m": "Min1", "1h": "Min60"}[interval]
    url = "https://futures.ourbit.com/api/v1/contract/kline/index_price/{}".format(symbol)
    params = {"interval": iv, "start": start_ts_s, "end": end_ts_s}
    async with sess.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        d = await r.json()
    data = d.get("data") or {}
    times = data.get("time") or []
    opens = data.get("realOpen") or data.get("open") or []
    highs = data.get("realHigh") or data.get("high") or []
    lows = data.get("realLow") or data.get("low") or []
    closes = data.get("realClose") or data.get("close") or []
    vols = data.get("vol") or []
    out = []
    for i, t in enumerate(times):
        out.append({"ts": int(t) * 1000,
                    "o": _f(opens[i]) if i < len(opens) else None,
                    "h": _f(highs[i]) if i < len(highs) else None,
                    "l": _f(lows[i]) if i < len(lows) else None,
                    "c": _f(closes[i]) if i < len(closes) else None,
                    "v": _f(vols[i]) if i < len(vols) else None})
    out.sort(key=lambda x: x["ts"])
    return out


# ──── PreStocks via DexScreener (Solana Meteora pool) ────
async def fetch_prestocks(sess: aiohttp.ClientSession, pair_addr: str, interval: str) -> list[dict]:
    """DexScreener 沒公開 historical OHLCV endpoint，只能拿當下 priceUsd。
    歷史 K 線真實上要透過 Birdeye (要付費) 或 Solana indexer 自建。
    暫時：返回當下 1 點 snapshot（占位實作，後續補 Birdeye 或自建 indexer）。
    """
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_addr}"
    async with sess.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
        d = await r.json()
    pair = d.get("pair") or (d.get("pairs") or [None])[0]
    if not pair:
        return []
    px = _f(pair.get("priceUsd"))
    if px is None:
        return []
    now_ms = int(time.time() * 1000)
    # 對齊 minute / hour 邊界
    if interval == "1m":
        ts = (now_ms // 60000) * 60000
    else:
        ts = (now_ms // 3600000) * 3600000
    return [{"ts": ts, "o": px, "h": px, "l": px, "c": px, "v": None}]
