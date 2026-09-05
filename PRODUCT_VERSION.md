# CHECKPOINT v0.2.1

**Git for AI agent actions** — a safety, observability, and transactional filesystem-recovery layer between autonomous agents and their sandboxed workspace.

This release hardens the 5–10 user MVP with:

- serialized per-session action execution
- strict action/session/checkpoint ownership enforcement
- production BYOK enforcement
- HTTPS-only, configured Solari endpoint validation
- startup expiration of orphaned runtime sessions
- idle/lifetime-aware session quotas
- command/file/intent size limits
- safer read-only agent path handling
- explicit failed/recovery-failed action states
- runtime command failure recording
- sessionStorage-only browser auth state
- production security headers
- modern Render Blueprint with paid compute defaults
- pinned Solari SDK version (`0.2.0`)
- offline integration tests

The single-worker limitation remains intentional because live runtime handles are process-local.
