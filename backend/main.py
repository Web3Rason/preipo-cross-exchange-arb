"""5032 PreIPO 後端：FastAPI + WS 推送"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from pathlib import Path

# aiodns（c-ares）在 Windows 讀不到系統 DNS 設定，會讓 aiohttp 預設的 AsyncResolver
# 全數「Could not contact DNS servers」失敗（prestocks / mexc_spot / bingx / kline 回補等
# 所有 aiohttp 來源因此抓不到資料）。強制改用 ThreadedResolver，走 socket.getaddrinfo，
# 與 websockets / urllib 同一條解析路徑。必須在任何 ClientSession 建立前設定。
import aiohttp.connector
import aiohttp.resolver
aiohttp.connector.DefaultResolver = aiohttp.resolver.ThreadedResolver

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from aggregator import Aggregator
from kline_orchestrator import KlineOrchestrator
import kline_storage as ST

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

aggregator = Aggregator()
kline_orch = KlineOrchestrator()
ws_clients: set[WebSocket] = set()


async def broadcast_loop():
    """每 2 秒打包 snapshot 推送給所有 WS 訂閱者"""
    while True:
        try:
            snap = aggregator.snapshot()
            payload = json.dumps({
                "type": "snapshot",
                "markets": [m.model_dump(mode="json") for m in snap],
            })
            dead = []
            for ws in ws_clients:
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                ws_clients.discard(ws)
        except Exception as e:
            logger.exception(f"broadcast 失敗: {e}")
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await aggregator.start()
    await kline_orch.start()
    task = asyncio.create_task(broadcast_loop())
    logger.info("5032 PreIPO 後端啟動完成（含 kline backfill）")
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/markets")
async def get_markets():
    snap = aggregator.snapshot()
    return {"markets": [m.model_dump(mode="json") for m in snap]}


@app.get("/api/contracts")
async def get_contracts(underlying: str, interval: str = "1m"):
    """列出指定 underlying 內有歷史 K 資料的合約 key"""
    keys = await ST.list_contracts(underlying, interval)
    return {"underlying": underlying, "interval": interval, "contracts": keys}


@app.get("/api/klines")
async def get_klines(contract_key: str, interval: str = "1m", lookback_hours: float = 24, limit: int = 100000):
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(lookback_hours * 3600 * 1000)
    rows = await ST.get_klines(contract_key, interval, from_ts=start_ms, to_ts=end_ms, limit=limit)
    return {"contract_key": contract_key, "interval": interval, "klines": rows}


@app.get("/api/spread")
async def get_spread(a: str, b: str, interval: str = "1m"):
    """對齊兩 contract 的全歷史 K，回傳 implied_valuation 雙線 + spread% + raw close（供下游下單服務算倍率用）"""
    rows_a = await ST.get_klines(a, interval, from_ts=0, limit=200000)
    rows_b = await ST.get_klines(b, interval, from_ts=0, limit=200000)
    map_a = {r["ts"]: (r["implied_valuation"], r["c"]) for r in rows_a if r["implied_valuation"]}
    map_b = {r["ts"]: (r["implied_valuation"], r["c"]) for r in rows_b if r["implied_valuation"]}
    common = sorted(set(map_a.keys()) & set(map_b.keys()))
    series = []
    for ts in common:
        va, ra = map_a[ts]
        vb, rb = map_b[ts]
        spread_pct = (va - vb) / vb * 100 if vb else None
        series.append({"ts": ts, "a": va, "b": vb, "raw_a": ra, "raw_b": rb, "spread_pct": spread_pct})
    return {"a": a, "b": b, "interval": interval, "points": len(series), "series": series}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.add(websocket)
    try:
        snap = aggregator.snapshot()
        await websocket.send_text(json.dumps({
            "type": "snapshot",
            "markets": [m.model_dump(mode="json") for m in snap],
        }))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(websocket)


DIST_DIR = Path(__file__).parent.parent / "frontend" / "dist"
if DIST_DIR.exists():
    app.mount("/", StaticFiles(directory=str(DIST_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=5032)
