from __future__ import annotations

import asyncio
import html
import json
import os
import time
from urllib.parse import quote

import websockets

# Robinhood Chain mainnet
CHAIN_ID = 4663
DEFAULT_RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
DEXSCREENER_CHAIN = "robinhood"
CONTRACTWOLF_BASE = "https://docs.contractwolf.io/api"

# Pools.trade / Uniswap Instant Launch strategies already used by the bot.
POOLS_STRATEGIES = (
    "0x23f8209572b4a1c2ad88a42749e830791fb027f1",
    "0xad44d55e7f8337c3ce113fbb591486e85be104b2",
    "0xce57498d3474dcc244dfb6710ffbe6d4441cd2b2",
    "0x60d73b21cdf2ea846ab3d58699bbbb8f29d72491",
)

POOLS_TOKEN_LAUNCHED_TOPIC = (
    "0x3b3d2bafdcae274a232217e1f80ee4305d3af6aa25c8b14b1681bd68d18042a4"
)

# Pons launch sources already proven in production.
PONS_V2_FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
PONS_V2_TOKEN_LAUNCHED_TOPIC = (
    "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
)

PONS_V1_FACTORY = "0xa5aab3f0c6eeadf30ef1d3eb997108e976351feb"
PONS_V1_TOKEN_LAUNCHED_TOPIC = (
    "0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a"
)

NAME_SELECTOR = "0x06fdde03"
SYMBOL_SELECTOR = "0x95d89b41"


