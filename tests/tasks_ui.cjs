/* Isolated browser checks: all providers, model calls and project APIs are mocked. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const projectId = 'a7c81063-d071-43d2-9933-e9c845860001';
const otherId = 'b7c81063-d071-43d2-9933-e9c845860002';
const runId = 'c7c81063-d071-43d2-9933-e9c845860003';
const localId = 'profile_11111111111111111111111111111111';
const project = id => ({ id, slug: id === projectId ? 'personal' : 'other', name: id === projectId ? 'Project A' : 'Project B', doc_count: 0, link_count: 0, total_tokens: 0 });
const providerStatus = configured => ({
  providers: [
    { id: 'openai', name: 'OpenAI', configured, source: configured ? 'environment' : null, default_model: null },
    { id: 'anthropic', name: 'Anthropic', configured: false, source: null, default_model: null },
  ], default_provider: configured ? 'openai' : null, keyring_available: false, onboarding_dismissed: true,
});

function runFixture(goal) {
  return {
    id: runId, store_id: projectId, goal, provider: 'openai', lead_model: 'gpt-5', worker_model: 'gpt-5-mini',
    root_goal: goal, turn_id: runId, turns: [],
    status: 'queued', max_tasks: 3, max_revisions: 1, revision_count: 0,
    usage: { input_tokens: 0, output_tokens: 0, model_calls: 0, unknown_calls: 0 },
    tasks: [], reviews: [], events: [], final_output: null, error: null,
    created_at: '2026-09-11T10:00:00Z', updated_at: '2026-09-11T10:00:00Z',
  };
}

function breakdown(status) {
  return [
    { id: 'physics', title: 'Specify the VQE Hamiltonian', instructions: 'Explain assumptions.', depends_on: [], acceptance_criteria: ['Use atomic units.'], model: 'gpt-5-mini', status, attempts: 1, output: status === 'completed' ? 'Hamiltonian draft' : null, error: null },
    { id: 'code', title: 'Draft a VQE implementation', instructions: 'Draft Python code; do not run it.', depends_on: ['physics'], acceptance_criteria: ['Include reproducibility notes.'], model: 'gpt-5-mini', status: status === 'running' ? 'pending' : status, attempts: 1, output: status === 'completed' ? '<script>window.injected = true</script>' : null, error: null },
  ];
}

async function mock(page, configured = true) {
  const state = { run: null, calls: [], phase: 0, autoAdvance: true, holdRead: null, providers: providerStatus(configured), savedRuns: [], prices: {} };
  await page.route('https://**', route => route.abort());
  await page.route('**/mcp/**', async route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === '/mcp/health') { await route.fulfill({ body: 'ok' }); return; }
    const body = route.request().postDataJSON();
    await route.fulfill({ json: { jsonrpc: '2.0', id: body.id, result: body.method === 'initialize'
      ? { serverInfo: { name: 'self_mcp/Project A', version: '1' } }
      : { tools: [{ name: 'continue_task_run' }, { name: 'list_documents' }] } } });
  });
  await page.route('**/api/**', async route => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    const method = request.method();
    const body = request.postDataJSON();
    state.calls.push({ pathname, method, body });
    let result = {};
    if (pathname === '/api/providers') result = state.providers;
    else if (pathname.endsWith('/model-options')) {
      result = body.provider === 'auto' ? {
        models: [{ id: 'qwen2.5:14b', name: 'Local lead' }], lead_model: 'qwen2.5:14b', worker_model: 'qwen2.5:7b',
        lead_provider: localId, worker_provider: localId, reason: 'HEURISTIC: prefer local within the allowlist.',
        mode: 'local_first', warnings: [], worker_candidates: [
          { provider: localId, model: 'qwen2.5:7b', name: 'Local worker', is_local: true },
          { provider: 'openai', model: 'gpt-5-mini', name: 'Remote worker', is_local: false },
        ],
      } : { models: [{ id: 'gpt-5', name: 'Lead' }, { id: 'gpt-5-mini', name: 'Worker' }], lead_model: 'gpt-5', worker_model: 'gpt-5-mini', reason: 'HEURISTIC: prefer known stronger lead and smaller workers.' };
    } else if (pathname.endsWith('/pricing') && method === 'PUT') {
      state.prices[`${body.provider}/${body.model}`] = body;
      result = { saved: true };
    } else if (pathname.endsWith('/usage')) {
      if (state.failUsage) { await route.fulfill({ status: 503, json: { detail: 'Usage temporarily unavailable.' } }); return; }
      const run = state.savedRuns.find(item => pathname.includes(item.id)) || state.run;
      const provider = run.lead_provider || run.provider, model = run.lead_model;
      const rates = state.prices[`${provider}/${model}`];
      const total = { ...run.usage, estimated_cost_usd: rates ? (run.usage.input_tokens * rates.input_per_million + run.usage.output_tokens * rates.output_per_million) / 1e6 : null,
        priced_calls: rates ? run.usage.model_calls : 0, unpriced_calls: rates ? 0 : run.usage.model_calls };
      result = { total, project_total: total,
        turns: [...(run.turns || []), run].map(turn => ({ ...total, turn_id: turn.turn_id || turn.id })),
        models: [{ ...total, provider, model, rates, stages: ['plan', 'worker', 'review'] }],
        calls: [{ call_id: 1, provider, model, stage: 'review', input_tokens: total.input_tokens, output_tokens: total.output_tokens, estimated_cost_usd: total.estimated_cost_usd }] };
    } else if (pathname.endsWith('/messages')) {
      if (state.dropFollowupBeforeAccept) { state.dropFollowupBeforeAccept = false; await route.abort(); return; }
      if (state.rejectFollowupOnce) {
        state.rejectFollowupOnce = false;
        await route.fulfill({ status: 409, json: { detail: 'This task has a newer message. Refresh before sending.' } }); return;
      }
      if (state.run.turn_id !== body.message_id && !(state.run.turns || []).some(turn => turn.turn_id === body.message_id)) {
        assert.equal(body.expected_turn_id, state.run.turn_id);
        const old = JSON.parse(JSON.stringify(state.run));
        const prior = old.turns; delete old.turns;
        state.run = { ...state.run, ...body, turn_id: body.message_id, turns: [...prior, old], status: 'queued', final_output: null, error: null };
        state.phase = 0;
        state.acceptedFollowups = (state.acceptedFollowups || 0) + 1;
      }
      result = state.run;
      if (state.loseFollowupReply) { state.loseFollowupReply = false; await route.abort(); return; }
    } else if (pathname.endsWith('/cancel')) {
      state.run.status = 'cancelled';
      state.run.error = 'Cancelled by user';
      result = state.run;
    } else if (pathname.endsWith('/task-runs')) {
      if (method === 'GET' && state.failList) { await route.fulfill({ status: 503, json: { detail: 'Task storage temporarily unavailable.' } }); return; }
      if (method === 'POST') {
        state.run = { ...runFixture(body.goal), ...body, id: runId,
          lead_model_pinned: Boolean(body.lead_model), worker_model_pinned: Boolean(body.worker_model) };
        state.phase = 0;
        if (body.provider === 'auto') {
          state.run.lead_provider = localId;
          state.run.worker_provider = localId;
          state.run.lead_model = 'qwen2.5:14b';
          state.run.worker_model = 'qwen2.5:7b';
          state.run.routing_mode = 'local_first';
          state.phase = 4;
        }
        result = state.run;
        if (state.loseSubmissionReply) {
          await route.abort();
          return;
        }
      } else {
        const offset = Number(new URL(request.url()).searchParams.get('offset') || 0);
        result = { runs: state.run && pathname.includes(projectId) ? offset ? state.savedRuns : [state.run] : [],
          next_offset: !offset && state.savedRuns.length ? 1 : null };
      }
    } else if (pathname.includes('/task-runs/')) {
      const saved = state.savedRuns.find(item => pathname.endsWith(item.id));
      if (saved) { await route.fulfill({ json: saved }); return; }
      if (state.holdRead) {
        const stale = JSON.parse(JSON.stringify(state.run));
        await state.holdRead;
        await route.fulfill({ json: stale });
        return;
      }
      if (state.autoAdvance && state.run.status !== 'cancelled') {
        state.phase += 1;
        state.run.updated_at = `2026-09-11T10:00:${String(state.phase).padStart(2, '0')}Z`;
        if (state.phase === 1) state.run.status = 'planning';
        if (state.phase === 2) { state.run.status = 'running'; state.run.tasks = breakdown('running'); }
        if (state.phase === 3) { state.run.status = 'reviewing'; state.run.tasks = breakdown('completed'); }
        if (state.phase === 4) {
          state.run.status = 'running';
          state.run.revision_count = 1;
          state.run.tasks = breakdown('running');
          state.run.tasks[1].attempts = 2;
          state.run.reviews = [{ approved: false, feedback: 'Clarify convergence assumptions.', revision_tasks: ['code'] }];
        }
        if (state.phase >= 5) {
          state.run.status = 'completed'; state.run.tasks = breakdown('completed');
          state.run.tasks[0].output_version = 2;
          state.run.tasks[0].output_versions = [
            { version: 1, model: 'gpt-5-mini', output: 'Original Hamiltonian draft' },
            { version: 2, model: 'gpt-5-mini', output: 'Hamiltonian draft' },
          ];
          state.run.shared_memory = { decisions: [{ decision: 'Preserve atomic units' }], prior_work: [], omitted_artifacts: 0 };
          state.run.knowledge_retrievals = [{ call_id: 1, stage: 'plan', status: 'ready', tokens: 320, retrieval_ms: 2.4,
            results: [{ source_id: 'a'.repeat(64), title: 'Saved atomic units decision', kind: 'worker_draft', version: 2, provider: 'openai', model: 'gpt-5-mini',
              review_status: 'unreviewed', offset: 45, end_offset: 90, excerpt_truncated: true,
              excerpt: 'Use atomic units. <img src=x onerror="window.injected=true">' }] }];
          state.run.reviews.push({ approved: true, feedback: 'Draft is consistent; simulation remains unexecuted.', revision_tasks: [] });
          state.run.final_output = `Reviewed VQE draft\n${'Long output line\n'.repeat(150)}<img src=x onerror="window.injected=true">\nCode and simulations have not been executed.`;
          state.run.usage = { input_tokens: 2000, output_tokens: 900, model_calls: 7, unknown_calls: 0 };
        }
      }
      if (state.run.provider === 'auto') {
        for (const task of state.run.tasks) {
          task.provider = task.id === 'physics' ? localId : 'openai';
          task.model = task.id === 'physics' ? 'qwen2.5:7b' : 'gpt-5-mini';
        }
      }
      if (state.run.mode === 'direct' && state.autoAdvance && state.run.status !== 'cancelled') {
        state.run.status = 'completed';
        state.run.tasks = [];
        state.run.reviews = [];
        state.run.final_output = `Direct answer from the selected model. ${state.run.goal}. Code has not been executed.`;
        state.run.usage = { input_tokens: 100, output_tokens: 30, model_calls: 1, unknown_calls: 0 };
      }
      result = state.run;
    } else if (pathname === '/api/stores') result = [project(projectId), project(otherId)];
    else if (pathname === `/api/stores/${projectId}`) result = project(projectId);
    else if (pathname === `/api/stores/${otherId}`) result = project(otherId);
    else result = [];
    await route.fulfill({ status: method === 'POST' && (pathname.endsWith('/task-runs') || pathname.endsWith('/messages')) ? 202 : 200, json: result });
  });
  return state;
}

async function runTests(browser, base) {
  const context = await browser.newContext({ viewport: { width: 390, height: 700 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const fixture = await mock(page);
  await page.goto(`${base}/projects/personal/tasks`);
  await page.waitForSelector('#project-view-tasks', { state: 'visible' });
  await page.waitForFunction(() => !document.querySelector('#task-submit').disabled);
  assert.equal(await page.locator('#project-tabs .project-tab').count(), 4);
  assert.equal(await page.locator('#task-cost-consent').isChecked(), false);
  assert.equal(await page.locator('#task-advanced').getAttribute('open'), null);
  assert.equal(await page.locator('#task-empty-state').isVisible(), true);
  await page.locator('#task-advanced > summary').click();
  await page.locator('#task-load-models').click();
  await page.waitForFunction(() => document.querySelector('#task-model-status').textContent.includes('HEURISTIC'));
  await page.locator('#task-lead-model').selectOption('gpt-5');
  await page.locator('#task-worker-model').selectOption('gpt-5-mini');
  await page.locator('#task-goal').fill('Design a VQE experiment; produce code and clearly state what has not been executed.');
  await page.locator('#task-project-context').check();
  assert.equal(await page.locator('#task-schema-repair').isChecked(), true);
  await page.locator('#task-max-tasks').fill('3');
  await page.locator('#task-max-revisions').fill('1');
  await page.locator('#task-cost-consent').check();
  await page.locator('#task-submit').click();
  await page.locator('.task-progress-link').click();
  await page.waitForSelector('.task-card');
  assert.equal(await page.locator('.task-card').count(), 2);
  assert.match(await page.locator('.task-dependencies').last().textContent(), /physics/);
  await page.locator('[data-section="task-physics"] summary').click();
  await page.waitForSelector('.task-final', { timeout: 25000 });
  assert.equal(await page.locator('[data-section="task-physics"]').getAttribute('open'), '');
  assert.match(await page.locator('.task-reviews').textContent(), /Clarify convergence assumptions/);
  assert.match(await page.locator('.task-final').textContent(), /simulations have not been executed/);
  assert.match(await page.locator('[data-section=versions-physics]').textContent(), /Original Hamiltonian draft/);
  assert.match(await page.locator('[data-section=shared-memory]').textContent(), /Preserve atomic units/);
  assert.match(await page.locator('[data-section=knowledge]').textContent(), /320 context tokens/);
  assert.match(await page.locator('[data-section=knowledge]').textContent(), /Saved atomic units decision/);
  assert.match(await page.locator('[data-section=knowledge]').textContent(), /unreviewed · version 2/);
  assert.match(await page.locator('[data-section=knowledge]').textContent(), /gpt-5-mini/);
  assert.equal(await page.locator('[data-section=knowledge] img').count(), 0, 'Knowledge excerpts must stay plain text');
  assert.equal(await page.locator('[data-section=technical]').getAttribute('open'), null);
  assert.equal(await page.locator('[data-section=reviews]').getAttribute('open'), null);
  assert.equal(await page.locator('#task-run-detail .task-card').count(), 0, 'Work details should not crowd the result');
  assert.equal(await page.locator('.task-final img, .task-final script').count(), 0);
  assert.equal(await page.evaluate(() => window.injected), undefined);
  const posts = fixture.calls.filter(call => call.method === 'POST' && call.pathname.endsWith('/task-runs'));
  assert.equal(posts.length, 1);
  assert.equal(posts[0].body.worker_model, 'gpt-5-mini');
  assert.equal(posts[0].body.max_revisions, 1);
  assert.equal(posts[0].body.include_project_context, true);
  assert.equal(posts[0].body.schema_retries, 1);
  assert.equal('api_key' in posts[0].body, false);
  await page.locator('#task-details-close').click();
  await page.waitForFunction(() => document.activeElement?.id === 'task-details-open');
  if (process.env.UI_SCREENSHOT) await page.locator('#task-run-form').screenshot({ path: process.env.UI_SCREENSHOT });
  const aligned = await page.locator('.task-run-actions').evaluate(row => {
    const badge = row.querySelector('.task-status').getBoundingClientRect();
    const button = row.querySelector('.task-progress-link').getBoundingClientRect();
    return Math.abs(badge.y + badge.height / 2 - button.y - button.height / 2) < 1;
  });
  assert.equal(aligned, true, 'Status and work details share the same vertical center');
  assert.match(await page.locator('#task-usage-summary').textContent(), /2,000 input.*900 output.*cost unavailable/);
  assert.equal(await page.locator('#task-run-detail .task-run-actions').count(), 0);
  await page.locator('#task-goal').scrollIntoViewIfNeeded();
  const progress = await page.locator('.task-live-status').boundingBox();
  const input = await page.locator('#task-goal').boundingBox();
  assert.ok(progress.y >= 0 && progress.y + progress.height <= input.y,
    'Live status stays visible above the input on mobile, even after a long answer');
  assert.ok(input.y - (progress.y + progress.height) < 120,
    'Progress and input remain together');
  const dimensions = await page.locator('.task-workspace').evaluate(node => {
    node.scrollTop = node.scrollHeight;
    return { scrolled: node.scrollTop > 0, overflow: node.scrollWidth > node.clientWidth + 1 };
  });
  assert.equal(dimensions.scrolled, true);
  assert.equal(dimensions.overflow, false);
  await page.setViewportSize({ width: 1280, height: 900 });
  assert.equal(await page.locator('#task-history-panel, #task-new, .task-controls').count(), 0);
  const main = await page.locator('.task-main').boundingBox();
  const columns = await page.locator('.task-columns').boundingBox();
  assert.ok(Math.abs(main.width - columns.width) < 2, 'The task gets the full reading column');
  await page.locator('#task-details-open').click();
  await page.locator('#task-pricing > summary').click();
  await page.locator('#task-price-input').fill('10');
  await page.locator('#task-price-output').fill('20');
  await page.locator('#task-price-save').click();
  await page.waitForFunction(() => document.querySelector('#task-usage-summary').textContent.includes('$0.038000'));
  assert.match(await page.locator('[data-section=usage]').textContent(), /All saved tasks in this project/);
  await page.locator('#task-details-close').click();

  fixture.autoAdvance = false;
  fixture.savedRuns = [{ ...runFixture('Older separate task'), id: '11111111-1111-4111-8111-111111111111',
    status: 'completed', final_output: 'An older saved result', created_at: '2026-08-01T10:00:00Z' }];
  await page.locator('#task-refresh').click();
  await page.locator('#task-more-runs').click();
  await page.locator('#task-saved-run').selectOption(fixture.savedRuns[0].id);
  await page.waitForFunction(() => document.querySelector('.task-final')?.textContent.includes('An older saved result'));
  await page.reload();
  await page.waitForFunction(() => document.querySelector('.task-final')?.textContent.includes('An older saved result'));
  assert.equal(await page.locator('#task-saved-run').inputValue(), fixture.savedRuns[0].id, 'Reload preserves the selected task');
  await page.locator('#task-saved-run').selectOption(runId);
  await page.waitForFunction(() => document.querySelector('.task-final')?.textContent.includes('Reviewed VQE draft'));
  fixture.failUsage = true;
  await page.locator('#task-refresh').click();
  await page.waitForFunction(() => document.querySelector('#task-usage-summary').textContent.includes('Usage unavailable'));
  assert.equal(await page.locator('.task-final').count(), 1, 'Accounting failure does not hide the saved answer');
  fixture.failUsage = false;
  fixture.run.review_approved = false;
  fixture.run.completion_reason = 'revision_limit';
  await page.locator('#task-refresh').click();
  await page.waitForFunction(() => document.querySelector('#task-run-status').textContent.includes('Final draft saved'));
  assert.match(await page.locator('.task-final h3').textContent(), /needs review/);
  assert.match(await page.locator('.task-run-actions .task-status').textContent(), /needs review/);
  assert.equal(await page.locator('#task-submit').isDisabled(), false);
  assert.equal(await page.locator('#task-submit').textContent(), 'Send follow-up');
  delete fixture.run.review_approved;
  // Older failures have saved worker outputs but no synthesized answer or stage metadata.
  fixture.run.status = 'failed';
  fixture.run.final_output = null;
  fixture.run.error = 'Answer exceeds the application character limit. <script>window.injected = true</script>';
  await page.locator('#task-refresh').click();
  await page.waitForSelector('.task-partial');
  assert.equal(await page.locator('.task-saved-draft').count(), 2);
  assert.match(await page.locator('.task-partial').textContent(), /Hamiltonian draft/);
  assert.match(await page.locator('.task-run-actions .task-status').textContent(), /Partial results/);
  assert.equal(await page.locator('.task-final').count(), 0, 'Partial work must not look like a reviewed answer');
  await page.locator('#task-run-problem > summary').click();
  assert.match(await page.locator('#task-run-error').textContent(), /character limit/);
  assert.match(await page.locator('#task-run-recovery').textContent(), /follow-up/);
  assert.equal(await page.locator('#task-run-problem script, .task-partial script').count(), 0);
  assert.equal(await page.evaluate(() => window.injected), undefined);
  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { value: { writeText: async text => { window.copiedDrafts = text; } }, configurable: true }));
  await page.getByRole('button', { name: 'Copy all drafts', exact: true }).click();
  const drafts = await page.evaluate(() => window.copiedDrafts);
  assert.match(drafts, /incomplete and not approved/);
  assert.match(drafts, /Hamiltonian draft/);
  assert.match(drafts, /window.injected/); // Copy the full source text, never execute it.
  await page.setViewportSize({ width: 390, height: 700 });
  assert.equal(await page.locator('.task-workspace').evaluate(el => el.scrollWidth > el.clientWidth + 1), false);
  await page.setViewportSize({ width: 1280, height: 900 });
  fixture.run.error = null;
  fixture.run.status = 'running';
  fixture.run.final_output = null;
  await page.locator('#task-refresh').click();
  await page.getByRole('button', { name: 'Cancel run', exact: true }).waitFor();
  assert.equal(await page.locator('#task-run-form #task-cancel').isVisible(), true);
  await page.locator('#task-details-open').focus();
  fixture.run.tasks[0].title = 'Updated worker progress';
  await page.waitForFunction(() => document.querySelector('#task-details-content').textContent.includes('Updated worker progress'));
  assert.equal(await page.evaluate(() => document.activeElement?.id), 'task-details-open',
    'Polling preserves focus on the live controls');
  fixture.holdRead = new Promise(resolve => { fixture.release = resolve; });
  await page.waitForTimeout(2800);
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: 'Cancel run', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('#task-run-status').textContent.includes('Cancelled'));
  fixture.release(); fixture.holdRead = null;
  await page.waitForTimeout(200);
  assert.match(await page.locator('#task-run-status').textContent(), /Cancelled/);

  await page.locator('#ptab-docs').click();
  await page.waitForSelector('#project-view-docs', { state: 'visible' });
  await page.locator('#ptab-links').click();
  await page.waitForSelector('#project-view-links', { state: 'visible' });
  await page.locator('#ptab-issues').click();
  await page.waitForSelector('#project-view-issues', { state: 'visible' });
  await page.locator('#ptab-tasks').click();
  await page.waitForFunction(() => document.querySelector('#task-run-status').textContent.includes('Cancelled'));
  assert.equal(await page.locator('#task-worker-model').inputValue(), 'gpt-5-mini');
  await page.goto(`${base}/projects/${otherId}/tasks`);
  await page.waitForFunction(() => document.querySelector('#task-run-status').textContent.includes('first message'));
  assert.equal(await page.locator('#task-run-detail .task-card').count(), 0);
  assert.equal(await page.locator('#task-goal').inputValue(), '');
  await page.locator('#nav-setup').click();
  await page.waitForSelector('.project-cfg-row');
  const config = JSON.parse(await page.locator('#cfg-output').textContent());
  assert.equal(config.mcp.self_mcp_personal.url, `${base}/mcp/personal/mcp`);
  assert.equal(config.mcp.self_mcp_personal.enabled, true);
  assert.equal(config.mcp.self_mcp_personal.oauth, false);
  await page.locator('.project-cfg-row').first().getByRole('button', { name: 'Test connection' }).click();
  await page.waitForFunction(() => document.querySelector('.mcp-connection-status').textContent.includes('Connected'));
  assert.match(await page.locator('#mcp-server-status').textContent(), /ready/);
  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { value: { writeText: async text => { window.copiedConfig = text; } }, configurable: true }));
  await page.locator('.project-cfg-row').first().getByRole('button', { name: 'Config', exact: true }).click();
  const copied = JSON.parse(await page.evaluate(() => window.copiedConfig));
  assert.equal(copied.mcp.self_mcp_personal.url, `${base}/mcp/personal/mcp`);
  assert.deepEqual(errors, []);
  await context.close();

  const noKey = await browser.newContext();
  const disabled = await noKey.newPage();
  await mock(disabled, false);
  await disabled.goto(`${base}/projects/${projectId}/tasks`);
  await disabled.waitForFunction(() => document.querySelector('#task-provider-status').textContent.includes('Configure a provider'));
  assert.equal(await disabled.locator('#task-submit').isDisabled(), true);
  await disabled.locator('#task-advanced > summary').click();
  await disabled.locator('#task-provider-settings').click();
  await disabled.waitForSelector('#view-providers', { state: 'visible' });
  await noKey.close();

  const uncertainContext = await browser.newContext();
  const uncertain = await uncertainContext.newPage();
  const uncertainFixture = await mock(uncertain);
  uncertainFixture.loseSubmissionReply = true;
  uncertainFixture.autoAdvance = false;
  await uncertain.goto(`${base}/projects/${projectId}/tasks`);
  await uncertain.waitForFunction(() => !document.querySelector('#task-submit').disabled);
  await uncertain.locator('#task-goal').fill('An unconfirmed but accepted task');
  await uncertain.locator('#task-cost-consent').check();
  await uncertain.locator('#task-submit').click();
  await uncertain.waitForSelector('#task-uncertain-actions', { state: 'visible' });
  assert.equal(await uncertain.locator('#task-submit').isDisabled(), true);
  await uncertain.locator('#task-reconcile').click();
  await uncertain.waitForFunction(() => document.querySelector('#task-submit-status').textContent.includes('Message confirmed'));
  assert.equal(uncertainFixture.calls.filter(call => call.method === 'POST' && call.pathname.endsWith('/task-runs')).length, 1);
  assert.equal(await uncertain.locator('#task-cost-consent').isChecked(), false);
  await uncertainContext.close();

  const raceContext = await browser.newContext();
  const race = await raceContext.newPage();
  await mock(race);
  let releaseProject;
  const delayedProject = new Promise(resolve => { releaseProject = resolve; });
  await race.route(`**/api/stores/${projectId}`, async route => {
    await delayedProject;
    await route.fulfill({ json: project(projectId) });
  });
  await race.goto(base);
  await race.waitForSelector('.project-card');
  await race.evaluate(id => navigate(`/projects/${id}/tasks`), projectId);
  await race.waitForTimeout(50);
  await race.evaluate(id => navigate(`/projects/${id}/tasks`), otherId);
  await race.waitForFunction(() => document.querySelector('#project-breadcrumb-name').textContent === 'Project B');
  releaseProject();
  await race.waitForTimeout(200);
  assert.equal(await race.locator('#project-breadcrumb-name').textContent(), 'Project B');
  await race.locator('#ptab-docs').click();
  assert.match(race.url(), new RegExp(`/projects/${otherId}$`));
  await raceContext.close();

  const autoContext = await browser.newContext({ viewport: { width: 390, height: 700 } });
  const automatic = await autoContext.newPage();
  const automaticState = await mock(automatic);
  automaticState.providers.providers.push({
    id: localId, name: 'Local Ollama', kind: 'ollama', configured: true, source: 'none', is_local: true,
    default_model: 'qwen2.5:14b', base_url: 'http://localhost:11434', requires_key: false,
  });
  automaticState.providers.routing = { mode: 'local_first', providers: [localId, 'openai'] };
  await automatic.goto(`${base}/projects/${projectId}/tasks`);
  await automatic.waitForFunction(() => document.querySelector('#task-provider').value === 'auto');
  assert.equal(await automatic.locator('#task-lead-model').isDisabled(), true);
  assert.equal(await automatic.locator('#task-worker-model').isDisabled(), true);
  await automatic.locator('#task-advanced > summary').click();
  await automatic.locator('#task-load-models').click();
  await automatic.waitForFunction(() => document.querySelector('#task-model-status').textContent.includes('Offered worker models'));
  assert.match(await automatic.locator('#task-model-status').textContent(), /Local Ollama.*qwen2.5:7b.*OpenAI.*gpt-5-mini/);
  await automatic.locator('#task-goal').fill('Let local and remote workers draft independent tasks.');
  await automatic.locator('#task-cost-consent').check();
  await automatic.locator('#task-submit').click();
  await automatic.waitForSelector('.task-final');
  assert.match(await automatic.locator('.task-card').first().textContent(), /Provider: Local Ollama.*qwen2.5:7b/);
  assert.match(await automatic.locator('.task-card').last().textContent(), /Provider: OpenAI.*gpt-5-mini/);
  const submitted = automaticState.calls.find(call => call.method === 'POST' && call.pathname.endsWith('/task-runs'));
  assert.equal(submitted.body.provider, 'auto');
  assert.equal(submitted.body.lead_model, null);
  assert.equal(submitted.body.worker_model, null);
  // Local-only preferences must not silently fall back to a configured cloud provider.
  automaticState.providers.routing = { mode: 'local_only', providers: ['openai'] };
  await automatic.goto(`${base}/projects/${otherId}/tasks`);
  await automatic.waitForFunction(() => document.querySelector('#task-provider-status').textContent.includes('No configured providers match'));
  assert.equal(await automatic.locator('#task-submit').isDisabled(), true);
  await autoContext.close();

  const directContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const direct = await directContext.newPage();
  const directState = await mock(direct);
  await direct.goto(`${base}/projects/${projectId}/tasks`);
  await direct.waitForFunction(() => !document.querySelector('#task-submit').disabled);
  await direct.locator('#task-mode').selectOption('direct');
  assert.equal(await direct.locator('#task-delegate-settings').isVisible(), false);
  await direct.locator('#task-direct-load').click();
  await direct.waitForFunction(() => document.querySelector('#task-direct-model').options.length > 1);
  await direct.locator('#task-direct-model').selectOption('gpt-5');
  await direct.locator('#task-goal').fill('Explain this directly.');
  await direct.locator('#task-advanced > summary').click();
  await direct.locator('#task-timeout').fill('300');
  await direct.locator('#task-output-budget').fill('8192');
  await direct.locator('#task-cost-consent').check();
  await direct.getByRole('button', { name: 'Ask model', exact: true }).click();
  await direct.waitForSelector('.task-final');
  assert.match(await direct.locator('.task-final').textContent(), /Direct answer/);
  assert.doesNotMatch(await direct.locator('.task-final').textContent(), /Reviewed/);
  assert.equal(await direct.locator('.task-card').count(), 0);
  const directPost = directState.calls.find(call => call.method === 'POST' && call.pathname.endsWith('/task-runs'));
  assert.equal(directPost.body.mode, 'direct');
  assert.equal(directPost.body.lead_model, 'gpt-5');
  assert.equal(directPost.body.worker_model, null);
  assert.equal(directPost.body.schema_retries, 0);
  assert.equal(directPost.body.timeout_seconds, 300);
  assert.equal(directPost.body.output_tokens, 8192);
  await direct.locator('.task-progress-link').click();
  assert.equal(await direct.locator('[data-section=reviews]').count(), 0);
  assert.match(await direct.locator('[data-section=technical]').textContent(), /Model calls: 1/);
  await direct.keyboard.press('Escape');
  assert.equal(await direct.locator('#task-details-panel').isVisible(), false);
  assert.equal(await direct.locator('#task-goal').inputValue(), '', 'Accepted messages clear the composer');
  assert.equal(await direct.locator('#task-submit').textContent(), 'Send follow-up');
  await direct.locator('#task-goal').fill('Now add pagination.');
  await direct.locator('#task-cost-consent').check();
  await direct.locator('#task-submit').click();
  await direct.waitForFunction(() => document.querySelector('.task-final')?.textContent.includes('pagination'));
  assert.equal(directState.calls.filter(call => call.method === 'POST' && call.pathname.endsWith('/task-runs')).length, 1);
  assert.equal(directState.acceptedFollowups, 1);
  assert.equal(directState.run.id, runId);
  assert.equal(directState.run.turns.length, 1);
  assert.equal(await direct.locator('#task-history-list').count(), 0);
  assert.equal(await direct.locator('#ptab-tasks-badge').textContent(), '1');
  assert.equal(await direct.locator('.task-exchange').count(), 1);
  assert.equal(await direct.locator('#task-run-detail .task-exchange').count(), 1);
  await direct.locator('[data-section=conversation] > summary').click();
  await direct.locator('.task-exchange > summary').click();
  assert.match(await direct.locator('.task-exchange').textContent(), /Explain this directly/);
  await direct.locator('[data-section=conversation] > summary').click();

  // A lost HTTP reply retries the same message ID, without another model turn.
  directState.loseFollowupReply = true;
  await direct.locator('#task-goal').fill('Keep pagination and add filtering.');
  await direct.locator('#task-cost-consent').check();
  await direct.locator('#task-submit').click();
  await direct.waitForSelector('#task-uncertain-actions', { state: 'visible' });
  assert.match(await direct.locator('#task-allow-retry').textContent(), /same message safely/);
  await direct.locator('#task-allow-retry').click();
  await direct.waitForFunction(() => document.querySelector('.task-final')?.textContent.includes('filtering'));
  assert.equal(directState.acceptedFollowups, 2);
  const followups = directState.calls.filter(call => call.method === 'POST' && call.pathname.endsWith('/messages'));
  assert.equal(followups.length, 3);
  assert.equal(followups[1].body.message_id, followups[2].body.message_id);
  assert.equal(await direct.locator('#task-history-list').count(), 0);
  assert.equal(await direct.locator('#ptab-tasks-badge').textContent(), '1');
  await direct.reload();
  await direct.waitForSelector('.task-final');
  assert.equal(await direct.locator('.task-exchange').count(), 2);
  assert.equal(await direct.locator('#task-mode').inputValue(), 'direct');
  assert.equal(await direct.locator('#task-direct-model').inputValue(), 'gpt-5');
  assert.equal(await direct.locator('#task-submit').textContent(), 'Send follow-up');
  directState.dropFollowupBeforeAccept = true;
  await direct.locator('#task-goal').fill('A change from a stale tab');
  await direct.locator('#task-cost-consent').check();
  await direct.locator('#task-submit').click();
  await direct.waitForSelector('#task-uncertain-actions', { state: 'visible' });
  directState.rejectFollowupOnce = true;
  await direct.locator('#task-allow-retry').click();
  await direct.waitForFunction(() => !document.querySelector('#task-submit').disabled);
  assert.equal(await direct.locator('#task-uncertain-actions').isVisible(), false);
  assert.equal(await direct.locator('#task-goal').inputValue(), 'A change from a stale tab');
  assert.equal(await direct.locator('#task-submit').isDisabled(), false);
  // Drafts and their provider/model settings survive refresh without restoring consent.
  await direct.locator('#task-cost-consent').check();
  await direct.reload();
  await direct.waitForSelector('.task-final');
  assert.equal(await direct.locator('#task-goal').inputValue(), 'A change from a stale tab');
  assert.equal(await direct.locator('#task-cost-consent').isChecked(), false);
  assert.equal(await direct.locator('#task-direct-model').inputValue(), 'gpt-5');
  await direct.locator('#task-direct-load').click();
  await direct.waitForFunction(() => !document.querySelector('#task-direct-load').disabled);
  assert.equal(await direct.locator('#task-direct-model').inputValue(), 'gpt-5', 'Model discovery must preserve an explicit model choice');

  // Keep the last answer on screen while a new message works or fails.
  directState.autoAdvance = false;
  await direct.locator('#task-goal').fill('Continue the implementation');
  await direct.locator('#task-cost-consent').check();
  await direct.locator('#task-goal').press('Control+Enter');
  await direct.waitForFunction(() => document.querySelector('.task-final h3')?.textContent === 'Last saved answer');
  assert.match(await direct.locator('.task-final').textContent(), /filtering/);
  assert.equal(await direct.locator('#task-submit').isDisabled(), true);
  directState.run.status = 'failed';
  directState.run.error = 'The model is temporarily unavailable.';
  await direct.locator('#task-refresh').click();
  await direct.waitForFunction(() => document.querySelector('#task-run-status').textContent.includes('Stopped before a final answer'));
  assert.equal(await direct.locator('.task-partial').count(), 0, 'No draft is invented when the current model returned nothing');
  assert.match(await direct.locator('.task-final').textContent(), /filtering/);
  assert.equal(await direct.locator('#task-submit').isDisabled(), false);

  // Recover an unaccepted message across reload, reusing the exact original ID.
  directState.dropFollowupBeforeAccept = true;
  await direct.locator('#task-goal').fill('Retry this after reconnecting');
  await direct.locator('#task-cost-consent').check();
  await direct.locator('#task-submit').click();
  await direct.waitForSelector('#task-uncertain-actions', { state: 'visible' });
  const pendingId = directState.calls.filter(call => call.pathname.endsWith('/messages')).at(-1).body.message_id;
  await direct.reload();
  await direct.waitForSelector('#task-uncertain-actions', { state: 'visible' });
  assert.equal(await direct.locator('#task-submit').isDisabled(), true);
  directState.autoAdvance = true;
  await direct.locator('#task-allow-retry').click();
  await direct.waitForFunction(() => document.querySelector('.task-final')?.textContent.includes('Retry this after reconnecting'));
  assert.equal(directState.calls.filter(call => call.pathname.endsWith('/messages')).at(-1).body.message_id, pendingId);
  assert.equal(directState.calls.filter(call => call.method === 'POST' && call.pathname.endsWith('/task-runs')).length, 1);
  await direct.setViewportSize({ width: 390, height: 700 });
  assert.equal(await direct.locator('.task-workspace').evaluate(el => el.scrollWidth > el.clientWidth + 1), false);
  await directContext.close();

  // A failed initial read cannot offer to start a duplicate task.
  const disconnectedContext = await browser.newContext();
  const disconnected = await disconnectedContext.newPage();
  const disconnectedState = await mock(disconnected);
  disconnectedState.failList = true;
  disconnectedState.run = { ...runFixture('An existing task'), status: 'completed', final_output: 'Saved work', mode: 'direct' };
  disconnectedState.autoAdvance = false;
  await disconnected.goto(`${base}/projects/${projectId}/tasks`);
  await disconnected.waitForFunction(() => document.querySelector('#task-run-status').textContent.includes('Refresh status'));
  assert.equal(await disconnected.locator('#task-submit').isDisabled(), true);
  disconnectedState.failList = false;
  await disconnected.locator('#task-refresh').click();
  await disconnected.waitForSelector('.task-final');
  assert.equal(await disconnected.locator('#task-submit').textContent(), 'Send follow-up');
  assert.equal(disconnectedState.calls.filter(call => call.method === 'POST').length, 0);
  await disconnectedContext.close();
}

async function main() {
  const staticRoot = path.resolve(__dirname, '../src/static');
  const base = 'http://127.0.0.1:8000'; // Fulfilled in Playwright; no listening socket.
  let browser;
  try {
    browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_PATH });
    const newContext = browser.newContext.bind(browser);
    browser.newContext = async options => {
      const context = await newContext(options);
      await context.route(`${base}/**`, async route => {
        const pathname = new URL(route.request().url()).pathname;
        const filename = pathname.startsWith('/static/') ? path.basename(pathname) : 'index.html';
        try {
          const data = await fs.readFile(path.join(staticRoot, filename));
          await route.fulfill({ body: data, contentType: { '.js': 'application/javascript', '.css': 'text/css', '.html': 'text/html' }[path.extname(filename)] || 'text/plain' });
        } catch { await route.fulfill({ status: 404 }); }
      });
      return context;
    };
    await runTests(browser, base);
    console.log('Workspace UI checks passed: same-task follow-ups, saved conversation, idempotent retry, draft recovery, single-task layout, connection recovery, preserved answers, model settings, cancellation races, mobile layout, and project isolation.');
  } finally {
    if (browser) await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
