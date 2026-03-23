"""
Polymarket Arbitrage Bot
========================
Entry point.

Architecture (6 models from the Twitter thread):
  1. Bayesian   - estimates true probability q
  2. Edge       - filters: EV_net = q - p - c > threshold
  3. Spread     - detects cross-market dislocations (z-score)
  4. Stoikov    - quality execution: r = s - q*gamma*sigma^2*(T-t)
  5. Kelly      - position sizing: f* = (b*p - q) / b
  6. Monte Carlo- strategy validation: W(t+1) = W(t) * (1 + r(t))

Usage:
  python main.py              # live mode (auto-discovers markets)
  python main.py --dry-run   # simulate only, no real orders
  python main.py --validate  # run Monte Carlo validation and exit

Environment variables (.env):
  POLYMARKET_PRIVATE_KEY
  POLYMARKET_PROXY_ADDRESS
  POLYMARKET_API_KEY / SECRET / PASSPHRASE
  BANKROLL
  LOG_LEVEL
"""

import argparse
import logging
import threading
import time
from utils.logger import setup_logger
from trading.bot import ArbitrageBot
from models.monte_carlo import MonteCarloSimulator
from config import MIN_EDGE

logger = setup_logger()

_dashboard_server = None
_dashboard_lock = threading.Lock()


def start_dashboard():
    """Start the dashboard HTTP server in a background thread with auto-restart."""
    global _dashboard_server
    while True:
        try:
            from dashboard.server import DashboardHandler, PORT
            from http.server import HTTPServer
            server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
            with _dashboard_lock:
                _dashboard_server = server
            logger.info(f"Dashboard gestartet auf http://0.0.0.0:{PORT}")
            server.serve_forever()
        except OSError as e:
            logger.warning(f"Dashboard Port belegt, retry in 10s: {e}")
            time.sleep(10)
        except Exception as e:
            logger.warning(f"Dashboard Fehler, retry in 5s: {e}")
            time.sleep(5)


def run_validation_only():
    """Run Monte Carlo validation and print results."""
    mc = MonteCarloSimulator(n_simulations=1000, n_trades=200)
    logger.info("=== Monte Carlo Validation ===")
    for ev in [0.02, 0.03, 0.05]:
        result = mc.run(
            base_ev=ev,
            base_win_rate=0.55 + ev,
            avg_position_fraction=0.25,
        )
        logger.info(f"EV={ev:.0%}: {result.description}")
    logger.info("=== Validation Complete ===")


def main():
    parser = argparse.ArgumentParser(description="Polymarket Arbitrage Bot")
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Log opportunities without placing real orders (default: True)",
    )
    parser.add_argument(
        "--live", action="store_true", default=False,
        help="Enable live trading (real orders)",
    )
    parser.add_argument(
        "--validate", action="store_true",
        help="Run Monte Carlo validation only and exit",
    )
    args = parser.parse_args()

    if args.validate:
        run_validation_only()
        return

    # Start dashboard in background thread
    t = threading.Thread(target=start_dashboard, daemon=True)
    t.start()

    dry_run = not args.live  # dry-run is default, --live enables real orders
    logger.info("Initializing Polymarket Arbitrage Bot...")
    logger.info(f"Mode: {'DRY RUN' if dry_run else '*** LIVE TRADING ***'}")

    BOT_RESTART_DELAY = 30  # seconds between crash restarts

    while True:
        try:
            bot = ArbitrageBot(dry_run=dry_run)

            # Auto-discover active crypto markets on Polymarket
            logger.info("Auto-discovering active markets...")
            count = bot.auto_discover_markets()

            if count == 0:
                logger.warning(
                    "No markets found via auto-discovery. "
                    "Bot will start anyway and retry market discovery automatically."
                )
            else:
                logger.info(f"Starting bot with {count} markets...")
            bot.run()
            # bot.run() only returns on clean shutdown (KeyboardInterrupt)
            logger.info("Bot stopped cleanly.")
            break
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            break
        except Exception as e:
            logger.error(
                f"Bot crashed: {e} — restarting in {BOT_RESTART_DELAY}s...",
                exc_info=True,
            )
            time.sleep(BOT_RESTART_DELAY)


if __name__ == "__main__":
    main()
