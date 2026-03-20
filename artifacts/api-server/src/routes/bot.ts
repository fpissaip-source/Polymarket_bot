import { Router, type IRouter } from "express";
import { spawn, type ChildProcess } from "child_process";
import { readFileSync, existsSync } from "fs";
import { resolve, join } from "path";

const router: IRouter = Router();

const BOT_DIR = resolve(process.cwd(), "../../bot");
const BANKROLL_FILE = join(BOT_DIR, "bankroll_state.json");
const TRADES_FILE = join(BOT_DIR, "trades.json");
const DRY_RUN_LOG = join(BOT_DIR, "dry_run_log.json");
const ADAPTIVE_STATE = join(BOT_DIR, "adaptive_state.json");
const LOG_FILE = join(BOT_DIR, "bot.log");
const DRY_RUN_BANKROLL = 25;

function getInitialBankroll(): number {
  try {
    if (existsSync(join(BOT_DIR, "config.py"))) {
      const cfg = readFileSync(join(BOT_DIR, "config.py"), "utf-8");
      const match = cfg.match(/DRY_RUN_BANKROLL\s*=\s*float\([^"]*"([\d.]+)"/);
      if (match) return parseFloat(match[1]);
    }
  } catch { /* fallback */ }
  return DRY_RUN_BANKROLL;
}

let botProcess: ChildProcess | null = null;
let botMode: "dry_run" | "live" | "stopped" = "stopped";
let botStartTime: number | null = null;

function readJson<T>(path: string, fallback: T): T {
  try {
    if (existsSync(path)) {
      return JSON.parse(readFileSync(path, "utf-8")) as T;
    }
  } catch { /* ignore */ }
  return fallback;
}

function getBankroll(): number {
  const data = readJson<{ bankroll?: number }>(BANKROLL_FILE, {});
  return data.bankroll ?? 2.0;
}

type TradeRecord = {
  id: string; marketId: string; asset: string; side: string;
  price: number; size: number; pnl: number; timestamp: string; status: string;
  question?: string; q?: number; edge?: number; confidence?: number;
  window_start?: string; window_end?: string;
  outcome?: string; actual_outcome?: string;
};

function getTrades(): TradeRecord[] {
  return readJson(TRADES_FILE, []);
}

type DryRunEntry = {
  timestamp: number; market_id: string; asset: string; side: string;
  q: number; p: number; edge: number; size: number; exec_price: number;
  outcome: string; pnl: number; question: string; timeframe: string;
  window_start: string; window_end: string; decision: string;
  confidence: number; bayesian_prior: number; kelly_lambda: number;
  min_edge_used: number; actual_outcome: string;
};

function getDryRunLog(): DryRunEntry[] {
  return readJson(DRY_RUN_LOG, []);
}

function getAdaptiveState(): Record<string, unknown> {
  return readJson(ADAPTIVE_STATE, {});
}

function getRecentLogs(n = 100): string[] {
  try {
    if (!existsSync(LOG_FILE)) return ["Bot log file not found. Start the bot to generate logs."];
    const content = readFileSync(LOG_FILE, "utf-8");
    const lines = content.split("\n").filter(Boolean);
    return lines.slice(-n);
  } catch {
    return [];
  }
}

router.get("/bot/status", (req, res) => {
  const trades = getTrades();
  const dryRunLog = getDryRunLog();
  const adaptive = getAdaptiveState();

  const resolved = dryRunLog.filter(e => e.outcome === "WIN" || e.outcome === "LOSS");
  const wins = resolved.filter(e => e.outcome === "WIN").length;
  const totalPnl = resolved.reduce((s, e) => s + (e.pnl || 0), 0);
  const winRate = resolved.length > 0 ? wins / resolved.length : 0;
  const biggestWin = resolved.length > 0 ? Math.max(0, ...resolved.map(e => e.pnl || 0)) : 0;
  const biggestLoss = resolved.length > 0 ? Math.min(0, ...resolved.map(e => e.pnl || 0)) : 0;

  const initialBk = getInitialBankroll();
  const virtualBankroll = initialBk + totalPnl;

  let uptime: string | null = null;
  if (botStartTime && botProcess) {
    const seconds = Math.floor((Date.now() - botStartTime) / 1000);
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    uptime = `${h}h ${m}m ${s}s`;
  }

  const perAsset: Record<string, { wins: number; total: number; pnl: number }> = {};
  for (const e of resolved) {
    if (!perAsset[e.asset]) perAsset[e.asset] = { wins: 0, total: 0, pnl: 0 };
    perAsset[e.asset].total++;
    if (e.outcome === "WIN") perAsset[e.asset].wins++;
    perAsset[e.asset].pnl += e.pnl || 0;
  }

  res.json({
    running: botProcess !== null && botProcess.exitCode === null,
    mode: botProcess && botProcess.exitCode === null ? botMode : "stopped",
    bankroll: getBankroll(),
    virtualBankroll: Math.round(virtualBankroll * 100) / 100,
    initialBankroll: initialBk,
    totalTrades: dryRunLog.length,
    resolvedTrades: resolved.length,
    openTrades: dryRunLog.length - resolved.length,
    winRate,
    totalPnl: Math.round(totalPnl * 10000) / 10000,
    biggestWin: Math.round(biggestWin * 10000) / 10000,
    biggestLoss: Math.round(biggestLoss * 10000) / 10000,
    marketsWatched: 0,
    uptime,
    pid: botProcess?.pid ?? null,
    perAsset,
    adaptive,
  });
});

router.get("/bot/trades", (req, res) => {
  const dryRunLog = getDryRunLog();
  const trades: TradeRecord[] = dryRunLog.map((e, i) => ({
    id: `dry_${i}_${Math.round(e.timestamp)}`,
    marketId: e.market_id,
    asset: e.asset,
    side: e.decision || e.side,
    price: e.exec_price,
    size: e.size,
    pnl: e.pnl || 0,
    timestamp: new Date(e.timestamp * 1000).toISOString(),
    status: e.outcome || "OPEN",
    question: e.question || "",
    q: e.q,
    edge: e.edge,
    confidence: e.confidence,
    window_start: e.window_start || "",
    window_end: e.window_end || "",
    outcome: e.outcome || "",
    actual_outcome: e.actual_outcome || "",
  }));
  res.json(trades);
});

router.get("/bot/simulation", (req, res) => {
  const dryRunLog = getDryRunLog();
  const adaptive = getAdaptiveState();

  const resolved = dryRunLog.filter(e => e.outcome === "WIN" || e.outcome === "LOSS");

  const pnlCurve: { time: string; pnl: number; bankroll: number }[] = [];
  let runningPnl = 0;
  for (const e of resolved) {
    runningPnl += e.pnl || 0;
    pnlCurve.push({
      time: new Date(e.timestamp * 1000).toISOString(),
      pnl: Math.round(runningPnl * 10000) / 10000,
      bankroll: Math.round((getInitialBankroll() + runningPnl) * 100) / 100,
    });
  }

  const decisionBreakdown = {
    UP: { total: 0, wins: 0, pnl: 0 },
    DOWN: { total: 0, wins: 0, pnl: 0 },
  };
  for (const e of resolved) {
    const d = e.decision === "UP" ? "UP" : "DOWN";
    decisionBreakdown[d].total++;
    if (e.outcome === "WIN") decisionBreakdown[d].wins++;
    decisionBreakdown[d].pnl += e.pnl || 0;
  }

  res.json({
    totalTrades: dryRunLog.length,
    resolvedTrades: resolved.length,
    pnlCurve,
    decisionBreakdown,
    adaptive,
    virtualBankroll: Math.round((getInitialBankroll() + runningPnl) * 100) / 100,
  });
});

router.get("/bot/logs", (req, res) => {
  const lines = getRecentLogs(200);
  res.json({ lines, total: lines.length });
});

router.post("/bot/start", (req, res) => {
  if (botProcess && botProcess.exitCode === null) {
    res.json({ success: false, message: "Bot is already running" });
    return;
  }

  const dryRun: boolean = req.body?.dryRun !== false;
  botMode = dryRun ? "dry_run" : "live";
  botStartTime = Date.now();

  const args = ["main.py"];
  if (dryRun) args.push("--dry-run");
  else args.push("--live");

  botProcess = spawn("python3", args, {
    cwd: BOT_DIR,
    env: { ...process.env },
    stdio: ["ignore", "pipe", "pipe"],
  });

  const logLine = (line: string) => {
    const { appendFileSync } = require("fs");
    appendFileSync(LOG_FILE, line + "\n");
  };

  botProcess.stdout?.on("data", (data: Buffer) => logLine(data.toString().trimEnd()));
  botProcess.stderr?.on("data", (data: Buffer) => logLine(data.toString().trimEnd()));
  botProcess.on("exit", (code) => {
    req.log?.info({ code }, "Bot process exited");
    botProcess = null;
    botMode = "stopped";
    botStartTime = null;
  });

  req.log.info({ pid: botProcess.pid, dryRun }, "Bot started");
  res.json({ success: true, message: `Bot started in ${botMode} mode (PID: ${botProcess.pid})` });
});

router.post("/bot/stop", (req, res) => {
  if (!botProcess || botProcess.exitCode !== null) {
    res.json({ success: false, message: "Bot is not running" });
    return;
  }
  botProcess.kill("SIGTERM");
  req.log.info({ pid: botProcess.pid }, "Bot stopped");
  res.json({ success: true, message: "Bot stopped" });
});

export default router;
