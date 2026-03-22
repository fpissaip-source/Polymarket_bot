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

PORT = 8080

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
    # Return newest first, last 30
    return list(reversed(data[-30:]))


def load_regime() -> dict:
    return _safe_read(REGIME_FILE, {})


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
    }


# ---------------------------------------------------------------------------
# HTML Dashboard (single-page, dark theme, mobile-friendly)
# ---------------------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Polymarket Bot</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px}
a{color:#58a6ff}

/* Header */
.hdr{background:#161b22;border-bottom:1px solid #30363d;padding:14px 18px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10}
.hdr h1{font-size:17px;color:#58a6ff;font-weight:700}
.hdr .right{display:flex;align-items:center;gap:10px;font-size:12px;color:#8b949e}
.dot{width:8px;height:8px;border-radius:50%;background:#3fb950;animation:pulse 2s infinite;flex-shrink:0}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

/* Navigation tabs */
.tabs{display:flex;background:#161b22;border-bottom:1px solid #30363d;overflow-x:auto;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tab{padding:10px 16px;font-size:13px;color:#8b949e;cursor:pointer;white-space:nowrap;border-bottom:2px solid transparent;transition:color .15s}
.tab.active{color:#58a6ff;border-bottom-color:#58a6ff}
.tab:hover{color:#e6edf3}
.page{display:none;padding:14px}
.page.active{display:block}

/* Metric grid */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-bottom:14px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px}
.card .lbl{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px}
.card .val{font-size:22px;font-weight:700}
.val.green{color:#3fb950}.val.red{color:#f85149}.val.blue{color:#58a6ff}.val.yellow{color:#d29922}

/* Section headers */
.sh{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin:14px 0 8px;border-top:1px solid #21262d;padding-top:12px}

/* Charts */
.chart-wrap{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px;margin-bottom:12px}
canvas{width:100%!important}

/* Progress bars */
.pb-wrap{margin:6px 0}
.pb-label{display:flex;justify-content:space-between;font-size:11px;color:#8b949e;margin-bottom:3px}
.pb-track{height:7px;background:#21262d;border-radius:4px;overflow:hidden}
.pb-fill{height:100%;background:linear-gradient(90deg,#58a6ff,#3fb950);border-radius:4px;transition:width .8s ease}

/* Tables */
.tbl-wrap{overflow-x:auto;margin-bottom:12px}
table{width:100%;border-collapse:collapse}
th{background:#21262d;color:#8b949e;font-size:10px;text-transform:uppercase;padding:7px 8px;text-align:left;white-space:nowrap}
td{padding:6px 8px;border-bottom:1px solid #21262d;font-size:12px;vertical-align:top}
tr:hover td{background:#1c2128}
.pnl-pos{color:#3fb950}.pnl-neg{color:#f85149}
.badge{display:inline-block;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:600}
.badge-green{background:#1a3a1a;color:#3fb950}
.badge-red{background:#3a1a1a;color:#f85149}
.badge-blue{background:#1a2a3a;color:#58a6ff}
.badge-yellow{background:#3a2a0a;color:#d29922}
.badge-gray{background:#21262d;color:#8b949e}

/* Neural Network AI tab */
.nn-wrap{background:#0a0d12;border:1px solid #30363d;border-radius:8px;overflow:hidden;margin-bottom:10px;position:relative}
.nn-ticker{padding:7px 12px;font-size:11px;font-family:'SF Mono',Menlo,monospace;border-bottom:1px solid #21262d;display:flex;align-items:center;gap:8px;min-height:30px;background:#0d1117}
.nn-ticker-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;transition:background .4s}
#nn-canvas{display:block;width:100%}
.ev-feed{display:flex;flex-direction:column;gap:5px;max-height:260px;overflow-y:auto;padding-bottom:4px}
.ev-feed::-webkit-scrollbar{width:3px}.ev-feed::-webkit-scrollbar-thumb{background:#30363d;border-radius:2px}
.ev-item{display:flex;align-items:flex-start;gap:8px;padding:7px 10px;background:#161b22;border-radius:6px;border-left:3px solid #30363d;transition:border-color .3s}
.ev-item.opp{border-left-color:#3fb950}.ev-item.no-edge{border-left-color:#f85149}.ev-item.skip{border-left-color:#d29922}.ev-item.trade{border-left-color:#58a6ff}
.ev-time{color:#484f58;flex-shrink:0;font-family:'SF Mono',Menlo,monospace;font-size:10px;padding-top:1px}
.ev-body{flex:1;min-width:0}
.ev-lbl{font-weight:700;font-size:10px;letter-spacing:.6px;text-transform:uppercase;margin-bottom:2px}
.ev-q{color:#8b949e;font-size:11px;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ev-meta{margin-top:3px;display:flex;gap:4px;flex-wrap:wrap}

/* Log */
.log-box{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:10px;font-family:'SF Mono',Menlo,monospace;font-size:11px;height:300px;overflow-y:auto;line-height:1.65}
.ll{color:#484f58}
.ll.opp{color:#f0883e}.ll.live{color:#3fb950}.ll.tier{color:#58a6ff;font-weight:600}
.ll.warn{color:#d29922}.ll.err{color:#f85149}.ll.gem{color:#a371f7}
.ll.heart{color:#8b949e}

/* Regime badges */
.regime-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px}
.regime-card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:8px 10px;text-align:center}
.regime-card .asset{font-size:11px;color:#8b949e;margin-bottom:3px}
.regime-card .rg{font-size:12px;font-weight:600}

@media(max-width:480px){.grid{grid-template-columns:repeat(2,1fr)}.val{font-size:19px}}
</style>
</head>
<body>

<div class="hdr">
  <h1>🤖 Polymarket Bot</h1>
  <div class="right">
    <div class="dot"></div>
    <span id="ts">--:--:--</span>
    <span id="tick" style="color:#484f58"></span>
  </div>
</div>

<div class="tabs">
  <div class="tab active" onclick="showTab('portfolio')">Portfolio</div>
  <div class="tab" onclick="showTab('gemini')">🧠 KI-Reasoning</div>
  <div class="tab" onclick="showTab('positions')">Positionen</div>
  <div class="tab" onclick="showTab('trades')">Trades</div>
  <div class="tab" onclick="showTab('log')">Log</div>
</div>

<!-- ════ TAB: PORTFOLIO ════ -->
<div id="page-portfolio" class="page active">
  <div class="grid">
    <div class="card"><div class="lbl">Portfolio <span id="bal-fresh" style="font-size:9px;color:#3fb950"></span></div><div class="val blue" id="total_portfolio">$0.00</div></div>
    <div class="card"><div class="lbl">Freies Cash</div><div class="val blue" id="bankroll">$0.00</div></div>
    <div class="card"><div class="lbl">In Positionen</div><div class="val yellow" id="committed">$0.00</div></div>
    <div class="card"><div class="lbl">Gesamt PnL</div><div class="val" id="total_pnl">$0.00</div></div>
    <div class="card"><div class="lbl">Win Rate</div><div class="val" id="win_rate">0%</div></div>
    <div class="card"><div class="lbl">Trades</div><div class="val" id="total_trades">0</div></div>
    <div class="card"><div class="lbl">Beste</div><div class="val green" id="biggest_win">$0</div></div>
    <div class="card"><div class="lbl">Schlechteste</div><div class="val red" id="biggest_loss">$0</div></div>
  </div>

  <div class="sh">Wachstumsziel</div>
  <div class="chart-wrap">
    <div class="pb-wrap">
      <div class="pb-label"><span>$5.27</span><span id="g1lbl">→ $100</span><span>$100</span></div>
      <div class="pb-track"><div class="pb-fill" id="g1bar" style="width:0%"></div></div>
    </div>
    <div class="pb-wrap" style="margin-top:10px">
      <div class="pb-label"><span>$100</span><span id="g2lbl">→ $1.000</span><span>$1.000</span></div>
      <div class="pb-track"><div class="pb-fill" id="g2bar" style="width:0%"></div></div>
    </div>
    <div class="pb-wrap" style="margin-top:10px">
      <div class="pb-label"><span>$1.000</span><span id="g3lbl">→ $10.000</span><span>$10.000</span></div>
      <div class="pb-track"><div class="pb-fill" id="g3bar" style="width:0%"></div></div>
    </div>
  </div>

  <div class="sh">Equity Kurve</div>
  <div class="chart-wrap"><canvas id="eqChart" height="110"></canvas></div>

  <div class="sh">Markt-Regime</div>
  <div class="regime-grid" id="regime-grid"></div>
</div>

<!-- ════ TAB: KI-REASONING (Neural Net) ════ -->
<div id="page-gemini" class="page">
  <div class="nn-wrap">
    <div class="nn-ticker">
      <div class="nn-ticker-dot" id="nn-dot" style="background:#484f58"></div>
      <span id="nn-ticker-txt" style="color:#484f58;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">Warte auf Marktdaten…</span>
    </div>
    <canvas id="nn-canvas" height="230"></canvas>
  </div>
  <div id="ev-feed" class="ev-feed">
    <div style="color:#484f58;text-align:center;padding:24px;font-size:12px">Noch keine Ereignisse</div>
  </div>
</div>

<!-- ════ TAB: AKTIVE POSITIONEN ════ -->
<div id="page-positions" class="page">
  <div class="tbl-wrap">
    <table>
      <thead><tr><th>Markt</th><th>Seite</th><th>Größe</th><th>Einstieg</th><th>Restlaufzeit</th></tr></thead>
      <tbody id="pos-body"><tr><td colspan="5" style="color:#8b949e;text-align:center;padding:30px">Keine aktiven Positionen</td></tr></tbody>
    </table>
  </div>
</div>

<!-- ════ TAB: TRADES ════ -->
<div id="page-trades" class="page">
  <div class="tbl-wrap">
    <table>
      <thead><tr><th>Zeit</th><th>Markt</th><th>Seite</th><th>Größe</th><th>PnL</th></tr></thead>
      <tbody id="trades-body"><tr><td colspan="5" style="color:#8b949e;text-align:center;padding:30px">Noch keine Trades</td></tr></tbody>
    </table>
  </div>
</div>

<!-- ════ TAB: LOG ════ -->
<div id="page-log" class="page">
  <div class="log-box" id="log-box"></div>
</div>

<script>
// ── Tab navigation ─────────────────────────────────────────────────────────
let activeTab = 'portfolio';
function showTab(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  document.querySelectorAll('.tab').forEach(t => {
    if (t.textContent.trim().includes(name === 'gemini' ? 'KI' : name === 'portfolio' ? 'Portfolio' : name === 'positions' ? 'Pos' : name === 'trades' ? 'Trade' : 'Log'))
      t.classList.add('active');
  });
  activeTab = name;
}
// Simpler tab detection via data attribute
document.querySelectorAll('.tab').forEach((t, i) => {
  const names = ['portfolio','gemini','positions','trades','log'];
  t.dataset.tab = names[i];
  t.onclick = () => {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.getElementById('page-' + names[i]).classList.add('active');
    t.classList.add('active');
    activeTab = names[i];
  };
});

// ── Tiny canvas chart ──────────────────────────────────────────────────────
function drawChart(id, data) {
  const c = document.getElementById(id);
  if (!c) return;
  const ctx = c.getContext('2d');
  const W = c.offsetWidth || 300, H = c.height;
  c.width = W;
  ctx.clearRect(0, 0, W, H);
  if (!data || data.length < 2) {
    ctx.fillStyle = '#484f58'; ctx.font = '12px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('Noch keine Daten', W/2, H/2); return;
  }
  const mn = Math.min(...data), mx = Math.max(...data);
  const r = mx - mn || 1, pad = 14;
  const xs = data.map((_,i) => pad + i/(data.length-1)*(W-pad*2));
  const ys = data.map(v => H - pad - (v-mn)/r*(H-pad*2));
  const grad = ctx.createLinearGradient(0,0,0,H);
  grad.addColorStop(0,'rgba(88,166,255,.25)'); grad.addColorStop(1,'rgba(88,166,255,0)');
  ctx.beginPath(); ctx.moveTo(xs[0], H);
  xs.forEach((x,i) => ctx.lineTo(x, ys[i]));
  ctx.lineTo(xs[xs.length-1], H); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
  ctx.beginPath();
  ctx.strokeStyle = data[data.length-1] >= data[0] ? '#3fb950' : '#f85149';
  ctx.lineWidth = 2;
  xs.forEach((x,i) => i===0 ? ctx.moveTo(x,ys[i]) : ctx.lineTo(x,ys[i]));
  ctx.stroke();
  ctx.beginPath(); ctx.arc(xs[xs.length-1], ys[ys.length-1], 3, 0, Math.PI*2);
  ctx.fillStyle = '#58a6ff'; ctx.fill();
}

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function pct(v, lo, hi) { return Math.min(100, Math.max(0, (v-lo)/(hi-lo)*100)).toFixed(1); }
function sign(v, dp=4) { return (v>=0?'+':'')+v.toFixed(dp); }
function badge(text, type) { return `<span class="badge badge-${type}">${esc(text)}</span>`; }

// ── Render ─────────────────────────────────────────────────────────────────
function render(s) {
  // Header
  document.getElementById('ts').textContent = s.timestamp;
  if (s.tick) document.getElementById('tick').textContent = 'tick #' + s.tick;

  // Portfolio metrics
  const br = s.bankroll;
  const portfolio = s.total_portfolio || br;
  document.getElementById('total_portfolio').textContent = '$' + portfolio.toFixed(2);
  document.getElementById('bankroll').textContent = '$' + br.toFixed(2);
  document.getElementById('committed').textContent = '$' + (s.committed || 0).toFixed(2);
  const freshEl = document.getElementById('bal-fresh');
  if (freshEl) freshEl.textContent = s.balance_fresh ? '● LIVE' : '○ cached';
  const pnlEl = document.getElementById('total_pnl');
  pnlEl.textContent = (s.total_pnl>=0?'+':'')+'$'+s.total_pnl.toFixed(4);
  pnlEl.className = 'val ' + (s.total_pnl>=0?'green':'red');
  document.getElementById('total_trades').textContent = s.total_trades;
  const wrEl = document.getElementById('win_rate');
  wrEl.textContent = s.win_rate.toFixed(1)+'%';
  wrEl.className = 'val ' + (s.win_rate>=55?'green':s.win_rate>=50?'':'red');
  document.getElementById('biggest_win').textContent = '+$'+s.biggest_win.toFixed(4);
  document.getElementById('biggest_loss').textContent = '$'+s.biggest_loss.toFixed(4);

  // Goal bars — use total portfolio (cash + positions)
  const tp = portfolio;
  document.getElementById('g1bar').style.width = pct(tp, 5.27, 100)+'%';
  document.getElementById('g1lbl').textContent = tp<100 ? '→ $100 ('+pct(tp,5.27,100)+'%)' : '✅ ERREICHT';
  document.getElementById('g2bar').style.width = pct(tp, 100, 1000)+'%';
  document.getElementById('g2lbl').textContent = tp<1000 ? '→ $1.000 ('+pct(tp,100,1000)+'%)' : '✅ ERREICHT';
  document.getElementById('g3bar').style.width = pct(tp, 1000, 10000)+'%';
  document.getElementById('g3lbl').textContent = tp<10000 ? '→ $10.000 ('+pct(tp,1000,10000)+'%)' : '✅ ERREICHT';

  // Equity curve
  drawChart('eqChart', s.equity_curve);

  // Regime grid
  const rg = document.getElementById('regime-grid');
  const regimes = s.regimes || {};
  if (Object.keys(regimes).length > 0) {
    rg.innerHTML = Object.entries(regimes).map(([asset, info]) => {
      const rname = (info.regime||'?').replace('_',' ');
      const mult = info.kelly_mult || 1;
      const cls = mult >= 1 ? 'green' : mult >= 0.75 ? 'yellow' : 'red';
      return `<div class="regime-card"><div class="asset">${esc(asset)}</div>
        <div class="rg ${cls}">${esc(rname)} ×${mult}</div></div>`;
    }).join('');
  } else {
    rg.innerHTML = '<div style="color:#484f58;font-size:12px">Noch keine Regime-Daten</div>';
  }

  // ── Neural Network update ─────────────────────────────────────────────────
  if (typeof nnUpdate === 'function') nnUpdate(s.gemini_decisions || []);
  if (typeof nnEvFeed === 'function') nnEvFeed(s.gemini_decisions || []);

  // ── Active positions ─────────────────────────────────────────────────────
  const pb = document.getElementById('pos-body');
  const ap = s.active_positions || [];
  if (ap.length === 0) {
    pb.innerHTML = '<tr><td colspan="5" style="color:#484f58;text-align:center;padding:30px">Keine aktiven Positionen</td></tr>';
  } else {
    pb.innerHTML = ap.map(p => {
      const rem = p.remaining_min != null ? p.remaining_min + 'min' : '—';
      const sideBadge = p.side === 'YES' ? badge('YES','green') : p.side === 'NO' ? badge('NO','red') : badge(p.side,'gray');
      return `<tr>
        <td style="max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(p.question||p.market)}">${esc(p.market)}</td>
        <td>${sideBadge}</td>
        <td>$${p.size.toFixed(2)}</td>
        <td>${p.entry.toFixed(4)}</td>
        <td style="color:${rem==='—'?'#8b949e':'#58a6ff'}">${rem}</td>
      </tr>`;
    }).join('');
  }

  // ── Trades table ─────────────────────────────────────────────────────────
  const tb = document.getElementById('trades-body');
  const rt = s.recent_trades || [];
  if (rt.length === 0) {
    tb.innerHTML = '<tr><td colspan="5" style="color:#484f58;text-align:center;padding:30px">Noch keine Trades</td></tr>';
  } else {
    tb.innerHTML = [...rt].reverse().map(t => {
      const p = t.pnl || 0;
      return `<tr>
        <td style="white-space:nowrap">${esc(t.time||'--')}</td>
        <td style="max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(t.market||'--')}</td>
        <td>${t.side==='YES'?badge('YES','green'):badge(t.side||'?','red')}</td>
        <td>$${(t.size||0).toFixed(2)}</td>
        <td class="${p>0?'pnl-pos':p<0?'pnl-neg':''}">${sign(p,4)}</td>
      </tr>`;
    }).join('');
  }

  // ── Log ──────────────────────────────────────────────────────────────────
  const lb = document.getElementById('log-box');
  if (s.logs && s.logs.length > 0) {
    const wasBottom = lb.scrollTop + lb.clientHeight >= lb.scrollHeight - 20;
    lb.innerHTML = s.logs.map(line => {
      let cls = 'll';
      if (line.includes('OPPORTUNITY')) cls += ' opp';
      else if (line.includes('[LIVE]') || line.includes('[DRY RUN]')) cls += ' live';
      else if (line.includes('TIER CHANGE')) cls += ' tier';
      else if (line.includes('[WARNING]') || line.includes('WARNING')) cls += ' warn';
      else if (line.includes('[ERROR]') || line.includes('ERROR')) cls += ' err';
      else if (line.includes('[GEMINI]') || line.includes('GEMINI')) cls += ' gem';
      else if (line.includes('[HEARTBEAT]')) cls += ' heart';
      return `<div class="${cls}">${esc(line)}</div>`;
    }).join('');
    if (wasBottom || activeTab === 'log') lb.scrollTop = lb.scrollHeight;
  }
}

// ── Neural Network Visualization ─────────────────────────────────────────
var _nnLayers = [5, 6, 5, 3];
var _nnLabIn  = ['Preis','Vol','KI','Flow','Zeit'];
var _nnLabOut = ['BUY YES','BUY NO','ABLEHNEN'];
var _nnOutClr = ['#3fb950','#58a6ff','#d29922'];
var _nnNodes=[], _nnEdges=[], _nnActs=[], _nnParts=[];
var _nnCtx=null, _nnCV=null, _nnW=0, _nnH=0;
var _nnReady=false, _nnLastTs='', _nnRaf=null;

function _nnBuild(){
  _nnW = _nnCV.offsetWidth||360;
  _nnH = parseInt(_nnCV.getAttribute('height'))||230;
  _nnCV.width=_nnW; _nnCV.height=_nnH;
  var px=58, py=26; _nnNodes=[]; _nnEdges=[]; _nnActs=[];
  var total=_nnLayers.reduce(function(a,b){return a+b;},0);
  for(var f=0;f<total;f++) _nnActs.push(0);
  var flat=0;
  for(var l=0;l<_nnLayers.length;l++){
    var n=_nnLayers[l];
    var x=px+(l/(_nnLayers.length-1))*(_nnW-px*2);
    for(var i=0;i<n;i++){
      var y=py+(n>1?i/(n-1):0.5)*(_nnH-py*2);
      _nnNodes.push({x:x,y:y,l:l,i:i,f:flat++});
    }
  }
  var base=0;
  for(var l=0;l<_nnLayers.length-1;l++){
    for(var i=0;i<_nnLayers[l];i++){
      for(var j=0;j<_nnLayers[l+1];j++){
        _nnEdges.push([base+i, base+_nnLayers[l]+j]);
      }
    }
    base+=_nnLayers[l];
  }
}

function _nnFire(color, outIdx){
  var DL=300;
  for(var l=0;l<_nnLayers.length;l++){
    (function(layer){
      setTimeout(function(){
        var base=0;
        for(var ll=0;ll<layer;ll++) base+=_nnLayers[ll];
        for(var i=0;i<_nnLayers[layer];i++){
          if(layer===_nnLayers.length-1 && i!==outIdx) continue;
          var fi=base+i; _nnActs[fi]=1.0;
          if(layer<_nnLayers.length-1){
            var fn=_nnNodes[fi], nb=base+_nnLayers[layer];
            for(var j=0;j<_nnLayers[layer+1];j++){
              if(layer===_nnLayers.length-2 && j!==outIdx) continue;
              var tn=_nnNodes[nb+j];
              _nnParts.push({ix:fn.x,iy:fn.y,tx:tn.x,ty:tn.y,t:0,
                s:0.014+Math.random()*0.008,c:color});
            }
          }
        }
      }, layer*DL);
    })(l);
  }
}

function _nnFrame(){
  if(!_nnCtx) return;
  var c=_nnCtx; c.clearRect(0,0,_nnW,_nnH);

  // Background grid fade
  c.fillStyle='rgba(10,13,18,0.3)'; c.fillRect(0,0,_nnW,_nnH);

  // Edges
  _nnEdges.forEach(function(e){
    var a=_nnNodes[e[0]], b=_nnNodes[e[1]];
    var act=(_nnActs[e[0]]+_nnActs[e[1]])*0.5;
    c.beginPath(); c.moveTo(a.x,a.y); c.lineTo(b.x,b.y);
    if(act>0.05){
      c.strokeStyle='rgba(88,166,255,'+(0.07+act*0.28)+')';
      c.lineWidth=0.8+act*0.8;
    } else {
      c.strokeStyle='rgba(33,38,45,0.55)'; c.lineWidth=0.5;
    }
    c.stroke();
  });

  // Particles
  var keep=[];
  _nnParts.forEach(function(p){
    p.t=Math.min(p.t+p.s,1);
    if(p.t>=1) return;
    keep.push(p);
    var px2=p.ix+(p.tx-p.ix)*p.t, py2=p.iy+(p.ty-p.iy)*p.t;
    var alpha=p.t<0.8?1:(1-p.t)/0.2;
    c.save();
    c.globalAlpha=alpha;
    c.shadowBlur=8; c.shadowColor=p.c;
    c.fillStyle=p.c;
    c.beginPath(); c.arc(px2,py2,2.8,0,Math.PI*2); c.fill();
    c.restore();
  });
  _nnParts=keep;

  // Idle dim particles
  if(Math.random()<0.05 && _nnNodes.length>0){
    var l2=Math.floor(Math.random()*(_nnLayers.length-1));
    var b2=0; for(var ll=0;ll<l2;ll++) b2+=_nnLayers[ll];
    var fi2=b2+Math.floor(Math.random()*_nnLayers[l2]);
    var nb2=b2+_nnLayers[l2];
    var ti2=nb2+Math.floor(Math.random()*_nnLayers[l2+1]);
    if(_nnNodes[fi2]&&_nnNodes[ti2]){
      _nnParts.push({ix:_nnNodes[fi2].x,iy:_nnNodes[fi2].y,
        tx:_nnNodes[ti2].x,ty:_nnNodes[ti2].y,t:0,s:0.006,c:'#30363d'});
    }
  }

  // Nodes
  _nnNodes.forEach(function(n){
    var act=_nnActs[n.f];
    var r=n.l===0||n.l===_nnLayers.length-1?9:6;
    var baseClr=n.l===_nnLayers.length-1?(_nnOutClr[n.i]||'#58a6ff'):'#58a6ff';
    c.save();
    if(act>0.05){ c.shadowBlur=20*act; c.shadowColor=baseClr; }
    // Outer ring (activated)
    if(act>0.3){
      c.beginPath(); c.arc(n.x,n.y,r+4,0,Math.PI*2);
      c.strokeStyle=baseClr; c.globalAlpha=act*0.3; c.lineWidth=1; c.stroke();
    }
    // Fill
    c.beginPath(); c.arc(n.x,n.y,r,0,Math.PI*2);
    c.fillStyle=act>0.08?baseClr:'#161b22';
    c.globalAlpha=0.3+act*0.7; c.fill();
    // Border
    c.globalAlpha=1;
    c.strokeStyle=act>0.08?baseClr:'#30363d';
    c.lineWidth=1.5; c.stroke();
    c.restore();
    // Labels
    c.font='9px -apple-system,sans-serif';
    if(n.l===0){
      c.fillStyle=act>0.1?'#c9d1d9':'#484f58';
      c.textAlign='right'; c.fillText(_nnLabIn[n.i]||'',n.x-12,n.y+3);
    }
    if(n.l===_nnLayers.length-1){
      c.fillStyle=act>0.1?_nnOutClr[n.i]:'#484f58';
      c.textAlign='left'; c.fillText(_nnLabOut[n.i]||'',n.x+12,n.y+3);
    }
    _nnActs[n.f]*=0.972;
  });

  _nnRaf=requestAnimationFrame(_nnFrame);
}

function _nnInit(){
  _nnCV=document.getElementById('nn-canvas');
  if(!_nnCV) return;
  _nnCtx=_nnCV.getContext('2d');
  _nnBuild();
  window.addEventListener('resize',function(){ if(_nnReady) _nnBuild(); });
  _nnRaf=requestAnimationFrame(_nnFrame);
  _nnReady=true;
}

function nnUpdate(decisions){
  if(!_nnReady) _nnInit();
  if(!decisions||!decisions.length) return;
  var latest=decisions[0];
  if(latest.ts===_nnLastTs) return;
  _nnLastTs=latest.ts;
  var d=latest.decision, color, outIdx, txt, tclr;
  if(d==='OPPORTUNITY'){
    var side=(latest.gemini_prob||0.5)>(latest.market_price||0.5)?0:1;
    color='#3fb950'; outIdx=side;
    var sideStr=side===0?'BUY YES ↑':'BUY NO ↑';
    txt='⚡ '+sideStr+' · '+(latest.question||'').substring(0,58);
    tclr='#3fb950';
  } else if(d==='NO_EDGE'){
    color='#f85149'; outIdx=2;
    var evp=((latest.edge_ev||0)*100).toFixed(1);
    txt='✗ KEIN EDGE (EV '+evp+'%) · '+(latest.question||'').substring(0,50);
    tclr='#f85149';
  } else {
    color='#d29922'; outIdx=2;
    var confp=Math.round((latest.confidence||0)*100);
    txt='⚠ SKIP · KI-Konfidenz '+confp+'% < 75% · '+(latest.question||'').substring(0,45);
    tclr='#d29922';
  }
  _nnFire(color,outIdx);
  _nnLastFire=Date.now();
  var dot=document.getElementById('nn-dot'), ttxt=document.getElementById('nn-ticker-txt');
  if(dot) dot.style.background=tclr;
  if(ttxt){ ttxt.style.color=tclr; ttxt.textContent=txt; }
}

function nnEvFeed(decisions){
  var feed=document.getElementById('ev-feed');
  if(!feed) return;
  if(!decisions||!decisions.length){
    feed.innerHTML='<div style="color:#484f58;text-align:center;padding:24px;font-size:12px">Noch keine Ereignisse</div>';
    return;
  }
  feed.innerHTML=decisions.slice(0,12).map(function(d){
    var isOpp=d.decision==='OPPORTUNITY';
    var isSkip=d.decision==='SKIPPED_LOW_CONF'||d.decision==='LOW_CONF';
    var cls=isOpp?'opp':isSkip?'skip':'no-edge';
    var lbl=isOpp?'⚡ OPPORTUNITY':isSkip?'⚠ SKIPPED':'✗ KEIN EDGE';
    var lclr=isOpp?'#3fb950':isSkip?'#d29922':'#f85149';
    var prob=Math.round((d.gemini_prob||0)*100);
    var mkt=Math.round((d.market_price||0)*100);
    var conf=Math.round((d.confidence||0)*100);
    var ev=((d.edge_ev||0)*100).toFixed(1);
    var q=esc((d.question||d.market_id||'').substring(0,72));
    var sideHint='';
    if(isOpp){
      sideHint=prob>mkt?badge('BUY YES','green'):badge('BUY NO','red');
    }
    return '<div class="ev-item '+cls+'">'
      +'<div class="ev-time">'+esc((d.ts||'').substring(11,16))+'</div>'
      +'<div class="ev-body">'
        +'<div class="ev-lbl" style="color:'+lclr+'">'+lbl+'</div>'
        +'<div class="ev-q">'+q+'</div>'
        +'<div class="ev-meta">'
          +(isOpp?sideHint:'')
          +badge('KI '+prob+'%',isOpp&&prob>60?'green':prob<40?'red':'yellow')
          +badge('Mkt '+mkt+'%','blue')
          +badge('Conf '+conf+'%',conf>=75?'green':'yellow')
          +badge('EV '+(d.edge_ev>=0?'+':'')+ev+'%',d.edge_ev>=0.04?'green':'red')
        +'</div>'
      +'</div>'
      +'</div>';
  }).join('');
}

// Auto-start NN on page load (runs in background loop even on other tabs)
setTimeout(_nnInit, 80);

// Idle loop: fire random neuron pattern every 4s when no real trade is active
var _nnIdleColors = ['#58a6ff','#3fb950','#d29922','#58a6ff','#a371f7'];
var _nnLastFire = 0;
setInterval(function(){
  if(!_nnReady) return;
  var now = Date.now();
  // Only idle-fire if no real signal in last 6s
  if(now - _nnLastFire < 6000) return;
  var outIdx = Math.floor(Math.random()*3);
  var clr = _nnIdleColors[Math.floor(Math.random()*_nnIdleColors.length)];
  _nnFire(clr, outIdx);
  _nnLastFire = now;
}, 4000);

// ── Poll every 3 seconds ──────────────────────────────────────────────────
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
