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
CRYPTO_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]

# Bayesian model
BAYESIAN_PRIOR = 0.5          # Initial prior probability
BAYESIAN_ALPHA = 0.3          # Learning rate for updates
BAYESIAN_MIN_SAMPLES = 5      # Minimum samples before trading

# Edge model
MIN_EDGE = 0.03               # Minimum net edge to trade (3%)
TRADING_FEE = 0.01            # Fee per side (1%)
SLIPPAGE_ESTIMATE = 0.005     # Estimated slippage (0.5%)
INCOMPLETE_EXEC_RISK = 0.005  # Risk of incomplete execution (0.5%)
TOTAL_COST = TRADING_FEE + SLIPPAGE_ESTIMATE + INCOMPLETE_EXEC_RISK  # c

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
BANKROLL = float(os.getenv("BANKROLL", "1000.0"))  # Total capital in USD

# Monte Carlo
MC_SIMULATIONS = 600          # Number of simulation paths
MC_TRADES = 200               # Number of trades per simulation
MC_MAX_DD_LIMIT = 0.30        # Stop if max drawdown exceeds 30%

# Bot loop
POLL_INTERVAL_SECONDS = 5     # How often to scan markets
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
