"""
tests/test_ddd_architecture.py
Modern Python DDD Modular Monolith AST Dependency & Contract Verification Suite.

Enforces all 5 strict AST architectural boundary rules:
  1. `app/domains/guardrails/*.py`: pure Python stdlib + `app.domains.guardrails.*` ONLY (and NO `router.py`).
  2. `app/domains/execution/{code_mode,runtime}.py`: pure Python stdlib + `app.domains.guardrails.*` + `app.domains.execution.{code_mode,runtime}` ONLY.
  3. `app/domains/workflow/evaluator.py`: pure Python stdlib ONLY.
  4. `app/domains/onboarding/*.py`: ZERO imports from `app.domains.{workflow,execution,guardrails}`, `app.core.llm`, `app.main`, `abyss`.
  5. Every `router.py`: ONLY stdlib, `fastapi`, `starlette`, `pydantic`, `app.core.container`, and its own package.
Also verifies:
  - Legacy `abyss/`, `app/{domain,application,infrastructure,api}/`, and root `PROJECT.md` are removed.
  - `GET /api/learning-core` serves all 5 live Modern DDD core modules + 4 supporting modules on disk.
  - All HTTP/SSE & Pod readiness routes are registered in `app.main:app`.
  - `EvaluatorRubric` enforces 40+25+20+15=100 weights and >=80 gate.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.domains.workflow.evaluator import EvaluatorRubric
from app.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"
STDLIB = sys.stdlib_module_names


def _extract_imports(py_file: Path) -> list[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
    return mods


def test_rule_1_guardrails_domain_is_pure_stdlib_and_has_no_router() -> None:
    guardrails_dir = APP_DIR / "domains" / "guardrails"
    assert guardrails_dir.is_dir()
    assert not (guardrails_dir / "router.py").exists(), "guardrails must remain a pure domain without router.py"

    expected = {"__init__.py", "isolation_policy.py", "permissions.py"}
    actual = {p.name for p in guardrails_dir.glob("*.py")}
    assert expected == actual

    for py_file in sorted(guardrails_dir.glob("*.py")):
        for mod in _extract_imports(py_file):
            top = mod.split(".")[0]
            assert top in STDLIB or mod.startswith("app.domains.guardrails"), (
                f"AST Rule #1 Violation in {py_file.name}: forbidden import '{mod}'"
            )


def test_rule_2_execution_core_modules_only_import_stdlib_and_guardrails() -> None:
    exec_dir = APP_DIR / "domains" / "execution"
    for filename in ("code_mode.py", "runtime.py"):
        py_file = exec_dir / filename
        assert py_file.is_file()
        for mod in _extract_imports(py_file):
            top = mod.split(".")[0]
            allowed = (
                top in STDLIB
                or mod.startswith("app.domains.guardrails")
                or mod in ("app.domains.execution.code_mode", "app.domains.execution.runtime")
            )
            assert allowed, f"AST Rule #2 Violation in {filename}: forbidden import '{mod}'"


def test_rule_3_workflow_evaluator_is_pure_stdlib() -> None:
    eval_file = APP_DIR / "domains" / "workflow" / "evaluator.py"
    assert eval_file.is_file()
    for mod in _extract_imports(eval_file):
        top = mod.split(".")[0]
        assert top in STDLIB, f"AST Rule #3 Violation in evaluator.py: non-stdlib import '{mod}'"


def test_rule_4_onboarding_domain_has_zero_cross_domain_imports() -> None:
    onboard_dir = APP_DIR / "domains" / "onboarding"
    assert onboard_dir.is_dir()
    forbidden_prefixes = (
        "app.domains.workflow",
        "app.domains.execution",
        "app.domains.guardrails",
        "app.core.llm",
        "app.main",
        "abyss",
    )
    for py_file in sorted(onboard_dir.glob("*.py")):
        for mod in _extract_imports(py_file):
            assert not mod.startswith(forbidden_prefixes), (
                f"AST Rule #4 Violation in onboarding/{py_file.name}: forbidden import '{mod}'"
            )
            if py_file.name != "router.py":
                assert not mod.startswith(("fastapi", "starlette")), (
                    f"AST Rule #4 Violation in onboarding/{py_file.name}: web import '{mod}'"
                )


def test_rule_5_every_router_is_thin_and_scoped_to_own_package() -> None:
    routers = sorted(APP_DIR.rglob("router.py"))
    assert len(routers) == 4  # core/llm, execution, workflow, onboarding

    for router_file in routers:
        rel_pkg = router_file.parent.relative_to(REPO_ROOT).as_posix().replace("/", ".")
        for mod in _extract_imports(router_file):
            top = mod.split(".")[0]
            allowed = (
                top in STDLIB
                or top in ("fastapi", "starlette", "pydantic")
                or mod == "app.core.container"
                or mod.startswith(rel_pkg)
            )
            assert allowed, (
                f"AST Rule #5 Violation in {router_file.relative_to(REPO_ROOT)}: forbidden import '{mod}'"
            )


def test_workflow_never_imports_onboarding() -> None:
    workflow_dir = APP_DIR / "domains" / "workflow"
    for py_file in sorted(workflow_dir.glob("*.py")):
        for mod in _extract_imports(py_file):
            assert not mod.startswith("app.domains.onboarding"), (
                f"Cross-domain Violation in workflow/{py_file.name}: '{mod}'"
            )


def test_legacy_directories_and_root_project_md_removed() -> None:
    assert not (REPO_ROOT / "abyss").exists()
    assert not (REPO_ROOT / "PROJECT.md").exists()
    for legacy_dir in ("domain", "application", "infrastructure", "api"):
        assert not (APP_DIR / legacy_dir).exists(), f"Legacy horizontal layer still exists: app/{legacy_dir}"

    assert (APP_DIR / "core").is_dir()
    assert (APP_DIR / "domains" / "guardrails").is_dir()
    assert (APP_DIR / "domains" / "execution").is_dir()
    assert (APP_DIR / "domains" / "workflow").is_dir()
    assert (APP_DIR / "domains" / "onboarding").is_dir()
    assert (APP_DIR / "main.py").is_file()


def test_architecture_catalog_endpoint_serves_live_modern_ddd_files() -> None:
    with TestClient(app) as client:
        res = client.get("/api/learning-core")
        assert res.status_code == 200
        payload = res.json()

        core_modules = payload["core_modules"]
        infra_modules = payload["infra_modules"]
        assert len(core_modules) == 5
        assert len(infra_modules) == 4

        expected_core_paths = {
            "app/domains/guardrails/isolation_policy.py",
            "app/domains/guardrails/permissions.py",
            "app/domains/execution/code_mode.py",
            "app/domains/execution/runtime.py",
            "app/domains/workflow/evaluator.py",
        }
        assert {item["file_path"] for item in core_modules} == expected_core_paths

        for item in core_modules:
            disk_path = REPO_ROOT / item["file_path"]
            assert disk_path.exists(), f"Missing core file on disk: {disk_path}"
            assert item["code"] != "# File not found"
            assert len(item["code"]) > 500
            assert "from __future__ import annotations" in item["code"]

        for item in infra_modules:
            assert (REPO_ROOT / item["file_path"]).exists()


def test_fastapi_registers_all_modern_ddd_and_pod_routes() -> None:
    openapi_paths = set(app.openapi()["paths"].keys())
    expected_api_paths = {
        "/",
        "/healthz",
        "/readyz",
        "/api/state",
        "/api/workflow/run",
        "/api/workflow/tasks/presets",
        "/api/workflow/tasks/build",
        "/api/workflow/tasks/review",
        "/api/workflow/tasks/{task_id}/preview",
        "/api/events/stream",
        "/api/learning-core",
        "/api/code-mode/scenarios",
        "/api/code-mode/run",
        "/api/providers/config",
        "/api/providers/test/{provider_id}",
        "/api/repo-onboard/analyze",
        "/api/repo-onboard/answer",
    }
    assert expected_api_paths.issubset(openapi_paths)
    mount_paths = {getattr(r, "path", "") for r in app.routes}
    assert "/static" in mount_paths


def test_evaluator_rubric_scoring_weights_and_gate() -> None:
    rubric = EvaluatorRubric()
    good_diff = "\n".join(
        [
            "+++ b/app/domains/guardrails/sample.py",
            "+class SampleService:",
            "+    def execute(self) -> str:",
            "+        value = 'clean'",
            "+        return value",
            "+",
            "+def helper_one() -> int:",
            "+    return 1",
            "+",
            "+def helper_two() -> int:",
            "+    return 2",
            "+",
            "+def helper_three() -> int:",
            "+    return 3",
        ]
    )
    approved = rubric.evaluate_worktree_submission(
        worker_id="worker-1",
        branch_name="worktree-worker-1",
        git_diff=good_diff,
        pytest_exit_code=0,
        pytest_output="1 passed",
        required_symbols=["class SampleService", "def execute"],
        mailbox_messages_sent=1,
    )
    assert approved.approved is True
    assert approved.total_score == 100
    assert [c.max_score for c in approved.criteria] == [40, 25, 20, 15]

    failed_pytest = rubric.evaluate_worktree_submission(
        worker_id="worker-1",
        branch_name="worktree-worker-1",
        git_diff=good_diff,
        pytest_exit_code=1,
        pytest_output="1 failed",
        required_symbols=["class SampleService"],
        mailbox_messages_sent=1,
    )
    assert failed_pytest.approved is False
    assert failed_pytest.total_score == 60
