"""
Deterministic risk rules.

Each rule is a plain function: (action, diff) -> RiskFinding | None.
`diff` is None during the pre-execution pass (we haven't run anything
yet) and populated during the post-execution pass. Rules that only need
the proposed action (secret/destructive/sensitive-path/scope) work in
both passes; the bulk-modification rule only fires post-execution, once
we know how many files were actually touched.

This is intentionally simple pattern matching, per the spec: no ML model.
"""
from __future__ import annotations

import re
from typing import Optional

from backend.models.schemas import ActionType, AgentAction, DiffResult, RiskFinding

SECRET_PATTERNS = [
    r"\.env$", r"\.env\.\w+$", r"credentials\.json$", r"id_rsa$", r"\.pem$", r"secrets?\.\w+$",
]

DESTRUCTIVE_KEYWORDS = ["rm ", "rm -rf", "delete", "drop", "truncate", "format"]

SENSITIVE_PATH_PREFIXES = ["/root", "/etc", "/home", "~/.ssh"]

BULK_THRESHOLD = 20

# Severities tuned so that: a secret-file delete or an intent-mismatched
# destructive command alone clears the DANGEROUS threshold (>70) on the
# PRE-execution pass (blocking before anything runs), while a bulk
# delete that evades keyword matching still clears DANGEROUS on the
# POST-execution pass once the diff reveals the real blast radius.
SEV_SECRET_ACCESS = 50
SEV_DESTRUCTIVE = 35
SEV_BULK = 40
SEV_SENSITIVE_DIR = 40
SEV_SCOPE_VIOLATION = 40


def _make(action: AgentAction, rule: str, severity: int, message: str, confidence: float = 1.0) -> RiskFinding:
    return RiskFinding(action_id=action.id, rule=rule, severity=severity, message=message, confidence=confidence)


def rule_secret_access(action: AgentAction, diff: Optional[DiffResult]) -> Optional[RiskFinding]:
    """RULE 1 — secret access. Checks the target path and any diff paths."""
    candidates = [action.target] if action.target else []
    if diff:
        candidates += diff.files_added + diff.files_removed + diff.files_modified
    for path in candidates:
        if not path:
            continue
        for pattern in SECRET_PATTERNS:
            if re.search(pattern, path, re.IGNORECASE):
                return _make(
                    action, "secret_access", SEV_SECRET_ACCESS,
                    f"Target '{path}' matches a likely-secret file pattern ({pattern})",
                )
    return None


def rule_destructive_operation(action: AgentAction, diff: Optional[DiffResult]) -> Optional[RiskFinding]:
    """RULE 2 — destructive operation. Checks action type and raw command payload."""
    if action.type == ActionType.FILE_DELETE:
        return _make(action, "destructive_operation", SEV_DESTRUCTIVE, "Action is a file/directory delete")
    command = str(action.parameters.get("command", ""))
    for kw in DESTRUCTIVE_KEYWORDS:
        if kw in command.lower():
            return _make(
                action, "destructive_operation", SEV_DESTRUCTIVE,
                f"Shell command contains destructive keyword '{kw.strip()}'",
            )
    return None


def rule_bulk_modification(action: AgentAction, diff: Optional[DiffResult]) -> Optional[RiskFinding]:
    """RULE 3 — bulk modification. Post-execution only: needs the real diff."""
    if diff is None:
        return None
    if diff.total_files_touched > BULK_THRESHOLD:
        return _make(
            action, "bulk_modification", SEV_BULK,
            f"{diff.total_files_touched} files touched in one action (threshold: {BULK_THRESHOLD})",
        )
    return None


def rule_sensitive_directory(action: AgentAction, diff: Optional[DiffResult]) -> Optional[RiskFinding]:
    """RULE 4 — sensitive directories.

    Check both explicit targets and shell command text. Shell actions often
    have no single target, so only inspecting ``action.target`` would miss
    commands such as ``rm -rf /etc/...``.
    """
    candidates = [action.target] if action.target else []
    command = str(action.parameters.get("command", ""))
    if command:
        candidates.append(command)
    if diff:
        candidates += diff.files_added + diff.files_removed + diff.files_modified
    for path in candidates:
        if not path:
            continue
        normalized = str(path).replace("\\", "/")
        for prefix in SENSITIVE_PATH_PREFIXES:
            if prefix in normalized:
                return _make(
                    action, "sensitive_directory", SEV_SENSITIVE_DIR,
                    f"Action references sensitive path '{prefix}'",
                )
    return None


def rule_scope_violation(action: AgentAction, diff: Optional[DiffResult], task_description: str) -> Optional[RiskFinding]:
    """
    RULE 5 — scope violation via intent/actual mismatch.

    Two detection paths, both deterministic (no LLM call — see
    risk/engine.py for where an LLM escalation would slot in for
    genuinely ambiguous cases):

      1. Keyword-based, pre- or post-execution: action type/command
         text is destructive but intent doesn't mention removal. Catches
         the killer-demo case (intent: "organize the reports folder",
         actual: recursive delete) before it ever runs.

      2. Diff-based, post-execution only: even when the command text
         didn't match any destructive keyword (e.g. a Python one-liner
         calling os.remove() in a loop, which slips past the keyword
         check), a diff showing files actually removed while intent
         says nothing about removal is still a real mismatch. This is
         what catches the "evaded pre-check" scenario in the demo.
    """
    intent_lower = action.intent.lower()
    removal_language = any(w in intent_lower for w in ["remove", "delete", "clean", "clear", "purge"])

    keyword_destructive = action.type == ActionType.FILE_DELETE or any(
        kw in str(action.parameters.get("command", "")).lower() for kw in DESTRUCTIVE_KEYWORDS
    )
    if keyword_destructive and not removal_language:
        return _make(
            action, "scope_violation", SEV_SCOPE_VIOLATION,
            f"Action is destructive but stated intent ('{action.intent}') does not mention removal — "
            f"possible intent/actual-action mismatch",
        )

    # A move naturally shows up as "removed old path + added new path" in
    # the diff — that's not a real removal, so don't flag it as one.
    if diff and diff.files_removed and not removal_language and action.type != ActionType.FILE_MOVE:
        return _make(
            action, "scope_violation", SEV_SCOPE_VIOLATION,
            f"Diff shows {len(diff.files_removed)} file(s) removed but stated intent "
            f"('{action.intent}') does not mention removal — possible intent/actual-action mismatch",
        )
    return None
