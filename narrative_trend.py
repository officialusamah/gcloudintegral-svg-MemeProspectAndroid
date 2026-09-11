from __future__ import annotations

import asyncio
import html
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from . import ultra_early as ue
from .scanner import Scanner


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


NARRATIVE_ENABLED = _bool("NARRATIVE_TREND_ENABLED", True)
NARRATIVE_MIN_SCORE = max(50, min(100, _int("NARRATIVE_TREND_MIN_SCORE", 75)))
NARRATIVE_MAX_ALERT_AGE = max(30, _int("NARRATIVE_TREND_MAX_ALERT_AGE_SECONDS", 180))
NARRATIVE_METADATA_WAIT = max(1.0, min(15.0, _float("NARRATIVE_METADATA_WAIT_SECONDS", 4.0)))

X_BEARER_TOKEN = str(os.getenv("X_BEARER_TOKEN", "") or "").strip()
X_TREND_REFRESH_SECONDS = max(30, _int("X_TREND_REFRESH_SECONDS", 90))
X_TREND_MAX_RESULTS = max(10, min(50, _int("X_TREND_MAX_RESULTS", 30)))
X_COUNTS_CACHE_SECONDS = max(30, _int("X_COUNTS_CACHE_SECONDS", 60))
X_COUNTS_LOOKBACK_MINUTES = max(20, min(180, _int("X_COUNTS_LOOKBACK_MINUTES", 60)))
X_MOMENTUM_WINDOW_MINUTES = max(5, min(30, _int("X_MOMENTUM_WINDOW_MINUTES", 10)))
X_COUNTS_MIN_PRE_SCORE = max(40, min(90, _int("X_COUNTS_MIN_PRE_SCORE", 55)))

# Default is worldwide only to keep API usage/cost low. Add locations later with
# X_TREND_WOEIDS=1,23424977,23424975,23424908 etc. if desired.
X_TREND_WOEIDS = tuple(
    int(x.strip())
    for x in os.getenv("X_TREND_WOEIDS", "1").split(",")
    if x.strip().isdigit()
) or (1,)

_X_TRENDS_BASE = "https://api.x.com/2/trends/by/woeid"
_X_COUNTS_URL = "https://api.x.com/2/tweets/counts/recent"

_GENERIC_WORDS = {
    "coin", "token", "meme", "memecoin", "crypto", "sol", "solana", "pump",
    "official", "new", "the", "a", "an", "of", "and", "to", "in", "on",
    "vs", "v", "coin2026", "ai",
}
_GENERIC_SYMBOLS = {"PUMP", "NEW", "SOL", "COIN", "TOKEN", "MEME", "AI"}


def _clean_text(value: Any) -> str:
    text = str(value or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = text.lstrip("#")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _compact(value: Any) -> str:
    return _clean_text(value).replace(" ", "")


def _tokens(value: Any) -> list[str]:
    return [x for x in _clean_text(value).split() if x not in _GENERIC_WORDS and len(x) >= 2]


def _x_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {X_BEARER_TOKEN}",
        "Accept": "application/json",
        "User-Agent": "MemeProspectPro/1.0",
    }


def _trend_label(woeid: int) -> str:
    known = {
        1: "Worldwide",
        23424977: "US",
        23424975: "UK",
        23424908: "Nigeria",
        23424848: "India",
        23424775: "Canada",
        23424748: "Australia",
    }
    return known.get(int(woeid), str(woeid))


