"""
Polymarket Bot - Live Dashboard Server
=======================================
Einfacher HTTP-Server der per Browser (Safari/Chrome) erreichbar ist.
Zeigt Echtzeit-Daten des laufenden Bots.

Start: python3 dashboard/server.py
Aufruf: http://<SERVER-IP>:8080
"""

import json
import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

DASHBOARD_DIR = Path(__file__).parent
BOT_ROOT = DASHBOARD_DIR.parent
STATE_FILE = BOT_ROOT / "bankroll_state.json"
LOG_FILE = BOT_ROOT / "bot.log"
TRADES_FILE = BOT_ROOT / "trades.json"

PORT = 8080


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_bankroll() -> float:
    try:
        data = json.loads(STATE_FILE.read_text())
        return float(data.get("bankroll", 5.27))
    except Exception:
        return 5.27


def load_trades() -> list:
    try:
        if TRADES_FILE.exists():
            return json.loads(TRADES_FILE.read_text())
        return []
    except Exception:
        return []


def load_recent_logs(n: int = 60) -> list[str]:
    """Read last n lines from bot.log."""
    try:
        if not LOG_FILE.exists():
            return ["Keine Log-Datei gefunden. Starte den Bot mit: python3 main.py"]
        lines = LOG_FILE.read_text().splitlines()
        return lines[-n:]
    except Exception:
        return []


