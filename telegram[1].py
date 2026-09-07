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

    async def send(self, text: str, chat_id: str | None = None):
        target = chat_id or self.chat_id
        if not target:
            return False
        r = await self.client.post(
            f"{self.base}/sendMessage",
            json={
                "chat_id": target,
                "text": text[:4096],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
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
        # First /start pairs the private bot to one chat. Existing pairing is not silently replaced.
        if not self.chat_id:
            self.chat_id = str(chat_id)
            self.store.set_setting("telegram_chat_id", self.chat_id)
            return True
        return str(chat_id) == str(self.chat_id)

def alert_message(s, score, age_min: float) -> str:
    sells = max(s.sells_m5, 1)
    ratio = s.buys_m5 / sells
    reasons = "\n".join(f"✅ {html.escape(x)}" for x in score.reasons[:6]) or "✅ Strong composite score"
    warnings = "\n".join(f"⚠️ {html.escape(x)}" for x in score.warnings[:5])
    security = "checked" if score.security_checked else "not available"
    link = f'\n<a href="{html.escape(s.url)}">Open chart</a>' if s.url else ""

    if score.score >= 85:
        label = "🟢 STRONG PROSPECT"
    elif score.score >= 60:
        label = "🟡 EARLY WATCH"
    else:
        label = "🔴 LOW SCORE"

    ultra = any("Ultra-early breakout profile" in x for x in score.reasons)
    ultra_line = "\n⚡ <b>ULTRA-EARLY BREAKOUT PROFILE</b>" if ultra else ""

    return (
        f"<b>{label} — {score.score}/100</b>{ultra_line}\n\n"
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
