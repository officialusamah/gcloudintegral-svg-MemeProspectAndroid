from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import struct
import time
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import websockets
from solders.pubkey import Pubkey

from .models import Candidate, PairSnapshot
from .scoring import score_token

SOL_MINT = "So11111111111111111111111111111111111111112"
BIRDEYE_WS = "wss://public-api.birdeye.so/socket/solana?x-api-key={}"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_TRADE_EVENT_DISCRIMINATOR = bytes.fromhex("bddb7fd34ee661ee")

# Never allow common Solana base assets, stablecoins, or program/system IDs
# to enter the Ultra meme-token pipeline. This also protects against a
# temporary mint-resolution mistake in a complex Pump transaction.
ULTRA_EXCLUDED_MINTS = {
    SOL_MINT,  # wrapped SOL
    "11111111111111111111111111111111",  # System Program / native SOL address
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token Program
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022 Program
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}

def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default

def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default

ULTRA_ENABLED = _bool("ULTRA_EARLY_ENABLED", True)
ULTRA_MIN_SCORE = max(1, min(100, _int("ULTRA_EARLY_MIN_SCORE", 65)))
ULTRA_MAX_AGE_SECONDS = max(15, _int("ULTRA_EARLY_MAX_AGE_SECONDS", 120))
ULTRA_RETRY_SECONDS = max(15, _int("ULTRA_EARLY_RETRY_SECONDS", 120))
ULTRA_MIN_LIQUIDITY = max(0.0, _float("ULTRA_EARLY_MIN_LIQUIDITY_USD", 10_000))
ULTRA_PUMP_MIN_REAL_SOL = max(0.1, _float("ULTRA_PUMP_MIN_REAL_SOL", 1.0))
ULTRA_MAX_MCAP = max(1.0, _float("ULTRA_EARLY_MAX_MARKET_CAP_USD", 750_000))
ULTRA_MIN_TXNS = max(1, _int("ULTRA_EARLY_MIN_TXNS", 8))
ULTRA_MIN_BUY_RATIO = min(1.0, max(0.50, _float("ULTRA_EARLY_MIN_BUY_RATIO", 0.65)))
ULTRA_MIN_VOLUME = max(0.0, _float("ULTRA_EARLY_MIN_VOLUME_USD", 2_000))
ULTRA_MAX_M5_MOVE = max(10.0, _float("ULTRA_EARLY_MAX_5M_MOVE_PERCENT", 80.0))
PROFIT_TARGET_USD = max(0.10, _float("LIVE_NET_PROFIT_TARGET_USD", 1.00))
EST_EXIT_COST_USD = max(0.0, _float("LIVE_ESTIMATED_EXIT_COST_USD", 0.05))
PROFIT_CHECK_SECONDS = max(1.0, _float("LIVE_PROFIT_CHECK_SECONDS", 2.0))
ULTRA_REST_POLL_SECONDS = max(30.0, _float("ULTRA_EARLY_REST_POLL_SECONDS", 60.0))
ULTRA_DEX_REQUEST_INTERVAL = max(0.5, _float("ULTRA_EARLY_DEX_REQUEST_INTERVAL", 1.0))
ULTRA_DEX_RETRY_SECONDS = max(1.0, _float("ULTRA_EARLY_DEX_RETRY_SECONDS", 2.0))
ULTRA_DEX_429_BACKOFF_STEPS = (2.0, 4.0, 8.0)
ULTRA_SELLABILITY_ENABLED = _bool("ULTRA_EARLY_SELLABILITY_CHECK", True)
ULTRA_SELLABILITY_MAX_ROUNDTRIP_LOSS_PCT = min(50.0, max(1.0, _float("ULTRA_EARLY_MAX_ROUNDTRIP_LOSS_PCT", 15.0)))
ULTRA_SELLABILITY_SLIPPAGE_BPS = max(50, min(3000, _int("ULTRA_EARLY_SELLABILITY_SLIPPAGE_BPS", 300)))
SOLANA_PUMP_LISTENER_ENABLED = _bool("ULTRA_SOLANA_PUMP_LISTENER_ENABLED", True)
BIRDEYE_WS_ENABLED = _bool("ULTRA_BIRDEYE_WS_ENABLED", False)
BIRDEYE_CU_BACKOFF_SECONDS = max(300, _int("ULTRA_BIRDEYE_CU_BACKOFF_SECONDS", 3600))

def _is_ultra_excluded_mint(address: str) -> bool:
    return str(address or "").strip() in ULTRA_EXCLUDED_MINTS


async def _mint_authorities_safe(scanner, mint: str) -> bool:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}],
    }
    try:
        r = await scanner.market.client.post(scanner.cfg.solana_rpc_url, json=payload)
        r.raise_for_status()
        value = ((r.json().get("result") or {}).get("value") or {})
        parsed = ((value.get("data") or {}).get("parsed") or {})
        info = parsed.get("info") or {}
        if not info:
            return False
        return info.get("mintAuthority") is None and info.get("freezeAuthority") is None
    except Exception:
        return False

async def _historical_sol_usd(market, ts: int) -> float:
    if getattr(market, "birdeye_api_key", "") and ts > 0:
        try:
            r = await market.client.get(
                "https://public-api.birdeye.so/defi/historical_price_unix",
                headers={
                    "X-API-KEY": market.birdeye_api_key,
                    "x-chain": "solana",
                    "accept": "application/json",
                },
                params={"address": SOL_MINT, "unixtime": int(ts)},
            )
            r.raise_for_status()
            payload = r.json()
            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            if isinstance(data, dict):
                for key in ("value", "price", "priceUsd", "price_usd"):
                    try:
                        value = float(data.get(key) or 0)
                        if value > 0:
                            return value
                    except (TypeError, ValueError):
                        pass
        except Exception:
            pass
    try:
        return float(await market.solana_sol_price_usd())
    except Exception:
        return 0.0

def _account_key_text(key: Any) -> str:
    if isinstance(key, str):
        return key
    if isinstance(key, dict):
        return str(key.get("pubkey") or "")
    return str(key or "")

def _owned_token_raw(rows: list | None, owner: str, mint: str) -> tuple[int, int]:
    total = 0
    decimals = 0
    for row in rows or []:
        if str(row.get("owner") or "") != owner:
            continue
        if str(row.get("mint") or "") != mint:
            continue
        ui = row.get("uiTokenAmount") or {}
        total += int(ui.get("amount") or 0)
        decimals = max(decimals, int(ui.get("decimals") or 0))
    return total, decimals

