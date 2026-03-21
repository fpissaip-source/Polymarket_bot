import os
from dotenv import load_dotenv

load_dotenv()

# Polymarket API
POLYMARKET_HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
GAMMA_API_HOST = os.getenv("GAMMA_API_HOST", "https://gamma-api.polymarket.com")
DATA_API_HOST = os.getenv("DATA_API_HOST", "https://data-api.polymarket.com")
POLYMARKET_PRIVATE_KEY = "".join(os.getenv("POLYMARKET_PRIVATE_KEY", "").split())
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET = os.getenv("POLYMARKET_API_SECRET", "")
POLYMARKET_API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE", "")
# Proxy wallet: holds the actual USDC.e balance; use sig_type=1 when set
POLYMARKET_PROXY_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS", "0x024558B703f59Bff6BBA21919697163E96E2353B")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))  # Polygon

# Crypto price feed
PRICE_FEED_URL = os.getenv("PRICE_FEED_URL", "https://api.binance.com/api/v3/ticker/price")
CRYPTO_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
# Only BTC/ETH/SOL have active 15-min Up/Down markets on Polymarket
POLYMARKET_ASSETS = ["BTC", "ETH", "SOL"]

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
MIN_EDGE_TAKER = 0.50         # 50% edge required — effectively disables taker/FOK orders

# Legacy aliases (used by EdgeModel default)
MIN_EDGE = MIN_EDGE_MAKER     # Maker orders only — 0.5% threshold
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

# Price sweet-spot filter — only trade where risk/reward is good for scalping
# Target: one side (YES or NO) sits at 75–85c (clear favourite, still room to run)
# p_yes in [0.75, 0.85]  →  buy YES
# p_yes in [0.15, 0.25]  →  buy NO  (because p_no = 1 - p_yes is in [0.75, 0.85])
SWEET_SPOT_HIGH_MIN = 0.75    # YES-side sweet zone: lower bound
SWEET_SPOT_HIGH_MAX = 0.85    # YES-side sweet zone: upper bound
SWEET_SPOT_LOW_MIN  = 1.0 - SWEET_SPOT_HIGH_MAX   # = 0.15  (NO-side mirror)
SWEET_SPOT_LOW_MAX  = 1.0 - SWEET_SPOT_HIGH_MIN   # = 0.25  (NO-side mirror)

# Kept for hard boundary checks (price sanity, not sweet-spot logic)
PRICE_FLOOR   = SWEET_SPOT_LOW_MIN    # = 0.15
PRICE_CEILING = SWEET_SPOT_HIGH_MAX   # = 0.85

# Time filter — no new positions in last N seconds before expiry
MIN_SECONDS_BEFORE_EXPIRY = 60  # 60s buffer avoids last-minute coinflip volatility

# Stoikov model
STOIKOV_GAMMA = 0.1           # Risk aversion coefficient
STOIKOV_SIGMA_DEFAULT = 0.02  # Default variance estimate

# Kelly model
KELLY_FRACTION = 0.25         # Fractional Kelly (lambda) — used internally, capped by BET_SIZE_PCT
KELLY_MAX_FRACTION = 0.02     # Hard cap: max 2% of bankroll per single trade
BANKROLL = float(os.getenv("BANKROLL", "25.00"))  # Total capital in USD (live trading)
DRY_RUN_BANKROLL = float(os.getenv("DRY_RUN_BANKROLL", "25.00"))  # Virtual capital for dry-run simulation

# Fixed bet sizing (overrides Kelly when smaller)
BET_SIZE_PCT = float(os.getenv("BET_SIZE_PCT", "0.20"))
MIN_BET_SIZE = float(os.getenv("MIN_BET_SIZE", "1.00"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "4"))
MAX_TOTAL_EXPOSURE_PCT = float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "0.85"))
MAX_POSITION_HOLD_MINUTES = float(os.getenv("MAX_POSITION_HOLD_MINUTES", "20.0"))  # Force-sell after this
MIN_BANKROLL_FLOOR = float(os.getenv("MIN_BANKROLL_FLOOR", "3.0"))

# Growth tiers: (min_balance, max_balance, kelly_lambda, min_edge)
GROWTH_TIERS = [
    (0.0,    50.0,   0.25,  0.020),         # Tier 1:   $0–$50     conservative, 2% edge
    (50.0,   100.0,  0.25,  0.025),         # Tier 2:  $50–$100
    (100.0,  500.0,  0.25,  0.025),         # Tier 3: $100–$500
    (500.0,  1000.0, 0.25,  0.030),         # Tier 4: $500–$1000
    (1000.0, float("inf"), 0.25, 0.030),    # Tier 5: $1000+
]

# State file for bankroll persistence across restarts
BANKROLL_STATE_FILE = "bankroll_state.json"

# Take-Profit / Stop-Loss
TP_RATIO = 0.10               # Take profit at +10% return on trade
SL_RATIO = 0.20               # Stop loss at -20% loss on trade

# Adaptive TP/SL for low-price positions (exec_price < LOW_PRICE_THRESHOLD)
# At prices like 0.19, a move from 0.19→0.12 is already -37% in one tick.
# Tighter thresholds catch losses faster before they snowball.
LOW_PRICE_THRESHOLD = 0.30    # Below this exec_price → use low-price ratios
TP_RATIO_LOW = 0.15           # TP at +15% for low-price positions
SL_RATIO_LOW = 0.10           # SL at -10% for low-price positions

TP_SL_CHECK_INTERVAL = 1      # Check TP/SL every 1 second (fast reaction)

# Monte Carlo
MC_SIMULATIONS = 600          # Number of simulation paths
MC_TRADES = 200               # Number of trades per simulation
MC_MAX_DD_LIMIT = 0.30        # Stop if max drawdown exceeds 30%

# Bot loop
POLL_INTERVAL_SECONDS = 2     # How often to scan markets (2s for 15-min markets)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
