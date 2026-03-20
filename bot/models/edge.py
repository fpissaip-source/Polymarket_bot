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
from config import (
    MIN_EDGE, TOTAL_COST,
    TOTAL_COST_MAKER, MIN_EDGE_MAKER,
    TOTAL_COST_TAKER, MIN_EDGE_TAKER,
)


@dataclass
class EdgeResult:
    has_edge: bool
    ev_net: float
    edge_type: str      # "directional" | "within_market" | "cross_market"
    side: str           # "YES" | "NO" | "BOTH"
    is_passive: bool    # True = maker limit order, False = taker market order
    description: str


class EdgeModel:
    def __init__(self, min_edge: float = MIN_EDGE, cost: float = TOTAL_COST):
        self.min_edge = min_edge
        self.cost = cost

    def evaluate_directional(self, q: float, p: float) -> EdgeResult:
        """
        Single-market directional edge. Tries maker first (low cost),
        falls back to taker (higher cost, requires bigger edge).
        """
        if q > p:
            side = "YES"
            ev_maker = q - p - TOTAL_COST_MAKER
            ev_taker = q - p - TOTAL_COST_TAKER
        elif q < p:
            side = "NO"
            ev_maker = p - q - TOTAL_COST_MAKER
            ev_taker = p - q - TOTAL_COST_TAKER
        else:
            return EdgeResult(False, 0.0, "directional", "NONE", False,
                              "No directional signal (q == p)")

        # Prefer maker (passive) — lower cost, lower required edge
        if ev_maker > MIN_EDGE_MAKER:
            return EdgeResult(
                has_edge=True, ev_net=ev_maker, edge_type="directional",
                side=side, is_passive=True,
                description=f"MAKER {side}: EV={ev_maker:.4f} (q={q:.3f}, p={p:.3f})"
            )
        if ev_taker > MIN_EDGE_TAKER:
            return EdgeResult(
                has_edge=True, ev_net=ev_taker, edge_type="directional",
                side=side, is_passive=False,
                description=f"TAKER {side}: EV={ev_taker:.4f} (q={q:.3f}, p={p:.3f})"
            )
        return EdgeResult(False, max(ev_maker, ev_taker), "directional", "NONE", False,
                          f"No edge: EV_maker={ev_maker:.4f}, EV_taker={ev_taker:.4f}")

    def evaluate_within_market(self, p_yes: float, p_no: float) -> EdgeResult:
        """
        Within-market arbitrage: buy both YES and NO if p_yes + p_no < 1.
        Uses maker cost (both legs as limit orders).
        """
        total = p_yes + p_no
        edge = 1.0 - total - TOTAL_COST_MAKER * 2  # two maker legs
        has_edge = edge > MIN_EDGE_MAKER

        return EdgeResult(
            has_edge=has_edge,
            ev_net=edge,
            edge_type="within_market",
            side="BOTH",
            is_passive=True,
            description=f"Within-market arb: 1-({p_yes:.3f}+{p_no:.3f})-2*{TOTAL_COST_MAKER:.3f}={edge:.4f}"
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
            is_passive=True,
            description=f"Cross-market arb: |{p1:.3f}-{p2:.3f}|-2*{TOTAL_COST_MAKER:.3f}={edge:.4f}"
        )