async def _wallet_trade_legs(market, owner: str, mint: str, since_ts: int, rpc_url: str) -> list[dict]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [owner, {"limit": 40, "commitment": "confirmed"}],
    }
    r = await market.client.post(rpc_url, json=payload)
    r.raise_for_status()
    rows = r.json().get("result") or []
    legs: list[dict] = []

    for row in rows:
        if row.get("err") is not None:
            continue
        block_time = int(row.get("blockTime") or 0)
        if block_time and block_time < int(since_ts) - 60:
            continue
        sig = str(row.get("signature") or "")
        if not sig:
            continue
        txp = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [sig, {
                "encoding": "jsonParsed",
                "commitment": "confirmed",
                "maxSupportedTransactionVersion": 0,
            }],
        }
        txr = await market.client.post(rpc_url, json=txp)
        txr.raise_for_status()
        tx = txr.json().get("result") or {}
        if not tx:
            continue
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            continue
        msg = ((tx.get("transaction") or {}).get("message") or {})
        keys = msg.get("accountKeys") or []
        wallet_index = None
        for i, key in enumerate(keys):
            if _account_key_text(key) == owner:
                wallet_index = i
                break
        if wallet_index is None:
            continue

        pre_raw, pre_dec = _owned_token_raw(meta.get("preTokenBalances"), owner, mint)
        post_raw, post_dec = _owned_token_raw(meta.get("postTokenBalances"), owner, mint)
        token_delta = post_raw - pre_raw
        if token_delta == 0:
            continue

        pre_bal = meta.get("preBalances") or []
        post_bal = meta.get("postBalances") or []
        if wallet_index >= len(pre_bal) or wallet_index >= len(post_bal):
            continue
        sol_delta = (int(post_bal[wallet_index]) - int(pre_bal[wallet_index])) / 1_000_000_000
        fee_sol = int(meta.get("fee") or 0) / 1_000_000_000
        decimals = max(pre_dec, post_dec)
        legs.append({
            "signature": sig,
            "block_time": int(tx.get("blockTime") or block_time or 0),
            "token_delta_raw": int(token_delta),
            "token_decimals": int(decimals),
            "sol_delta": float(sol_delta),
            "fee_sol": float(fee_sol),
        })

    legs.sort(key=lambda x: int(x.get("block_time") or 0))
    return legs

async def _actual_trade_pnl_usd(live, p: dict) -> dict:
    legs = await _wallet_trade_legs(
        live.market,
        str(p["wallet_address"]),
        str(p["token_address"]),
        int(p.get("opened_ts") or 0),
        live.cfg.solana_rpc_url,
    )
    buys = [x for x in legs if int(x.get("token_delta_raw") or 0) > 0 and float(x.get("sol_delta") or 0) < 0]
    sells = [x for x in legs if int(x.get("token_delta_raw") or 0) < 0 and float(x.get("sol_delta") or 0) > 0]
    if not buys or not sells:
        return {}

    buy_cost_usd = 0.0
    sell_proceeds_usd = 0.0
    for leg in buys:
        sol_usd = await _historical_sol_usd(live.market, int(leg.get("block_time") or 0))
        buy_cost_usd += abs(float(leg.get("sol_delta") or 0)) * sol_usd
    for leg in sells:
        sol_usd = await _historical_sol_usd(live.market, int(leg.get("block_time") or 0))
        sell_proceeds_usd += max(0.0, float(leg.get("sol_delta") or 0)) * sol_usd

    if buy_cost_usd <= 0:
        return {}
    pnl = sell_proceeds_usd - buy_cost_usd
    ret = (pnl / buy_cost_usd) * 100.0
    return {
        "buy_cost_usd": buy_cost_usd,
        "sell_proceeds_usd": sell_proceeds_usd,
        "pnl_usd": pnl,
        "return_pct": ret,
    }

async def _ultra_send_buy(scanner, user: dict, snap, result, age_seconds: int) -> None:
    total = int(snap.buys_m5 or 0) + int(snap.sells_m5 or 0)
    buy_ratio = (int(snap.buys_m5 or 0) / total * 100.0) if total else 0.0
    await scanner.telegram.send(
        f"⚡🚨 <b>ULTRA EARLY BUY SETUP</b> 🚨⚡\n\n"
        f"🟣 <b>{html.escape(snap.token_name)} ({html.escape(snap.token_symbol)})</b>\n"
        f"Age: <b>{age_seconds}s</b>\n"
        f"Prospect score: <b>{result.score}/100</b>\n"
        f"Liquidity: <b>${snap.liquidity_usd:,.0f}</b>\n"
        f"Market cap: <b>${snap.market_cap:,.0f}</b>\n"
        f"Early volume: <b>${snap.volume_m5:,.0f}</b>\n"
        f"Buys / sells: <b>{snap.buys_m5} / {snap.sells_m5}</b> ({buy_ratio:.0f}% buys)\n"
        f"Data: <b>{'Pump.fun on-chain' if snap.source == 'pump_onchain' else 'DEX market'}</b>\n"
        f"Contract: <code>{html.escape(snap.token_address)}</code>\n\n"
        f"💰 Planned buy: <b>{scanner.cfg.live_buy_sol:.3f} SOL</b>\n"
        f"🎯 Target: <b>about +${PROFIT_TARGET_USD:.2f} NET → SELL ALL</b>\n"
        "🛑 Emergency risk protection remains active.\n\n"
        "Tap BUY NOW, verify the token and amount, then approve manually in your wallet.",
        str(user["chat_id"]),
        buttons=scanner.live._buy_buttons(snap.token_address),
        urgent=True,
    )


def _ultra_dex_state(scanner):
    if not hasattr(scanner, "_ultra_dex_lock"):
        scanner._ultra_dex_lock = asyncio.Lock()
    if not hasattr(scanner, "_ultra_dex_next_allowed"):
        scanner._ultra_dex_next_allowed = 0.0
    if not hasattr(scanner, "_ultra_dex_429_level"):
        scanner._ultra_dex_429_level = 0
    return scanner._ultra_dex_lock

async def _ultra_pair_snapshot(scanner, candidate):
    """Return (snapshot, reason, retry_seconds) using one globally paced DEX queue."""
    lock = _ultra_dex_state(scanner)

    async with lock:
        now = time.monotonic()
        wait_for = max(0.0, float(scanner._ultra_dex_next_allowed) - now)
        if wait_for > 0:
            await asyncio.sleep(wait_for)

        try:
            snap = await scanner.market.pair_snapshot(candidate)

            # Success: reset 429 escalation and resume normal 1 req/sec pacing.
            scanner._ultra_dex_429_level = 0
            scanner._ultra_dex_next_allowed = (
                time.monotonic() + ULTRA_DEX_REQUEST_INTERVAL
            )
            return snap, "", ULTRA_DEX_RETRY_SECONDS

        except Exception as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)

            if status is not None:
                status = int(status)

                if status == 429:
                    level = min(
                        int(scanner._ultra_dex_429_level),
                        len(ULTRA_DEX_429_BACKOFF_STEPS) - 1,
                    )
                    backoff = ULTRA_DEX_429_BACKOFF_STEPS[level]
                    scanner._ultra_dex_429_level = min(
                        level + 1,
                        len(ULTRA_DEX_429_BACKOFF_STEPS) - 1,
                    )
                    scanner._ultra_dex_next_allowed = time.monotonic() + backoff
                    return None, f"DEX HTTP 429 — backoff {backoff:.0f}s", backoff

                # Temporary server failures: use the longest cooldown.
                if status >= 500:
                    backoff = ULTRA_DEX_429_BACKOFF_STEPS[-1]
                    scanner._ultra_dex_next_allowed = time.monotonic() + backoff
                    return None, f"DEX HTTP {status} — backoff {backoff:.0f}s", backoff

                # Other HTTP responses still obey normal global pacing.
                scanner._ultra_dex_next_allowed = (
                    time.monotonic() + ULTRA_DEX_REQUEST_INTERVAL
                )
                return None, f"DEX HTTP {status}", ULTRA_DEX_RETRY_SECONDS

            scanner._ultra_dex_next_allowed = (
                time.monotonic() + ULTRA_DEX_REQUEST_INTERVAL
            )
            return None, f"DEX error {type(exc).__name__}", ULTRA_DEX_RETRY_SECONDS


