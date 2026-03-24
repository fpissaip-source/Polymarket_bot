"""
Alpha-Cluster Detection
========================
Identifies which market segments have a genuine statistical edge (Alpha)
and which are noise. Based on the document concept of clustering trade
logs by market type/timeframe to find where real edge exists.

Key insight: Alpha is NOT uniformly distributed. Some market segments
(e.g. "News-Resolution" markets) can reach 70%+ win rates, while
generic crypto markets hover at 51-54%. The bot must concentrate
resources on high-alpha clusters and avoid low-edge segments.

Uses rolling trade history to compute per-cluster metrics:
  - Win rate
  - Average EV
  - Sharpe ratio
  - Profit factor (gross wins / gross losses)

Clusters are defined by: market_category + timeframe + price_range
"""

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

CLUSTER_STATE_FILE = Path(__file__).parent.parent / "alpha_cluster_state.json"


@dataclass
class ClusterStats:
    cluster_id: str
    total_trades: int = 0
    wins: int = 0
    total_pnl: float = 0.0
    gross_wins: float = 0.0
    gross_losses: float = 0.0
    pnl_history: list = field(default_factory=list)
    last_updated: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.total_trades if self.total_trades > 0 else 0.0

    @property
    def avg_pnl(self) -> float:
        return self.total_pnl / self.total_trades if self.total_trades > 0 else 0.0

    @property
    def profit_factor(self) -> float:
        if self.gross_losses <= 0:
            return float('inf') if self.gross_wins > 0 else 0.0
        return self.gross_wins / self.gross_losses

    @property
    def sharpe(self) -> float:
        if len(self.pnl_history) < 2:
            return 0.0
        mean = sum(self.pnl_history) / len(self.pnl_history)
        variance = sum((p - mean) ** 2 for p in self.pnl_history) / len(self.pnl_history)
        std = variance ** 0.5
        return mean / std if std > 1e-8 else 0.0


@dataclass
class ClusterDecision:
    cluster_id: str
    is_tradeable: bool
    alpha_score: float          # 0.0 - 1.0, higher = better alpha
    kelly_multiplier: float     # scale position size by cluster quality
    reason: str