async def _refresh_x_trends(scanner) -> list[dict]:
    if not X_BEARER_TOKEN:
        return []

    lock = getattr(scanner, "_x_trends_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        scanner._x_trends_lock = lock

    async with lock:
        now = time.time()
        cache = getattr(scanner, "_x_trends_cache", None) or {}
        if cache and now < float(cache.get("expires", 0) or 0):
            return list(cache.get("items") or [])

        grouped: dict[str, dict] = {}
        for woeid in X_TREND_WOEIDS:
            try:
                r = await scanner.market.client.get(
                    f"{_X_TRENDS_BASE}/{int(woeid)}",
                    params={
                        "max_trends": str(int(X_TREND_MAX_RESULTS)),
                        "trend.fields": "trend_name,tweet_count",
                    },
                    headers=_x_headers(),
                    timeout=8.0,
                )
                if int(r.status_code) != 200:
                    print(
                        f"NARRATIVE X trends HTTP {int(r.status_code)} — {_trend_label(woeid)}",
                        flush=True,
                    )
                    continue
                payload = r.json() if r.content else {}
                rows = payload.get("data") or []
                if not isinstance(rows, list):
                    rows = []

                for rank, row in enumerate(rows, start=1):
                    if not isinstance(row, dict):
                        continue
                    title = str(row.get("trend_name") or "").strip()
                    if not title:
                        continue
                    norm = _clean_text(title)
                    if not norm:
                        continue
                    tweet_count = int(row.get("tweet_count") or 0)
                    existing = grouped.get(norm)
                    if existing is None:
                        grouped[norm] = {
                            "title": title,
                            "norm": norm,
                            "compact": _compact(title),
                            "best_rank": rank,
                            "tweet_count": tweet_count,
                            "locations": {_trend_label(woeid)},
                        }
                    else:
                        existing["best_rank"] = min(int(existing.get("best_rank") or 999), rank)
                        existing["tweet_count"] = max(int(existing.get("tweet_count") or 0), tweet_count)
                        existing["locations"].add(_trend_label(woeid))
            except Exception as exc:
                print(
                    f"NARRATIVE X trends error — {_trend_label(woeid)} — {type(exc).__name__}",
                    flush=True,
                )

        items: list[dict] = []
        for row in grouped.values():
            row["locations"] = sorted(row.get("locations") or [])
            items.append(row)
        items.sort(key=lambda x: (int(x.get("best_rank") or 999), -int(x.get("tweet_count") or 0)))

        scanner._x_trends_cache = {
            "items": items,
            "expires": now + X_TREND_REFRESH_SECONDS,
            "updated": int(now),
        }
        print(
            f"NARRATIVE X TRENDS READY — {len(items)} unique trends — "
            f"locations={','.join(_trend_label(x) for x in X_TREND_WOEIDS)}",
            flush=True,
        )
        return items


async def _x_refresh_loop(scanner) -> None:
    while True:
        try:
            cache = getattr(scanner, "_x_trends_cache", None)
            if isinstance(cache, dict):
                cache["expires"] = 0
            await _refresh_x_trends(scanner)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"NARRATIVE X refresh error: {type(exc).__name__}: {exc}", flush=True)
        await asyncio.sleep(X_TREND_REFRESH_SECONDS)


def _metadata_from_cache(scanner, mint: str) -> tuple[str, str]:
    cache = getattr(scanner, "_narrative_launch_meta", None) or {}
    row = cache.get(mint) or {}
    return str(row.get("name") or "").strip(), str(row.get("symbol") or "").strip()


async def _helius_metadata(scanner, mint: str) -> tuple[str, str]:
    payload = {
        "jsonrpc": "2.0",
        "id": "narrative-token-meta",
        "method": "getAsset",
        "params": {"id": mint, "displayOptions": {"showFungible": True}},
    }
    try:
        r = await scanner.market.client.post(
            scanner.cfg.solana_rpc_url,
            json=payload,
            timeout=8.0,
        )
        r.raise_for_status()
        result = (r.json().get("result") or {}) if r.content else {}
        metadata = (((result.get("content") or {}).get("metadata") or {}))
        return str(metadata.get("name") or "").strip(), str(metadata.get("symbol") or "").strip()
    except Exception:
        return "", ""


async def _resolve_metadata(scanner, mint: str) -> tuple[str, str]:
    deadline = time.monotonic() + NARRATIVE_METADATA_WAIT
    while time.monotonic() < deadline:
        name, symbol = _metadata_from_cache(scanner, mint)
        if name and _clean_text(name) and not name.lower().startswith("pump.fun "):
            return name, symbol
        await asyncio.sleep(0.25)

    # Helius is metadata fallback only; the Ultra launch path remains independent.
    for delay in (0.0, 1.5):
        if delay:
            await asyncio.sleep(delay)
        name, symbol = await _helius_metadata(scanner, mint)
        if name and _clean_text(name):
            return name, symbol
    return "", ""


