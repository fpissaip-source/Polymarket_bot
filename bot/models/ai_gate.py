"""
AI Gatekeeper (KI-Gatter)
==========================
Implements the "Gate" concept from the document: AI acts as a filter
and reasoning layer between signal generation and order execution.

The AI Gate does NOT act as an oracle. It is a noise filter that:
1. Checks divergence between primary data sources and market price
2. Estimates resolution probability based on historical patterns
3. Computes a position-size multiplier based on confidence level
4. Blocks trades below a confidence threshold (default: 0.62)

Filter Layers (from document):
  1. Data-Logging    → Bias elimination, identify inefficiencies
  2. Market-Clustering → Alpha focus, increase base win rate
  3. Confidence-Gate → Noise suppression (P < 0.62 → skip)
  4. AI-Divergence-Check → Arbitrage detection, maximize edge

By skipping trades with <62% confidence, ~40% of bad entries are
eliminated before any capital is risked.
"""

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class GateDecision:
    approved: bool
    confidence: float               # 0.0 - 1.0
    position_multiplier: float      # scale factor for position size
    divergence_score: float         # how much sources disagree with market
    reasoning: str
    filters_passed: list            # which filter layers passed
    filters_failed: list            # which filter layers failed


class AIGate:
    """
    Multi-layer filter that must approve every trade before execution.

    The gate combines multiple confidence signals into a single
    pass/fail decision with a position-size multiplier.

    Usage:
        gate = AIGate()
        decision = gate.evaluate(
            q_model=0.72,           # Bayesian model probability
            q_gemini=0.68,          # Gemini LLM probability
            gemini_confidence=0.75, # Gemini's self-reported confidence
            market_price=0.60,      # Current market price
            cluster_alpha=0.7,      # Alpha cluster score
            regime_multiplier=1.0,  # Regime model multiplier
        )
        if decision.approved:
            position_size *= decision.position_multiplier
    """

    # Minimum confidence to approve a trade
    CONFIDENCE_THRESHOLD = 0.62

    # Minimum divergence (difference between our estimate and market price)
    MIN_DIVERGENCE = 0.03       # 3% minimum divergence required

    # Weight for each confidence signal
    WEIGHT_MODEL = 0.25         # Bayesian model weight
    WEIGHT_GEMINI = 0.35        # Gemini LLM weight (highest - it has live data)
    WEIGHT_GEMINI_CONF = 0.20   # Gemini's own confidence rating
    WEIGHT_CLUSTER = 0.20       # Alpha cluster historical performance

    def __init__(self, confidence_threshold: float = CONFIDENCE_THRESHOLD):
        self.confidence_threshold = confidence_threshold
        self._stats = {
            "total_evaluated": 0,
            "approved": 0,
            "blocked": 0,
            "blocked_reasons": {},
        }

    def evaluate(
        self,
        q_model: float,
        q_gemini: float | None,
        gemini_confidence: float,
        market_price: float,
        cluster_alpha: float = 0.5,
        regime_multiplier: float = 1.0,
        edge_ev: float = 0.0,
    ) -> GateDecision:
        """
        Run all filter layers and produce a pass/fail gate decision.
        """
        self._stats["total_evaluated"] += 1
        filters_passed = []
        filters_failed = []

        # Layer 1: Determine best probability estimate
        if q_gemini is not None and gemini_confidence >= 0.50:
            # Blend Bayesian model with Gemini (Gemini weighted higher)
            q_blended = 0.35 * q_model + 0.65 * q_gemini
        else:
            q_blended = q_model

        # Layer 2: Divergence Check
        # How much does our estimate diverge from the market?
        divergence = abs(q_blended - market_price)
        if divergence >= self.MIN_DIVERGENCE:
            filters_passed.append("divergence")
        else:
            filters_failed.append(f"divergence({divergence:.3f}<{self.MIN_DIVERGENCE})")

        # Layer 3: Confidence Gate
        # Combine all confidence signals into a composite score
        signals = []

        # Model confidence: how far from 0.5 is the Bayesian estimate?
        model_conf = min(1.0, abs(q_model - 0.5) * 2 + 0.3)
        signals.append(("model", model_conf, self.WEIGHT_MODEL))

        # Gemini confidence
        if q_gemini is not None:
            signals.append(("gemini", min(1.0, gemini_confidence), self.WEIGHT_GEMINI_CONF))
            # Gemini probability strength
            gemini_strength = min(1.0, abs(q_gemini - 0.5) * 2 + 0.3)
            signals.append(("gemini_prob", gemini_strength, self.WEIGHT_GEMINI))
        else:
            # Without Gemini, redistribute weight to model
            signals.append(("model_extra", model_conf, self.WEIGHT_GEMINI + self.WEIGHT_GEMINI_CONF))

        # Cluster alpha score
        signals.append(("cluster", cluster_alpha, self.WEIGHT_CLUSTER))

        # Compute weighted confidence
        total_weight = sum(w for _, _, w in signals)
        composite_confidence = sum(v * w for _, v, w in signals) / total_weight if total_weight > 0 else 0.0

        if composite_confidence >= self.confidence_threshold:
            filters_passed.append("confidence")
        else:
            filters_failed.append(f"confidence({composite_confidence:.3f}<{self.confidence_threshold})")

        # Layer 4: Edge validation
        # Positive EV is mandatory
        if edge_ev > 0:
            filters_passed.append("positive_ev")
        else:
            filters_failed.append(f"negative_ev({edge_ev:.4f})")

        # Layer 5: Regime filter
        # In high-vol regime, require higher confidence
        effective_threshold = self.confidence_threshold
        if regime_multiplier < 0.75:
            effective_threshold = min(0.80, self.confidence_threshold + 0.10)
            if composite_confidence < effective_threshold:
                filters_failed.append(f"regime_elevated({composite_confidence:.3f}<{effective_threshold})")
            else:
                filters_passed.append("regime_check")
        else:
            filters_passed.append("regime_check")

        # Final decision
        approved = len(filters_failed) == 0

        # Position multiplier: scale based on confidence
        if approved:
            # Higher confidence → larger position (up to 1.5x)
            confidence_boost = (composite_confidence - self.confidence_threshold) / (1.0 - self.confidence_threshold)
            position_multiplier = 0.7 + 0.8 * confidence_boost  # range: 0.7 - 1.5
            position_multiplier *= regime_multiplier
            position_multiplier = max(0.3, min(1.5, position_multiplier))
        else:
            position_multiplier = 0.0

        if approved:
            self._stats["approved"] += 1
        else:
            self._stats["blocked"] += 1
            reason_key = filters_failed[0] if filters_failed else "unknown"
            self._stats["blocked_reasons"][reason_key] = self._stats["blocked_reasons"].get(reason_key, 0) + 1

        reasoning_parts = []
        if q_gemini is not None:
            reasoning_parts.append(f"q_blend={q_blended:.3f}(model={q_model:.3f},gemini={q_gemini:.3f})")
        else:
            reasoning_parts.append(f"q_model={q_model:.3f}")
        reasoning_parts.append(f"market={market_price:.3f}")
        reasoning_parts.append(f"div={divergence:.3f}")
        reasoning_parts.append(f"conf={composite_confidence:.3f}")

        return GateDecision(
            approved=approved,
            confidence=composite_confidence,
            position_multiplier=position_multiplier,
            divergence_score=divergence,
            reasoning=" | ".join(reasoning_parts),
            filters_passed=filters_passed,
            filters_failed=filters_failed,
        )

    def get_stats(self) -> dict:
        total = self._stats["total_evaluated"]
        return {
            "total_evaluated": total,
            "approved": self._stats["approved"],
            "blocked": self._stats["blocked"],
            "approval_rate": round(self._stats["approved"] / total, 3) if total > 0 else 0.0,
            "block_rate": round(self._stats["blocked"] / total, 3) if total > 0 else 0.0,
            "top_block_reasons": dict(
                sorted(self._stats["blocked_reasons"].items(), key=lambda x: x[1], reverse=True)[:5]
            ),
        }
