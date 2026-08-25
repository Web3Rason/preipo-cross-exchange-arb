"""SPACEX 78 對價差「收斂到阻力位」套利訊號

策略邏輯：
  每對 (A, B) 的 spread% 在歷史上會在一個「均衡帶」內震盪，
  均衡帶不是 0（不同合約有結構性 offset：SPV 折價、不同 funding 制度、現貨/永續價差等）。
  進場：spread 衝出 p10-p90 band 外 → 預期回 median
  目標：median（即均衡），**不是 0**
  停損：若 spread 繼續往同方向走 (1.5*band 寬)，視為 regime break、退出

定義：
  spread(t) = (A.iv(t) - B.iv(t)) / ((A.iv(t)+B.iv(t))/2)
  median = 歷史均衡（資料夠多時近似 mode）
  p10/p90 = band 邊界（80% 區間）
  current spread = 最後一根 K
  deviation = current - median（單位 %）
  z = deviation / (p90 - p10)（band-normalized；|z| > 0.5 視為衝出 band 一半以上、可進場觀察）

輸出：可進場（|z| 大）→ 預期 %收益 = |deviation|（從邊回到中位）
"""
import sqlite3
import statistics
from itertools import combinations
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "klines.db"
UNDERLYING = "SPACEX"
INTERVAL = "1h"
MIN_OVERLAP = 72  # 至少 3 天歷史
MIN_ABS_DEV_PCT = 0.5  # 偏離均衡至少 0.5% 才看（扣完雙腿 fee ~0.2% 後還能賺）
# 只用 2026-05-07 09:00 UTC 之後的數據，避開 BingX V2/P2 regime break 污染均衡計算
SINCE_TS_MS = 1778148000 * 1000


def load_series(conn, key):
    rows = conn.execute(
        "SELECT ts, implied_valuation FROM klines "
        "WHERE contract_key=? AND interval=? AND implied_valuation IS NOT NULL AND ts >= ? "
        "ORDER BY ts ASC",
        (key, INTERVAL, SINCE_TS_MS),
    ).fetchall()
    return {ts: iv for ts, iv in rows if iv and iv > 0}


def analyze(a_series, b_series):
    common = sorted(set(a_series) & set(b_series))
    if len(common) < MIN_OVERLAP:
        return None
    spreads = []
    for ts in common:
        a, b = a_series[ts], b_series[ts]
        mid = (a + b) / 2
        if mid > 0:
            spreads.append(((ts, (a - b) / mid * 100)))
    if len(spreads) < MIN_OVERLAP:
        return None
    vals = [s for _, s in spreads]
    sorted_v = sorted(vals)
    n = len(sorted_v)
    median = statistics.median(vals)
    p10 = sorted_v[int(n * 0.1)]
    p90 = sorted_v[int(n * 0.9)]
    p25 = sorted_v[int(n * 0.25)]
    p75 = sorted_v[int(n * 0.75)]
    cur_ts, cur = spreads[-1]
    band_w = p90 - p10
    dev = cur - median
    z = (dev / band_w) if band_w else 0
    # 進場方向：dev > 0 → A 太貴、short A long B → 目標收回 median，預期收益 = dev
    # dev < 0 → A 太便宜、long A short B → 收益 = -dev
    direction = "short_A_long_B" if dev > 0 else "long_A_short_B"
    target = median
    abs_dev = abs(dev)
    return {
        "n": n,
        "median_pct": median,
        "p10_pct": p10, "p90_pct": p90,
        "p25_pct": p25, "p75_pct": p75,
        "band_w_pct": band_w,
        "current_pct": cur,
        "dev_pct": dev,
        "abs_dev_pct": abs_dev,
        "z": z,
        "direction": direction,
        "target_pct": target,
    }


def short(k):
    parts = k.split("/")
    ex = parts[0]
    dep = parts[1] if len(parts) > 2 else ""
    sym = parts[-1]
    return f"{ex}:{dep}/{sym}" if dep else f"{ex}/{sym}"


def main():
    conn = sqlite3.connect(DB)
    contracts = [r[0] for r in conn.execute(
        "SELECT DISTINCT contract_key FROM klines WHERE underlying=? AND interval=? ORDER BY contract_key",
        (UNDERLYING, INTERVAL),
    ).fetchall()]
    series = {k: load_series(conn, k) for k in contracts}

    results = []
    for a, b in combinations(contracts, 2):
        r = analyze(series[a], series[b])
        if r is None:
            continue
        r["pair"] = (a, b)
        results.append(r)

    # 排序：|z| 大代表衝出 band 越多、進場越急；abs_dev_pct 代表預期收益
    results.sort(key=lambda r: (-abs(r["z"]), -r["abs_dev_pct"]))

    actionable = [r for r in results if r["abs_dev_pct"] >= MIN_ABS_DEV_PCT and abs(r["z"]) >= 0.5]

    print(f"# SPACEX 收斂到均衡（非 0）套利訊號  /  {INTERVAL} K  /  共 {len(results)} 對通過樣本門檻")
    print(f"# Actionable（|dev| ≥ {MIN_ABS_DEV_PCT}% 且 |z| ≥ 0.5）：{len(actionable)} 對\n")
    if not actionable:
        print("→ 當下沒有 spread 顯著衝出歷史 band 的 pair，等下次機會\n")

    fmt = "{:>4} {:>5} {:>7} {:>7} {:>7} {:>7} {:>7} {:>8} {:>8} {:>5}  {:<14}  {}"
    hdr = fmt.format("rk", "bars", "med%", "p10%", "p90%", "bandW%", "cur%", "dev%", "tgt%", "z", "dir", "pair")
    print(hdr)
    print("-" * len(hdr))
    for i, r in enumerate(actionable[:25], 1):
        a, b = r["pair"]
        print(fmt.format(
            i, r["n"], f"{r['median_pct']:+.2f}", f"{r['p10_pct']:+.2f}", f"{r['p90_pct']:+.2f}",
            f"{r['band_w_pct']:.2f}", f"{r['current_pct']:+.2f}",
            f"{r['dev_pct']:+.2f}", f"{r['target_pct']:+.2f}", f"{r['z']:+.2f}",
            r["direction"], f"{short(a)} <> {short(b)}"
        ))

    if actionable:
        print("\n## 進場/目標解讀")
        for r in actionable[:5]:
            a, b = r["pair"]
            cur = r["current_pct"]; tgt = r["target_pct"]; dev = r["dev_pct"]
            p10, p90 = r["p10_pct"], r["p90_pct"]
            band_l = f"[{p10:+.2f}%, {p90:+.2f}%]"
            print(f"\n  {short(a)}  vs  {short(b)}")
            print(f"    歷史均衡 band: {band_l}（中位 {r['median_pct']:+.2f}%）")
            print(f"    現在: spread = {cur:+.2f}%  →  {'高於 p90、賣 A 買 B' if dev > 0 else '低於 p10、買 A 賣 B'}")
            print(f"    目標: spread 收回 median = {tgt:+.2f}%  →  預期捕獲 {abs(dev):.2f}%")
            bw = r["band_w_pct"]
            stop = cur + 0.5 * bw if dev > 0 else cur - 0.5 * bw
            print(f"    停損: spread 擴張到 {stop:+.2f}%（regime break）")


if __name__ == "__main__":
    main()
