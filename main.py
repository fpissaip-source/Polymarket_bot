import logging, sys
from data.market_data import GammaClient
from trading.bot import ArbitrageBot, MarketState
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
def main():
    gamma = GammaClient()
    raw = gamma.get_markets(active=True, limit=20)
    markets_dict = {}
    for m in raw:
        mid, tokens = m.get("conditionId"), m.get("clobTokenIds", [])
        if mid and len(tokens) >= 2 and len(markets_dict) < 5:
            markets_dict[mid] = MarketState(mid, tokens[0], tokens[1], m.get("question","").split(" ")[0].upper(), "5m")
    if markets_dict:
        print(f"✅ {len(markets_dict)} Märkte geladen. Starte Scan...")
        ArbitrageBot(markets_dict=markets_dict).run()
if __name__ == "__main__": main()
