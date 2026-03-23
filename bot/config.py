import os
from pathlib import Path
from dotenv import load_dotenv

# Search for .env in bot/ dir first, then project root
_bot_dir = Path(__file__).parent
load_dotenv(_bot_dir / ".env")               # bot/.env
load_dotenv(_bot_dir.parent / ".env")        # project root .env

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
MIN_EDGE_MAKER = 0.04         # 4% edge required — events need clearer Gemini signal

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

# Event markets — ALL categories EXCEPT sports (Gemini 3 Flash powered)
EVENT_SENTIMENT_MIN_BANKROLL = 1.0
EVENT_SENTIMENT_REFRESH = 300          # Re-analyze every 5 minutes
EVENT_MARKET_LIMIT = 100              # Broader discovery — more categories
EVENT_MARKET_MIN_VOLUME = 500.0       # Min total volume ($) for market quality

# Categories the bot WILL trade (Gamma API tag slugs)
EVENT_MARKET_TAGS = [
    "politics",
    "geopolitics",
    "elections",
    "economics",
    "finance",
    "science",
    "technology",
    "climate",
    "weather",
    "environment",
    "health",
    "law",
    "ai",
    "crypto",       # non-price crypto events (regulations, ETF approvals, etc.)
    "business",
    "culture",
    "entertainment",
    "media",
    "international",
]

# Categories and keywords the bot will NEVER trade (sports exclusion)
SPORTS_EXCLUDE_TAGS = [
    "sports", "football", "soccer", "basketball", "baseball",
    "hockey", "tennis", "golf", "racing", "boxing", "mma", "ufc",
    "olympics", "esports",
]
SPORTS_EXCLUDE_KEYWORDS = [
    "nfl", "nba", "mlb", "nhl", "nascar", "f1", "formula 1", "formula one",
    "premier league", "bundesliga", "la liga", "serie a", "ligue 1", "champions league",
    "world cup", "superbowl", "super bowl", "stanley cup", "world series",
    "wimbledon", "us open", "french open", "australian open",
    "ufc", "boxing", "mma", "wrestl",
    "match", "game 1", "game 2", "game 3", "game 4", "game 5", "game 6", "game 7",
    "score", "winner of the", "championship",
    "quarterback", "touchdown", "home run", "penalty",
]

# Gemini API
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Only trade event markets when Gemini's confidence is at or above this threshold
GEMINI_MIN_CONFIDENCE = float(os.getenv("GEMINI_MIN_CONFIDENCE", "0.60"))

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

# Hard price boundaries — events can sit at extreme prices legitimately
PRICE_FLOOR   = 0.05    # Skip markets where both sides < 5¢ (dead/broken market)
PRICE_CEILING = 0.95    # Skip markets where outcome is virtually certain

# Time filter — no new positions in last N seconds before expiry
MIN_SECONDS_BEFORE_EXPIRY = 60  # 60s buffer avoids last-minute coinflip volatility

# Stoikov model
STOIKOV_GAMMA = 0.1           # Risk aversion coefficient
STOIKOV_SIGMA_DEFAULT = 0.02  # Default variance estimate

# Kelly model
KELLY_FRACTION = 0.25         # Fractional Kelly (lambda) — used internally, capped by BET_SIZE_PCT
KELLY_MAX_FRACTION = 0.02     # Hard cap: max 2% of bankroll per single trade
BANKROLL = float(os.getenv("BANKROLL", "25.00"))  # Total capital in USD (live trading)
DRY_RUN_BANKROLL = float(os.getenv("DRY_RUN_BANKROLL", os.getenv("BANKROLL", "25.00")))  # Virtual capital for dry-run simulation

# Fixed bet sizing (overrides Kelly when smaller)
BET_SIZE_PCT = float(os.getenv("BET_SIZE_PCT", "0.20"))
MIN_BET_SIZE = float(os.getenv("MIN_BET_SIZE", "1.00"))
MAX_OPEN_TRADES = int(os.getenv("MAX_OPEN_TRADES", "10"))
MAX_TRADES_5MIN = int(os.getenv("MAX_TRADES_5MIN", "0"))   # disabled
MAX_TRADES_15MIN = int(os.getenv("MAX_TRADES_15MIN", "0")) # disabled
MAX_TRADES_EVENT = int(os.getenv("MAX_TRADES_EVENT", "10"))
MAX_TOTAL_EXPOSURE_PCT = float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "0.85"))
MAX_POSITION_HOLD_MINUTES = float(os.getenv("MAX_POSITION_HOLD_MINUTES", "10080.0"))  # 7 days — events resolve slowly
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

# Take-Profit for event markets
# SL is DISABLED for event markets — prediction markets have binary outcomes;
# early stop-loss causes unnecessary losses and fees before resolution.
# TP is only triggered when enough time remains to recycle capital.
TP_RATIO = 0.30               # Take profit at +30% (when >2h left or market near resolved)
SL_RATIO = 0.35               # Stop-loss at -35% loss (GROWTH phase only)
SL_RATIO_CATASTROPHIC = 0.65  # Emergency SL at -65% loss regardless of phase

# Minimum time remaining before a TP exit is allowed (seconds).
# With <2h left, just hold to resolution — exiting costs fees without benefit.
TP_MIN_TIME_REMAINING = 7200  # 2 hours

# A market is considered "near resolved" when price is this extreme — TP allowed regardless of time.
TP_NEAR_RESOLVED_THRESHOLD = 0.90   # price ≥ 0.90 (YES) or ≤ 0.10 (NO) = basically done

# Legacy aliases kept for import compatibility (values unused)
TP_RATIO_5MIN = 0.10
SL_RATIO_5MIN = 0.05
LOW_PRICE_THRESHOLD = 0.30
TP_RATIO_LOW = TP_RATIO
SL_RATIO_LOW = 0.0

TP_SL_CHECK_INTERVAL = 1      # Check TP every 1 second

# Monte Carlo
MC_SIMULATIONS = 600          # Number of simulation paths
MC_TRADES = 200               # Number of trades per simulation
MC_MAX_DD_LIMIT = 0.30        # Stop if max drawdown exceeds 30%

# Copy-trading from polybot-arena.com top bots
# Add proxy wallet addresses (comma-separated 0x...) of top traders you want to follow.
# Find addresses: open polymarket.com/@<username>, copy the 0x wallet shown on the profile.
# Known top bots (Mar 2026): BoneReader (+$457k), vidarx (+$274k), vague-sourdough (+$165k)
# Example: COPY_TRADE_WALLETS=0xabc...123,0xdef...456
COPY_TRADE_WALLETS = os.getenv("COPY_TRADE_WALLETS", "")  # read by wallet_tracker.py

# Bot loop
POLL_INTERVAL_SECONDS = 30    # Event markets move slowly — scan every 30s
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
