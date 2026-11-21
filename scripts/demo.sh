#!/usr/bin/env bash
# ==============================================================================
# scripts/demo.sh — Executable 4-Act End-to-End Verification & Demo Script
#
# Runs Track A (HTTP API + `langchain-ai/deepagents` Single Engine + Antigravity
# Plan Review / Proceed + Real Git Worktrees + Sandboxed Live Preview +
# Deterministic Guardrails + Independent Read-Only MR Review) against a live server.
#
# Usage:
#   cp .env.example .env   # configure GEMINI_API_KEY (or QWEN_API_KEY / OPENAI_API_KEY)
#   BASE_URL=http://localhost:8000 bash scripts/demo.sh
# ==============================================================================
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

for bin in curl jq git; do
  if ! command -v "${bin}" >/dev/null 2>&1; then
    echo "ERROR: required command '${bin}' not found in PATH." >&2
    exit 1
  fi
done

echo "========================================================================"
echo " Modern Coding Agent Architecture — 4-Act Live Demo (deepagents v0.7)"
echo " Server URL : ${BASE_URL}"
echo " Track B UI : ${BASE_URL}/"
echo " OpenAPI UI : ${BASE_URL}/docs"
echo "========================================================================"

# ------------------------------------------------------------------------------
# 0. Pre-flight Pod Probes (/healthz, /readyz & /api/state Engine Check)
# ------------------------------------------------------------------------------
echo ""
echo "[Step 0] Pre-flight Pod Liveness (/healthz), Readiness (/readyz) & Engine Check"
HEALTHZ=$(curl -fsS "${BASE_URL}/healthz")
echo "${HEALTHZ}" | jq -e '.status == "ok"' >/dev/null
echo "  ✓ /healthz: $(echo "${HEALTHZ}" | jq -c .)"

READYZ=$(curl -fsS "${BASE_URL}/readyz")
echo "${READYZ}" | jq -e '.status == "ready" and .checks.git == true and .checks.internal_bare_remote == true and .checks.static_ui == true' >/dev/null
echo "  ✓ /readyz : $(echo "${READYZ}" | jq -c .)"

STATE_JSON=$(curl -fsS "${BASE_URL}/api/state")
echo "${STATE_JSON}" | jq -e '.engine == "langchain-ai/deepagents"' >/dev/null
HAS_LIVE_LLM=$(echo "${STATE_JSON}" | jq -r '.rotator.has_live_credentials')
STATUS_BANNER=$(echo "${STATE_JSON}" | jq -r '.rotator.status_banner')
echo "  ✓ Engine  : langchain-ai/deepagents (${STATUS_BANNER})"

# ------------------------------------------------------------------------------
# Act 1: Legacy Structure Analysis & Socratic Onboarding (Offline-Ready)
# Prompt: "이 프로젝트 구조와 핵심 파일을 한눈에 정리해줘"
# ------------------------------------------------------------------------------
echo ""
echo "[Act 1] 레거시 코드 구조 파악 & 온보딩"
echo "  Prompt: \"이 프로젝트 구조와 핵심 파일을 한눈에 정리해줘\""

ACT1_INTERNAL=$(curl -fsS -X POST "${BASE_URL}/api/repo-onboard/analyze" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n '{repo_url_or_path: "internal-origin"}')")
echo "${ACT1_INTERNAL}" | jq -e '
  .analysis.source_type == "internal_git_bare"
  and (.analysis.archify_graph.nodes | length) >= 1
  and (.socratic_ladder | length) == 3
' >/dev/null
echo "  ✓ Internal Git clone analysis: source_type=$(echo "${ACT1_INTERNAL}" | jq -r '.analysis.source_type'), modules=$(echo "${ACT1_INTERNAL}" | jq '.analysis.archify_graph.nodes | length')"

ACT1_SELF=$(curl -fsS -X POST "${BASE_URL}/api/repo-onboard/analyze" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n '{repo_url_or_path: "self"}')")
echo "${ACT1_SELF}" | jq -e '
  .analysis.source_type == "local_path"
  and (.analysis.archify_graph.nodes | length) >= 8
  and (.socratic_ladder | length) == 3
' >/dev/null
echo "  ✓ Local dev folder ('self') analysis: modules=$(echo "${ACT1_SELF}" | jq '.analysis.archify_graph.nodes | length')"

# ------------------------------------------------------------------------------
# Act 3 (Offline Guardrail Lab): Compound Shell Bypass & Runtime Escape Refusal
# Prompt: "에이전트가 main 브랜치에 push하거나 상위 비밀 파일을 건드리지 못하게 막아줘"
# ------------------------------------------------------------------------------
echo ""
echo "[Act 3] 결정론적 보안 가드레일 차단 & Code Mode REPL (Focus Domains #1 & #2)"
echo "  Prompt: \"에이전트가 main 브랜치에 push하거나 상위 비밀 파일을 건드리지 못하게 막아줘\""

SCENARIOS=$(curl -fsS "${BASE_URL}/api/code-mode/scenarios")
RETRY_CODE=$(echo "${SCENARIOS}" | jq -r '.scenarios[] | select(.id == "retry_policy") | .python_code')

