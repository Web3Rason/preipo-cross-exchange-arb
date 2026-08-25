"""SPACEX 13 合約 × 78 對歷史價差分析 (1% threshold 版)

定義：
- spread(t) = (A.iv - B.iv) / ((A.iv + B.iv)/2)
- centered(t) = spread(t) - median(spread)
- 「有效來回」=兩次 centered zero-crossing 之間，極值絕對值必須 >= THRESHOLD (1%)
- 同時記錄每趟可捕獲的幅度 (median of excursion peaks)
"""
import sqlite3
import statistics
from itertools import combinations
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "klines.db"
UNDERLYING = "SPACEX"
INTERVAL = "1h"
MIN_OVERLAP = 48
THRESHOLD = 0.01  # 1%


def load_series(conn, key):
    rows = conn.execute(
        "SELECT ts, implied_valuation FROM klines "
        "WHERE contract_key=? AND interval=? AND implied_valuation IS NOT NULL "
        "ORDER BY ts ASC",
        (key, INTERVAL),
    ).fetchall()
    return {ts: iv for ts, iv in rows if iv and iv > 0}


def effective_round_trips(centered, threshold):
    """切成多段（以 zero-crossing 為界），每段取極值絕對值；
    一段極值 >= threshold 算一次有效偏離；
    `round_trips` = 連續兩段（正負）都 >= threshold 的對數
    """
    segs = []
    cur_sign = None
    cur_peak = 0.0
    for v in centered:
        if v == 0:
            continue
        s = 1 if v > 0 else -1
        if cur_sign is None:
            cur_sign = s
            cur_peak = abs(v)
        elif s == cur_sign:
            cur_peak = max(cur_peak, abs(v))
        else:
            segs.append((cur_sign, cur_peak))
            cur_sign = s
            cur_peak = abs(v)
    if cur_sign is not None:
        segs.append((cur_sign, cur_peak))

    qualifying = [p for _, p in segs if p >= threshold]
    n_excursions = len(qualifying)
    round_trips = 0
    last_qual_sign = None
    for sign, peak in segs:
        if peak < threshold:
            continue
        if last_qual_sign is not None and sign != last_qual_sign:
            round_trips += 1
        last_qual_sign = sign
    median_peak = statistics.median(qualifying) if qualifying else 0.0
    return {
        "excursions_1pct": n_excursions,
        "round_trips_1pct": round_trips,
        "median_peak_pct": median_peak * 100,
    }


def analyze_pair(a_series, b_series):
    common = sorted(set(a_series) & set(b_series))
    if len(common) < MIN_OVERLAP:
        return None
    spreads = []
    for ts in common:
        a, b = a_series[ts], b_series[ts]
        mid = (a + b) / 2
        if mid <= 0:
            continue
        spreads.append((a - b) / mid)
    if len(spreads) < MIN_OVERLAP:
        return None
    med = statistics.median(spreads)
    centered = [s - med for s in spreads]
    rt = effective_round_trips(centered, THRESHOLD)
    rt.update({
        "n": len(spreads),
        "median_pct": med * 100,
        "abs_median_pct": statistics.median([abs(s) for s in spreads]) * 100,
    })
    return rt


def short(key):
    parts = key.split("/")
    ex = parts[0]
    deployer = parts[1] if len(parts) > 2 else ""
    sym = parts[-1]
    return f"{ex}:{deployer}/{sym}" if deployer else f"{ex}/{sym}"


def main():
    conn = sqlite3.connect(DB)
    contracts = [r[0] for r in conn.execute(
        "SELECT DISTINCT contract_key FROM klines WHERE underlying=? AND interval=? ORDER BY contract_key",
        (UNDERLYING, INTERVAL),
    ).fetchall()]
    series = {k: load_series(conn, k) for k in contracts}

    results = []
    for a, b in combinations(contracts, 2):
        r = analyze_pair(series[a], series[b])
        if r is None:
            continue
        r["pair"] = (a, b)
        results.append(r)

    results.sort(key=lambda r: (-r["round_trips_1pct"], -r["median_peak_pct"]))

    print(f"# SPACEX 1% threshold 過濾後排序  /  {INTERVAL} kline  /  共 {len(results)} 對通過")
    print(f"# 「round_trips_1pct」= 連續兩段極值都 >=1% 的次數（一次完整 ≥1% 往返）")
    print(f"# 「excursions_1pct」= 單邊極值 >=1% 的段數")
    print(f"# 「median_peak%」= 這些有效偏離的中位幅度")
    print()
    print(f"{'rank':>4} {'RT_1%':>5} {'exc':>4} {'pk%':>6} {'bars':>5} {'med%':>7} {'absmed%':>7}  pair")
    print("-" * 110)
    for i, r in enumerate(results, 1):
        a, b = r["pair"]
        print(f"{i:>4} {r['round_trips_1pct']:>5} {r['excursions_1pct']:>4} "
              f"{r['median_peak_pct']:>6.2f} {r['n']:>5} "
              f"{r['median_pct']:>+7.2f} {r['abs_median_pct']:>7.2f}  "
              f"{short(a)} <> {short(b)}")


if __name__ == "__main__":
    main()