class AlphaClusterDetector:
    """
    Tracks trade outcomes by cluster and determines which clusters
    have genuine alpha (edge) versus noise.

    Usage:
        detector = AlphaClusterDetector()
        cluster_id = detector.classify(category="politics", timeframe="event", price=0.65)
        detector.record_trade(cluster_id, pnl=0.15, won=True)
        decision = detector.evaluate_cluster(cluster_id)
        if decision.is_tradeable:
            # proceed with trade, scale by decision.kelly_multiplier
    """

    # Minimum trades before we trust the cluster statistics
    MIN_TRADES_FOR_SIGNAL = 8

    # Thresholds for alpha classification
    HIGH_ALPHA_WIN_RATE = 0.58      # >58% = strong alpha
    MEDIUM_ALPHA_WIN_RATE = 0.52    # >52% = moderate alpha
    MIN_PROFIT_FACTOR = 1.2         # gross_wins must be 1.2x gross_losses

    def __init__(self, max_history: int = 200):
        self._clusters: dict[str, ClusterStats] = {}
        self._max_history = max_history
        self._load()

    def classify(self, category: str, timeframe: str, price: float) -> str:
        """
        Assign a trade to a cluster based on its characteristics.

        Returns a cluster_id string like "politics_event_mid" or "crypto_5m_high".
        """
        # Price range bucket
        if price < 0.30:
            price_bucket = "low"
        elif price < 0.70:
            price_bucket = "mid"
        else:
            price_bucket = "high"

        cluster_id = f"{category}_{timeframe}_{price_bucket}"
        if cluster_id not in self._clusters:
            self._clusters[cluster_id] = ClusterStats(cluster_id=cluster_id)
        return cluster_id

    def record_trade(self, cluster_id: str, pnl: float, won: bool):
        """Record a completed trade outcome for the given cluster."""
        if cluster_id not in self._clusters:
            self._clusters[cluster_id] = ClusterStats(cluster_id=cluster_id)

        c = self._clusters[cluster_id]
        c.total_trades += 1
        c.total_pnl += pnl
        if won:
            c.wins += 1
            c.gross_wins += pnl
        else:
            c.gross_losses += abs(pnl)

        c.pnl_history.append(pnl)
        if len(c.pnl_history) > self._max_history:
            c.pnl_history = c.pnl_history[-self._max_history:]

        c.last_updated = time.time()
        self._save()

    def evaluate_cluster(self, cluster_id: str) -> ClusterDecision:
        """
        Evaluate whether a cluster has genuine alpha.

        Returns ClusterDecision with:
          - is_tradeable: should we trade this cluster?
          - alpha_score: 0.0 - 1.0
          - kelly_multiplier: scale factor for position sizing
        """
        c = self._clusters.get(cluster_id)

        # New/unknown cluster: allow trading with conservative sizing
        if not c or c.total_trades < self.MIN_TRADES_FOR_SIGNAL:
            return ClusterDecision(
                cluster_id=cluster_id,
                is_tradeable=True,
                alpha_score=0.5,
                kelly_multiplier=0.6,  # conservative for unknown clusters
                reason=f"Insufficient data ({c.total_trades if c else 0}/{self.MIN_TRADES_FOR_SIGNAL} trades)"
            )

        wr = c.win_rate
        pf = c.profit_factor
        sharpe = c.sharpe

        # Compute alpha score (0-1)
        wr_score = min(1.0, max(0.0, (wr - 0.45) / 0.25))         # 0.45->0, 0.70->1
        pf_score = min(1.0, max(0.0, (pf - 0.8) / 1.2))           # 0.8->0, 2.0->1
        sharpe_score = min(1.0, max(0.0, (sharpe + 0.5) / 2.0))    # -0.5->0, 1.5->1
        alpha_score = 0.4 * wr_score + 0.3 * pf_score + 0.3 * sharpe_score

        # Classification
        if wr >= self.HIGH_ALPHA_WIN_RATE and pf >= self.MIN_PROFIT_FACTOR:
            # High alpha cluster: increase sizing
            return ClusterDecision(
                cluster_id=cluster_id,
                is_tradeable=True,
                alpha_score=alpha_score,
                kelly_multiplier=1.3,  # boost sizing for proven clusters
                reason=f"HIGH ALPHA: WR={wr:.1%} PF={pf:.2f} Sharpe={sharpe:.2f}"
            )
        elif wr >= self.MEDIUM_ALPHA_WIN_RATE and pf >= 1.0:
            # Medium alpha: normal sizing
            return ClusterDecision(
                cluster_id=cluster_id,
                is_tradeable=True,
                alpha_score=alpha_score,
                kelly_multiplier=1.0,
                reason=f"MEDIUM ALPHA: WR={wr:.1%} PF={pf:.2f} Sharpe={sharpe:.2f}"
            )
        elif wr < 0.45 or pf < 0.8:
            # Negative alpha: block this cluster
            return ClusterDecision(
                cluster_id=cluster_id,
                is_tradeable=False,
                alpha_score=alpha_score,
                kelly_multiplier=0.0,
                reason=f"NO ALPHA: WR={wr:.1%} PF={pf:.2f} Sharpe={sharpe:.2f} — BLOCKED"
            )
        else:
            # Low/marginal alpha: reduce sizing
            return ClusterDecision(
                cluster_id=cluster_id,
                is_tradeable=True,
                alpha_score=alpha_score,
                kelly_multiplier=0.5,
                reason=f"LOW ALPHA: WR={wr:.1%} PF={pf:.2f} Sharpe={sharpe:.2f} — reduced sizing"
            )

    def get_all_clusters(self) -> dict[str, ClusterStats]:
        return dict(self._clusters)

    def get_top_clusters(self, n: int = 5) -> list[tuple[str, ClusterStats]]:
        """Return the top-N clusters by alpha score."""
        scored = []
        for cid, c in self._clusters.items():
            if c.total_trades >= self.MIN_TRADES_FOR_SIGNAL:
                decision = self.evaluate_cluster(cid)
                scored.append((cid, c, decision.alpha_score))
        scored.sort(key=lambda x: x[2], reverse=True)
        return [(cid, c) for cid, c, _ in scored[:n]]

    def summary(self) -> dict:
        result = {}
        for cid, c in self._clusters.items():
            if c.total_trades > 0:
                decision = self.evaluate_cluster(cid)
                result[cid] = {
                    "trades": c.total_trades,
                    "win_rate": round(c.win_rate, 3),
                    "profit_factor": round(c.profit_factor, 2),
                    "sharpe": round(c.sharpe, 2),
                    "alpha_score": round(decision.alpha_score, 2),
                    "tradeable": decision.is_tradeable,
                    "kelly_mult": decision.kelly_multiplier,
                }
        return result

    def _load(self):
        if CLUSTER_STATE_FILE.exists():
            try:
                raw = json.loads(CLUSTER_STATE_FILE.read_text())
                for cid, data in raw.items():
                    self._clusters[cid] = ClusterStats(
                        cluster_id=cid,
                        total_trades=data.get("total_trades", 0),
                        wins=data.get("wins", 0),
                        total_pnl=data.get("total_pnl", 0.0),
                        gross_wins=data.get("gross_wins", 0.0),
                        gross_losses=data.get("gross_losses", 0.0),
                        pnl_history=data.get("pnl_history", []),
                        last_updated=data.get("last_updated", 0.0),
                    )
                logger.info(f"AlphaCluster: loaded {len(self._clusters)} clusters")
            except Exception as e:
                logger.warning(f"AlphaCluster: could not load state: {e}")

    def _save(self):
        try:
            data = {}
            for cid, c in self._clusters.items():
                data[cid] = {
                    "total_trades": c.total_trades,
                    "wins": c.wins,
                    "total_pnl": c.total_pnl,
                    "gross_wins": c.gross_wins,
                    "gross_losses": c.gross_losses,
                    "pnl_history": c.pnl_history[-self._max_history:],
                    "last_updated": c.last_updated,
                }
            CLUSTER_STATE_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning(f"AlphaCluster: could not save state: {e}")
