"""
Multi-Agent Portfolio Manager
==============================
Implements the document's multi-agent trading firm architecture.

The bot is no longer a single script — it operates as a team of specialized agents:

  1. Analyst Team     → Transforms raw data into analysis reports
  2. Researcher Team  → Bullish & bearish researchers debate for balanced view
  3. Risk Manager     → Monitors portfolio exposure and drawdown limits
  4. Portfolio Manager → Final decision authority: approve/reject/size trades

This module implements the Portfolio Manager as the top-level coordinator.
It consumes outputs from all other components and makes the final
approve/reject decision for every trade proposal.

Key principle: Decisions are NOT based on a single signal. They are based
on structured consensus from multiple agents, which significantly
improves the Sharpe ratio.
"""

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class TradeProposal:
    """A trade proposal that the Portfolio Manager must approve or reject."""
    market_id: str
    side: str                   # "YES" or "NO"
    proposed_size: float        # USD amount
    entry_price: float          # target price
    edge_ev: float              # expected value
    q_estimate: float           # probability estimate
    market_price: float         # current market price

    # Agent signals
    analyst_score: float = 0.0          # -1 to +1 (bearish to bullish)
    gemini_probability: float | None = None
    gemini_confidence: float = 0.0
    gate_confidence: float = 0.0        # AI gate composite confidence
    gate_approved: bool = False
    cluster_alpha: float = 0.5          # alpha cluster score
    cluster_tradeable: bool = True
    regime_multiplier: float = 1.0
    drawdown_multiplier: float = 1.0    # from risk-constrained Kelly
    sharpe_current: float = 0.0         # current portfolio Sharpe

    # Context
    current_exposure_pct: float = 0.0   # % of bankroll at risk
    open_positions: int = 0
    max_positions: int = 10
    bankroll: float = 0.0
    end_time: float = 0.0


@dataclass
class PortfolioDecision:
    """Final decision from the Portfolio Manager."""
    approved: bool
    final_size: float           # adjusted position size
    final_price: float          # adjusted entry price
    risk_score: float           # 0-1, higher = more risk
    reasoning: str
    vetoed_by: str | None = None  # which agent vetoed (if rejected)


class RiskManager:
    """
    Risk Management Agent: monitors portfolio exposure and enforces limits.

    Continuously tracks:
      - Total portfolio exposure vs max allowed
      - Drawdown levels
      - Correlation between open positions
      - Single-position concentration
    """

    MAX_SINGLE_POSITION_PCT = 0.15      # No single position > 15% of bankroll
    MAX_TOTAL_EXPOSURE_PCT = 0.85       # Max 85% of bankroll at risk
    MAX_CORRELATED_EXPOSURE_PCT = 0.30  # Max 30% in correlated markets
    DRAWDOWN_HALT_THRESHOLD = 0.35      # Stop trading at 35% drawdown

    def evaluate(self, proposal: TradeProposal) -> tuple[bool, float, str]:
        """
        Evaluate a trade proposal from a risk perspective.

        Returns (approved, risk_multiplier, reason)
        """
        reasons = []

        # Check 1: Maximum total exposure
        new_exposure = proposal.current_exposure_pct + (proposal.proposed_size / proposal.bankroll if proposal.bankroll > 0 else 1.0)
        if new_exposure > self.MAX_TOTAL_EXPOSURE_PCT:
            return False, 0.0, f"Total exposure {new_exposure:.1%} > {self.MAX_TOTAL_EXPOSURE_PCT:.0%} limit"

        # Check 2: Maximum positions
        if proposal.open_positions >= proposal.max_positions:
            return False, 0.0, f"Max positions reached ({proposal.open_positions}/{proposal.max_positions})"

        # Check 3: Single position concentration
        position_pct = proposal.proposed_size / proposal.bankroll if proposal.bankroll > 0 else 1.0
        if position_pct > self.MAX_SINGLE_POSITION_PCT:
            # Don't reject — reduce size to fit limit
            adjusted_size = proposal.bankroll * self.MAX_SINGLE_POSITION_PCT
            reasons.append(f"Position reduced {proposal.proposed_size:.2f}→{adjusted_size:.2f} (concentration limit)")
            proposal.proposed_size = adjusted_size

        # Check 4: Drawdown halt
        if proposal.drawdown_multiplier < 0.25:
            return False, 0.0, f"Drawdown halt: multiplier={proposal.drawdown_multiplier:.2f} — survival mode"

        # Check 5: Risk score based on edge quality
        risk_score = self._compute_risk_score(proposal)
        risk_multiplier = 1.0

        if risk_score > 0.7:
            risk_multiplier = 0.6
            reasons.append(f"High risk ({risk_score:.2f}) — sizing reduced 40%")
        elif risk_score > 0.5:
            risk_multiplier = 0.8
            reasons.append(f"Moderate risk ({risk_score:.2f}) — sizing reduced 20%")

        reason_str = " | ".join(reasons) if reasons else "Risk within limits"
        return True, risk_multiplier, reason_str

    def _compute_risk_score(self, proposal: TradeProposal) -> float:
        """
        Compute overall risk score (0=safe, 1=dangerous).
        """
        score = 0.0

        # Low edge → higher risk
        if proposal.edge_ev < 0.05:
            score += 0.3
        elif proposal.edge_ev < 0.08:
            score += 0.15

        # Low confidence → higher risk
        if proposal.gate_confidence < 0.65:
            score += 0.2

        # High exposure → higher risk
        if proposal.current_exposure_pct > 0.60:
            score += 0.2

        # Low Sharpe → system not performing well
        if proposal.sharpe_current < 0.5:
            score += 0.15

        # Volatile regime → higher risk
        if proposal.regime_multiplier < 0.75:
            score += 0.15

        return min(1.0, score)


