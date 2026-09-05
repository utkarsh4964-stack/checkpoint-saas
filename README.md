# CHECKPOINT

**Git for AI agent actions.**

CHECKPOINT is a safety, observability, and recovery layer that sits between an autonomous AI agent and the sandboxed filesystem it is allowed to change.

Instead of trusting an agent after the fact, CHECKPOINT records the proposed action, evaluates risk before execution, creates a recovery checkpoint for mutating work, compares the real filesystem diff after execution, and surfaces dangerous results for human review.

## Architecture

```text
AI AGENT
   │
   ▼
CHECKPOINT SDK / TOOL INTERCEPTOR
   │
   ├── pre-execution risk check
   │       └── dangerous → BLOCK (never executes)
   │
   ├── checkpoint before mutation
   │
   ▼
SOLARI SANDBOX
   │
   ▼
EXECUTE
   │
   ▼
FILESYSTEM DIFF
   │
   ▼
post-execution risk check
   │
   ├── safe/suspicious → complete + audit
   └── dangerous → PAUSED
                         │
                    human decision
                    ┌────┴────┐
                 APPROVE   REJECT
                              │
                              ▼
                         RESTORE CHECKPOINT
                              │
                              ▼
                           TIMELINE
```

## Core capabilities

### 1. Pre-execution prevention

Deterministic rules inspect the proposed action before it reaches the runtime.

Examples:

- secret-file deletion
- destructive shell commands
- sensitive filesystem paths
- destructive actions that contradict the stated intent

A dangerous pre-check becomes `BLOCKED`: the runtime is never touched.

### 2. Risk-based checkpointing

Reads do not need filesystem snapshots. Mutating actions — writes, deletes, moves, directory creation, and shell execution — receive a checkpoint before execution.

### 3. Post-execution detection

The real before/after filesystem state is diffed. This catches behavior that static rules cannot reliably infer.

Example:

```text
Intent:  "Consolidate the bulk data into one report"
Actual:  Python code removes 25 files

Filesystem diff
      ↓
25 files removed
      ↓
Bulk modification + scope mismatch
      ↓
PAUSED
```

### 4. Human approval

A dangerous post-execution result becomes `PAUSED`. A human can approve the result or reject it and request recovery.

Approval is an acknowledgement of an already-executed post-check action; it does not silently re-run the action.

### 5. Transactional filesystem recovery

Rejecting a paused action restores the filesystem to the checkpoint taken immediately before that action.

This is intentionally scoped: CHECKPOINT provides **transactional recovery for supported sandbox filesystem state**, not universal rollback of arbitrary external side effects such as emails, payments, browser interactions, or third-party API calls.

### 6. Solari + local development runtime

The product has one runtime interface with two implementations:

- `SolariSandboxRuntime` — real Solari headless microVM sandbox.
- `LocalFallbackRuntime` — local-directory implementation for development and offline tests.

The local fallback is **not a security boundary** and is disabled for production deployments.

### 7. BYOK

Each production session supplies its own Solari API key.

The key is:

- accepted only for session creation
- held in live server memory for the runtime lifetime
- never persisted in SQLite/Postgres
- never returned by the API
- never stored in browser local/session storage
- never intentionally logged

The production API rejects session creation without a per-session Solari key.

## Risk model

| Score | Tier | Behavior |
|---:|---|---|
| 0–30 | SAFE | Execute and audit |
| 31–70 | SUSPICIOUS | Execute and audit |
| 71–100 | DANGEROUS | Block pre-execution when detectable; otherwise pause after execution |

Current deterministic rules:

| Rule | Severity | Example |
|---|---:|---|
| Secret access | 50 | `.env`, `credentials.json`, `id_rsa`, `.pem` |
| Destructive operation | 35 | delete action, `rm -rf`, `drop`, `truncate` |
| Bulk modification | 40 | more than 20 files touched |
| Sensitive directory | 40 | `/root`, `/etc`, `/home`, `~/.ssh` |
| Scope violation | 40 | destructive behavior inconsistent with intent |

Scores are capped at 100.

## Killer demo

The scripted Workspace Agent demonstrates the complete safety loop:

1. Safe changes execute normally.
2. `secrets.env` deletion is blocked before execution.
3. `rm -rf reports` with the intent `Organize the reports folder` is blocked before execution.
4. An evasive Python bulk deletion executes because the command text does not match the simple destructive-keyword rule.
5. The post-execution diff discovers 25 removed files and pauses the action.
6. Human rejection restores the pre-action checkpoint.
7. The demo verifies the 25 files are back.

