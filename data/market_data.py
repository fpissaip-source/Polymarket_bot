import requests
class GammaClient:
    def __init__(self): self.host = "https://gamma-api.polymarket.com"
    def get_markets(self, active=True, limit=100):
        return requests.get(f"{self.host}/markets", params={"active": "true", "limit": limit}).json()
class ClobClient:
    def __init__(self): self.host = "https://clob.polymarket.com"
    def get_order_book(self, token_id):
        try:
            r = requests.get(f"{self.host}/book", params={"token_id": token_id}, timeout=5).json()
            b, a = r.get("bids", []), r.get("asks", [])
            if b and a:
                return {"mid_price": (float(b[0]['price']) + float(a[0]['price'])) / 2, "depth": sum([float(x['size']) for x in b+a])}
        except: pass
        return {"mid_price": None, "depth": 0}
