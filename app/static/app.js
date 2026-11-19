/**
 * Modern Coding Agent Control Plane — Frontend Controller (app/static/app.js)
 * Adheres to modern web best practices:
 *   - Parameterized AbortSignal.timeout() on fetch calls (90s for long build/review/analyze)
 *   - document.startViewTransition() for smooth tab switches with graceful fallback
 *   - Object.groupBy() with reducer fallback for architectural layer grouping
 *   - IME-safe Enter handling (!event.isComposing && event.keyCode !== 229)
 *   - Native <dialog> modal API & accessible error banners (role="alert")
 *   - Origin-isolated Live Preview iframe (sandbox="allow-scripts" opaque origin only)
 */

const BUILD_PRESETS = {
  project_map_app: {
    instruction: '이 프로젝트 지도를 클릭해서 볼 수 있는 웹페이지를 만들어줘',
    label: '★ 프로젝트 지도 웹앱 (open-swe openwiki)',
  },
  inspection_dashboard: {
    instruction: '서비스 상태를 한눈에 보는 점검 대시보드 웹페이지를 만들어줘',
    label: '서비스 점검 대시보드',
  },
};

const state = {
  dashboard: null,
  learningCore: null,
  onboardPackage: null,
  codeModeScenarios: [],
  selectedCodeModeScenarioId: 'retry_policy',
  lastCodeModeResponse: null,
  ctrlOTranscriptExpanded: false,
  selectedWorkerDiff: 'worker-analyzer',
  selectedCoreId: 'isolation_policy',
  selectedArchifyNode: null,
  dispatcherMode: 'build',
  selectedBuildPreset: 'project_map_app',
  lastBuildResult: null,
  lastBuildTargetRepo: 'demo',
  lastReviewResult: null,
};

const escapeHtml = (raw = '') =>
  String(raw)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');

function formatColorizedDiff(diffText = '', maxLines = 240) {
  if (!diffText) return 'No diff recorded.';
  return diffText
    .split('\n')
    .slice(0, maxLines)
    .map((line) => {
      if (line.startsWith('+') && !line.startsWith('+++')) {
        return `<span class="diff-line-add">${escapeHtml(line)}</span>`;
      }
      if (line.startsWith('-') && !line.startsWith('---')) {
        return `<span class="diff-line-del">${escapeHtml(line)}</span>`;
      }
      return escapeHtml(line);
    })
    .join('\n');
}

function setBannerError(elementId, message = '') {
  const el = document.getElementById(elementId);
  if (!el) return;
  if (message) {
    el.textContent = message;
    el.setAttribute('data-visible', 'true');
  } else {
    el.textContent = '';
    el.setAttribute('data-visible', 'false');
  }
}

async function apiFetch(url, options = {}, timeoutMs = 25000) {
  const response = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers ?? {}),
    },
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!response.ok) {
    const errBody = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(errBody.detail || `HTTP ${response.status}`);
  }
  return response.json();
}

/* ============================================================================
 * Tab Navigation & Left Dispatcher Mode Switching
 * ============================================================================ */
function switchTab(targetPanelId) {
  const applySwitch = () => {
    document.querySelectorAll('.tab-btn').forEach((btn) => {
      const active = btn.dataset.tab === targetPanelId;
      btn.setAttribute('aria-selected', String(active));
    });
    document.querySelectorAll('.tab-panel').forEach((panel) => {
      panel.setAttribute('data-active', String(panel.id === targetPanelId));
    });
  };

  if (document.startViewTransition) {
    document.startViewTransition(applySwitch);
  } else {
    applySwitch();
  }
}

function switchDispatcherMode(mode) {
  state.dispatcherMode = mode;
  document.querySelectorAll('.mode-tab-btn').forEach((btn) => {
    btn.setAttribute('aria-pressed', String(btn.dataset.dispatcherMode === mode));
  });
  document.querySelectorAll('.dispatcher-mode-panel').forEach((panel) => {
    panel.setAttribute('data-active', String(panel.id === `dispatcher-panel-${mode}`));
  });
}

function selectBuildPreset(presetId) {
  const cfg = BUILD_PRESETS[presetId];
  if (!cfg) return;
  state.selectedBuildPreset = presetId;
  document.querySelectorAll('.build-preset-chip').forEach((btn) => {
    btn.setAttribute('data-active', String(btn.dataset.buildPreset === presetId));
  });
  const textarea = document.getElementById('build-instruction-input');
  if (textarea) {
    textarea.value = cfg.instruction;
  }
}

/* ============================================================================
 * Left Pane: Live Event Stream Renderer
 * ============================================================================ */
function renderEventStream(events = []) {
  const container = document.getElementById('event-stream-container');
  const countBadge = document.getElementById('event-count-badge');
  if (!container || !countBadge) return;
  countBadge.textContent = `${events.length} events`;

  container.innerHTML = events
    .map((evt) => {
      const kind = evt.event_type || 'info';
      const agent = evt.agent_id || 'harness';
      const branch = evt.branch ? ` (${evt.branch})` : '';
      let body = '';

      if (kind === 'thinking') {
        body = `<think> ${evt.thought || ''}`;
      } else if (kind === 'isolation_blocked') {
        body = `[BLOCKED by WorktreeIsolationPolicy · ${evt.check_name}]\n${evt.reason}\nRemediation: ${evt.remediation}`;
      } else if (kind === 'tool_executed') {
        body = `[Tool: ${evt.tool_name}] (${evt.check_name}: PASS)\n${evt.output_preview || ''}`;
      } else if (kind === 'code_mode_tool') {
        body = `[Code Mode -> ${evt.tool}] (${evt.decided_by})\n${evt.diff || evt.output_preview || ''}`;
      } else if (kind === 'code_mode_tool_refused') {
        body = `[Code Mode REFUSED -> ${evt.tool}] (${evt.decided_by})\n${evt.reason}`;
      } else if (kind === 'mailbox_sent') {
        body = `[Mailbox -> ${evt.recipient}] ${evt.content}`;
      } else if (kind === 'review_completed') {
        body = `[Evaluator Rubric] ${evt.worker_id} on ${evt.branch}: Score ${evt.score}/100 (${evt.approved ? 'APPROVED' : 'REJECTED'})`;
      } else if (kind === 'branch_merged') {
        body = `[Git Merge] Merged '${evt.branch}' into main (HEAD -> ${evt.main_head})`;
      } else if (kind === 'build_task_started') {
        body = `[Act 2 Build Started] preset=${evt.preset} repo=${evt.repo_name} worktree=${evt.worktree_path}`;
      } else if (kind === 'build_task_completed') {
        body = `[Act 2 Build ${String(evt.status || '').toUpperCase()}] task=${evt.task_id} preview=${evt.preview_url || 'none'} pushed=${evt.pushed_to_remote}`;
      } else if (kind === 'mr_review_completed') {
        body = `[Act 4 Review] ${evt.target}: ${evt.decision} (${evt.score}/100, ${evt.findings_count} findings)`;
      } else {
        body = evt.message || evt.summary || evt.goal || JSON.stringify(evt);
      }

      const displayKind =
        kind === 'mr_review_completed' || kind === 'build_task_completed'
          ? 'review_completed'
          : kind;

      return `
        <article class="stream-item" data-kind="${escapeHtml(displayKind)}">
          <div class="stream-meta">
            <span><strong>${escapeHtml(agent)}</strong>${escapeHtml(branch)}</span>
            <span>${escapeHtml(kind)}</span>
          </div>
          <div class="stream-body">${escapeHtml(body)}</div>
        </article>
      `;
    })
    .join('');

  container.scrollTop = container.scrollHeight;
}