async def _ultra_sellability_check(scanner, token_mint: str, wallet_address: str) -> tuple[bool, str]:
    """
    Require an executable Jupiter route in both directions for the planned buy size.
    This is a quote/order check only. It never signs or submits a transaction.
    """
    if not ULTRA_SELLABILITY_ENABLED:
        return True, "disabled"

    planned_lamports = max(1, int(float(scanner.cfg.live_buy_sol) * 1_000_000_000))
    headers = scanner.market._jupiter_headers()
    base_url = "https://api.jup.ag/swap/v2"
    common = {
        "taker": wallet_address,
        "slippageBps": str(int(ULTRA_SELLABILITY_SLIPPAGE_BPS)),
        "excludeRouters": "jupiterz",
    }

    try:
        # First ensure the planned SOL buy has an executable route and learn expected token output.
        buy_r = await scanner.market.client.get(
            f"{base_url}/order",
            params={
                **common,
                "inputMint": SOL_MINT,
                "outputMint": token_mint,
                "amount": str(planned_lamports),
            },
            headers=headers,
        )
        buy_r.raise_for_status()
        buy_order = buy_r.json() if buy_r.content else {}
        buy_out = int(buy_order.get("outAmount") or 0)
        if buy_out <= 0 or not str(buy_order.get("transaction") or ""):
            return False, "Jupiter BUY route unavailable"

        # Now require a real reverse route for the full expected token amount.
        sell_r = await scanner.market.client.get(
            f"{base_url}/order",
            params={
                **common,
                "inputMint": token_mint,
                "outputMint": SOL_MINT,
                "amount": str(buy_out),
            },
            headers=headers,
        )
        sell_r.raise_for_status()
        sell_order = sell_r.json() if sell_r.content else {}
        sell_out = int(sell_order.get("outAmount") or 0)
        if sell_out <= 0 or not str(sell_order.get("transaction") or ""):
            return False, "Jupiter SELL route unavailable"

        roundtrip_loss_pct = max(
            0.0,
            (1.0 - (sell_out / planned_lamports)) * 100.0,
        )
        if roundtrip_loss_pct > ULTRA_SELLABILITY_MAX_ROUNDTRIP_LOSS_PCT:
            return (
                False,
                f"Jupiter round-trip loss {roundtrip_loss_pct:.1f}% > "
                f"{ULTRA_SELLABILITY_MAX_ROUNDTRIP_LOSS_PCT:.0f}%",
            )

        return True, f"SELL route OK — round-trip loss {roundtrip_loss_pct:.1f}%"

    except Exception as exc:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status is not None:
            return False, f"Jupiter route check HTTP {int(status)}"
        return False, f"Jupiter route check failed: {type(exc).__name__}"

