from __future__ import annotations

import asyncio
import html
import os
import re
import time
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

from . import narrative_trend as nt
from . import ultra_early as ue


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


GOOGLE_TREND_ENABLED = _bool("GOOGLE_TREND_ENABLED", True)
GOOGLE_TREND_MIN_SCORE = max(
    50, min(100, _int("GOOGLE_TREND_MIN_SCORE", 75))
)
GOOGLE_TREND_MAX_ALERT_AGE = max(
    30, _int("GOOGLE_TREND_MAX_ALERT_AGE_SECONDS", 180)
)
GOOGLE_TREND_REFRESH_SECONDS = max(
    60, _int("GOOGLE_TREND_REFRESH_SECONDS", 300)
)
GOOGLE_TREND_MAX_RESULTS = max(
    5, min(50, _int("GOOGLE_TREND_MAX_RESULTS", 20))
)
GOOGLE_TREND_GEOS = tuple(
    x.strip().upper()
    for x in os.getenv("GOOGLE_TREND_GEOS", "NG,US,GB").split(",")
    if x.strip()
) or ("NG", "US", "GB")

_GOOGLE_RSS = "https://trends.google.com/trending/rss"

_GEO_LABELS = {
    "NG": "Nigeria",
    "US": "US",
    "GB": "UK",
    "IN": "India",
    "CA": "Canada",
    "AU": "Australia",
}


def _geo_label(geo: str) -> str:
    return _GEO_LABELS.get(str(geo or "").upper(), str(geo or "").upper())


def _local_tag(tag: str) -> str:
    return str(tag or "").split("}")[-1].lower()


def _parse_traffic(value: str) -> int:
    raw = str(value or "").strip().upper().replace(",", "").replace("+", "")
    if not raw:
        return 0
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KMB]?)", raw)
    if not m:
        return 0
    number = float(m.group(1))
    suffix = m.group(2)
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    return int(number * mult.get(suffix, 1))


