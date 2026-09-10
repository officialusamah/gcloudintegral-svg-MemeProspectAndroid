import asyncio
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