The failure scenario is deliberately engineered for deterministic demonstration; it is not presented as spontaneous LLM behavior.

## Multi-user MVP

The hosted product adds:

- account registration/login
- scrypt password hashing
- JWT bearer authentication
- server-side tenant isolation
- per-user active-session quota
- per-session action quota
- idle timeout + maximum session lifetime
- startup cleanup of orphaned runtime sessions
- in-memory per-IP abuse limits
- friendly Solari failure messages
- security headers
- PostgreSQL support
- Docker + Render deployment configuration
- sessionStorage-only browser authentication state

### Deliberate scale boundary

The live runtime registry is process-local, so the hosted MVP runs **one Uvicorn worker and one service instance**. Do not increase worker/instance count until runtime handles are moved to a shared/reconnectable control plane.

The in-memory rate limiter also resets when the process restarts. Put a real edge/WAF/shared limiter in front of CHECKPOINT before scaling beyond the MVP.

## Python SDK

The SDK exposes a small HTTP client for agent processes:

```python
import os
from checkpoint import Checkpoint

checkpoint = Checkpoint(
    base_url="https://your-checkpoint.example.com",
    solari_api_key=os.environ["SOLARI_API_KEY"],
)
checkpoint.login("you@example.com", "your-password")

session = checkpoint.start_session(
    agent_id="workspace-agent",
    task_description="Clean up my project",
)

result = session.run(
    intent="Create a cleanup summary",
    type="file.write",
    target="summary.txt",
    parameters={"content": "Cleanup complete"},
)

print(result.status, result.risk_score)
session.end()
```

For production, pass the caller's Solari key to `Checkpoint(..., solari_api_key="...")`; the SDK keeps it only in the client process and sends it only when creating a session.

## API

| Endpoint | Purpose |
|---|---|
| `POST /auth/register` | Create account + bearer token |
| `POST /auth/login` | Authenticate |
| `GET /auth/me` | Current account |
| `GET /sessions` | List own sessions |
| `POST /sessions` | Start session + initial checkpoint |
| `GET /sessions/{id}` | Get own session |
| `POST /sessions/{id}/end` | End session |
| `GET /sessions/{id}/timeline` | Timeline, findings, diffs, rollbacks |
| `GET /sessions/{id}/checkpoints` | List own checkpoints |
| `GET /checkpoints/{id}` | Get own checkpoint |
| `POST /sessions/{id}/actions` | Intercept and execute an action |
| `GET /actions/{id}` | Get own action |
| `GET /actions/{id}/risk` | Risk findings |
| `POST /actions/{id}/approve` | Approve paused action |
| `POST /actions/{id}/reject` | Reject and optionally recover |
| `POST /sessions/{id}/rollback/{checkpoint_id}` | Manual recovery |
| `GET /health` | Health + database check |

All protected endpoints require:

```text
Authorization: Bearer <checkpoint-jwt>
```

Resources owned by another account return `404` rather than confirming that the resource exists.

## Run locally

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Run the API:

```powershell
python -m uvicorn backend.main:app --reload --port 8000
```

Then open:

```text
http://localhost:8000/app/
```

Local development defaults to SQLite and the local fallback runtime. No Solari key is required for the offline demo.

Run the deterministic demo:

```powershell
python -m examples.workspace_agent
```

Run the LLM tool loop with Groq:

```powershell
$env:GROQ_API_KEY="your-key"
python -m examples.llm_workspace_agent
```

## Production deployment

The repository includes:

- `Dockerfile`
- `render.yaml`
- `.env.example`
- `DEPLOYMENT.md`

The Render Blueprint uses managed Postgres, one Docker web instance, one Uvicorn worker, generated JWT secret, and production BYOK enforcement.

See `DEPLOYMENT.md` before deploying.

## Project structure

```text
checkpoint/
├── backend/
│   ├── agent/                 Guarded LLM tools + agent loop
│   ├── api/                   Auth, sessions, actions, checkpoints, rollback
│   ├── core/                  Orchestration, lifecycle, diffing, registry
│   ├── db/                    SQLite/Postgres persistence
│   ├── models/                Pydantic models
│   ├── risk/                  Deterministic risk engine
│   ├── runtimes/              Solari + local runtime adapters
│   └── sdk/                   Python HTTP SDK
├── examples/                  Deterministic + LLM demos
├── frontend/                  Zero-build dashboard
├── tests/                     Offline integration tests
├── Dockerfile
├── render.yaml
├── requirements.txt
└── DEPLOYMENT.md
```

## License

MIT
