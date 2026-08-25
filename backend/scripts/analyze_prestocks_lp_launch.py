"""鏈上抓 PreStocks 7 個 Meteora DLMM pool 的建倉初始價，
作為推導真實 token → valuation conversion ratio 的 ground truth。

資料來源：
  - DexScreener: pairCreatedAt (建倉時間)、current price、liquidity
  - GeckoTerminal OHLCV: 拿建倉日 / 隔日的 open price = LP launch 起點價
"""
import asyncio
import time
import aiohttp

POOLS = {
    "SPACEX":     "v4D5b4knJ83WzErtDiugxChUZUfWxe2FtDLguarvgFc",
    "OPENAI":     "Y6wSJPjwcnfY8sHM2b3gTht3gGCGdf838gRkQJpfNYe",
    "ANTHROPIC":  "HhABC2WjgUq6xYUSA92diUuQ8A9rxoXaWbN38KQkhZSt",
    "ANDURIL":    "Gug9TrrLMoz8NpC4pE8ANQ6H7XzMMZYPuu1EJCn59ZwM",
    "NEURALINK":  "EyyeCgFRX8GBvBU8PBupYKr2LRTewYo7m6YkTKCZHjWv",
    "KALSHI":     "FoL5dFhV7XUootAbrqDdA72oXL8HExspLNRt1tuJNv53",
    "POLYMARKET": "gPchZYUR4prBpYr4BQDZdB4fwYxWSh8h2rY83CQxN61",
}


async def fetch_dexscreener(sess, pair_addr):
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_addr}"
    async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        return await r.json()


async def fetch_gecko_ohlcv(sess, pair_addr, before_ts=None, aggregate=1, timeframe="day", limit=10):
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair_addr}/ohlcv/{timeframe}?aggregate={aggregate}&limit={limit}"
    if before_ts:
        url += f"&before_timestamp={before_ts}"
    async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        return await r.json()


async def fetch_gecko_pool_info(sess, pair_addr):
    """嘗試取 GeckoTerminal 的 pool 中繼資料（pool_created_at 在這裡會有）"""
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair_addr}"
    async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        return await r.json()


async def analyze(sess, sym, pool):
    out = {"symbol": sym, "pool": pool}
    # DexScreener
    try:
        ds = await fetch_dexscreener(sess, pool)
        p = (ds.get("pairs") or [None])[0]
        if p:
            out["dex"] = p.get("dexId")
            out["created_at_ms"] = p.get("pairCreatedAt")
            out["created_at_iso"] = (
                time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(p.get("pairCreatedAt") / 1000))
                if p.get("pairCreatedAt") else None
            )
            out["current_price_usd"] = float(p.get("priceUsd") or 0)
            out["liquidity_usd"] = (p.get("liquidity") or {}).get("usd")
            out["fdv"] = p.get("fdv")
            out["vol_24h"] = (p.get("volume") or {}).get("h24")
    except Exception as e:
        out["dex_err"] = str(e)
    # GeckoTerminal — 拿建倉日 +/- 幾天的 OHLCV
    created_ts = (out.get("created_at_ms") or 0) // 1000
    if created_ts:
        # 拉建倉日後 5 天的 daily candles：最早那根的 open 就是 LP launch 起點
        try:
            after_5d = created_ts + 5 * 86400
            r = await fetch_gecko_ohlcv(sess, pool, before_ts=after_5d, timeframe="day", limit=6)
            candles = ((r.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
            candles.sort(key=lambda x: x[0])
            if candles:
                first = candles[0]
                out["launch_candle_ts"] = first[0]
                out["launch_candle_iso"] = time.strftime("%Y-%m-%d UTC", time.gmtime(first[0]))
                out["launch_open"] = first[1]
                out["launch_high"] = first[2]
                out["launch_low"] = first[3]
                out["launch_close"] = first[4]
                out["launch_volume_usd"] = first[5]
        except Exception as e:
            out["gecko_err"] = str(e)
    return out


async def main():
    async with aiohttp.ClientSession() as sess:
        tasks = [analyze(sess, sym, pool) for sym, pool in POOLS.items()]
        results = await asyncio.gather(*tasks)
    # Print
    print(f"\n{'sym':<11} {'created':<18} {'launch_open':>11} {'launch_low':>10} {'launch_high':>11} {'current':>9} {'×':>6} {'liq($)':>10}")
    print("-" * 100)
    for r in results:
        sym = r['symbol']
        created = r.get("created_at_iso") or "n/a"
        lo = f"${r.get('launch_open'):.2f}" if r.get('launch_open') else "n/a"
        lh = f"${r.get('launch_high'):.2f}" if r.get('launch_high') else "n/a"
        ll = f"${r.get('launch_low'):.2f}" if r.get('launch_low') else "n/a"
        cur = f"${r.get('current_price_usd'):.2f}" if r.get('current_price_usd') else "n/a"
        mult = (r.get('current_price_usd') / r.get('launch_open')) if (r.get('current_price_usd') and r.get('launch_open')) else None
        mult_s = f"{mult:.2f}x" if mult else "n/a"
        liq = f"${r.get('liquidity_usd'):,.0f}" if r.get('liquidity_usd') else "n/a"
        print(f"{sym:<11} {created:<18} {lo:>11} {ll:>10} {lh:>11} {cur:>9} {mult_s:>6} {liq:>10}")
    print()
    # Detail
    for r in results:
        print(f"\n## {r['symbol']}")
        for k, v in r.items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