/* ============================================================================
 * Tab 1: Focus Domains (Guardrails & Execution) vs Supporting Domains
 * ============================================================================ */
function renderLearningCore() {
  if (!state.learningCore) return;
  const { core_modules: coreMods = [], infra_modules: infraMods = [] } = state.learningCore;

  const cardsHtml = [
    ...coreMods.map((m) => {
      const isFocus = String(m.ownership || '').includes('★');
      return `
      <div class="stream-item" data-kind="${isFocus ? 'thinking' : 'info'}" style="cursor: pointer;" data-select-core="${escapeHtml(m.id)}">
        <div class="stream-meta">
          <span class="badge" data-tone="${isFocus ? 'core' : 'info'}">${escapeHtml(m.ownership || '★ Focus Domain')}</span>
          <span><code>${escapeHtml(m.file_path)}</code></span>
        </div>
        <div style="font-weight: 600; margin: 4px 0;">${escapeHtml(m.title)}</div>
        <div style="font-size: 12px; color: var(--color-text-secondary);">${escapeHtml(m.claude_pattern)}</div>
      </div>
    `;
    }),
    ...infraMods.map(
      (inf) => `
      <div class="stream-item" style="border-left-color: var(--color-border-strong); opacity: 0.92;">
        <div class="stream-meta">
          <span class="badge">${escapeHtml(inf.ownership || 'Supporting Domain / Shared Kernel')}</span>
          <span><code>${escapeHtml(inf.file_path)}</code></span>
        </div>
        <div style="font-size: 12.5px; color: var(--color-text-secondary);">${escapeHtml(inf.role)}</div>
      </div>
    `
    ),
  ].join('');

  const ownContainer = document.getElementById('ownership-cards-container');
  if (!ownContainer) return;
  ownContainer.innerHTML = cardsHtml;
  ownContainer.querySelectorAll('[data-select-core]').forEach((el) => {
    el.addEventListener('click', () => {
      state.selectedCoreId = el.getAttribute('data-select-core');
      renderSelectedCoreCode();
    });
  });

  renderSelectedCoreCode();
}

function renderSelectedCoreCode() {
  if (!state.learningCore) return;
  const target =
    state.learningCore.core_modules.find((m) => m.id === state.selectedCoreId) ||
    state.learningCore.core_modules[0];
  if (!target) return;

  document.getElementById('core-code-title').textContent = target.file_path;
  document.getElementById('core-code-why').textContent = `[설계 의도 & 핵심 포인트] ${target.why_different}`;
  document.getElementById('core-code-viewer').textContent = target.code;
}

/* ============================================================================
 * Tab 2: Act 2 Visual Build Result + Embedded Sandboxed Live Preview
 * ============================================================================ */
function renderBuildTaskResult() {
  const res = state.lastBuildResult;
  const emptyEl = document.getElementById('build-result-empty');
  const contentEl = document.getElementById('build-result-content');
  const statusBadge = document.getElementById('build-result-status-badge');
  const jumpBtn = document.getElementById('jump-to-review-built-btn');
  if (!res || !emptyEl || !contentEl) return;

  emptyEl.style.display = 'none';
  contentEl.style.display = 'grid';

  const passed = Boolean(res.test_result?.passed);
  const checks = Array.isArray(res.guardrail_checks) ? res.guardrail_checks : [];
  const passedChecks = checks.filter((c) => c.allowed !== false).length;

  statusBadge.textContent = `${String(res.status || 'completed').toUpperCase()} · ${res.branch_name}`;
  statusBadge.setAttribute('data-tone', res.status === 'completed' ? 'success' : 'danger');

  if (jumpBtn) {
    jumpBtn.style.display = 'inline-flex';
  }

  document.getElementById('build-result-meta').innerHTML = `
    <span class="badge" data-tone="info">branch: ${escapeHtml(res.branch_name)}</span>
    <span class="badge" data-tone="core">preset: ${escapeHtml(res.preset)}</span>
    <span class="badge">repo: ${escapeHtml(res.repo_name)} (${escapeHtml(res.source_type)})</span>
    <span class="badge" data-tone="${passed ? 'success' : 'danger'}">
      pytest: ${passed ? 'PASS' : 'FAIL'} (exit ${res.test_result?.exit_code ?? -1})
    </span>
    <span class="badge" data-tone="success">Guardrails: ${passedChecks}/${checks.length} PASS</span>
    <span class="badge" data-tone="${res.pushed_to_remote ? 'success' : 'info'}">
      ${res.pushed_to_remote ? 'Pushed to Remote (origin)' : 'Isolated Local Branch'}
    </span>
    <span class="badge">LLM: ${escapeHtml(res.provider_used || 'auto')}</span>
  `;

  document.getElementById('build-result-summary').textContent = res.summary || '';

  document.getElementById('build-changed-files').innerHTML = (res.changed_files || [])
    .map((f) => `<span class="file-pill">${escapeHtml(f)}</span>`)
    .join('');

  document.getElementById('build-diff-viewer').innerHTML = formatColorizedDiff(res.git_diff || '', 260);
  document.getElementById('build-pytest-viewer').textContent =
    res.test_result?.output || '(no pytest output)';

  // Embedded Live Preview (`sandbox="allow-scripts"` opaque origin only)
  const iframe = document.getElementById('build-live-preview-iframe');
  const link = document.getElementById('build-live-preview-link');
  if (res.preview_url && iframe && link) {
    iframe.setAttribute('sandbox', 'allow-scripts');
    iframe.src = res.preview_url;
    link.href = res.preview_url;
    link.style.display = 'inline-flex';
  }
}

