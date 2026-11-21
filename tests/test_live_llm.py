"""
Live LLM End-to-End Verification (`@pytest.mark.live_llm`).

Skipped automatically when `GEMINI_API_KEY` is absent or when `GEMINI_ENABLED` is explicitly
disabled (`false`/`0`), so normal offline CI (`pytest -q`) never makes external network calls.

Manual execution by the user:
    cp .env.example .env   # set GEMINI_API_KEY=...
    .venv/bin/pytest -m live_llm -v
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from dotenv import load_dotenv

from app.core.llm.provider_rotator import LLMProviderRotator
from app.domains.execution.deepagent_backend import GuardedWorktreeBackend, collect_env_secrets
from app.domains.execution.runtime import DeepAgentRuntime


def _has_live_gemini_key() -> bool:
    if os.getenv("GEMINI_ENABLED", "true").strip().lower() in ("0", "false", "no", "off"):
        return False
    repo_env = Path(__file__).resolve().parent.parent / ".env"
    if repo_env.is_file() and not os.getenv("GEMINI_API_KEY"):
        load_dotenv(repo_env, override=False)
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key or key.lower().startswith(("your-", "<", "changeme", "placeholder")):
        return False
    return True


@pytest.mark.live_llm
@pytest.mark.skipif(
    os.name == "nt",
    reason="GuardedWorktreeBackend POSIX shell execution requires Linux/WSL.",
)
def test_live_gemini_fixes_pricing_bug_with_antigravity_artifacts(tmp_path: Path) -> None:
    if not _has_live_gemini_key():
        pytest.skip("GEMINI_API_KEY not configured (or GEMINI_ENABLED=false); skipping live_llm test.")

    main_repo = tmp_path / "main_repo"
    wt_root = main_repo / ".abyss" / "worktrees" / "live-pricing-fix"
    (wt_root / "src").mkdir(parents=True)
    (wt_root / "tests").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(wt_root), check=True)

    # Intentional bug: `discount_pct` (e.g. 20 for 20%) is subtracted raw instead of percentage
    (wt_root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (wt_root / "src" / "pricing.py").write_text(
        '"""Order pricing calculator."""\n\n\n'
        "def final_price(subtotal: float, discount_pct: float) -> float:\n"
        "    if subtotal < 0:\n"
        '        raise ValueError("subtotal must be non-negative")\n'
        "    # BUG: subtracts discount_pct directly instead of percentage of subtotal\n"
        "    return round(subtotal - discount_pct, 2)\n",
        encoding="utf-8",
    )
    (wt_root / "tests" / "test_pricing.py").write_text(
        "from src.pricing import final_price\n\n\n"
        "def test_twenty_percent_discount_on_250() -> None:\n"
        "    assert final_price(250.0, 20.0) == 200.0\n\n\n"
        "def test_zero_discount() -> None:\n"
        "    assert final_price(99.95, 0.0) == 99.95\n",
        encoding="utf-8",
    )

    rotator = LLMProviderRotator()
    rotator.update_config({"strategy": "single", "active_single_provider": "gemini"})
    selection = rotator.build_chat_model("live-pricing-fixer", "builder")

    artifacts_dir = tmp_path / "artifacts"
    backend = GuardedWorktreeBackend(
        worktree_root=wt_root,
        main_checkout_root=main_repo,
        secrets=collect_env_secrets(),
    )
    runtime = DeepAgentRuntime(
        model=selection.model,
        backend=backend,
        mode="builder",
        workflow="planning",
        plan_review="always_proceed",
        artifacts_dir=artifacts_dir,
        completion_cmd="pytest -q",
        rubric="Fix `final_price` in `src/pricing.py` so percentage discounts compute accurately and all pytest tests pass.",
        extra_middleware=selection.middleware(),
        provider_label=selection.label,
    )

    res = runtime.run(
        system_prompt="You are an autonomous Python engineer. Inspect the failing test, write your plan, fix the bug, and document the walkthrough.",
        task_prompt="Fix the percentage discount bug in `src/pricing.py` so `pytest -q` passes.",
    )

    assert res.passed is True, f"Live Gemini run failed: {res.summary}"
    assert "# Implementation Plan" in res.artifacts["implementation_plan"]
    assert "abyss:verified-gate" in res.artifacts["walkthrough"]
    assert res.pytest_gate_runs and res.pytest_gate_runs[-1].passed is True
