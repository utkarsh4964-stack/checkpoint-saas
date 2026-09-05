# CHECKPOINT demo script

## What to demonstrate

**CHECKPOINT — Git for AI agent actions.**

The goal is to show both sides of the product:

1. prevent dangerous actions before they execute when the risk is knowable;
2. detect unexpected filesystem damage after execution and recover it.

The demo failure scenario is intentionally scripted/deterministic. It should not be presented as spontaneous LLM behavior.

## 90-second flow

### 0–10s — Positioning

Show the dashboard.

Say:

> Autonomous agents can take real actions. CHECKPOINT sits between the agent and its sandbox so every meaningful action can be evaluated, audited, and recovered.

### 10–25s — Normal operation

Start a session and show the initial checkpoint.

Task:

> Clean up this project directory. Remove temporary files, organize the reports folder, and create a summary of what you changed.

Run one safe write or move and show it completing normally.

### 25–45s — Prevention

Attempt:

```text
Intent: Organize the reports folder
Actual: rm -rf reports
```

Expected:

```text
Risk: 75/100
Destructive operation
Scope violation
BLOCKED
```

Then attempt to delete `secrets.env`.

Expected:

```text
Risk: 85/100
Secret access
Destructive operation
BLOCKED
```

Neither action reaches the runtime.

### 45–70s — Detection after execution

Trigger the separate evasive bulk-delete scenario.

The command uses Python filesystem calls instead of an obvious `rm`/`delete` keyword.

Expected:

```text
25 files removed
        ↓
Filesystem diff
        ↓
Bulk modification + scope mismatch
        ↓
Risk: 80/100
        ↓
PAUSED
```

This demonstrates why post-execution diffing exists: static text rules cannot know the final blast radius of every program.

### 70–85s — Recovery

Click:

**Reject & Roll Back**

Expected:

```text
PAUSED
   ↓
REJECT
   ↓
restore checkpoint
   ↓
25 files back
```

When running against Solari, CHECKPOINT uses Solari's snapshot/revert mechanism for the remote sandbox. When running offline, the local fallback has its own filesystem snapshot/restore implementation.

### 85–90s — Close

Say:

> Autonomous agents shouldn't just be able to act. They should be able to prove what they did — and recover when they get it wrong.

## Run the scripted demo

Offline/local fallback:

```powershell
python -m examples.workspace_agent
```

Real Solari runtime:

```powershell
$env:SOLARI_API_KEY="slr_live_..."
python -m examples.workspace_agent
```

## Run the real LLM loop

Set a Groq key:

```powershell
$env:GROQ_API_KEY="your-key"
```

Then:

```powershell
python -m examples.llm_workspace_agent
```

The real LLM loop chooses tool calls; every mutating tool call still passes through CHECKPOINT before reaching the runtime.
