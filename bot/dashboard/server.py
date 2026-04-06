"""
Polymarket Bot – Live Dashboard Server (v2)
==========================================
Erreichbar per Browser (Safari / Chrome):  http://<SERVER-IP>:8080

Neu in v2:
  • Gemini KI-Reasoning Feed (letzte Entscheidungen + Begründung)
  • Aktive Positionen (laufende Trades)
  • Portfolio-Übersicht mit Marktregime
  • Automatische Aktualisierung alle 3 Sekunden
"""

import json
import os
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

DASHBOARD_DIR = Path(__file__).parent
BOT_ROOT = DASHBOARD_DIR.parent
STATE_FILE   = BOT_ROOT / "bankroll_state.json"
LOG_FILE     = BOT_ROOT / "bot.log"
TRADES_FILE  = BOT_ROOT / "trades.json"
POSITIONS_FILE = BOT_ROOT / "live_positions.json"
GEMINI_FILE  = BOT_ROOT / "gemini_decisions.json"
REGIME_FILE  = BOT_ROOT / "regime_state.json"

PORT = 5002

# ---------------------------------------------------------------------------
# Live CLOB balance cache (updated every 30s in background thread)
# ---------------------------------------------------------------------------
_live_balance: float = 0.0
_live_balance_lock = threading.Lock()
_live_balance_ts: float = 0.0


def _update_live_balance():
    """Background thread: fetch real CLOB balance every 30 seconds."""
    global _live_balance, _live_balance_ts
    # Add bot root to sys.path so we can import bot modules
    bot_root_str = str(BOT_ROOT)
    if bot_root_str not in sys.path:
        sys.path.insert(0, bot_root_str)
    while True:
        try:
            from trading.order_executor import fetch_live_balance_usd
            bal = fetch_live_balance_usd()
            if bal > 0:
                with _live_balance_lock:
                    _live_balance = bal
                    _live_balance_ts = time.time()
        except Exception:
            pass
        time.sleep(30)


# Start background balance-updater as daemon thread
_balance_thread = threading.Thread(target=_update_live_balance, daemon=True, name="live-balance")
_balance_thread.start()


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _safe_read(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default


def load_bankroll() -> float:
    """Return live CLOB balance if fresh (< 90s), else fall back to JSON state file."""
    with _live_balance_lock:
        bal = _live_balance
        ts = _live_balance_ts
    if bal > 0 and (time.time() - ts) < 90:
        return bal
    data = _safe_read(STATE_FILE, {})
    return float(data.get("bankroll", 0.0))


def load_trades() -> list:
    return _safe_read(TRADES_FILE, [])


def load_positions() -> list:
    raw = _safe_read(POSITIONS_FILE, {})
    if isinstance(raw, dict):
        return list(raw.values())
    return raw


def load_gemini_decisions() -> list:
    data = _safe_read(GEMINI_FILE, [])
    # Return newest first, last 60 for richer history
    return list(reversed(data[-60:]))


def load_regime() -> dict:
    return _safe_read(REGIME_FILE, {})


SHARPE_FILE  = BOT_ROOT / "sharpe_state.json"
CLUSTER_FILE = BOT_ROOT / "alpha_cluster_state.json"


def load_sharpe() -> dict:
    return _safe_read(SHARPE_FILE, {})


def load_clusters() -> dict:
    return _safe_read(CLUSTER_FILE, {})


def load_recent_logs(n: int = 80) -> list:
    try:
        if not LOG_FILE.exists():
            return ["Keine Log-Datei gefunden. Starte den Bot: python3 main.py"]
        lines = LOG_FILE.read_text(errors="replace").splitlines()
        return lines[-n:]
    except Exception:
        return []


def get_dashboard_state() -> dict:
    bankroll  = load_bankroll()   # free CLOB cash (live from API)
    trades    = load_trades()
    positions = load_positions()
    gemini    = load_gemini_decisions()
    regime    = load_regime()
    logs      = load_recent_logs(80)

    # Committed capital: sum of entry sizes in open positions
    committed = sum(float(p.get("size") or p.get("entry_size") or 0) for p in positions)
    total_portfolio = bankroll + committed  # free cash + locked in positions

    total_trades = len(trades)
    wins   = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) < 0]
    total_pnl   = sum(t.get("pnl", 0) for t in trades)
    win_rate    = len(wins) / total_trades * 100 if total_trades else 0.0
    biggest_win  = max((t.get("pnl", 0) for t in trades), default=0)
    biggest_loss = min((t.get("pnl", 0) for t in trades), default=0)

    recent = trades[-20:] if len(trades) >= 20 else trades
    equity_curve = []
    running = bankroll - total_pnl  # start value
    for t in trades:
        running += t.get("pnl", 0)
        equity_curve.append(round(running, 4))

    # Active positions summary
    active_positions = []
    now = time.time()
    for pos in positions:
        end_t = pos.get("end_time", 0)
        remaining = max(0, end_t - now) if end_t > 0 else 0
        active_positions.append({
            "market":  (pos.get("market_id") or pos.get("market") or "?")[:40],
            "question": (pos.get("question") or "")[:60],
            "side":    pos.get("side", "?"),
            "size":    round(float(pos.get("size", 0)), 2),
            "entry":   round(float(pos.get("exec_price") or pos.get("entry_price") or 0), 4),
            "remaining_min": round(remaining / 60, 1) if remaining > 0 else None,
        })

    # Regime info
    regimes = regime.get("regimes", {})
    gas_info = regime.get("gas", {})
    tick     = regime.get("tick", 0)

    # Sharpe & performance data
    sharpe_data = load_sharpe()
    sharpe_ratio = 0.0
    sharpe_class = "NO_DATA"
    max_drawdown = 0.0
    if sharpe_data:
        pnl_history = sharpe_data.get("pnl_history", [])
        if len(pnl_history) >= 2:
            mean_pnl = sum(pnl_history) / len(pnl_history)
            var_pnl = sum((p - mean_pnl) ** 2 for p in pnl_history) / len(pnl_history)
            std_pnl = var_pnl ** 0.5
            sharpe_ratio = round(mean_pnl / std_pnl, 3) if std_pnl > 1e-8 else 0.0
        if sharpe_ratio >= 2.0:
            sharpe_class = "EXCELLENT"
        elif sharpe_ratio >= 1.0:
            sharpe_class = "SOLID"
        else:
            sharpe_class = "UNSTABLE"
        peak = sharpe_data.get("peak_capital", 0)
        current = sharpe_data.get("current_capital", 0)
        if peak > 0:
            max_drawdown = round((peak - current) / peak * 100, 1)

    # Alpha cluster summary
    cluster_data = load_clusters()
    cluster_summary = []
    for cid, c in cluster_data.items():
        n = c.get("total_trades", 0)
        if n > 0:
            wr = c.get("wins", 0) / n if n > 0 else 0
            cluster_summary.append({
                "cluster": cid,
                "trades": n,
                "win_rate": round(wr * 100, 1),
                "pnl": round(c.get("total_pnl", 0), 4),
            })
    cluster_summary.sort(key=lambda x: x.get("pnl", 0), reverse=True)

    return {
        "bankroll":    round(bankroll, 2),
        "free_cash":   round(bankroll, 2),
        "committed":   round(committed, 2),
        "total_portfolio": round(total_portfolio, 2),
        "balance_fresh": (time.time() - _live_balance_ts) < 90 and _live_balance > 0,
        "total_trades": total_trades,
        "wins":        len(wins),
        "losses":      len(losses),
        "win_rate":    round(win_rate, 1),
        "total_pnl":   round(total_pnl, 4),
        "biggest_win": round(biggest_win, 4),
        "biggest_loss": round(biggest_loss, 4),
        "recent_trades": recent,
        "equity_curve": equity_curve,
        "active_positions": active_positions,
        "gemini_decisions": gemini,
        "regimes":   regimes,
        "gas_info":  gas_info,
        "tick":      tick,
        "logs":      logs,
        "timestamp": time.strftime("%H:%M:%S"),
        # New v2 architecture metrics
        "sharpe_ratio": sharpe_ratio,
        "sharpe_class": sharpe_class,
        "max_drawdown": max_drawdown,
        "alpha_clusters": cluster_summary[:10],
    }


