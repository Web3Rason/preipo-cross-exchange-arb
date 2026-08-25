"""SPACEX 78 對「會反覆來回（振盪）」的 pair 排名。

目的：給你的策略找「會再發散」的對，不是只看現在誰偏離多。

評分維度：
  1. round_trips_through_median: 穿越自身中位數的次數
     → 越多代表「來回」越頻繁、能反覆進出場
  2. excursion_amplitude_p75: 每次離開中位數的「典型振幅」(p75 of peak excursions)
     → 太小（< 0.5%）扣完 fee 沒得賺、再多次也沒用
  3. excursion_per_day: round_trips / 天數
     → 標準化到「每天平均幾次來回」
  4. now_far_from_median: 現在 |spread - median| / band_width
     → 越大代表「現在已發散、可進場」；接近 0 代表「現在貼著均衡、要等」
  5. recent_oscillation_consistency: 最近 24h 振盪是否還在進行（避免 dead pair）

最終分數：
  score = excursion_per_day × min(amplitude, 5%) × (current_z > 0.3 ? 1.0 : 0.3)
  → 「振盪頻率 × 單次幅度 × 是否現在就有進場點」三者乘積
"""
import sqlite3
import statistics
from itertools import combinations
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "klines.db"
UNDERLYING = "SPACEX"
INTERVAL = "1h"
MIN_OVERLAP = 120  # 至少 5 天
MIN_AMP_PCT = 0.5  # 振幅小於 0.5% 不考慮
SINCE_TS_MS = 1778148000 * 1000  # 2026-05-07 後（避開 BingX regime break 污染）


def load_series(conn, key):
    rows = conn.execute(
        "SELECT ts, implied_valuation FROM klines "
        "WHERE contract_key=? AND interval=? AND implied_valuation IS NOT NULL AND ts >= ? "
        "ORDER BY ts ASC",
        (key, INTERVAL, SINCE_TS_MS),
    ).fetchall()
    return {ts: iv for ts, iv in rows if iv and iv > 0}


def list_4part_contracts(conn, underlying, interval):
    """只列新 4-part schema 的 contract_key（過濾掉舊 3-part 殘留 garbage）"""
    rows = conn.execute(
        "SELECT DISTINCT contract_key FROM klines "
        "WHERE underlying=? AND interval=? AND contract_key LIKE '%/%/%/%' "
        "ORDER BY contract_key",
        (underlying, interval),
    ).fetchall()
    return [r[0] for r in rows]


def analyze(a_series, b_series):
    common = sorted(set(a_series) & set(b_series))
    if len(common) < MIN_OVERLAP:
        return None
    spreads = []
    for ts in common:
        a, b = a_series[ts], b_series[ts]
        mid = (a + b) / 2
        if mid > 0:
            spreads.append((ts, (a - b) / mid * 100))
    if len(spreads) < MIN_OVERLAP:
        return None
    vals = [s for _, s in spreads]
    sorted_v = sorted(vals)
    n = len(sorted_v)
    median = statistics.median(vals)
    p10 = sorted_v[int(n * 0.1)]
    p90 = sorted_v[int(n * 0.9)]
    band_w = max(p90 - p10, 1e-9)
    centered = [v - median for v in vals]

    # ---- round trips: 切段、找每段極值、極值 ≥ MIN_AMP_PCT 才算一次有效偏離
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

    qualifying = [p for _, p in segs if p >= MIN_AMP_PCT]
    excursion_count = len(qualifying)
    median_amp = statistics.median(qualifying) if qualifying else 0.0
    # round-trip = 連續兩段都 qualifying 才算一次「來回」
    round_trips = 0
    last_sign = None
    for sign, peak in segs:
        if peak < MIN_AMP_PCT:
            continue
        if last_sign is not None and sign != last_sign:
            round_trips += 1
        last_sign = sign

    days = n / 24.0  # 1h K
    excursion_per_day = excursion_count / days if days > 0 else 0
    cur = vals[-1]
    cur_dev = cur - median
    cur_z = abs(cur_dev) / band_w if band_w else 0

    score = excursion_per_day * min(median_amp, 5.0) * (1.0 if cur_z > 0.3 else 0.3)

    # 最近 24h 是否仍在振盪
    recent = vals[-24:] if len(vals) >= 24 else vals
    recent_range = max(recent) - min(recent) if recent else 0
    recent_active = recent_range > MIN_AMP_PCT

    return {
        "n": n, "days": round(days, 1),
        "median": median, "p10": p10, "p90": p90, "band_w": band_w,
        "current": cur, "cur_dev": cur_dev, "cur_z": cur_z,
        "excursion_count": excursion_count,
        "excursion_per_day": excursion_per_day,
        "round_trips": round_trips,
        "median_amp": median_amp,
        "recent_24h_range": recent_range,
        "recent_active": recent_active,
        "score": score,
    }