class PortfolioManager:
    """
    Final decision authority for all trades.

    Consumes outputs from:
      - Edge model (EV)
      - AI Gate (confidence, divergence)
      - Alpha Cluster (cluster quality)
      - Risk Manager (exposure, drawdown)
      - Sharpe Tracker (portfolio performance)

    Makes the final approve/reject decision with adjusted sizing.
    """

    def __init__(self):
        self.risk_manager = RiskManager()
        self._stats = {
            "proposals": 0,
            "approved": 0,
            "rejected": 0,
            "reject_reasons": {},
        }

    def decide(self, proposal: TradeProposal) -> PortfolioDecision:
        """
        Make the final trading decision.

        This is the "last instance" that approves or rejects based on
        a holistic evaluation of risk-return parameters.
        """
        self._stats["proposals"] += 1

        # Step 1: AI Gate must approve
        if not proposal.gate_approved:
            return self._reject(proposal, "ai_gate", "AI Gate rejected: low confidence or divergence")

        # Step 2: Alpha Cluster must be tradeable
        if not proposal.cluster_tradeable:
            return self._reject(proposal, "alpha_cluster", f"Cluster blocked (alpha={proposal.cluster_alpha:.2f})")

        # Step 3: Risk Manager check
        risk_approved, risk_mult, risk_reason = self.risk_manager.evaluate(proposal)
        if not risk_approved:
            return self._reject(proposal, "risk_manager", risk_reason)

        # Step 4: Compute final position size
        # Start with proposed size, apply all multipliers
        final_size = proposal.proposed_size
        final_size *= proposal.drawdown_multiplier    # Drawdown protection
        final_size *= risk_mult                        # Risk manager adjustment

        # Minimum viable trade
        if final_size < 0.50:
            return self._reject(proposal, "min_size", f"Final size ${final_size:.2f} < $0.50 minimum")

        # Step 5: Build reasoning
        reasoning_parts = [
            f"EV={proposal.edge_ev:.4f}",
            f"gate_conf={proposal.gate_confidence:.3f}",
            f"cluster_alpha={proposal.cluster_alpha:.2f}",
            f"dd_mult={proposal.drawdown_multiplier:.2f}",
            f"risk_mult={risk_mult:.2f}",
        ]
        if risk_reason != "Risk within limits":
            reasoning_parts.append(risk_reason)

        self._stats["approved"] += 1

        risk_score = self.risk_manager._compute_risk_score(proposal)

        decision = PortfolioDecision(
            approved=True,
            final_size=round(final_size, 2),
            final_price=proposal.entry_price,
            risk_score=round(risk_score, 3),
            reasoning=" | ".join(reasoning_parts),
        )

        logger.info(
            f"[PORTFOLIO] APPROVED {proposal.market_id}: "
            f"${proposal.proposed_size:.2f}→${final_size:.2f} | "
            f"{decision.reasoning}"
        )

        return decision

    def _reject(self, proposal: TradeProposal, vetoed_by: str, reason: str) -> PortfolioDecision:
        self._stats["rejected"] += 1
        self._stats["reject_reasons"][vetoed_by] = self._stats["reject_reasons"].get(vetoed_by, 0) + 1

        logger.info(
            f"[PORTFOLIO] REJECTED {proposal.market_id}: "
            f"vetoed_by={vetoed_by} | {reason}"
        )

        return PortfolioDecision(
            approved=False,
            final_size=0.0,
            final_price=proposal.entry_price,
            risk_score=1.0,
            reasoning=reason,
            vetoed_by=vetoed_by,
        )

    def get_stats(self) -> dict:
        total = self._stats["proposals"]
        return {
            "total_proposals": total,
            "approved": self._stats["approved"],
            "rejected": self._stats["rejected"],
            "approval_rate": round(self._stats["approved"] / total, 3) if total > 0 else 0.0,
            "reject_reasons": dict(
                sorted(self._stats["reject_reasons"].items(), key=lambda x: x[1], reverse=True)
            ),
        }
