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

# Official Uniswap Liquidity Launchpad v3.2.0 Instant Launch strategies
# on Robinhood Chain. One has creator fees enabled and one does not.
DEFAULT_INSTANT_STRATEGIES = (
    "0x23f8209572b4a1c2ad88a42749e830791fb027f1",
    "0xad44d55e7f8337c3ce113fbb591486e85be104b2",
)

# TokenLaunched(
#   bytes32 indexed poolId,
#   address indexed token,
#   address indexed finalPositionRecipient,
#   (address,address,uint24,int24,address) key
# )
TOKEN_LAUNCHED_TOPIC = (
    "0x3b3d2bafdcae274a232217e1f80ee4305d3af6aa25c8b14b1681bd68d18042a4"
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


def _rpc_url() -> str:
    return str(
        os.getenv("ROBINHOOD_RPC_URL", DEFAULT_RPC_URL) or DEFAULT_RPC_URL
    ).strip()


def _ws_url() -> str:
    # For production latency, set an Alchemy/QuickNode WebSocket endpoint here.
    # Example:
    # wss://robinhood-mainnet.g.alchemy.com/v2/YOUR_API_KEY
    return str(os.getenv("ROBINHOOD_WS_URL", "") or "").strip()


def _strategies() -> list[str]:
    raw = str(os.getenv("ROBINHOOD_INSTANT_STRATEGIES", "") or "").strip()
    values = (
        [x.strip().lower() for x in raw.split(",") if x.strip()]
        if raw
        else list(DEFAULT_INSTANT_STRATEGIES)
    )
    return [
        x
        for x in values
        if x.startswith("0x") and len(x) == 42
    ]


def _eligible_users(scanner) -> list[dict]:
    # Robinhood alerts do not require a Solana wallet. Any active subscriber
    # with live alerts enabled is eligible, including the admin.
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

    # Standard ABI dynamic string:
    # word0 = offset, word at offset = length, followed by bytes.
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

    # Some older/non-standard ERC20s return bytes32 for name/symbol.
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
    # Current Uniswap web-app format supports Robinhood Chain directly.
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
    return "https://robinhoodchain.blockscout.com/address/" + quote(
        token, safe=""
    )


def _seen(scanner) -> set[str]:
    value = getattr(scanner, "_rh_launch_seen", None)
    if value is None:
        value = set()
        scanner._rh_launch_seen = value
    return value


async def _send_launch_alert(scanner, log: dict) -> None:
    topics = list(log.get("topics") or [])
    if len(topics) < 4:
        return
    if str(topics[0]).lower() != TOKEN_LAUNCHED_TOPIC:
        return

    token = _topic_address(topics[2])
    if not token:
        return

    seen = _seen(scanner)
    token_key = token.lower()
    if token_key in seen:
        return
    seen.add(token_key)
    if len(seen) > 10000:
        scanner._rh_launch_seen = set(list(seen)[-5000:])

    block_hex = str(log.get("blockNumber") or "")
    launch_ts = await _block_timestamp(scanner, block_hex)
    age = max(0, int(time.time()) - int(launch_ts))

    name, symbol = await _token_identity(scanner, token)

    pool_id = str(topics[1] or "")
    strategy = str(log.get("address") or "").lower()
    creator_fees = (
        strategy
        == "0x23f8209572b4a1c2ad88a42749e830791fb027f1"
    )
    fees_line = "enabled" if creator_fees else "disabled"

    message = (
        "🟢⚡ <b>ROBINHOOD MEME — JUST LAUNCHED</b>\n\n"
        f"🪙 <b>{html.escape(name)} "
        f"({html.escape(symbol)})</b>\n"
        "Chain: <b>Robinhood Chain (4663)</b>\n"
        f"Age: <b>{age}s</b>\n"
        "Source: <b>Pools.xyz / Uniswap Instant Launch</b>\n"
        "Pool: <b>Uniswap v4 — native ETH pair</b>\n"
        f"Creator fees: <b>{fees_line}</b>\n"
        f"Contract: <code>{html.escape(token)}</code>\n"
        f"Pool ID: <code>{html.escape(_short(pool_id))}</code>\n\n"
        "⚡ <b>Tradable immediately.</b>\n"
        "Tap BUY NOW to open ETH → token on Robinhood Chain.\n\n"
        "⚠️ <b>INSTANT WATCH — not BUY SETUP READY.</b> "
        "This is a launch-speed alert. Token quality, price movement, "
        "liquidity depth and exit conditions can change immediately. "
        "Review the wallet transaction before signing."
    )

    buttons = [
        [
            {
                "text": "⚡ BUY NOW — UNISWAP",
                "url": _uniswap_buy_url(token),
            }
        ],
        [
            {
                "text": "📊 TOKEN",
                "url": _uniswap_token_url(token),
            },
            {
                "text": "🔎 EXPLORER",
                "url": _explorer_url(token),
            },
        ],
    ]

    users = _eligible_users(scanner)
    if not users:
        print(
            f"ROBINHOOD LAUNCH NO RECIPIENTS — {_short(token)}",
            flush=True,
        )
        return

    async def send_one(user: dict) -> bool:
        chat_id = str(user.get("chat_id") or "")
        if not chat_id:
            return False
        try:
            ok = await scanner.telegram.send(
                message,
                chat_id,
                buttons=buttons,
                urgent=True,
            )
            if ok:
                print(
                    f"ROBINHOOD ALERT OK — chat {chat_id} — "
                    f"{_short(token)} — age {age}s",
                    flush=True,
                )
            return bool(ok)
        except Exception as exc:
            print(
                f"ROBINHOOD ALERT ERROR — chat {chat_id} — "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            return False

    results = await asyncio.gather(
        *(send_one(user) for user in users),
        return_exceptions=True,
    )
    sent = sum(result is True for result in results)

    print(
        f"ROBINHOOD LAUNCH ALERT SENT — {_short(token)} — "
        f"{html.escape(symbol)} — age {age}s — alerts {sent}/{len(users)}",
        flush=True,
    )


async def _handle_log(scanner, log: dict) -> None:
    try:
        await _send_launch_alert(scanner, log)
    except Exception as exc:
        print(
            "ROBINHOOD launch handling error — "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


async def _websocket_loop(scanner, ws_url: str) -> None:
    strategies = _strategies()
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
                            "address": strategies,
                            "topics": [TOKEN_LAUNCHED_TOPIC],
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
                    raise RuntimeError("eth_subscribe returned no subscription id")

                print(
                    "ROBINHOOD LAUNCH WS LIVE — "
                    f"strategies={len(strategies)}",
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
                        asyncio.create_task(
                            _handle_log(scanner, result)
                        )
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
    strategies = _strategies()
    poll_seconds = max(
        0.25,
        min(5.0, _float("ROBINHOOD_POLL_SECONDS", 0.50)),
    )

    # Start from the current head so redeploying the bot does not spam old launches.
    try:
        head = await _rpc(scanner, "eth_blockNumber", [])
        last_block = int(str(head or "0x0"), 16)
    except Exception:
        last_block = 0

    print(
        "ROBINHOOD LAUNCH HTTP LIVE — "
        f"poll={poll_seconds:.2f}s — strategies={len(strategies)}",
        flush=True,
    )

    while True:
        try:
            head_hex = await _rpc(scanner, "eth_blockNumber", [])
            head = int(str(head_hex or "0x0"), 16)

            if last_block <= 0:
                last_block = head

            if head > last_block:
                # Keep each range small on reconnects and avoid public-RPC log caps.
                start = last_block + 1
                while start <= head:
                    end = min(head, start + 250)
                    logs = await _rpc(
                        scanner,
                        "eth_getLogs",
                        [
                            {
                                "fromBlock": hex(start),
                                "toBlock": hex(end),
                                "address": strategies,
                                "topics": [TOKEN_LAUNCHED_TOPIC],
                            }
                        ],
                    )
                    for log in logs or []:
                        if isinstance(log, dict):
                            asyncio.create_task(
                                _handle_log(scanner, log)
                            )
                    start = end + 1
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

    if not _strategies():
        print(
            "ROBINHOOD LAUNCH OFF — no strategy addresses configured",
            flush=True,
        )
        return

    ws_url = _ws_url()
    if ws_url:
        await _websocket_loop(scanner, ws_url)
    else:
        # Public HTTP RPC fallback works without an API key.
        # For the lowest latency, set ROBINHOOD_WS_URL to a production
        # Alchemy/QuickNode Robinhood Chain WebSocket endpoint.
        await _http_poll_loop(scanner)


def install() -> None:
    # Install by wrapping Scanner.start so this module remains independent
    # from Solana/Pump.fun launch handling.
    from .scanner import Scanner

    if getattr(Scanner, "_robinhood_launch_installed", False):
        return
    Scanner._robinhood_launch_installed = True

    original_start = Scanner.start

    async def start_with_robinhood(self, *args, **kwargs):
        task = None
        if _truthy("ROBINHOOD_LAUNCH_ENABLED", True):
            task = asyncio.create_task(_run(self))
            self._robinhood_launch_task = task
        try:
            return await original_start(self, *args, **kwargs)
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    Scanner.start = start_with_robinhood

    mode = "WebSocket" if _ws_url() else "public HTTP fallback"
    print(
        "ROBINHOOD INSTANT-LAUNCH SCANNER ON — "
        f"chain 4663 — official Pools.xyz/Uniswap Instant Launch — "
        f"{mode} — strategies={len(_strategies())} — "
        "one-tap Uniswap BUY link — manual wallet approval only.",
        flush=True,
    )
