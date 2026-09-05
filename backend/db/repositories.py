"""
Repository functions: translate between Pydantic models and SQLite rows.

Kept as plain functions rather than a class hierarchy — there's one
runtime, one agent, one demo. Keep the repository layer intentionally small for the MVP.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from backend.db.database import dumps, loads, transaction
from backend.models.schemas import (
    ActionStatus,
    AgentAction,
    ActionType,
    Checkpoint,
    DiffResult,
    RiskFinding,
    RollbackEvent,
    RollbackTrigger,
    RuntimeType,
    Session,
    SessionStatus,
    User,
)


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

def create_user(user: User) -> User:
    with transaction() as conn:
        conn.execute(
            """INSERT INTO users (id, email, password_hash, created_at, is_active)
               VALUES (?, ?, ?, ?, ?)""",
            (user.id, user.email, user.password_hash, user.created_at.isoformat(), int(user.is_active)),
        )
    return user


def _row_to_user(row) -> User:
    return User(
        id=row["id"],
        email=row["email"],
        password_hash=row["password_hash"],
        created_at=datetime.fromisoformat(row["created_at"]),
        is_active=bool(row["is_active"]),
    )


def get_user(user_id: str) -> Optional[User]:
    with transaction() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_email(email: str) -> Optional[User]:
    with transaction() as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    return _row_to_user(row) if row else None


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

def create_session(session: Session) -> Session:
    with transaction() as conn:
        conn.execute(
            """INSERT INTO sessions (id, user_id, agent_id, runtime, status, started_at, ended_at, last_active_at, runtime_handle)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.id,
                session.user_id,
                session.agent_id,
                session.runtime.value,
                session.status.value,
                session.started_at.isoformat(),
                session.ended_at.isoformat() if session.ended_at else None,
                session.last_active_at.isoformat(),
                session.runtime_handle,
            ),
        )
    return session


def update_session(session: Session) -> None:
    with transaction() as conn:
        conn.execute(
            """UPDATE sessions SET status=?, ended_at=?, last_active_at=?, runtime_handle=? WHERE id=?""",
            (
                session.status.value,
                session.ended_at.isoformat() if session.ended_at else None,
                session.last_active_at.isoformat(),
                session.runtime_handle,
                session.id,
            ),
        )


def touch_session_activity(session_id: str, when: Optional[datetime] = None) -> None:
    ts = (when or datetime.now(timezone.utc)).isoformat()
    with transaction() as conn:
        conn.execute("UPDATE sessions SET last_active_at=? WHERE id=?", (ts, session_id))


def _row_to_session(row) -> Session:
    return Session(
        id=row["id"],
        user_id=row["user_id"],
        agent_id=row["agent_id"],
        runtime=RuntimeType(row["runtime"]),
        status=SessionStatus(row["status"]),
        started_at=datetime.fromisoformat(row["started_at"]),
        ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
        last_active_at=datetime.fromisoformat(row["last_active_at"]),
        runtime_handle=row["runtime_handle"],
    )


def get_session(session_id: str) -> Optional[Session]:
    with transaction() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        return None
    return _row_to_session(row)


def count_active_sessions(user_id: str, now: Optional[datetime] = None, idle_timeout_minutes: int = 30, max_lifetime_minutes: int = 120) -> int:
    """Count running sessions whose idle and lifetime leases have not elapsed."""
    current = now or datetime.now(timezone.utc)
    idle_cutoff = (current - timedelta(minutes=idle_timeout_minutes)).isoformat()
    lifetime_cutoff = (current - timedelta(minutes=max_lifetime_minutes)).isoformat()
    with transaction() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE user_id=? AND status=? AND ended_at IS NULL AND last_active_at > ? AND started_at > ?",
            (user_id, SessionStatus.RUNNING.value, idle_cutoff, lifetime_cutoff),
        ).fetchone()
    return int(row["c"])


def mark_all_running_sessions_expired() -> int:
    """A process restart destroys the in-memory runtime registry; mark those
    sessions expired immediately rather than waiting for a future sweep."""
    now = datetime.now(timezone.utc).isoformat()
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE sessions SET status=?, ended_at=?, last_active_at=? WHERE status=?",
            (SessionStatus.EXPIRED.value, now, now, SessionStatus.RUNNING.value),
        )
        return cur.rowcount


def list_running_sessions() -> list[Session]:
    """Used by the cleanup sweep — all sessions still marked RUNNING,
    across all users, regardless of which process created them."""
    with transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE status=?", (SessionStatus.RUNNING.value,)
        ).fetchall()
    return [_row_to_session(r) for r in rows]


def list_sessions_for_user(user_id: str) -> list[Session]:
    with transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE user_id=? ORDER BY started_at DESC", (user_id,)
        ).fetchall()
    return [_row_to_session(r) for r in rows]


# --------------------------------------------------------------------------
# Checkpoints
# --------------------------------------------------------------------------

