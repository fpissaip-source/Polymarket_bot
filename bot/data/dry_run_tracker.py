"""
Dry Run Tracker
===============
Records every opportunity seen in dry-run mode, checks market outcomes,
simulates P&L with virtual bankroll, and writes trades to trades.json
for dashboard consumption.

Data is stored in dry_run_log.json (one entry per opportunity).
"""

import json
import time
import logging
from pathlib import Path
from dataclasses import dataclass, asdict, field

logger = logging.getLogger(__name__)

DRY_RUN_LOG = Path(__file__).parent.parent / "dry_run_log.json"
TRADES_FILE = Path(__file__).parent.parent / "trades.json"


@dataclass
class DryRunEntry:
    timestamp: float
    market_id: str
    asset: str
    side: str
    q: float
    p: float
    edge: float
    size: float
    exec_price: float
    outcome: str = ""
    pnl: float = 0.0
    question: str = ""
    timeframe: str = "5m"
    window_start: str = ""
    window_end: str = ""
    decision: str = ""
    confidence: float = 0.0
    bayesian_prior: float = 0.5
    kelly_lambda: float = 0.25
    min_edge_used: float = 0.01
    actual_outcome: str = ""
    trade_id: str = ""
    exit_reason: str = ""


class DryRunTracker:
    def __init__(self, virtual_bankroll: float = 25.0):
        self._entries: list[DryRunEntry] = []
        self._virtual_bankroll = virtual_bankroll
        self._initial_bankroll = virtual_bankroll
        self._load()

    @property
    def virtual_bankroll(self) -> float:
        return self._virtual_bankroll

    def _load(self):
        if DRY_RUN_LOG.exists():
            try:
                raw = json.loads(DRY_RUN_LOG.read_text())
                self._entries = [DryRunEntry(**{k: v for k, v in e.items() if k in DryRunEntry.__dataclass_fields__}) for e in raw]
                resolved_pnl = sum(e.pnl for e in self._entries if e.outcome in ("WIN", "LOSS"))
                self._virtual_bankroll = self._initial_bankroll + resolved_pnl
                logger.info(f"DryRunTracker: loaded {len(self._entries)} entries, virtual bankroll=${self._virtual_bankroll:.2f}")
            except Exception as e:
                logger.warning(f"DryRunTracker: could not load log: {e}")

    def _save(self):
        try:
            DRY_RUN_LOG.write_text(json.dumps([asdict(e) for e in self._entries], indent=2))
        except Exception as e:
            logger.warning(f"DryRunTracker: could not save log: {e}")

    def record(self, market_id: str, asset: str, side: str,
               q: float, p: float, edge: float, size: float, exec_price: float,
               question: str = "", timeframe: str = "5m",
               window_start: str = "", window_end: str = "",
               confidence: float = 0.0, bayesian_prior: float = 0.5,
               kelly_lambda: float = 0.25, min_edge_used: float = 0.01):
        decision = side
        if side == "YES":
            decision = "UP"
        elif side == "NO":
            decision = "DOWN"

        capped_size = min(size, self._virtual_bankroll * 0.5)
        if capped_size <= 0:
            logger.warning(f"DryRunTracker: insufficient virtual bankroll (${self._virtual_bankroll:.2f}), skipping")
            return

        ts = time.time()
        trade_id = f"dry_{int(ts * 1000)}_{market_id[-6:]}"

        entry = DryRunEntry(
            timestamp=ts,
            market_id=market_id,
            asset=asset,
            side=side,
            q=q,
            p=p,
            edge=edge,
            size=round(capped_size, 4),
            exec_price=exec_price,
            question=question,
            timeframe=timeframe,
            window_start=window_start,
            window_end=window_end,
            decision=decision,
            confidence=confidence,
            bayesian_prior=bayesian_prior,
            kelly_lambda=kelly_lambda,
            min_edge_used=min_edge_used,
            trade_id=trade_id,
        )
        self._entries.append(entry)
        self._save()
        self._write_trade(entry)
        logger.info(
            f"[DRY RUN TRADE] {asset} {decision} | q={q:.3f} p={p:.3f} edge={edge:.4f} "
            f"size=${capped_size:.2f} | bankroll=${self._virtual_bankroll:.2f} | {question[:60]}"
        )

    def get_open_entries(self) -> list[DryRunEntry]:
        return [e for e in self._entries if e.outcome == ""]

    def early_exit(self, trade_id: str, exit_price: float, reason: str):
        for e in self._entries:
            if e.trade_id == trade_id and e.outcome == "":
                shares = e.size / e.exec_price if e.exec_price > 0 else 0
                sell_value = shares * exit_price
                e.pnl = round(sell_value - e.size, 4)
                e.outcome = "WIN" if e.pnl >= 0 else "LOSS"
                e.exit_reason = reason
                e.actual_outcome = reason
                self._virtual_bankroll += e.pnl
                self._save()
                self._update_trades_file()
                logger.info(
                    f"[EARLY EXIT] {e.asset} {e.side} | {reason} | "
                    f"entry={e.exec_price:.4f} exit={exit_price:.4f} | "
                    f"P&L=${e.pnl:+.4f} | bankroll=${self._virtual_bankroll:.2f}"
                )
                return True
        return False

    def resolve(self, market_id: str, winning_side: str):
        resolved = 0
        for e in self._entries:
            if e.market_id == market_id and e.outcome == "":
                e.actual_outcome = "UP" if winning_side in ("YES", "UP") else "DOWN"
                shares = e.size / e.exec_price if e.exec_price > 0 else 0
                if e.side == winning_side:
                    e.outcome = "WIN"
                    e.pnl = round(shares * 1.0 - e.size, 4)
                else:
                    e.outcome = "LOSS"
                    e.pnl = round(-e.size, 4)
                self._virtual_bankroll += e.pnl
                resolved += 1
        if resolved:
            self._save()
            self._update_trades_file()
            logger.info(
                f"[DRY RUN RESOLVED] {market_id} -> {winning_side} wins | "
                f"{resolved} trades resolved | bankroll=${self._virtual_bankroll:.2f}"
            )

    def _write_trade(self, entry: DryRunEntry):
        try:
            trades = []
            if TRADES_FILE.exists():
                try:
                    trades = json.loads(TRADES_FILE.read_text())
                except Exception:
                    pass
            trades.append({
                "id": entry.trade_id,
                "marketId": entry.market_id,
                "asset": entry.asset,
                "side": entry.decision,
                "price": round(entry.exec_price, 4),
                "size": round(entry.size, 4),
                "pnl": 0.0,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry.timestamp)),
                "status": "OPEN",
                "question": entry.question,
                "q": round(entry.q, 4),
                "edge": round(entry.edge, 4),
                "confidence": round(entry.confidence, 4),
                "window_start": entry.window_start,
                "window_end": entry.window_end,
                "outcome": "",
                "actual_outcome": "",
            })
            TRADES_FILE.write_text(json.dumps(trades[-500:], indent=2))
        except Exception as e:
            logger.warning(f"DryRunTracker: could not write trade: {e}")

    def _update_trades_file(self):
        try:
            trades = []
            if TRADES_FILE.exists():
                trades = json.loads(TRADES_FILE.read_text())

            entry_map = {}
            for e in self._entries:
                if e.outcome in ("WIN", "LOSS") and e.trade_id:
                    entry_map[e.trade_id] = e

            updated = 0
            for t in trades:
                tid = t.get("id", "")
                if tid in entry_map and t.get("status") == "OPEN":
                    e = entry_map[tid]
                    t["pnl"] = e.pnl
                    t["status"] = e.outcome
                    t["outcome"] = e.outcome
                    t["actual_outcome"] = e.actual_outcome
                    updated += 1

            if updated:
                TRADES_FILE.write_text(json.dumps(trades, indent=2))
        except Exception as e:
            logger.warning(f"DryRunTracker: could not update trades: {e}")

    def stats(self) -> dict:
        resolved = [e for e in self._entries if e.outcome in ("WIN", "LOSS")]
        total = len(self._entries)
        n = len(resolved)
        if n == 0:
            return {
                "total_opportunities": total,
                "resolved": 0,
                "virtual_bankroll": round(self._virtual_bankroll, 2),
                "initial_bankroll": self._initial_bankroll,
            }

        wins = sum(1 for e in resolved if e.outcome == "WIN")
        total_pnl = sum(e.pnl for e in resolved)
        avg_edge = sum(e.edge for e in resolved) / n
        avg_size = sum(e.size for e in resolved) / n

        mean_pnl = total_pnl / n
        variance = sum((e.pnl - mean_pnl) ** 2 for e in resolved) / n
        std_pnl = variance ** 0.5
        sharpe = mean_pnl / std_pnl if std_pnl > 1e-8 else 0.0

        per_asset = {}
        for e in resolved:
            a = e.asset
            if a not in per_asset:
                per_asset[a] = {"wins": 0, "total": 0, "pnl": 0.0}
            per_asset[a]["total"] += 1
            if e.outcome == "WIN":
                per_asset[a]["wins"] += 1
            per_asset[a]["pnl"] += e.pnl

        return {
            "total_opportunities": total,
            "resolved": n,
            "wins": wins,
            "losses": n - wins,
            "win_rate": round(wins / n, 3),
            "total_pnl": round(total_pnl, 4),
            "avg_edge": round(avg_edge, 4),
            "avg_size": round(avg_size, 4),
            "sharpe": round(sharpe, 3),
            "virtual_bankroll": round(self._virtual_bankroll, 2),
            "initial_bankroll": self._initial_bankroll,
            "bankroll_change_pct": round(((self._virtual_bankroll - self._initial_bankroll) / self._initial_bankroll) * 100, 1),
            "per_asset": per_asset,
        }

    def log_stats(self):
        s = self.stats()
        if s["resolved"] == 0:
            logger.info(
                f"[DRY RUN STATS] {s['total_opportunities']} opportunities | "
                f"0 resolved | bankroll=${s['virtual_bankroll']:.2f}"
            )
        else:
            logger.info(
                f"[DRY RUN STATS] opportunities={s['total_opportunities']} | "
                f"resolved={s['resolved']} | win_rate={s['win_rate']:.1%} | "
                f"P&L=${s['total_pnl']:+.4f} | bankroll=${s['virtual_bankroll']:.2f} "
                f"({s['bankroll_change_pct']:+.1f}%) | Sharpe={s['sharpe']:.2f}"
            )
            for asset, data in s.get("per_asset", {}).items():
                wr = data["wins"] / data["total"] if data["total"] > 0 else 0
                logger.info(
                    f"  {asset}: {data['wins']}/{data['total']} wins ({wr:.0%}) | P&L=${data['pnl']:+.2f}"
                )

    def get_resolved_entries(self, last_n: int = 50) -> list[DryRunEntry]:
        resolved = [e for e in self._entries if e.outcome in ("WIN", "LOSS")]
        return resolved[-last_n:]

    def get_all_entries(self) -> list[DryRunEntry]:
        return list(self._entries)
