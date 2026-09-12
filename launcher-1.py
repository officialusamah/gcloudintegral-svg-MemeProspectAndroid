import asyncio
import builtins
import html
import importlib
import re
import time

# Railway log-rate protection.
# This affects console output only; scanning, Telegram alerts, safety checks,
# Jupiter checks, and manual wallet approval logic continue unchanged.
_original_print = builtins.print

_NOISY_PREFIXES = (
    "ULTRA WAIT",
    "ULTRA REJECT",
    "ULTRA SECURITY provider unavailable",
    "ULTRA REST fallback ready",
)

def _quiet_print(*args, **kwargs):
    try:
        text = " ".join(str(x) for x in args)
        if text.startswith(_NOISY_PREFIXES):
            return
    except Exception:
        pass
    _original_print(*args, **kwargs)

builtins.print = _quiet_print

from .launch_age import install as install_launch_age
install_launch_age()

from .ultra_early import install
install()

from .launch_first import install as install_launch_first
install_launch_first()

# Fix the 0-30s WATCH data path before the PumpPortal listener starts.
from .watch_curve_fix import install as install_watch_curve_fix
install_watch_curve_fix()

# X trend intelligence wraps the already-working Ultra/Launch-First handler.
from .narrative_trend import install as install_narrative_trend
install_narrative_trend()

# Google Trends is a second independent narrative layer.
from .google_trend import install as install_google_trend
install_google_trend()

# Robinhood Chain Instant Launch scanner is completely separate from Solana.
from .robinhood_launch import install as install_robinhood_launch
install_robinhood_launch()

from .pumpportal_launch import install as install_pumpportal_launch
install_pumpportal_launch()


# Robinhood Chain / Uniswap public wallet support.
# Keeps the existing Solana wallet untouched and adds a separate public EVM 0x address.
from .accounts import Accounts
from .telegram import Telegram

_EVM_WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
_robinhood_wallet_accounts = None

if not getattr(Accounts, "_robinhood_wallet_support_installed", False):
    _original_accounts_init = Accounts.__init__

    def _accounts_init_with_robinhood_wallet(self, *args, **kwargs):
        global _robinhood_wallet_accounts
        _original_accounts_init(self, *args, **kwargs)

        with self.connect() as c:
            columns = {
                str(row[1])
                for row in c.execute("PRAGMA table_info(bot_users)").fetchall()
            }
            if "robinhood_wallet_address" not in columns:
                c.execute(
                    "ALTER TABLE bot_users "
                    "ADD COLUMN robinhood_wallet_address TEXT NOT NULL DEFAULT ''"
                )

        _robinhood_wallet_accounts = self

    def _set_robinhood_wallet(self, chat_id: str, address: str):
        with self.connect() as c:
            c.execute(
                "UPDATE bot_users "
                "SET robinhood_wallet_address=?,last_seen_ts=? "
                "WHERE chat_id=?",
                (
                    str(address or "").strip(),
                    int(time.time()),
                    str(chat_id),
                ),
            )

    def _get_robinhood_wallet(self, chat_id: str) -> str:
        user = self.get(chat_id) or {}
        return str(user.get("robinhood_wallet_address") or "").strip()

    Accounts.__init__ = _accounts_init_with_robinhood_wallet
    Accounts.set_robinhood_wallet = _set_robinhood_wallet
    Accounts.get_robinhood_wallet = _get_robinhood_wallet
    Accounts._robinhood_wallet_support_installed = True


def _mask_evm_wallet(address: str) -> str:
    address = str(address or "").strip()
    if len(address) <= 16:
        return address
    return f"{address[:8]}…{address[-6:]}"


