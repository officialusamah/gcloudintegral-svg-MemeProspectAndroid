import asyncio
import builtins

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

from .main import main

if __name__ == "__main__":
    asyncio.run(main())
