from __future__ import annotations
import time
from .models import PairSnapshot, ScoreResult

def _ratio(a: float, b: float) -> float:
    return a / b if b > 0 else (a if a > 0 else 0.0)

def _security_bool(data: dict, *keys: str):
    for k in keys:
        if k in data:
            v = data[k]
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return bool(v)
            if isinstance(v, str):
                s = v.strip().lower()
                if s in {"1","true","yes","y"}:
                    return True
                if s in {"0","false","no","n","", "null", "none"}:
                    return False
    return None

def score_token(
    s: PairSnapshot,
    previous: dict | None = None,
    security: dict | None = None,
    min_liquidity: float = 10_000,
    min_mcap: float = 200_000,
    max_mcap: float = 30_000_000,
    max_age_minutes: int = 360,
) -> ScoreResult:
    score = 0
    reasons: list[str] = []
    warnings: list[str] = []
    hard_block = False
    now_ms = int(time.time() * 1000)
    age_min = (now_ms - s.pair_created_at_ms) / 60000 if s.pair_created_at_ms else 999999

    # Hard universe filters.
    if s.liquidity_usd < min_liquidity:
        warnings.append(f"Liquidity below ${min_liquidity:,.0f}")
        hard_block = True
    if s.market_cap and s.market_cap < min_mcap:
        warnings.append(f"Market cap below ${min_mcap:,.0f}")
    if s.market_cap and s.market_cap > max_mcap:
        warnings.append(f"Market cap above ${max_mcap:,.0f}")
        hard_block = True
    if age_min > max_age_minutes:
        warnings.append(f"Pair older than {max_age_minutes} min")
        hard_block = True

    # Age: early enough to catch acceleration but not purely seconds-old.
    if age_min <= 15:
        score += 10; reasons.append("Very early pair")
    elif age_min <= 60:
        score += 12; reasons.append("Young pair")
    elif age_min <= 180:
        score += 7; reasons.append("Still early")
    elif age_min <= 360:
        score += 3

    # Market cap.
    mc = s.market_cap
    if 500_000 <= mc <= 10_000_000:
        score += 14; reasons.append("Prime early market-cap range")
    elif 200_000 <= mc < 500_000:
        score += 7; reasons.append("Micro-cap")
    elif 10_000_000 < mc <= 30_000_000:
        score += 8; reasons.append("Mid-cap with room to expand")

    # Liquidity quality.
    if s.liquidity_usd >= 100_000:
        score += 12; reasons.append("Strong liquidity")
    elif s.liquidity_usd >= 50_000:
        score += 10; reasons.append("Healthy liquidity")
    elif s.liquidity_usd >= 25_000:
        score += 7
    elif s.liquidity_usd >= 10_000:
        score += 3

    lm = _ratio(s.liquidity_usd, mc) if mc else 0
    if lm >= 0.10:
        score += 8; reasons.append("Excellent liquidity/market-cap ratio")
    elif lm >= 0.05:
        score += 6; reasons.append("Good liquidity/market-cap ratio")
    elif lm >= 0.025:
        score += 3

    # 5m demand intensity.
    vol_liq = _ratio(s.volume_m5, s.liquidity_usd)
    if vol_liq >= 1:
        score += 12; reasons.append("Exceptional 5m turnover")
    elif vol_liq >= 0.5:
        score += 10; reasons.append("Strong 5m turnover")
    elif vol_liq >= 0.2:
        score += 6

    tx5 = s.buys_m5 + s.sells_m5
    buy_ratio = _ratio(s.buys_m5, max(s.sells_m5, 1))
    if tx5 >= 50 and buy_ratio >= 2.0:
        score += 12; reasons.append(f"Buy pressure {buy_ratio:.1f}x")
    elif tx5 >= 30 and buy_ratio >= 1.5:
        score += 8; reasons.append(f"Buy pressure {buy_ratio:.1f}x")
    elif tx5 >= 15 and buy_ratio >= 1.2:
        score += 4
    if tx5 >= 100:
        score += 4; reasons.append("High 5m transaction count")
    elif tx5 >= 50:
        score += 2

    # Momentum: reward meaningful move, penalize extremely late chase.
    if 5 <= s.price_change_m5 <= 45:
        score += 8; reasons.append("Constructive 5m momentum")
    elif 45 < s.price_change_m5 <= 100:
        score += 4; warnings.append("Already moving very fast")
    elif s.price_change_m5 > 100:
        score -= 8; warnings.append("5m move is extremely extended")
    elif s.price_change_m5 < -10:
        score -= 8; warnings.append("Negative 5m momentum")

    if 10 <= s.price_change_h1 <= 150:
        score += 5
    elif s.price_change_h1 > 300:
        score -= 5; warnings.append("1h move may be late-stage")

    # Acceleration vs previous 5-minute scan.
    if previous:
        prev_vol = float(previous.get("volume_m5") or 0)
        prev_buys = int(previous.get("buys_m5") or 0)
        if prev_vol > 0:
            vol_growth = (s.volume_m5 - prev_vol) / prev_vol
            if vol_growth >= 1.0:
                score += 7; reasons.append("5m volume accelerating >100%")
            elif vol_growth >= 0.5:
                score += 5; reasons.append("5m volume accelerating")
            elif vol_growth <= -0.5:
                score -= 3
        if prev_buys > 0 and s.buys_m5 >= prev_buys * 1.5:
            score += 4; reasons.append("Buyer activity accelerating")

    if s.socials_count >= 2:
        score += 3; reasons.append("Multiple social links")
    elif s.socials_count == 1:
        score += 1

    # Paid boosts are not treated as proof of organic quality.
    if s.boosts_active:
        warnings.append("DEX boost active; do not treat promotion as organic demand")

    # Security gate: flexible across provider field naming.
    checked = bool(security)
    if security:
        critical_sets = [
            (("honeypot", "isHoneypot", "is_honeypot"), "Honeypot risk"),
            (("fakeToken", "fake_token", "isFakeToken", "is_fake_token"), "Fake-token risk"),
            (("freezable", "isFreezable", "is_freezable"), "Token can be frozen"),
        ]
        for keys, label in critical_sets:
            if _security_bool(security, *keys) is True:
                warnings.append(label)
                hard_block = True

        # Authority fields: a non-empty authority often deserves a warning.
        for key, label in (
            ("freezeAuthority", "Freeze authority present"),
            ("freeze_authority", "Freeze authority present"),
            ("mintAuthority", "Mint authority present"),
            ("mint_authority", "Mint authority present"),
        ):
            v = security.get(key)
            if v not in (None, "", False, "0", "null"):
                warnings.append(label)
                if "Freeze" in label:
                    hard_block = True
                else:
                    score -= 5

        # Common concentration names if provider returns them numerically.
        for key in ("top10HolderPercent", "top10_holder_percent", "top10HolderPercentage"):
            if key in security:
                try:
                    pct = float(security[key])
                    if pct <= 1:
                        pct *= 100
                    if pct >= 50:
                        warnings.append(f"Top-10 holder concentration {pct:.1f}%")
                        score -= 12
                    elif pct >= 30:
                        warnings.append(f"Top-10 holder concentration {pct:.1f}%")
                        score -= 5
                except Exception:
                    pass
                break

    score = max(0, min(100, int(round(score))))
    return ScoreResult(score, reasons, warnings, hard_block=hard_block, security_checked=checked)
