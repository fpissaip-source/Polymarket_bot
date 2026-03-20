import os
from dotenv import load_dotenv

load_dotenv()

# Polymarket API
POLYMARKET_HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
GAMMA_API_HOST = os.getenv("GAMMA_API_HOST", "https://gamma-api.polymarket.com")
DATA_API_HOST = os.getenv("DATA_API_HOST", "https://data-api.polymarket.com")
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET = os.getenv("POLYMARKET_API_SECRET", "")
POLYMARKET_API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE", "")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))  # Polygon

# Crypto price feed
PRICE_FEED_URL = os.getenv("PRICE_FEED_URL", "https://api.binance.com/api/v3/ticker/price")
CRYPTO_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "BNBUSDT"]
# HYPE is not on Binance – excluded from price feed, still discovered on Polymarket
POLYMARKET_ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"]

# Bayesian model
BAYESIAN_PRIOR = 0.5          # Initial prior probability
BAYESIAN_ALPHA = 0.3          # Learning rate for updates
BAYESIAN_MIN_SAMPLES = 5      # Minimum samples before trading

# Edge model — Maker vs Taker costs
# Maker (passive limit order): 0% fee on Polymarket → very low cost
MAKER_FEE = 0.000             # Polymarket maker fee
MAKER_SLIPPAGE = 0.002        # Minimal slippage for resting limit orders
MAKER_EXEC_RISK = 0.003       # Risk of partial fill
TOTAL_COST_MAKER = MAKER_FEE + MAKER_SLIPPAGE + MAKER_EXEC_RISK   # ~0.5%
MIN_EDGE_MAKER = 0.005        # 0.5% edge sufficient for passive orders

# Taker (aggressive market order): fees + slippage apply
TAKER_FEE = 0.01              # Polymarket taker fee per side (1%)
TAKER_SLIPPAGE = 0.005        # Slippage on aggressive fills
TAKER_EXEC_RISK = 0.005       # Incomplete execution risk
TOTAL_COST_TAKER = TAKER_FEE + TAKER_SLIPPAGE + TAKER_EXEC_RISK   # ~2%
MIN_EDGE_TAKER = 0.020        # 2% edge required for aggressive orders

# Legacy aliases (used by EdgeModel default)
MIN_EDGE = MIN_EDGE_TAKER
TRADING_FEE = TAKER_FEE
SLIPPAGE_ESTIMATE = TAKER_SLIPPAGE
INCOMPLETE_EXEC_RISK = TAKER_EXEC_RISK
TOTAL_COST = TOTAL_COST_TAKER

# Event markets (politics, geopolitics, sports) — Gemini-powered
EVENT_SENTIMENT_MIN_BANKROLL = 100.0   # Only active above $100 portfolio
EVENT_MARKET_TAGS = ["politics", "geopolitics", "elections", "sports", "entertainment"]
EVENT_SENTIMENT_REFRESH = 1800         # Refresh every 30 minutes

# Spread model
SPREAD_ZSCORE_THRESHOLD = 2.0  # z-score threshold for arbitrage signal
SPREAD_LOOKBACK = 50           # Number of periods for mu/sigma calculation
RELATED_MARKETS = [
    ("BTC_5m", "BTC_15m"),
    ("ETH_5m", "ETH_15m"),
    ("SOL_5m", "SOL_15m"),
]

# Stoikov model
STOIKOV_GAMMA = 0.1           # Risk aversion coefficient
STOIKOV_SIGMA_DEFAULT = 0.02  # Default variance estimate

# Kelly model
KELLY_FRACTION = 0.25         # Fractional Kelly (lambda), conservative
KELLY_MAX_FRACTION = 0.5      # Maximum fraction of bankroll per trade
BANKROLL = float(os.getenv("BANKROLL", "5.27"))  # Total capital in USD

# Growth tiers: (min_balance, max_balance, kelly_lambda, min_edge)
# Bot starts aggressive at $5 and scales down as bankroll grows.
# Goal: $5.27 → $100 → $1,000 → $10,000
GROWTH_TIERS = [
    (0.0,    50.0,   0.50,  0.010),         # Tier 1:   $0–$50     aggressive, 1% edge
    (50.0,   100.0,  0.45,  0.025),         # Tier 2:  $50–$100    still aggressive
    (100.0,  500.0,  0.35,  0.025),         # Tier 3: $100–$500    moderate
    (500.0,  1000.0, 0.30,  0.030),         # Tier 4: $500–$1000   standard
    (1000.0, float("inf"), 0.25, 0.030),    # Tier 5: $1000+       conservative
]

# State file for bankroll persistence across restarts
BANKROLL_STATE_FILE = "bankroll_state.json"

# Monte Carlo
MC_SIMULATIONS = 600          # Number of simulation paths
MC_TRADES = 200               # Number of trades per simulation
MC_MAX_DD_LIMIT = 0.30        # Stop if max drawdown exceeds 30%

# Bot loop
POLL_INTERVAL_SECONDS = 1     # How often to scan markets (1s for 5-min markets)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