async def _handle_ultra_listing(scanner, address: str, listed_ts: int) -> None:
    if not ULTRA_ENABLED or not address:
        return
    if _is_ultra_excluded_mint(address):
        print(
            f"ULTRA REJECT — {address[:12]} — excluded SOL/stable/system mint",
            flush=True,
        )
        return
    started = time.time()
    candidate = Candidate(chain="solana", address=address, source="ultra_launch")
    short = address[:6] + "…" + address[-4:] if len(address) > 12 else address
    print(f"ULTRA DETECTED — {short} — age 0s", flush=True)

    last_reason = ""
    while time.time() - started <= ULTRA_RETRY_SECONDS:
        now = int(time.time())
        age = max(0, now - int(listed_ts or now))
        if age > ULTRA_MAX_AGE_SECONDS:
            print(f"ULTRA REJECT — {short} — age {age}s — too old", flush=True)
            return

        snap = await _pump_direct_snapshot(scanner, address, listed_ts)
        market_source = "PUMP ONCHAIN" if snap else ""

        if not snap:
            snap, dex_reason, dex_sleep = await _ultra_pair_snapshot(scanner, candidate)
            market_source = "DEX" if snap else ""
        else:
            dex_reason, dex_sleep = "", 1.0

        if not snap:
            reason = dex_reason or "market data not ready"
            if reason != last_reason:
                print(f"ULTRA WAIT — {short} — age {age}s — {reason}", flush=True)
                last_reason = reason
            await asyncio.sleep(dex_sleep)
            continue

        source_reason = f"{market_source} market data ready"
        if source_reason != last_reason:
            print(f"ULTRA DATA — {short} — age {age}s — {source_reason}", flush=True)
            last_reason = source_reason

        total = int(snap.buys_m5 or 0) + int(snap.sells_m5 or 0)
        buy_ratio = int(snap.buys_m5 or 0) / total if total else 0.0

        is_pump_onchain = str(getattr(snap, "source", "")) == "pump_onchain"
        real_sol_reserve = (
            float((getattr(snap, "raw", {}) or {}).get("real_sol_reserves") or 0)
            / 1_000_000_000
            if is_pump_onchain else 0.0
        )

        if is_pump_onchain:
            depth_check = (
                real_sol_reserve >= ULTRA_PUMP_MIN_REAL_SOL,
                f"Pump real reserve {real_sol_reserve:.2f} SOL < "
                f"{ULTRA_PUMP_MIN_REAL_SOL:.2f} SOL",
            )
        else:
            depth_check = (
                snap.liquidity_usd >= ULTRA_MIN_LIQUIDITY,
                f"DEX liquidity ${snap.liquidity_usd:,.0f} < "
                f"${ULTRA_MIN_LIQUIDITY:,.0f}",
            )

        checks = [
            (bool(snap.price_usd and snap.price_usd > 0), "price not ready"),
            depth_check,
            (snap.market_cap > 0, "market cap not ready"),
            (snap.market_cap <= ULTRA_MAX_MCAP, f"market cap ${snap.market_cap:,.0f} > ${ULTRA_MAX_MCAP:,.0f}"),
            (total >= ULTRA_MIN_TXNS, f"transactions {total} < {ULTRA_MIN_TXNS}"),
            (buy_ratio >= ULTRA_MIN_BUY_RATIO, f"buy ratio {buy_ratio*100:.0f}% < {ULTRA_MIN_BUY_RATIO*100:.0f}%"),
            (snap.volume_m5 >= ULTRA_MIN_VOLUME, f"volume ${snap.volume_m5:,.0f} < ${ULTRA_MIN_VOLUME:,.0f}"),
            (snap.price_change_m5 <= ULTRA_MAX_M5_MOVE, f"5m move {snap.price_change_m5:+.1f}% > {ULTRA_MAX_M5_MOVE:.0f}%"),
        ]

        failed = next((msg for ok, msg in checks if not ok), "")
        if failed:
            if failed != last_reason:
                print(f"ULTRA WAIT — {short} — age {age}s — {failed}", flush=True)
                last_reason = failed
            await asyncio.sleep(1)
            continue

        authorities_safe = await _mint_authorities_safe(scanner, address)
        if not authorities_safe:
            print(f"ULTRA REJECT — {short} — age {age}s — mint/freeze authority unsafe or unreadable", flush=True)
            return

        try:
            security = await scanner.market.security("solana", address)
        except Exception as exc:
            security = None
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            print(
                f"ULTRA SECURITY provider unavailable — {short} — "
                f"{'HTTP ' + str(status) if status else type(exc).__name__}; "
                "on-chain mint/freeze gate still passed",
                flush=True,
            )

        result = score_token(
            snap,
            scanner.store.previous_snapshot("solana", address),
            security,
            # A Pump bonding curve is not a normal DEX LP. Its dedicated real-SOL
            # reserve gate above replaces the DEX liquidity hard-block.
            min_liquidity=0.0 if is_pump_onchain else scanner.cfg.min_liquidity_usd,
            min_mcap=scanner.cfg.min_market_cap_usd,
            max_mcap=scanner.cfg.max_market_cap_usd,
            max_age_minutes=scanner.cfg.max_pair_age_minutes,
        )

        if is_pump_onchain:
            # Reward only ACTUAL SOL accumulated in the curve. Never count virtual
            # reserves as cash liquidity. Keep the bonus modest so activity,
            # buy pressure, volume and momentum still have to carry the score.
            if real_sol_reserve >= 5.0:
                depth_bonus = 10
            elif real_sol_reserve >= 3.0:
                depth_bonus = 7
            elif real_sol_reserve >= 1.0:
                depth_bonus = 4
            else:
                depth_bonus = 0

            if depth_bonus:
                result.score = min(100, int(result.score) + depth_bonus)
                result.reasons.append(
                    f"Pump bonding curve real reserve {real_sol_reserve:.2f} SOL"
                )

        scanner.store.save_snapshot(snap, result)

        if result.hard_block:
            print(f"ULTRA REJECT — {short} — age {age}s — hard security block", flush=True)
            return
        if result.score < ULTRA_MIN_SCORE:
            reason = f"score {result.score}/100 < {ULTRA_MIN_SCORE}"
            if reason != last_reason:
                print(f"ULTRA WAIT — {short} — age {age}s — {reason}", flush=True)
                last_reason = reason
            await asyncio.sleep(1)
            continue

        sent = 0
        for user in scanner.accounts.trade_users():
            chat_id = str(user["chat_id"])
            if scanner.live.get_open(chat_id, "solana", address):
                continue
            if scanner.live._has_history(chat_id, "solana", address):
                continue
            if scanner.live.open_count(chat_id) >= scanner.cfg.live_signal_max_open_positions:
                continue
            wallet_address = str(user["wallet_address"])
            sellable, sell_reason = await _ultra_sellability_check(
                scanner, address, wallet_address
            )
            if not sellable:
                print(
                    f"ULTRA REJECT — {short} — age {age}s — {sell_reason}",
                    flush=True,
                )
                continue
            print(
                f"ULTRA SELLABILITY PASS — {short} — age {age}s — {sell_reason}",
                flush=True,
            )
            try:
                bal = await scanner.live._wallet_balance(wallet_address, address)
            except Exception:
                continue
            scanner.live._open_signal(user, snap, result, int(bal.get("raw") or 0))
            await _ultra_send_buy(scanner, user, snap, result, age)
            sent += 1

        if sent:
            # Record the first alert globally. The normal scanner may later alert
            # this token only if it crosses into the configured Strong Prospect tier.
            scanner.store.mark_alert("solana", address, result.score)
            print(f"ULTRA PASS — {short} — age {age}s — score {result.score}/100 — alerts {sent}", flush=True)
        else:
            print(f"ULTRA REJECT — {short} — age {age}s — no eligible trade users / slot unavailable", flush=True)
        return

    print(f"ULTRA REJECT — {short} — retry window expired after {ULTRA_RETRY_SECONDS}s", flush=True)


def _claim_ultra_address(scanner, address: str) -> bool:
    seen = getattr(scanner, "_ultra_seen_addresses", None)
    if seen is None:
        seen = set()
        scanner._ultra_seen_addresses = seen
    if address in seen:
        return False
    seen.add(address)
    # Bound memory while keeping plenty of recent addresses for de-duplication.
    if len(seen) > 5000:
        for old in list(seen)[:1000]:
            seen.discard(old)
    return True

