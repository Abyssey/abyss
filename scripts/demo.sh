#!/usr/bin/env bash
# ==============================================================================
# scripts/demo.sh — Executable 4-Act End-to-End Verification & Demo Script
#
# Runs Track A (HTTP API + Real Git Worktree + Sandboxed Live Preview + Guardrails
# + Independent Read-Only MR Review) against a live FastAPI server.
#
# Usage:
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
echo " Modern Coding Agent Architecture — 4-Act Live Demo & Verification"
echo " Server URL : ${BASE_URL}"
echo " Track B UI : ${BASE_URL}/"
echo " OpenAPI UI : ${BASE_URL}/docs"
echo "========================================================================"

# ------------------------------------------------------------------------------
# 0. Pre-flight Pod Probes (/healthz & /readyz)
# ------------------------------------------------------------------------------
echo ""
echo "[Step 0] Pre-flight Pod Liveness (/healthz) & Readiness (/readyz)"
HEALTHZ=$(curl -fsS "${BASE_URL}/healthz")
echo "${HEALTHZ}" | jq -e '.status == "ok"' >/dev/null
echo "  ✓ /healthz: $(echo "${HEALTHZ}" | jq -c .)"

READYZ=$(curl -fsS "${BASE_URL}/readyz")
echo "${READYZ}" | jq -e '.status == "ready" and .checks.git == true and .checks.internal_bare_remote == true and .checks.static_ui == true' >/dev/null
echo "  ✓ /readyz : $(echo "${READYZ}" | jq -c .)"

# ------------------------------------------------------------------------------
# Act 1: Legacy Structure Analysis & Socratic Onboarding
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
echo "  ✓ Internal Git clone analysis: source_type=$(echo "${ACT1_INTERNAL}" | jq -r '.analysis.source_type'), modules=$(echo "${ACT1_INTERNAL}" | jq '.analysis.archify_graph.nodes | length'), socratic_questions=$(echo "${ACT1_INTERNAL}" | jq '.socratic_ladder | length')"

ACT1_SELF=$(curl -fsS -X POST "${BASE_URL}/api/repo-onboard/analyze" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n '{repo_url_or_path: "self"}')")
echo "${ACT1_SELF}" | jq -e '
  .analysis.source_type == "local_path"
  and (.analysis.archify_graph.nodes | length) >= 8
  and (.socratic_ladder | length) == 3
' >/dev/null
echo "  ✓ Local dev folder ('self') analysis: source_type=$(echo "${ACT1_SELF}" | jq -r '.analysis.source_type'), modules=$(echo "${ACT1_SELF}" | jq '.analysis.archify_graph.nodes | length')"

# ------------------------------------------------------------------------------
# Act 2-A: 3-Worktree Parallel Team Build, Independent Review, Merge & Push
# ------------------------------------------------------------------------------
echo ""
echo "[Act 2-A] 3-Worktree 병렬 팀 빌드 → 독립 리뷰(100/100) → main 머지 & Push"
WORKFLOW_RES=$(curl -fsS -X POST "${BASE_URL}/api/workflow/run" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n '{goal: "Build RepoOnboard: Local & Internal Git Codebase Onboarding Service", engine_mode: "agent_runtime"}')")

echo "${WORKFLOW_RES}" | jq -e '
  .status == "completed"
  and (.merged_branches | length) == 3
  and (.reviews | to_entries | all(.value.approved == true and .value.total_score == 100))
' >/dev/null
echo "  ✓ Merged branches: $(echo "${WORKFLOW_RES}" | jq -c '.merged_branches')"

