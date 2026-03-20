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
  question?: string;
  q?: number;
  edge?: number;
  confidence?: number;
  window_start?: string;
  window_end?: string;
  outcome?: string;
  actual_outcome?: string;
};

export function Trades() {
  const { data: trades, isLoading } = useQuery<Trade[]>({
    queryKey: ["bot-trades"],
    queryFn: async () => {
      const r = await fetch(`${BASE}/api/bot/trades`);
      return r.json();
    },
    refetchInterval: 3000,
  });

  const resolved = (trades ?? []).filter((t) => t.outcome === "WIN" || t.outcome === "LOSS");
  const totalPnl = resolved.reduce((s, t) => s + t.pnl, 0);
  const wins = resolved.filter((t) => t.outcome === "WIN").length;
  const losses = resolved.filter((t) => t.outcome === "LOSS").length;
  const openCount = (trades ?? []).filter((t) => !t.outcome || t.outcome === "OPEN" || t.outcome === "").length;
  const winRate = resolved.length > 0 ? (wins / resolved.length) * 100 : 0;

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-xl font-bold">Simulation Trade-Verlauf</h2>
          <p className="text-sm text-muted-foreground">Dry-Run Entscheidungen mit virtuellem Guthaben</p>
        </div>
        {(trades ?? []).length > 0 && (
          <div className="flex gap-4 text-sm flex-wrap">
            <span className="text-yellow-400">{openCount} Offen</span>
            <span className="text-green-400">{wins} Gewonnen</span>
            <span className="text-red-400">{losses} Verloren</span>
            <span className="text-muted-foreground">WR: {winRate.toFixed(1)}%</span>
            <span className={`font-mono font-bold ${totalPnl >= 0 ? "text-green-400" : "text-red-400"}`}>
              {totalPnl >= 0 ? "+" : ""}${totalPnl.toFixed(4)} P&L
            </span>
          </div>
        )}
      </div>

      {isLoading && (
        <div className="text-center py-12 text-muted-foreground">
          <p>Trades werden geladen...</p>
        </div>
      )}

      {trades && trades.length === 0 && (
        <div className="text-center py-16 text-muted-foreground border border-dashed border-border rounded-lg">
          <p className="font-medium text-lg mb-2">Noch keine Simulation-Trades</p>
          <p className="text-xs">Starte den Bot im Dry-Run Modus um die Simulation zu beginnen</p>
        </div>
      )}

      {trades && trades.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-muted-foreground text-xs uppercase tracking-wider">
                <th className="text-left py-3 px-2">Zeit</th>
                <th className="text-left py-3 px-2">Asset</th>
                <th className="text-left py-3 px-2">Entscheidung</th>
                <th className="text-right py-3 px-2">Prob (q)</th>
                <th className="text-right py-3 px-2">Preis</th>
                <th className="text-right py-3 px-2">Edge</th>
                <th className="text-right py-3 px-2">Einsatz</th>
                <th className="text-center py-3 px-2">Ergebnis</th>
                <th className="text-right py-3 px-2">P&L</th>
                <th className="text-left py-3 px-2">Zeitfenster</th>
              </tr>
            </thead>
            <tbody>
              {[...trades].reverse().map((t) => (
                <tr
                  key={t.id}
                  className={`border-b border-border/50 hover:bg-accent/30 transition-colors ${
                    t.outcome === "WIN" ? "bg-green-500/5" :
                    t.outcome === "LOSS" ? "bg-red-500/5" : ""
                  }`}
                >
                  <td className="py-2.5 px-2 text-muted-foreground text-xs font-mono">
                    {formatDistanceToNow(new Date(t.timestamp), { addSuffix: true, locale: de })}
                  </td>
                  <td className="py-2.5 px-2">
                    <span className="px-2 py-0.5 bg-primary/20 text-primary rounded text-xs font-bold">{t.asset}</span>
                  </td>
                  <td className="py-2.5 px-2">
                    <span className={`text-sm font-bold ${
                      t.side === "UP" ? "text-green-400" : "text-red-400"
                    }`}>
                      {t.side === "UP" ? "\u2191 UP" : "\u2193 DOWN"}
                    </span>
                  </td>
                  <td className="py-2.5 px-2 text-right font-mono text-xs">
                    {t.q != null ? `${(t.q * 100).toFixed(1)}%` : "—"}
                  </td>
                  <td className="py-2.5 px-2 text-right font-mono text-xs">${t.price.toFixed(3)}</td>
                  <td className="py-2.5 px-2 text-right font-mono text-xs">
                    {t.edge != null ? `${(t.edge * 100).toFixed(2)}%` : "—"}
                  </td>
                  <td className="py-2.5 px-2 text-right font-mono text-xs">${t.size.toFixed(2)}</td>
                  <td className="py-2.5 px-2 text-center">
                    {t.outcome === "WIN" && (
                      <span className="px-2 py-0.5 rounded text-xs font-bold bg-green-500/20 text-green-400">
                        GEWONNEN
                      </span>
                    )}
                    {t.outcome === "LOSS" && (
                      <span className="px-2 py-0.5 rounded text-xs font-bold bg-red-500/20 text-red-400">
                        VERLOREN
                      </span>
                    )}
                    {(!t.outcome || t.outcome === "" || t.outcome === "OPEN") && (
                      <span className="px-2 py-0.5 rounded text-xs font-medium bg-yellow-500/20 text-yellow-400 animate-pulse">
                        OFFEN
                      </span>
                    )}
                  </td>
                  <td className={`py-2.5 px-2 text-right font-mono font-bold text-xs ${
                    t.pnl > 0 ? "text-green-400" : t.pnl < 0 ? "text-red-400" : "text-muted-foreground"
                  }`}>
                    {t.outcome === "WIN" || t.outcome === "LOSS"
                      ? `${t.pnl >= 0 ? "+" : ""}$${t.pnl.toFixed(4)}`
                      : "—"}
                  </td>
                  <td className="py-2.5 px-2 text-xs text-muted-foreground font-mono">
                    {t.window_end ? new Date(t.window_end).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }) : "—"}
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