def _x_name_match_score(name: str, symbol: str, trend: dict) -> tuple[int, str]:
    trend_norm = str(trend.get("norm") or "")
    trend_compact = str(trend.get("compact") or "")
    name_norm = _clean_text(name)
    name_compact = _compact(name)
    symbol_clean = re.sub(r"[^A-Z0-9]", "", str(symbol or "").upper())
    symbol_norm = _clean_text(symbol)

    if not name_norm or not trend_norm:
        return 0, ""

    base = 0
    reason = ""
    if name_norm == trend_norm or (name_compact and name_compact == trend_compact):
        base, reason = 68, "exact token-name/X-trend match"
    elif len(name_compact) >= 5 and (
        name_compact in trend_compact or trend_compact in name_compact
    ):
        base, reason = 60, "strong token-name/X-trend phrase match"
    else:
        nt = set(_tokens(name))
        tt = set(_tokens(trend_norm))
        jaccard = (len(nt & tt) / len(nt | tt)) if nt and tt else 0.0
        seq = SequenceMatcher(None, name_norm, trend_norm).ratio()
        if jaccard >= 0.80 and len(nt & tt) >= 1:
            base, reason = 57, "high keyword overlap with X trend"
        elif jaccard >= 0.60 and len(nt & tt) >= 2:
            base, reason = 52, "multi-keyword X trend match"
        elif seq >= 0.88 and min(len(name_norm), len(trend_norm)) >= 5:
            base, reason = 50, "close X trend-name match"

    if symbol_clean and symbol_clean not in _GENERIC_SYMBOLS and len(symbol_clean) >= 4:
        if symbol_norm == trend_norm or _compact(symbol_norm) == trend_compact:
            if 64 > base:
                base, reason = 64, "exact ticker/X-trend match"
        elif len(trend_compact) >= 5 and symbol_clean.lower() in trend_compact:
            if 54 > base:
                base, reason = 54, "ticker appears in X trend"

    if base <= 0:
        return 0, ""

    rank = int(trend.get("best_rank") or 999)
    if rank <= 3:
        base += 14
    elif rank <= 10:
        base += 10
    elif rank <= 20:
        base += 6
    else:
        base += 3

    tweet_count = int(trend.get("tweet_count") or 0)
    if tweet_count >= 250_000:
        base += 14
    elif tweet_count >= 100_000:
        base += 12
    elif tweet_count >= 25_000:
        base += 9
    elif tweet_count >= 5_000:
        base += 6
    elif tweet_count > 0:
        base += 3

    locations = trend.get("locations") or []
    if len(locations) >= 3:
        base += 6
    elif len(locations) >= 2:
        base += 3

    return min(92, int(base)), reason


def _x_query_for_trend(title: str) -> str:
    raw = str(title or "").strip()
    if not raw:
        return ""
    # Preserve hashtags; quote multi-word phrases for better precision.
    if raw.startswith("#") and " " not in raw:
        core = raw
    elif " " in raw:
        safe = raw.replace('"', "").strip()
        core = f'"{safe}"'
    else:
        core = raw
    return f"{core} -is:retweet"


