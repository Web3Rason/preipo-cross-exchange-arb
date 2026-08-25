"""K 線 SQLite storage — 跨所歷史 K 統一存取

contract_key 格式：'{exchange}/{deployer or ""}/{raw_symbol}/{settlement}'
  例： 'okx//SPACEX-USDT-SWAP/perpetual'
       'hyperliquid/xyz/xyz:SPCX/perpetual'
       'gateio//SPCX_USDT/perpetual'   ← Gate 永續
       'gateio//SPCX_USDT/spot'        ← Gate 現貨（跟永續同 raw_symbol、需用 settlement 區分）
       'prestocks//PreStocks:SPACEX/spot_solana'

settlement 加入 key 是因為某些交易所（Gate）對同一 raw_symbol 同時上永續和現貨，
不帶 settlement 會撞 PRIMARY KEY，永續/現貨 K 線互相覆蓋。
"""

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "klines.db"
SCHEMA = """
CREATE TABLE IF NOT EXISTS klines (
    contract_key TEXT NOT NULL,
    underlying TEXT NOT NULL,
    interval TEXT NOT NULL,
    ts INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL,
    implied_valuation REAL,
    PRIMARY KEY (contract_key, interval, ts)
);
CREATE INDEX IF NOT EXISTS idx_underlying_interval_ts
    ON klines(underlying, interval, ts);
"""


def make_key(exchange: str, deployer: Optional[str], raw_symbol: str, settlement: str = "perpetual") -> str:
    return f"{exchange}/{deployer or ''}/{raw_symbol}/{settlement}"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_sync():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _upsert_sync(contract_key: str, underlying: str, interval: str, rows: list[dict]):
    """rows: [{'ts':int, 'o':float, 'h':float, 'l':float, 'c':float, 'v':float, 'implied_valuation':float|None}, ...]"""
    if not rows:
        return 0
    conn = _connect()
    try:
        conn.executemany("""
            INSERT INTO klines(contract_key, underlying, interval, ts, open, high, low, close, volume, implied_valuation)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(contract_key, interval, ts) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
                volume=excluded.volume, implied_valuation=excluded.implied_valuation
        """, [
            (contract_key, underlying, interval, r["ts"],
             r.get("o"), r.get("h"), r.get("l"), r.get("c"),
             r.get("v"), r.get("implied_valuation"))
            for r in rows
        ])
        return len(rows)
    finally:
        conn.close()


def _get_klines_sync(contract_key: str, interval: str, from_ts: int = 0, to_ts: Optional[int] = None, limit: int = 100000) -> list[dict]:
    conn = _connect()
    try:
        params = [contract_key, interval, from_ts]
        sql = "SELECT ts, open, high, low, close, volume, implied_valuation FROM klines WHERE contract_key=? AND interval=? AND ts>=?"
        if to_ts is not None:
            sql += " AND ts<=?"
            params.append(to_ts)
        sql += " ORDER BY ts ASC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [{"ts": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5], "implied_valuation": r[6]} for r in rows]
    finally:
        conn.close()


def _get_latest_ts_sync(contract_key: str, interval: str) -> Optional[int]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT MAX(ts) FROM klines WHERE contract_key=? AND interval=?",
            (contract_key, interval)
        ).fetchone()
        return row[0] if row and row[0] else None
    finally:
        conn.close()


def _list_contracts_sync(underlying: str, interval: str) -> list[str]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT contract_key FROM klines WHERE underlying=? AND interval=? ORDER BY contract_key",
            (underlying, interval)
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


# ──── async wrappers ────
async def init_db():
    await asyncio.to_thread(_init_sync)

async def upsert(contract_key: str, underlying: str, interval: str, rows: list[dict]) -> int:
    return await asyncio.to_thread(_upsert_sync, contract_key, underlying, interval, rows)

async def get_klines(contract_key: str, interval: str, from_ts: int = 0, to_ts: Optional[int] = None, limit: int = 100000) -> list[dict]:
    return await asyncio.to_thread(_get_klines_sync, contract_key, interval, from_ts, to_ts, limit)

async def get_latest_ts(contract_key: str, interval: str) -> Optional[int]:
    return await asyncio.to_thread(_get_latest_ts_sync, contract_key, interval)

async def list_contracts(underlying: str, interval: str) -> list[str]:
    return await asyncio.to_thread(_list_contracts_sync, underlying, interval)