async function runBuildTask(overrides = {}) {
  const buildBtn = document.getElementById('run-build-task-btn');
  const quickBtn = document.getElementById('tab2-quick-build-btn');
  const quickDashBtn = document.getElementById('tab2-quick-dashboard-btn');
  const runBadge = document.getElementById('run-status-badge');

  const repoInput = document.getElementById('build-repo-input');
  const instInput = document.getElementById('build-instruction-input');
  const pushCb = document.getElementById('build-push-remote-cb');
  const cmCb = document.getElementById('build-codemode-cb');

  const preset = overrides.preset || state.selectedBuildPreset || 'project_map_app';
  if (overrides.preset && BUILD_PRESETS[overrides.preset]) {
    selectBuildPreset(overrides.preset);
  }
  const repoUrlOrPath = (overrides.repo_url_or_path ?? repoInput?.value ?? 'demo').trim() || 'demo';
  const instruction =
    (overrides.instruction ?? instInput?.value ?? BUILD_PRESETS.project_map_app.instruction).trim() ||
    BUILD_PRESETS.project_map_app.instruction;
  const pushToRemote = overrides.push_to_remote ?? Boolean(pushCb?.checked);
  const useCodeMode = overrides.use_code_mode ?? Boolean(cmCb?.checked);

  setBannerError('build-error-banner', '');
  if (buildBtn) buildBtn.disabled = true;
  if (quickBtn) quickBtn.disabled = true;
  if (quickDashBtn) quickDashBtn.disabled = true;
  if (runBadge) {
    runBadge.textContent = `ACT 2 BUILDING (${preset})...`;
    runBadge.setAttribute('data-tone', 'info');
  }

  try {
    const res = await apiFetch(
      '/api/workflow/tasks/build',
      {
        method: 'POST',
        body: JSON.stringify({
          repo_url_or_path: repoUrlOrPath,
          instruction,
          preset,
          push_to_remote: pushToRemote,
          use_code_mode: useCodeMode,
        }),
      },
      90000
    );
    state.lastBuildResult = res;
    state.lastBuildTargetRepo = repoUrlOrPath;

    // Prime Act 4-B review inputs automatically with the newly built branch
    const revBranch = document.getElementById('review-branch-input');
    const revRepo = document.getElementById('review-repo-input');
    if (revBranch) revBranch.value = res.branch_name || '';
    if (revRepo) revRepo.value = repoUrlOrPath;

    renderBuildTaskResult();
    switchTab('panel-worktrees');

    if (runBadge) {
      runBadge.textContent = `BUILT ${res.branch_name} · PREVIEW READY`;
      runBadge.setAttribute('data-tone', 'success');
    }
  } catch (err) {
    setBannerError('build-error-banner', `빌드 실행 실패: ${err.message}`);
    if (runBadge) {
      runBadge.textContent = `BUILD ERROR`;
      runBadge.setAttribute('data-tone', 'danger');
    }
  } finally {
    if (buildBtn) buildBtn.disabled = false;
    if (quickBtn) quickBtn.disabled = false;
    if (quickDashBtn) quickDashBtn.disabled = false;
  }
}

/* ============================================================================
 * Tab 2 (Lower Section): 3-Worktree Team Cards, Git Graph & Rotation Logs
 * ============================================================================ */
function renderWorktreesAndTasks() {
  if (!state.dashboard) return;
  const { tasks = [], worktrees = {}, git_graph: gitGraph = [], rotator, handoffs = {} } = state.dashboard;

  const grid = document.getElementById('worktree-cards-grid');
  grid.innerHTML = tasks
    .map((t) => {
      const wt = worktrees[t.assigned_to] || {};
      const ho = handoffs[t.assigned_to] || {};
      const tone = t.status === 'merged' || t.status === 'approved' ? 'success' : 'info';
      return `
        <div class="panel-box">
          <div class="panel-title">
            <span><code>${escapeHtml(t.assigned_to)}</code></span>
            <span class="badge" data-tone="${tone}">${escapeHtml(t.status.toUpperCase())} · ${t.review_score}/100</span>
          </div>
          <div style="font-weight: 600; font-size: 13.5px;">${escapeHtml(t.title)}</div>
          <div style="font-family: var(--font-family-code); font-size: 11.5px; color: var(--color-worktree);">
            branch: ${escapeHtml(t.worktree_branch)} @ ${escapeHtml(t.commit_hash || wt.head_commit || 'HEAD')}
          </div>
          <div style="font-family: var(--font-family-code); font-size: 11px; color: var(--color-text-muted);">
            worktree: ${escapeHtml(wt.worktree_path || '')}
          </div>
          <div style="display: flex; gap: 6px; flex-wrap: wrap; margin-top: 4px;">
            <span class="badge" data-tone="info">LLM: ${escapeHtml(t.provider_used || 'auto')}</span>
            <span class="badge" data-tone="${ho.blocked_attempts > 0 ? 'danger' : 'success'}">
              Guard Blocked: ${ho.blocked_attempts ?? 0}
            </span>
            <span class="badge">Tools: ${ho.tool_calls_count ?? 0} calls</span>
          </div>
        </div>
      `;
    })
    .join('');

  document.getElementById('git-graph-viewer').textContent = gitGraph.map((g) => g.raw).join('\n');

  if (rotator) {
    document.getElementById('strategy-select').value = rotator.strategy;
    document.getElementById('single-provider-select').value = rotator.active_single_provider;
    document.getElementById('active-strategy-badge').textContent = `strategy: ${rotator.strategy}`;

    const rotLines = (rotator.recent_rotations || []).map(
      (r) =>
        `[${r.agent_id}] strategy=${r.strategy} -> ${r.selected_provider} (${r.latency_ms}ms)${
          r.fallback_triggered ? ` [FALLBACK: ${r.fallback_reason}]` : ''
        }`
    );
    const daPreview = state.dashboard.deepagents_preview || {};
    const headerBlock = [
      `=== LangChain DeepAgents Harness Bridge ===`,
      `Harness     : ${daPreview.harness || 'deepagents'}`,
      `Middlewares : ${(daPreview.middlewares || []).join(', ')}`,
      `Skills      : ${(daPreview.skills_loaded || []).join(', ')}`,
      `Active Route: ${daPreview.provider_id}:${daPreview.model} (${daPreview.base_url})`,
      ``,
      `=== Recent Provider Rotations ===`,
      ...rotLines,
    ].join('\n');
    document.getElementById('rotation-log-viewer').textContent = headerBlock;
  }
}

/* ============================================================================
 * Tab 3: Act 4 Independent MR / Branch Code Review Walkthrough + Mailbox
 * ============================================================================ */
