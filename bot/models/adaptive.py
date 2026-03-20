"""
Adaptive Learning Module
========================
Analyzes dry-run trade results and adjusts bot parameters to improve
future performance. Uses rolling windows to detect patterns and adapt.

Optimized parameters:
  - Bayesian prior weight per asset
  - Edge thresholds (min_edge)
  - Kelly lambda (aggressiveness)

Metrics used:
  - Rolling win rate (last N trades)
  - Average P&L per trade
  - Signal strength performance (which q values win most)
"""

import json
import logging
from pathlib import Path
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

ADAPTIVE_STATE_FILE = Path(__file__).parent.parent / "adaptive_state.json"


@dataclass
class AdaptiveParams:
    bayesian_alpha_adj: dict = field(default_factory=dict)
    edge_threshold_adj: float = 0.0
    kelly_lambda_adj: float = 0.0
    asset_bias: dict = field(default_factory=dict)
    total_analyzed: int = 0
    last_update: float = 0.0


class AdaptiveLearner:
    def __init__(self, lookback: int = 30):
        self.lookback = lookback
        self.params = AdaptiveParams()
        self._load()

    def _load(self):
        if ADAPTIVE_STATE_FILE.exists():
            try:
                raw = json.loads(ADAPTIVE_STATE_FILE.read_text())
                self.params = AdaptiveParams(**{k: v for k, v in raw.items() if k in AdaptiveParams.__dataclass_fields__})
                logger.info(f"AdaptiveLearner: loaded state (analyzed={self.params.total_analyzed})")
            except Exception as e:
                logger.warning(f"AdaptiveLearner: could not load state: {e}")

    def _save(self):
        try:
            ADAPTIVE_STATE_FILE.write_text(json.dumps({
                "bayesian_alpha_adj": self.params.bayesian_alpha_adj,
                "edge_threshold_adj": self.params.edge_threshold_adj,
                "kelly_lambda_adj": self.params.kelly_lambda_adj,
                "asset_bias": self.params.asset_bias,
                "total_analyzed": self.params.total_analyzed,
                "last_update": self.params.last_update,
            }, indent=2))
        except Exception as e:
            logger.warning(f"AdaptiveLearner: could not save state: {e}")

    def _compute_sharpe(self, entries: list) -> float:
        if len(entries) < 2:
            return 0.0
        pnls = [e.pnl for e in entries]
        mean = sum(pnls) / len(pnls)
        variance = sum((p - mean) ** 2 for p in pnls) / len(pnls)
        std = variance ** 0.5
        return mean / std if std > 1e-8 else 0.0

    def analyze_and_adapt(self, resolved_entries: list) -> dict:
        import time
        if len(resolved_entries) < 5:
            return {"status": "insufficient_data", "count": len(resolved_entries)}

        recent = resolved_entries[-self.lookback:]
        n = len(recent)

        wins = sum(1 for e in recent if e.outcome == "WIN")
        losses = n - wins
        win_rate = wins / n if n > 0 else 0.5
        total_pnl = sum(e.pnl for e in recent)
        avg_pnl = total_pnl / n
        sharpe = self._compute_sharpe(recent)

        high_q_trades = [e for e in recent if e.q > 0.6 or e.q < 0.4]
        high_q_wins = sum(1 for e in high_q_trades if e.outcome == "WIN")
        high_q_wr = high_q_wins / len(high_q_trades) if high_q_trades else 0.5

        low_q_trades = [e for e in recent if 0.4 <= e.q <= 0.6]
        low_q_wins = sum(1 for e in low_q_trades if e.outcome == "WIN")
        low_q_wr = low_q_wins / len(low_q_trades) if low_q_trades else 0.5

        if sharpe > 0.5 and win_rate > 0.55:
            self.params.kelly_lambda_adj = min(0.15, self.params.kelly_lambda_adj + 0.02)
            self.params.edge_threshold_adj = max(-0.005, self.params.edge_threshold_adj - 0.001)
        elif sharpe < -0.3 or win_rate < 0.4:
            self.params.kelly_lambda_adj = max(-0.15, self.params.kelly_lambda_adj - 0.03)
            self.params.edge_threshold_adj = min(0.01, self.params.edge_threshold_adj + 0.002)
        elif sharpe > 0.2 and win_rate > 0.5:
            self.params.kelly_lambda_adj = min(0.15, self.params.kelly_lambda_adj + 0.01)
            self.params.edge_threshold_adj *= 0.95
        else:
            self.params.kelly_lambda_adj *= 0.9
            self.params.edge_threshold_adj *= 0.9

        asset_stats = {}
        for e in recent:
            a = e.asset
            if a not in asset_stats:
                asset_stats[a] = {"wins": 0, "total": 0, "pnl": 0.0, "entries": []}
            asset_stats[a]["total"] += 1
            if e.outcome == "WIN":
                asset_stats[a]["wins"] += 1
            asset_stats[a]["pnl"] += e.pnl
            asset_stats[a]["entries"].append(e)

        for asset, s in asset_stats.items():
            wr = s["wins"] / s["total"] if s["total"] > 0 else 0.5
            asset_sharpe = self._compute_sharpe(s["entries"])

            if wr > 0.55:
                self.params.asset_bias[asset] = min(0.05, self.params.asset_bias.get(asset, 0) + 0.01)
            elif wr < 0.45:
                self.params.asset_bias[asset] = max(-0.05, self.params.asset_bias.get(asset, 0) - 0.01)
            else:
                self.params.asset_bias[asset] = self.params.asset_bias.get(asset, 0) * 0.8

            if high_q_wr > 0.55 and asset_sharpe > 0.2:
                self.params.bayesian_alpha_adj[asset] = min(0.10, self.params.bayesian_alpha_adj.get(asset, 0) + 0.02)
            elif high_q_wr < 0.45 or asset_sharpe < -0.2:
                self.params.bayesian_alpha_adj[asset] = max(-0.10, self.params.bayesian_alpha_adj.get(asset, 0) - 0.03)
            else:
                self.params.bayesian_alpha_adj[asset] = self.params.bayesian_alpha_adj.get(asset, 0) * 0.9

        self.params.total_analyzed = len(resolved_entries)
        self.params.last_update = time.time()
        self._save()

        adjustments = {
            "status": "adapted",
            "trades_analyzed": n,
            "win_rate": round(win_rate, 3),
            "avg_pnl": round(avg_pnl, 4),
            "sharpe": round(sharpe, 3),
            "high_q_win_rate": round(high_q_wr, 3),
            "low_q_win_rate": round(low_q_wr, 3),
            "kelly_lambda_adj": round(self.params.kelly_lambda_adj, 4),
            "edge_threshold_adj": round(self.params.edge_threshold_adj, 4),
            "asset_bias": {k: round(v, 4) for k, v in self.params.asset_bias.items()},
        }

        logger.info(
            f"[ADAPTIVE] win_rate={win_rate:.1%} | sharpe={sharpe:.2f} | avg_pnl=${avg_pnl:+.4f} | "
            f"kelly_adj={self.params.kelly_lambda_adj:+.3f} | "
            f"edge_adj={self.params.edge_threshold_adj:+.4f} | "
            f"high_q_wr={high_q_wr:.1%} | low_q_wr={low_q_wr:.1%}"
        )
        return adjustments

    def get_kelly_lambda(self, base_lambda: float) -> float:
        adjusted = base_lambda + self.params.kelly_lambda_adj
        return max(0.10, min(0.60, adjusted))

    def get_min_edge(self, base_min_edge: float) -> float:
        adjusted = base_min_edge + self.params.edge_threshold_adj
        return max(0.003, min(0.05, adjusted))

    def get_bayesian_alpha(self, asset: str, base_alpha: float) -> float:
        adj = self.params.bayesian_alpha_adj.get(asset, 0.0)
        adjusted = base_alpha + adj
        return max(0.1, min(0.5, adjusted))

    def get_asset_bias(self, asset: str) -> float:
        return self.params.asset_bias.get(asset, 0.0)

    def get_state(self) -> dict:
        return {
            "kelly_lambda_adj": round(self.params.kelly_lambda_adj, 4),
            "edge_threshold_adj": round(self.params.edge_threshold_adj, 4),
            "asset_bias": {k: round(v, 4) for k, v in self.params.asset_bias.items()},
            "bayesian_alpha_adj": {k: round(v, 4) for k, v in self.params.bayesian_alpha_adj.items()},
            "total_analyzed": self.params.total_analyzed,
        }