def _truthy(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _rpc_url() -> str:
    return str(
        os.getenv("ROBINHOOD_RPC_URL", DEFAULT_RPC_URL) or DEFAULT_RPC_URL
    ).strip()


def _ws_url() -> str:
    return str(os.getenv("ROBINHOOD_WS_URL", "") or "").strip()


def _pools_strategies() -> list[str]:
    raw = str(os.getenv("ROBINHOOD_POOLS_STRATEGIES", "") or "").strip()
    values = (
        [x.strip().lower() for x in raw.split(",") if x.strip()]
        if raw
        else list(POOLS_STRATEGIES)
    )
    return [
        x for x in values
        if x.startswith("0x") and len(x) == 42
    ]


def _emitters() -> list[str]:
    values = list(_pools_strategies())
    if _truthy("ROBINHOOD_PONS_V2_ENABLED", True):
        values.append(PONS_V2_FACTORY)
    if _truthy("ROBINHOOD_PONS_V1_LEGACY_ENABLED", True):
        values.append(PONS_V1_FACTORY)
    return list(dict.fromkeys(x.lower() for x in values))


def _launch_topics() -> list[str]:
    topics = [POOLS_TOKEN_LAUNCHED_TOPIC]
    if _truthy("ROBINHOOD_PONS_V2_ENABLED", True):
        topics.append(PONS_V2_TOKEN_LAUNCHED_TOPIC)
    if _truthy("ROBINHOOD_PONS_V1_LEGACY_ENABLED", True):
        topics.append(PONS_V1_TOKEN_LAUNCHED_TOPIC)
    return list(dict.fromkeys(x.lower() for x in topics))


def _eligible_users(scanner) -> list[dict]:
    try:
        return [
            user
            for user in scanner.accounts.active_users()
            if int(user.get("live_enabled") or 0)
        ]
    except Exception:
        return []


def _short(address: str) -> str:
    address = str(address or "")
    if len(address) <= 14:
        return address
    return f"{address[:8]}…{address[-6:]}"


def _topic_address(topic: str) -> str:
    raw = str(topic or "").lower().removeprefix("0x")
    if len(raw) < 40:
        return ""
    address = "0x" + raw[-40:]
    if address == "0x" + ("0" * 40):
        return ""
    return address


def _decode_abi_string(data: str) -> str:
    raw = str(data or "").removeprefix("0x")
    if not raw:
        return ""
    try:
        blob = bytes.fromhex(raw)
    except ValueError:
        return ""

    try:
        if len(blob) >= 64:
            offset = int.from_bytes(blob[0:32], "big")
            if 0 <= offset <= len(blob) - 32:
                length = int.from_bytes(blob[offset:offset + 32], "big")
                start = offset + 32
                end = start + length
                if 0 <= length <= 512 and end <= len(blob):
                    return blob[start:end].decode(
                        "utf-8", errors="replace"
                    ).strip("\x00 ").strip()
    except Exception:
        pass

    try:
        return blob[:32].rstrip(b"\x00").decode(
            "utf-8", errors="replace"
        ).strip()
    except Exception:
        return ""


async def _rpc(scanner, method: str, params: list):
    payload = {
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000) % 2_000_000_000,
        "method": method,
        "params": params,
    }
    response = await scanner.market.client.post(
        _rpc_url(),
        json=payload,
        timeout=8.0,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("error"):
        raise RuntimeError(str(body["error"]))
    return body.get("result")


async def _erc20_text(scanner, token: str, selector: str) -> str:
    try:
        result = await _rpc(
            scanner,
            "eth_call",
            [{"to": token, "data": selector}, "latest"],
        )
        return _decode_abi_string(str(result or ""))
    except Exception:
        return ""


async def _token_identity(scanner, token: str) -> tuple[str, str]:
    try:
        name, symbol = await asyncio.gather(
            _erc20_text(scanner, token, NAME_SELECTOR),
            _erc20_text(scanner, token, SYMBOL_SELECTOR),
        )
    except Exception:
        name, symbol = "", ""

    name = str(name or "").strip()
    symbol = str(symbol or "").strip()

    if not name:
        name = f"Robinhood token {_short(token)}"
    if not symbol:
        symbol = "NEW"

    return name[:80], symbol[:24]


async def _contract_exists(scanner, token: str) -> bool:
    try:
        code = str(await _rpc(scanner, "eth_getCode", [token, "latest"]) or "")
        return code not in {"", "0x", "0x0"}
    except Exception:
        return False


async def _block_timestamp(scanner, block_number_hex: str) -> int:
    if not block_number_hex:
        return int(time.time())

    cache = getattr(scanner, "_rh_block_ts_cache", None)
    if cache is None:
        cache = {}
        scanner._rh_block_ts_cache = cache

    if block_number_hex in cache:
        return int(cache[block_number_hex])

    try:
        block = await _rpc(
            scanner,
            "eth_getBlockByNumber",
            [block_number_hex, False],
        )
        ts = int(str((block or {}).get("timestamp") or "0x0"), 16)
        if ts <= 0:
            ts = int(time.time())
    except Exception:
        ts = int(time.time())

    cache[block_number_hex] = ts
    if len(cache) > 500:
        for key in list(cache)[:100]:
            cache.pop(key, None)
    return ts


def _uniswap_buy_url(token: str) -> str:
    return (
        "https://app.uniswap.org/swap"
        "?chain=robinhood"
        "&inputCurrency=NATIVE"
        f"&outputCurrency={quote(token, safe='')}"
    )


def _uniswap_token_url(token: str) -> str:
    return (
        "https://app.uniswap.org/explore/tokens/robinhood/"
        + quote(token, safe="")
    )


def _explorer_url(token: str) -> str:
    return (
        "https://robinhoodchain.blockscout.com/address/"
        + quote(token, safe="")
    )


def _pons_trade_url(token: str) -> str:
    return (
        "https://www.ponsfamily.com/launchpad/"
        + quote(token, safe="")
    )


def _data_words(data: str) -> list[str]:
    raw = str(data or "").lower().removeprefix("0x")
    if not raw:
        return []
    return [
        raw[i:i + 64]
        for i in range(0, len(raw) - (len(raw) % 64), 64)
    ]


def _word_address(word: str) -> str:
    raw = str(word or "").lower().removeprefix("0x")
    if len(raw) < 40:
        return ""
    address = "0x" + raw[-40:]
    if address == "0x" + ("0" * 40):
        return ""
    return address


def _classify_launch(log: dict) -> dict | None:
    address = str(log.get("address") or "").lower()
    topics = [str(x or "").lower() for x in (log.get("topics") or [])]
    if not topics:
        return None

    topic0 = topics[0]
    data_words = _data_words(log.get("data") or "0x")

    if (
        address in set(_pools_strategies())
        and topic0 == POOLS_TOKEN_LAUNCHED_TOPIC
        and len(topics) >= 3
    ):
        token = _topic_address(topics[2])
        if not token:
            return None
        return {
            "source": "pools",
            "source_label": "Pools.trade / Uniswap Instant Launch",
            "token": token,
            "curve": "",
            "deployer": "",
            "pair_token": "",
            "venue": "Uniswap",
            "buy_url": _uniswap_buy_url(token),
            "buy_label": "⚡ BUY NOW — UNISWAP",
        }

    if (
        address == PONS_V2_FACTORY
        and topic0 == PONS_V2_TOKEN_LAUNCHED_TOPIC
        and len(topics) >= 4
    ):
        token = _topic_address(topics[1])
        curve = _topic_address(topics[2])
        deployer = _topic_address(topics[3])
        if not token:
            return None

        pair_token = _word_address(data_words[0]) if data_words else ""
        quote_label = "native ETH" if not pair_token else _short(pair_token)

        return {
            "source": "pons_v2",
            "source_label": "Pons V2",
            "token": token,
            "curve": curve,
            "deployer": deployer,
            "pair_token": pair_token,
            "venue": f"Pons / {quote_label}",
            "buy_url": _pons_trade_url(token),
            "buy_label": "⚡ BUY NOW — OFFICIAL PONS",
        }

    if (
        address == PONS_V1_FACTORY
        and topic0 == PONS_V1_TOKEN_LAUNCHED_TOPIC
        and len(topics) >= 4
    ):
        token = _topic_address(topics[1])
        deployer = _topic_address(topics[2])
        if not token:
            return None
        return {
            "source": "pons_v1",
            "source_label": "Pons V1 (legacy)",
            "token": token,
            "curve": "",
            "deployer": deployer,
            "pair_token": "",
            "venue": "Pons / Uniswap",
            "buy_url": _pons_trade_url(token),
            "buy_label": "⚡ OPEN — OFFICIAL PONS",
        }

    return None


def _seen(scanner) -> set[str]:
    value = getattr(scanner, "_rh_launch_seen", None)
    if value is None:
        value = set()
        scanner._rh_launch_seen = value
    return value


def _alerted(scanner) -> set[str]:
    value = getattr(scanner, "_rh_buy_ready_alerted", None)
    if value is None:
        value = set()
        scanner._rh_buy_ready_alerted = value
    return value


def _eval_semaphore(scanner):
    sem = getattr(scanner, "_rh_buy_eval_sem", None)
    if sem is None:
        sem = asyncio.Semaphore(
            max(1, _int("ROBINHOOD_BUY_MAX_CONCURRENT", 8))
        )
        scanner._rh_buy_eval_sem = sem
    return sem


def _number(value, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _window_value(data: dict | None, key: str) -> dict:
    data = data if isinstance(data, dict) else {}
    value = data.get(key)
    return value if isinstance(value, dict) else {}


async def _dex_market(scanner, token: str) -> dict | None:
    """
    Read current market data from DexScreener.

    We only accept a pair where this exact token is the base token, so the
    buys/sells figures refer to the meme token rather than an inverted pair.
    """
    url = (
        "https://api.dexscreener.com/token-pairs/v1/"
        f"{DEXSCREENER_CHAIN}/{quote(token, safe='')}"
    )

    try:
        response = await scanner.market.client.get(
            url,
            timeout=8.0,
            headers={"Accept": "application/json"},
        )
        if response.status_code == 429:
            return None
        response.raise_for_status()
        body = response.json()
    except Exception:
        return None

    pairs = body if isinstance(body, list) else []
    token_lower = token.lower()
    candidates = []

    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        if str(pair.get("chainId") or "").lower() != DEXSCREENER_CHAIN:
            continue
        base = pair.get("baseToken") or {}
        if str(base.get("address") or "").lower() != token_lower:
            continue
        candidates.append(pair)

    if not candidates:
        return None

    pair = max(
        candidates,
        key=lambda p: _number((p.get("liquidity") or {}).get("usd")),
    )

    txns = pair.get("txns") or {}
    volume = pair.get("volume") or {}
    price_change = pair.get("priceChange") or {}
    m5_txns = _window_value(txns, "m5")
    h1_txns = _window_value(txns, "h1")

    # Prefer 5-minute data. Fall back to h1 only if m5 is absent.
    buys = int(_number(m5_txns.get("buys")))
    sells = int(_number(m5_txns.get("sells")))
    volume_usd = _number(volume.get("m5"))

    if not m5_txns and h1_txns:
        buys = int(_number(h1_txns.get("buys")))
        sells = int(_number(h1_txns.get("sells")))
    if volume_usd <= 0 and "m5" not in volume:
        volume_usd = _number(volume.get("h1"))

    total = buys + sells
    buy_ratio = (buys / total * 100.0) if total else 0.0

    return {
        "pair_address": str(pair.get("pairAddress") or ""),
        "dex_id": str(pair.get("dexId") or ""),
        "price_usd": _number(pair.get("priceUsd")),
        "market_cap": _number(pair.get("marketCap") or pair.get("fdv")),
        "fdv": _number(pair.get("fdv")),
        "liquidity_usd": _number((pair.get("liquidity") or {}).get("usd")),
        "volume_usd": volume_usd,
        "buys": buys,
        "sells": sells,
        "buy_ratio": buy_ratio,
        "price_change_m5": _number(price_change.get("m5")),
        "pair_created_at": int(_number(pair.get("pairCreatedAt"))),
        "url": str(pair.get("url") or ""),
    }


def _market_passes(market: dict) -> tuple[bool, str]:
    min_liquidity = max(
        0.0, _float("ROBINHOOD_BUY_MIN_LIQUIDITY_USD", 1500.0)
    )
    min_volume = max(
        0.0, _float("ROBINHOOD_BUY_MIN_EARLY_VOLUME_USD", 750.0)
    )
    min_buys = max(1, _int("ROBINHOOD_BUY_MIN_BUYS", 5))
    min_sells = max(1, _int("ROBINHOOD_BUY_MIN_SELLS", 1))
    min_buy_ratio = max(
        0.0, min(100.0, _float("ROBINHOOD_BUY_MIN_BUY_RATIO", 55.0))
    )
    min_mcap = max(
        0.0, _float("ROBINHOOD_BUY_MIN_MARKET_CAP_USD", 5000.0)
    )
    max_mcap = max(
        min_mcap, _float("ROBINHOOD_BUY_MAX_MARKET_CAP_USD", 250000.0)
    )

    if market["liquidity_usd"] < min_liquidity:
        return False, "liquidity"
    if market["volume_usd"] < min_volume:
        return False, "volume"
    if market["buys"] < min_buys:
        return False, "buys"
    if market["sells"] < min_sells:
        return False, "no observed sells"
    if market["buy_ratio"] < min_buy_ratio:
        return False, "buy pressure"

    mcap = market["market_cap"]
    if mcap <= 0 or mcap < min_mcap or mcap > max_mcap:
        return False, "market cap"

    return True, "market pass"


async def _contractwolf_scan(scanner, token: str) -> dict | None:
    url = (
        f"{CONTRACTWOLF_BASE}/v1/scan/"
        f"{quote(token, safe='')}?format=json"
    )
    headers = {"Accept": "application/json"}
    key = str(os.getenv("CONTRACTWOLF_API_KEY", "") or "").strip()
    if key:
        headers["X-Api-Key"] = key

    try:
        response = await scanner.market.client.get(
            url,
            timeout=max(
                15.0,
                _float("ROBINHOOD_SECURITY_SCAN_TIMEOUT_SECONDS", 45.0),
            ),
            headers=headers,
        )
        if response.status_code == 429:
            return None
        response.raise_for_status()
        body = response.json()
        return body if isinstance(body, dict) else None
    except Exception as exc:
        print(
            "ROBINHOOD SECURITY SCAN ERROR — "
            f"{_short(token)} — {type(exc).__name__}",
            flush=True,
        )
        return None


def _security_passes(scan: dict) -> tuple[bool, str, dict]:
    """
    Conservative BUY gate.

    Unknown / unmeasured is treated as a rejection, not as a pass.
    A passing result reduces risk; it does not make a token guaranteed safe.
    """
    verdict = str(scan.get("verdict") or "").upper()
    checks = scan.get("checks") or {}
    if not isinstance(checks, dict):
        return False, "no security checks", {}

    danger = checks.get("danger_level") or {}
    honeypot = checks.get("honeypot") or {}
    trade_fee = checks.get("trade_fee") or {}
    lp_owner = checks.get("lp_owner") or {}
    lp_lock = checks.get("lp_lock") or {}
    proxy = checks.get("proxy") or {}
    mint = checks.get("mint") or {}
    deployer = checks.get("deployer") or {}
    bots = checks.get("bots") or {}
    holders = checks.get("holder_concentration") or {}

    danger_status = str(danger.get("status") or "").lower()
    honeypot_status = str(honeypot.get("status") or "").lower()
    lp_owner_status = str(lp_owner.get("status") or "").lower()
    lp_lock_status = str(lp_lock.get("status") or "").lower()
    proxy_status = str(proxy.get("status") or "").lower()
    mint_status = str(mint.get("status") or "").lower()
    deployer_status = str(deployer.get("status") or "").lower()
    bots_status = str(bots.get("status") or "").lower()
    holder_status = str(holders.get("status") or "").lower()

    round_trip_bps = _number(
        honeypot.get("round_trip_bps")
        or trade_fee.get("measured_round_trip_bps")
    )
    max_round_trip_bps = max(
        0.0, _float("ROBINHOOD_BUY_MAX_ROUND_TRIP_BPS", 1500.0)
    )

    if verdict != "CLEAR":
        return False, f"verdict {verdict or 'unknown'}", checks

    if danger_status not in {"low", "medium"}:
        return False, f"danger {danger_status or 'unknown'}", checks

    if honeypot_status not in {"not_honeypot", "sells_observed"}:
        return False, f"honeypot {honeypot_status or 'unknown'}", checks

    if round_trip_bps <= 0 or round_trip_bps > max_round_trip_bps:
        return False, f"round trip {round_trip_bps:.0f}bps", checks

    if lp_owner_status != "known_launchpad":
        return False, f"LP owner {lp_owner_status or 'unknown'}", checks

    if lp_lock_status not in {
        "locked",
        "launchpad_custody",
        "vault_custody",
    }:
        return False, f"LP lock {lp_lock_status or 'unknown'}", checks

    if proxy_status != "no":
        return False, f"proxy {proxy_status or 'unknown'}", checks

    if mint_status != "not_mintable":
        return False, f"mint {mint_status or 'unknown'}", checks

    if deployer_status != "clean":
        return False, f"deployer {deployer_status or 'unknown'}", checks

    if bots_status == "high":
        return False, "high bot activity", checks

    # Holder concentration is useful but can be partial on a seconds-old coin.
    # If the scanner has a complete measurement, enforce the threshold.
    top10_pct = _number(holders.get("top10_pct"), -1)
    max_top10 = max(
        1.0, min(100.0, _float("ROBINHOOD_BUY_MAX_TOP10_PERCENT", 50.0))
    )
    if holder_status == "ok" and top10_pct >= 0 and top10_pct > max_top10:
        return False, f"top10 {top10_pct:.1f}%", checks

    summary = {
        "verdict": verdict,
        "danger": danger_status,
        "honeypot": honeypot_status,
        "round_trip_bps": round_trip_bps,
        "lp_owner": lp_owner_status,
        "lp_lock": lp_lock_status,
        "proxy": proxy_status,
        "mint": mint_status,
        "deployer": deployer_status,
        "bots": bots_status,
        "top10_pct": top10_pct,
    }
    return True, "security pass", summary


async def _send_buy_ready(
    scanner,
    launch: dict,
    launch_ts: int,
    market: dict,
    security: dict,
) -> None:
    token = str(launch["token"])
    alert_key = token.lower()
    alerted = _alerted(scanner)
    if alert_key in alerted:
        return

    users = _eligible_users(scanner)
    if not users:
        return

    name, symbol = await _token_identity(scanner, token)
    age = max(0, int(time.time()) - int(launch_ts))

    round_trip_pct = _number(security.get("round_trip_bps")) / 100.0
    top10_pct = _number(security.get("top10_pct"), -1)

    holder_line = (
        f"Top-10 holders: <b>{top10_pct:.1f}%</b>\n"
        if top10_pct >= 0
        else ""
    )

    message = (
        "🟢✅ <b>ROBINHOOD BUY SETUP READY</b>\n\n"
        f"🪙 <b>{html.escape(name)} ({html.escape(symbol)})</b>\n"
        "Chain: <b>Robinhood Chain (4663)</b>\n"
        f"Age: <b>{age}s</b>\n"
        f"Source: <b>{html.escape(str(launch['source_label']))}</b>\n\n"
        f"Market cap: <b>${market['market_cap']:,.0f}</b>\n"
        f"Liquidity: <b>${market['liquidity_usd']:,.0f}</b>\n"
        f"Early volume: <b>${market['volume_usd']:,.0f}</b>\n"
        f"Buys / sells: <b>{market['buys']} / {market['sells']}</b>\n"
        f"Buy pressure: <b>{market['buy_ratio']:.1f}%</b>\n"
        f"5m move: <b>{market['price_change_m5']:+.1f}%</b>\n\n"
        "🛡 <b>Security gate passed</b>\n"
        "Honeypot simulation: <b>PASS ✅</b>\n"
        f"Measured round trip: <b>{round_trip_pct:.2f}%</b>\n"
        f"LP custody: <b>{html.escape(str(security.get('lp_lock') or 'verified'))}</b>\n"
        "Mint: <b>NOT MINTABLE ✅</b>\n"
        "Proxy: <b>NO ✅</b>\n"
        f"{holder_line}"
        f"\nContract: <code>{html.escape(token)}</code>\n\n"
        "⚠️ <b>Higher-confidence setup, not a guarantee.</b> "
        "Re-check the contract and transaction preview before signing. "
        "Manual wallet approval remains required."
    )

    buttons = [
        [
            {
                "text": str(launch["buy_label"]),
                "url": str(launch["buy_url"]),
            }
        ],
        [
            {
                "text": "📊 TOKEN",
                "url": _uniswap_token_url(token),
            }
        ],
    ]

    async def send_one(user: dict) -> bool:
        chat_id = str(user.get("chat_id") or "")
        if not chat_id:
            return False
        try:
            return bool(
                await scanner.telegram.send(
                    message,
                    chat_id,
                    buttons=buttons,
                    urgent=True,
                )
            )
        except Exception as exc:
            print(
                "ROBINHOOD BUY READY SEND ERROR — "
                f"chat {chat_id} — {type(exc).__name__}",
                flush=True,
            )
            return False

    results = await asyncio.gather(
        *(send_one(user) for user in users),
        return_exceptions=True,
    )
    sent = sum(result is True for result in results)

    if sent:
        alerted.add(alert_key)
        if len(alerted) > 10000:
            scanner._rh_buy_ready_alerted = set(list(alerted)[-5000:])

    print(
        "ROBINHOOD BUY SETUP READY SENT — "
        f"{html.escape(symbol)} — {_short(token)} — "
        f"MC ${market['market_cap']:,.0f} — "
        f"Liq ${market['liquidity_usd']:,.0f} — "
        f"alerts {sent}/{len(users)}",
        flush=True,
    )


async def _evaluate_buy_setup(
    scanner,
    launch: dict,
    launch_ts: int,
) -> None:
    """
    BUY-only mode:
    - NO Telegram alert on raw launch detection.
    - Wait for live market activity.
    - Require observed sells.
    - Require an independent Robinhood-chain security/honeypot scan.
    - Only then send BUY SETUP READY.
    """
    sem = _eval_semaphore(scanner)

    # Do not let a launch storm create a stale FIFO backlog.
    try:
        await asyncio.wait_for(sem.acquire(), timeout=0.10)
    except asyncio.TimeoutError:
        print(
            "ROBINHOOD EVAL SKIP BUSY — "
            f"{_short(launch.get('token'))}",
            flush=True,
        )
        return

    try:
        token = str(launch["token"])
        if not await _contract_exists(scanner, token):
            return

        eval_seconds = max(
            20.0,
            _float("ROBINHOOD_BUY_EVAL_SECONDS", 180.0),
        )
        poll_seconds = max(
            1.5,
            min(10.0, _float("ROBINHOOD_BUY_POLL_SECONDS", 3.0)),
        )
        deadline = time.monotonic() + eval_seconds
        last_reason = "waiting for market data"
        security_attempts = 0

        while time.monotonic() < deadline:
            market = await _dex_market(scanner, token)

            if market:
                market_ok, reason = _market_passes(market)
                last_reason = reason

                if market_ok:
                    security_attempts += 1
                    scan = await _contractwolf_scan(scanner, token)

                    if scan:
                        security_ok, security_reason, security = (
                            _security_passes(scan)
                        )
                        last_reason = security_reason

                        if security_ok:
                            await _send_buy_ready(
                                scanner,
                                launch,
                                launch_ts,
                                market,
                                security,
                            )
                            return

                        # Some early scans can be incomplete. Give only
                        # transient states one retry later in the window.
                        verdict = str(scan.get("verdict") or "").upper()
                        if verdict not in {
                            "INSUFFICIENT_DATA",
                            "NO_LIQUIDITY",
                            "BONDING_CURVE",
                        }:
                            print(
                                "ROBINHOOD BUY REJECT — "
                                f"{_short(token)} — {security_reason}",
                                flush=True,
                            )
                            return

                        if security_attempts >= 2:
                            return

            await asyncio.sleep(poll_seconds)

        print(
            "ROBINHOOD BUY WINDOW EXPIRED — "
            f"{_short(token)} — {last_reason}",
            flush=True,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(
            "ROBINHOOD BUY EVAL ERROR — "
            f"{_short(launch.get('token'))} — {type(exc).__name__}: {exc}",
            flush=True,
        )
    finally:
        sem.release()


async def _handle_log(scanner, log: dict) -> None:
    try:
        launch = _classify_launch(log)
        if not launch:
            return

        token = str(launch["token"])
        source = str(launch["source"])
        seen = _seen(scanner)
        seen_key = f"{source}:{token.lower()}"

        if seen_key in seen:
            return
        seen.add(seen_key)

        if len(seen) > 20000:
            scanner._rh_launch_seen = set(list(seen)[-10000:])

        block_hex = str(log.get("blockNumber") or "")
        launch_ts = await _block_timestamp(scanner, block_hex)

        # Deliberately NO Telegram "JUST LAUNCHED" alert here.
        # Only the completed BUY gate is allowed to notify the user.
        print(
            "ROBINHOOD LAUNCH DETECTED — silent evaluation — "
            f"{source} — {_short(token)}",
            flush=True,
        )

        asyncio.create_task(
            _evaluate_buy_setup(scanner, launch, launch_ts)
        )

    except Exception as exc:
        print(
            "ROBINHOOD launch handling error — "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


async def _websocket_loop(scanner, ws_url: str) -> None:
    emitters = _emitters()
    topics = _launch_topics()
    backoff = 1.0

    while True:
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=20,
                max_size=2 * 1024 * 1024,
            ) as ws:
                request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_subscribe",
                    "params": [
                        "logs",
                        {
                            "address": emitters,
                            "topics": [topics],
                        },
                    ],
                }
                await ws.send(json.dumps(request))
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                response = json.loads(raw)

                if response.get("error"):
                    raise RuntimeError(str(response["error"]))

                subscription_id = str(response.get("result") or "")
                if not subscription_id:
                    raise RuntimeError(
                        "eth_subscribe returned no subscription id"
                    )

                print(
                    "ROBINHOOD BUY-ONLY WS LIVE — "
                    f"emitters={len(emitters)} — topics={len(topics)}",
                    flush=True,
                )
                backoff = 1.0

                async for raw in ws:
                    try:
                        message = json.loads(raw)
                    except Exception:
                        continue

                    if message.get("method") != "eth_subscription":
                        continue

                    params = message.get("params") or {}
                    result = params.get("result")
                    if isinstance(result, dict):
                        asyncio.create_task(_handle_log(scanner, result))

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                "ROBINHOOD WS reconnect — "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            await asyncio.sleep(backoff)
            backoff = min(20.0, backoff * 2)


async def _http_poll_loop(scanner) -> None:
    emitters = _emitters()
    topics = _launch_topics()
    poll_seconds = max(
        0.25,
        min(5.0, _float("ROBINHOOD_POLL_SECONDS", 0.50)),
    )

    try:
        head = await _rpc(scanner, "eth_blockNumber", [])
        last_block = int(str(head or "0x0"), 16)
    except Exception:
        last_block = 0

    print(
        "ROBINHOOD BUY-ONLY HTTP LIVE — "
        f"poll={poll_seconds:.2f}s — emitters={len(emitters)} — "
        f"topics={len(topics)} — raw launch alerts OFF",
        flush=True,
    )

    while True:
        try:
            head_hex = await _rpc(scanner, "eth_blockNumber", [])
            head = int(str(head_hex or "0x0"), 16)

            if last_block <= 0:
                last_block = head

            if head > last_block:
                start_block = last_block + 1

                while start_block <= head:
                    end_block = min(head, start_block + 250)
                    logs = await _rpc(
                        scanner,
                        "eth_getLogs",
                        [
                            {
                                "fromBlock": hex(start_block),
                                "toBlock": hex(end_block),
                                "address": emitters,
                                "topics": [topics],
                            }
                        ],
                    )

                    for log in logs or []:
                        if isinstance(log, dict):
                            asyncio.create_task(_handle_log(scanner, log))

                    start_block = end_block + 1

                last_block = head

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                "ROBINHOOD HTTP poll error — "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

        await asyncio.sleep(poll_seconds)


async def _run(scanner) -> None:
    if not _truthy("ROBINHOOD_LAUNCH_ENABLED", True):
        return

    if not _emitters():
        print(
            "ROBINHOOD BUY-ONLY OFF — no launch emitters configured",
            flush=True,
        )
        return

    ws_url = _ws_url()
    if ws_url:
        await _websocket_loop(scanner, ws_url)
    else:
        await _http_poll_loop(scanner)


def install() -> None:
    from .scanner import Scanner

    if getattr(Scanner, "_robinhood_launch_installed", False):
        return

    Scanner._robinhood_launch_installed = True
    original_loop = Scanner.loop

    async def loop_with_robinhood(self, *args, **kwargs):
        task = None

        if _truthy("ROBINHOOD_LAUNCH_ENABLED", True):
            task = asyncio.create_task(_run(self))
            self._robinhood_launch_task = task

            def _done(t):
                if t.cancelled():
                    return
                try:
                    exc = t.exception()
                except Exception:
                    exc = None
                if exc is not None:
                    print(
                        "ROBINHOOD background task stopped — "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )

            task.add_done_callback(_done)

        try:
            return await original_loop(self, *args, **kwargs)
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    print(
                        "ROBINHOOD shutdown task error — "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )

    Scanner.loop = loop_with_robinhood

    mode = "WebSocket" if _ws_url() else "public HTTP fallback"

    print(
        "ROBINHOOD BUY-ONLY SCANNER ON — "
        f"chain 4663 — Pools.trade(4) + Pons V2 + Pons V1 legacy — "
        f"{mode} — instant WATCH alerts OFF — "
        "DexScreener market gate + ContractWolf on-chain safety/honeypot gate — "
        "only BUY SETUP READY can notify — manual wallet approval only.",
        flush=True,
    )