def short(k):
    """4-part: ex/dep/sym/settlement → 'ex:dep/sym (settle)' 或 'ex/sym (settle)'
    向後相容 3-part 舊 key（無 settlement）"""
    parts = k.split("/")
    if len(parts) >= 4 and parts[-1] in ("perpetual", "spot", "spot_solana"):
        ex, dep = parts[0], parts[1]
        sym = "/".join(parts[2:-1])  # symbol 可能含 :（HL HIP-3）
        settle = parts[-1]
    else:
        ex = parts[0]
        dep = parts[1] if len(parts) > 2 else ""
        sym = "/".join(parts[2:]) if len(parts) > 2 else parts[-1]
        settle = ""
    head = f"{ex}:{dep}/{sym}" if dep else f"{ex}/{sym}"
    if settle == "perpetual":
        return head  # 永續省略後綴節省版面
    if settle == "spot":
        return head + "(spot)"
    if settle == "spot_solana":
        return head + "(sol)"
    return head


def main():
    conn = sqlite3.connect(DB)
    contracts = list_4part_contracts(conn, UNDERLYING, INTERVAL)
    series = {k: load_series(conn, k) for k in contracts}

    results = []
    for a, b in combinations(contracts, 2):
        r = analyze(series[a], series[b])
        if r is None: continue
        r["pair"] = (a, b)
        results.append(r)

    # 雙條件排序：score 高 + recent_active
    results.sort(key=lambda r: (-r["score"], -r["round_trips"]))

    print(f"# SPACEX 振盪型套利 pair 排名  /  {INTERVAL} K / 5/7 後 / {len(results)} 對通過樣本門檻\n")
    print("score 公式：每日振盪次數 × min(典型振幅, 5%) × 現在是否在 band 邊\n")

    fmt = "{:>3} {:>6} {:>5} {:>6} {:>8} {:>8} {:>7} {:>6} {:>6} {:>6}  {:<3}  {}"
    hdr = fmt.format("rk", "score", "days", "trips", "ex/day", "amp_med", "bandW", "cur_z", "rcnt24", "active", "now", "pair")
    print(hdr)
    print("-" * len(hdr))
    for i, r in enumerate(results[:25], 1):
        a, b = r["pair"]
        now_at_edge = ""
        if r["cur_z"] > 0.6:
            now_at_edge = "↑↑" if r["cur_dev"] > 0 else "↓↓"
        elif r["cur_z"] > 0.3:
            now_at_edge = "↑" if r["cur_dev"] > 0 else "↓"
        print(fmt.format(
            i, f"{r['score']:.2f}", f"{r['days']}",
            f"{r['round_trips']}", f"{r['excursion_per_day']:.2f}",
            f"{r['median_amp']:.2f}%", f"{r['band_w']:.2f}%",
            f"{r['cur_z']:.2f}", f"{r['recent_24h_range']:.2f}%",
            "yes" if r["recent_active"] else "—",
            now_at_edge, f"{short(a)} <> {short(b)}"
        ))

    # 細節展開 top 5
    print("\n## 推薦 TOP 5 完整資訊\n")
    for r in results[:5]:
        a, b = r["pair"]
        print(f"### {short(a)}  vs  {short(b)}")
        print(f"  資料: {r['n']} bar / {r['days']} 天")
        print(f"  歷史 band [{r['p10']:+.2f}% , {r['p90']:+.2f}%]  median {r['median']:+.2f}%  band 寬 {r['band_w']:.2f}%")
        print(f"  振盪: 共 {r['excursion_count']} 次有效偏離 / {r['round_trips']} 次完整來回 / 每日 {r['excursion_per_day']:.2f} 次")
        print(f"  典型振幅 (median peak): {r['median_amp']:.2f}%")
        print(f"  最近 24h 範圍: {r['recent_24h_range']:.2f}% ({'活躍' if r['recent_active'] else '冷淡'})")
        print(f"  現在 spread: {r['current']:+.2f}%  偏離 median {r['cur_dev']:+.2f}% (z={r['cur_z']:.2f})")
        if r["cur_z"] > 0.5:
            dir_ = "空 A 多 B" if r["cur_dev"] > 0 else "多 A 空 B"
            print(f"  ✅ 現在就有進場點：{dir_}，預期回 median 賺 {abs(r['cur_dev']):.2f}%")
        else:
            print(f"  ⏳ 現在貼近均衡 (z={r['cur_z']:.2f})，等下次發散到 |z|>0.5 再進")
        print()


if __name__ == "__main__":
    main()
