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

from .pumpportal_launch import install as install_pumpportal_launch
install_pumpportal_launch()

from .main import main

if __name__ == "__main__":
    asyncio.run(main())
