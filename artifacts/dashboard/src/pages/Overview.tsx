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

async function fetchSimulation() {
  const r = await fetch(`${BASE}/api/bot/simulation`);
  return r.json();
}

type BotStatus = {
  running: boolean;
  mode: "dry_run" | "live" | "stopped";
  bankroll: number;
  virtualBankroll: number;
  initialBankroll: number;
  totalTrades: number;
  resolvedTrades: number;
  openTrades: number;
  winRate: number;
  totalPnl: number;
  biggestWin: number;
  biggestLoss: number;
  marketsWatched: number;
  uptime: string | null;
  pid: number | null;
  perAsset: Record<string, { wins: number; total: number; pnl: number }>;
  adaptive: Record<string, unknown>;
};

type PriceMap = { prices: Record<string, number>; updatedAt: string };

type RegimeInfo = { regime: string; vol: number; kelly_mult: number };

type SimulationData = {
  totalTrades: number;
  resolvedTrades: number;
  pnlCurve: { time: string; pnl: number; bankroll: number }[];
  decisionBreakdown: {
    UP: { total: number; wins: number; pnl: number };
    DOWN: { total: number; wins: number; pnl: number };
  };
  adaptive: Record<string, unknown>;
  virtualBankroll: number;
  regime: Record<string, RegimeInfo>;
  gas: { gwei?: number; matic_usd?: number; gas_cost_usd?: number; veto_ratio?: number };
};

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
    return <span className="px-2 py-0.5 rounded text-xs font-medium bg-yellow-500/20 text-yellow-400">DRY RUN SIMULATION</span>;
  }
  return <span className="px-2 py-0.5 rounded text-xs font-medium bg-green-500/20 text-green-400 animate-pulse">LIVE</span>;
}