async def _x_recent_momentum(scanner, trend_title: str) -> dict:
    if not X_BEARER_TOKEN or not trend_title:
        return {}

    key = _clean_text(trend_title)
    cache = getattr(scanner, "_x_counts_cache", None)
    if cache is None:
        cache = {}
        scanner._x_counts_cache = cache
    now = time.time()
    cached = cache.get(key) or {}
    if cached and now < float(cached.get("expires", 0) or 0):
        return dict(cached.get("value") or {})

    query = _x_query_for_trend(trend_title)
    if not query:
        return {}

    end_dt = datetime.now(timezone.utc) - timedelta(seconds=10)
    start_dt = end_dt - timedelta(minutes=X_COUNTS_LOOKBACK_MINUTES)
    try:
        r = await scanner.market.client.get(
            _X_COUNTS_URL,
            params={
                "query": query,
                "start_time": start_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "end_time": end_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "granularity": "minute",
                "search_count.fields": "start,end,tweet_count",
            },
            headers=_x_headers(),
            timeout=8.0,
        )
        if int(r.status_code) != 200:
            return {}
        payload = r.json() if r.content else {}
        rows = payload.get("data") or []
        counts = [int(x.get("tweet_count") or 0) for x in rows if isinstance(x, dict)]
        if not counts:
            return {}

        n = min(X_MOMENTUM_WINDOW_MINUTES, len(counts))
        recent = sum(counts[-n:])
        previous_counts = counts[:-n]
        previous_total = sum(previous_counts)
        previous_minutes = len(previous_counts)
        previous_equiv = (
            previous_total * (n / previous_minutes)
            if previous_minutes > 0 else 0.0
        )
        if previous_equiv > 0:
            acceleration = recent / previous_equiv
        elif recent > 0:
            acceleration = 10.0
        else:
            acceleration = 0.0

        result = {
            "recent_posts": int(recent),
            "previous_equiv_posts": float(previous_equiv),
            "acceleration": float(acceleration),
            "window_minutes": int(n),
            "total_posts": int(sum(counts)),
        }
        cache[key] = {"value": result, "expires": now + X_COUNTS_CACHE_SECONDS}
        if len(cache) > 500:
            for old_key in list(cache)[:100]:
                cache.pop(old_key, None)
        return result
    except Exception:
        return {}


def _momentum_bonus(momentum: dict) -> int:
    recent = int(momentum.get("recent_posts") or 0)
    accel = float(momentum.get("acceleration") or 0)
    if recent >= 500 and accel >= 2.0:
        return 12
    if recent >= 150 and accel >= 1.8:
        return 10
    if recent >= 50 and accel >= 1.5:
        return 8
    if recent >= 20 and accel >= 1.25:
        return 6
    if recent >= 5:
        return 3
    return 0


