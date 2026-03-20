import { Router, type IRouter } from "express";

const router: IRouter = Router();

const CLOB_HOST = "https://clob.polymarket.com";
const GAMMA_HOST = "https://gamma-api.polymarket.com";
const COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price";

const COINGECKO_IDS = "bitcoin,ethereum,solana,ripple,dogecoin,binancecoin";
const COINGECKO_SYMBOL_MAP: Record<string, string> = {
  bitcoin: "BTCUSDT",
  ethereum: "ETHUSDT",
  solana: "SOLUSDT",
  ripple: "XRPUSDT",
  dogecoin: "DOGEUSDT",
  binancecoin: "BNBUSDT",
};
const POLYMARKET_ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"];

async function fetchJson(url: string, params?: Record<string, string>): Promise<unknown> {
  const urlObj = new URL(url);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      urlObj.searchParams.set(k, v);
    }
  }
  const resp = await fetch(urlObj.toString(), { signal: AbortSignal.timeout(8000) });
  if (!resp.ok) throw new Error(`HTTP ${resp.status} from ${url}`);
  return resp.json();
}

router.get("/markets", async (req, res) => {
  try {
    const markets: unknown[] = [];

    const eventsData = await fetchJson(`${GAMMA_HOST}/events`, {
      active: "true",
      closed: "false",
      limit: "30",
      order: "volume",
    }) as unknown;
    const events = Array.isArray(eventsData) ? eventsData : [];

    const allMarkets: Record<string, unknown>[] = [];
    for (const event of events as Record<string, unknown>[]) {
      const subMarkets = (event.markets ?? []) as Record<string, unknown>[];
      for (const m of subMarkets) {
        if (m.active !== false && !m.closed) allMarkets.push(m);
      }
    }

    for (const m of allMarkets.slice(0, 100)) {
      const question = String(m.question ?? "");

      let yesPrice: number | null = null;
      let noPrice: number | null = null;
      const rawPrices = m.outcomePrices;
      let pricesArr: string[] = [];
      if (typeof rawPrices === "string") {
        try { pricesArr = JSON.parse(rawPrices); } catch { /* ignore */ }
      } else if (Array.isArray(rawPrices)) {
        pricesArr = rawPrices as string[];
      }
      if (pricesArr.length >= 2) {
        yesPrice = parseFloat(pricesArr[0]);
        noPrice = parseFloat(pricesArr[1]);
      }

      let asset = "OTHER";
      for (const a of POLYMARKET_ASSETS) {
        if (question.toUpperCase().includes(a)) { asset = a; break; }
      }

      const clobIds = typeof m.clobTokenIds === "string"
        ? JSON.parse(m.clobTokenIds)
        : (m.clobTokenIds ?? []);

      markets.push({
        id: m.conditionId ?? m.id ?? "",
        question,
        asset,
        yesPrice: isNaN(yesPrice as number) ? null : yesPrice,
        noPrice: isNaN(noPrice as number) ? null : noPrice,
        volume: m.volume ? parseFloat(String(m.volume)) : null,
        endDate: m.endDate ?? m.closeTime ?? null,
        active: true,
        hasClob: Array.isArray(clobIds) && clobIds.length >= 2,
      });
    }

    res.json(markets);
  } catch (err) {
    req.log.error({ err }, "Failed to fetch markets");
    res.json([]);
  }
});

router.get("/markets/prices", async (req, res) => {
  try {
    const data = await fetchJson(COINGECKO_URL, {
      ids: COINGECKO_IDS,
      vs_currencies: "usd",
    }) as Record<string, { usd: number }>;

    const prices: Record<string, number> = {};
    for (const [geckoId, val] of Object.entries(data)) {
      const symbol = COINGECKO_SYMBOL_MAP[geckoId];
      if (symbol) prices[symbol] = val.usd;
    }

    res.json({ prices, updatedAt: new Date().toISOString() });
  } catch (err) {
    req.log.error({ err }, "Failed to fetch prices");
    res.json({ prices: {}, updatedAt: new Date().toISOString() });
  }
});

router.get("/orderbook/:tokenId", async (req, res) => {
  const { tokenId } = req.params;
  try {
    const data = await fetchJson(`${CLOB_HOST}/book`, { token_id: tokenId }) as {
      bids: { price: string; size: string }[];
      asks: { price: string; size: string }[];
    };

    const bids = (data.bids ?? []).map((b) => ({ price: parseFloat(b.price), size: parseFloat(b.size) }));
    const asks = (data.asks ?? []).map((a) => ({ price: parseFloat(a.price), size: parseFloat(a.size) }));

    const bestBid = bids.length > 0 ? bids[0].price : null;
    const bestAsk = asks.length > 0 ? asks[0].price : null;
    const midPrice = bestBid !== null && bestAsk !== null ? (bestBid + bestAsk) / 2 : null;
    const spread = bestBid !== null && bestAsk !== null ? bestAsk - bestBid : null;
    const depth = [...bids, ...asks].reduce((s, l) => s + l.size, 0);

    res.json({ tokenId, bestBid, bestAsk, midPrice, spread, depth, bids: bids.slice(0, 10), asks: asks.slice(0, 10) });
  } catch (err) {
    req.log.error({ err }, "Failed to fetch order book");
    res.json({ tokenId, bestBid: null, bestAsk: null, midPrice: null, spread: null, depth: 0, bids: [], asks: [] });
  }
});

export default router;