def get_dashboard_state() -> dict:
    bankroll = load_bankroll()
    trades = load_trades()
    logs = load_recent_logs(80)

    # Compute stats from trades
    total_trades = len(trades)
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) < 0]
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    win_rate = len(wins) / total_trades * 100 if total_trades > 0 else 0.0
    biggest_win = max((t.get("pnl", 0) for t in trades), default=0)
    biggest_loss = min((t.get("pnl", 0) for t in trades), default=0)

    # Recent 20 trades for chart
    recent = trades[-20:] if len(trades) >= 20 else trades
    equity_curve = []
    running = 5.27
    for t in trades:
        running += t.get("pnl", 0)
        equity_curve.append(round(running, 4))

    return {
        "bankroll": round(bankroll, 2),
        "total_trades": total_trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 4),
        "biggest_win": round(biggest_win, 4),
        "biggest_loss": round(biggest_loss, 4),
        "recent_trades": recent,
        "equity_curve": equity_curve,
        "logs": logs,
        "timestamp": time.strftime("%H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Bot Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }
  .header { background: #161b22; border-bottom: 1px solid #30363d; padding: 16px 20px; display: flex; align-items: center; justify-content: space-between; }
  .header h1 { font-size: 18px; color: #58a6ff; }
  .header .status { display: flex; align-items: center; gap: 8px; font-size: 12px; color: #8b949e; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #3fb950; animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; padding: 16px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; }
  .card .label { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
  .card .value { font-size: 24px; font-weight: 700; color: #e6edf3; }
  .card .value.green { color: #3fb950; }
  .card .value.red { color: #f85149; }
  .card .value.blue { color: #58a6ff; }
  .section { padding: 0 16px 16px; }
  .section h2 { font-size: 13px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 10px; border-top: 1px solid #21262d; padding-top: 14px; }
  .chart-wrap { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px; margin-bottom: 14px; }
  canvas { width: 100% !important; }
  .trades-table { width: 100%; border-collapse: collapse; }
  .trades-table th { background: #21262d; color: #8b949e; font-size: 11px; text-transform: uppercase; padding: 8px 10px; text-align: left; }
  .trades-table td { padding: 7px 10px; border-bottom: 1px solid #21262d; font-size: 12px; }
  .trades-table tr:hover td { background: #1c2128; }
  .pnl-pos { color: #3fb950; }
  .pnl-neg { color: #f85149; }
  .log-box { background: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 12px; font-family: 'SF Mono', Menlo, monospace; font-size: 11px; height: 260px; overflow-y: auto; line-height: 1.6; }
  .log-line { color: #8b949e; }
  .log-line.opportunity { color: #f0883e; }
  .log-line.live { color: #3fb950; }
  .log-line.tier { color: #58a6ff; font-weight: 600; }
  .log-line.warning { color: #d29922; }
  .log-line.error { color: #f85149; }
  .goal-bar-wrap { margin: 8px 0; }
  .goal-label { display: flex; justify-content: space-between; font-size: 11px; color: #8b949e; margin-bottom: 4px; }
  .goal-bar { height: 8px; background: #21262d; border-radius: 4px; overflow: hidden; }
  .goal-fill { height: 100%; background: linear-gradient(90deg, #58a6ff, #3fb950); border-radius: 4px; transition: width 1s ease; }
  @media (max-width: 600px) { .grid { grid-template-columns: repeat(2, 1fr); } }
</style>
</head>
<body>
<div class="header">
  <h1>Polymarket Bot</h1>
  <div class="status"><div class="dot"></div><span id="ts">--:--:--</span></div>
</div>

<div class="grid">
  <div class="card">
    <div class="label">Kontostand</div>
    <div class="value blue" id="bankroll">$0.00</div>
  </div>
  <div class="card">
    <div class="label">Gesamt PnL</div>
    <div class="value" id="total_pnl">$0.0000</div>
  </div>
  <div class="card">
    <div class="label">Trades</div>
    <div class="value" id="total_trades">0</div>
  </div>
  <div class="card">
    <div class="label">Win Rate</div>
    <div class="value" id="win_rate">0%</div>
  </div>
  <div class="card">
    <div class="label">Beste Trade</div>
    <div class="value green" id="biggest_win">$0</div>
  </div>
  <div class="card">
    <div class="label">Schlechtester</div>
    <div class="value red" id="biggest_loss">$0</div>
  </div>
</div>

<div class="section">
  <h2>Wachstumsziel</h2>
  <div class="chart-wrap">
    <div class="goal-bar-wrap">
      <div class="goal-label"><span>$5.27</span><span id="goal1_label">Ziel: $100</span><span>$100</span></div>
      <div class="goal-bar"><div class="goal-fill" id="goal1_bar" style="width:0%"></div></div>
    </div>
    <div class="goal-bar-wrap" style="margin-top:10px">
      <div class="goal-label"><span>$100</span><span id="goal2_label">Ziel: $1.000</span><span>$1.000</span></div>
      <div class="goal-bar"><div class="goal-fill" id="goal2_bar" style="width:0%"></div></div>
    </div>
    <div class="goal-bar-wrap" style="margin-top:10px">
      <div class="goal-label"><span>$1.000</span><span id="goal3_label">Ziel: $10.000</span><span>$10.000</span></div>
      <div class="goal-bar"><div class="goal-fill" id="goal3_bar" style="width:0%"></div></div>
    </div>
  </div>
</div>

<div class="section">
  <h2>Equity Kurve</h2>
  <div class="chart-wrap"><canvas id="equityChart" height="120"></canvas></div>
</div>

<div class="section">
  <h2>Letzte Trades</h2>
  <div style="overflow-x:auto">
  <table class="trades-table">
    <thead><tr><th>Zeit</th><th>Markt</th><th>Seite</th><th>Größe</th><th>PnL</th></tr></thead>
    <tbody id="trades_body"><tr><td colspan="5" style="color:#8b949e;text-align:center;padding:20px">Noch keine Trades</td></tr></tbody>
  </table>
  </div>
</div>

<div class="section">
  <h2>Live Bot-Log</h2>
  <div class="log-box" id="log_box"></div>
</div>

<script>
// ── Mini chart library (canvas) ──────────────────────────────────────────
function drawChart(canvasId, data) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.offsetWidth || 300;
  const H = canvas.height;
  canvas.width = W;
  ctx.clearRect(0, 0, W, H);
  if (!data || data.length < 2) {
    ctx.fillStyle = '#8b949e';
    ctx.font = '12px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('Noch keine Daten', W/2, H/2);
    return;
  }
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const pad = 16;
  const xs = data.map((_, i) => pad + (i / (data.length-1)) * (W - pad*2));
  const ys = data.map(v => H - pad - ((v - min) / range) * (H - pad*2));

  // Gradient fill
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, 'rgba(88,166,255,0.3)');
  grad.addColorStop(1, 'rgba(88,166,255,0.0)');
  ctx.beginPath();
  ctx.moveTo(xs[0], H);
  xs.forEach((x, i) => ctx.lineTo(x, ys[i]));
  ctx.lineTo(xs[xs.length-1], H);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // Line
  ctx.beginPath();
  ctx.strokeStyle = data[data.length-1] >= data[0] ? '#3fb950' : '#f85149';
  ctx.lineWidth = 2;
  xs.forEach((x, i) => i === 0 ? ctx.moveTo(x, ys[i]) : ctx.lineTo(x, ys[i]));
  ctx.stroke();

  // Last value dot
  const lx = xs[xs.length-1], ly = ys[ys.length-1];
  ctx.beginPath();
  ctx.arc(lx, ly, 3, 0, Math.PI*2);
  ctx.fillStyle = '#58a6ff';
  ctx.fill();
}

// ── Color helpers ─────────────────────────────────────────────────────────
function colorPnl(v) {
  return v > 0 ? 'pnl-pos' : v < 0 ? 'pnl-neg' : '';
}

function pct(val, from, to) {
  const p = Math.min(100, Math.max(0, ((val - from) / (to - from)) * 100));
  return p.toFixed(1);
}

// ── Render state ──────────────────────────────────────────────────────────
function render(state) {
  const br = state.bankroll;
  document.getElementById('ts').textContent = state.timestamp;
  document.getElementById('bankroll').textContent = '$' + br.toFixed(2);
  document.getElementById('bankroll').className = 'value blue';

  const pnl = state.total_pnl;
  const pnlEl = document.getElementById('total_pnl');
  pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(4);
  pnlEl.className = 'value ' + (pnl >= 0 ? 'green' : 'red');

  document.getElementById('total_trades').textContent = state.total_trades;

  const wr = state.win_rate;
  const wrEl = document.getElementById('win_rate');
  wrEl.textContent = wr.toFixed(1) + '%';
  wrEl.className = 'value ' + (wr >= 55 ? 'green' : wr >= 50 ? '' : 'red');

  document.getElementById('biggest_win').textContent = '+$' + state.biggest_win.toFixed(4);
  document.getElementById('biggest_loss').textContent = '$' + state.biggest_loss.toFixed(4);

  // Growth goal bars
  document.getElementById('goal1_bar').style.width = pct(br, 5.27, 100) + '%';
  document.getElementById('goal1_label').textContent = br < 100 ? 'Ziel: $100 (' + pct(br, 5.27, 100) + '%)' : 'ERREICHT!';
  document.getElementById('goal2_bar').style.width = pct(br, 100, 1000) + '%';
  document.getElementById('goal2_label').textContent = br < 1000 ? 'Ziel: $1.000 (' + pct(br, 100, 1000) + '%)' : 'ERREICHT!';
  document.getElementById('goal3_bar').style.width = pct(br, 1000, 10000) + '%';
  document.getElementById('goal3_label').textContent = br < 10000 ? 'Ziel: $10.000 (' + pct(br, 1000, 10000) + '%)' : 'ERREICHT!';

  // Equity curve
  if (state.equity_curve && state.equity_curve.length > 0) {
    drawChart('equityChart', state.equity_curve);
  }

  // Trades table
  const tbody = document.getElementById('trades_body');
  if (state.recent_trades && state.recent_trades.length > 0) {
    tbody.innerHTML = state.recent_trades.slice().reverse().map(t => {
      const p = t.pnl || 0;
      return `<tr>
        <td>${t.time || '--'}</td>
        <td style="max-width:120px;overflow:hidden;text-overflow:ellipsis">${t.market || '--'}</td>
        <td>${t.side || '--'}</td>
        <td>$${(t.size || 0).toFixed(2)}</td>
        <td class="${colorPnl(p)}">${p >= 0 ? '+' : ''}$${p.toFixed(4)}</td>
      </tr>`;
    }).join('');
  }

  // Logs
  const logBox = document.getElementById('log_box');
  if (state.logs && state.logs.length > 0) {
    logBox.innerHTML = state.logs.map(line => {
      let cls = 'log-line';
      if (line.includes('OPPORTUNITY')) cls += ' opportunity';
      else if (line.includes('[LIVE]')) cls += ' live';
      else if (line.includes('TIER CHANGE')) cls += ' tier';
      else if (line.includes('[WARNING]')) cls += ' warning';
      else if (line.includes('[ERROR]')) cls += ' error';
      return `<div class="${cls}">${escapeHtml(line)}</div>`;
    }).join('');
    logBox.scrollTop = logBox.scrollHeight;
  }
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Poll every 2 seconds ──────────────────────────────────────────────────
async function poll() {
  try {
    const r = await fetch('/api/state');
    if (r.ok) render(await r.json());
  } catch(e) {
    document.getElementById('ts').textContent = 'Verbindung getrennt';
  }
}

poll();
setInterval(poll, 2000);
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default HTTP logs

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode())

        elif self.path == "/api/state":
            state = get_dashboard_state()
            data = json.dumps(state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        else:
            self.send_response(404)
            self.end_headers()


def main():
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"Dashboard läuft auf http://0.0.0.0:{PORT}")
    print(f"Aufruf im Browser: http://<DEINE-SERVER-IP>:{PORT}")
    print("Strg+C zum Stoppen")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Dashboard gestoppt.")


if __name__ == "__main__":
    main()