if not getattr(Telegram, "_robinhood_wallet_support_installed", False):
    _original_telegram_updates = Telegram.updates

    async def _telegram_updates_with_robinhood_wallet(self):
        updates = await _original_telegram_updates(self)
        accounts = _robinhood_wallet_accounts

        if accounts is None:
            return updates

        forwarded = []

        for update in updates:
            msg = update.get("message") or {}
            chat = msg.get("chat") or {}
            sender = msg.get("from") or {}
            chat_id = str(chat.get("id") or "")
            text = str(msg.get("text") or "").strip()

            if not chat_id or not text:
                forwarded.append(update)
                continue

            parts = text.split()
            cmd = parts[0].split("@")[0].lower()

            if cmd not in {"/setrobinhoodwallet", "/robinhoodwallet"}:
                forwarded.append(update)
                continue

            # These commands are handled here, so advance the Telegram offset.
            self.offset = max(
                self.offset,
                int(update.get("update_id", 0)) + 1,
            )

            accounts.register(
                chat_id,
                str(sender.get("username") or ""),
                str(sender.get("first_name") or ""),
            )

            if cmd == "/setrobinhoodwallet":
                if len(parts) != 2:
                    await self.send(
                        "Use:\n"
                        "<code>/setrobinhoodwallet 0xYOUR_PUBLIC_UNISWAP_ADDRESS</code>\n\n"
                        "Use only your public 0x address. "
                        "Never send a seed phrase or private key.",
                        chat_id,
                    )
                    continue

                address = parts[1].strip()

                if not _EVM_WALLET_RE.fullmatch(address):
                    await self.send(
                        "That does not look like a valid EVM wallet address.\n"
                        "It must start with <code>0x</code> followed by "
                        "40 hexadecimal characters.",
                        chat_id,
                    )
                    continue

                accounts.set_robinhood_wallet(chat_id, address)

                await self.send(
                    "✅ <b>Robinhood Chain wallet connected.</b>\n"
                    f"Uniswap/EVM address: "
                    f"<code>{html.escape(_mask_evm_wallet(address))}</code>\n\n"
                    "Read-only tracking only. The bot cannot sign or move funds.\n"
                    "Every BUY still requires your wallet approval.",
                    chat_id,
                )
                continue

            address = accounts.get_robinhood_wallet(chat_id)

            if not address:
                await self.send(
                    "No Robinhood Chain wallet connected yet.\n"
                    "Use "
                    "<code>/setrobinhoodwallet 0xYOUR_PUBLIC_UNISWAP_ADDRESS</code>",
                    chat_id,
                )
            else:
                await self.send(
                    "🟢 <b>Your Robinhood Chain Wallet</b>\n"
                    f"Uniswap/EVM address: "
                    f"<code>{html.escape(_mask_evm_wallet(address))}</code>\n"
                    "Network: <b>Robinhood Chain (4663)</b>\n"
                    "Auto execution: <b>DISABLED 🔒</b>\n"
                    "Manual wallet approval: <b>ON ✅</b>",
                    chat_id,
                )

        return forwarded

    Telegram.updates = _telegram_updates_with_robinhood_wallet
    Telegram._robinhood_wallet_support_installed = True

from .main import main


# Add the Robinhood wallet commands to /help without changing main.py.
try:
    _main_module = importlib.import_module(f"{__package__}.main")
    _help_marker = "/setwallet ADDRESS — connect your PUBLIC Solana wallet\n"
    if "/setrobinhoodwallet" not in _main_module.HELP:
        _main_module.HELP = _main_module.HELP.replace(
            _help_marker,
            _help_marker
            + "/setrobinhoodwallet 0xADDRESS — connect your PUBLIC Uniswap/Robinhood wallet\n"
            + "/robinhoodwallet — show your Robinhood Chain wallet\n",
        )
except Exception as exc:
    print("ROBINHOOD WALLET help patch warning —", repr(exc))

print(
    "ROBINHOOD WALLET SUPPORT ON — "
    "/setrobinhoodwallet + /robinhoodwallet — "
    "public 0x address only — manual wallet approval only."
)

if __name__ == "__main__":
    asyncio.run(main())
