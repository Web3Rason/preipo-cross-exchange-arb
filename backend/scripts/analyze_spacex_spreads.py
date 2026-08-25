"""SPACEX 13 合約 × 78 對歷史價差分析

定義：
- spread(t) = (A.iv(t) - B.iv(t)) / ((A.iv(t) + B.iv(t))/2)  # 中位百分比價差
- "來回次數" = spread - median(spread) 的 zero-crossing 次數
  → 真正會「來回穿越中位數」才算，單調漂移不算
- 同步：以兩家共同存在的 1h ts 對齊

輸出：
- 78 對的 (來回次數, 價差中位數%, 價差絕對中位%, p10, p90, 共同 bar 數)
- 排序：來回次數 desc
"""
import sqlite3
import statistics
from itertools import combinations
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "klines.db"
UNDERLYING = "SPACEX"
INTERVAL = "1h"
MIN_OVERLAP = 48  # 至少 48h 共同存在才納入排序


def load_series(conn, key):
    rows = conn.execute(
        "SELECT ts, implied_valuation FROM klines "
        "WHERE contract_key=? AND interval=? AND implied_valuation IS NOT NULL "
        "ORDER BY ts ASC",
        (key, INTERVAL),
    ).fetchall()
    return {ts: iv for ts, iv in rows if iv and iv > 0}


def zero_crossings(values):
    n = 0
    prev = None
    for v in values:
        if v == 0:
            continue
        s = 1 if v > 0 else -1
        if prev is not None and s != prev:
            n += 1
        prev = s
    return n


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
    crosses = zero_crossings(centered)
    sorted_s = sorted(spreads)
    n = len(sorted_s)
    p10 = sorted_s[int(n * 0.1)]
    p90 = sorted_s[int(n * 0.9)]
    abs_med = statistics.median([abs(s) for s in spreads])
    return {
        "n": n,
        "crosses": crosses,
        "median_pct": med * 100,
        "abs_median_pct": abs_med * 100,
        "p10_pct": p10 * 100,
        "p90_pct": p90 * 100,
        "range_pct": (p90 - p10) * 100,
    }


def short(key):
    parts = key.split("/")
    ex = parts[0]
    deployer = parts[1] if len(parts) > 2 else ""
    sym = parts[-1]
    if deployer:
        return f"{ex}:{deployer}/{sym}"
    return f"{ex}/{sym}"


def main():
    conn = sqlite3.connect(DB)
    contracts = [r[0] for r in conn.execute(
        "SELECT DISTINCT contract_key FROM klines WHERE underlying=? AND interval=? ORDER BY contract_key",
        (UNDERLYING, INTERVAL),
    ).fetchall()]
    print(f"# SPACEX {len(contracts)} 合約 / {INTERVAL} kline / 共 {len(contracts)*(len(contracts)-1)//2} 對\n")

    series = {k: load_series(conn, k) for k in contracts}

    results = []
    for a, b in combinations(contracts, 2):
        r = analyze_pair(series[a], series[b])
        if r is None:
            continue
        r["pair"] = (a, b)
        results.append(r)

    results.sort(key=lambda r: (-r["crosses"], -r["n"]))

    print(f"納入排序的對數（共同 bar ≥ {MIN_OVERLAP}）：{len(results)} / 78")
    print()
    print(f"{'rank':>4} {'crosses':>7} {'bars':>5} {'med%':>8} {'absmed%':>8} {'p10%':>8} {'p90%':>8}  pair")
    print("-" * 110)
    for i, r in enumerate(results[:30], 1):
        a, b = r["pair"]
        print(f"{i:>4} {r['crosses']:>7} {r['n']:>5} "
              f"{r['median_pct']:>+8.2f} {r['abs_median_pct']:>8.2f} "
              f"{r['p10_pct']:>+8.2f} {r['p90_pct']:>+8.2f}  "
              f"{short(a)} <> {short(b)}")
    print()
    print("=== 後段（最少來回，通常是長期單邊偏離）===")
    for i, r in enumerate(results[-10:], len(results) - 9):
        a, b = r["pair"]
        print(f"{i:>4} {r['crosses']:>7} {r['n']:>5} "
              f"{r['median_pct']:>+8.2f} {r['abs_median_pct']:>8.2f} "
              f"{r['p10_pct']:>+8.2f} {r['p90_pct']:>+8.2f}  "
              f"{short(a)} <> {short(b)}")


if __name__ == "__main__":
    main()