async def _ultra_rest_fallback(scanner) -> None:
    """Poll Birdeye new-listing REST feed when the websocket is quiet.

    The first response is used only as a baseline so the bot does not treat
    already-listed tokens as brand-new on every restart.
    """
    print("ULTRA REST TASK STARTED", flush=True)
    if not ULTRA_ENABLED:
        print("ULTRA REST TASK DISABLED — ULTRA_EARLY_ENABLED is false", flush=True)
        return
    if not scanner.cfg.birdeye_api_key:
        print("ULTRA REST fallback disabled: missing Birdeye API key", flush=True)
        return

    baseline_ready = False
    local_seen: set[str] = set()

    while True:
        try:
            print("ULTRA REST REQUEST START", flush=True)

            # Query Birdeye directly here so a bad optional meme-platform
            # parameter cannot disable the whole REST backup feed.
            headers = {
                "X-API-KEY": scanner.cfg.birdeye_api_key,
                "x-chain": "solana",
                "accept": "application/json",
            }
            url = "https://public-api.birdeye.so/defi/v2/tokens/new_listing"
            params = {"limit": 20, "meme_platform_enabled": "true"}

            response = await scanner.market.client.get(
                url, headers=headers, params=params
            )

            if response.status_code == 400:
                body = (response.text or "")[:500].replace("\n", " ")
                if "compute units usage limit exceeded" in body.lower():
                    print(
                        f"ULTRA REST PAUSED — Birdeye compute units exhausted — "
                        f"retry in {BIRDEYE_CU_BACKOFF_SECONDS}s",
                        flush=True,
                    )
                    await asyncio.sleep(BIRDEYE_CU_BACKOFF_SECONDS)
                    continue
                print(
                    f"ULTRA REST 400 — meme filter request rejected — body={body!r} — "
                    "retrying without optional meme_platform_enabled",
                    flush=True,
                )
                response = await scanner.market.client.get(
                    url, headers=headers, params={"limit": 20}
                )

            if response.status_code >= 400:
                body = (response.text or "")[:500].replace("\n", " ")
                if response.status_code == 400 and "compute units usage limit exceeded" in body.lower():
                    print(
                        f"ULTRA REST PAUSED — Birdeye compute units exhausted — "
                        f"retry in {BIRDEYE_CU_BACKOFF_SECONDS}s",
                        flush=True,
                    )
                    await asyncio.sleep(BIRDEYE_CU_BACKOFF_SECONDS)
                    continue
                print(
                    f"ULTRA REST HTTP {response.status_code} — body={body!r}",
                    flush=True,
                )
                response.raise_for_status()

            payload = response.json()
            data = payload.get("data", payload) if isinstance(payload, dict) else payload
            if isinstance(data, dict):
                items = (
                    data.get("items")
                    or data.get("tokens")
                    or data.get("list")
                    or data.get("result")
                    or []
                )
            elif isinstance(data, list):
                items = data
            else:
                items = []

            batch = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                address = (
                    item.get("address")
                    or item.get("tokenAddress")
                    or item.get("token_address")
                )
                if address:
                    batch.append(
                        Candidate(
                            chain="solana",
                            address=str(address),
                            source="birdeye_new_listing",
                        )
                    )

            addresses = [
                str(getattr(c, "address", "") or "")
                for c in (batch or [])
                if str(getattr(c, "address", "") or "")
            ]

            if not baseline_ready:
                local_seen.update(addresses)
                # Share the initial baseline with websocket de-duplication.
                shared = getattr(scanner, "_ultra_seen_addresses", None)
                if shared is None:
                    shared = set()
                    scanner._ultra_seen_addresses = shared
                shared.update(addresses)
                baseline_ready = True
                print(
                    f"ULTRA REST fallback ready — baseline {len(addresses)} listings — "
                    f"poll every {ULTRA_REST_POLL_SECONDS:.0f}s",
                    flush=True,
                )
            else:
                for address in reversed(addresses):
                    if address in local_seen:
                        continue
                    local_seen.add(address)
                    if not _claim_ultra_address(scanner, address):
                        continue
                    short = address[:6] + "…" + address[-4:] if len(address) > 12 else address
                    print(f"ULTRA REST DETECTED — {short}", flush=True)
                    asyncio.create_task(
                        _handle_ultra_listing(scanner, address, int(time.time()))
                    )

                if len(local_seen) > 5000:
                    # Keep the current feed plus a bounded recent set.
                    local_seen = set(addresses) | set(list(local_seen)[-3000:])

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"ULTRA REST error: {type(exc).__name__}: {exc}", flush=True)

        # REST is only an emergency backup to the Birdeye WebSocket.
        # Keep it slow to preserve Birdeye compute units.
        await asyncio.sleep(ULTRA_REST_POLL_SECONDS)

def _solana_ws_url(rpc_url: str) -> str:
    """Convert an HTTPS Solana RPC endpoint into its WebSocket endpoint."""
    override = str(os.getenv("ULTRA_SOLANA_WS_URL", "") or "").strip()
    if override:
        return override
    raw = str(rpc_url or "").strip()
    if not raw:
        return "wss://api.mainnet-beta.solana.com"
    parts = urlsplit(raw)
    scheme = "wss" if parts.scheme in {"https", "wss"} else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


def _tx_account_key_text(key: Any) -> str:
    if isinstance(key, str):
        return key
    if isinstance(key, dict):
        return str(key.get("pubkey") or "")
    return str(key or "")



def _pump_trade_event_from_logs(logs: list) -> list[dict]:
    """
    Decode Pump.fun Anchor TradeEvent payloads emitted as `Program data:` logs.

    Only the stable prefix is decoded:
      mint, sol_amount, token_amount, is_buy, user, timestamp,
      virtual_sol_reserves, virtual_token_reserves,
      real_sol_reserves, real_token_reserves.
    """
    out: list[dict] = []
    for line in logs or []:
        s = str(line or "")
        if "Program data: " not in s:
            continue
        encoded = s.split("Program data: ", 1)[1].strip()
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception:
            continue
        if len(raw) < 129 or raw[:8] != PUMP_TRADE_EVENT_DISCRIMINATOR:
            continue
        try:
            off = 8
            mint = str(Pubkey.from_bytes(raw[off:off + 32])); off += 32
            sol_amount = struct.unpack_from("<Q", raw, off)[0]; off += 8
            token_amount = struct.unpack_from("<Q", raw, off)[0]; off += 8
            is_buy = bool(raw[off]); off += 1
            user = str(Pubkey.from_bytes(raw[off:off + 32])); off += 32
            timestamp = struct.unpack_from("<q", raw, off)[0]; off += 8
            virtual_sol = struct.unpack_from("<Q", raw, off)[0]; off += 8
            virtual_token = struct.unpack_from("<Q", raw, off)[0]; off += 8
            real_sol = struct.unpack_from("<Q", raw, off)[0]; off += 8
            real_token = struct.unpack_from("<Q", raw, off)[0]
        except Exception:
            continue
        out.append({
            "mint": mint,
            "sol_amount": int(sol_amount),
            "token_amount": int(token_amount),
            "is_buy": bool(is_buy),
            "user": user,
            "timestamp": int(timestamp),
            "virtual_sol_reserves": int(virtual_sol),
            "virtual_token_reserves": int(virtual_token),
            "real_sol_reserves": int(real_sol),
            "real_token_reserves": int(real_token),
        })
    return out


def _record_pump_trade_logs(scanner, logs: list) -> None:
    market = getattr(scanner, "_pump_direct_market", None)
    if market is None:
        market = {}
        scanner._pump_direct_market = market

    now = int(time.time())
    for event in _pump_trade_event_from_logs(logs):
        mint = str(event.get("mint") or "")
        if not mint:
            continue
        row = market.setdefault(mint, {"trades": [], "created_ts": now})
        row["last"] = event
        row.setdefault("trades", []).append(event)

        # Keep only the most recent hour and cap memory per mint.
        cutoff = now - 3600
        row["trades"] = [
            x for x in row["trades"][-2500:]
            if int(x.get("timestamp") or now) >= cutoff
        ]

    # Bound the number of token histories retained in memory.
    if len(market) > 3000:
        newest = sorted(
            market.items(),
            key=lambda kv: int((kv[1].get("last") or {}).get("timestamp") or kv[1].get("created_ts") or 0),
            reverse=True,
        )[:2000]
        scanner._pump_direct_market = dict(newest)


async def _pump_token_supply(scanner, mint: str) -> tuple[float, int]:
    cache = getattr(scanner, "_pump_supply_cache", None)
    if cache is None:
        cache = {}
        scanner._pump_supply_cache = cache
    if mint in cache:
        return cache[mint]

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenSupply",
        "params": [mint, {"commitment": "confirmed"}],
    }
    try:
        r = await scanner.market.client.post(scanner.cfg.solana_rpc_url, json=payload)
        r.raise_for_status()
        value = ((r.json().get("result") or {}).get("value") or {})
        decimals = int(value.get("decimals") or 0)
        amount_raw = int(value.get("amount") or 0)
        if amount_raw <= 0:
            return 0.0, decimals
        supply_ui = amount_raw / (10 ** decimals)
        cache[mint] = (float(supply_ui), decimals)
        return cache[mint]
    except Exception:
        return 0.0, 0


