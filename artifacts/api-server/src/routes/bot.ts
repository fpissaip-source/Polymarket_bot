import { Router, type IRouter } from "express";
import { spawn, type ChildProcess } from "child_process";
import { readFileSync, existsSync } from "fs";
import { resolve, join } from "path";

const router: IRouter = Router();

const BOT_DIR = resolve(process.cwd(), "bot");
const BANKROLL_FILE = join(BOT_DIR, "bankroll_state.json");
const TRADES_FILE = join(BOT_DIR, "trades.json");
const LOG_FILE = join(BOT_DIR, "bot.log");

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
  return data.bankroll ?? 5.27;
}

function getTrades(): Array<{
  id: string; marketId: string; asset: string; side: string;
  price: number; size: number; pnl: number; timestamp: string; status: string;
}> {
  return readJson(TRADES_FILE, []);
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
  const wins = trades.filter((t) => t.pnl > 0);
  const totalPnl = trades.reduce((s, t) => s + t.pnl, 0);
  const winRate = trades.length > 0 ? wins.length / trades.length : 0;
  const biggestWin = trades.length > 0 ? Math.max(...trades.map((t) => t.pnl)) : 0;
  const biggestLoss = trades.length > 0 ? Math.min(...trades.map((t) => t.pnl)) : 0;

  let uptime: string | null = null;
  if (botStartTime && botProcess) {
    const seconds = Math.floor((Date.now() - botStartTime) / 1000);
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    uptime = `${h}h ${m}m ${s}s`;
  }

  res.json({
    running: botProcess !== null && botProcess.exitCode === null,
    mode: botProcess && botProcess.exitCode === null ? botMode : "stopped",
    bankroll: getBankroll(),
    totalTrades: trades.length,
    winRate,
    totalPnl,
    biggestWin,
    biggestLoss,
    marketsWatched: 0,
    uptime,
    pid: botProcess?.pid ?? null,
  });
});

router.get("/bot/trades", (req, res) => {
  res.json(getTrades());
});

router.get("/bot/logs", (req, res) => {
  const lines = getRecentLogs(100);
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
  if (!dryRun) args.push("--live");

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
