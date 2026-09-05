"""Offline integration tests for the Checkpoint MVP.

These tests intentionally use the local fallback runtime so CI does not need
Solari credentials. Production Solari integration is exercised separately
against the published SDK/API when credentials are available.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("CHECKPOINT_ENV", "development")
os.environ.setdefault("CHECKPOINT_ALLOW_LOCAL_FALLBACK", "true")

from fastapi.testclient import TestClient

from backend.db import database
from backend.main import app


def auth(client: TestClient, email: str, password: str = "correct-horse-battery") -> str:
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_auth_tenant_isolation_and_recovery():
    with tempfile.TemporaryDirectory() as td:
        database.DB_PATH = Path(td) / "checkpoint.db"
        with TestClient(app) as client:
            alice = auth(client, "alice@example.com")
            bob = auth(client, "bob@example.com")

            a = client.post(
                "/sessions",
                headers=headers(alice),
                json={"agent_id": "agent-a", "task_description": "Edit project files"},
            )
            assert a.status_code == 200, a.text
            sid = a.json()["id"]

            write = client.post(
                f"/sessions/{sid}/actions",
                headers=headers(alice),
                json={
                    "session_id": sid,
                    "type": "file.write",
                    "intent": "Create a harmless summary file",
                    "target": "summary.txt",
                    "parameters": {"content": "hello"},
                },
            )
            assert write.status_code == 200, write.text
            action_id = write.json()["id"]

            assert client.get(f"/actions/{action_id}", headers=headers(bob)).status_code == 404
            assert client.get(f"/sessions/{sid}", headers=headers(bob)).status_code == 404

            # Seed bulk files through a single non-destructive command.
            seed = client.post(
                f"/sessions/{sid}/actions",
                headers=headers(alice),
                json={
                    "session_id": sid,
                    "type": "shell.execute",
                    "intent": "Create test fixture files",
                    "parameters": {"command": "python -c \"import pathlib; [pathlib.Path('bulk').mkdir(exist_ok=True) for _ in [0]]; [pathlib.Path(f'bulk/f{i}.txt').write_text('x') for i in range(25)]\""},
                },
            )
            assert seed.status_code == 200, seed.text

            # Evasive delete: no destructive keyword, but post-diff sees
            # 25 removals and scope mismatch -> PAUSED.
            dangerous = client.post(
                f"/sessions/{sid}/actions",
                headers=headers(alice),
                json={
                    "session_id": sid,
                    "type": "shell.execute",
                    "intent": "Consolidate the bulk data into one report",
                    "parameters": {"command": "python -c \"import os,glob; [os.remove(f) for f in glob.glob('bulk/*')]\""},
                },
            )
            assert dangerous.status_code == 200, dangerous.text
            assert dangerous.json()["status"] == "paused"
            dangerous_id = dangerous.json()["id"]

            # Cross-session manager confusion must not be possible; Bob cannot
            # act on Alice's paused action even though the action id is known.
            assert client.post(f"/actions/{dangerous_id}/reject", headers=headers(bob)).status_code == 404

            rejected = client.post(
                f"/actions/{dangerous_id}/reject?rollback=true",
                headers=headers(alice),
            )
            assert rejected.status_code == 200, rejected.text
            assert rejected.json()["status"] == "rejected"

            timeline = client.get(f"/sessions/{sid}/timeline", headers=headers(alice))
            assert timeline.status_code == 200
            assert timeline.json()["summary"]["rollbacks"] >= 1

            end = client.post(f"/sessions/{sid}/end", headers=headers(alice))
            assert end.status_code == 200
            state = client.get(f"/sessions/{sid}", headers=headers(alice))
            assert state.status_code == 200
            assert state.json()["status"] == "completed"
