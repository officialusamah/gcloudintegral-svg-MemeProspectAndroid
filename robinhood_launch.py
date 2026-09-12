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

# Robinhood Chain launch sources.
#
# Pools.trade / Uniswap Instant Launch:
# - two current launch strategies
# - two original launch strategies that still matter for coverage
POOLS_STRATEGIES = (
    "0x23f8209572b4a1c2ad88a42749e830791fb027f1",
    "0xad44d55e7f8337c3ce113fbb591486e85be104b2",
    "0xce57498d3474dcc244dfb6710ffbe6d4441cd2b2",
    "0x60d73b21cdf2ea846ab3d58699bbbb8f29d72491",
)

# TokenLaunched(bytes32,address,address,(address,address,uint24,int24,address))
POOLS_TOKEN_LAUNCHED_TOPIC = (
    "0x3b3d2bafdcae274a232217e1f80ee4305d3af6aa25c8b14b1681bd68d18042a4"
)

# Official Pons V2 factory.
PONS_V2_FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
# TokenLaunched(address,address,address,address,uint256,uint256)
PONS_V2_TOKEN_LAUNCHED_TOPIC = (
    "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
)

# Pons V1 is superseded but is cheap to keep as a legacy catch.
PONS_V1_FACTORY = "0xa5aab3f0c6eeadf30ef1d3eb997108e976351feb"
# TokenLaunched(address,address,address,address,address,uint256,uint256,uint256,uint256,uint256)
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


def _rpc_url() -> str:
    return str(
        os.getenv("ROBINHOOD_RPC_URL", DEFAULT_RPC_URL) or DEFAULT_RPC_URL
    ).strip()


def _ws_url() -> str:
    # For production latency, set an Alchemy/QuickNode WebSocket endpoint here.
    # Example:
    # wss://robinhood-mainnet.g.alchemy.com/v2/YOUR_API_KEY
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

    # Keep stable order while deduplicating.
    return list(dict.fromkeys(x.lower() for x in values))


def _launch_topics() -> list[str]:
    topics = [POOLS_TOKEN_LAUNCHED_TOPIC]
    if _truthy("ROBINHOOD_PONS_V2_ENABLED", True):
        topics.append(PONS_V2_TOKEN_LAUNCHED_TOPIC)
    if _truthy("ROBINHOOD_PONS_V1_LEGACY_ENABLED", True):
        topics.append(PONS_V1_TOKEN_LAUNCHED_TOPIC)
    return list(dict.fromkeys(x.lower() for x in topics))


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


def _data_words(data: str) -> list[str]:
    raw = str(data or "").lower().removeprefix("0x")
    if not raw:
        return []
    # Ignore malformed tails instead of letting one log break the listener.
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


def _pons_trade_url(token: str) -> str:
    # Verified official Pons frontend.
    return (
        "https://www.ponsfamily.com/launchpad/"
        + quote(token, safe="")
    )


def _classify_launch(log: dict) -> dict | None:
    address = str(log.get("address") or "").lower()
    topics = [str(x or "").lower() for x in (log.get("topics") or [])]
    if not topics:
        return None

    topic0 = topics[0]
    data_words = _data_words(log.get("data") or "0x")

    # Pools.trade / Uniswap Instant Launch.
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
            "venue": "Uniswap v4 — native ETH pool",
            "buy_url": _uniswap_buy_url(token),
            "buy_label": "⚡ BUY NOW — UNISWAP",
        }

    # Pons V2 bonding-curve launch.
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

        # Non-indexed word 0 is pairToken. Zero address means native ETH.
        pair_token = _word_address(data_words[0]) if data_words else ""
        quote_label = "native ETH" if not pair_token else _short(pair_token)

        return {
            "source": "pons_v2",
            "source_label": "Pons V2",
            "token": token,
            "curve": curve,
            "deployer": deployer,
            "pair_token": pair_token,
            "venue": f"Pons bonding curve — {quote_label} quote",
            "buy_url": _pons_trade_url(token),
            "buy_label": "⚡ BUY NOW — OFFICIAL PONS",
        }

    # Pons V1 legacy launch. Kept so an unexpected legacy launch is not missed.
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
            "venue": "Pons / Uniswap v3",
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


