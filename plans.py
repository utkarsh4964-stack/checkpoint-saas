"""
Plan tiers for the hosted product.

This is the single place that defines what a user gets for what they
pay. `backend.config.settings` still defines the *global* MVP ceiling
(via CHECKPOINT_MAX_ACTIVE_SESSIONS etc.) — per-plan limits here are
capped at that ceiling so a Stripe price change can never grant more
than the process-local runtime registry can safely hold (see the
"Deliberate scale boundary" note in the README).
"""
from __future__ import annotations

from dataclasses import dataclass

from backend.config import settings
from backend.models.schemas import Plan


@dataclass(frozen=True)
class PlanLimits:
    max_active_sessions: int
    max_actions_per_session: int
    label: str


PLAN_LIMITS: dict[Plan, PlanLimits] = {
    Plan.FREE: PlanLimits(
        max_active_sessions=1,
        max_actions_per_session=25,
        label="Free",
    ),
    Plan.PRO: PlanLimits(
        max_active_sessions=min(3, settings.MAX_ACTIVE_SESSIONS_PER_USER),
        max_actions_per_session=min(100, settings.MAX_ACTIONS_PER_SESSION),
        label="Pro",
    ),
    Plan.TEAM: PlanLimits(
        max_active_sessions=min(10, settings.MAX_ACTIVE_SESSIONS_PER_USER),
        max_actions_per_session=min(500, settings.MAX_ACTIONS_PER_SESSION),
        label="Team",
    ),
}

# Maps a Stripe Price ID (from the Dashboard) back to the internal Plan
# it should unlock. Populated from settings so it stays in one place.
PRICE_TO_PLAN: dict[str, Plan] = {}
if settings.STRIPE_PRICE_PRO:
    PRICE_TO_PLAN[settings.STRIPE_PRICE_PRO] = Plan.PRO
if settings.STRIPE_PRICE_TEAM:
    PRICE_TO_PLAN[settings.STRIPE_PRICE_TEAM] = Plan.TEAM


def limits_for(plan: Plan) -> PlanLimits:
    return PLAN_LIMITS.get(plan, PLAN_LIMITS[Plan.FREE])
