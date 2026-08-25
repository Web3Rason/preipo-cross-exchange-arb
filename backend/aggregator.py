"""讀 preipo_assets.json，串接各個 WS source，產出正規化快照清單"""

import json
import logging
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from aster_source import AsterFuturesSource
from binance_source import BinanceFuturesSource
from bingx_source import BingxSwapSource
from bitget_perp_source import BitgetPerpSource
from bitget_source import BitgetSpotSource
from gate_source import GateFuturesSource
from gate_spot_source import GateSpotSource
from hyperliquid_source import HyperliquidSource
from mexc_source import MexcFuturesSource
from mexc_spot_source import MexcSpotSource
from models import NormalizedMarket
from normalizer import to_implied
from okx_source import OkxSwapSource
from ourbit_source import OurbitFuturesSource
from ourbit_spot_source import OurbitSpotSource
from prestocks_source import PreStocksSource

logger = logging.getLogger(__name__)

ASSETS_FILE = Path(__file__).parent / "data" / "preipo_assets.json"
OUTLIER_THRESHOLD = 0.5


class Aggregator:
    def __init__(self):
        self.assets = self._load_assets()
        self.hl_sources: dict[str, HyperliquidSource] = {}
        self.bitget_source: Optional[BitgetSpotSource] = None
        self.bitget_perp_source: Optional[BitgetPerpSource] = None
        self.okx_source: Optional[OkxSwapSource] = None
        self.aster_source: Optional[AsterFuturesSource] = None
        self.bingx_source: Optional[BingxSwapSource] = None
        self.gate_source: Optional[GateFuturesSource] = None
        self.gate_spot_source: Optional[GateSpotSource] = None
        self.ourbit_source: Optional[OurbitFuturesSource] = None
        self.ourbit_spot_source: Optional[OurbitSpotSource] = None
        self.prestocks_source: Optional[PreStocksSource] = None
        self.binance_source: Optional[BinanceFuturesSource] = None
        self.mexc_source: Optional[MexcFuturesSource] = None
        self.mexc_spot_source: Optional[MexcSpotSource] = None

    def _load_assets(self) -> dict:
        with open(ASSETS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}

    async def start(self):
        # 蒐集各交易所要訂閱的 symbol 集合（spot / perp 拆 key）
        hl_watch: dict[str, set[str]] = {}
        watches: dict[str, set[str]] = {
            "bitget": set(),       # spot (IPO Prime preSPAX/preOPAI)
            "bitget_perp": set(),  # USDT-FUTURES (SPCXUSDT 等 RWA 永續)
            "okx": set(),
            "aster": set(),
            "bingx": set(),
            "gateio": set(),       # futures
            "gateio_spot": set(),
            "ourbit": set(),       # futures
            "ourbit_spot": set(),
            "binance": set(),
            "mexc": set(),         # futures
            "mexc_spot": set(),
        }

        prestocks_pool_map: dict[str, str] = {}

        # 同一個 exchange array 可能混 spot/perp entries，按 settlement 分流
        SPOT_KEY = {"gateio": "gateio_spot", "ourbit": "ourbit_spot", "mexc": "mexc_spot"}

        for underlying, info in self.assets.items():
            for ex, contracts in info.get("exchanges", {}).items():
                for c in contracts:
                    if ex == "hyperliquid":
                        deployer = c["deployer"]
                        hl_watch.setdefault(deployer, set()).add(c["symbol"])
                    elif ex == "bitget":
                        # spot 用 bitget_spot_symbol，perp 用 ws_symbol
                        if c.get("settlement") == "perpetual":
                            sym = c.get("ws_symbol")
                            if sym:
                                watches["bitget_perp"].add(sym)
                        else:
                            sym = c.get("bitget_spot_symbol")
                            if sym:
                                watches["bitget"].add(sym)
                    elif ex == "prestocks":
                        pool = c.get("prestocks_pool")
                        if pool:
                            prestocks_pool_map[underlying] = pool
                    elif ex in SPOT_KEY and c.get("settlement") == "spot":
                        sym = c.get("ws_symbol")
                        if sym:
                            watches[SPOT_KEY[ex]].add(sym)
                    elif ex in watches:
                        sym = c.get("ws_symbol")
                        if sym:
                            watches[ex].add(sym)

        # 啟動所有 source
        for dex, symbols in hl_watch.items():
            src = HyperliquidSource(dex=dex, watch_symbols=symbols)
            await src.start()
            self.hl_sources[dex] = src

        if watches["bitget"]:
            self.bitget_source = BitgetSpotSource(watch_symbols=watches["bitget"])
            await self.bitget_source.start()
        if watches["bitget_perp"]:
            self.bitget_perp_source = BitgetPerpSource(watch_symbols=watches["bitget_perp"])
            await self.bitget_perp_source.start()
        if watches["okx"]:
            self.okx_source = OkxSwapSource(watch_symbols=watches["okx"])
            await self.okx_source.start()
        if watches["aster"]:
            self.aster_source = AsterFuturesSource(watch_symbols=watches["aster"])
            await self.aster_source.start()
        if watches["bingx"]:
            self.bingx_source = BingxSwapSource(watch_symbols=watches["bingx"])
            await self.bingx_source.start()
        if watches["gateio"]:
            self.gate_source = GateFuturesSource(watch_symbols=watches["gateio"])
            await self.gate_source.start()
        if watches["gateio_spot"]:
            self.gate_spot_source = GateSpotSource(watch_symbols=watches["gateio_spot"])
            await self.gate_spot_source.start()
        if watches["ourbit"]:
            self.ourbit_source = OurbitFuturesSource(watch_symbols=watches["ourbit"])
            await self.ourbit_source.start()
        if watches["ourbit_spot"]:
            self.ourbit_spot_source = OurbitSpotSource(watch_symbols=watches["ourbit_spot"])
            await self.ourbit_spot_source.start()
        if prestocks_pool_map:
            self.prestocks_source = PreStocksSource(pool_map=prestocks_pool_map)
            await self.prestocks_source.start()
        if watches["binance"]:
            self.binance_source = BinanceFuturesSource(watch_symbols=watches["binance"])
            await self.binance_source.start()
        if watches["mexc"]:
            self.mexc_source = MexcFuturesSource(watch_symbols=watches["mexc"])
            await self.mexc_source.start()
        if watches["mexc_spot"]:
            self.mexc_spot_source = MexcSpotSource(watch_symbols=watches["mexc_spot"])
            await self.mexc_spot_source.start()

        # 等就緒
        for src in self.hl_sources.values():
            await src.wait_ready(timeout=15.0)
        for src in (self.bitget_source, self.bitget_perp_source, self.okx_source, self.aster_source,
                    self.bingx_source, self.gate_source, self.gate_spot_source,
                    self.ourbit_source, self.ourbit_spot_source,
                    self.prestocks_source, self.binance_source, self.mexc_source,
                    self.mexc_spot_source):
            if src:
                await src.wait_ready(timeout=15.0)

    def snapshot(self) -> list[NormalizedMarket]:
        out: list[NormalizedMarket] = []
        now = datetime.now(timezone.utc)

        for underlying, info in self.assets.items():
            company_name = info.get("company_name", underlying)
            total_supply = info.get("total_supply_shares", 0) or 0

            for exchange, contracts in info.get("exchanges", {}).items():
                for c in contracts:
                    raw_symbol = c["symbol"]
                    pricing_basis = c["pricing_basis"]
                    multiplier = c.get("multiplier", 1) or 1
                    settlement = c.get("settlement", "perpetual")

                    raw_mark = raw_bid = raw_ask = None
                    funding = oi = day_vlm = None
                    max_lev = c.get("max_leverage")
                    api_implied_override = None  # PreStocks 官方 API 直給 implied_valuation 時填入

                    if exchange == "hyperliquid":
                        src = self.hl_sources.get(c.get("deployer"))
                        if src:
                            snap = src.snapshot(raw_symbol)
                            if snap:
                                ctx, book, meta = snap["ctx"], snap["book"], snap["meta"]
                                raw_mark = ctx.get("mark_px")
                                raw_bid = book.get("bid")
                                raw_ask = book.get("ask")
                                funding = ctx.get("funding")
                                oi = ctx.get("open_interest")
                                day_vlm = ctx.get("day_ntl_vlm")
                                max_lev = meta.get("max_leverage") or max_lev

                    elif exchange == "bitget":
                        # spot 用 bitget_spot_symbol；perp 用 ws_symbol
                        if c.get("settlement") == "perpetual" and self.bitget_perp_source:
                            t = self.bitget_perp_source.snapshot(c.get("ws_symbol", ""))
                            if t:
                                raw_mark = t.get("mark") or t.get("last")
                                raw_bid = t.get("bid")
                                raw_ask = t.get("ask")
                                day_vlm = t.get("usdt_vol")
                                funding = t.get("funding")
                        elif self.bitget_source:
                            t = self.bitget_source.snapshot(c.get("bitget_spot_symbol", ""))
                            if t:
                                raw_mark = t.get("last")
                                raw_bid = t.get("bid")
                                raw_ask = t.get("ask")
                                day_vlm = t.get("usdt_vol")

                    elif exchange == "okx" and self.okx_source:
                        t = self.okx_source.snapshot(c.get("ws_symbol", ""))
                        if t:
                            raw_mark = t.get("mark")
                            raw_bid = t.get("bid")
                            raw_ask = t.get("ask")
                            day_vlm = t.get("usdt_vol")
                            funding = t.get("funding")

                    elif exchange == "aster" and self.aster_source:
                        t = self.aster_source.snapshot(c.get("ws_symbol", ""))
                        if t:
                            raw_mark = t.get("mark") or t.get("last")
                            raw_bid = t.get("bid")
                            raw_ask = t.get("ask")
                            day_vlm = t.get("usdt_vol")
                            funding = t.get("funding")

                    elif exchange == "bingx" and self.bingx_source:
                        t = self.bingx_source.snapshot(c.get("ws_symbol", ""))
                        if t:
                            raw_mark = t.get("mark") or t.get("last")
                            raw_bid = t.get("bid")
                            raw_ask = t.get("ask")
                            day_vlm = t.get("usdt_vol")
                            funding = t.get("funding")

                    elif exchange == "gateio":
                        src = self.gate_spot_source if c.get("settlement") == "spot" else self.gate_source
                        if src:
                            t = src.snapshot(c.get("ws_symbol", ""))
                            if t:
                                raw_mark = t.get("mark") or t.get("last")
                                raw_bid = t.get("bid")
                                raw_ask = t.get("ask")
                                day_vlm = t.get("usdt_vol")
                                funding = t.get("funding")

                    elif exchange == "ourbit":
                        src = self.ourbit_spot_source if c.get("settlement") == "spot" else self.ourbit_source
                        if src:
                            t = src.snapshot(c.get("ws_symbol", ""))
                            if t:
                                raw_bid = t.get("bid")
                                raw_ask = t.get("ask")
                                day_vlm = t.get("usdt_vol")
                                funding = t.get("funding")
                                # Ourbit spot miniTicker 對冷門 PreIPO 對不推送（last/mark 一直 None）
                                # → 用 bookTicker 的 bid/ask 中價回填，避免 UI 顯示「—」
                                raw_mark = t.get("mark") or t.get("last")
                                if raw_mark is None and raw_bid and raw_ask:
                                    raw_mark = (raw_bid + raw_ask) / 2

                    elif exchange == "prestocks" and self.prestocks_source:
                        t = self.prestocks_source.snapshot(underlying)
                        if t:
                            raw_mark = t.get("mark") or t.get("last")
                            raw_bid = t.get("bid")
                            raw_ask = t.get("ask")
                            day_vlm = t.get("usdt_vol")
                            api_implied_override = t.get("implied_valuation")

                    elif exchange == "binance" and self.binance_source:
                        t = self.binance_source.snapshot(c.get("ws_symbol", ""))
                        if t:
                            raw_mark = t.get("mark") or t.get("last")
                            raw_bid = t.get("bid")
                            raw_ask = t.get("ask")
                            day_vlm = t.get("usdt_vol")
                            funding = t.get("funding")
                            # Binance TRADIFI funding=0 是設計，不要當「沒資料」

                    elif exchange == "mexc":
                        src = self.mexc_spot_source if c.get("settlement") == "spot" else self.mexc_source
                        if src:
                            t = src.snapshot(c.get("ws_symbol", ""))
                            if t:
                                raw_mark = t.get("mark") or t.get("last")
                                raw_bid = t.get("bid")
                                raw_ask = t.get("ask")
                                day_vlm = t.get("usdt_vol")
                                funding = t.get("funding")

                    extra = {
                        "subscription_price": c.get("subscription_price"),
                        "initial_valuation_usd": c.get("initial_valuation_usd"),
                    }
                    # PreStocks 官方 API 直接給 implied_valuation → 跳過 normalizer 公式
                    if api_implied_override is not None:
                        implied_val = api_implied_override
                        implied_ps = (implied_val / total_supply) if total_supply else None
                    else:
                        implied_ps, implied_val = to_implied(
                            raw_mark, pricing_basis, total_supply, multiplier, extra
                        )
                    # 算 raw → per-share 換算因子（讓 UI 顯示「raw × X = per-share」）
                    per_share_factor = None
                    if api_implied_override is not None:
                        # API 直給 implied，per_share_factor = implied_per_share / raw_mark
                        if implied_ps and raw_mark:
                            per_share_factor = implied_ps / raw_mark
                    elif total_supply:
                        if pricing_basis == "per_share_usd":
                            per_share_factor = 1.0 / multiplier if multiplier else None
                        elif pricing_basis == "valuation_billion_usd":
                            per_share_factor = 1e9 / total_supply
                        elif pricing_basis == "valuation_per_billion_usd":
                            per_share_factor = 1e9 * multiplier / total_supply
                        elif pricing_basis == "subscription_proxy":
                            sp = extra.get("subscription_price")
                            iv = extra.get("initial_valuation_usd")
                            if sp and iv:
                                per_share_factor = iv / sp / total_supply

                    source_family = c.get("source_family", "perp_synth")
                    out.append(NormalizedMarket(
                        underlying=underlying,
                        company_name=company_name,
                        exchange=exchange,
                        deployer=c.get("deployer"),
                        raw_symbol=raw_symbol,
                        pricing_basis=pricing_basis,
                        total_supply_shares=total_supply,
                        multiplier=multiplier,
                        contract_size=c.get("contract_size"),
                        per_share_factor=per_share_factor,
                        confidence=c.get("confidence", "inferred"),
                        confidence_note=c.get("confidence_note"),
                        source_url=c.get("source_url"),
                        source_type=c.get("source_type"),
                        raw_mark_px=raw_mark,
                        raw_bid=raw_bid,
                        raw_ask=raw_ask,
                        implied_per_share_usd=implied_ps,
                        implied_valuation_usd=implied_val,
                        funding_rate_hourly=funding,
                        open_interest=oi,
                        day_ntl_vlm_usd=day_vlm,
                        max_leverage=max_lev,
                        settlement=settlement,
                        source_family=source_family,
                        updated_at=now,
                    ))

        self._mark_outliers(out)
        return out

    @staticmethod
    def _mark_outliers(markets: list[NormalizedMarket]):
        """按 (underlying, source_family) 分組算中位數，偏離 > 50% 標 outlier。

        Why: 不同 source_family（合成永續 vs SPV 鏡像 vs IPO Prime）天然存在結構性
        折價/溢價（如 PreStocks 系列整體折價 60-70%）。若混在一起算中位數會誤把
        真實市場結構性折價當成計算錯誤。outlier 真正的意義是「同 family 內 row
        跟其他 row 不一致 → 八成是 multiplier 或 pricing_basis 設錯」。
        """
        by_group: dict[tuple, list[NormalizedMarket]] = {}
        for m in markets:
            if m.implied_valuation_usd is not None:
                by_group.setdefault((m.underlying, m.source_family), []).append(m)

        for (und, family), rows in by_group.items():
            if len(rows) < 2:
                continue
            vals = [r.implied_valuation_usd for r in rows]
            med = statistics.median(vals)
            if med <= 0:
                continue
            for r in rows:
                dev = abs(r.implied_valuation_usd - med) / med
                if dev > OUTLIER_THRESHOLD:
                    r.is_outlier = True
                    r.outlier_note = (
                        f"在 {family} 系列內隱含估值 ${r.implied_valuation_usd/1e12:.2f}T "
                        f"偏離 family 中位數 ${med/1e12:.2f}T 達 {dev*100:.0f}%，"
                        f"可能 pricing_basis / multiplier 設錯"
                    )