def _rss_rows(xml_text: str, geo: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return []

    rows: list[dict] = []
    rank = 0

    for node in root.iter():
        if _local_tag(node.tag) != "item":
            continue
        rank += 1
        title = ""
        traffic_label = ""

        for child in list(node):
            tag = _local_tag(child.tag)
            text = str(child.text or "").strip()
            if tag == "title":
                title = text
            elif tag in {"approx_traffic", "traffic"}:
                traffic_label = text

        if not title:
            continue

        norm = nt._clean_text(title)
        if not norm:
            continue

        rows.append(
            {
                "title": title,
                "norm": norm,
                "compact": nt._compact(title),
                "best_rank": rank,
                "search_volume": _parse_traffic(traffic_label),
                "search_volume_label": traffic_label,
                "locations": [_geo_label(geo)],
            }
        )

        if len(rows) >= GOOGLE_TREND_MAX_RESULTS:
            break

    return rows


async def _refresh_google_trends(scanner) -> list[dict]:
    lock = getattr(scanner, "_google_trends_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        scanner._google_trends_lock = lock

    async with lock:
        now = time.time()
        cache = getattr(scanner, "_google_trends_cache", None) or {}
        if cache and now < float(cache.get("expires", 0) or 0):
            return list(cache.get("items") or [])

        grouped: dict[str, dict] = {}

        for geo in GOOGLE_TREND_GEOS:
            try:
                r = await scanner.market.client.get(
                    _GOOGLE_RSS,
                    params={"geo": geo},
                    headers={
                        "Accept": "application/rss+xml, application/xml, text/xml",
                        "User-Agent": "MemeProspectPro/1.0",
                    },
                    timeout=8.0,
                )
                if int(r.status_code) != 200:
                    print(
                        f"GOOGLE TRENDS HTTP {int(r.status_code)} — {_geo_label(geo)}",
                        flush=True,
                    )
                    continue

                for row in _rss_rows(r.text, geo):
                    key = str(row.get("norm") or "")
                    if not key:
                        continue

                    existing = grouped.get(key)
                    if existing is None:
                        grouped[key] = {
                            **row,
                            "locations": set(row.get("locations") or []),
                        }
                    else:
                        existing["best_rank"] = min(
                            int(existing.get("best_rank") or 999),
                            int(row.get("best_rank") or 999),
                        )
                        if int(row.get("search_volume") or 0) > int(
                            existing.get("search_volume") or 0
                        ):
                            existing["search_volume"] = int(
                                row.get("search_volume") or 0
                            )
                            existing["search_volume_label"] = str(
                                row.get("search_volume_label") or ""
                            )
                        existing["locations"].update(
                            row.get("locations") or []
                        )
            except Exception as exc:
                print(
                    f"GOOGLE TRENDS error — {_geo_label(geo)} — "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )

        items: list[dict] = []
        for row in grouped.values():
            row["locations"] = sorted(row.get("locations") or [])
            items.append(row)

        items.sort(
            key=lambda x: (
                int(x.get("best_rank") or 999),
                -int(x.get("search_volume") or 0),
            )
        )

        scanner._google_trends_cache = {
            "items": items,
            "expires": now + GOOGLE_TREND_REFRESH_SECONDS,
            "updated": int(now),
        }

        print(
            f"GOOGLE TRENDS READY — {len(items)} unique trends — "
            f"locations={','.join(_geo_label(x) for x in GOOGLE_TREND_GEOS)}",
            flush=True,
        )
        return items


def _google_match_score(
    name: str,
    symbol: str,
    trend: dict,
) -> tuple[int, str]:
    trend_norm = str(trend.get("norm") or "")
    trend_compact = str(trend.get("compact") or "")
    name_norm = nt._clean_text(name)
    name_compact = nt._compact(name)
    symbol_clean = re.sub(
        r"[^A-Z0-9]", "", str(symbol or "").upper()
    )
    symbol_norm = nt._clean_text(symbol)

    if not name_norm or not trend_norm:
        return 0, ""

    base = 0
    reason = ""

    if name_norm == trend_norm or (
        name_compact and name_compact == trend_compact
    ):
        base, reason = 70, "exact token-name/Google-trend match"
    elif len(name_compact) >= 5 and (
        name_compact in trend_compact
        or trend_compact in name_compact
    ):
        base, reason = 62, "strong token-name/Google-trend phrase match"
    else:
        ntokens = set(nt._tokens(name))
        ttokens = set(nt._tokens(trend_norm))
        jaccard = (
            len(ntokens & ttokens) / len(ntokens | ttokens)
            if ntokens and ttokens
            else 0.0
        )
        seq = SequenceMatcher(
            None, name_norm, trend_norm
        ).ratio()

        if jaccard >= 0.80 and len(ntokens & ttokens) >= 1:
            base, reason = 59, "high keyword overlap with Google trend"
        elif jaccard >= 0.60 and len(ntokens & ttokens) >= 2:
            base, reason = 54, "multi-keyword Google trend match"
        elif seq >= 0.88 and min(
            len(name_norm), len(trend_norm)
        ) >= 5:
            base, reason = 52, "close Google trend-name match"

    generic_symbols = getattr(
        nt,
        "_GENERIC_SYMBOLS",
        {"PUMP", "NEW", "SOL", "COIN", "TOKEN", "MEME", "AI"},
    )

    if (
        symbol_clean
        and symbol_clean not in generic_symbols
        and len(symbol_clean) >= 4
    ):
        if (
            symbol_norm == trend_norm
            or nt._compact(symbol_norm) == trend_compact
        ):
            if 66 > base:
                base, reason = 66, "exact ticker/Google-trend match"
        elif (
            len(trend_compact) >= 5
            and symbol_clean.lower() in trend_compact
        ):
            if 56 > base:
                base, reason = 56, "ticker appears in Google trend"

    if base <= 0:
        return 0, ""

    rank = int(trend.get("best_rank") or 999)
    if rank <= 3:
        base += 13
    elif rank <= 10:
        base += 9
    elif rank <= 20:
        base += 5
    else:
        base += 2

    volume = int(trend.get("search_volume") or 0)
    if volume >= 500_000:
        base += 13
    elif volume >= 100_000:
        base += 10
    elif volume >= 20_000:
        base += 7
    elif volume >= 5_000:
        base += 4
    elif volume > 0:
        base += 2

    locations = trend.get("locations") or []
    if len(locations) >= 3:
        base += 6
    elif len(locations) >= 2:
        base += 3

    return min(95, int(base)), reason


def _best_x_cached_match(
    scanner,
    name: str,
    symbol: str,
) -> tuple[int, dict | None, str]:
    cache = getattr(scanner, "_x_trends_cache", None) or {}
    trends = list(cache.get("items") or [])

    best_score = 0
    best_trend = None
    best_reason = ""

    for trend in trends:
        try:
            score, reason = nt._x_name_match_score(
                name, symbol, trend
            )
        except Exception:
            continue
        if score > best_score:
            best_score = int(score)
            best_trend = trend
            best_reason = reason

    return best_score, best_trend, best_reason


def _alerted_set(scanner) -> set[str]:
    cache = getattr(scanner, "_google_trend_alerted", None)
    if cache is None:
        cache = set()
        scanner._google_trend_alerted = cache
    return cache


async def _analyze_google_trend(
    scanner,
    mint: str,
    listed_ts: int,
) -> None:
    if not GOOGLE_TREND_ENABLED:
        return

    age = max(
        0,
        int(time.time()) - int(listed_ts or time.time()),
    )
    if age > GOOGLE_TREND_MAX_ALERT_AGE:
        return

    alerted = _alerted_set(scanner)
    if mint in alerted:
        return

    try:
        name, symbol = await nt._resolve_metadata(scanner, mint)
    except Exception:
        return

    if not name:
        return

    age = max(
        0,
        int(time.time()) - int(listed_ts or time.time()),
    )
    if age > GOOGLE_TREND_MAX_ALERT_AGE:
        return

    trends = await _refresh_google_trends(scanner)
    if not trends:
        return

    best_score = 0
    best_trend = None
    best_reason = ""

    for trend in trends:
        score, reason = _google_match_score(
            name, symbol, trend
        )
        if score > best_score:
            best_score = score
            best_trend = trend
            best_reason = reason

    if (
        best_trend is None
        or best_score < GOOGLE_TREND_MIN_SCORE
    ):
        return

    x_score, x_trend, _ = _best_x_cached_match(
        scanner, name, symbol
    )

    hot_both = bool(
        x_trend is not None
        and x_score >= max(70, nt.NARRATIVE_MIN_SCORE - 5)
    )

    alerted.add(mint)
    if len(alerted) > 10000:
        scanner._google_trend_alerted = set(
            list(alerted)[-5000:]
        )

    google_title = str(
        best_trend.get("title") or "Unknown"
    )
    locations = ", ".join(
        best_trend.get("locations") or []
    ) or "Unknown"
    volume_label = str(
        best_trend.get("search_volume_label") or ""
    ).strip()
    if not volume_label:
        volume = int(best_trend.get("search_volume") or 0)
        volume_label = f"{volume:,}+" if volume > 0 else "n/a"

    if hot_both:
        x_title = str(
            (x_trend or {}).get("title") or "Unknown"
        )
        final_score = min(
            100,
            max(best_score, x_score) + 10,
        )
        heading = "🔥🔥 <b>X + GOOGLE HOT NARRATIVE MATCH</b>"
        extra = (
            f"X trend: <b>{html.escape(x_title)}</b>\n"
            f"X score: <b>{x_score}/100</b>\n"
        )
    else:
        final_score = best_score
        heading = "🔎🔥 <b>GOOGLE TRENDING MEME MATCH</b>"
        extra = ""

    text = (
        f"{heading}\n\n"
        f"🟣 <b>{html.escape(name)} "
        f"({html.escape(symbol or '?')})</b>\n"
        f"Age: <b>{age}s</b>\n"
        f"Narrative score: <b>{final_score}/100</b>\n"
        f"Google trend: <b>{html.escape(google_title)}</b>\n"
        f"Google searches: <b>{html.escape(volume_label)}</b>\n"
        f"Trend locations: <b>{html.escape(locations)}</b>\n"
        f"{extra}"
        f"Match: <b>{html.escape(best_reason)}</b>\n"
        f"Contract: <code>{html.escape(mint)}</code>\n\n"
        "⚡ Status: <b>PRIORITY WATCH</b>\n"
        "⚠️ Trend strength does not bypass mint/freeze safety, "
        "momentum, Jupiter BUY/reverse-SELL checks, or manual "
        "wallet approval.\n\n"
        "Wait for 🚨 BUY SETUP READY for the qualified entry signal."
    )

    sent = 0
    for user in scanner.accounts.trade_users():
        try:
            ok = await scanner.telegram.send(
                text,
                str(user["chat_id"]),
                urgent=True,
            )
            if ok:
                sent += 1
        except Exception as exc:
            print(
                f"GOOGLE TREND alert error — {mint[:6]}…{mint[-4:]} — "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    if sent:
        kind = "X+GOOGLE" if hot_both else "GOOGLE"
        print(
            f"{kind} TREND ALERT SENT — {mint[:6]}…{mint[-4:]} — "
            f"age {age}s — score {final_score} — alerts {sent}",
            flush=True,
        )


def install() -> None:
    if getattr(ue, "_google_trend_installed", False):
        return
    ue._google_trend_installed = True

    previous_handler = ue._handle_ultra_listing

    async def wrapped_handler(
        scanner,
        address: str,
        listed_ts: int,
    ):
        if GOOGLE_TREND_ENABLED:
            asyncio.create_task(
                _analyze_google_trend(
                    scanner,
                    address,
                    listed_ts,
                )
            )
        return await previous_handler(
            scanner,
            address,
            listed_ts,
        )

    ue._handle_ultra_listing = wrapped_handler

    print(
        "GOOGLE TREND MATCH ON — public Google Trends RSS; "
        f"geos={','.join(GOOGLE_TREND_GEOS)}; "
        f"min score {GOOGLE_TREND_MIN_SCORE}; "
        f"max token age {GOOGLE_TREND_MAX_ALERT_AGE}s; "
        "X+Google matches upgraded to HOT NARRATIVE; "
        "BUY safety/Jupiter/manual approval unchanged.",
        flush=True,
    )
