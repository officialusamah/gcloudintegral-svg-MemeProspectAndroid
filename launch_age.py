from __future__ import annotations

from .models import PairSnapshot
from .providers import DEX, DEX_CHAIN, MarketProviders, _int, _num


def install() -> None:
    """Use the earliest valid pool creation time as token launch age."""
    if getattr(MarketProviders, "_earliest_launch_age_installed", False):
        return

    async def _pair_snapshot_earliest_age(self, c):
        chain = DEX_CHAIN.get(c.chain)
        if not chain:
            return None

        r = await self.client.get(f"{DEX}/token-pairs/v1/{chain}/{c.address}")
        r.raise_for_status()
        pairs = r.json()
        if not isinstance(pairs, list) or not pairs:
            return None

        def liq(p):
            return _num((p.get("liquidity") or {}).get("usd"))

        # Current market metrics still come from the deepest pool.
        p = max(pairs, key=liq)

        # Launch age comes from the earliest valid pool timestamp.
        created_times = [
            _int(x.get("pairCreatedAt"))
            for x in pairs
            if isinstance(x, dict) and _int(x.get("pairCreatedAt")) > 0
        ]
        earliest_created_at_ms = (
            min(created_times)
            if created_times
            else _int(p.get("pairCreatedAt"))
        )

        base = p.get("baseToken") or {}
        quote = p.get("quoteToken") or {}
        candidate_is_base = str(base.get("address", "")).lower() == c.address.lower()
        token = base if candidate_is_base else quote
        txns = p.get("txns") or {}
        vol = p.get("volume") or {}
        pc = p.get("priceChange") or {}
        info = p.get("info") or {}
        socials = info.get("socials") or []
        boosts = p.get("boosts") or {}

        return PairSnapshot(
            chain=c.chain,
            token_address=c.address,
            token_name=str(token.get("name") or "Unknown"),
            token_symbol=str(token.get("symbol") or "?"),
            pair_address=str(p.get("pairAddress") or ""),
            dex_id=str(p.get("dexId") or ""),
            url=str(p.get("url") or ""),
            market_cap=_num(p.get("marketCap") or p.get("fdv")),
            fdv=_num(p.get("fdv")),
            liquidity_usd=liq(p),
            pair_created_at_ms=earliest_created_at_ms,
            price_usd=_num(p.get("priceUsd")),
            price_change_m5=_num(pc.get("m5")),
            price_change_h1=_num(pc.get("h1")),
            volume_m5=_num(vol.get("m5")),
            volume_h1=_num(vol.get("h1")),
            buys_m5=_int((txns.get("m5") or {}).get("buys")),
            sells_m5=_int((txns.get("m5") or {}).get("sells")),
            buys_h1=_int((txns.get("h1") or {}).get("buys")),
            sells_h1=_int((txns.get("h1") or {}).get("sells")),
            socials_count=len(socials),
            boosts_active=_int(boosts.get("active")),
            source=c.source,
            raw=p,
        )

    MarketProviders.pair_snapshot = _pair_snapshot_earliest_age
    MarketProviders._earliest_launch_age_installed = True
    print(
        "EARLIEST LAUNCH AGE ON — deepest pool kept for market data; "
        "earliest pairCreatedAt across pools used for token age.",
        flush=True,
    )
