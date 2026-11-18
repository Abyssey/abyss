/**
 * Abyss Control Plane — Modern Frontend Controller (abyss/web/static/app.js)
 * Adheres to GoogleChrome/modern-web-guidance:
 *   - AbortSignal.timeout() on fetch calls
 *   - document.startViewTransition() for smooth tab switches
 *   - Object.groupBy() for architectural layer grouping
 *   - IME-safe Enter handling (!event.isComposing)
 *   - Native <dialog> modal API
 */

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
};

const escapeHtml = (raw = '') =>
  String(raw)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');

async function apiFetch(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers ?? {}),
    },
    signal: AbortSignal.timeout(25000),
  });
  if (!response.ok) {
    const errBody = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(errBody.detail || `HTTP ${response.status}`);
  }
  return response.json();
}

/* ============================================================================
 * Tab Navigation (with View Transitions API fallback)
 * ============================================================================ */
function switchTab(targetPanelId) {
  const applySwitch = () => {
    document.querySelectorAll('.tab-btn').forEach((btn) => {
      btn.setAttribute('aria-selected', String(btn.dataset.tab === targetPanelId));
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

/* ============================================================================
 * Left Pane: Live Event Stream Renderer
 * ============================================================================ */
function renderEventStream(events = []) {
  const container = document.getElementById('event-stream-container');
  const countBadge = document.getElementById('event-count-badge');
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
      } else {
        body = evt.message || evt.summary || evt.goal || JSON.stringify(evt);
      }

      return `
        <article class="stream-item" data-kind="${escapeHtml(kind)}">
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
 * Tab 1: My Learning Core & Ownership Split
 * ============================================================================ */
function renderLearningCore() {
  if (!state.learningCore) return;
  const { core_modules: coreMods, infra_modules: infraMods } = state.learningCore;

  const cardsHtml = [
    ...coreMods.map(
      (m) => `
      <div class="stream-item" data-kind="thinking" style="cursor: pointer;" data-select-core="${escapeHtml(m.id)}">
        <div class="stream-meta">
          <span class="badge" data-tone="core">내가 직접 구현·학습할 코어</span>
          <span><code>${escapeHtml(m.file_path)}</code></span>
        </div>
        <div style="font-weight: 600; margin: 4px 0;">${escapeHtml(m.title)}</div>
        <div style="font-size: 12px; color: var(--color-text-secondary);">${escapeHtml(m.claude_pattern)}</div>
      </div>
    `
    ),
    ...infraMods.map(
      (inf) => `
      <div class="stream-item" style="border-left-color: var(--color-border-strong); opacity: 0.9;">
        <div class="stream-meta">
          <span class="badge">AI 구축 인프라 (Scaffold)</span>
          <span><code>${escapeHtml(inf.file_path)}</code></span>
        </div>
        <div style="font-size: 12.5px; color: var(--color-text-secondary);">${escapeHtml(inf.role)}</div>
      </div>
    `
    ),
  ].join('');

  const ownContainer = document.getElementById('ownership-cards-container');
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
  document.getElementById('core-code-why').textContent = `[차별점 & 학습 포인트] ${target.why_different}`;
  document.getElementById('core-code-viewer').textContent = target.code;
}

/* ============================================================================
 * Tab 2: Worktrees, Shared Task Board & Rotation Logs
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
 * Tab 3: Agent Mailbox & Evaluator Diff Review
 * ============================================================================ */
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

  const diffHtml = (rev.diff || '')
    .split('\n')
    .slice(0, 220)
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

  document.getElementById('worktree-diff-viewer').innerHTML = diffHtml || 'No diff recorded.';
}

/* ============================================================================
 * Tab 4 & Tab 5: RepoOnboard Service + Archify Interactive Architecture Map
 * ============================================================================ */
function renderOnboardAndArchify() {
  const pkg = state.onboardPackage;
  if (!pkg) return;

  const brief = pkg.onboarding_brief || {};
  const analysis = pkg.analysis || {};
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

  // Modern JS Object.groupBy (with fallback)
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
              const isCore = n.file_path?.includes('abyss/core/');
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

      document.getElementById('archify-inspector-title').textContent = nodeObj.file_path;
      document.getElementById('archify-inspector-body').innerHTML = `
        <div><span class="badge" data-tone="${nodeObj.file_path.includes('abyss/core/') ? 'core' : 'info'}">${escapeHtml(nodeObj.layer)}</span></div>
        <div><strong>Purpose:</strong> ${escapeHtml(nodeObj.purpose)}</div>
        <div><strong>Exported Symbols:</strong> <code>${escapeHtml((nodeObj.symbols || []).join(', ') || 'module-level')}</code></div>
        <div><strong>Imports From (${outgoing.length}):</strong> <code>${escapeHtml(outgoing.join(', ') || 'None (Leaf Core Module)')}</code></div>
        <div><strong>Imported By (${incoming.length}):</strong> <code>${escapeHtml(incoming.join(', ') || 'Entry Point')}</code></div>
      `;
    });
  });

  const firstBtn = canvas.querySelector('.archify-node');
  if (firstBtn) firstBtn.click();
}

/* ============================================================================
 * Tab 6: Claude Code Experimental Code Mode (REPL · Ctrl+O) Lab
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
  document.getElementById('codemode-learning-point').textContent = `[학습 포인트 · ${sc.doc_section}] ${sc.learning_point}`;
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

  // 1) Metrics comparison bar
  const metricsBar = document.getElementById('codemode-metrics-bar');
  if (metricsBar && comparison) {
    metricsBar.innerHTML = `
      <div class="stream-item" style="border-left-color: var(--color-accent);">
        <div class="stream-meta"><span>LLM Round-Trips</span><span class="badge" data-tone="success">1 Step</span></div>
        <div style="font-size: 13px; font-weight: 600;">
          Direct: ${comparison.direct_mode_llm_turns}회 왕복 → Code Mode: <strong>1회</strong>
        </div>
      </div>
      <div class="stream-item" style="border-left-color: var(--color-info);">
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
    apiFetch('/api/repo-onboard/analyze', {
      method: 'POST',
      body: JSON.stringify({ repo_url_or_path: 'niisow/abyss' }),
    }),
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

  // Run Parallel Worktree Build & Merge
  const runBtn = document.getElementById('run-workflow-btn');
  const runBadge = document.getElementById('run-status-badge');
  runBtn.addEventListener('click', async () => {
    runBtn.disabled = true;
    runBadge.textContent = 'RUNNING 3 PARALLEL WORKTREES...';
    runBadge.setAttribute('data-tone', 'info');
    try {
      const goal = document.getElementById('workflow-goal-input').value;
      const engineMode = document.getElementById('engine-select').value;
      const dash = await apiFetch('/api/workflow/run', {
        method: 'POST',
        body: JSON.stringify({ goal, engine_mode: engineMode }),
      });
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

  // RepoOnboard Analysis & Preset Buttons
  async function triggerRepoAnalyze(targetValue) {
    const input = document.getElementById('onboard-repo-input');
    if (targetValue) input.value = targetValue;
    const pkg = await apiFetch('/api/repo-onboard/analyze', {
      method: 'POST',
      body: JSON.stringify({ repo_url_or_path: input.value }),
    });
    state.onboardPackage = pkg;
    renderOnboardAndArchify();
  }

  document.getElementById('analyze-repo-btn').addEventListener('click', () => triggerRepoAnalyze());
  document.querySelectorAll('.quick-repo-btn').forEach((btn) => {
    btn.addEventListener('click', () => triggerRepoAnalyze(btn.getAttribute('data-target')));
  });

  // IME-safe Enter submit on repo input (Modern Web Guidance: ime-safe-enter-submit)
  document.getElementById('onboard-repo-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) {
      e.preventDefault();
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

  await refreshAll();
});
