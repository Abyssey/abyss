# AGENTS.md — Abyss Coding Agent Control Plane (`langchain-ai/deepagents` Single Engine)

> Automatically loaded by Antigravity in every session inside `/home/abyss/projects/coding_agent`.
> For full historical context and transcript from the Windows migration session (`62c48c51-4a88-4e16-90bf-92fc64085f79`), inspect `.abyss/session-handoff/SESSION_HANDOFF.md` and `.abyss/session-handoff/transcript.jsonl`.

## 1. Environment & Primary Commands (WSL2 Ubuntu)

- **Active Repository Root**: `/home/abyss/projects/coding_agent`
- **Python Virtualenv**: `.venv` (Python 3.12 via `~/.local/bin/uv`, `deepagents 0.7.41`, `langchain 1.5.2`, `langchain-core 1.8.4`, `langchain-openai 1.8.5`, `langchain-google-genai 4.5.2`, `langgraph 1.2.24`)
- **CLI Tools Location**: `~/.local/bin` (`uv`, `node`, `npm`, `npx`, `jq`, `gh`)
- **Git Push Auth**: Configured in local `.git/config` to delegate to Windows Git Credential Manager.
- **Secrets**: `.env` (git-ignored, `chmod 600`) holds live `GEMINI_API_KEY`. **Never copy real keys into `.env.example` or commit secrets.**

```bash
# Offline full test suite (fast, deterministic, zero external LLM network calls)
GEMINI_ENABLED=false QWEN_ENABLED=false OPENAI_ENABLED=false .venv/bin/pytest -q

# Live Gemini end-to-end verification (requires GEMINI_API_KEY in .env)
.venv/bin/pytest -m live_llm -v

# Frontend syntax check
node --check app/static/app.js

# Run FastAPI Control Plane & 4-Act Demo Script
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
bash scripts/demo.sh

# Install or update Python dependencies inside .venv
~/.local/bin/uv pip install --python .venv/bin/python -e .
```

## 2. Current Migration & Branch Status

- **Active Branch**: `feat/deepagents-migration` (PR #1 open against `main`: https://github.com/Abyssey/abyss/pull/1)
- **Completed Phases**: Phases 0–8 all completed, verified (`105 passed, 1 skipped`), committed, and pushed.
  - Design & checklist: [docs/plans/deepagents-migration.md](docs/plans/deepagents-migration.md)
  - Phase-by-phase walkthrough: [docs/plans/deepagents-migration-walkthrough.md](docs/plans/deepagents-migration-walkthrough.md)

## 3. Non-Negotiable Architecture & Security Invariants (`tests/test_ddd_architecture.py`)

Every commit must keep `tests/test_ddd_architecture.py` 100% green:

1. **Rule #1 (`app/domains/guardrails/*.py`)**: Pure Python stdlib ONLY (zero third-party imports, no `router.py`).
2. **Rule #2 (`app/domains/execution/`)**:
   - `code_mode.py`: Pure Python stdlib + `app.domains.guardrails.*` ONLY.
   - `runtime.py`, `deepagent_backend.py`, `artifact_workflow.py`: Allow `deepagents`, `langchain*`, `langgraph`, `pydantic`, `typing_extensions` + `guardrails`, with **ZERO** imports from `workflow`, `onboarding`, `app.core`, or `fastapi`.
   - `execution/service.py` receives `model_factory` via DI from `app/core/container.py` (never import `app.core` outside `router.py`).
3. **Rule #3 (`app/domains/workflow/evaluator.py`)**: Pure Python stdlib ONLY.
4. **Rule #4 (`app/domains/onboarding/*.py`)**: Zero cross-domain (`workflow`, `execution`, `guardrails`) or `app.core.llm` imports.
5. **Rule #5 (`**/router.py`)**: Thin HTTP routers — import only stdlib, `fastapi`, `starlette`, `pydantic`, `app.core.container`, and own domain package.
6. **Rule #8 (Single `deepagents` Engine & Zero Fake Fallbacks)**:
   - `DeepAgentRuntime` (`app/domains/execution/runtime.py`) wraps `create_deep_agent` as the sole agent engine. Never re-introduce manual `while`/`for` agent loops, `mock` providers, `mock_responder`, or hardcoded code-generation templates.
   - Missing live LLM credentials must fail fast with `LLMConfigurationError` → **HTTP 503 (`⛔ LLM CONFIG REQUIRED`)**, while offline endpoints (`/api/repo-onboard/*`, `POST /api/code-mode/run`) remain functional without keys.
   - `builder` and `worker` modes run the Antigravity 3-phase workflow (`PLANNING -> EXECUTION -> VERIFICATION` via `ArtifactWorkflowMiddleware`) producing `/artifacts/implementation_plan.md`, `/artifacts/task.md`, and `pytest`-stamped `/artifacts/walkthrough.md`. Default plan policy is `AGENT_PLAN_REVIEW=request_review` (LangGraph `interrupt()` + `/api/workflow/tasks/{task_id}/plan/approve`).
   - Test doubles (`tests/helpers_llm_doubles.py`, `ScriptedDeepAgentChatModel`) must live strictly under `tests/`, never under `app/`.
   - Live Preview iframes must use `sandbox="allow-scripts"` only (never grant `allow-same-origin`) and zero external CDN/Google Fonts dependencies.
