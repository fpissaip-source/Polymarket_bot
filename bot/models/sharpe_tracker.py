"""
Sharpe-Ratio Performance Tracker
=================================
Comprehensive performance evaluation based on the document's benchmarking system.

Sharpe-Ratio Classification (industry standard):
  SR < 1.0  → Unstable system, high risk relative to returns
  SR 1.0-2.0 → Solid system, suitable for professional deployment
  SR > 2.0  → Excellent system, high predictive power, low risk

The tracker monitors:
  - Rolling Sharpe ratio (daily, weekly, monthly)
  - Maximum drawdown and current drawdown
  - Win rate and profit factor
  - Risk-adjusted returns
  - Equity curve stability

Target: Sharpe > 2.0 through integration of AI filters, Kelly sizing,
and comprehensive data logging (document reports improvement from 0.0 to 2.3).
"""

import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SHARPE_STATE_FILE = Path(__file__).parent.parent / "sharpe_state.json"


@dataclass
class PerformanceSnapshot:
    sharpe_ratio: float
    classification: str         # "UNSTABLE" | "SOLID" | "EXCELLENT"
    total_return_pct: float
    max_drawdown_pct: float
    current_drawdown_pct: float
    win_rate: float
    profit_factor: float
    total_trades: int
    avg_pnl: float
    best_trade: float
    worst_trade: float
    equity_stability: float     # 0.0-1.0, higher = more stable curve


