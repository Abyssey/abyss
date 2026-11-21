# DESIGN.md — Modern Coding Agent Architecture Control Plane

> Verified UI foundations:
> - **Component System**: Meta Astryx (`@astryxdesign/core`, `@astryxdesign/theme-neutral`, locally bundled under `app/static/vendor/`)
> - **Web Platform Standards**: Native `<dialog closedby="any">`, CSS Nesting, `AbortSignal.timeout()`, `document.startViewTransition()`, IME-safe Enter handling, accessible error announcements (`role="alert"`)
> - **Air-Gap Pod Readiness**: 100% self-contained static assets with zero external CDN or Google Fonts requests
> - **Interactive Architecture Map**: Layered Modern DDD graph highlighting `★ Focus Domains (guardrails · execution)`

## 1. Visual Direction & Domain Fit
- **Domain**: Modern Coding Agent Control Plane — Isolated Worktree Build & Live Preview, Read-Only Walkthrough Code Review, Deterministic Guardrails & Code Mode REPL Lab, Codebase Onboarding
- **Style**: `Dark Mode (OLED) — Code Dark + Run Green`
- **Aesthetic Principles**:
  - High-density developer workspace aligned with modern coding agent control planes (`open-swe`, Claude Code, Codex)
  - Zero decorative slop: no purple-to-blue hero gradients, no nested cards inside cards, crisp inline SVG icons only
  - Clear visual separation between **Focus Domains (`app/domains/guardrails/`, `app/domains/execution/`)** highlighted in Amber Gold and **Supporting Domains & Shared Kernel (`workflow`, `onboarding`, `core`)**
  - Prominent **Engine & Live LLM Status Pills** in the top bar (`langchain-ai/deepagents v0.7` + `🟢 LIVE LLM` / `⛔ LLM CONFIG REQUIRED` fail-fast guidance banner)
  - **Antigravity 3-Phase Artifact Cards** (`implementation_plan.md` with inline comment + **Proceed** approval bar for `request_review`, `task.md` synced from `write_todos`, and `pytest`-stamped `walkthrough.md`) paired with `Turn N: Tool -> Observation` & Dual Gate telemetry
  - Sandboxed Live Preview for built HTML artifacts using `<iframe sandbox="allow-scripts">` (never granting `allow-same-origin`) paired with an explicit `"새 탭으로 열기"` link

## 2. Color Tokens (Mapped to `@astryxdesign/theme-neutral` & `oklch`)
| Token | Value | Role |
| :--- | :--- | :--- |
| `--color-background` | `#0B0F19` / `oklch(0.16 0.02 255)` | Root OLED background |
| `--color-surface` | `#0F172A` / `oklch(0.21 0.025 255)` | Primary control plane pane |
| `--color-card` | `#161F33` / `oklch(0.25 0.03 255)` | Interactive card / worktree node |
| `--color-border` | `#2A364F` | 1px structural hairline divider |
| `--color-text-primary` | `#F8FAFC` | Primary readable text (contrast > 14:1) |
| `--color-text-muted` | `#94A3B8` | Supporting metadata & file paths |
| `--color-accent` | `#22C55E` | Run Green (Active agent / Approved / Merged / Passed) |
| `--color-worktree` | `#38BDF8` | Sky Cyan (Worktree branch / Mailbox / Info) |
| `--color-core-gold` | `#F59E0B` | Amber Gold (Focus Domains: `guardrails` & `execution`) |
| `--color-danger` | `#EF4444` | Guardrail Refusal / `REQUEST_CHANGES` / Test failure |

## 3. Air-Gap Typography Stack
- **UI & Headings (`--font-family-body`, `--font-family-heading`)**: `'IBM Plex Sans', 'Pretendard', 'Noto Sans KR', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif` (zero external font network fetches)
- **Code, Diffs, Worktree Paths & Logs (`--font-family-code`)**: `'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace`
