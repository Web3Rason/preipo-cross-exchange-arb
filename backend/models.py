from typing import Optional, Literal
from pydantic import BaseModel
from datetime import datetime


PricingBasis = Literal[
    "per_share_usd",
    "valuation_billion_usd",
    "valuation_per_billion_usd",
    "spot_token_per_share",
    "subscription_proxy",
    "api_direct",  # PreStocks 官方 API 直接回 impliedValuation，跳過 normalizer 公式
]


class NormalizedMarket(BaseModel):
    """單一 PreIPO 合約的標準化即時報價"""
    underlying: str
    company_name: str
    exchange: str
    deployer: Optional[str] = None
    raw_symbol: str
    pricing_basis: PricingBasis

    total_supply_shares: int
    multiplier: float = 1.0          # normalizer 公式用的計算乘數
    contract_size: Optional[float] = None  # 該交易所實際合約規格（1 張對應幾個 underlying unit）
    per_share_factor: Optional[float] = None  # raw_mark_px × 此因子 = implied_per_share_usd

    raw_mark_px: Optional[float] = None
    raw_bid: Optional[float] = None
    raw_ask: Optional[float] = None

    implied_per_share_usd: Optional[float] = None
    implied_valuation_usd: Optional[float] = None

    funding_rate_hourly: Optional[float] = None
    open_interest: Optional[float] = None
    day_ntl_vlm_usd: Optional[float] = None
    max_leverage: Optional[float] = None
    settlement: str = "perpetual"
    source_family: str = "perp_synth"  # perp_synth / spv_mirror / ipo_prime
    confidence: str = "inferred"  # verified=官方明文 / partial=部分口徑 / inferred=反推
    confidence_note: Optional[str] = None
    source_url: Optional[str] = None  # 對應 confidence_note 的原始官方公告/docs URL
    source_type: Optional[str] = None  # official_announcement / official_docs / official_twitter / third_party_news / exchange_trade_page
    is_outlier: bool = False  # 同 source_family 內偏離中位數 > 50%
    outlier_note: Optional[str] = None
    updated_at: datetime
