# Architecture

> Detailed architecture documentation for Deep Researcher Agent.

## System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Deep Researcher Agent                      │
│                                                              │
│  ┌─────────────┐                                             │
│  │ config.yaml │──→ Configuration for all components         │
│  └─────────────┘                                             │
│                                                              │
│  ┌──────────── Core Loop (loop.py) ────────────────────┐     │
│  │                                                      │     │
│  │  ┌───────┐    ┌─────────┐    ┌─────────┐           │     │
│  │  │ THINK │───→│ EXECUTE │───→│ REFLECT │──→ repeat  │     │
│  │  └───┬───┘    └────┬────┘    └────┬────┘           │     │
│  │      │             │              │                 │     │
│  │      ↓             ↓              ↓                 │     │
│  │  ┌───────────────────────────────────────┐          │     │
│  │  │        Agent Dispatcher (agents.py)   │          │     │
│  │  │                                       │          │     │
│  │  │  Leader ──→ Idea / Code / Writing     │          │     │
│  │  └───────────────────────────────────────┘          │     │
│  │      │             │              │                 │     │
│  │      ↓             ↓              ↓                 │     │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐           │     │
│  │  │ Memory   │ │ Monitor  │ │  Tools   │           │     │
│  │  │ Manager  │ │ (Zero$)  │ │ Registry │           │     │
│  │  └──────────┘ └──────────┘ └──────────┘           │     │
│  └──────────────────────────────────────────────────────┘     │
│                                                              │
│  ┌──────────── GPU Layer ──────────────────────────────┐     │
│  │  detect.py  │  keeper.py                            │     │
│  └─────────────────────────────────────────────────────┘     │
│                                                              │
│  ┌──────────── Skills Layer ───────────────────────────┐     │
│  │  daily-papers │ paper-analyze │ conf-search │ report │     │
│  └─────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────┘
```

## Component Details

### 1. Core Loop (`core/loop.py`)

The main orchestrator. Runs the THINK → EXECUTE → REFLECT cycle indefinitely.

**Key design decisions:**
- **Signal handling**: SIGTERM/SIGINT trigger graceful shutdown
- **Cycle counter**: Persisted to `.cycle_counter` file (survives restarts)
- **Smart cooldown**: Polls every N seconds instead of fixed sleep
- **Directive consumption**: Human directives are archived after reading (no re-reads)
- **Error backoff**: Doubles cooldown after errors to prevent burn loops

### 2. Agent Dispatcher (`core/agents.py`)

**Leader-Worker pattern** where:
- Leader persists conversation within a cycle (for coherent multi-step reasoning)
- Workers are stateless (each dispatch is independent)
- Only one worker runs at a time

**Why this works:**
- Leader sees the full picture without re-reading everything each step
- Workers are cheap (no accumulated context)
- Switching workers costs nothing (previous worker's context is gone)

### 3. Memory Manager (`core/memory.py`)

**Two tiers with automatic compaction:**

- **Tier 1 (Brief)**: Human-written, frozen. The "constitution" of the project.
- **Tier 2 (Log)**: Agent-written, rolling. Milestones and decisions.

**Compaction rules:**
1. Milestones: Drop oldest when section exceeds 1,200 chars
2. Decisions: Keep only last 15 entries
3. Total log: Hard cap at 2,000 chars (aggressive compaction if exceeded)

### 4. Experiment Monitor (`core/monitor.py`)

**The zero-cost innovation.** During training:
- `os.kill(pid, 0)` — is process alive? (zero cost)
- `nvidia-smi` — GPU utilization (zero cost)
- File tail read — last log lines (zero cost)

No LLM API calls until training completes.

### 5. Tool Registry (`core/tools.py`)

**Per-agent minimal tool sets** reduce token overhead:
- Each tool definition is ~200 tokens in the API call
- 15 tools = 3,000 extra tokens per call
- 4 tools = 800 extra tokens per call
- Over 100 API calls/day, that's 220K tokens saved

### 6. GPU Utilities (`gpu/`)

- **detect.py**: Auto-detect GPUs, check availability, reserve last GPU
- **keeper.py**: Keep cloud instances alive with minimal GPU activity