function renderMrReviewResult() {
  const rev = state.lastReviewResult;
  const emptyEl = document.getElementById('mr-review-empty');
  const contentEl = document.getElementById('mr-review-content');
  const decisionBadge = document.getElementById('mr-review-decision-badge');
  const scoreBadge = document.getElementById('mr-review-score-badge');
  if (!rev || !emptyEl || !contentEl) return;

  emptyEl.style.display = 'none';
  contentEl.style.display = 'flex';

  const approved = rev.decision === 'APPROVE';
  decisionBadge.textContent = rev.decision;
  decisionBadge.setAttribute('data-tone', approved ? 'success' : 'danger');
  scoreBadge.textContent = `${rev.score} / 100`;
  scoreBadge.setAttribute('data-tone', approved ? 'success' : 'danger');

  const ds = rev.diff_stats || {};
  document.getElementById('mr-review-meta').innerHTML = `
    <span class="badge" data-tone="info">target: ${escapeHtml(rev.target || '')}</span>
    <span class="badge">source: ${escapeHtml(rev.source_type || 'diff')}</span>
    <span class="badge" data-tone="success">
      diff: ${ds.files_changed ?? 0} files (+${ds.additions ?? 0} / -${ds.deletions ?? 0})
    </span>
    <span class="badge" data-tone="success">${rev.read_only_verified ? 'Read-Only Verified' : 'Read-Only'}</span>
    <span class="badge">LLM: ${escapeHtml(rev.provider_used || 'auto')}</span>
  `;

  document.getElementById('mr-review-summary').textContent = rev.summary || '';

  const findings = rev.findings || [];
  const findingsEl = document.getElementById('mr-review-findings-list');
  if (!findings.length) {
    findingsEl.innerHTML = `
      <div class="stream-item" data-kind="review_completed">
        <div class="stream-meta"><span><strong>Clean Diff</strong></span><span class="badge" data-tone="success">0 findings</span></div>
        <div class="stream-body">보안 취약점(비밀키 하드코딩, eval/exec, 빈 except:pass)이나 테스트 누락이 발견되지 않았습니다.</div>
      </div>
    `;
  } else {
    findingsEl.innerHTML = findings
      .map((f) => {
        const sevTone = f.severity === 'high' ? 'danger' : f.severity === 'medium' ? 'core' : 'info';
        return `
          <div class="review-finding-card" data-severity="${escapeHtml(f.severity)}">
            <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap;">
              <span class="badge" data-tone="${sevTone}">${escapeHtml(String(f.severity).toUpperCase())}</span>
              <code style="font-size: 11.5px; color: var(--color-worktree);">${escapeHtml(f.file)}:${f.line}</code>
            </div>
            <div style="color: var(--color-text-primary); font-weight: 500;">${escapeHtml(f.comment)}</div>
            <div class="finding-suggestion"><strong>수정 제안:</strong> ${escapeHtml(f.suggestion)}</div>
          </div>
        `;
      })
      .join('');
  }

  document.getElementById('mr-review-rubric-list').innerHTML = (rev.rubric_breakdown || [])
    .map(
      (c) => `
      <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px; font-size: 12px; padding: 7px 10px; background: var(--color-card); border-radius: 4px; border: 1px solid var(--color-border);">
        <span><strong>${escapeHtml(c.name)}</strong>: ${escapeHtml(c.comment)}</span>
        <span class="badge" data-tone="${c.passed ? 'success' : 'danger'}">${c.score}/${c.max_score}</span>
      </div>
    `
    )
    .join('');

  const testWrap = document.getElementById('mr-review-test-wrap');
  const testPre = document.getElementById('mr-review-test-output');
  if (rev.test_result && testWrap && testPre) {
    testWrap.style.display = 'block';
    testPre.textContent = rev.test_result.output || `exit_code=${rev.test_result.exit_code}`;
  } else if (testWrap) {
    testWrap.style.display = 'none';
  }
}

async function runReviewTask(overrides = {}) {
  const runBtn = document.getElementById('run-review-task-btn');
  const defBtn = document.getElementById('review-preset-defective-btn');
  const clnBtn = document.getElementById('review-preset-clean-btn');
  const tab3Def = document.getElementById('tab3-quick-defective-btn');
  const tab3Cln = document.getElementById('tab3-quick-clean-btn');
  const runBadge = document.getElementById('run-status-badge');

  setBannerError('review-error-banner', '');
  [runBtn, defBtn, clnBtn, tab3Def, tab3Cln].forEach((b) => {
    if (b) b.disabled = true;
  });
  if (runBadge) {
    runBadge.textContent = 'ACT 4 REVIEWING CODE...';
    runBadge.setAttribute('data-tone', 'info');
  }

  try {
    let payload;
    if (overrides.preset === 'defective_mr') {
      payload = { preset: 'defective_mr', run_tests: false };
    } else if (overrides.preset === 'clean_branch') {
      payload = {
        repo_url_or_path: overrides.repo_url_or_path || state.lastBuildTargetRepo || 'demo',
        branch_name: overrides.branch_name || state.lastBuildResult?.branch_name || null,
        base_branch: overrides.base_branch || state.lastBuildResult?.base_branch || 'main',
        preset: 'clean_branch',
        run_tests: true,
      };
    } else {
      const rawDiff = (document.getElementById('review-raw-diff-input')?.value || '').trim();
      const repoVal = (document.getElementById('review-repo-input')?.value || 'demo').trim() || 'demo';
      const branchVal = (document.getElementById('review-branch-input')?.value || '').trim();
      const baseVal = (document.getElementById('review-base-input')?.value || 'main').trim() || 'main';
      const runTests = Boolean(document.getElementById('review-run-tests-cb')?.checked);
      payload = rawDiff
        ? { raw_diff: rawDiff, run_tests: false }
        : {
            repo_url_or_path: repoVal,
            branch_name: branchVal || state.lastBuildResult?.branch_name || null,
            base_branch: baseVal,
            preset: branchVal ? null : 'clean_branch',
            run_tests: runTests,
          };
    }

    const res = await apiFetch(
      '/api/workflow/tasks/review',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
      90000
    );
    state.lastReviewResult = res;
    renderMrReviewResult();
    switchTab('panel-mailbox');

    if (runBadge) {
      runBadge.textContent = `REVIEW: ${res.decision} (${res.score}/100)`;
      runBadge.setAttribute('data-tone', res.decision === 'APPROVE' ? 'success' : 'danger');
    }
  } catch (err) {
    setBannerError('review-error-banner', `코드 리뷰 실패: ${err.message}`);
    if (runBadge) {
      runBadge.textContent = 'REVIEW ERROR';
      runBadge.setAttribute('data-tone', 'danger');
    }
  } finally {
    [runBtn, defBtn, clnBtn, tab3Def, tab3Cln].forEach((b) => {
      if (b) b.disabled = false;
    });
  }
}

function renderMailboxAndReviews() {
  if (!state.dashboard) return;
  const { mailbox = [], reviews = {} } = state.dashboard;

  document.getElementById('mailbox-count-badge').textContent = `${mailbox.length} messages`;
  document.getElementById('mailbox-list').innerHTML = mailbox
    .map(
      (m) => `
      <div class="stream-item" data-kind="${m.sender === 'reviewer' ? 'review_completed' : 'info'}">
        <div class="stream-meta">
          <span><strong>${escapeHtml(m.sender)}</strong> &rarr; <strong>${escapeHtml(m.recipient)}</strong></span>
          <span>${escapeHtml(m.msg_id)}</span>
        </div>
        <div class="stream-body">${escapeHtml(m.content)}</div>
      </div>
    `
    )
    .join('');

  const rev = reviews[state.selectedWorkerDiff] || Object.values(reviews)[0];
  if (!rev) return;

  document.getElementById('review-card-title').textContent = `Reviewer Rubric & Git Diff — ${rev.worker_id} (${rev.branch_name})`;
  const scoreBadge = document.getElementById('review-score-badge');
  scoreBadge.textContent = `${rev.total_score} / 100 ${rev.approved ? 'APPROVED' : 'REJECTED'}`;
  scoreBadge.setAttribute('data-tone', rev.approved ? 'success' : 'danger');

  document.getElementById('review-criteria-list').innerHTML = (rev.criteria || [])
    .map(
      (c) => `
      <div style="display: flex; justify-content: space-between; align-items: center; font-size: 12px; padding: 6px 10px; background: var(--color-card); border-radius: 4px; border: 1px solid var(--color-border);">
        <span><strong>${escapeHtml(c.name)}</strong>: ${escapeHtml(c.comment)}</span>
        <span class="badge" data-tone="${c.passed ? 'success' : 'danger'}">${c.score}/${c.max_score}</span>
      </div>
    `
    )
    .join('');

  document.getElementById('worktree-diff-viewer').innerHTML = formatColorizedDiff(rev.diff || '', 220);
}