async def _send_launch_alert(scanner, log: dict) -> None:
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
    age = max(0, int(time.time()) - int(launch_ts))
    name, symbol = await _token_identity(scanner, token)

    extra_lines = []
    curve = str(launch.get("curve") or "")
    deployer = str(launch.get("deployer") or "")
    if curve:
        extra_lines.append(
            f"Curve: <code>{html.escape(_short(curve))}</code>"
        )
    if deployer:
        extra_lines.append(
            f"Creator: <code>{html.escape(_short(deployer))}</code>"
        )

    message = (
        "🟢⚡ <b>ROBINHOOD MEME — JUST LAUNCHED</b>\n\n"
        f"🪙 <b>{html.escape(name)} "
        f"({html.escape(symbol)})</b>\n"
        "Chain: <b>Robinhood Chain (4663)</b>\n"
        f"Age: <b>{age}s</b>\n"
        f"Source: <b>{html.escape(str(launch['source_label']))}</b>\n"
        f"Market: <b>{html.escape(str(launch['venue']))}</b>\n"
        + ("\n".join(extra_lines) + "\n" if extra_lines else "")
        + f"Contract: <code>{html.escape(token)}</code>\n\n"
        "⚡ <b>Launch detected directly on-chain.</b>\n"
        "Tap the trading button to open the exact contract.\n\n"
        "⚠️ <b>INSTANT WATCH — not BUY SETUP READY.</b> "
        "A fresh launch can move or fail extremely quickly. "
        "Check the token address and wallet transaction preview before signing. "
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
                "text": "🔎 EXPLORER",
                "url": _explorer_url(token),
            }
        ],
    ]

    # Pools tokens also get Uniswap's token view.
    if source == "pools":
        buttons[1].insert(
            0,
            {
                "text": "📊 TOKEN",
                "url": _uniswap_token_url(token),
            },
        )

    users = _eligible_users(scanner)
    if not users:
        print(
            f"ROBINHOOD LAUNCH NO RECIPIENTS — "
            f"{source} — {_short(token)}",
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
                    f"ROBINHOOD ALERT OK — {source} — chat {chat_id} — "
                    f"{_short(token)} — age {age}s",
                    flush=True,
                )
            return bool(ok)
        except Exception as exc:
            print(
                f"ROBINHOOD ALERT ERROR — {source} — chat {chat_id} — "
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
        f"ROBINHOOD LAUNCH ALERT SENT — {source} — "
        f"{_short(token)} — {html.escape(symbol)} — age {age}s — "
        f"alerts {sent}/{len(users)}",
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
                            # OR-match the first topic across all launch sources.
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
                    "ROBINHOOD LAUNCH WS LIVE — "
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
    emitters = _emitters()
    topics = _launch_topics()
    poll_seconds = max(
        0.25,
        min(5.0, _float("ROBINHOOD_POLL_SECONDS", 0.50)),
    )

    # Start at the current head to avoid dumping historical alerts on deploy.
    try:
        head = await _rpc(scanner, "eth_blockNumber", [])
        last_block = int(str(head or "0x0"), 16)
    except Exception:
        last_block = 0

    print(
        "ROBINHOOD LAUNCH HTTP LIVE — "
        f"poll={poll_seconds:.2f}s — emitters={len(emitters)} — "
        f"topics={len(topics)} — Pools.trade + Pons V2/V1",
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
                                # OR-match Pools and Pons launch signatures.
                                "topics": [topics],
                            }
                        ],
                    )
                    for log in logs or []:
                        if isinstance(log, dict):
                            asyncio.create_task(
                                _handle_log(scanner, log)
                            )
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
            "ROBINHOOD LAUNCH OFF — no launch emitters configured",
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
    # Scanner has loop(), not start(). Wrap loop() so the Robinhood listener
    # runs beside the existing scanner without changing the Solana path.
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

            # Consume unexpected task failures so they never bring down
            # the existing Solana scanner loop.
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
        "ROBINHOOD INSTANT-LAUNCH SCANNER ON — "
        f"chain 4663 — Pools.trade(4) + Pons V2 + Pons V1 legacy — "
        f"{mode} — emitters={len(_emitters())} — "
        "green Telegram alerts — official trading links — "
        "manual wallet approval only.",
        flush=True,
    )
