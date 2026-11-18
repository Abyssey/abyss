# DESIGN.md — Abyss Multi-Agent Coding Harness & RepoOnboard

> Generated and verified with:
> - **Component System**: `meta/astryx` (`@astryxdesign/core@0.7.16`, `@astryxdesign/theme-neutral@0.7.16`)
> - **Web Platform Standards**: `GoogleChrome/modern-web-guidance` (v1.0.1)
> - **Design System Database**: `nextlevelbuilder/ui-ux-pro-max-skill` (`search.py --design-system -p "Abyss Harness"`)
> - **Layout & Anti-Slop Gates**: `Nutlope/hallmark` & `pbakaus/impeccable`
> - **Interactive Architecture Map**: `tt-a1i/archify` (`archify-ir-3.2`)

## 1. Visual Direction & Domain Fit
- **Domain**: AI Coding Agent Control Plane, Multi-Agent Git Worktree Orchestrator, Codebase Onboarding (`RepoOnboard`)
- **Style**: `Dark Mode (OLED) — Code Dark + Run Green`
- **Aesthetic Principles**:
  - High-density developer workspace inspired by Claude Code, OpenAI Codex, and Antigravity
  - Zero decorative slop: no purple-to-blue hero gradients, no nested cards inside cards, no emoji icons (crisp inline SVG icons only)
  - Clear visual separation between **[My Learning Core (User-Implemented Alignment Modules)]** and **[AI-Built Infrastructure & UI]**

## 2. Color Tokens (Mapped to `@astryxdesign/theme-neutral` & `oklch`)
| Token | Value | Role |
| :--- | :--- | :--- |
| `--surface-bg` | `#0B0F19` / `oklch(0.16 0.02 255)` | Root OLED background |
| `--surface-panel` | `#111827` / `oklch(0.21 0.025 255)` | Primary control plane pane |
| `--surface-elevated` | `#1B2336` / `oklch(0.25 0.03 255)` | Interactive card / worktree node |
| `--border-hairline` | `#2A364F` | 1px structural divider |
| `--text-primary` | `#F8FAFC` | Primary readable text (contrast > 14:1) |
| `--text-secondary` | `#94A3B8` | Supporting metadata & file paths |
| `--accent-run` | `#22C55E` | Run Green (Active agent / Approved / Merged) |
| `--accent-info` | `#38BDF8` | Sky Cyan (Worktree branch / Mailbox contract) |
| `--accent-core` | `#F59E0B` | Amber Gold (Highlights User's Learning Core modules) |
| `--accent-danger` | `#EF4444` | Worktree Isolation Refusal / Test failure |

## 3. Typography (`ui-ux-pro-max` Recommendation)
- **UI & Headings (`--font-family-body`, `--font-family-heading`)**: `'IBM Plex Sans'`, system-ui, sans-serif
- **Code, Diffs, Worktree Paths, & Logs (`--font-family-code`)**: `'JetBrains Mono'`, ui-monospace, monospace
