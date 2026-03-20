"""
Polymarket Bot — Root Launcher
Dispatches to bot/main.py so it works from the repo root.

Usage (from repo root):
  python3 main.py --dry-run
  python3 main.py --live
  python3 main.py --validate
"""

import sys
import os

bot_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot")
sys.path.insert(0, bot_dir)
os.chdir(bot_dir)

if __name__ == "__main__":
    from main import main
    main()