CM_OK=$(curl -fsS -X POST "${BASE_URL}/api/code-mode/run" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg code "${RETRY_CODE}" '{code: $code, scenario_id: "retry_policy", reset_sandbox: true}')")
echo "${CM_OK}" | jq -e '
  .result.status == "ok"
  and .comparison.code_mode_llm_turns == 1
  and .result.return_value.edited == "src/retry_policy.py"
  and .result.return_value.tests.passed == true
' >/dev/null
echo "  ✓ Code Mode REPL 1-Step fix: edited=src/retry_policy.py, pytest=PASS"

CM_COMPOUND=$(curl -fsS -X POST "${BASE_URL}/api/code-mode/run" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n '{
    code: "await sh(\"cd '\''app'\'' && FOO=1 git push origin main\")",
    scenario_id: "compound_cmd_trap",
    reset_sandbox: true
  }')")
echo "${CM_COMPOUND}" | jq -e '
  .result.status == "refused"
  and .result.tool_calls[0].allowed == false
  and .result.tool_calls[0].decided_by == "permission:deny"
' >/dev/null
echo "  ✓ Compound push bypass REFUSED: decided_by=$(echo "${CM_COMPOUND}" | jq -r '.result.tool_calls[0].decided_by')"

CM_ESCAPE=$(curl -fsS -X POST "${BASE_URL}/api/code-mode/run" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n '{
    code: "parts = [\"..\", \"..\", \".env\"]\nawait write(\"/\".join(parts), \"LEAKED=1\")",
    scenario_id: "runtime_escape_and_hook",
    reset_sandbox: true
  }')")
echo "${CM_ESCAPE}" | jq -e '
  .result.status == "refused"
  and .result.tool_calls[0].allowed == false
  and .result.tool_calls[0].decided_by == "isolation:file_edit"
' >/dev/null
echo "  ✓ Runtime ../../.env escape REFUSED: decided_by=$(echo "${CM_ESCAPE}" | jq -r '.result.tool_calls[0].decided_by')"

# ------------------------------------------------------------------------------
# Fail-Fast HTTP 503 Check when Live LLM Credentials are Unconfigured
# ------------------------------------------------------------------------------
if [ "${HAS_LIVE_LLM}" != "true" ]; then
  echo ""
  echo "[Fail-Fast Check] No live LLM API key configured — verifying HTTP 503 policy..."
  HTTP_CODE=$(curl -s -o /tmp/abyss_503_check.json -w "%{http_code}" -X POST "${BASE_URL}/api/workflow/tasks/build" \
    -H 'Content-Type: application/json' \
    -d '{"repo_url_or_path":"demo","preset":"project_map_app"}')
  if [ "${HTTP_CODE}" != "503" ]; then
    echo "ERROR: Expected HTTP 503 when unconfigured, got ${HTTP_CODE}" >&2
    exit 1
  fi
  jq -e '.error == "⛔ LLM CONFIG REQUIRED"' /tmp/abyss_503_check.json >/dev/null
  echo "  ✓ Verified HTTP 503 (⛔ LLM CONFIG REQUIRED). Configure GEMINI_API_KEY in .env to run Live LLM Acts 2 & 4."
  exit 0
fi

# ------------------------------------------------------------------------------
# Act 2-A: 3-Worktree Parallel Team Build, Independent Review, Merge & Push
# ------------------------------------------------------------------------------
echo ""
echo "[Act 2-A] 3-Worktree 병렬 팀 빌드 → 독립 리뷰 → main 머지 & Push"
WORKFLOW_RES=$(curl -fsS -X POST "${BASE_URL}/api/workflow/run" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n '{goal: "Build RepoOnboard: Local & Internal Git Codebase Onboarding Service"}')")

echo "${WORKFLOW_RES}" | jq -e '
  .engine == "langchain-ai/deepagents"
  and .status == "completed"
  and (.merged_branches | length) >= 2
' >/dev/null
echo "  ✓ Merged branches: $(echo "${WORKFLOW_RES}" | jq -c '.merged_branches')"

# ------------------------------------------------------------------------------
# Act 2-B: Autonomous Build (`request_review` -> `/plan/approve` -> Live Preview)
# Prompt: "이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘"
# ------------------------------------------------------------------------------
echo ""
echo "[Act 2-B] 격리 Worktree 자율 빌드 (Plan 작성 → 승인 API → 코딩·검증 → Live Preview)"
echo "  Prompt: \"이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘\""

BUILD_RES=$(curl -fsS -X POST "${BASE_URL}/api/workflow/tasks/build" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n '{
    repo_url_or_path: "demo",
    instruction: "이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘",
    preset: "project_map_app",
    use_code_mode: false,
    push_to_remote: true,
    plan_review: "request_review"
  }')")

BUILD_TASK_ID=$(echo "${BUILD_RES}" | jq -r '.task_id')
BUILD_STATUS=$(echo "${BUILD_RES}" | jq -r '.status')

