import { useQuery } from "@tanstack/react-query";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

async function fetchStatus() {
  const r = await fetch(`${BASE}/api/bot/status`);
  return r.json();
}

async function fetchPrices() {
  const r = await fetch(`${BASE}/api/markets/prices`);
  return r.json();
}

type BotStatus = {
  running: boolean;
  mode: "dry_run" | "live" | "stopped";
  bankroll: number;
  totalTrades: number;
  winRate: number;
  totalPnl: number;
  biggestWin: number;
  biggestLoss: number;
  marketsWatched: number;
  uptime: string | null;
  pid: number | null;
};

type PriceMap = { prices: Record<string, number>; updatedAt: string };

const SYMBOL_LABELS: Record<string, string> = {
  BTCUSDT: "BTC",
  ETHUSDT: "ETH",
  SOLUSDT: "SOL",
  XRPUSDT: "XRP",
  DOGEUSDT: "DOGE",
  BNBUSDT: "BNB",
};

function StatCard({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div className="bg-card border border-border rounded-lg p-4">
      <p className="text-xs text-muted-foreground mb-1">{label}</p>
      <p className={`text-2xl font-bold font-mono ${color ?? "text-foreground"}`}>{value}</p>
      {sub && <p className="text-xs text-muted-foreground mt-1">{sub}</p>}
    </div>
  );
}

function StatusBadge({ mode, running }: { mode: string; running: boolean }) {
  if (!running || mode === "stopped") {
    return <span className="px-2 py-0.5 rounded text-xs font-medium bg-muted text-muted-foreground">GESTOPPT</span>;
  }
  if (mode === "dry_run") {
    return <span className="px-2 py-0.5 rounded text-xs font-medium bg-yellow-500/20 text-yellow-400">DRY RUN</span>;
  }
  return <span className="px-2 py-0.5 rounded text-xs font-medium bg-green-500/20 text-green-400 animate-pulse">● LIVE</span>;
}

export function Overview() {
  const { data: status } = useQuery<BotStatus>({
    queryKey: ["bot-status"],
    queryFn: fetchStatus,
    refetchInterval: 2000,
  });

  const { data: priceData } = useQuery<PriceMap>({
    queryKey: ["crypto-prices"],
    queryFn: fetchPrices,
    refetchInterval: 5000,
  });

  const pnlColor = !status ? "" : status.totalPnl >= 0 ? "text-green-400" : "text-red-400";
  const bankrollChange = status ? ((status.bankroll - 5.27) / 5.27) * 100 : 0;

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-bold">Dashboard Übersicht</h2>
          <p className="text-sm text-muted-foreground">Echtzeit-Bot-Statistiken</p>
        </div>
        <div className="flex items-center gap-3">
          {status && <StatusBadge mode={status.mode} running={status.running} />}
          {status?.uptime && (
            <span className="text-xs text-muted-foreground font-mono">Laufzeit: {status.uptime}</span>
          )}
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          label="Bankroll"
          value={`$${status?.bankroll?.toFixed(2) ?? "—"}`}
          sub={bankrollChange !== 0 ? `${bankrollChange >= 0 ? "+" : ""}${bankrollChange.toFixed(1)}% seit Start` : undefined}
          color={bankrollChange >= 0 ? "text-green-400" : "text-red-400"}
        />
        <StatCard
          label="Gesamt P&L"
          value={status ? `${status.totalPnl >= 0 ? "+" : ""}$${status.totalPnl.toFixed(2)}` : "—"}
          color={pnlColor}
        />
        <StatCard
          label="Win Rate"
          value={status ? `${(status.winRate * 100).toFixed(1)}%` : "—"}
          sub={`${status?.totalTrades ?? 0} Trades gesamt`}
        />
        <StatCard
          label="Märkte beobachtet"
          value={`${status?.marketsWatched ?? 0}`}
          sub="aktive Märkte"
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <StatCard
          label="Größter Gewinn"
          value={status ? `+$${status.biggestWin.toFixed(2)}` : "—"}
          color="text-green-400"
        />
        <StatCard
          label="Größter Verlust"
          value={status ? `$${status.biggestLoss.toFixed(2)}` : "—"}
          color="text-red-400"
        />
      </div>

      <div>
        <h3 className="text-sm font-semibold text-muted-foreground mb-3 uppercase tracking-wider">Spot Preise (CoinGecko)</h3>
        <div className="grid grid-cols-3 md:grid-cols-6 gap-3">
          {priceData &&
            Object.entries(priceData.prices).map(([symbol, price]) => (
              <div key={symbol} className="bg-card border border-border rounded-lg p-3 text-center">
                <p className="text-xs text-muted-foreground mb-1">{SYMBOL_LABELS[symbol] ?? symbol}</p>
                <p className="text-sm font-bold font-mono text-foreground">
                  ${price > 100 ? price.toLocaleString("en-US", { maximumFractionDigits: 0 }) : price.toFixed(4)}
                </p>
              </div>
            ))}
        </div>
        {priceData && (
          <p className="text-xs text-muted-foreground mt-2">
            Aktualisiert: {new Date(priceData.updatedAt).toLocaleTimeString("de-DE")}
          </p>
        )}
      </div>
    </div>
  );
}
