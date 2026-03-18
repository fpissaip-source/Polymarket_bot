"""
Edge Model
==========
Determines whether a detected dislocation represents a real mathematical
advantage after all costs are taken into account.

EV_net = q - p - c
  q = internal model probability
  p = market price
  c = fees + spread + slippage + incomplete execution risk

Only trades where EV_net > MIN_EDGE are considered.

Two types of edge on Polymarket:
  1. Within-market: p_yes + p_no < 1  →  Edge = 1 - (p_yes + p_no) - c
  2. Between related markets: price gap covers fees + spread + exec risk
"""

from dataclasses import dataclass
from config import MIN_EDGE, TOTAL_COST


@dataclass
class EdgeResult:
    has_edge: bool
    ev_net: float
    edge_type: str      # "directional" | "within_market" | "cross_market"
    side: str           # "YES" | "NO" | "BOTH"
    description: str


class EdgeModel:
    def __init__(self, min_edge: float = MIN_EDGE, cost: float = TOTAL_COST):
        self.min_edge = min_edge
        self.cost = cost

    def evaluate_directional(self, q: float, p: float) -> EdgeResult:
        """
        Single-market directional edge.
        q = our model probability, p = current market price.
        EV_net = q - p - c
        """
        ev_net = q - p - self.cost
        has_edge = ev_net > self.min_edge

        if q > p:
            side = "YES"
        elif q < p:
            # Could buy NO at (1-p), our prob of NO is (1-q)
            # EV for NO: (1-q) - (1-p) - c = p - q - c
            ev_net_no = p - q - self.cost
            if ev_net_no > self.min_edge:
                return EdgeResult(
                    has_edge=True,
                    ev_net=ev_net_no,
                    edge_type="directional",
                    side="NO",
                    description=f"Buy NO: EV_net={ev_net_no:.4f} (q={q:.3f}, p={p:.3f}, c={self.cost:.3f})"
                )
            side = "NO"
        else:
            side = "NONE"

        return EdgeResult(
            has_edge=has_edge,
            ev_net=ev_net,
            edge_type="directional",
            side=side if has_edge else "NONE",
            description=f"EV_net={ev_net:.4f} (q={q:.3f}, p={p:.3f}, c={self.cost:.3f})"
        )

    def evaluate_within_market(self, p_yes: float, p_no: float) -> EdgeResult:
        """
        Within-market arbitrage: buy both YES and NO if p_yes + p_no < 1.
        Edge = 1 - (p_yes + p_no) - c
        """
        total = p_yes + p_no
        edge = 1.0 - total - self.cost
        has_edge = edge > self.min_edge

        return EdgeResult(
            has_edge=has_edge,
            ev_net=edge,
            edge_type="within_market",
            side="BOTH",
            description=(
                f"Within-market arb: 1-({p_yes:.3f}+{p_no:.3f})-{self.cost:.3f}={edge:.4f}"
            )
        )

    def evaluate_cross_market(
        self,
        p1: float,
        p2: float,
        market1_id: str,
        market2_id: str,
    ) -> EdgeResult:
        """
        Cross-market arbitrage between two related markets.
        If p1 >> p2 (abnormally): sell p1, buy p2.
        Edge = (p1 - p2) - c (need to cover both legs of the trade)
        """
        spread = p1 - p2
        edge = abs(spread) - self.cost * 2  # cost applies to both legs
        has_edge = edge > self.min_edge

        if spread > 0:
            side = f"SELL {market1_id} / BUY {market2_id}"
        else:
            side = f"BUY {market1_id} / SELL {market2_id}"

        return EdgeResult(
            has_edge=has_edge,
            ev_net=edge,
            edge_type="cross_market",
            side=side,
            description=(
                f"Cross-market arb: |{p1:.3f}-{p2:.3f}|-2*{self.cost:.3f}={edge:.4f}"
            )
        )