if [ "${BUILD_STATUS}" = "awaiting_plan_review" ]; then
  echo "${BUILD_RES}" | jq -e '(.artifacts.implementation_plan | length) > 20' >/dev/null
  echo "  ✓ Paused at awaiting_plan_review (task_id=${BUILD_TASK_ID}); approving implementation_plan.md..."
  BUILD_RES=$(curl -fsS -X POST "${BASE_URL}/api/workflow/tasks/${BUILD_TASK_ID}/plan/approve" \
    -H 'Content-Type: application/json' \
    -d "$(jq -n '{proceed: true, comments: "Approved via scripts/demo.sh"}')")
fi

echo "${BUILD_RES}" | jq -e '
  .status == "completed"
  and (.branch_name | startswith("agent/task-"))
  and (.changed_files | length) >= 1
  and .test_result.exit_code == 0
  and .test_result.passed == true
  and .pushed_to_remote == true
  and (.artifacts.walkthrough | length) > 20
  and (.preview_url | startswith("/api/workflow/tasks/"))
' >/dev/null

BUILD_BRANCH=$(echo "${BUILD_RES}" | jq -r '.branch_name')
PREVIEW_PATH=$(echo "${BUILD_RES}" | jq -r '.preview_url')
PREVIEW_URL="${BASE_URL}${PREVIEW_PATH}"

PREVIEW_HTML=$(curl -fsS "${PREVIEW_URL}")
if echo "${PREVIEW_HTML}" | grep -q 'fonts\.googleapis\.com'; then
  echo "ERROR: Preview HTML must not depend on external fonts.googleapis.com!" >&2
  exit 1
fi
echo "  ✓ Built task_id=${BUILD_TASK_ID}, branch=${BUILD_BRANCH}, pytest=PASS, artifacts=3/3"
echo "  ★ Live Built Project Map Preview: ${PREVIEW_URL}"

# ------------------------------------------------------------------------------
# Act 2-C: Natural-Language Code Mode Agent (`POST /api/code-mode/agent`, `repl`)
# ------------------------------------------------------------------------------
echo ""
echo "[Act 2-C] Natural-Language Code Mode DeepAgent (mode=repl)"
CM_AGENT=$(curl -fsS -X POST "${BASE_URL}/api/code-mode/agent" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n '{
    instruction: "Raise retryPolicy maxAttempts from 3 to 5 in src/retry_policy.py and run pytest -q src/test_retry.py",
    mode: "repl",
    scenario_id: "retry_policy",
    reset_sandbox: true,
    completion_cmd: "pytest -q src/test_retry.py"
  }')")
echo "${CM_AGENT}" | jq -e '.result.passed == true' >/dev/null
echo "  ✓ Code Mode DeepAgent (repl): turns=$(echo "${CM_AGENT}" | jq -r '.comparison.llm_turns'), files=$(echo "${CM_AGENT}" | jq -c '.comparison.files_touched')"

# ------------------------------------------------------------------------------
# Act 4: Independent Read-Only MR / Branch Code Review (Walkthrough)
# Prompt: "방금 작업한 브랜치 코드에 보안 문제나 버그가 없는지 리뷰해줘"
# ------------------------------------------------------------------------------
echo ""
echo "[Act 4] 독립 읽기 전용 코드 리뷰어 (ReadOnlyGuardedBackend + ReviewFindings + EvaluatorRubric)"
echo "  Prompt: \"방금 작업한 브랜치 코드에 보안 문제나 버그가 없는지 리뷰해줘\""

REV_DEFECT=$(curl -fsS -X POST "${BASE_URL}/api/workflow/tasks/review" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n '{preset: "defective_mr", run_tests: false}')")

echo "${REV_DEFECT}" | jq -e '
  .decision == "REQUEST_CHANGES"
  and .score < 80
  and .read_only_verified == true
  and (.findings | length) >= 3
' >/dev/null
echo "  ✓ Preset 4-A (Defective MR): decision=$(echo "${REV_DEFECT}" | jq -r '.decision'), score=$(echo "${REV_DEFECT}" | jq -r '.score')/100"

REV_CLEAN=$(curl -fsS -X POST "${BASE_URL}/api/workflow/tasks/review" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg branch "${BUILD_BRANCH}" '{
    repo_url_or_path: "demo",
    branch_name: $branch,
    base_branch: "main",
    run_tests: true
  }')")

echo "${REV_CLEAN}" | jq -e '
  .decision == "APPROVE"
  and .score >= 80
  and .read_only_verified == true
  and .test_result.passed == true
' >/dev/null
echo "  ✓ Preset 4-B (Act 2 Branch '${BUILD_BRANCH}'): decision=$(echo "${REV_CLEAN}" | jq -r '.decision'), score=$(echo "${REV_CLEAN}" | jq -r '.score')/100, pytest=PASS"

echo ""
echo "========================================================================"
echo " ALL 4 ACTS PASSED END-TO-END!"
echo " ★ Live Built Project Map Preview : ${PREVIEW_URL}"
echo " ★ Interactive Control Plane UI   : ${BASE_URL}/"
echo "========================================================================"