/* ============================================================================
 * Tab 4 & Tab 5: Act 1 RepoOnboard + Archify Interactive Architecture Map
 * ============================================================================ */
function isFocusDomainNode(node) {
  return Boolean(
    (node.file_path &&
      (node.file_path.includes('domains/guardrails/') ||
        node.file_path.includes('domains/execution/'))) ||
      (node.layer && node.layer.includes('★'))
  );
}

function renderOnboardAndArchify() {
  const pkg = state.onboardPackage;
  if (!pkg) return;

  const brief = pkg.onboarding_brief || {};
  const analysis = pkg.analysis || {};

  const srcBadge = document.getElementById('onboard-source-badge');
  if (srcBadge) {
    const srcType = analysis.source_type || 'local_path';
    srcBadge.textContent = `source: ${srcType}`;
    srcBadge.setAttribute('data-tone', srcType === 'local_path' ? 'success' : 'info');
  }

  document.getElementById('onboard-headline').textContent = brief.headline || pkg.repo_name;
  document.getElementById('onboard-stats-badge').textContent = `${brief.stats?.modules_count ?? 0} modules · ${brief.stats?.total_lines ?? 0} LOC`;
  document.getElementById('onboard-purpose-text').textContent = brief.purpose_summary || '';

  document.getElementById('onboard-reading-order').innerHTML = (brief.reading_order || [])
    .map(
      (r) => `
      <div style="padding: 8px 10px; background: var(--color-card); border: 1px solid var(--color-border); border-radius: 6px; font-size: 12.5px;">
        <span class="badge" data-tone="info">${escapeHtml(r.step)}</span>
        <code style="margin-left: 6px; color: var(--color-worktree);">${escapeHtml(r.file_path)}</code>
        <div style="color: var(--color-text-secondary); margin-top: 4px;">${escapeHtml(r.reason)}</div>
      </div>
    `
    )
    .join('');

  document.getElementById('onboard-layers-list').innerHTML = (brief.layers || [])
    .map(
      (l) => `
      <div style="padding: 8px 10px; background: var(--color-elevated); border-radius: 6px; font-size: 12px;">
        <div style="display: flex; justify-content: space-between;">
          <strong>${escapeHtml(l.layer_name)}</strong>
          <span class="badge">${l.module_count} files</span>
        </div>
        <div style="color: var(--color-text-muted); font-family: var(--font-family-code); font-size: 11px; margin-top: 4px;">
          ${escapeHtml((l.files || []).join(', '))}
        </div>
      </div>
    `
    )
    .join('');

  // Render Socratic Question Ladder
  const ladder = pkg.socratic_ladder || [];
  const qContainer = document.getElementById('socratic-questions-container');
  qContainer.innerHTML = ladder
    .map(
      (q) => `
      <div class="panel-box" style="padding: 14px; background: var(--color-card);" data-qid="${escapeHtml(q.id)}">
        <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px;">
          <span class="badge" data-tone="core">${escapeHtml(q.stage)}</span>
          <code style="font-size: 11px; color: var(--color-worktree);">${escapeHtml(q.source_evidence)}</code>
        </div>
        <div style="font-weight: 600; font-size: 13.5px;">${escapeHtml(q.question)}</div>
        <div style="font-size: 12px; color: var(--color-core-gold); background: var(--color-core-soft); padding: 6px 10px; border-radius: 4px;">
          <strong>Socratic Hint:</strong> ${escapeHtml(q.socratic_hint)}
        </div>
        <div style="display: flex; flex-direction: column; gap: 6px;">
          ${(q.options || [])
            .map(
              (opt, idx) => `
              <button type="button" class="socratic-option" data-qid="${escapeHtml(q.id)}" data-idx="${idx}">
                <span class="badge">${idx + 1}</span>
                <span>${escapeHtml(opt)}</span>
              </button>
            `
            )
            .join('')}
        </div>
        <div class="socratic-feedback" id="feedback-${escapeHtml(q.id)}" style="display: none; font-size: 12.5px; padding: 8px 10px; border-radius: 6px;"></div>
      </div>
    `
    )
    .join('');

  qContainer.querySelectorAll('.socratic-option').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const qid = btn.getAttribute('data-qid');
      const idx = Number(btn.getAttribute('data-idx'));
      try {
        const res = await apiFetch('/api/repo-onboard/answer', {
          method: 'POST',
          body: JSON.stringify({
            repo_url_or_path: document.getElementById('onboard-repo-input').value,
            question_id: qid,
            selected_index: idx,
          }),
        });

        const parent = btn.closest('[data-qid]');
        parent.querySelectorAll('.socratic-option').forEach((b, bIdx) => {
          b.removeAttribute('data-result');
          if (bIdx === idx) {
            b.setAttribute('data-result', res.is_correct ? 'correct' : 'wrong');
          }
        });

        const fb = document.getElementById(`feedback-${qid}`);
        fb.style.display = 'block';
        fb.style.background = res.is_correct ? 'var(--color-accent-Soft)' : 'var(--color-danger-soft)';
        fb.style.color = res.is_correct ? 'var(--color-accent)' : '#fca5a5';
        fb.innerHTML = `<strong>${escapeHtml(res.coach_response)}</strong><br/><span style="color: var(--color-text-secondary);">Follow-up: ${escapeHtml(res.follow_up_prompt)}</span>`;
      } catch (err) {
        setBannerError('onboard-error-banner', `문답 확인 오류: ${err.message}`);
      }
    });
  });

  // Render Archify Interactive Architecture Map (Tab 5)
  renderArchifyDiagram(analysis);
}

