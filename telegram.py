from __future__ import annotations
import html
import httpx
from .db import Store

class Telegram:
    def __init__(self, token: str, store: Store, configured_chat_id: str = ""):
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is missing. Put it in .env.")
        self.base = f"https://api.telegram.org/bot{token}"
        self.store = store
        self.chat_id = configured_chat_id or store.get_setting("telegram_chat_id")
        self.offset = 0
        self.client = httpx.AsyncClient(timeout=35)

    async def close(self):
        await self.client.aclose()

    async def send(self, text: str, chat_id: str | None = None, buttons=None, urgent: bool = False):
        target = chat_id or self.chat_id
        if not target:
            return False
        payload = {
            "chat_id": target,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": False if urgent else False,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        r = await self.client.post(
            f"{self.base}/sendMessage",
            json=payload,
        )
        r.raise_for_status()
        return True

    async def updates(self):
        r = await self.client.get(
            f"{self.base}/getUpdates",
            params={"timeout": 25, "offset": self.offset, "allowed_updates": '["message"]'},
        )
        r.raise_for_status()
        data = r.json()
        return data.get("result", []) if data.get("ok") else []

    def accept_chat(self, chat_id: str):
        # Multi-user mode: every private chat can register.
        return bool(str(chat_id or "").strip())

def alert_message(
    s, score, age_min: float, headline: str | None = None,
    acceleration: str = "", previous_score: int | None = None
) -> str:
    sells = max(s.sells_m5, 1)
    ratio = s.buys_m5 / sells
    reasons = "\n".join(f"✅ {html.escape(x)}" for x in score.reasons[:6]) or "✅ Strong composite score"
    warnings = "\n".join(f"⚠️ {html.escape(x)}" for x in score.warnings[:5])
    security = "checked" if score.security_checked else "not available"
    link = f'\n<a href="{html.escape(s.url)}">Open chart</a>' if s.url else ""

    if headline:
        label = headline
    elif score.score >= 85:
        label = "🟢 STRONG PROSPECT"
    elif score.score >= 60:
        label = "🟡 EARLY WATCH"
    else:
        label = "🔴 LOW SCORE"

    ultra = any("Ultra-early breakout profile" in x for x in score.reasons)
    ultra_line = "\n⚡ <b>ULTRA-EARLY BREAKOUT PROFILE</b>" if ultra else ""
    accel_line = f"\n🚀 <b>RAPID ACCELERATION</b>\n{html.escape(acceleration)}" if acceleration else ""
    score_move = (
        f"\nScore move: <b>{previous_score} → {score.score}</b>"
        if previous_score is not None and previous_score != score.score else ""
    )

    return (
        f"<b>{label} — {score.score}/100</b>{score_move}{ultra_line}{accel_line}\n\n"
        f"<b>{html.escape(s.token_name)} ({html.escape(s.token_symbol)})</b>\n"
        f"Chain: <b>{html.escape(s.chain.upper())}</b>\n"
        f"Age: <b>{age_min:.0f} min</b>\n"
        f"Market cap: <b>${s.market_cap:,.0f}</b>\n"
        f"Liquidity: <b>${s.liquidity_usd:,.0f}</b>\n"
        f"5m volume: <b>${s.volume_m5:,.0f}</b>\n"
        f"5m buys/sells: <b>{s.buys_m5}/{s.sells_m5} ({ratio:.2f}x)</b>\n"
        f"5m price: <b>{s.price_change_m5:+.1f}%</b>\n"
        f"Security: <b>{security}</b>\n\n"
        f"<b>Why it qualifies</b>\n{reasons}\n"
        + (f"\n<b>Warnings</b>\n{warnings}\n" if warnings else "")
        + f"\nContract:\n<code>{html.escape(s.token_address)}</code>"
        + link
        + "\n\n⚠️ <b>High risk:</b> meme coins can lose most or all of their value. This alert is a screening signal, not a guarantee."
    )

def paper_buy_message(s, score: int, usd: float) -> str:
    return (
        f"🧪🟢 <b>PAPER BUY</b>\n\n"
        f"<b>{html.escape(s.token_name)} ({html.escape(s.token_symbol)})</b>\n"
        f"Chain: <b>{html.escape(s.chain.upper())}</b>\n"
        f"Score: <b>{score}/100</b>\n"
        f"Simulated buy: <b>${usd:,.2f}</b>\n"
        f"Entry price: <b>${s.price_usd:.10f}</b>\n"
        f"Market cap: <b>${s.market_cap:,.0f}</b>\n"
        f"Liquidity: <b>${s.liquidity_usd:,.0f}</b>\n\n"
        f"ℹ️ <b>Simulation only.</b> No wallet transaction was made."
    )

def paper_sell_message(symbol: str, result: dict) -> str:
    pct = float(result.get("sell_percent") or 0)
    proceeds = float(result.get("sell_proceeds") or 0)
    pnl = float(result.get("sell_pnl") or 0)
    reason = html.escape(str(result.get("sell_reason") or "Rule triggered"))
    icon = "🟢" if pnl >= 0 else "🔴"
    remaining = float(result.get("remaining_percent") or 0)
    return (
        f"🧪💰 <b>PAPER SELL</b>\n\n"
        f"<b>{html.escape(symbol)}</b>\n"
        f"Sold: <b>{pct:.0f}% of original position</b>\n"
        f"Simulated proceeds: <b>${proceeds:,.2f}</b>\n"
        f"{icon} P/L on this sale: <b>${pnl:+,.2f}</b>\n"
        f"Remaining position: <b>{remaining:.0f}%</b>\n"
        f"Reason: <b>{reason}</b>\n\n"
        f"ℹ️ Simulation only. No wallet transaction was made."
    )
