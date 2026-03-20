import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

type BotStatus = {
  running: boolean;
  mode: "dry_run" | "live" | "stopped";
  bankroll: number;
  totalTrades: number;
  winRate: number;
  totalPnl: number;
  uptime: string | null;
  pid: number | null;
};

type ActionResponse = { success: boolean; message: string };

export function BotControl() {
  const [dryRun, setDryRun] = useState(true);
  const [showLiveWarning, setShowLiveWarning] = useState(false);
  const queryClient = useQueryClient();

  const { data: status } = useQuery<BotStatus>({
    queryKey: ["bot-status"],
    queryFn: async () => {
      const r = await fetch(`${BASE}/api/bot/status`);
      return r.json();
    },
    refetchInterval: 2000,
  });

  const startMutation = useMutation<ActionResponse, Error, boolean>({
    mutationFn: async (dr: boolean) => {
      const r = await fetch(`${BASE}/api/bot/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dryRun: dr }),
      });
      return r.json();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["bot-status"] });
      setShowLiveWarning(false);
    },
  });

  const stopMutation = useMutation<ActionResponse>({
    mutationFn: async () => {
      const r = await fetch(`${BASE}/api/bot/stop`, { method: "POST" });
      return r.json();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["bot-status"] });
    },
  });

  const isRunning = status?.running ?? false;
  const lastMessage = startMutation.data?.message ?? stopMutation.data?.message;

  return (
    <div className="p-6 space-y-6">
      <div>
        <h2 className="text-xl font-bold">Bot Steuerung</h2>
        <p className="text-sm text-muted-foreground">Starten, Stoppen und Konfigurieren des Trading Bots</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-card border border-border rounded-lg p-6">
          <h3 className="font-semibold mb-4">Bot Status</h3>
          <div className="space-y-3">
            <div className="flex justify-between">
              <span className="text-muted-foreground text-sm">Status</span>
              <span className={`text-sm font-medium ${isRunning ? "text-green-400" : "text-muted-foreground"}`}>
                {isRunning ? "🟢 Läuft" : "⚫ Gestoppt"}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground text-sm">Modus</span>
              <span className={`text-sm font-medium ${
                status?.mode === "live" ? "text-red-400" :
                status?.mode === "dry_run" ? "text-yellow-400" : "text-muted-foreground"
              }`}>
                {status?.mode === "live" ? "🔴 LIVE" : status?.mode === "dry_run" ? "🟡 Dry Run" : "—"}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground text-sm">PID</span>
              <span className="text-sm font-mono">{status?.pid ?? "—"}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground text-sm">Laufzeit</span>
              <span className="text-sm font-mono">{status?.uptime ?? "—"}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground text-sm">Bankroll</span>
              <span className="text-sm font-mono font-bold text-foreground">${status?.bankroll?.toFixed(2) ?? "—"}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground text-sm">Trades</span>
              <span className="text-sm font-mono">{status?.totalTrades ?? 0}</span>
            </div>
          </div>
        </div>

        <div className="bg-card border border-border rounded-lg p-6 space-y-4">
          <h3 className="font-semibold">Bot steuern</h3>

          {!isRunning && (
            <div className="space-y-3">
              <div>
                <label className="text-sm text-muted-foreground mb-2 block">Handelsmodus</label>
                <div className="flex gap-2">
                  <button
                    onClick={() => { setDryRun(true); setShowLiveWarning(false); }}
                    className={`flex-1 py-2 px-3 rounded text-sm font-medium border transition-colors ${
                      dryRun
                        ? "bg-yellow-500/20 border-yellow-500/50 text-yellow-400"
                        : "border-border text-muted-foreground hover:border-border/80"
                    }`}
                  >
                    🟡 Dry Run
                  </button>
                  <button
                    onClick={() => { setDryRun(false); setShowLiveWarning(true); }}
                    className={`flex-1 py-2 px-3 rounded text-sm font-medium border transition-colors ${
                      !dryRun
                        ? "bg-red-500/20 border-red-500/50 text-red-400"
                        : "border-border text-muted-foreground hover:border-border/80"
                    }`}
                  >
                    🔴 Live Trading
                  </button>
                </div>
              </div>

              {showLiveWarning && !dryRun && (
                <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-3">
                  <p className="text-red-400 text-xs font-medium mb-1">⚠️ WARNUNG: Live Trading Modus</p>
                  <p className="text-xs text-muted-foreground">
                    Im Live-Modus werden echte Trades mit echtem Geld ausgeführt. Stelle sicher, dass deine API-Keys und Private Key korrekt konfiguriert sind.
                  </p>
                </div>
              )}

              <button
                onClick={() => startMutation.mutate(dryRun)}
                disabled={startMutation.isPending}
                className="w-full py-3 rounded-lg font-semibold text-sm bg-primary text-primary-foreground hover:opacity-90 transition-opacity disabled:opacity-50"
              >
                {startMutation.isPending ? "Starte..." : "▶ Bot Starten"}
              </button>
            </div>
          )}

          {isRunning && (
            <div className="space-y-3">
              <div className="bg-green-500/10 border border-green-500/30 rounded-lg p-3">
                <p className="text-green-400 text-sm font-medium">✅ Bot läuft</p>
                <p className="text-xs text-muted-foreground mt-1">
                  Modus: {status?.mode === "dry_run" ? "Dry Run (keine echten Trades)" : "LIVE TRADING"}
                </p>
              </div>
              <button
                onClick={() => stopMutation.mutate()}
                disabled={stopMutation.isPending}
                className="w-full py-3 rounded-lg font-semibold text-sm bg-red-500/20 border border-red-500/40 text-red-400 hover:bg-red-500/30 transition-colors disabled:opacity-50"
              >
                {stopMutation.isPending ? "Stoppe..." : "⏹ Bot Stoppen"}
              </button>
            </div>
          )}

          {lastMessage && (
            <p className="text-xs text-muted-foreground border border-border rounded p-2 font-mono">
              {lastMessage}
            </p>
          )}
        </div>
      </div>

      <div className="bg-card border border-border rounded-lg p-6">
        <h3 className="font-semibold mb-3">API Keys Konfiguration</h3>
        <p className="text-sm text-muted-foreground mb-3">
          Für Live-Trading benötigst du folgende Umgebungsvariablen in der <code className="font-mono bg-accent px-1 py-0.5 rounded text-xs">bot/.env</code> Datei:
        </p>
        <div className="space-y-2 font-mono text-xs text-muted-foreground bg-black/30 rounded-lg p-4">
          <p><span className="text-blue-400">POLYMARKET_PRIVATE_KEY</span>=dein_private_key</p>
          <p><span className="text-blue-400">POLYMARKET_API_KEY</span>=dein_api_key</p>
          <p><span className="text-blue-400">POLYMARKET_API_SECRET</span>=dein_api_secret</p>
          <p><span className="text-blue-400">POLYMARKET_API_PASSPHRASE</span>=deine_passphrase</p>
          <p><span className="text-blue-400">BANKROLL</span>=dein_startkapital_in_usd</p>
        </div>
        <p className="text-xs text-muted-foreground mt-3">
          API-Keys erhältst du auf <a href="https://polymarket.com" target="_blank" className="text-primary hover:underline">polymarket.com</a> — verbinde dein Wallet und gehe zu den API-Einstellungen.
        </p>
      </div>
    </div>
  );
}