function renderArchifyDiagram(analysis) {
  const graph = analysis?.archify_graph || { nodes: [], edges: [] };
  const nodes = graph.nodes || [];
  const edges = graph.edges || [];

  document.getElementById('archify-mermaid-viewer').textContent = analysis?.mermaid || '';

  const grouped = Object.groupBy
    ? Object.groupBy(nodes, (n) => n.layer || 'Application')
    : nodes.reduce((acc, n) => {
        const k = n.layer || 'Application';
        (acc[k] = acc[k] || []).push(n);
        return acc;
      }, {});

  const canvas = document.getElementById('archify-canvas');
  canvas.innerHTML = Object.entries(grouped)
    .map(
      ([layerName, layerNodes]) => `
      <div class="archify-layer-row">
        <div class="layer-label">
          <span>${escapeHtml(layerName)}</span>
          <span class="badge">${layerNodes.length} modules</span>
        </div>
        <div class="layer-nodes">
          ${layerNodes
            .map((n) => {
              const isCore = isFocusDomainNode(n);
              return `
                <button type="button" class="archify-node" data-node-id="${escapeHtml(n.id)}" data-core="${isCore}">
                  <div class="node-title">${escapeHtml(n.label)}</div>
                  <div class="node-path">${escapeHtml(n.file_path)}</div>
                  <div class="node-desc">${escapeHtml(n.purpose)}</div>
                </button>
              `;
            })
            .join('')}
        </div>
      </div>
    `
    )
    .join('');

  canvas.querySelectorAll('.archify-node').forEach((btn) => {
    btn.addEventListener('click', () => {
      const nodeId = btn.getAttribute('data-node-id');
      canvas.querySelectorAll('.archify-node').forEach((b) => {
        b.setAttribute('data-selected', String(b.getAttribute('data-node-id') === nodeId));
      });
      const nodeObj = nodes.find((n) => n.id === nodeId);
      if (!nodeObj) return;

      const outgoing = edges.filter((e) => e.source === nodeId).map((e) => e.target);
      const incoming = edges.filter((e) => e.target === nodeId).map((e) => e.source);
      const focusTone = isFocusDomainNode(nodeObj) ? 'core' : 'info';

      document.getElementById('archify-inspector-title').textContent = nodeObj.file_path;
      document.getElementById('archify-inspector-body').innerHTML = `
        <div><span class="badge" data-tone="${focusTone}">${escapeHtml(nodeObj.layer)}</span></div>
        <div><strong>Purpose:</strong> ${escapeHtml(nodeObj.purpose)}</div>
        <div><strong>Exported Symbols:</strong> <code>${escapeHtml((nodeObj.symbols || []).join(', ') || 'module-level')}</code></div>
        <div><strong>Imports From (${outgoing.length}):</strong> <code>${escapeHtml(outgoing.join(', ') || 'None (Pure Stdlib Leaf)')}</code></div>
        <div><strong>Imported By (${incoming.length}):</strong> <code>${escapeHtml(incoming.join(', ') || 'Entry Point')}</code></div>
      `;
    });
  });

  const firstBtn = canvas.querySelector('.archify-node');
  if (firstBtn) firstBtn.click();
}

/* ============================================================================
 * Tab 6: Act 2 & 3 — Code Mode (REPL) & Security Guardrails Lab
 * ============================================================================ */
function renderCodeModeScenarios() {
  const btnContainer = document.getElementById('codemode-scenario-buttons');
  if (!btnContainer || !state.codeModeScenarios.length) return;

  btnContainer.innerHTML = state.codeModeScenarios
    .map((sc) => {
      const isActive = sc.id === state.selectedCodeModeScenarioId;
      return `
        <button
          type="button"
          class="btn ${isActive ? 'btn-primary' : ''}"
          data-cm-scenario="${escapeHtml(sc.id)}"
        >
          <span>${escapeHtml(sc.title)}</span>
          <span class="badge" data-tone="${isActive ? 'core' : 'info'}">${escapeHtml(sc.badge)}</span>
        </button>
      `;
    })
    .join('');

  btnContainer.querySelectorAll('[data-cm-scenario]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const scId = btn.getAttribute('data-cm-scenario');
      selectCodeModeScenario(scId, true);
    });
  });
}

function selectCodeModeScenario(scenarioId, autoRun = false) {
  state.selectedCodeModeScenarioId = scenarioId;
  const sc =
    state.codeModeScenarios.find((s) => s.id === scenarioId) || state.codeModeScenarios[0];
  if (!sc) return;

  renderCodeModeScenarios();
  document.getElementById('codemode-learning-point').textContent = `[핵심 포인트 · ${sc.doc_section}] ${sc.learning_point}`;
  document.getElementById('codemode-js-reference').textContent = sc.claude_js_code;
  document.getElementById('codemode-editor').value = sc.python_code;

  if (autoRun) {
    runCodeModeLab();
  }
}

function toggleCtrlOTranscript(forceState) {
  state.ctrlOTranscriptExpanded =
    typeof forceState === 'boolean' ? forceState : !state.ctrlOTranscriptExpanded;
  const box = document.getElementById('codemode-transcript-box');
  const btn = document.getElementById('toggle-ctrlo-btn');
  if (!box || !btn) return;

  box.style.display = state.ctrlOTranscriptExpanded ? 'block' : 'none';
  btn.textContent = state.ctrlOTranscriptExpanded
    ? 'Ctrl+O: Transcript Code 접기'
    : 'Ctrl+O: Transcript Code 펼치기';
}

async function runCodeModeLab() {
  const code = document.getElementById('codemode-editor')?.value || '';
  const slowHook = Boolean(document.getElementById('codemode-slow-hook-cb')?.checked);
  const hookOnFailure = document.getElementById('codemode-hook-onfailure')?.value || 'block';
  const badge = document.getElementById('codemode-status-badge');
  const runBtn = document.getElementById('run-codemode-btn');

  if (runBtn) runBtn.disabled = true;
  if (badge) {
    badge.textContent = 'Running REPL...';
    badge.setAttribute('data-tone', 'info');
  }

  try {
    const resp = await apiFetch('/api/code-mode/run', {
      method: 'POST',
      body: JSON.stringify({
        code,
        scenario_id: state.selectedCodeModeScenarioId,
        reset_sandbox: true,
        simulate_slow_hook: slowHook,
        hook_on_failure: hookOnFailure,
      }),
    });
    state.lastCodeModeResponse = resp;
    renderCodeModeOutput();
  } catch (err) {
    if (badge) {
      badge.textContent = `Error: ${err.message}`;
      badge.setAttribute('data-tone', 'danger');
    }
  } finally {
    if (runBtn) runBtn.disabled = false;
  }
}

