import { useQuery } from "@tanstack/react-query";
import { formatDistanceToNow } from "date-fns";
import { de } from "date-fns/locale";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

type Trade = {
  id: string;
  marketId: string;
  asset: string;
  side: string;
  price: number;
  size: number;
  pnl: number;
  timestamp: string;
  status: string;
};

export function Trades() {
  const { data: trades, isLoading } = useQuery<Trade[]>({
    queryKey: ["bot-trades"],
    queryFn: async () => {
      const r = await fetch(`${BASE}/api/bot/trades`);
      return r.json();
    },
    refetchInterval: 5000,
  });

  const totalPnl = (trades ?? []).reduce((s, t) => s + t.pnl, 0);
  const wins = (trades ?? []).filter((t) => t.pnl > 0).length;
  const losses = (trades ?? []).filter((t) => t.pnl < 0).length;

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-bold">Trade-Verlauf</h2>
          <p className="text-sm text-muted-foreground">Alle ausgeführten Trades</p>
        </div>
        {trades && trades.length > 0 && (
          <div className="flex gap-4 text-sm">
            <span className="text-green-400">{wins} Gewinne</span>
            <span className="text-red-400">{losses} Verluste</span>
            <span className={`font-mono font-bold ${totalPnl >= 0 ? "text-green-400" : "text-red-400"}`}>
              {totalPnl >= 0 ? "+" : ""}${totalPnl.toFixed(2)} gesamt
            </span>
          </div>
        )}
      </div>

      {isLoading && (
        <div className="text-center py-12 text-muted-foreground">
          <div className="text-2xl mb-2">⏳</div>
          <p>Trades werden geladen...</p>
        </div>
      )}

      {trades && trades.length === 0 && (
        <div className="text-center py-16 text-muted-foreground border border-dashed border-border rounded-lg">
          <div className="text-3xl mb-3">📭</div>
          <p className="font-medium">Noch keine Trades</p>
          <p className="text-xs mt-1">Starte den Bot, um Trades zu sehen</p>
        </div>
      )}

      {trades && trades.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-muted-foreground text-xs uppercase tracking-wider">
                <th className="text-left py-3 px-3">Zeit</th>
                <th className="text-left py-3 px-3">Asset</th>
                <th className="text-left py-3 px-3">Seite</th>
                <th className="text-right py-3 px-3">Preis</th>
                <th className="text-right py-3 px-3">Größe</th>
                <th className="text-right py-3 px-3">P&L</th>
                <th className="text-left py-3 px-3">Status</th>
              </tr>
            </thead>
            <tbody>
              {[...trades].reverse().map((t) => (
                <tr
                  key={t.id}
                  className={`border-b border-border/50 hover:bg-accent/30 transition-colors ${
                    t.pnl > 0 ? "bg-green-500/5" : t.pnl < 0 ? "bg-red-500/5" : ""
                  }`}
                >
                  <td className="py-3 px-3 text-muted-foreground text-xs font-mono">
                    {formatDistanceToNow(new Date(t.timestamp), { addSuffix: true, locale: de })}
                  </td>
                  <td className="py-3 px-3">
                    <span className="px-2 py-0.5 bg-primary/20 text-primary rounded text-xs font-bold">{t.asset}</span>
                  </td>
                  <td className="py-3 px-3">
                    <span className={`text-xs font-medium ${t.side === "BUY" ? "text-green-400" : "text-red-400"}`}>
                      {t.side}
                    </span>
                  </td>
                  <td className="py-3 px-3 text-right font-mono">${t.price.toFixed(3)}</td>
                  <td className="py-3 px-3 text-right font-mono">${t.size.toFixed(2)}</td>
                  <td className={`py-3 px-3 text-right font-mono font-bold ${t.pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                    {t.pnl >= 0 ? "+" : ""}${t.pnl.toFixed(2)}
                  </td>
                  <td className="py-3 px-3">
                    <span className="text-xs text-muted-foreground">{t.status}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
