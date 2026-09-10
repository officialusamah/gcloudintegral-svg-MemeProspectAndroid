from __future__ import annotations

import asyncio
import json
import os
import time

import websockets

from . import ultra_early as ue
from .scanner import Scanner


PUMPPORTAL_WS_URL = os.getenv(
    "PUMPPORTAL_WS_URL",
    "wss://pumpportal.fun/api/data",
).strip()

PUMPPORTAL_ENABLED = os.getenv(
    "PUMPPORTAL_LAUNCH_FIRST_ENABLED",
    "true",
).strip().lower() in {"1", "true", "yes", "y", "on"}


def _event_age_ts(payload: dict) -> int:
    now = int(time.time())
    for key in ("timestamp", "createdTimestamp", "created_at", "createdAt"):
        raw = payload.get(key)
        if raw is None:
            continue
        try:
            ts = float(raw)
            if ts > 10_000_000_000:
                ts /= 1000.0
            if 1_500_000_000 <= ts <= now + 60:
                return int(ts)
        except (TypeError, ValueError):
            pass
    return now


async def _pumpportal_listener(scanner) -> None:
    """
    Data-only low-latency Pump.fun launch detector.

    It never signs or submits a transaction. Every detected mint is passed into
    the bot's existing Launch-First safety, liquidity, Jupiter BUY/reverse-SELL
    and Telegram manual-approval pipeline.
    """
    if not PUMPPORTAL_ENABLED:
        print("PUMPPORTAL launch detector OFF", flush=True)
        return

    while True:
        try:
            async with websockets.connect(
                PUMPPORTAL_WS_URL,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_size=2_000_000,
            ) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                print(
                    "PUMPPORTAL CONNECTED — 0-30s launch detector ON",
                    flush=True,
                )

                async for raw in ws:
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        continue
                    if not isinstance(payload, dict):
                        continue

                    mint = str(
                        payload.get("mint")
                        or payload.get("tokenAddress")
                        or payload.get("address")
                        or ""
                    ).strip()

                    if not mint or ue._is_ultra_excluded_mint(mint):
                        continue

                    if not ue._claim_ultra_address(scanner, mint):
                        continue

                    listed_ts = _event_age_ts(payload)
                    age = max(0, int(time.time()) - listed_ts)
                    short = mint[:6] + "…" + mint[-4:] if len(mint) > 12 else mint

                    print(
                        f"PUMPPORTAL DETECTED — {short} — age {age}s",
                        flush=True,
                    )

                    asyncio.create_task(
                        ue._handle_ultra_listing(scanner, mint, listed_ts)
                    )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                f"PUMPPORTAL listener error: {type(exc).__name__}: {exc}",
                flush=True,
            )
            await asyncio.sleep(2)


def install() -> None:
    if getattr(Scanner, "_pumpportal_launch_first_installed", False):
        return

    Scanner._pumpportal_launch_first_installed = True
    original_loop = Scanner.loop

    async def patched_loop(self):
        task = asyncio.create_task(_pumpportal_listener(self))
        try:
            await original_loop(self)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    Scanner.loop = patched_loop

    print(
        "PUMPPORTAL Launch-First integration installed — "
        "manual wallet approval remains required.",
        flush=True,
    )
