"""把不同 pricing_basis 的原始報價換算成統一的『隱含每股價』與『公司隱含總估值』"""

from typing import Optional


def to_implied(
    raw_px: Optional[float],
    pricing_basis: str,
    total_supply_shares: int,
    multiplier: float = 1.0,
    extra: Optional[dict] = None,
) -> tuple[Optional[float], Optional[float]]:
    """回 (implied_per_share_usd, implied_valuation_usd)。raw_px 缺 → 兩個 None。

    extra 可帶各 basis 特定參數：
      - subscription_proxy: {"subscription_price": float, "initial_valuation_usd": float}
    """
    if raw_px is None:
        return None, None
    extra = extra or {}

    if pricing_basis == "per_share_usd":
        per_share = raw_px / multiplier if multiplier else raw_px
        valuation = per_share * total_supply_shares if total_supply_shares else None
        return per_share, valuation

    if pricing_basis == "valuation_billion_usd":
        valuation = raw_px * 1e9
        per_share = valuation / total_supply_shares if total_supply_shares else None
        return per_share, valuation

    if pricing_basis == "valuation_per_billion_usd":
        valuation = raw_px * 1e9 * multiplier if multiplier else raw_px * 1e9
        per_share = valuation / total_supply_shares if total_supply_shares else None
        return per_share, valuation

    if pricing_basis == "spot_token_per_share":
        per_share = raw_px / multiplier if multiplier else raw_px
        valuation = per_share * total_supply_shares if total_supply_shares else None
        return per_share, valuation

    if pricing_basis == "api_direct":
        # PreStocks 官方 API 已給 implied_valuation，這個分支不該被呼叫到（aggregator 會跳過 to_implied）
        # 留 fallback：raw=token_price 但沒 conversion 資訊，回 None
        return None, None

    if pricing_basis == "subscription_proxy":
        # Republic / Bitget IPO Prime 模式：token 沒有固定 token→share ratio
        # 但 implied valuation = current_price / subscription_price × initial_valuation
        # 例如 preSPAX 訂閱 $650 對應 $1.5T 估值，現價 $856 → $1.5T × 856/650 ≈ $1.97T
        sub_px = extra.get("subscription_price")
        init_val = extra.get("initial_valuation_usd")
        if not sub_px or not init_val:
            return None, None
        valuation = raw_px / sub_px * init_val
        per_share = valuation / total_supply_shares if total_supply_shares else None
        return per_share, valuation

    return None, None