function renderCodeModeOutput() {
  const resp = state.lastCodeModeResponse;
  if (!resp) return;
  const { result, comparison } = resp;

  const badge = document.getElementById('codemode-status-badge');
  if (badge) {
    const isOk = result.status === 'ok';
    badge.textContent = `${result.status.toUpperCase()} · ${result.tool_calls.length} inner calls · ${result.duration_ms}ms`;
    badge.setAttribute('data-tone', isOk ? 'success' : 'danger');
  }

  // 1) Metrics comparison bar (fixed: uses var(--color-worktree), not undefined --color-info)
  const metricsBar = document.getElementById('codemode-metrics-bar');
  if (metricsBar && comparison) {
    metricsBar.innerHTML = `
      <div class="stream-item" style="border-left-color: var(--color-accent);">
        <div class="stream-meta"><span>LLM Round-Trips</span><span class="badge" data-tone="success">1 Step</span></div>
        <div style="font-size: 13px; font-weight: 600;">
          Direct: ${comparison.direct_mode_llm_turns}회 왕복 → Code Mode: <strong>1회</strong>
        </div>
      </div>
      <div class="stream-item" style="border-left-color: var(--color-worktree);">
        <div class="stream-meta"><span>Inner Tool Calls</span><span class="badge" data-tone="info">REPL Gate</span></div>
        <div style="font-size: 13px; font-weight: 600;">
          코드 내부 도구 ${comparison.inner_tool_calls_executed}건 개별 권한/격리 검증
        </div>
      </div>
      <div class="stream-item" style="border-left-color: var(--color-core-gold);">
        <div class="stream-meta"><span>Context Return Size</span><span class="badge" data-tone="core">Filtered</span></div>
        <div style="font-size: 13px; font-weight: 600;">
          ${comparison.returned_to_model_chars} chars 반환 (${comparison.context_saved_pct}% 절감)
        </div>
      </div>
    `;
  }

  // 2) Ctrl+O Transcript Code Box
  const transcriptCode = document.getElementById('codemode-transcript-code');
  const reviewBadge = document.getElementById('codemode-review-badge');
  if (transcriptCode) {
    transcriptCode.textContent = result.code;
  }
  if (reviewBadge && result.review) {
    reviewBadge.textContent = result.review.allowed
      ? 'REPL Static Review: PASS'
      : `REPL Static Review: BLOCKED (${result.review.findings.join('; ')})`;
    reviewBadge.setAttribute('data-tone', result.review.allowed ? 'success' : 'danger');
  }

  // 3) Conversation Tool Calls & Diffs
  const listEl = document.getElementById('codemode-toolcalls-list');
  if (listEl) {
    if (!result.tool_calls.length) {
      listEl.innerHTML = `
        <div class="stream-item" data-kind="isolation_blocked">
          <div class="stream-meta"><span><strong>REPL PreToolUse Review</strong></span><span>Refused before execution</span></div>
          <div class="stream-body">${escapeHtml(result.error || (result.review?.findings || []).join('\n'))}</div>
        </div>
      `;
    } else {
      listEl.innerHTML = result.tool_calls
        .map((tc) => {
          const kind = tc.allowed ? 'tool_executed' : 'isolation_blocked';
          const tone = tc.allowed ? 'success' : 'danger';
          const detail = tc.diff
            ? `[Unified Diff shown in conversation]\n${tc.diff}`
            : tc.allowed
              ? tc.output_preview || '(completed)'
              : `[REFUSED by ${tc.decided_by}]\n${tc.reason}`;
          return `
            <article class="stream-item" data-kind="${kind}">
              <div class="stream-meta">
                <span><strong>#${tc.seq} · ${escapeHtml(tc.tool)}</strong> <code>${escapeHtml(JSON.stringify(tc.arguments))}</code></span>
                <span class="badge" data-tone="${tone}">${escapeHtml(tc.decided_by)} (${tc.duration_ms}ms)</span>
              </div>
              <div class="stream-body">${escapeHtml(detail)}</div>
            </article>
          `;
        })
        .join('');
    }
  }

  // 4) Final expression returned to model
  const retViewer = document.getElementById('codemode-return-viewer');
  if (retViewer) {
    retViewer.textContent = result.model_view || result.return_text || result.error;
  }
}

/* ============================================================================
 * Initialization & Event Wiring
 * ============================================================================ */
async function refreshAll() {
  const [dash, core, onboard, cmScenarios] = await Promise.all([
    apiFetch('/api/state'),
    apiFetch('/api/learning-core'),
    apiFetch(
      '/api/repo-onboard/analyze',
      {
        method: 'POST',
        body: JSON.stringify({ repo_url_or_path: 'self' }),
      },
      90000
    ),
    apiFetch('/api/code-mode/scenarios'),
  ]);

  state.dashboard = dash;
  state.learningCore = core;
  state.onboardPackage = onboard;
  state.codeModeScenarios = cmScenarios.scenarios || [];

  renderEventStream(dash.recent_events || []);
  renderLearningCore();
  renderWorktreesAndTasks();
  renderMailboxAndReviews();
  renderOnboardAndArchify();
  selectCodeModeScenario(state.selectedCodeModeScenarioId, true);
  populateModalInputs(dash.rotator);
}

function populateModalInputs(rotator) {
  if (!rotator) return;
  const p = rotator.providers || {};
  if (p.qwen) {
    document.getElementById('qwen-base-url').value = p.qwen.base_url || '';
    document.getElementById('qwen-model').value = p.qwen.model || '';
  }
  if (p.gemini) {
    document.getElementById('gemini-base-url').value = p.gemini.base_url || '';
    document.getElementById('gemini-model').value = p.gemini.model || '';
  }
  if (p.openai) {
    document.getElementById('openai-base-url').value = p.openai.base_url || '';
    document.getElementById('openai-model').value = p.openai.model || '';
  }
  const rm = rotator.role_mapping || {};
  if (rm.lead) document.getElementById('role-map-lead').value = rm.lead;
  if (rm['worker-analyzer']) document.getElementById('role-map-worker').value = rm['worker-analyzer'];
  if (rm.reviewer) document.getElementById('role-map-reviewer').value = rm.reviewer;
}