async def _pump_sol_usd(scanner) -> float:
    now = time.monotonic()
    cached = getattr(scanner, "_pump_sol_usd_cache", None)
    if cached:
        price, expires = cached
        if now < expires and float(price) > 0:
            return float(price)
    try:
        price = float(await scanner.market.solana_sol_price_usd())
    except Exception:
        price = 0.0
    if price > 0:
        scanner._pump_sol_usd_cache = (price, now + 20.0)
    return price


async def _pump_direct_snapshot(scanner, mint: str, listed_ts: int):
    """
    Build a conservative early-market snapshot directly from Pump.fun TradeEvents.

    `liquidity_usd` is deliberately based on REAL SOL reserves only. It is a
    conservative on-curve depth proxy, not virtual liquidity. The existing
    Jupiter round-trip gate remains mandatory before any BUY alert.
    """
    market = getattr(scanner, "_pump_direct_market", {}) or {}
    row = market.get(mint)
    if not row:
        return None

    now = int(time.time())
    trades = [
        x for x in (row.get("trades") or [])
        if now - int(x.get("timestamp") or now) <= 300
    ]
    if not trades:
        return None

    last = row.get("last") or trades[-1]
    v_sol = int(last.get("virtual_sol_reserves") or 0)
    v_token = int(last.get("virtual_token_reserves") or 0)
    real_sol = int(last.get("real_sol_reserves") or 0)
    if v_sol <= 0 or v_token <= 0:
        return None

    sol_usd = await _pump_sol_usd(scanner)
    if sol_usd <= 0:
        return None

    supply_ui, decimals = await _pump_token_supply(scanner, mint)
    if supply_ui <= 0:
        return None

    token_scale = 10 ** decimals
    latest_price_sol = (v_sol / 1_000_000_000) / (v_token / token_scale)
    price_usd = latest_price_sol * sol_usd
    market_cap = price_usd * supply_ui

    buys = sum(1 for x in trades if bool(x.get("is_buy")))
    sells = len(trades) - buys
    volume_usd = sum(int(x.get("sol_amount") or 0) for x in trades) / 1_000_000_000 * sol_usd

    def event_price_usd(x):
        vs = int(x.get("virtual_sol_reserves") or 0)
        vt = int(x.get("virtual_token_reserves") or 0)
        if vs <= 0 or vt <= 0:
            return 0.0
        return ((vs / 1_000_000_000) / (vt / token_scale)) * sol_usd

    first_price = next((event_price_usd(x) for x in trades if event_price_usd(x) > 0), 0.0)
    move_m5 = ((price_usd / first_price) - 1.0) * 100.0 if first_price > 0 else 0.0

    # Conservative: only real SOL sitting in the bonding curve counts as depth.
    # Do not use virtual reserves as fake liquidity.
    liquidity_usd = max(0.0, real_sol / 1_000_000_000 * sol_usd)

    created = int(listed_ts or row.get("created_ts") or now)
    return PairSnapshot(
        chain="solana",
        token_address=mint,
        token_name=f"Pump.fun {mint[:6]}",
        token_symbol="PUMP",
        pair_address="",
        dex_id="pumpfun",
        url=f"https://pump.fun/coin/{mint}",
        market_cap=float(market_cap),
        fdv=float(market_cap),
        liquidity_usd=float(liquidity_usd),
        pair_created_at_ms=created * 1000,
        price_usd=float(price_usd),
        price_change_m5=float(move_m5),
        price_change_h1=float(move_m5),
        volume_m5=float(volume_usd),
        volume_h1=float(volume_usd),
        buys_m5=int(buys),
        sells_m5=int(sells),
        buys_h1=int(buys),
        sells_h1=int(sells),
        socials_count=0,
        boosts_active=0,
        source="pump_onchain",
        raw={
            "market_source": "pump_onchain_trade_events",
            "real_sol_reserves": real_sol,
            "virtual_sol_reserves": v_sol,
            "virtual_token_reserves": v_token,
            "token_decimals": decimals,
        },
    )


async def _pump_mint_from_signature(scanner, signature: str) -> str:
    """Resolve a Pump create transaction and return the newly created token mint."""
    if not signature:
        return ""

    delays = (0.15, 0.35, 0.75, 1.25, 2.0)
    tx = {}
    for delay in delays:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        }
        try:
            r = await scanner.market.client.post(scanner.cfg.solana_rpc_url, json=payload)
            r.raise_for_status()
            tx = (r.json().get("result") or {})
            if tx:
                break
        except Exception:
            pass
        await asyncio.sleep(delay)

    if not tx:
        return ""

    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return ""

    # A Pump creation normally introduces a mint into post-token balances.
    pre_mints = {
        str(x.get("mint") or "")
        for x in (meta.get("preTokenBalances") or [])
        if isinstance(x, dict) and x.get("mint")
    }
    post_mints = [
        str(x.get("mint") or "")
        for x in (meta.get("postTokenBalances") or [])
        if isinstance(x, dict) and x.get("mint")
    ]
    new_mints = [m for m in dict.fromkeys(post_mints) if m and m not in pre_mints]
    pump_suffix = [m for m in new_mints if m.lower().endswith("pump")]
    if len(pump_suffix) == 1:
        return pump_suffix[0]
    if len(new_mints) == 1:
        return new_mints[0]

    # Fallback: for Pump's create instruction the mint is the first account.
    msg = ((tx.get("transaction") or {}).get("message") or {})
    instructions = msg.get("instructions") or []
    for ix in instructions:
        if not isinstance(ix, dict):
            continue
        if str(ix.get("programId") or "") != PUMP_PROGRAM:
            continue
        accounts = ix.get("accounts") or []
        if not accounts:
            continue
        mint = _tx_account_key_text(accounts[0])
        if mint and mint != PUMP_PROGRAM:
            return mint
    return ""


async def _handle_pump_signature(scanner, signature: str) -> None:
    mint = await _pump_mint_from_signature(scanner, signature)
    if not mint:
        print(f"ULTRA SOLANA WAIT — could not resolve Pump mint — tx {signature[:12]}…", flush=True)
        return
    direct_market = getattr(scanner, "_pump_direct_market", None)
    if direct_market is None:
        direct_market = {}
        scanner._pump_direct_market = direct_market
    direct_market.setdefault(mint, {"trades": [], "created_ts": int(time.time())})

    if _is_ultra_excluded_mint(mint):
        print(
            f"ULTRA SOLANA IGNORE — {mint[:12]} — excluded SOL/stable/system mint",
            flush=True,
        )
        return

    if not _claim_ultra_address(scanner, mint):
        return
    short = mint[:6] + "…" + mint[-4:] if len(mint) > 12 else mint
    print(f"ULTRA SOLANA DETECTED — {short} — Pump create", flush=True)
    asyncio.create_task(_handle_ultra_listing(scanner, mint, int(time.time())))


