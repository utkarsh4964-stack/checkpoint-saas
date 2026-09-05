"""
Risk engine.

Runs deterministic rules and produces a normalized 0-100 score. Exposes
two entry points, matching the pre/post-execution split fixed in the
architecture review:

  - assess_pre_execution(): called BEFORE the action runs. Only has the
    proposed action (type, target, parameters, intent) — no diff yet.
    This is what allows `secrets.env` to be BLOCKED before deletion.

  - assess_post_execution(): called AFTER the action runs. Has the real
    diff, so it catches things the pre-check couldn't know (e.g. "agent
    said it'd touch 3 files, diff shows 37").

Both return the same RiskResult shape so callers (checkpoint_manager,
API) don't need to branch on which pass produced it.
"""
from __future__ import annotations

from typing import Optional

from backend.models.schemas import AgentAction, DiffResult, RiskFinding, RiskResult, RiskTier
from backend.risk import rules

SAFE_MAX = 30
SUSPICIOUS_MAX = 70


def _tier_for(score: int) -> RiskTier:
    if score <= SAFE_MAX:
        return RiskTier.SAFE
    if score <= SUSPICIOUS_MAX:
        return RiskTier.SUSPICIOUS
    return RiskTier.DANGEROUS


def _run_rules(action: AgentAction, diff: Optional[DiffResult], task_description: str) -> list[RiskFinding]:
    findings: list[RiskFinding] = []
    for finding in (
        rules.rule_secret_access(action, diff),
        rules.rule_destructive_operation(action, diff),
        rules.rule_bulk_modification(action, diff),
        rules.rule_sensitive_directory(action, diff),
        rules.rule_scope_violation(action, diff, task_description),
    ):
        if finding:
            findings.append(finding)
    return findings


def _score(findings: list[RiskFinding]) -> int:
    # Sum severities, weighted by confidence, capped at 100.
    raw = sum(f.severity * f.confidence for f in findings)
    return min(100, round(raw))


def assess_pre_execution(action: AgentAction, task_description: str = "") -> RiskResult:
    """
    Static check on the proposed action, before it touches the runtime.
    No diff is available yet, so bulk-modification never fires here —
    that's expected and correct.
    """
    findings = _run_rules(action, diff=None, task_description=task_description)
    score = _score(findings)
    return RiskResult(score=score, tier=_tier_for(score), findings=findings)


def assess_post_execution(action: AgentAction, diff: DiffResult, task_description: str = "") -> RiskResult:
    """Full check once the action has actually run and we have a real diff."""
    findings = _run_rules(action, diff=diff, task_description=task_description)
    score = _score(findings)
    return RiskResult(score=score, tier=_tier_for(score), findings=findings)