def create_checkpoint(cp: Checkpoint) -> Checkpoint:
    with transaction() as conn:
        conn.execute(
            """INSERT INTO checkpoints (id, session_id, sequence, snapshot_id, created_at, note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (cp.id, cp.session_id, cp.sequence, cp.snapshot_id, cp.created_at.isoformat(), cp.note),
        )
    return cp


def get_checkpoint(checkpoint_id: str) -> Optional[Checkpoint]:
    with transaction() as conn:
        row = conn.execute("SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
    if not row:
        return None
    return Checkpoint(
        id=row["id"],
        session_id=row["session_id"],
        sequence=row["sequence"],
        snapshot_id=row["snapshot_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        note=row["note"],
    )


def next_checkpoint_sequence(session_id: str) -> int:
    with transaction() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence), -1) AS m FROM checkpoints WHERE session_id=?",
            (session_id,),
        ).fetchone()
    return row["m"] + 1


def list_checkpoints(session_id: str) -> list[Checkpoint]:
    with transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM checkpoints WHERE session_id=? ORDER BY sequence ASC", (session_id,)
        ).fetchall()
    return [
        Checkpoint(
            id=r["id"], session_id=r["session_id"], sequence=r["sequence"],
            snapshot_id=r["snapshot_id"], created_at=datetime.fromisoformat(r["created_at"]),
            note=r["note"],
        )
        for r in rows
    ]


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

def create_action(action: AgentAction) -> AgentAction:
    with transaction() as conn:
        conn.execute(
            """INSERT INTO actions
               (id, session_id, checkpoint_id, type, intent, target, parameters,
                reversible, status, risk_score, started_at, completed_at, diff)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                action.id, action.session_id, action.checkpoint_id, action.type.value,
                action.intent, action.target, dumps(action.parameters),
                int(action.reversible), action.status.value, action.risk_score,
                action.started_at.isoformat(),
                action.completed_at.isoformat() if action.completed_at else None,
                dumps(action.diff.model_dump()) if action.diff else None,
            ),
        )
    return action


def update_action(action: AgentAction) -> None:
    with transaction() as conn:
        conn.execute(
            """UPDATE actions SET status=?, risk_score=?, completed_at=?, diff=?, checkpoint_id=?
               WHERE id=?""",
            (
                action.status.value,
                action.risk_score,
                action.completed_at.isoformat() if action.completed_at else None,
                dumps(action.diff.model_dump()) if action.diff else None,
                action.checkpoint_id,
                action.id,
            ),
        )


def get_action(action_id: str) -> Optional[AgentAction]:
    with transaction() as conn:
        row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    if not row:
        return None
    return _row_to_action(row)


def count_actions(session_id: str) -> int:
    with transaction() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE session_id=?", (session_id,)).fetchone()
    return int(row["c"])


def list_actions(session_id: str) -> list[AgentAction]:
    with transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM actions WHERE session_id=? ORDER BY started_at ASC", (session_id,)
        ).fetchall()
    return [_row_to_action(r) for r in rows]


def _row_to_action(row) -> AgentAction:
    diff_data = loads(row["diff"])
    return AgentAction(
        id=row["id"], session_id=row["session_id"], checkpoint_id=row["checkpoint_id"],
        type=ActionType(row["type"]), intent=row["intent"], target=row["target"],
        parameters=loads(row["parameters"]) or {}, reversible=bool(row["reversible"]),
        status=ActionStatus(row["status"]), risk_score=row["risk_score"],
        started_at=datetime.fromisoformat(row["started_at"]),
        completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
        diff=DiffResult(**diff_data) if diff_data else None,
    )


# --------------------------------------------------------------------------
# Risk findings
# --------------------------------------------------------------------------

def create_findings(findings: list[RiskFinding]) -> None:
    if not findings:
        return
    with transaction() as conn:
        conn.executemany(
            """INSERT INTO risk_findings (id, action_id, rule, severity, message, confidence)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(f.id, f.action_id, f.rule, f.severity, f.message, f.confidence) for f in findings],
        )


def list_findings(action_id: str) -> list[RiskFinding]:
    with transaction() as conn:
        rows = conn.execute("SELECT * FROM risk_findings WHERE action_id=?", (action_id,)).fetchall()
    return [
        RiskFinding(
            id=r["id"], action_id=r["action_id"], rule=r["rule"],
            severity=r["severity"], message=r["message"], confidence=r["confidence"],
        )
        for r in rows
    ]


# --------------------------------------------------------------------------
# Rollback events
# --------------------------------------------------------------------------

def create_rollback_event(event: RollbackEvent) -> RollbackEvent:
    with transaction() as conn:
        conn.execute(
            """INSERT INTO rollback_events (id, session_id, checkpoint_id, trigger_type, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event.id, event.session_id, event.checkpoint_id, event.trigger.value,
             event.reason, event.created_at.isoformat()),
        )
    return event


def list_rollback_events(session_id: str) -> list[RollbackEvent]:
    with transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM rollback_events WHERE session_id=? ORDER BY created_at ASC", (session_id,)
        ).fetchall()
    return [
        RollbackEvent(
            id=r["id"], session_id=r["session_id"], checkpoint_id=r["checkpoint_id"],
            trigger=RollbackTrigger(r["trigger_type"]), reason=r["reason"],
            created_at=datetime.fromisoformat(r["created_at"]),
        )
        for r in rows
    ]