async def _analyze_narrative(scanner, mint: str, listed_ts: int) -> None:
    if not NARRATIVE_ENABLED or not X_BEARER_TOKEN or not mint:
        return

    name, symbol = await _resolve_metadata(scanner, mint)
    if not name:
        return
    if name.lower().startswith("pump.fun ") or str(symbol or "").upper() in {"PUMP", "NEW"}:
        return

    trends = await _refresh_x_trends(scanner)
    if not trends:
        return

    best = None
    best_score = 0
    best_reason = ""
    for trend in trends:
        score, reason = _x_name_match_score(name, symbol, trend)
        if score > best_score:
            best = trend
            best_score = score
            best_reason = reason

    if best is None or best_score < X_COUNTS_MIN_PRE_SCORE:
        return

    momentum = await _x_recent_momentum(scanner, str(best.get("title") or ""))
    final_score = min(100, int(best_score) + _momentum_bonus(momentum))
    if final_score < NARRATIVE_MIN_SCORE:
        return

    age = max(0, int(time.time()) - int(listed_ts or time.time()))
    if age > NARRATIVE_MAX_ALERT_AGE:
        return

    scores = getattr(scanner, "_narrative_scores", None)
    if scores is None:
        scores = {}
        scanner._narrative_scores = scores
    scores[mint] = {
        "score": final_score,
        "name": name,
        "symbol": symbol,
        "trend": str(best.get("title") or ""),
        "rank": int(best.get("best_rank") or 0),
        "tweet_count": int(best.get("tweet_count") or 0),
        "locations": list(best.get("locations") or []),
        "momentum": dict(momentum),
        "matched_at": int(time.time()),
    }

    trend_title = str(best.get("title") or "")
    rank = int(best.get("best_rank") or 0)
    tweet_count = int(best.get("tweet_count") or 0)
    locations = ", ".join(best.get("locations") or []) or "Worldwide"
    recent_posts = int(momentum.get("recent_posts") or 0)
    accel = float(momentum.get("acceleration") or 0)
    window = int(momentum.get("window_minutes") or X_MOMENTUM_WINDOW_MINUTES)

    tweet_volume_line = f"{tweet_count:,}+" if tweet_count > 0 else "X did not publish a count"
    if recent_posts > 0:
        momentum_line = f"{recent_posts:,} posts / {window}m — {accel:.1f}× recent acceleration"
    else:
        momentum_line = "Trend confirmed by X; minute-by-minute count unavailable"

    try:
        jupiter_url = scanner.live._jupiter_url("SOL", mint)
        buttons = [[{"text": "🪐 Open Jupiter", "url": jupiter_url}]]
    except Exception:
        buttons = None

    sent = 0
    for user in scanner.accounts.trade_users():
        try:
            await scanner.telegram.send(
                "𝕏🔥 <b>TRENDING NARRATIVE MATCH</b>\n\n"
                f"🟣 <b>{html.escape(name)} ({html.escape(symbol or '?')})</b>\n"
                f"Age: <b>{age}s</b>\n"
                f"Narrative score: <b>{final_score}/100</b>\n"
                f"Matched X trend: <b>{html.escape(trend_title)}</b>\n"
                f"Match: <b>{html.escape(best_reason)}</b>\n"
                f"X trend rank: <b>#{rank}</b>\n"
                f"X trend volume: <b>{html.escape(tweet_volume_line)}</b>\n"
                f"X momentum: <b>{html.escape(momentum_line)}</b>\n"
                f"Trend location: <b>{html.escape(locations)}</b>\n"
                f"Contract: <code>{html.escape(mint)}</code>\n\n"
                "⚡ Status: <b>PRIORITY WATCH</b>\n"
                "⚠️ X narrative strength does not bypass mint/freeze safety, momentum, "
                "Jupiter BUY/reverse-SELL checks, or manual wallet approval.\n\n"
                "Wait for 🚨 BUY SETUP READY for the bot's qualified entry signal.",
                str(user["chat_id"]),
                buttons=buttons,
                urgent=True,
            )
            sent += 1
        except Exception as exc:
            print(
                f"NARRATIVE X alert error — {mint[:6]}…{mint[-4:]} — {type(exc).__name__}: {exc}",
                flush=True,
            )

    if sent:
        print(
            f"NARRATIVE X MATCH — {name} ({symbol}) — score {final_score}/100 — "
            f"trend={trend_title!r} — alerts {sent}",
            flush=True,
        )


def install() -> None:
    if getattr(ue, "_narrative_trend_installed", False):
        return
    ue._narrative_trend_installed = True

    previous_handler = ue._handle_ultra_listing

    async def _handler_with_narrative(scanner, address: str, listed_ts: int):
        seen = getattr(scanner, "_narrative_analyzed", None)
        if seen is None:
            seen = set()
            scanner._narrative_analyzed = seen
        if address not in seen:
            seen.add(address)
            if len(seen) > 5000:
                for old in list(seen)[:1000]:
                    seen.discard(old)
            # Fire-and-forget: never delay the 0-second Ultra Early WATCH alert.
            asyncio.create_task(_analyze_narrative(scanner, address, listed_ts))
        return await previous_handler(scanner, address, listed_ts)

    ue._handle_ultra_listing = _handler_with_narrative

    previous_loop = Scanner.loop

    async def _loop_with_x_cache(self):
        x_task = None
        if X_BEARER_TOKEN:
            x_task = asyncio.create_task(_x_refresh_loop(self))
        try:
            await previous_loop(self)
        finally:
            if x_task is not None:
                x_task.cancel()
                try:
                    await x_task
                except asyncio.CancelledError:
                    pass

    Scanner.loop = _loop_with_x_cache

    if X_BEARER_TOKEN:
        print(
            f"NARRATIVE X RADAR ON — min_score={NARRATIVE_MIN_SCORE}/100 — "
            f"WOEIDs={','.join(str(x) for x in X_TREND_WOEIDS)} — "
            f"refresh={X_TREND_REFRESH_SECONDS}s — Ultra Early remains non-blocking",
            flush=True,
        )
    else:
        print(
            "NARRATIVE X RADAR WAITING — add X_BEARER_TOKEN in Railway; "
            "Ultra Early continues normally",
            flush=True,
        )