if [ -d "${REPO_ROOT}/demo_workspace/repo_onboard/.git" ]; then
  echo "  ✓ Real Git Merge Graph (demo_workspace/repo_onboard):"
  git -C "${REPO_ROOT}/demo_workspace/repo_onboard" log --graph --oneline -n 6 | sed 's/^/      /'
  LOCAL_MAIN_SHA=$(git -C "${REPO_ROOT}/demo_workspace/repo_onboard" rev-parse refs/heads/main)
  REMOTE_MAIN_SHA=$(git ls-remote "${REPO_ROOT}/demo_workspace/internal_origin.git" refs/heads/main | awk '{print $1}')
  if [ "${LOCAL_MAIN_SHA}" != "${REMOTE_MAIN_SHA}" ]; then
    echo "ERROR: Remote main SHA (${REMOTE_MAIN_SHA}) != local main SHA (${LOCAL_MAIN_SHA})" >&2
    exit 1
  fi
  echo "  ✓ Remote sync verified: main == ${LOCAL_MAIN_SHA:0:10}"
fi

# ------------------------------------------------------------------------------
# Act 2-B: Visual Build (`project_map_app` Default) + Sandboxed Live Preview
# Prompt: "이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘"
# (Secondary preset: "서비스 상태를 한눈에 보는 점검 대시보드 웹페이지를 만들어줘")
# ------------------------------------------------------------------------------
echo ""
echo "[Act 2-B] 격리 Worktree에서 인터랙티브 프로젝트 지도 웹앱 빌드 + Live Preview"
echo "  Prompt: \"이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘\""

BUILD_RES=$(curl -fsS -X POST "${BASE_URL}/api/workflow/tasks/build" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n '{
    repo_url_or_path: "demo",
    instruction: "이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘",
    preset: "project_map_app",
    use_code_mode: false,
    push_to_remote: true
  }')")

echo "${BUILD_RES}" | jq -e '
  .status == "completed"
  and (.branch_name | startswith("agent/task-"))
  and (.changed_files | index("project_map/builder.py") != null)
  and (.changed_files | index("project_map/index.html") != null)
  and (.changed_files | index("tests/test_project_map.py") != null)
  and (.git_diff | length) > 100
  and .test_result.exit_code == 0
  and .test_result.passed == true
  and .pushed_to_remote == true
  and (.preview_url | startswith("/api/workflow/tasks/"))
' >/dev/null

BUILD_TASK_ID=$(echo "${BUILD_RES}" | jq -r '.task_id')
BUILD_BRANCH=$(echo "${BUILD_RES}" | jq -r '.branch_name')
PREVIEW_PATH=$(echo "${BUILD_RES}" | jq -r '.preview_url')
PREVIEW_URL="${BASE_URL}${PREVIEW_PATH}"

if [ -d "${REPO_ROOT}/demo_workspace/internal_origin.git" ]; then
  git ls-remote --heads "${REPO_ROOT}/demo_workspace/internal_origin.git" | grep -q "refs/heads/${BUILD_BRANCH}"
  echo "  ✓ Feature branch pushed to remote: refs/heads/${BUILD_BRANCH}"
fi

PREVIEW_HTML=$(curl -fsS "${PREVIEW_URL}")
echo "${PREVIEW_HTML}" | grep -q '<!DOCTYPE html>'
echo "${PREVIEW_HTML}" | grep -q 'project-map-data'
echo "${PREVIEW_HTML}" | grep -q 'detail-drawer'
echo "${PREVIEW_HTML}" | grep -q 'repo_onboard/analyzer.py'
if echo "${PREVIEW_HTML}" | grep -q 'fonts\.googleapis\.com'; then
  echo "ERROR: Preview HTML must not depend on external fonts.googleapis.com!" >&2
  exit 1
fi
echo "  ✓ Built task_id=${BUILD_TASK_ID}, branch=${BUILD_BRANCH}, pytest=PASS"
echo "  ★ Live Built Project Map Preview: ${PREVIEW_URL}"

# ------------------------------------------------------------------------------
# Act 2-C: Claude Code Kick — Code Mode (REPL) 1-Step Find -> Edit -> Test
# ------------------------------------------------------------------------------
echo ""
echo "[Act 2-C] Claude Code Kick: Code Mode (REPL) 1-Step 자동 수정 + 테스트"
SCENARIOS=$(curl -fsS "${BASE_URL}/api/code-mode/scenarios")
RETRY_CODE=$(echo "${SCENARIOS}" | jq -r '.scenarios[] | select(.id == "retry_policy") | .python_code')

