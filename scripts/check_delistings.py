"""定期巡查各交易所公告中心，命中『下架/暫停 + 我們追蹤的標的』時推 TG。

用法：
    python scripts/check_delistings.py              # 預設：掃 + 命中推通知
    python scripts/check_delistings.py --dry-run    # 只印不推
    python scripts/check_delistings.py --rescan     # 忽略 seen.json，全部重掃

掃描範圍（MVP）：
    ✅ Binance — bapi JSON API（catalogId=161 Delisting）
    ✅ BingX   — sitemap-support.xml diff，再 fetch title regex
    ⚠️ MEXC / OKX / Bitget / Gate — 被 Cloudflare/Akamai 擋，需 cloudscraper（pip install cloudscraper）才可開啟，
       見 SOURCES_TODO 段落

去重：scripts/delisting_seen.json 記錄已通知的 article URL，避免重複推。
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
ASSETS_FILE = ROOT / "backend" / "data" / "preipo_assets.json"
SEEN_FILE = Path(__file__).resolve().parent / "delisting_seen.json"
# 通知腳本：設環境變數 NOTIFY_SCRIPT 指向你自己的推送程式（會以
# `python <script> "<訊息>"` 呼叫）。沒設就只印出來，不影響巡查功能。
NOTIFY_SCRIPT = os.environ.get("NOTIFY_SCRIPT", "")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

DELIST_KEYWORDS = re.compile(
    r"(delist|delisting|removal|cease trading|discontinu|suspend|"
    r"下架|終止|暫停|停止上架|退市|移除)",
    re.IGNORECASE,
)


def load_tracked_keywords() -> set[str]:
    """從 preipo_assets.json 撈追蹤的 underlying / company / symbol / base 名稱"""
    data = json.loads(ASSETS_FILE.read_text(encoding="utf-8"))
    kw = set()
    for und, info in data.items():
        if und.startswith("_"):
            continue
        kw.add(und.upper())
        if info.get("company_name"):
            kw.add(info["company_name"].upper())
        for _ex, lst in info.get("exchanges", {}).items():
            for c in lst:
                sym = (c.get("symbol") or "").upper()
                if sym:
                    kw.add(sym)
                    base = re.sub(r"[-_/]?USDT.*", "", sym)
                    base = re.sub(r"^NCSK|USD$", "", base)
                    if base and len(base) >= 3:
                        kw.add(base)
    discard = {"PRE", "PERSHARE", "USDT", "USD", "USDC"}
    return {k for k in kw if k not in discard}


def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen(seen: set[str]):
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


def match_tracked(text: str, keywords: set[str]) -> list[str]:
    """回傳命中的標的清單"""
    upper = text.upper()
    return sorted([k for k in keywords if k in upper])


async def fetch(session: aiohttp.ClientSession, url: str, timeout: int = 20) -> str:
    headers = {"User-Agent": USER_AGENT}
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.text()


# ───────────────────────── Binance ─────────────────────────
async def scan_binance(_session, keywords: set[str]) -> list[dict]:
    """Binance bapi JSON：catalogId=161 是 Delisting category。用獨立 session 避免共用 cookie 觸發 anti-bot"""
    url = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query?type=1&pageNo=1&pageSize=20&catalogId=161"
    out = []
    try:
        async with aiohttp.ClientSession() as session:
            h = {"User-Agent": USER_AGENT}
            async with session.get(url, headers=h) as r:
                status = r.status
                text = await r.text()
        if not text.strip():
            print(f"  [binance] empty body (status={status}, len={len(text)})", file=sys.stderr)
            return out
        if text.lstrip().startswith("<"):
            print(f"  [binance] non-JSON body: {text[:200]}", file=sys.stderr)
            return out
        data = json.loads(text)
        catalogs = data.get("data", {}).get("catalogs", [])
        scanned = 0
        for cat in catalogs:
            for art in cat.get("articles", []):
                scanned += 1
                title = art.get("title", "")
                code = art.get("code", "")
                hits = match_tracked(title, keywords)
                if hits:
                    out.append({
                        "exchange": "Binance",
                        "kind": "delist",
                        "title": title,
                        "url": f"https://www.binance.com/en/support/announcement/{code}",
                        "hits": hits,
                        "release_ts": art.get("releaseDate"),
                    })
        print(f"  [binance] scanned {scanned} delisting articles, hits={len(out)}")
    except Exception as e:
        print(f"  [binance] error: {e}", file=sys.stderr)
    return out


# ───────────────────────── BingX ─────────────────────────
async def scan_bingx(session, keywords: set[str], seen: set[str]) -> list[dict]:
    """BingX 用 sitemap diff，新文章 fetch title 判斷"""
    sitemap_url = "https://bingx.com/en/sitemap-support.xml"
    out = []
    try:
        sm = await fetch(session, sitemap_url)
        root = ET.fromstring(sm)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        urls = [u.text for u in root.findall("sm:url/sm:loc", ns) if u.text]
        # 抽 article ID 排序、只看最新 20 個未 seen 的（避免初次跑爆量）
        candidates = [u for u in urls if "/support/articles/" in u and u not in seen]
        candidates.sort(key=lambda u: int(re.search(r"/(\d+)$", u).group(1)) if re.search(r"/(\d+)$", u) else 0, reverse=True)
        candidates = candidates[:20]
        print(f"  [bingx] sitemap urls={len(urls)}, new candidates={len(candidates)}")

        for art_url in candidates:
            try:
                html = await fetch(session, art_url, timeout=10)
                m = re.search(r'<title>([^<]+)</title>', html)
                title = m.group(1).strip() if m else ""
                if not DELIST_KEYWORDS.search(title):
                    continue
                hits = match_tracked(title, keywords)
                if hits:
                    out.append({
                        "exchange": "BingX",
                        "kind": "delist",
                        "title": title,
                        "url": art_url,
                        "hits": hits,
                        "release_ts": None,
                    })
            except Exception as e:
                print(f"  [bingx] fetch {art_url} error: {e}", file=sys.stderr)
    except Exception as e:
        print(f"  [bingx] error: {e}", file=sys.stderr)
    return out


# ───────────────────────── 合約探活（補強 4 家被擋的公告爬取） ─────────────────────────
# 對每個追蹤 symbol 打 REST contract detail，回 dead code = 已下架/暫停
# 比公告爬取精確：拿到的是「結果」而非「預告」

DEAD_PROBES = {
    "mexc": {
        "url": "https://contract.mexc.com/api/v1/contract/detail?symbol={sym}",
        "dead_codes": [1001],  # Contract not exists
        "field": "code",
    },
    "bingx": {
        "url": "https://open-api.bingx.com/openApi/swap/v2/quote/ticker?symbol={sym}",
        "dead_codes": [109415],  # is pause currently
        "field": "code",
    },
    "binance": {
        # 公告爬蟲已涵蓋；無需探活（且 binance pre-IPO 永續 symbol 是穩定的）
    },
    "gateio": {
        "url": "https://api.gateio.ws/api/v4/futures/usdt/contracts/{sym}",
        "dead_status": [404],  # HTTP 404 = 合約不存在
    },
    "okx": {
        "url": "https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId={sym}",
        "dead_empty": True,  # data 為 [] 表示已下架
    },
    "bitget": {
        "url": "https://api.bitget.com/api/v2/mix/market/symbol-price?symbol={sym}&productType=USDT-FUTURES",
        "dead_codes": ["40034", "22002"],  # 40034=symbol not exist
    },
}


async def probe_alive(session, exchange: str, ws_symbol: str) -> tuple[bool, str]:
    """回 (alive, msg)。alive=False 表示合約已下架/暫停"""
    cfg = DEAD_PROBES.get(exchange)
    if not cfg or "url" not in cfg:
        return True, "no_probe_config"
    url = cfg["url"].format(sym=ws_symbol)
    try:
        async with session.get(url, headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if "dead_status" in cfg and r.status in cfg["dead_status"]:
                return False, f"HTTP {r.status}"
            text = await r.text()
        if not text.strip():
            return True, "empty"
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return True, "non-json"
        if cfg.get("dead_empty"):
            d = data.get("data") if isinstance(data, dict) else None
            if d == [] or d is None:
                return False, "data empty"
        if cfg.get("dead_codes"):
            code = str(data.get(cfg.get("field", "code"), ""))
            if code in [str(c) for c in cfg["dead_codes"]]:
                msg = data.get("msg") or data.get("message") or ""
                return False, f"code={code} {msg[:80]}"
        return True, "alive"
    except Exception as e:
        return True, f"error: {e}"  # 探活失敗不當下架（保守）


async def scan_dead_contracts(session, assets_data: dict) -> list[dict]:
    """逐 entry 探活，發現下架/暫停回報"""
    out = []
    for und, info in assets_data.items():
        if und.startswith("_"):
            continue
        for ex, lst in info.get("exchanges", {}).items():
            if ex not in DEAD_PROBES or "url" not in DEAD_PROBES[ex]:
                continue
            for c in lst:
                # 只對 perpetual 探活；spot 與 spot_solana 各家 endpoint 差異大、暫不支援
                if c.get("settlement") != "perpetual":
                    continue
                ws_sym = c.get("ws_symbol") or c.get("symbol")
                if not ws_sym:
                    continue
                alive, msg = await probe_alive(session, ex, ws_sym)
                if not alive:
                    out.append({
                        "exchange": ex,
                        "kind": "dead",
                        "title": f"[合約探活] {ex}/{ws_sym} 已失活（{msg}）",
                        "url": f"probe://{ex}/{ws_sym}",
                        "hits": [und],
                        "release_ts": None,
                    })
    print(f"  [probe] dead contracts found={len(out)}")
    return out


# ───────────────────────── 新上架掃描 ─────────────────────────
# 對每家交易所撈全合約列表，篩含追蹤 base name 的 symbol，跟 preipo_assets 已有的 diff
# 新增的 → 推 TG「新合約發現」

LONG_BASES = ["SPACEX", "OPENAI", "ANTHROPIC", "ANDURIL", "NEURALINK", "KALSHI", "POLYMARKET"]
SHORT_BASES = ["SPCX", "SPAX"]  # 短 base 要求 word boundary 才命中，避免 EOSPAX/WAVESPAX 誤報


def _has_base(sym: str) -> str | None:
    """回傳命中的 base name；無則 None"""
    s = sym.upper()
    for b in LONG_BASES:
        if b in s:
            return b
    for b in SHORT_BASES:
        # 前不可是字母（允許數字、符號、字串邊界）；後不可是字母
        if re.search(r'(?<![A-Z])' + b + r'(?![A-Z])', s):
            return b
    return None


def _known_symbols(assets_data: dict) -> dict[str, set[str]]:
    """回 {exchange: {symbol1, symbol2, ...}}，從 preipo_assets 已記錄的"""
    out: dict[str, set[str]] = {}
    for und, info in assets_data.items():
        if und.startswith("_"):
            continue
        for ex, lst in info.get("exchanges", {}).items():
            out.setdefault(ex, set())
            for c in lst:
                for k in ("symbol", "ws_symbol", "bitget_spot_symbol"):
                    v = c.get(k)
                    if v:
                        out[ex].add(v)
    return out


async def _list_markets_binance(session) -> list[tuple[str, str, str]]:
    """回 [(symbol, market_type, status), ...]，market_type 為 perp/spot"""
    out = []
    try:
        for url, mt in [
            ("https://fapi.binance.com/fapi/v1/exchangeInfo", "perp"),
            ("https://api.binance.com/api/v3/exchangeInfo", "spot"),
        ]:
            text = await fetch(session, url)
            data = json.loads(text)
            for s in data.get("symbols", []):
                out.append((s.get("symbol", ""), mt, s.get("status", "")))
    except Exception as e:
        print(f"  [binance markets] error: {e}", file=sys.stderr)
    return out


async def _list_markets_bingx(session) -> list[tuple[str, str, str]]:
    out = []
    try:
        text = await fetch(session, "https://open-api.bingx.com/openApi/swap/v2/quote/contracts")
        data = json.loads(text)
        for c in data.get("data", []):
            out.append((c.get("symbol", ""), "perp", str(c.get("status", ""))))
    except Exception as e:
        print(f"  [bingx markets] error: {e}", file=sys.stderr)
    return out


async def _list_markets_mexc(session) -> list[tuple[str, str, str]]:
    out = []
    try:
        text = await fetch(session, "https://contract.mexc.com/api/v1/contract/detail")
        data = json.loads(text)
        for c in data.get("data", []):
            out.append((c.get("symbol", ""), "perp", str(c.get("state", ""))))
    except Exception as e:
        print(f"  [mexc markets] error: {e}", file=sys.stderr)
    return out


async def _list_markets_okx(session) -> list[tuple[str, str, str]]:
    out = []
    try:
        text = await fetch(session, "https://www.okx.com/api/v5/public/instruments?instType=SWAP")
        data = json.loads(text)
        for c in data.get("data", []):
            out.append((c.get("instId", ""), "perp", c.get("state", "")))
    except Exception as e:
        print(f"  [okx markets] error: {e}", file=sys.stderr)
    return out


async def _list_markets_hyperliquid(session) -> list[tuple[str, str, str]]:
    """xyz / vntl 兩個 HIP-3 deployer 各撈 meta"""
    out = []
    try:
        for dex in ("xyz", "vntl"):
            async with session.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "meta", "dex": dex},
                headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                data = await r.json()
            for u in data.get("universe", []):
                name = u.get("name", "")  # HL meta 的 name 已含 dex 前綴（如 "xyz:SPCX"）
                if not name:
                    continue
                status = "delisted" if u.get("isDelisted") else "live"
                out.append((name, "perp", status))
    except Exception as e:
        print(f"  [hyperliquid markets] error: {e}", file=sys.stderr)
    return out


MARKET_FETCHERS = {
    "binance": _list_markets_binance,
    "bingx": _list_markets_bingx,
    "mexc": _list_markets_mexc,
    "okx": _list_markets_okx,
    "hyperliquid": _list_markets_hyperliquid,
}


async def scan_new_listings(session, assets_data: dict) -> list[dict]:
    """跑每家市場列表，篩出含追蹤 base 但不在 preipo_assets 的 symbol"""
    known = _known_symbols(assets_data)
    out = []
    total_scanned = 0
    for ex, fetcher in MARKET_FETCHERS.items():
        markets = await fetcher(session)
        total_scanned += len(markets)
        for sym, mt, status in markets:
            base = _has_base(sym)
            if not base:
                continue
            if sym in known.get(ex, set()):
                continue
            out.append({
                "exchange": ex,
                "kind": "new",
                "title": f"[新合約發現] {ex}/{sym} ({mt}, status={status}) 命中 {base}",
                "url": f"newlist://{ex}/{sym}",
                "hits": [base],
                "release_ts": None,
            })
    print(f"  [new_listings] scanned {total_scanned} markets across {len(MARKET_FETCHERS)} exchanges, new={len(out)}")
    return out


# ───────────────────────── 通知 ─────────────────────────
def send_notify(payload: dict, dry_run: bool):
    title = payload["title"]
    hits = ", ".join(payload["hits"])
    kind = payload.get("kind", "alert")
    tag_map = {
        "delist": "[5032 下架警報]",
        "dead": "[5032 合約失活]",
        "new": "[5032 新合約上架]",
    }
    tag = tag_map.get(kind, "[5032 警報]")
    msg = f"{tag} {payload['exchange']}：命中 {hits}\n{title}\n{payload['url']}"
    print(f">>> {msg}\n")
    if dry_run:
        return
    if not NOTIFY_SCRIPT:
        return  # 沒設定推送腳本，上面已經印出來了
    try:
        subprocess.run(
            ["python", NOTIFY_SCRIPT, msg],
            check=True,
            capture_output=True,
            timeout=15,
        )
    except Exception as e:
        print(f"  notify error: {e}", file=sys.stderr)


# ───────────────────────── main ─────────────────────────
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只印不推 TG")
    parser.add_argument("--rescan", action="store_true", help="忽略 seen.json，全部重掃")
    args = parser.parse_args()

    keywords = load_tracked_keywords()
    print(f"[check_delistings] tracked keywords ({len(keywords)}): {sorted(keywords)}\n")

    seen = set() if args.rescan else load_seen()

    assets_data = json.loads(ASSETS_FILE.read_text(encoding="utf-8"))

    async with aiohttp.ClientSession() as session:
        results = []
        results += await scan_binance(session, keywords)
        results += await scan_bingx(session, keywords, seen)
        results += await scan_dead_contracts(session, assets_data)
        results += await scan_new_listings(session, assets_data)

    new_hits = []
    for r in results:
        key = r["url"]
        if key in seen:
            continue
        seen.add(key)
        new_hits.append(r)
        send_notify(r, dry_run=args.dry_run)

    if not args.dry_run:
        save_seen(seen)

    if not new_hits:
        print(f"[check_delistings] {datetime.now().isoformat()} — 無新命中（總 seen={len(seen)}）")
    else:
        print(f"[check_delistings] {datetime.now().isoformat()} — 新命中 {len(new_hits)} 筆")


if __name__ == "__main__":
    asyncio.run(main())
