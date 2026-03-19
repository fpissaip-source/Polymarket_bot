"""
Dry Run Tracker
===============
Records every opportunity seen in dry-run mode, checks market outcomes,
simulates P&L, and prints periodic statistics.

Data is stored in dry_run_log.json (one entry per opportunity).
"""

import json
import time
import logging
from pathlib import Path
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

DRY_RUN_LOG = Path(__file__).parent.parent / "dry_run_log.json"


@dataclass
class DryRunEntry:
    timestamp: float
    market_id: str
    asset: str
    side: str           # "YES" or "NO"
    q: float            # Bayesian probability
    p: float            # Market price
    edge: float         # EV_net
    size: float         # Kelly position size $
    exec_price: float   # Stoikov reservation price
    outcome: str = ""   # "WIN" / "LOSS" / "UNKNOWN"
    pnl: float = 0.0    # Simulated P&L in $


class DryRunTracker:
    def __init__(self):
        self._entries: list[DryRunEntry] = []
        self._load()

    def _load(self):
        if DRY_RUN_LOG.exists():
            try:
                raw = json.loads(DRY_RUN_LOG.read_text())
                self._entries = [DryRunEntry(**e) for e in raw]
                logger.info(f"DryRunTracker: loaded {len(self._entries)} existing entries")
            except Exception as e:
                logger.warning(f"DryRunTracker: could not load log: {e}")

    def _save(self):
        try:
            DRY_RUN_LOG.write_text(json.dumps([asdict(e) for e in self._entries], indent=2))
        except Exception as e:
            logger.warning(f"DryRunTracker: could not save log: {e}")

    def record(self, market_id: str, asset: str, side: str,
               q: float, p: float, edge: float, size: float, exec_price: float):
        """Record a new dry-run opportunity."""
        entry = DryRunEntry(
            timestamp=time.time(),
            market_id=market_id,
            asset=asset,
            side=side,
            q=q,
            p=p,
            edge=edge,
            size=size,
            exec_price=exec_price,
        )
        self._entries.append(entry)
        self._save()

    def resolve(self, market_id: str, winning_side: str):
        """
        Mark all open entries for a market as WIN or LOSS and compute P&L.
        winning_side: "YES" or "NO"
        Call this when a market resolves.
        """
        resolved = 0
        for e in self._entries:
            if e.market_id == market_id and e.outcome == "UNKNOWN":
                if e.side == winning_side:
                    # WIN: receive $1 per share, paid exec_price per share
                    e.outcome = "WIN"
                    e.pnl = round(e.size * (1.0 - e.exec_price), 4)
                else:
                    # LOSS: lose the amount invested
                    e.outcome = "LOSS"
                    e.pnl = round(-e.size * e.exec_price, 4)
                resolved += 1
        if resolved:
            self._save()
            logger.info(f"DryRunTracker: resolved {resolved} entries for {market_id} → {winning_side} wins")

    def stats(self) -> dict:
        """Compute summary statistics over all resolved entries."""
        resolved = [e for e in self._entries if e.outcome in ("WIN", "LOSS")]
        total = len(self._entries)
        n = len(resolved)
        if n == 0:
            return {"total": total, "resolved": 0}

        wins = sum(1 for e in resolved if e.outcome == "WIN")
        total_pnl = sum(e.pnl for e in resolved)
        avg_edge = sum(e.edge for e in resolved) / n
        avg_size = sum(e.size for e in resolved) / n

        # Simulated Sharpe: mean_pnl / std_pnl
        mean_pnl = total_pnl / n
        variance = sum((e.pnl - mean_pnl) ** 2 for e in resolved) / n
        std_pnl = variance ** 0.5
        sharpe = mean_pnl / std_pnl if std_pnl > 1e-8 else 0.0

        return {
            "total_opportunities": total,
            "resolved": n,
            "win_rate": round(wins / n, 3),
            "total_pnl": round(total_pnl, 4),
            "avg_edge": round(avg_edge, 4),
            "avg_size": round(avg_size, 4),
            "sharpe": round(sharpe, 3),
        }

    def log_stats(self):
        """Log a stats summary — call this periodically."""
        s = self.stats()
        if s["resolved"] == 0:
            logger.info(
                f"[DRY RUN STATS] {s['total']} opportunities recorded, "
                f"0 resolved yet (markets still open)"
            )
        else:
            logger.info(
                f"[DRY RUN STATS] opportunities={s['total']} | "
                f"resolved={s['resolved']} | win_rate={s['win_rate']:.1%} | "
                f"P&L=${s['total_pnl']:+.4f} | avg_edge={s['avg_edge']:.3f} | "
                f"Sharpe={s['sharpe']:.2f}"
            )