CM_OK=$(curl -fsS -X POST "${BASE_URL}/api/code-mode/run" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg code "${RETRY_CODE}" '{code: $code, scenario_id: "retry_policy", reset_sandbox: true}')")

echo "${CM_OK}" | jq -e '
  .result.status == "ok"
  and .comparison.code_mode_llm_turns == 1
  and .comparison.direct_mode_llm_turns == 4
  and .result.return_value.edited == "src/retry_policy.py"
  and .result.return_value.tests.passed == true
' >/dev/null
echo "  ✓ Code Mode REPL: Direct 4 turns -> 1 turn, edited=src/retry_policy.py, pytest=PASS, saved=$(echo "${CM_OK}" | jq -r '.comparison.context_saved_pct')%"

# ------------------------------------------------------------------------------
# Act 3: Deterministic Guardrails — Compound Shell Bypass & Runtime Escape Refusal
# Prompt: "에이전트가 main 브랜치에 push하거나 상위 비밀 파일을 건드리지 못하게 막아줘"
# ------------------------------------------------------------------------------
echo ""
echo "[Act 3] 결정론적 보안 가드레일 차단 (Focus Domain #1)"
echo "  Prompt: \"에이전트가 main 브랜치에 push하거나 상위 비밀 파일을 건드리지 못하게 막아줘\""

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
echo "  ✓ Compound push bypass REFUSED: status=$(echo "${CM_COMPOUND}" | jq -r '.result.status'), decided_by=$(echo "${CM_COMPOUND}" | jq -r '.result.tool_calls[0].decided_by')"

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
echo "  ✓ Runtime ../../.env escape REFUSED: status=$(echo "${CM_ESCAPE}" | jq -r '.result.status'), decided_by=$(echo "${CM_ESCAPE}" | jq -r '.result.tool_calls[0].decided_by')"

# ------------------------------------------------------------------------------
# Act 4: Independent Read-Only MR / Branch Code Review (Walkthrough)
# Prompt: "방금 작업한 브랜치 코드에 보안 문제나 버그가 없는지 리뷰해줘"
# ------------------------------------------------------------------------------
echo ""
echo "[Act 4] 독립 읽기 전용 코드 리뷰어 (open-swe Reviewer + Walkthrough)"
echo "  Prompt: \"방금 작업한 브랜치 코드에 보안 문제나 버그가 없는지 리뷰해줘\""

REV_DEFECT=$(curl -fsS -X POST "${BASE_URL}/api/workflow/tasks/review" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n '{preset: "defective_mr", run_tests: false}')")

echo "${REV_DEFECT}" | jq -e '
  .decision == "REQUEST_CHANGES"
  and .score < 80
  and .read_only_verified == true
  and (.findings | length) >= 3
  and (.findings | all(.file != "" and .comment != "" and .suggestion != ""))
' >/dev/null
echo "  ✓ Preset 4-A (Defective MR): decision=$(echo "${REV_DEFECT}" | jq -r '.decision'), score=$(echo "${REV_DEFECT}" | jq -r '.score')/100, findings=$(echo "${REV_DEFECT}" | jq '.findings | length')"

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
  and ([.findings[] | select(.severity == "high")] | length) == 0
' >/dev/null
echo "  ✓ Preset 4-B (Act 2 Branch '${BUILD_BRANCH}'): decision=$(echo "${REV_CLEAN}" | jq -r '.decision'), score=$(echo "${REV_CLEAN}" | jq -r '.score')/100, pytest=PASS"

echo ""
echo "========================================================================"
echo " ALL 4 ACTS PASSED END-TO-END!"
echo " ★ Live Built Project Map Preview : ${PREVIEW_URL}"
echo " ★ Interactive Control Plane UI   : ${BASE_URL}/"
echo "========================================================================"