class SharpeTracker:
    """
    Tracks all trade P&L and computes rolling performance metrics.

    Usage:
        tracker = SharpeTracker(initial_capital=25.0)
        tracker.record_trade(pnl=1.50)
        tracker.record_trade(pnl=-0.30)
        snapshot = tracker.get_snapshot()
        print(f"Sharpe: {snapshot.sharpe_ratio:.2f} ({snapshot.classification})")
    """

    def __init__(self, initial_capital: float = 25.0):
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.peak_capital = initial_capital
        self._pnl_history: list[float] = []
        self._equity_curve: list[float] = [initial_capital]
        self._trade_times: list[float] = []
        self._load()

    def record_trade(self, pnl: float):
        """Record a completed trade's P&L."""
        self._pnl_history.append(pnl)
        self.current_capital += pnl
        self.current_capital = max(0.0, self.current_capital)

        if self.current_capital > self.peak_capital:
            self.peak_capital = self.current_capital

        self._equity_curve.append(self.current_capital)
        self._trade_times.append(time.time())

        # Keep last 1000 trades
        if len(self._pnl_history) > 1000:
            self._pnl_history = self._pnl_history[-1000:]
            self._equity_curve = self._equity_curve[-1001:]
            self._trade_times = self._trade_times[-1000:]

        self._save()

    def compute_sharpe(self, pnls: list[float] | None = None, annualize: bool = True) -> float:
        """
        Compute Sharpe ratio from P&L series.

        For trade-based (not time-based) Sharpe, we annualize by
        estimating trades per year from the observed trading frequency.
        """
        data = pnls if pnls is not None else self._pnl_history
        if len(data) < 2:
            return 0.0

        mean = sum(data) / len(data)
        variance = sum((p - mean) ** 2 for p in data) / len(data)
        std = math.sqrt(variance)

        if std < 1e-10:
            return 0.0

        sharpe = mean / std

        if annualize and len(self._trade_times) >= 2:
            # Estimate trades per year
            time_span = self._trade_times[-1] - self._trade_times[0]
            if time_span > 0:
                trades_per_day = len(self._trade_times) / (time_span / 86400)
                trades_per_year = trades_per_day * 252  # trading days
                sharpe *= math.sqrt(min(trades_per_year, 10000))

        return sharpe

    def get_snapshot(self) -> PerformanceSnapshot:
        """Get a complete performance snapshot."""
        pnls = self._pnl_history
        n = len(pnls)

        if n == 0:
            return PerformanceSnapshot(
                sharpe_ratio=0.0,
                classification="NO_DATA",
                total_return_pct=0.0,
                max_drawdown_pct=0.0,
                current_drawdown_pct=0.0,
                win_rate=0.0,
                profit_factor=0.0,
                total_trades=0,
                avg_pnl=0.0,
                best_trade=0.0,
                worst_trade=0.0,
                equity_stability=0.0,
            )

        # Sharpe
        sharpe = self.compute_sharpe()

        # Classification
        if sharpe >= 2.0:
            classification = "EXCELLENT"
        elif sharpe >= 1.0:
            classification = "SOLID"
        else:
            classification = "UNSTABLE"

        # Total return
        total_return = (self.current_capital - self.initial_capital) / self.initial_capital * 100

        # Max drawdown from equity curve
        max_dd = 0.0
        peak = self._equity_curve[0]
        for eq in self._equity_curve:
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (peak - eq) / peak
                max_dd = max(max_dd, dd)

        # Current drawdown
        current_dd = 0.0
        if self.peak_capital > 0:
            current_dd = (self.peak_capital - self.current_capital) / self.peak_capital

        # Win/loss stats
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        win_rate = len(wins) / n if n > 0 else 0.0
        gross_wins = sum(wins)
        gross_losses = sum(abs(l) for l in losses)
        profit_factor = gross_wins / gross_losses if gross_losses > 0 else (float('inf') if gross_wins > 0 else 0.0)

        # Equity stability: R-squared of equity curve vs linear fit
        equity_stability = self._compute_equity_stability()

        return PerformanceSnapshot(
            sharpe_ratio=round(sharpe, 3),
            classification=classification,
            total_return_pct=round(total_return, 2),
            max_drawdown_pct=round(max_dd * 100, 2),
            current_drawdown_pct=round(current_dd * 100, 2),
            win_rate=round(win_rate, 3),
            profit_factor=round(profit_factor, 2),
            total_trades=n,
            avg_pnl=round(sum(pnls) / n, 4),
            best_trade=round(max(pnls), 4),
            worst_trade=round(min(pnls), 4),
            equity_stability=round(equity_stability, 3),
        )

    def _compute_equity_stability(self) -> float:
        """
        R-squared of equity curve: how close to a straight line?
        1.0 = perfectly smooth growth, 0.0 = random walk.
        """
        curve = self._equity_curve
        n = len(curve)
        if n < 3:
            return 0.0

        # Simple linear regression
        x_mean = (n - 1) / 2.0
        y_mean = sum(curve) / n
        ss_xy = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(curve))
        ss_xx = sum((i - x_mean) ** 2 for i in range(n))
        ss_yy = sum((y - y_mean) ** 2 for y in curve)

        if ss_xx < 1e-10 or ss_yy < 1e-10:
            return 0.0

        r_squared = (ss_xy ** 2) / (ss_xx * ss_yy)
        return max(0.0, min(1.0, r_squared))

    def get_drawdown_risk(self) -> float:
        """
        Returns current drawdown as fraction of peak (0.0 - 1.0).
        Used by Risk-Constrained Kelly to reduce position sizes during drawdowns.
        """
        if self.peak_capital <= 0:
            return 0.0
        return (self.peak_capital - self.current_capital) / self.peak_capital

    def should_reduce_risk(self) -> tuple[bool, float]:
        """
        Risk-Constrained Kelly: dynamically reduce position sizes
        when in a drawdown to protect the equity curve.

        Returns (should_reduce, multiplier):
          - drawdown < 10%: no reduction (1.0)
          - drawdown 10-20%: reduce to 70%
          - drawdown 20-30%: reduce to 40%
          - drawdown > 30%: reduce to 20% (survival mode)
        """
        dd = self.get_drawdown_risk()
        if dd < 0.10:
            return False, 1.0
        elif dd < 0.20:
            return True, 0.7
        elif dd < 0.30:
            return True, 0.4
        else:
            return True, 0.2

    def summary(self) -> str:
        snap = self.get_snapshot()
        return (
            f"Sharpe={snap.sharpe_ratio:.2f}({snap.classification}) | "
            f"Return={snap.total_return_pct:+.1f}% | "
            f"MaxDD={snap.max_drawdown_pct:.1f}% | "
            f"WR={snap.win_rate:.1%} | "
            f"PF={snap.profit_factor:.2f} | "
            f"Trades={snap.total_trades}"
        )

    def _load(self):
        if SHARPE_STATE_FILE.exists():
            try:
                raw = json.loads(SHARPE_STATE_FILE.read_text())
                self._pnl_history = raw.get("pnl_history", [])
                self._equity_curve = raw.get("equity_curve", [self.initial_capital])
                self._trade_times = raw.get("trade_times", [])
                self.current_capital = raw.get("current_capital", self.initial_capital)
                self.peak_capital = raw.get("peak_capital", self.initial_capital)
                logger.info(f"SharpeTracker: loaded {len(self._pnl_history)} trades")
            except Exception as e:
                logger.warning(f"SharpeTracker: could not load state: {e}")

    def _save(self):
        try:
            SHARPE_STATE_FILE.write_text(json.dumps({
                "pnl_history": self._pnl_history[-1000:],
                "equity_curve": self._equity_curve[-1001:],
                "trade_times": self._trade_times[-1000:],
                "current_capital": round(self.current_capital, 4),
                "peak_capital": round(self.peak_capital, 4),
            }, indent=2))
        except Exception as e:
            logger.warning(f"SharpeTracker: could not save state: {e}")