function PnlBar({ pnl, maxPnl }: { pnl: number; maxPnl: number }) {
  const width = maxPnl > 0 ? Math.min(100, (Math.abs(pnl) / maxPnl) * 100) : 0;
  return (
    <div className="w-full bg-muted/30 rounded h-2">
      <div
        className={`h-2 rounded ${pnl >= 0 ? "bg-green-500" : "bg-red-500"}`}
        style={{ width: `${width}%` }}
      />
    </div>
  );
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

  const { data: sim } = useQuery<SimulationData>({
    queryKey: ["simulation"],
    queryFn: fetchSimulation,
    refetchInterval: 3000,
  });

  const pnlColor = !status ? "" : status.totalPnl >= 0 ? "text-green-400" : "text-red-400";
  const bankrollChange = status ? ((status.virtualBankroll - status.initialBankroll) / status.initialBankroll) * 100 : 0;

  const adaptive = (status?.adaptive ?? {}) as Record<string, unknown>;

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-bold">Dashboard</h2>
          <p className="text-sm text-muted-foreground">Dry-Run Simulation mit virtuellem Guthaben</p>
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
          label="Virtuelles Guthaben"
          value={`$${status?.virtualBankroll?.toFixed(2) ?? "25.00"}`}
          sub={bankrollChange !== 0 ? `${bankrollChange >= 0 ? "+" : ""}${bankrollChange.toFixed(1)}% seit Start ($${status?.initialBankroll ?? 25})` : `Startkapital: $${status?.initialBankroll ?? 25}`}
          color={bankrollChange >= 0 ? "text-green-400" : "text-red-400"}
        />
        <StatCard
          label="Gesamt P&L"
          value={status ? `${status.totalPnl >= 0 ? "+" : ""}$${status.totalPnl.toFixed(4)}` : "--"}
          color={pnlColor}
        />
        <StatCard
          label="Win Rate"
          value={status ? `${(status.winRate * 100).toFixed(1)}%` : "--"}
          sub={`${status?.resolvedTrades ?? 0} abgeschlossen / ${status?.totalTrades ?? 0} gesamt`}
        />
        <StatCard
          label="Offene Trades"
          value={`${status?.openTrades ?? 0}`}
          sub="warten auf Ergebnis"
          color="text-yellow-400"
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <StatCard
          label="Live Bankroll"
          value={`$${status?.bankroll?.toFixed(2) ?? "2.00"}`}
          sub="fuer Live-Trading"
        />
        <div className="bg-card border border-border rounded-lg p-4">
          <p className="text-xs text-muted-foreground mb-2">Entscheidungen</p>
          {sim?.decisionBreakdown ? (
            <div className="space-y-2">
              <div className="flex justify-between text-sm">
                <span className="text-green-400 font-bold">UP</span>
                <span className="font-mono text-xs">
                  {sim.decisionBreakdown.UP.wins}/{sim.decisionBreakdown.UP.total} Wins
                  {sim.decisionBreakdown.UP.total > 0 && ` (${((sim.decisionBreakdown.UP.wins / sim.decisionBreakdown.UP.total) * 100).toFixed(0)}%)`}
                </span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-red-400 font-bold">DOWN</span>
                <span className="font-mono text-xs">
                  {sim.decisionBreakdown.DOWN.wins}/{sim.decisionBreakdown.DOWN.total} Wins
                  {sim.decisionBreakdown.DOWN.total > 0 && ` (${((sim.decisionBreakdown.DOWN.wins / sim.decisionBreakdown.DOWN.total) * 100).toFixed(0)}%)`}
                </span>
              </div>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">Keine Daten</p>
          )}
        </div>
      </div>

      {status?.perAsset && Object.keys(status.perAsset).length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-muted-foreground mb-3 uppercase tracking-wider">Performance pro Asset</h3>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            {Object.entries(status.perAsset).map(([asset, data]) => {
              const wr = data.total > 0 ? (data.wins / data.total) * 100 : 0;
              const maxPnl = Math.max(...Object.values(status.perAsset).map(d => Math.abs(d.pnl)), 0.01);
              return (
                <div key={asset} className="bg-card border border-border rounded-lg p-3">
                  <div className="flex justify-between items-center mb-2">
                    <span className="text-sm font-bold">{asset}</span>
                    <span className={`text-xs font-mono font-bold ${data.pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                      {data.pnl >= 0 ? "+" : ""}${data.pnl.toFixed(4)}
                    </span>
                  </div>
                  <PnlBar pnl={data.pnl} maxPnl={maxPnl} />
                  <div className="flex justify-between mt-2 text-xs text-muted-foreground">
                    <span>{data.wins}/{data.total} Wins</span>
                    <span>{wr.toFixed(0)}% WR</span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {adaptive && (adaptive as Record<string, unknown>).total_analyzed != null && (
        <div>
          <h3 className="text-sm font-semibold text-muted-foreground mb-3 uppercase tracking-wider">Adaptive Optimierung</h3>
          <div className="bg-card border border-border rounded-lg p-4 grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div>
              <p className="text-xs text-muted-foreground">Kelly Anpassung</p>
              <p className={`font-mono font-bold ${(adaptive.kelly_lambda_adj as number) >= 0 ? "text-green-400" : "text-red-400"}`}>
                {(adaptive.kelly_lambda_adj as number) >= 0 ? "+" : ""}{((adaptive.kelly_lambda_adj as number) ?? 0).toFixed(4)}
              </p>
            </div>
            <div>
              <p className="text-xs text-muted-foreground">Edge Anpassung</p>
              <p className={`font-mono font-bold ${(adaptive.edge_threshold_adj as number) <= 0 ? "text-green-400" : "text-red-400"}`}>
                {(adaptive.edge_threshold_adj as number) >= 0 ? "+" : ""}{((adaptive.edge_threshold_adj as number) ?? 0).toFixed(4)}
              </p>
            </div>
            <div>
              <p className="text-xs text-muted-foreground">Trades analysiert</p>
              <p className="font-mono font-bold">{(adaptive.total_analyzed as number) ?? 0}</p>
            </div>
            <div>
              <p className="text-xs text-muted-foreground">Asset Bias</p>
              <div className="flex gap-2 flex-wrap">
                {adaptive.asset_bias && Object.entries(adaptive.asset_bias as Record<string, number>).map(([a, v]) => (
                  <span key={a} className={`text-xs font-mono ${v >= 0 ? "text-green-400" : "text-red-400"}`}>
                    {a}: {v >= 0 ? "+" : ""}{v.toFixed(3)}
                  </span>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {sim?.regime && Object.keys(sim.regime).length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-muted-foreground mb-3 uppercase tracking-wider">Regime-Detektor (Wetterfrosch)</h3>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            {Object.entries(sim.regime).map(([asset, info]) => {
              const r = info as RegimeInfo;
              const isBreakout = r.regime === "HIGH_VOL_BREAKOUT";
              const isTrending = r.regime === "TRENDING";
              const color = isBreakout ? "text-red-400" : isTrending ? "text-yellow-400" : "text-green-400";
              const emoji = isBreakout ? "⚡" : isTrending ? "📈" : "〰️";
              return (
                <div key={asset} className="bg-card border border-border rounded-lg p-3">
                  <div className="flex justify-between items-center mb-2">
                    <span className="font-bold text-sm">{asset}</span>
                    <span className={`text-xs font-mono font-bold ${color}`}>{emoji} {r.regime.replace(/_/g, " ")}</span>
                  </div>
                  <div className="grid grid-cols-2 gap-1 text-xs text-muted-foreground">
                    <span>Vol: <span className="text-foreground font-mono">{r.vol.toFixed(5)}</span></span>
                    <span>Kelly×: <span className={`font-mono font-bold ${color}`}>{r.kelly_mult.toFixed(2)}</span></span>
                  </div>
                </div>
              );
            })}
          </div>
          {sim.gas && sim.gas.gwei != null && (
            <div className="mt-2 bg-card border border-border rounded-lg p-3 flex gap-6 text-xs text-muted-foreground">
              <span>Gas: <span className="text-foreground font-mono">{sim.gas.gwei?.toFixed(1)} Gwei</span></span>
              <span>MATIC: <span className="text-foreground font-mono">${sim.gas.matic_usd?.toFixed(4)}</span></span>
              <span>Tx-Kosten: <span className="text-foreground font-mono">${sim.gas.gas_cost_usd?.toFixed(5)}</span></span>
              <span>Veto bei: <span className="text-orange-400 font-mono">&gt;{((sim.gas.veto_ratio ?? 0.3) * 100).toFixed(0)}% Edge</span></span>
            </div>
          )}
        </div>
      )}

      {sim?.pnlCurve && sim.pnlCurve.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold text-muted-foreground mb-3 uppercase tracking-wider">P&L Verlauf</h3>
          <div className="bg-card border border-border rounded-lg p-4">
            <div className="flex items-end gap-1 h-32">
              {sim.pnlCurve.map((point, i) => {
                const maxAbs = Math.max(...sim.pnlCurve.map(p => Math.abs(p.pnl)), 0.01);
                const height = Math.max(2, (Math.abs(point.pnl) / maxAbs) * 100);
                return (
                  <div
                    key={i}
                    className={`flex-1 rounded-t ${point.pnl >= 0 ? "bg-green-500/60" : "bg-red-500/60"}`}
                    style={{ height: `${height}%`, minWidth: "3px" }}
                    title={`${new Date(point.time).toLocaleTimeString("de-DE")} | P&L: $${point.pnl.toFixed(4)} | Bankroll: $${point.bankroll.toFixed(2)}`}
                  />
                );
              })}
            </div>
            <div className="flex justify-between mt-2 text-xs text-muted-foreground">
              <span>{sim.pnlCurve.length > 0 ? new Date(sim.pnlCurve[0].time).toLocaleTimeString("de-DE") : ""}</span>
              <span>{sim.pnlCurve.length > 0 ? new Date(sim.pnlCurve[sim.pnlCurve.length - 1].time).toLocaleTimeString("de-DE") : ""}</span>
            </div>
          </div>
        </div>
      )}

      <div>
        <h3 className="text-sm font-semibold text-muted-foreground mb-3 uppercase tracking-wider">Spot Preise (CoinGecko)</h3>
        <div className="grid grid-cols-3 md:grid-cols-6 gap-3">
          {priceData &&
            Object.entries(priceData.prices).map(([symbol, price]) => (
              <div key={symbol} className="bg-card border border-border rounded-lg p-3 text-center">
                <p className="text-xs text-muted-foreground mb-1">{SYMBOL_LABELS[symbol] ?? symbol}</p>
                <p className="text-sm font-bold font-mono text-foreground">
                  ${(price as number) > 100 ? (price as number).toLocaleString("en-US", { maximumFractionDigits: 0 }) : (price as number).toFixed(4)}
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
