"""Top-level launcher for Citadel Quant Bot.

This wrapper makes `py main.py` work from the project root and ensures the
`citadel_bot` package is importable even when the current working directory
is not the repository root.
"""

import asyncio
import os
import platform
import sys

if os.name == "nt":
    platform.system = lambda: "Windows"

# Ensure project root is importable when launching from the repository root.
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from citadel_bot.main import main

if __name__ == "__main__":
    asyncio.run(main())
