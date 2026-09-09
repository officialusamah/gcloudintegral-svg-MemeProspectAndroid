import asyncio
from .ultra_early import install

install()

from .main import main

if __name__ == "__main__":
    asyncio.run(main())
