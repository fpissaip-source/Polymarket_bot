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
from utils.logger import setup_logger
from trading.bot import ArbitrageBot
from models.monte_carlo import MonteCarloSimulator
from config import MIN_EDGE

logger = setup_logger()


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
        "--dry-run", action="store_true", default=False,
        help="Log opportunities without placing real orders",
    )
    parser.add_argument(
        "--validate", action="store_true",
        help="Run Monte Carlo validation only and exit",
    )
    args = parser.parse_args()

    if args.validate:
        run_validation_only()
        return

    logger.info("Initializing Polymarket Arbitrage Bot...")
    logger.info(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")

    bot = ArbitrageBot(dry_run=args.dry_run)

    # Auto-discover active crypto markets on Polymarket
    logger.info("Auto-discovering active markets...")
    count = bot.auto_discover_markets()

    if count == 0:
        logger.warning(
            "No markets found via auto-discovery. "
            "Markets may have different naming - check Polymarket manually."
        )
    else:
        logger.info(f"Starting bot with {count} markets...")
        bot.run()


if __name__ == "__main__":
    main()