document.addEventListener('DOMContentLoaded', async () => {
  // Tab buttons
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  // 4-Act Story Ribbon quick-jump buttons
  document.querySelectorAll('.story-act-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const act = btn.dataset.act;
      if (act === 'act1') {
        switchTab('panel-onboard');
      } else if (act === 'act2') {
        switchDispatcherMode('build');
        switchTab('panel-worktrees');
      } else if (act === 'act3') {
        switchTab('panel-codemode');
        selectCodeModeScenario('compound_cmd_trap', true);
      } else if (act === 'act4') {
        switchDispatcherMode('review');
        switchTab('panel-mailbox');
      }
    });
  });

  // Left Dispatcher 3-Mode buttons
  document.querySelectorAll('.mode-tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => switchDispatcherMode(btn.dataset.dispatcherMode));
  });

  // Left Mode 1 (Act 2 Build) target chips & preset chips
  document.querySelectorAll('.build-target-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = btn.getAttribute('data-build-target') || 'demo';
      document.getElementById('build-repo-input').value = target;
      document.querySelectorAll('.build-target-btn').forEach((b) => {
        b.setAttribute('data-active', String(b === btn));
      });
    });
  });

  document.querySelectorAll('.build-preset-chip').forEach((btn) => {
    btn.addEventListener('click', () => selectBuildPreset(btn.getAttribute('data-build-preset')));
  });

  document.getElementById('run-build-task-btn')?.addEventListener('click', () => runBuildTask());
  document.getElementById('tab2-quick-build-btn')?.addEventListener('click', () =>
    runBuildTask({ preset: 'project_map_app', instruction: BUILD_PRESETS.project_map_app.instruction })
  );
  document.getElementById('tab2-quick-dashboard-btn')?.addEventListener('click', () =>
    runBuildTask({ preset: 'inspection_dashboard', instruction: BUILD_PRESETS.inspection_dashboard.instruction })
  );
  document.getElementById('jump-to-review-built-btn')?.addEventListener('click', () => {
    switchDispatcherMode('review');
    runReviewTask({ preset: 'clean_branch' });
  });

  // Left Mode 2 (Act 4 Review) & Tab 3 quick review buttons
  document.getElementById('review-preset-defective-btn')?.addEventListener('click', () =>
    runReviewTask({ preset: 'defective_mr' })
  );
  document.getElementById('review-preset-clean-btn')?.addEventListener('click', () =>
    runReviewTask({ preset: 'clean_branch' })
  );
  document.getElementById('tab3-quick-defective-btn')?.addEventListener('click', () =>
    runReviewTask({ preset: 'defective_mr' })
  );
  document.getElementById('tab3-quick-clean-btn')?.addEventListener('click', () =>
    runReviewTask({ preset: 'clean_branch' })
  );
  document.getElementById('run-review-task-btn')?.addEventListener('click', () => runReviewTask());

  // Core file selector buttons
  document.querySelectorAll('.core-file-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.selectedCoreId = btn.getAttribute('data-core-id');
      renderSelectedCoreCode();
    });
  });

  // Code Mode Lab controls & Ctrl+O shortcut
  document.getElementById('run-codemode-btn')?.addEventListener('click', () => runCodeModeLab());
  document.getElementById('toggle-ctrlo-btn')?.addEventListener('click', () => toggleCtrlOTranscript());
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'o') {
      e.preventDefault();
      toggleCtrlOTranscript();
    }
  });

  // Worker diff selector buttons
  document.querySelectorAll('.diff-select-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.selectedWorkerDiff = btn.getAttribute('data-worker');
      renderMailboxAndReviews();
    });
  });

  // Quick rotation dropdowns in header
  document.getElementById('strategy-select').addEventListener('change', async (e) => {
    const res = await apiFetch('/api/providers/config', {
      method: 'POST',
      body: JSON.stringify({ strategy: e.target.value }),
    });
    state.dashboard.rotator = res.rotator;
    renderWorktreesAndTasks();
  });

  document.getElementById('single-provider-select').addEventListener('change', async (e) => {
    const res = await apiFetch('/api/providers/config', {
      method: 'POST',
      body: JSON.stringify({ active_single_provider: e.target.value }),
    });
    state.dashboard.rotator = res.rotator;
    renderWorktreesAndTasks();
  });

  // Native <dialog> modal controls
  const modal = document.getElementById('provider-modal');
  document.getElementById('open-provider-modal-btn').addEventListener('click', () => modal.showModal());
  document.getElementById('close-provider-modal-btn').addEventListener('click', () => modal.close());

  document.querySelectorAll('.test-provider-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const pid = btn.getAttribute('data-provider');
      const fb = document.getElementById('provider-test-feedback');
      fb.textContent = `Testing connection to '${pid}'...`;
      await saveModalSettings(false);
      const res = await apiFetch(`/api/providers/test/${pid}`, { method: 'POST' });
      fb.style.color = res.ok ? 'var(--color-accent)' : 'var(--color-danger)';
      fb.textContent = `[${pid.toUpperCase()}] ${res.message}`;
    });
  });

  async function saveModalSettings(closeAfter = true) {
    const workerRole = document.getElementById('role-map-worker').value;
    const payload = {
      strategy: document.getElementById('strategy-select').value,
      active_single_provider: document.getElementById('single-provider-select').value,
      role_mapping: {
        lead: document.getElementById('role-map-lead').value,
        builder: workerRole,
        'worker-analyzer': workerRole,
        'worker-tutor': workerRole,
        'worker-web': workerRole,
        reviewer: document.getElementById('role-map-reviewer').value,
      },
      providers: {
        qwen: {
          base_url: document.getElementById('qwen-base-url').value,
          model: document.getElementById('qwen-model').value,
          api_key: document.getElementById('qwen-api-key').value,
        },
        gemini: {
          base_url: document.getElementById('gemini-base-url').value,
          model: document.getElementById('gemini-model').value,
          api_key: document.getElementById('gemini-api-key').value,
        },
        openai: {
          base_url: document.getElementById('openai-base-url').value,
          model: document.getElementById('openai-model').value,
          api_key: document.getElementById('openai-api-key').value,
        },
      },
    };
    const res = await apiFetch('/api/providers/config', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    state.dashboard.rotator = res.rotator;
    renderWorktreesAndTasks();
    if (closeAfter) modal.close();
  }

  document.getElementById('save-provider-config-btn').addEventListener('click', () => saveModalSettings(true));

  // Mode 3: Run Parallel 3-Worktree Team Build & Merge
  const runBtn = document.getElementById('run-workflow-btn');
  const runBadge = document.getElementById('run-status-badge');
  runBtn.addEventListener('click', async () => {
    runBtn.disabled = true;
    runBadge.textContent = 'RUNNING 3 PARALLEL WORKTREES...';
    runBadge.setAttribute('data-tone', 'info');
    try {
      const goal = document.getElementById('workflow-goal-input').value;
      const engineMode = document.getElementById('engine-select').value;
      const dash = await apiFetch(
        '/api/workflow/run',
        {
          method: 'POST',
          body: JSON.stringify({ goal, engine_mode: engineMode }),
        },
        90000
      );
      state.dashboard = dash;
      renderEventStream(dash.recent_events || []);
      renderWorktreesAndTasks();
      renderMailboxAndReviews();
      runBadge.textContent = 'ALL 3 WORKTREES REVIEWED & MERGED';
      runBadge.setAttribute('data-tone', 'success');
    } catch (err) {
      runBadge.textContent = `ERROR: ${err.message}`;
      runBadge.setAttribute('data-tone', 'danger');
    } finally {
      runBtn.disabled = false;
    }
  });

  // Tab 4: Act 1 RepoOnboard Analysis & Preset Buttons (with try/catch + error banner)
  async function triggerRepoAnalyze(targetValue) {
    const input = document.getElementById('onboard-repo-input');
    const analyzeBtn = document.getElementById('analyze-repo-btn');
    if (targetValue) input.value = targetValue;
    setBannerError('onboard-error-banner', '');
    if (analyzeBtn) analyzeBtn.disabled = true;
    try {
      const pkg = await apiFetch(
        '/api/repo-onboard/analyze',
        {
          method: 'POST',
          body: JSON.stringify({ repo_url_or_path: input.value.trim() || 'self' }),
        },
        90000
      );
      state.onboardPackage = pkg;
      renderOnboardAndArchify();
    } catch (err) {
      setBannerError(
        'onboard-error-banner',
        `저장소 분석 실패: ${err.message}\n힌트: 로컬 폴더(self 또는 /path/to/my-project), 데모 샘플(demo), 또는 사내 GitLab URL(https://gitlab.internal/...)을 확인하세요.`
      );
    } finally {
      if (analyzeBtn) analyzeBtn.disabled = false;
    }
  }

  document.getElementById('analyze-repo-btn').addEventListener('click', () => triggerRepoAnalyze());
  document.querySelectorAll('.quick-repo-btn').forEach((btn) => {
    btn.addEventListener('click', () => triggerRepoAnalyze(btn.getAttribute('data-target')));
  });

  // IME-safe Enter submit on repo input (catches Safari WebKit bug 165004 via keyCode === 229)
  document.getElementById('onboard-repo-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      if (e.isComposing || e.keyCode === 229) return;
      triggerRepoAnalyze();
    }
  });

  // Connect SSE EventStream for real-time updates
  const evtSource = new EventSource('/api/events/stream');
  evtSource.onmessage = (msg) => {
    try {
      const parsed = JSON.parse(msg.data);
      if (state.dashboard?.recent_events) {
        state.dashboard.recent_events.push(parsed);
        renderEventStream(state.dashboard.recent_events);
      }
    } catch {
      // Ignore heartbeat comments
    }
  };

  try {
    await refreshAll();
  } catch (err) {
    setBannerError('onboard-error-banner', `초기 데이터 로드 실패: ${err.message}`);
  }
});