async def _solana_pump_listener(scanner) -> None:
    """Primary no-Birdeye launch detector: subscribe to Pump create logs on Solana."""
    if not ULTRA_ENABLED or not SOLANA_PUMP_LISTENER_ENABLED:
        return

    ws_url = _solana_ws_url(scanner.cfg.solana_rpc_url)
    seen_sigs: set[str] = set()
    while True:
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_size=2_000_000,
            ) as ws:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [PUMP_PROGRAM]},
                        {"commitment": "processed"},
                    ],
                }))
                ack = json.loads(await ws.recv())
                if ack.get("error"):
                    raise RuntimeError(f"logsSubscribe failed: {ack['error']}")
                print("ULTRA SOLANA Pump listener connected — primary launch detector ON", flush=True)

                async for raw in ws:
                    try:
                        payload = json.loads(raw)
                        if payload.get("method") != "logsNotification":
                            continue
                        value = (((payload.get("params") or {}).get("result") or {}).get("value") or {})
                        if value.get("err") is not None:
                            continue
                        logs = value.get("logs") or []
                        _record_pump_trade_logs(scanner, logs)

                        if not any(
                            "Instruction: Create" in str(line)
                            or "Instruction: CreateV2" in str(line)
                            for line in logs
                        ):
                            continue
                        signature = str(value.get("signature") or "")
                        if not signature or signature in seen_sigs:
                            continue
                        seen_sigs.add(signature)
                        if len(seen_sigs) > 5000:
                            seen_sigs = set(list(seen_sigs)[-3000:])
                        asyncio.create_task(_handle_pump_signature(scanner, signature))
                    except Exception as exc:
                        print("ULTRA SOLANA message error:", repr(exc), flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("ULTRA SOLANA Pump listener error:", repr(exc), flush=True)
            await asyncio.sleep(5)


async def _ultra_listener(scanner) -> None:
    if not ULTRA_ENABLED or not BIRDEYE_WS_ENABLED:
        if ULTRA_ENABLED:
            print("ULTRA Birdeye websocket backup OFF — direct Solana is primary", flush=True)
        return
    if not scanner.cfg.birdeye_api_key:
        print("ultra early websocket disabled: missing Birdeye API key", flush=True)
        return

    url = BIRDEYE_WS.format(quote(scanner.cfg.birdeye_api_key, safe=""))
    while True:
        try:
            async with websockets.connect(
                url,
                subprotocols=["echo-protocol"],
                origin="ws://public-api.birdeye.so",
                additional_headers={
                    "Sec-WebSocket-Origin": "ws://public-api.birdeye.so",
                },
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            ) as ws:
                await ws.send(json.dumps({
                    "type": "SUBSCRIBE_TOKEN_NEW_LISTING",
                    "meme_platform_enabled": True,
                }))
                print("ultra early Birdeye websocket connected", flush=True)
                async for raw in ws:
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        continue
                    if payload.get("type") != "TOKEN_NEW_LISTING_DATA":
                        continue
                    data = payload.get("data") or {}
                    address = str(data.get("address") or "")
                    listed_ts = int(data.get("liquidityAddedAt") or time.time())
                    if address and _claim_ultra_address(scanner, address):
                        asyncio.create_task(_handle_ultra_listing(scanner, address, listed_ts))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("ultra early websocket error:", repr(exc), flush=True)
            await asyncio.sleep(5)

async def _estimated_profit_monitor(scanner) -> None:
    while True:
        try:
            for p in scanner.live.open_positions():
                if not int(p.get("wallet_buy_confirmed") or 0):
                    continue
                if int(p.get("sell_requested") or 0):
                    continue
                try:
                    snap = await scanner.market.pair_snapshot(
                        Candidate(chain=str(p["chain"]), address=str(p["token_address"]), source="profit_monitor")
                    )
                    if not snap or not snap.price_usd or snap.price_usd <= 0:
                        continue
                    wallet = await scanner.live._wallet_balance(str(p["wallet_address"]), str(p["token_address"]))
                    token_ui = float(wallet.get("ui") or 0)
                    if token_ui <= 0:
                        continue
                    fill = await scanner.market.solana_recent_buy_fill(
                        str(p["wallet_address"]),
                        str(p["token_address"]),
                        int(p.get("opened_ts") or 0),
                        scanner.cfg.solana_rpc_url,
                    )
                    if not fill:
                        continue
                    sol_spent = float(fill.get("sol_spent") or 0) + float(fill.get("network_fee_sol") or 0)
                    if sol_spent <= 0:
                        continue
                    sol_usd = await _historical_sol_usd(scanner.market, int(fill.get("block_time") or 0))
                    if sol_usd <= 0:
                        continue
                    buy_cost_usd = sol_spent * sol_usd
                    current_value_usd = token_ui * float(snap.price_usd)
                    estimated_net = current_value_usd - buy_cost_usd - EST_EXIT_COST_USD
                    if estimated_net < PROFIT_TARGET_USD:
                        continue
                    entry = float(p.get("confirmed_entry_price_usd") or p.get("signal_entry_price_usd") or 0)
                    ret = ((float(snap.price_usd) / entry) - 1.0) * 100 if entry > 0 else 0.0
                    scanner.live._request_sell(int(p["id"]))
                    await scanner.telegram.send(
                        f"🎯🚨 <b>${PROFIT_TARGET_USD:.2f} NET TARGET REACHED — SELL ALL NOW</b>\n\n"
                        f"<b>{html.escape(str(p.get('token_symbol') or '?'))}</b>\n"
                        f"Estimated net profit: <b>+${estimated_net:.2f}</b>\n"
                        f"Tracked market return: <b>{ret:+.1f}%</b>\n\n"
                        "Tap SELL ALL NOW, choose MAX, verify the quote and approve manually. "
                        "Final P/L will be recalculated from confirmed on-chain buy and sell amounts.",
                        str(p["user_chat_id"]),
                        buttons=scanner.live._sell_buttons(str(p["token_address"])),
                        urgent=True,
                    )
                except Exception as exc:
                    print("$1 profit monitor position error:", repr(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print("$1 profit monitor error:", repr(exc))
        await asyncio.sleep(PROFIT_CHECK_SECONDS)

def install() -> None:
    from .scanner import Scanner
    from .live import LiveTrader
    from .db import Store

    if getattr(Scanner, "_ultra_early_installed", False):
        return
    Scanner._ultra_early_installed = True

    original_scanner_init = Scanner.__init__
    original_loop = Scanner.loop
    original_send_sell_confirmed = LiveTrader._send_sell_confirmed

    def patched_can_alert(
        self, chain: str, address: str, score: int, cooldown_minutes: int,
        strong_threshold: int = 85, rapid_acceleration: bool = False
    ) -> bool:
        """
        Alert policy:
        1) First qualifying alert for a token: send once.
        2) After that, alert again only if it upgrades from below the
           Strong Prospect threshold to Strong Prospect.
        No timer/cooldown repeats and no rapid-acceleration repeats.
        """
        row = self.alert_state(chain, address)
        if not row:
            return True
        last_score = int(row["last_score"])
        return last_score < strong_threshold <= int(score)

    def patched_scanner_init(self, *args, **kwargs):
        original_scanner_init(self, *args, **kwargs)
        self.cfg.live_signal_take_profit_percent = 999999.0
        self._ultra_seen_addresses = set()
        self._ultra_dex_lock = asyncio.Lock()
        self._ultra_dex_next_allowed = 0.0
        self._ultra_dex_429_level = 0
        self._pump_direct_market = {}
        self._pump_supply_cache = {}
        self._pump_sol_usd_cache = None

    async def patched_send_buy(self, user, snap, result):
        await self.telegram.send(
            f"🚨🚨 <b>ACTION REQUIRED — BUY SETUP READY</b> 🚨🚨\n\n"
            f"🟢 <b>{html.escape(snap.token_name)} ({html.escape(snap.token_symbol)})</b>\n"
            f"Prospect score: <b>{result.score}/100</b>\n"
            f"Signal price: <b>${snap.price_usd:.10f}</b>\n"
            f"Market cap: <b>${snap.market_cap:,.0f}</b>\n"
            f"Liquidity: <b>${snap.liquidity_usd:,.0f}</b>\n\n"
            f"💰 Planned buy: <b>{self.cfg.live_buy_sol:.3f} SOL</b>\n"
            f"🎯 Profit target: <b>about +${PROFIT_TARGET_USD:.2f} NET → SELL ALL</b>\n"
            f"🛑 Risk exit: <b>-{self.cfg.live_signal_stop_loss_percent:.0f}% → EXIT ALL</b>\n\n"
            "Tap BUY NOW, verify the coin and amount, then approve manually. "
            "The bot waits for public-wallet confirmation before tracking P/L.",
            str(user["chat_id"]),
            buttons=self._buy_buttons(snap.token_address),
            urgent=True,
        )

    async def patched_send_buy_confirmed(self, p, snap, entry_price, signature):
        sig_line = f"\nTransaction: <code>{html.escape(signature[:18])}…</code>" if signature else ""
        await self.telegram.send(
            f"✅ <b>BUY CONFIRMED ON-CHAIN</b>\n\n"
            f"<b>{html.escape(snap.token_symbol)}</b>\n"
            f"Tracked entry: <b>${entry_price:.10f}</b>{sig_line}\n"
            f"🎯 Target: <b>about +${PROFIT_TARGET_USD:.2f} NET → SELL ALL alert</b>\n"
            "🛑 Emergency risk protection remains active.\n\n"
            "Monitoring has started from the confirmed wallet purchase.",
            str(p["user_chat_id"]),
            urgent=True,
        )

    async def patched_send_sell_confirmed(self, p, snap, tracked_ret):
        actual = {}
        try:
            actual = await _actual_trade_pnl_usd(self, p)
        except Exception as exc:
            print("actual realized P/L calculation error:", repr(exc))
        if actual:
            try:
                with self.connect() as c:
                    cols = {row[1] for row in c.execute("PRAGMA table_info(user_live_positions)").fetchall()}
                    if "realized_pnl_usd" not in cols:
                        c.execute("ALTER TABLE user_live_positions ADD COLUMN realized_pnl_usd REAL")
                    if "actual_buy_cost_usd" not in cols:
                        c.execute("ALTER TABLE user_live_positions ADD COLUMN actual_buy_cost_usd REAL")
                    if "actual_sell_proceeds_usd" not in cols:
                        c.execute("ALTER TABLE user_live_positions ADD COLUMN actual_sell_proceeds_usd REAL")
                    c.execute(
                        "UPDATE user_live_positions SET realized_return_pct=?, realized_pnl_usd=?, actual_buy_cost_usd=?, actual_sell_proceeds_usd=? WHERE id=?",
                        (
                            float(actual["return_pct"]),
                            float(actual["pnl_usd"]),
                            float(actual["buy_cost_usd"]),
                            float(actual["sell_proceeds_usd"]),
                            int(p["id"]),
                        ),
                    )
                await self.telegram.send(
                    f"✅ <b>SELL CONFIRMED ON-CHAIN</b>\n\n"
                    f"<b>{html.escape(snap.token_symbol)}</b>\n"
                    f"Actual buy cost: <b>${actual['buy_cost_usd']:.2f}</b>\n"
                    f"Actual sell proceeds: <b>${actual['sell_proceeds_usd']:.2f}</b>\n"
                    f"Realized net P/L: <b>{actual['pnl_usd']:+.2f} USD</b>\n"
                    f"Realized return: <b>{actual['return_pct']:+.1f}%</b>\n\n"
                    "Position closed. The live slot is free for the next prospect.",
                    str(p["user_chat_id"]),
                    urgent=True,
                )
                return
            except Exception as exc:
                print("actual P/L persistence error:", repr(exc))
        await original_send_sell_confirmed(self, p, snap, tracked_ret)

    async def patched_loop(self):
        print(
            f"ULTRA DEX global queue ON — interval={ULTRA_DEX_REQUEST_INTERVAL:.1f}s "
            f"429_backoff=2s→4s→8s",
            flush=True,
        )
        print(
            f"ULTRA sellability gate ON — Jupiter reverse route required — "
            f"max_roundtrip_loss={ULTRA_SELLABILITY_MAX_ROUNDTRIP_LOSS_PCT:.0f}%",
            flush=True,
        )
        print(
            "ALERT DEDUPE ON — first alert once; repeat only on Strong Prospect upgrade",
            flush=True,
        )
        print(
            f"ULTRA EARLY window — max_age={ULTRA_MAX_AGE_SECONDS}s retry_window={ULTRA_RETRY_SECONDS}s",
            flush=True,
        )
        print(
            "ULTRA primary detector — direct Solana Pump.fun logsSubscribe",
            flush=True,
        )
        print(
            "ULTRA market data — Pump.fun TradeEvents primary; DEX Screener fallback",
            flush=True,
        )
        print(
            "ULTRA mint exclusions ON — SOL/WSOL/stable/system addresses blocked",
            flush=True,
        )
        print(
            f"ULTRA Pump depth gate — real reserve >= {ULTRA_PUMP_MIN_REAL_SOL:.2f} SOL; "
            f"DEX pairs still require ${ULTRA_MIN_LIQUIDITY:,.0f} liquidity",
            flush=True,
        )
        solana_task = asyncio.create_task(_solana_pump_listener(self))
        ultra_task = asyncio.create_task(_ultra_listener(self))
        rest_task = asyncio.create_task(_ultra_rest_fallback(self))
        profit_task = asyncio.create_task(_estimated_profit_monitor(self))
        try:
            await original_loop(self)
        finally:
            solana_task.cancel()
            ultra_task.cancel()
            rest_task.cancel()
            profit_task.cancel()

    Store.can_alert = patched_can_alert
    Scanner.__init__ = patched_scanner_init
    Scanner.loop = patched_loop
    LiveTrader._send_buy = patched_send_buy
    LiveTrader._send_buy_confirmed = patched_send_buy_confirmed
    LiveTrader._send_sell_confirmed = patched_send_sell_confirmed