# ---------------------------------------------------------------------------
# HTML Dashboard (single-page, dark theme, mobile-friendly)
# ---------------------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>CORTEX · Polymarket AI</title>
<style>
/* ══ RESET & BASE ══════════════════════════════════════════════════════ */
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:#06080e;color:#e2e8f0;font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  font-size:14px;overflow-x:hidden;min-height:100vh}
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600&display=swap');
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-thumb{background:rgba(99,102,241,.3);border-radius:10px}
::-webkit-scrollbar-track{background:transparent}

/* ══ HEADER ═════════════════════════════════════════════════════════════ */
.hdr{background:#08090f;
  border-bottom:1px solid rgba(88,166,255,0.18);
  padding:11px 16px;display:flex;align-items:center;justify-content:space-between;
  position:sticky;top:0;z-index:100}
.hdr-left{display:flex;align-items:center;gap:10px}
.hdr h1{font-size:15px;font-weight:800;letter-spacing:1px;
  background:linear-gradient(90deg,#58a6ff 0%,#a371f7 60%,#3fb950 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hdr-badge{padding:2px 8px;border-radius:4px;font-size:9px;font-weight:700;
  letter-spacing:1px;background:rgba(63,185,80,0.12);color:#3fb950;
  border:1px solid rgba(63,185,80,0.25);text-transform:uppercase}
.hdr .right{display:flex;align-items:center;gap:8px;font-size:11px;color:#8b949e}
.dot{width:7px;height:7px;border-radius:50%;background:#3fb950;
  box-shadow:0 0 8px #3fb950;animation:livepulse 2s infinite;flex-shrink:0}
@keyframes livepulse{0%,100%{opacity:1;box-shadow:0 0 8px #3fb950}50%{opacity:.4;box-shadow:0 0 3px #3fb950}}

/* ══ TABS ════════════════════════════════════════════════════════════════ */
.tabs{display:flex;background:#08090f;
  border-bottom:1px solid rgba(88,166,255,0.12);
  overflow-x:auto;scrollbar-width:none;position:sticky;top:40px;z-index:99}
.tabs::-webkit-scrollbar{display:none}
.tab{padding:10px 16px;font-size:12px;color:#6e7681;cursor:pointer;
  white-space:nowrap;border-bottom:2px solid transparent;transition:all .2s;user-select:none}
.tab.active{color:#58a6ff;border-bottom-color:#58a6ff;
  text-shadow:0 0 12px rgba(88,166,255,.5)}
.tab:hover:not(.active){color:#c9d1d9}
.page{display:none;padding:14px;animation:pgfade .25s ease}
.page.active{display:block}
@keyframes pgfade{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}

/* ══ CARDS ═══════════════════════════════════════════════════════════════ */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px;margin-bottom:12px}
.card{background:rgba(20,24,36,0.85);border:1px solid rgba(48,54,61,0.7);
  border-radius:8px;padding:11px;transition:border-color .2s,transform .15s;position:relative;overflow:hidden}
.card::after{content:'';position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(88,166,255,0.12),transparent)}
.card:hover{border-color:rgba(88,166,255,.22);transform:translateY(-1px)}
.card .lbl{font-size:9px;color:#6e7681;text-transform:uppercase;letter-spacing:.7px;margin-bottom:5px}
.card .val{font-size:21px;font-weight:700;line-height:1}
.val.green{color:#3fb950;text-shadow:0 0 12px rgba(63,185,80,.25)}
.val.red{color:#f85149;text-shadow:0 0 12px rgba(248,81,73,.25)}
.val.blue{color:#58a6ff;text-shadow:0 0 12px rgba(88,166,255,.25)}
.val.yellow{color:#d29922}
.val.purple{color:#a371f7;text-shadow:0 0 12px rgba(163,113,247,.25)}

/* ══ SECTION HEADERS ════════════════════════════════════════════════════ */
.sh{font-size:10px;color:#6e7681;text-transform:uppercase;letter-spacing:.7px;
  margin:14px 0 8px;padding-top:12px;border-top:1px solid rgba(30,36,46,.8);
  display:flex;align-items:center;gap:6px}
.sh::after{content:'';flex:1;height:1px;background:rgba(30,36,46,.8)}

/* ══ CHART + PROGRESS ═══════════════════════════════════════════════════ */
.chart-wrap{background:rgba(12,15,24,.6);border:1px solid rgba(48,54,61,.5);
  border-radius:8px;padding:10px;margin-bottom:12px}
canvas{width:100%!important;display:block}
.pb-wrap{margin:6px 0}
.pb-label{display:flex;justify-content:space-between;font-size:10px;color:#6e7681;margin-bottom:3px}
.pb-track{height:6px;background:rgba(33,38,45,.9);border-radius:4px;overflow:hidden}
.pb-fill{height:100%;border-radius:4px;transition:width 1.2s cubic-bezier(.4,0,.2,1);
  background:linear-gradient(90deg,#58a6ff,#a371f7)}
.pb-fill.done{background:linear-gradient(90deg,#3fb950,#58a6ff)}

/* ══ CORTEX CANVAS (homepage) ════════════════════════════════════════════ */
.cortex-wrap{position:relative;border-radius:10px;overflow:hidden;margin-bottom:14px;
  border:1px solid rgba(88,166,255,0.08);
  box-shadow:0 0 60px rgba(88,166,255,.04),0 0 120px rgba(163,113,247,.03)}
#cortex-canvas{display:block;width:100%}
.cx-overlay{position:absolute;bottom:0;left:0;right:0;padding:10px 14px 11px;
  background:linear-gradient(transparent,rgba(6,9,18,0.88));
  display:flex;align-items:flex-end;justify-content:space-between;pointer-events:none}
.cx-status{font-size:10px;color:#3fb950;font-family:'SF Mono',monospace;letter-spacing:.8px}
.cx-pulse{width:5px;height:5px;border-radius:50%;background:#3fb950;
  box-shadow:0 0 8px #3fb950;animation:livepulse 1.6s infinite;display:inline-block;margin-right:5px;vertical-align:middle}
.cx-stats{display:flex;gap:16px}
.cx-s{text-align:right}
.cx-sv{font-size:15px;font-weight:700;background:linear-gradient(135deg,#58a6ff,#a371f7);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.cx-sl{font-size:9px;color:#484f58;text-transform:uppercase;letter-spacing:.5px}
.cx-ticker{position:absolute;top:10px;left:0;right:0;display:flex;justify-content:center;pointer-events:none}
.cx-tick-inner{background:rgba(6,9,18,0.82);border:1px solid rgba(88,166,255,0.18);
  border-radius:20px;padding:4px 14px;font-size:10px;font-family:'SF Mono',monospace;
  color:#58a6ff;max-width:85%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  box-shadow:0 0 20px rgba(88,166,255,.08)}

/* ══ GEMINI DECISIONS TAB ════════════════════════════════════════════════ */
.gm-stats{display:grid;grid-template-columns:repeat(5,1fr);gap:7px;margin-bottom:12px}
.gm-sc{background:rgba(20,24,36,.8);border:1px solid rgba(48,54,61,.6);
  border-radius:7px;padding:8px 6px;text-align:center}
.gm-sc .gsv{font-size:17px;font-weight:700;margin-bottom:1px}
.gm-sc .gsl{font-size:8px;color:#6e7681;text-transform:uppercase;letter-spacing:.5px}

.gm-filters{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}
.gmf{padding:5px 11px;border-radius:20px;font-size:11px;cursor:pointer;
  border:1px solid rgba(48,54,61,.8);color:#6e7681;background:rgba(20,24,36,.5);
  transition:all .2s;user-select:none}
.gmf.active{background:rgba(88,166,255,.12);color:#58a6ff;border-color:rgba(88,166,255,.35)}
.gmf.fa.active{background:rgba(63,185,80,.1);color:#3fb950;border-color:rgba(63,185,80,.35)}
.gmf.fn.active{background:rgba(248,81,73,.1);color:#f85149;border-color:rgba(248,81,73,.35)}
.gmf.fs.active{background:rgba(210,153,34,.1);color:#d29922;border-color:rgba(210,153,34,.35)}

.gm-list{display:flex;flex-direction:column;gap:7px}
.gmc{background:rgba(20,24,36,.75);border-radius:8px;
  border-left:3px solid #30363d;
  border-top:1px solid rgba(48,54,61,.35);border-right:1px solid rgba(48,54,61,.35);
  border-bottom:1px solid rgba(48,54,61,.35);
  overflow:hidden;transition:transform .12s}
.gmc:hover{transform:translateX(2px)}
.gmc.co{border-left-color:#3fb950}.gmc.cn{border-left-color:#f85149}.gmc.cs{border-left-color:#d29922}
.gmh{display:flex;align-items:center;gap:7px;padding:8px 11px 4px;cursor:pointer}
.gmt{font-size:10px;color:#484f58;font-family:'SF Mono',monospace;flex-shrink:0}
.gmbd{padding:2px 6px;border-radius:3px;font-size:9px;font-weight:700;letter-spacing:.5px;
  text-transform:uppercase;flex-shrink:0}
.gmbd.bo{background:rgba(63,185,80,.12);color:#3fb950;border:1px solid rgba(63,185,80,.25)}
.gmbd.bn{background:rgba(248,81,73,.12);color:#f85149;border:1px solid rgba(248,81,73,.25)}
.gmbd.bs{background:rgba(210,153,34,.12);color:#d29922;border:1px solid rgba(210,153,34,.25)}
.gmbd.bt{background:rgba(163,113,247,.12);color:#a371f7;border:1px solid rgba(163,113,247,.25)}
.gmmid{font-size:9px;color:#484f58;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.gmchev{margin-left:auto;color:#484f58;font-size:11px;flex-shrink:0;transition:transform .2s}
.gmchev.open{transform:rotate(180deg)}
.gmq{padding:0 11px 7px;font-size:12px;color:#c9d1d9;line-height:1.45}
.gmbars{padding:0 11px 7px;display:flex;flex-direction:column;gap:4px}
.brow{display:flex;align-items:center;gap:6px;font-size:10px}
.blbl{color:#6e7681;width:22px;flex-shrink:0}
.btrk{flex:1;height:5px;background:rgba(30,36,46,.9);border-radius:3px;overflow:hidden}
.bfll{height:100%;border-radius:3px;transition:width .7s ease}
.bval{width:32px;text-align:right;flex-shrink:0}
.gmmeta{padding:0 11px 8px;display:flex;gap:5px;flex-wrap:wrap}
.gmrbox{padding:0 11px 10px;display:none}
.gmrbox.open{display:block}
.rtext{background:rgba(8,10,18,.7);border:1px solid rgba(48,54,61,.5);border-radius:6px;
  padding:10px;font-size:11px;color:#8b949e;line-height:1.65;
  font-family:'SF Mono',Menlo,monospace;white-space:pre-wrap;max-height:180px;overflow-y:auto}
.rbtn{display:inline-block;cursor:pointer;font-size:10px;color:#6e7681;
  padding:3px 8px;border-radius:4px;border:1px solid rgba(48,54,61,.6);
  background:rgba(20,24,36,.5);margin:0 11px 7px;transition:all .15s}
.rbtn:hover{color:#58a6ff;border-color:rgba(88,166,255,.3)}

/* ══ REGIME ═════════════════════════════════════════════════════════════ */
.regime-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:7px}
.regime-card{background:rgba(20,24,36,.7);border:1px solid rgba(48,54,61,.55);
  border-radius:6px;padding:7px 9px;text-align:center}
.regime-card .asset{font-size:10px;color:#6e7681;margin-bottom:2px}
.regime-card .rg{font-size:11px;font-weight:600}

/* ══ BADGES ═════════════════════════════════════════════════════════════ */
.badge{display:inline-block;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:600}
.badge-green{background:rgba(26,58,26,.85);color:#3fb950}
.badge-red{background:rgba(58,26,26,.85);color:#f85149}
.badge-blue{background:rgba(26,42,58,.85);color:#58a6ff}
.badge-yellow{background:rgba(58,42,10,.85);color:#d29922}
.badge-purple{background:rgba(42,26,58,.85);color:#a371f7}
.badge-gray{background:rgba(33,38,45,.85);color:#8b949e}

/* ══ TABLES ═════════════════════════════════════════════════════════════ */
.tbl-wrap{overflow-x:auto;margin-bottom:12px}
table{width:100%;border-collapse:collapse}
th{background:rgba(28,33,40,.9);color:#6e7681;font-size:10px;text-transform:uppercase;
  padding:8px 9px;text-align:left;white-space:nowrap;letter-spacing:.4px}
td{padding:7px 9px;border-bottom:1px solid rgba(30,36,46,.7);font-size:12px;vertical-align:top}
tr:hover td{background:rgba(22,27,34,.6)}
.pnl-pos{color:#3fb950}.pnl-neg{color:#f85149}

/* ══ LOG ════════════════════════════════════════════════════════════════ */
.log-box{background:rgba(6,9,18,.9);border:1px solid rgba(48,54,61,.6);border-radius:8px;
  padding:10px;font-family:'SF Mono',Menlo,monospace;font-size:11px;
  height:360px;overflow-y:auto;line-height:1.7}
.ll{color:#484f58}
.ll.opp{color:#f0883e}.ll.live{color:#3fb950}.ll.tier{color:#58a6ff;font-weight:600}
.ll.warn{color:#d29922}.ll.err{color:#f85149}.ll.gem{color:#a371f7}.ll.heart{color:#6e7681}

/* ══ RESPONSIVE ═════════════════════════════════════════════════════════ */
@media(max-width:520px){
  .grid{grid-template-columns:repeat(2,1fr)}
  .val{font-size:18px}
  .gm-stats{grid-template-columns:repeat(3,1fr)}
  .cx-stats{gap:10px}
}
</style>
</head>
<body>

<!-- ═══ HEADER ══════════════════════════════════════════════════════════ -->
<div class="hdr">
  <div class="hdr-left">
    <h1>⬡ POLYMARKET CORTEX</h1>
    <span class="hdr-badge" id="mode-badge">LIVE</span>
  </div>
  <div class="right">
    <div class="dot"></div>
    <span id="ts">--:--:--</span>
    <span id="tick" style="color:#484f58;font-size:10px;font-family:'SF Mono',monospace"></span>
  </div>
</div>

<!-- ═══ TABS ════════════════════════════════════════════════════════════ -->
<div class="tabs">
  <div class="tab active" data-tab="brain">⚡ Brain</div>
  <div class="tab" data-tab="gemini">🧠 KI-Decisions</div>
  <div class="tab" data-tab="positions">📊 Positionen</div>
  <div class="tab" data-tab="trades">💰 Trades</div>
  <div class="tab" data-tab="log">📝 Log</div>
</div>

<!-- ════ TAB: BRAIN ═════════════════════════════════════════════════════ -->
<div id="page-brain" class="page active">
  <div class="cortex-wrap">
    <canvas id="cortex-canvas" height="300"></canvas>
    <div class="cx-ticker">
      <div class="cx-tick-inner" id="cx-ticker">NEURAL ENGINE INITIALISIERT — WARTE AUF MARKTDATEN…</div>
    </div>
    <div class="cx-overlay">
      <div class="cx-status"><span class="cx-pulse"></span>CORTEX AKTIV</div>
      <div class="cx-stats">
        <div class="cx-s"><div class="cx-sv" id="cx-analyzed">—</div><div class="cx-sl">Analysen</div></div>
        <div class="cx-s"><div class="cx-sv" id="cx-opp">—</div><div class="cx-sl">Chancen</div></div>
        <div class="cx-s"><div class="cx-sv" id="cx-tick2">—</div><div class="cx-sl">Tick</div></div>
      </div>
    </div>
  </div>

  <div class="grid">
    <div class="card"><div class="lbl">Portfolio <span id="bal-fresh" style="font-size:9px;color:#3fb950"></span></div><div class="val blue" id="total_portfolio">$0.00</div></div>
    <div class="card"><div class="lbl">Freies Cash</div><div class="val blue" id="bankroll">$0.00</div></div>
    <div class="card"><div class="lbl">In Positionen</div><div class="val yellow" id="committed">$0.00</div></div>
    <div class="card"><div class="lbl">Gesamt PnL</div><div class="val" id="total_pnl">$0.00</div></div>
    <div class="card"><div class="lbl">Win Rate</div><div class="val" id="win_rate">0%</div></div>
    <div class="card"><div class="lbl">Trades</div><div class="val" id="total_trades">0</div></div>
    <div class="card"><div class="lbl">Bester Trade</div><div class="val green" id="biggest_win">$0</div></div>
    <div class="card"><div class="lbl">Schlechtester</div><div class="val red" id="biggest_loss">$0</div></div>
  </div>

  <div class="sh">Wachstumsziel</div>
  <div class="chart-wrap">
    <div class="pb-wrap">
      <div class="pb-label"><span>$5.27</span><span id="g1lbl">→ $100</span><span>$100</span></div>
      <div class="pb-track"><div class="pb-fill" id="g1bar" style="width:0%"></div></div>
    </div>
    <div class="pb-wrap" style="margin-top:9px">
      <div class="pb-label"><span>$100</span><span id="g2lbl">→ $1.000</span><span>$1.000</span></div>
      <div class="pb-track"><div class="pb-fill" id="g2bar" style="width:0%"></div></div>
    </div>
    <div class="pb-wrap" style="margin-top:9px">
      <div class="pb-label"><span>$1.000</span><span id="g3lbl">→ $10.000</span><span>$10.000</span></div>
      <div class="pb-track"><div class="pb-fill" id="g3bar" style="width:0%"></div></div>
    </div>
  </div>

  <div class="sh">Equity Kurve</div>
  <div class="chart-wrap"><canvas id="eqChart" height="100"></canvas></div>

  <div class="sh">Markt-Regime</div>
  <div class="regime-grid" id="regime-grid"></div>
</div>

<!-- ════ TAB: KI-DECISIONS ══════════════════════════════════════════════ -->
<div id="page-gemini" class="page">
  <div class="gm-stats">
    <div class="gm-sc"><div class="gsv blue" id="gm-total">0</div><div class="gsl">Gesamt</div></div>
    <div class="gm-sc"><div class="gsv green" id="gm-opp">0</div><div class="gsl">Chancen</div></div>
    <div class="gm-sc"><div class="gsv red" id="gm-no">0</div><div class="gsl">Kein Edge</div></div>
    <div class="gm-sc"><div class="gsv yellow" id="gm-skip">0</div><div class="gsl">Skip</div></div>
    <div class="gm-sc"><div class="gsv purple" id="gm-rate">0%</div><div class="gsl">Acceptance</div></div>
  </div>
  <div class="gm-filters">
    <div class="gmf active" data-gf="all">Alle</div>
    <div class="gmf fa" data-gf="opp">⚡ Opportunities</div>
    <div class="gmf fn" data-gf="no">✗ Kein Edge</div>
    <div class="gmf fs" data-gf="skip">⚠ Skip</div>
  </div>
  <div class="gm-list" id="gm-list">
    <div style="color:#484f58;text-align:center;padding:40px 0;font-size:12px">Warte auf Gemini-Analysen…</div>
  </div>
</div>

<!-- ════ TAB: POSITIONEN ════════════════════════════════════════════════ -->
<div id="page-positions" class="page">
  <div class="tbl-wrap">
    <table>
      <thead><tr><th>Frage / Markt</th><th>Seite</th><th>Größe</th><th>Einstieg</th><th>Restzeit</th></tr></thead>
      <tbody id="pos-body"><tr><td colspan="5" style="color:#484f58;text-align:center;padding:30px">Keine aktiven Positionen</td></tr></tbody>
    </table>
  </div>
</div>

<!-- ════ TAB: TRADES ════════════════════════════════════════════════════ -->
<div id="page-trades" class="page">
  <div class="tbl-wrap">
    <table>
      <thead><tr><th>Zeit</th><th>Frage</th><th>Seite</th><th>Größe</th><th>PnL</th></tr></thead>
      <tbody id="trades-body"><tr><td colspan="5" style="color:#484f58;text-align:center;padding:30px">Noch keine Trades</td></tr></tbody>
    </table>
  </div>
</div>

<!-- ════ TAB: LOG ════════════════════════════════════════════════════════ -->
<div id="page-log" class="page">
  <div class="log-box" id="log-box"></div>
</div>

<script>
'use strict';
/* ══════════════════════════════════════════════════════════════════════
   TABS
   ══════════════════════════════════════════════════════════════════════ */
let activeTab = 'brain';
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    const name = t.dataset.tab;
    document.getElementById('page-' + name)?.classList.add('active');
    t.classList.add('active');
    activeTab = name;
  });
});

/* ══════════════════════════════════════════════════════════════════════
   HELPERS
   ══════════════════════════════════════════════════════════════════════ */
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function pct(v,lo,hi){return Math.min(100,Math.max(0,(v-lo)/(hi-lo)*100)).toFixed(1);}
function sign(v,dp){dp=dp||4;return(v>=0?'+':'')+Number(v).toFixed(dp);}
function badge(text,type){return '<span class="badge badge-'+type+'">'+esc(text)+'</span>';}
function h2r(hex){return[parseInt(hex.slice(1,3),16),parseInt(hex.slice(3,5),16),parseInt(hex.slice(5,7),16)];}

/* ══════════════════════════════════════════════════════════════════════
   EQUITY CHART
   ══════════════════════════════════════════════════════════════════════ */
function drawChart(id, data) {
  const c = document.getElementById(id);
  if (!c) return;
  const ctx = c.getContext('2d');
  const W = c.offsetWidth||300, H = c.height;
  c.width = W;
  ctx.clearRect(0,0,W,H);
  if (!data||data.length<2){
    ctx.fillStyle='#484f58';ctx.font='11px sans-serif';ctx.textAlign='center';
    ctx.fillText('Noch keine Daten',W/2,H/2);return;
  }
  const mn=Math.min(...data),mx=Math.max(...data),r=mx-mn||1,pad=12;
  const xs=data.map((_,i)=>pad+i/(data.length-1)*(W-pad*2));
  const ys=data.map(v=>H-pad-(v-mn)/r*(H-pad*2));
  const isUp=data[data.length-1]>=data[0];
  const lc=isUp?'#3fb950':'#f85149';
  const grad=ctx.createLinearGradient(0,0,0,H);
  grad.addColorStop(0,isUp?'rgba(63,185,80,.18)':'rgba(248,81,73,.18)');
  grad.addColorStop(1,'rgba(0,0,0,0)');
  ctx.beginPath();ctx.moveTo(xs[0],H);
  xs.forEach((x,i)=>ctx.lineTo(x,ys[i]));
  ctx.lineTo(xs[xs.length-1],H);ctx.closePath();
  ctx.fillStyle=grad;ctx.fill();
  ctx.beginPath();ctx.strokeStyle=lc;ctx.lineWidth=2;
  xs.forEach((x,i)=>i===0?ctx.moveTo(x,ys[i]):ctx.lineTo(x,ys[i]));
  ctx.stroke();
  ctx.beginPath();ctx.arc(xs[xs.length-1],ys[ys.length-1],3.5,0,Math.PI*2);
  ctx.fillStyle='#58a6ff';ctx.fill();
}
/* ══════════════════════════════════════════════════════════════════════
   CORTEX — QUANTUM NEURAL ANIMATION
   ══════════════════════════════════════════════════════════════════════ */
const Cortex = (function(){
  var cv,ctx,W,H,fc=0,ready=false;
  var neurons=[],impulses=[],rings=[];
  var scanY=0,scanDir=1,lastFire=0,lastDecTs='';
  var N=155, MAXD=115;

  function rnd(a,b){return a+Math.random()*(b-a);}

  function init(){
    cv=document.getElementById('cortex-canvas');
    if(!cv)return;
    ctx=cv.getContext('2d');
    resize();
    window.addEventListener('resize',resize);
    neurons=[];
    for(var i=0;i<N;i++){
      var z=rnd(0.25,1.0);
      var isHub=(i<18);
      neurons.push({
        x:rnd(0,W),y:rnd(0,H),z:z,
        vx:rnd(-0.22,0.22)*z,vy:rnd(-0.18,0.18)*z,
        r:(isHub?rnd(4,7):rnd(1.5,3.5))*z,
        act:0,col:'#58a6ff',base:'#58a6ff',
        ph:rnd(0,6.28),hub:isHub
      });
    }
    ready=true;
    requestAnimationFrame(frame);
  }

  function resize(){
    W=cv.offsetWidth||360;
    H=parseInt(cv.getAttribute('height'))||300;
    cv.width=W;cv.height=H;
  }

  function hexrgb(h){return[parseInt(h.slice(1,3),16),parseInt(h.slice(3,5),16),parseInt(h.slice(5,7),16)];}

  function cascade(ox,oy,col,strength){
    rings.push({x:ox,y:oy,r:0,col:col,a:0.7});
    rings.push({x:ox,y:oy,r:0,col:col,a:0.35,delay:8});
    neurons.forEach(function(n){
      var dx=n.x-ox,dy=n.y-oy,d=Math.sqrt(dx*dx+dy*dy);
      if(d<MAXD*2.8&&d>5){
        (function(nn,dist){
          setTimeout(function(){
            nn.act=Math.max(nn.act,(1-dist/(MAXD*2.8))*(strength||1));
            nn.col=col;
            for(var k=0;k<2;k++){
              var tn=neurons[Math.floor(Math.random()*neurons.length)];
              impulses.push({x:nn.x,y:nn.y,tx:tn.x,ty:tn.y,t:0,spd:rnd(0.014,0.026),col:col});
            }
          },dist*2.2+rnd(0,80));
        })(n,d);
      }
    });
  }

  function fire(col,strength){
    var src=neurons.filter(function(n){return n.hub;})[Math.floor(Math.random()*18)]||neurons[0];
    src.act=strength||1.0;src.col=col;
    cascade(src.x,src.y,col,strength);
    lastFire=Date.now();
  }

  function drawBg(){
    var bg=ctx.createRadialGradient(W*.5,H*.5,0,W*.5,H*.5,Math.max(W,H)*.75);
    bg.addColorStop(0,'#0b0e1c');bg.addColorStop(.6,'#080c18');bg.addColorStop(1,'#060912');
    ctx.fillStyle=bg;ctx.fillRect(0,0,W,H);
    /* grid */
    ctx.strokeStyle='rgba(88,166,255,0.022)';ctx.lineWidth=.5;
    for(var x=0;x<W;x+=44){ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,H);ctx.stroke();}
    for(var y=0;y<H;y+=44){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(W,y);ctx.stroke();}
    /* scan line */
    var sa=0.35+Math.sin(fc*.025)*.12;
    var sg=ctx.createLinearGradient(0,scanY-7,0,scanY+7);
    sg.addColorStop(0,'rgba(88,166,255,0)');
    sg.addColorStop(.5,'rgba(88,166,255,'+sa+')');
    sg.addColorStop(1,'rgba(88,166,255,0)');
    ctx.fillStyle=sg;ctx.fillRect(0,scanY-7,W,14);
  }

  function drawEdges(){
    for(var i=0;i<neurons.length;i++){
      for(var j=i+1;j<neurons.length;j++){
        var a=neurons[i],b=neurons[j];
        var dx=b.x-a.x,dy=b.y-a.y,d=Math.sqrt(dx*dx+dy*dy);
        if(d>MAXD)continue;
        var prox=1-d/MAXD,act=(a.act+b.act)*.5,dep=(a.z+b.z)*.5;
        if(act>0.05){
          ctx.strokeStyle='rgba(88,166,255,'+(prox*.12+act*.45)+')';
          ctx.lineWidth=.5+act*1.8*dep;
        }else{
          ctx.strokeStyle='rgba(28,34,46,'+(prox*.65*dep)+')';
          ctx.lineWidth=.4;
        }
        ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
      }
    }
  }

  function drawRings(){
    rings=rings.filter(function(r){return r.a>0.01;});
    rings.forEach(function(r){
      if(r.delay&&r.delay-->0)return;
      r.r+=2.2;r.a*=0.93;
      var rgb=hexrgb(r.col);
      ctx.beginPath();ctx.arc(r.x,r.y,r.r,0,Math.PI*2);
      ctx.strokeStyle='rgba('+rgb[0]+','+rgb[1]+','+rgb[2]+','+r.a+')';
      ctx.lineWidth=1.2;ctx.stroke();
    });
  }

  function drawImpulses(){
    impulses=impulses.filter(function(p){return p.t<1;});
    impulses.forEach(function(p){
      p.t=Math.min(p.t+p.spd,1);
      var px=p.x+(p.tx-p.x)*p.t,py=p.y+(p.ty-p.y)*p.t;
      var al=p.t<0.75?0.9:(1-p.t)/0.25;
      ctx.save();ctx.globalAlpha=al;
      ctx.shadowBlur=9;ctx.shadowColor=p.col;
      ctx.fillStyle=p.col;
      ctx.beginPath();ctx.arc(px,py,2.2,0,Math.PI*2);ctx.fill();
      ctx.restore();
    });
  }

  function drawNeurons(){
    var t=fc*.018;
    neurons.forEach(function(n){
      var act=n.act,pulse=1+Math.sin(t+n.ph)*.09;
      var rr=(n.r+act*2.8)*pulse;
      ctx.save();
      if(act>0.05){ctx.shadowBlur=16*act*n.z;ctx.shadowColor=n.col;}
      if(n.hub||act>0.2){
        var rgb=hexrgb(n.col);
        ctx.beginPath();ctx.arc(n.x,n.y,rr+3*act+(n.hub?2.5:0),0,Math.PI*2);
        ctx.strokeStyle='rgba('+rgb[0]+','+rgb[1]+','+rgb[2]+','+(act*.32+(n.hub?.07:0))+')';
        ctx.lineWidth=.8;ctx.stroke();
      }
      ctx.beginPath();ctx.arc(n.x,n.y,rr,0,Math.PI*2);
      if(act>0.08){
        var rgb2=hexrgb(n.col);
        var g=ctx.createRadialGradient(n.x,n.y,0,n.x,n.y,rr);
        g.addColorStop(0,n.col);
        g.addColorStop(1,'rgba('+rgb2[0]+','+rgb2[1]+','+rgb2[2]+',.08)');
        ctx.fillStyle=g;
      }else{ctx.fillStyle=n.z>.6?'#14182a':'#0b0e1a';}
      ctx.globalAlpha=.22+act*.78+n.z*.12;ctx.fill();
      ctx.globalAlpha=(.32+act*.68)*n.z;
      ctx.strokeStyle=act>.05?n.col:(n.z>.55?'#2a3040':'#1a2030');
      ctx.lineWidth=n.hub?1.4:.9;ctx.stroke();
      ctx.restore();
      n.act*=0.977;
      if(n.act<.006){n.col=n.base;n.act=0;}
    });
  }

  function drawIdleFlow(){
    if(Math.random()<.035){
      var a=neurons[Math.floor(Math.random()*N)];
      var b=neurons[Math.floor(Math.random()*N)];
      var dx=b.x-a.x,dy=b.y-a.y;
      if(Math.sqrt(dx*dx+dy*dy)<MAXD)
        impulses.push({x:a.x,y:a.y,tx:b.x,ty:b.y,t:0,spd:rnd(.005,.009),col:'#1a2840'});
    }
  }

  function update(){
    neurons.forEach(function(n){
      n.x+=n.vx;n.y+=n.vy;
      if(n.x<-15)n.x=W+15;if(n.x>W+15)n.x=-15;
      if(n.y<-15)n.y=H+15;if(n.y>H+15)n.y=-15;
    });
    scanY+=.55*scanDir;
    if(scanY>H){scanY=H;scanDir=-1;}
    if(scanY<0){scanY=0;scanDir=1;}
  }

  function frame(){
    fc++;
    drawBg();drawEdges();drawRings();drawImpulses();drawNeurons();drawIdleFlow();update();
    requestAnimationFrame(frame);
  }

  /* idle random sparks */
  setInterval(function(){
    if(!ready||Date.now()-lastFire<3200)return;
    var n=neurons[Math.floor(Math.random()*N)];
    var idleCols=['#1e3356','#1e2a40','#2a1e40','#1e3a2a'];
    n.act=rnd(.15,.4);n.col=idleCols[Math.floor(Math.random()*4)];
    for(var k=0;k<4;k++){
      var m=neurons[Math.floor(Math.random()*N)];
      impulses.push({x:n.x,y:n.y,tx:m.x,ty:m.y,t:0,spd:rnd(.006,.012),col:n.col});
    }
    lastFire=Date.now()-2800;
  },350);

  setTimeout(init,60);

  return {
    trigger:function(dec,q){
      var col=dec==='OPPORTUNITY'?'#3fb950':dec==='NO_EDGE'?'#f85149':'#d29922';
      fire(col,1.0);
      var lbl=dec==='OPPORTUNITY'?'⚡ CHANCE ERKANNT':dec==='NO_EDGE'?'✗ KEIN EDGE':'⚠ ÜBERSPRUNGEN';
      var el=document.getElementById('cx-ticker');
      if(el){el.style.color=col;el.textContent=lbl+' — '+(q||'').substring(0,62);}
    }
  };
})();

/* ══════════════════════════════════════════════════════════════════════
   GEMINI FILTER
   ══════════════════════════════════════════════════════════════════════ */
var gmFilter='all',_lastDecs=null,_lastDecTs='';
document.querySelectorAll('.gmf').forEach(function(b){
  b.addEventListener('click',function(){
    document.querySelectorAll('.gmf').forEach(function(x){x.classList.remove('active');});
    b.classList.add('active');gmFilter=b.dataset.gf;
    if(_lastDecs)renderGmList(_lastDecs);
  });
});

function renderGmStats(ds){
  var tot=ds.length,opp=0,no=0,sk=0;
  ds.forEach(function(d){
    if(d.decision==='OPPORTUNITY')opp++;
    else if(d.decision==='NO_EDGE')no++;
    else sk++;
  });
  var rate=tot>0?Math.round(opp/tot*100):0;
  var el=function(id,v){var e=document.getElementById(id);if(e)e.textContent=v;};
  el('gm-total',tot);el('gm-opp',opp);el('gm-no',no);el('gm-skip',sk);
  el('gm-rate',rate+'%');
  el('cx-analyzed',tot);el('cx-opp',opp);
}

function renderGmList(ds){
  _lastDecs=ds;
  var list=document.getElementById('gm-list');
  if(!list)return;
  var f=ds;
  if(gmFilter==='opp')f=ds.filter(function(d){return d.decision==='OPPORTUNITY';});
  else if(gmFilter==='no')f=ds.filter(function(d){return d.decision==='NO_EDGE';});
  else if(gmFilter==='skip')f=ds.filter(function(d){return d.decision!=='OPPORTUNITY'&&d.decision!=='NO_EDGE';});
  if(!f.length){
    list.innerHTML='<div style="color:#484f58;text-align:center;padding:36px;font-size:12px">Keine Einträge für diesen Filter</div>';
    return;
  }
  list.innerHTML=f.map(function(d,idx){
    var isO=d.decision==='OPPORTUNITY',isN=d.decision==='NO_EDGE';
    var cc=isO?'co':isN?'cn':'cs';
    var bc=isO?'bo':isN?'bn':'bs';
    var bl=isO?'⚡ TRADE':isN?'✗ KEIN EDGE':'⚠ SKIP';
    var prob=Math.round((d.gemini_prob||0)*100);
    var mkt=Math.round((d.market_price||0)*100);
    var conf=Math.round((d.confidence||0)*100);
    var ev=((d.edge_ev||0)*100).toFixed(1);
    var ts=esc((d.ts||'').substring(11,16));
    var q=esc((d.question||d.market_id||'Unbekannte Frage'));
    var mid=esc((d.market_id||'').substring(0,16));
    var reasoning=esc(d.reasoning||'');
    var hasR=!!(d.reasoning&&d.reasoning.length>5);
    var side='';
    if(isO)side=prob>mkt?'<span class="gmbd bt" style="font-size:8px">▲ BUY YES</span>':'<span class="gmbd bn" style="font-size:8px">▼ BUY NO</span>';
    var evColor=(d.edge_ev||0)>=0.02?'green':'red';
    var confColor=conf>=60?'#3fb950':'#d29922';
    var kiColor=prob>=60?'#a371f7':'#d29922';
    return '<div class="gmc '+cc+'" id="gmc'+idx+'">'
      +'<div class="gmh">'
        +'<span class="gmt">'+ts+'</span>'
        +'<span class="gmbd '+bc+'">'+bl+'</span>'
        +side
        +'<span class="gmmid">'+mid+'</span>'
        +'<span class="gmchev" id="gce'+idx+'">▾</span>'
      +'</div>'
      +'<div class="gmq">'+q+'</div>'
      +'<div class="gmbars">'
        +'<div class="brow"><span class="blbl" style="color:#a371f7">KI</span>'
          +'<div class="btrk"><div class="bfll" style="width:'+prob+'%;background:'+kiColor+'"></div></div>'
          +'<span class="bval" style="color:'+kiColor+'">'+prob+'%</span></div>'
        +'<div class="brow"><span class="blbl" style="color:#58a6ff">Mkt</span>'
          +'<div class="btrk"><div class="bfll" style="width:'+mkt+'%;background:#58a6ff"></div></div>'
          +'<span class="bval" style="color:#58a6ff">'+mkt+'%</span></div>'
        +'<div class="brow"><span class="blbl" style="color:#6e7681">Conf</span>'
          +'<div class="btrk"><div class="bfll" style="width:'+conf+'%;background:'+confColor+'"></div></div>'
          +'<span class="bval" style="color:'+confColor+'">'+conf+'%</span></div>'
      +'</div>'
      +'<div class="gmmeta">'
        +badge('EV '+((d.edge_ev||0)>=0?'+':'')+ev+'%',evColor)
        +badge('KI '+prob+'%',prob>=60?'purple':'yellow')
        +badge('Mkt '+mkt+'%','blue')
        +badge('Conf '+conf+'%',conf>=60?'green':'yellow')
      +'</div>'
      +(hasR?'<span class="rbtn" onclick="gmr('+idx+',this)">▸ Begründung</span>'
            +'<div class="gmrbox" id="gmrb'+idx+'"><div class="rtext">'+reasoning+'</div></div>':'')
      +'</div>';
  }).join('');
}

function gmr(idx,btn){
  var box=document.getElementById('gmrb'+idx);
  if(!box)return;
  var op=box.classList.toggle('open');
  btn.textContent=op?'▾ Begründung verbergen':'▸ Begründung';
}

/* ══════════════════════════════════════════════════════════════════════
   MAIN RENDER
   ══════════════════════════════════════════════════════════════════════ */
function set(id,v){var e=document.getElementById(id);if(e)e.textContent=v;}

function render(s) {
  set('ts', s.timestamp);
  if(s.tick){set('tick','tick #'+s.tick);set('cx-tick2','#'+s.tick);}

  var br=s.bankroll||0, tp=s.total_portfolio||br;
  set('total_portfolio','$'+tp.toFixed(2));
  set('bankroll','$'+br.toFixed(2));
  set('committed','$'+(s.committed||0).toFixed(2));
  var fe=document.getElementById('bal-fresh');
  if(fe)fe.textContent=s.balance_fresh?'● LIVE':'○ cached';
  var pe=document.getElementById('total_pnl');
  pe.textContent=(s.total_pnl>=0?'+':'')+'$'+Math.abs(s.total_pnl||0).toFixed(4);
  pe.className='val '+(s.total_pnl>=0?'green':'red');
  set('total_trades',s.total_trades);
  var we=document.getElementById('win_rate');
  we.textContent=(s.win_rate||0).toFixed(1)+'%';
  we.className='val '+((s.win_rate||0)>=55?'green':(s.win_rate||0)>=50?'':'red');
  set('biggest_win','+$'+(s.biggest_win||0).toFixed(4));
  set('biggest_loss','$'+(s.biggest_loss||0).toFixed(4));

  var g1=document.getElementById('g1bar');
  if(g1){g1.style.width=pct(tp,5.27,100)+'%';g1.className='pb-fill'+(tp>=100?' done':'');}
  set('g1lbl',tp<100?'→ $100 ('+pct(tp,5.27,100)+'%)':'✅ ERREICHT');
  var g2=document.getElementById('g2bar');
  if(g2){g2.style.width=pct(tp,100,1000)+'%';g2.className='pb-fill'+(tp>=1000?' done':'');}
  set('g2lbl',tp<1000?'→ $1.000 ('+pct(tp,100,1000)+'%)':'✅ ERREICHT');
  var g3=document.getElementById('g3bar');
  if(g3){g3.style.width=pct(tp,1000,10000)+'%';g3.className='pb-fill'+(tp>=10000?' done':'');}
  set('g3lbl',tp<10000?'→ $10.000 ('+pct(tp,1000,10000)+'%)':'✅ ERREICHT');

  drawChart('eqChart', s.equity_curve);

  var rg=document.getElementById('regime-grid'),regimes=s.regimes||{};
  var rkeys=Object.keys(regimes);
  rg.innerHTML=rkeys.length>0
    ?rkeys.map(function(a){
        var info=regimes[a],rn=(info.regime||'?').replace(/_/g,' '),m=info.kelly_mult||1;
        return '<div class="regime-card"><div class="asset">'+esc(a)+'</div>'
          +'<div class="rg '+(m>=1?'green':m>=.75?'yellow':'red')+'">'+esc(rn)+' x'+m+'</div></div>';
      }).join('')
    :'<div style="color:#484f58;font-size:11px">Keine Daten</div>';

  var gd=s.gemini_decisions||[];
  renderGmStats(gd);
  if(gd.length>0&&gd[0].ts!==_lastDecTs){
    _lastDecTs=gd[0].ts;
    Cortex.trigger(gd[0].decision,(gd[0].question||'').substring(0,62));
  }
  renderGmList(gd);

  var pb=document.getElementById('pos-body'),ap=s.active_positions||[];
  pb.innerHTML=ap.length===0
    ?'<tr><td colspan="5" style="color:#484f58;text-align:center;padding:28px">Keine aktiven Positionen</td></tr>'
    :ap.map(function(p){
        var rem=p.remaining_min!=null?p.remaining_min+'min':'—';
        var sb=p.side==='YES'?badge('YES','green'):badge(p.side||'?','red');
        return '<tr><td><div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:160px;font-size:11px" title="'+esc(p.question)+'">'+esc(p.question||p.market)+'</div>'
          +'<div style="color:#484f58;font-size:9px">'+esc(p.market)+'</div></td>'
          +'<td>'+sb+'</td>'
          +'<td>$'+p.size.toFixed(2)+'</td>'
          +'<td style="font-family:\'SF Mono\',monospace">'+p.entry.toFixed(4)+'</td>'
          +'<td style="color:'+(rem==='—'?'#6e7681':'#58a6ff')+'">'+rem+'</td></tr>';
      }).join('');

  var tb=document.getElementById('trades-body'),rt=s.recent_trades||[];
  tb.innerHTML=rt.length===0
    ?'<tr><td colspan="5" style="color:#484f58;text-align:center;padding:28px">Noch keine Trades</td></tr>'
    :[].concat(rt).reverse().map(function(t){
        var p=t.pnl||0;
        var q=esc((t.question||t.market||'--').substring(0,55));
        return '<tr>'
          +'<td style="white-space:nowrap;font-size:11px">'+esc(t.time||'--')+'</td>'
          +'<td style="max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px" title="'+q+'">'+q+'</td>'
          +'<td>'+(t.side==='YES'?badge('YES','green'):badge(t.side||'?','red'))+'</td>'
          +'<td>$'+(t.size||0).toFixed(2)+'</td>'
          +'<td class="'+(p>0?'pnl-pos':p<0?'pnl-neg':'')+'" style="font-family:\'SF Mono\',monospace">'+sign(p,4)+'</td></tr>';
      }).join('');

  var lb=document.getElementById('log-box');
  if(s.logs&&s.logs.length>0){
    var wb=lb.scrollTop+lb.clientHeight>=lb.scrollHeight-20;
    lb.innerHTML=s.logs.map(function(ln){
      var c='ll';
      if(ln.includes('OPPORTUNITY'))c+=' opp';
      else if(ln.includes('[LIVE]')||ln.includes('[DRY RUN]'))c+=' live';
      else if(ln.includes('TIER CHANGE'))c+=' tier';
      else if(ln.includes('WARNING'))c+=' warn';
      else if(ln.includes('ERROR'))c+=' err';
      else if(ln.includes('GEMINI'))c+=' gem';
      else if(ln.includes('HEARTBEAT'))c+=' heart';
      return '<div class="'+c+'">'+esc(ln)+'</div>';
    }).join('');
    if(wb||activeTab==='log')lb.scrollTop=lb.scrollHeight;
  }
}

/* old NN code removed — Cortex handles animation */

/* ══════════════════════════════════════════════════════════════════════
   POLL
   ══════════════════════════════════════════════════════════════════════ */
async function poll() {
  try {
    const r = await fetch('/api/state');
    if (r.ok) render(await r.json());
  } catch(e) {
    document.getElementById('ts').textContent = '⚠ Verbindung getrennt';
  }
}
poll();
setInterval(poll, 3000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress HTTP access logs

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode())

        elif self.path == "/api/state":
            state = get_dashboard_state()
            data = json.dumps(state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        else:
            self.send_response(404)
            self.end_headers()


def main():
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    import socket
    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
    except Exception:
        local_ip = "YOUR-SERVER-IP"
    print(f"╔══════════════════════════════════════════════════╗")
    print(f"║  Polymarket Bot Dashboard läuft auf Port {PORT}    ║")
    print(f"║  Öffne in Safari/Chrome:                         ║")
    print(f"║  → http://{local_ip}:{PORT}  ║")
    print(f"╚══════════════════════════════════════════════════╝")
    print("Strg+C zum Stoppen")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard gestoppt.")


if __name__ == "__main__":
    main()
