# CHECKPOINT production deployment

## Production topology

```text
Browser / Agent
      │ HTTPS + JWT
      ▼
CHECKPOINT FastAPI
      │
      ├── PostgreSQL
      │
      └── one process-local runtime registry
                 │
                 ▼
          Solari Sandbox
```

The single-process boundary is intentional. One Checkpoint manager owns one live Solari runtime handle, and that handle currently lives in process memory.

## Required production environment

```text
CHECKPOINT_ENV=production
CHECKPOINT_JWT_SECRET=<random secret, at least 32 bytes>
DATABASE_URL=postgresql://...
CHECKPOINT_CORS_ORIGINS=https://your-checkpoint-host.example
CHECKPOINT_ALLOW_LOCAL_FALLBACK=false
SOLARI_BASE_URL=https://api.getsolari.com
```

Do **not** set `SOLARI_API_KEY` for the public product. Production sessions are BYOK: each customer supplies their own Solari key at session creation.

## Security requirements

- Terminate TLS at the hosting edge.
- Use a JWT secret of at least 32 random bytes.
- Never commit `.env` or real Solari credentials.
- Keep `CHECKPOINT_ALLOW_LOCAL_FALLBACK=false` in production.
- Keep the Solari endpoint fixed to the configured HTTPS origin; arbitrary customer-supplied endpoints are rejected to prevent the server from becoming an SSRF proxy.
- Do not increase Uvicorn worker count.
- Do not add multiple service instances until the runtime registry supports shared/reconnectable handles.

## Render

`render.yaml` provisions:

- Docker web service
- one web instance
- managed Postgres
- generated JWT secret
- production BYOK enforcement
- one Uvicorn worker

Create a Render Blueprint from this repository and let Render apply `render.yaml`.

After the first deployment, set:

```text
CHECKPOINT_CORS_ORIGINS=https://<your-render-service>.onrender.com
```

The frontend is served by the same FastAPI service at `/app/`, so the browser normally uses the same origin and does not require cross-origin requests.

## Database

Production uses Postgres when `DATABASE_URL` begins with `postgres://` or `postgresql://`.

Local development uses SQLite automatically when `DATABASE_URL` is unset.

The repository layer uses the same interface for both databases.

## Session lifecycle

Each session has:

- maximum active-session quota per user
- action quota per session
- idle timeout
- maximum lifetime
- explicit teardown
- startup orphan cleanup
- periodic cleanup sweep

The browser's timeline polling does **not** refresh the session lease. Only actual session activity does. This prevents an open dashboard tab from keeping an otherwise idle Solari sandbox alive forever.

## BYOK lifecycle

A production session request contains:

```json
{
  "agent_id": "workspace-agent",
  "task_description": "Clean up my project",
  "solari_api_key": "slr_live_..."
}
```

The API extracts the key, passes it to the Solari runtime, and never persists it in the database. The frontend clears the key input immediately after successful session creation.

The key is not included in the `Session` response, JWT claims, timeline, or action records.

## Health checks

```text
GET /health
GET /api/health
```

The response reports application status plus database connectivity.

## Local verification

```powershell
python -m compileall -q backend
PYTHONPATH=. pytest -q tests/test_mvp.py
python -m examples.workspace_agent
```

The tests use the local fallback runtime and therefore do not require Solari credentials.

## Known MVP limitations

### One process

The runtime registry is in-memory. One worker/instance is required.

### Rate limiter

The rate limiter is also in-memory and resets on process restart. It is basic abuse protection, not DDoS protection.

### Account recovery

There is currently no email verification or password-reset flow. Add those before opening registration to a broad public audience.

### External side effects

CHECKPOINT recovery is scoped to supported sandbox filesystem state. It does not automatically undo arbitrary network requests, emails, payments, browser actions, or other external side effects.

## Solari dependency

The production image pins `solari-sandbox==0.2.0` for reproducibility. The adapter uses the documented `SandboxClient`, sandbox `commands.run`, filesystem operations, snapshots, `revert`, and `kill` surface.
