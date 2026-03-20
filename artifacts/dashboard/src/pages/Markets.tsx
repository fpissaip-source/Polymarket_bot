import { useQuery } from "@tanstack/react-query";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

type Market = {
  id: string;
  question: string;
  asset: string;
  yesPrice: number | null;
  noPrice: number | null;
  volume: number | null;
  endDate: string | null;
  active: boolean;
};

type PriceMap = { prices: Record<string, number>; updatedAt: string };

const SYMBOL_MAP: Record<string, string> = {
  BTC: "BTCUSDT", ETH: "ETHUSDT", SOL: "SOLUSDT",
  XRP: "XRPUSDT", DOGE: "DOGEUSDT", BNB: "BNBUSDT",
};

export function Markets() {
  const { data: markets, isLoading } = useQuery<Market[]>({
    queryKey: ["markets"],
    queryFn: async () => {
      const r = await fetch(`${BASE}/api/markets`);
      return r.json();
    },
    refetchInterval: 15000,
  });

  const { data: priceData } = useQuery<PriceMap>({
    queryKey: ["crypto-prices"],
    queryFn: async () => {
      const r = await fetch(`${BASE}/api/markets/prices`);
      return r.json();
    },
    refetchInterval: 5000,
  });

  return (
    <div className="p-6 space-y-6">
      <div>
        <h2 className="text-xl font-bold">Aktive Märkte</h2>
        <p className="text-sm text-muted-foreground">Polymarket Vorhersagemärkte — aktualisiert alle 15s</p>
      </div>

      {isLoading && (
        <div className="text-center py-12 text-muted-foreground">
          <div className="text-2xl mb-2">⏳</div>
          <p>Märkte werden geladen...</p>
        </div>
      )}

      {markets && markets.length === 0 && !isLoading && (
        <div className="text-center py-12 text-muted-foreground border border-dashed border-border rounded-lg">
          <div className="text-2xl mb-2">📭</div>
          <p>Keine aktiven Märkte gefunden</p>
        </div>
      )}

      {markets && markets.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-muted-foreground text-xs uppercase tracking-wider">
                <th className="text-left py-3 px-3">Asset</th>
                <th className="text-left py-3 px-3">Frage</th>
                <th className="text-right py-3 px-3">YES Preis</th>
                <th className="text-right py-3 px-3">NO Preis</th>
                <th className="text-right py-3 px-3">Spread</th>
                <th className="text-right py-3 px-3">Spot Preis</th>
                <th className="text-right py-3 px-3">Volumen</th>
              </tr>
            </thead>
            <tbody>
              {markets.map((m) => {
                const spread =
                  m.yesPrice !== null && m.noPrice !== null
                    ? Math.abs(1 - m.yesPrice - m.noPrice)
                    : null;
                const symbolKey = SYMBOL_MAP[m.asset];
                const spotPrice = priceData?.prices[symbolKey];

                return (
                  <tr key={m.id} className="border-b border-border/50 hover:bg-accent/30 transition-colors">
                    <td className="py-3 px-3">
                      <span className="px-2 py-0.5 bg-primary/20 text-primary rounded text-xs font-bold">
                        {m.asset}
                      </span>
                    </td>
                    <td className="py-3 px-3 max-w-xs">
                      <p className="text-xs text-foreground line-clamp-2">{m.question}</p>
                    </td>
                    <td className="py-3 px-3 text-right font-mono">
                      <span className="text-green-400">
                        {m.yesPrice !== null ? `$${m.yesPrice.toFixed(3)}` : "—"}
                      </span>
                    </td>
                    <td className="py-3 px-3 text-right font-mono">
                      <span className="text-red-400">
                        {m.noPrice !== null ? `$${m.noPrice.toFixed(3)}` : "—"}
                      </span>
                    </td>
                    <td className="py-3 px-3 text-right font-mono text-muted-foreground">
                      {spread !== null ? `${(spread * 100).toFixed(1)}%` : "—"}
                    </td>
                    <td className="py-3 px-3 text-right font-mono text-foreground">
                      {spotPrice
                        ? `$${spotPrice > 100 ? spotPrice.toLocaleString("en-US", { maximumFractionDigits: 0 }) : spotPrice.toFixed(4)}`
                        : "—"}
                    </td>
                    <td className="py-3 px-3 text-right font-mono text-muted-foreground">
                      {m.volume !== null ? `$${m.volume.toLocaleString("de-DE", { maximumFractionDigits: 0 })}` : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
