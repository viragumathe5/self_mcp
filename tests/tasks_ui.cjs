/* Isolated browser checks: all providers, model calls and project APIs are mocked. */
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const projectId = 'a7c81063-d071-43d2-9933-e9c845860001';
const otherId = 'b7c81063-d071-43d2-9933-e9c845860002';
const runId = 'c7c81063-d071-43d2-9933-e9c845860003';
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
  const state = { run: null, calls: [], phase: 0, autoAdvance: true, holdRead: null };
  await page.route('https://**', route => route.abort());
  await page.route('**/api/**', async route => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    const method = request.method();
    const body = request.postDataJSON();
    state.calls.push({ pathname, method, body });
    let result = {};
    if (pathname === '/api/providers') result = providerStatus(configured);
    else if (pathname.endsWith('/model-options')) {
      result = { models: [{ id: 'gpt-5', name: 'Lead' }, { id: 'gpt-5-mini', name: 'Worker' }], lead_model: 'gpt-5', worker_model: 'gpt-5-mini', reason: 'HEURISTIC: prefer known stronger lead and smaller workers.' };
    } else if (pathname.endsWith('/cancel')) {
      state.run.status = 'cancelled';
      state.run.error = 'Cancelled by user';
      result = state.run;
    } else if (pathname.endsWith('/task-runs')) {
      if (method === 'POST') {
        state.run = { ...runFixture(body.goal), ...body, id: runId };
        state.phase = 0;
        result = state.run;
        if (state.loseSubmissionReply) {
          await route.abort();
          return;
        }
      } else result = { runs: state.run && pathname.includes(projectId) ? [state.run] : [] };
    } else if (pathname.includes('/task-runs/')) {
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
          state.run.reviews.push({ approved: true, feedback: 'Draft is consistent; simulation remains unexecuted.', revision_tasks: [] });
          state.run.final_output = `Reviewed VQE draft\n${'Long output line\n'.repeat(150)}<img src=x onerror="window.injected=true">\nCode and simulations have not been executed.`;
          state.run.usage = { input_tokens: 2000, output_tokens: 900, model_calls: 7, unknown_calls: 0 };
        }
      }
      result = state.run;
    } else if (pathname === '/api/stores') result = [project(projectId), project(otherId)];
    else if (pathname === `/api/stores/${projectId}`) result = project(projectId);
    else if (pathname === `/api/stores/${otherId}`) result = project(otherId);
    else result = [];
    await route.fulfill({ status: method === 'POST' && pathname.endsWith('/task-runs') ? 202 : 200, json: result });
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
  await page.locator('#task-load-models').click();
  await page.waitForFunction(() => document.querySelector('#task-model-status').textContent.includes('HEURISTIC'));
  await page.locator('#task-lead-model').selectOption('gpt-5');
  await page.locator('#task-worker-model').selectOption('gpt-5-mini');
  await page.locator('#task-goal').fill('Design a VQE experiment; produce code and clearly state what has not been executed.');
  await page.locator('#task-max-tasks').fill('3');
  await page.locator('#task-max-revisions').fill('1');
  await page.locator('#task-cost-consent').check();
  await page.locator('#task-submit').click();
  await page.waitForSelector('.task-card');
  assert.equal(await page.locator('.task-card').count(), 2);
  assert.match(await page.locator('.task-dependencies').last().textContent(), /physics/);
  await page.locator('[data-section="instructions-physics"] summary').click();
  await page.waitForSelector('.task-final', { timeout: 25000 });
  assert.equal(await page.locator('[data-section="instructions-physics"]').getAttribute('open'), '');
  assert.match(await page.locator('.task-reviews').textContent(), /Clarify convergence assumptions/);
  assert.match(await page.locator('.task-final').textContent(), /simulations have not been executed/);
  assert.equal(await page.locator('.task-final img, .task-final script').count(), 0);
  assert.equal(await page.evaluate(() => window.injected), undefined);
  const posts = fixture.calls.filter(call => call.method === 'POST' && call.pathname.endsWith('/task-runs'));
  assert.equal(posts.length, 1);
  assert.equal(posts[0].body.worker_model, 'gpt-5-mini');
  assert.equal(posts[0].body.max_revisions, 1);
  assert.equal('api_key' in posts[0].body, false);
  const dimensions = await page.locator('.task-workspace').evaluate(node => {
    node.scrollTop = node.scrollHeight;
    return { scrolled: node.scrollTop > 0, overflow: node.scrollWidth > node.clientWidth + 1 };
  });
  assert.equal(dimensions.scrolled, true);
  assert.equal(dimensions.overflow, false);
  await page.setViewportSize({ width: 1280, height: 900 });
  const controls = await page.locator('.task-controls').boundingBox();
  const results = await page.locator('.task-results').boundingBox();
  assert.ok(results.x > controls.x + controls.width, 'Desktop should show controls alongside results');

  fixture.autoAdvance = false;
  fixture.run.status = 'running';
  fixture.run.final_output = null;
  await page.locator('#task-history-refresh').click();
  await page.getByRole('button', { name: 'Cancel run', exact: true }).waitFor();
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
  await page.waitForFunction(() => document.querySelector('#task-history-status').textContent.includes('No task runs'));
  assert.equal(await page.locator('#task-run-detail .task-card').count(), 0);
  assert.equal(await page.locator('#task-goal').inputValue(), '');
  assert.deepEqual(errors, []);
  await context.close();

  const noKey = await browser.newContext();
  const disabled = await noKey.newPage();
  await mock(disabled, false);
  await disabled.goto(`${base}/projects/${projectId}/tasks`);
  await disabled.waitForFunction(() => document.querySelector('#task-provider-status').textContent.includes('Configure a provider'));
  assert.equal(await disabled.locator('#task-submit').isDisabled(), true);
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
  await uncertain.waitForFunction(() => document.querySelector('#task-submit-status').textContent.includes('matching new run'));
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
}

async function main() {
  const staticRoot = path.resolve(__dirname, '../src/static');
  const server = http.createServer(async (request, response) => {
    const pathname = new URL(request.url, 'http://localhost').pathname;
    const filename = pathname.startsWith('/static/') ? path.basename(pathname) : 'index.html';
    try {
      const data = await fs.readFile(path.join(staticRoot, filename));
      response.writeHead(200, { 'Content-Type': { '.js': 'application/javascript', '.css': 'text/css', '.html': 'text/html' }[path.extname(filename)] || 'text/plain' });
      response.end(data);
    } catch { response.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_PATH });
    await runTests(browser, `http://127.0.0.1:${server.address().port}`);
    console.log('Tasks UI checks passed: models, submit, DAG, revision, final output, cancellation races, mobile scroll, project isolation, provider setup and existing tabs.');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
