import time, logging
from dataclasses import dataclass
from data.market_data import ClobClient
logger = logging.getLogger("bot")
@dataclass
class MarketState:
    market_id: str
    token_id_yes: str
    token_id_no: str
    asset: str
    timeframe: str
class ArbitrageBot:
    def __init__(self, markets_dict, dry_run=True):
        self.markets = markets_dict
        self.clob = ClobClient()
    def _tick(self):
        for mid, state in self.markets.items():
            book = self.clob.get_order_book(state.token_id_yes)
            p, d = book.get("mid_price"), book.get("depth", 0)
            if p and d > 0:
                print(f"[LIVE] {state.asset}: ${p:.4f} | Vol: ${d:.0f}")
    def run(self):
        while True:
            self._tick()
            time.sleep(1)
