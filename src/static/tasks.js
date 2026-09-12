/* Project-scoped workflow UI. Polling never starts or retries model work. */
window.TasksUI = (() => {
  const active = new Set(['queued', 'discovering', 'planning', 'running', 'reviewing']);
  const labels = {
    queued: 'Queued', discovering: 'Discovering models', planning: 'Lead planning',
    pending: 'Waiting for dependencies', running: 'Working', reviewing: 'Lead reviewing',
    completed: 'Completed', failed: 'Failed', cancelled: 'Cancelled', interrupted: 'Interrupted',
  };
  const state = {
    project: null, generation: 0, selection: 0, controller: null, timer: null,
    runId: null, runs: [], providers: [], modelLoading: false, initialized: false,
    rendered: '', pollVersion: 0, drafts: new Map(), submissions: new Set(), uncertain: new Map(),
  };
  const el = id => document.getElementById(id);
  const endpoint = project => `/api/stores/${encodeURIComponent(project)}/task-runs`;

  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  }

  function message(id, text, error = false) {
    el(id).textContent = text;
    el(id).classList.toggle('task-error', error);
  }

  async function request(url, { method = 'GET', body, signal } = {}) {
    let response;
    try {
      response = await fetch(url, {
        method, headers: { 'Content-Type': 'application/json' }, cache: 'no-store',
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(45000)]) : AbortSignal.timeout(45000),
      });
    } catch {
      throw Object.assign(new Error('Request could not be confirmed. Check the server and refresh runs before submitting again.'), { uncertain: true });
    }
    let result;
    try { result = await response.json(); } catch {
      throw Object.assign(new Error('The server returned an invalid task response. Refresh runs before retrying a submission.'), { uncertain: true });
    }
    if (!response.ok) throw Object.assign(new Error(typeof result.detail === 'string' ? result.detail : 'Task request failed.'), { uncertain: response.status >= 500 });
    return result;
  }

  function current(generation) {
    return state.project !== null && generation === state.generation;
  }

  function resetModels() {
    for (const [id, label] of [['task-lead-model', 'Automatic lead selection'], ['task-worker-model', 'Automatic smaller-model selection']]) {
      el(id).replaceChildren(new Option(label, ''));
    }
    message('task-model-status', 'Automatic selection runs at task start. Load accessible models to inspect or override recommendations.');
  }

  function updateControls() {
    const configured = state.providers.some(provider => provider.id === el('task-provider').value && provider.configured);
    const submitting = state.submissions.has(state.project);
    el('task-submit').disabled = !configured || submitting || state.modelLoading || state.uncertain.has(state.project);
    el('task-uncertain-actions').hidden = !state.uncertain.has(state.project);
    el('task-submit').textContent = submitting ? 'Submitting…' : 'Start task';
    el('task-run-form').setAttribute('aria-busy', String(submitting));
    el('task-load-models').disabled = !configured || submitting || state.modelLoading;
    for (const id of ['task-provider', 'task-lead-model', 'task-worker-model']) {
      el(id).disabled = submitting || state.modelLoading;
    }
  }

  function captureDraft() {
    if (!state.project) return;
    state.drafts.set(state.project, {
      goal: el('task-goal').value, provider: el('task-provider').value,
      maxTasks: el('task-max-tasks').value, maxRevisions: el('task-max-revisions').value,
      leadModel: el('task-lead-model').value, workerModel: el('task-worker-model').value,
      runId: state.runId,
    });
  }

  function hide() {
    captureDraft();
    state.generation += 1;
    state.selection += 1;
    state.controller?.abort();
    clearTimeout(state.timer);
    state.project = null;
    state.modelLoading = false;
  }

  async function show(project) {
    if (!state.initialized) init();
    hide();
    state.project = project.id;
    state.controller = new AbortController();
    state.runs = [];
    state.providers = [];
    state.rendered = '';
    const draft = state.drafts.get(project.id);
    state.runId = draft?.runId || null;
    el('task-run-form').reset();
    el('task-goal').value = draft?.goal || '';
    el('task-max-tasks').value = draft?.maxTasks || '6';
    el('task-max-revisions').value = draft?.maxRevisions || '2';
    el('task-provider').replaceChildren(new Option('Loading providers…', ''));
    el('task-run-detail').replaceChildren();
    el('task-history-list').replaceChildren();
    resetModels();
    for (const [id, model] of [['task-lead-model', draft?.leadModel], ['task-worker-model', draft?.workerModel]]) {
      if (model) {
        el(id).append(new Option(`${model} (rechecked at task start)`, model));
        el(id).value = model;
      }
    }
    updateControls();
    message('task-submit-status', state.uncertain.has(project.id)
      ? 'A previous submission was not confirmed. Check recent runs before authorizing another billable run.' : '', state.uncertain.has(project.id));
    message('task-run-status', 'Submit a goal or select a previous run to see the breakdown.');
    await refresh(draft?.provider);
  }

  async function loadProviders(preferred) {
    const generation = state.generation;
    try {
      const status = await request('/api/providers', { signal: state.controller.signal });
      if (!current(generation)) return;
      const previous = preferred || el('task-provider').value;
      state.providers = status.providers;
      el('task-provider').replaceChildren(...status.providers.map(provider => {
        const option = new Option(`${provider.name}${provider.configured ? '' : ' — not configured'}`, provider.id);
        option.disabled = !provider.configured;
        return option;
      }));
      const configured = status.providers.filter(provider => provider.configured);
      el('task-provider').value = configured.find(provider => provider.id === previous)?.id
        || configured.find(provider => provider.id === status.default_provider)?.id || configured[0]?.id || '';
      if (previous !== el('task-provider').value) resetModels();
      message('task-provider-status', configured.length
        ? 'Using your saved provider access. No key is sent to the browser.'
        : 'Configure a provider in Settings before starting a task.', !configured.length);
    } catch (error) {
      if (current(generation)) message('task-provider-status', error.message, true);
    } finally {
      if (current(generation)) updateControls();
    }
  }

  async function loadModels() {
    if (!state.project || state.modelLoading) return;
    const generation = state.generation;
    const provider = el('task-provider').value;
    if (!provider) return;
    state.modelLoading = true;
    resetModels();
    updateControls();
    message('task-model-status', 'Discovering accessible text-generation models…');
    try {
      const options = await request(`${endpoint(state.project)}/model-options`, {
        method: 'POST', body: { provider }, signal: state.controller.signal,
      });
      if (!current(generation) || el('task-provider').value !== provider) return;
      for (const id of ['task-lead-model', 'task-worker-model']) {
        for (const model of options.models) el(id).append(new Option(`${model.name} (${model.id})`, model.id));
      }
      message('task-model-status', `${options.reason} Recommended lead: ${options.lead_model || 'choose explicitly'}; workers: ${options.worker_model || 'choose explicitly'}. ${options.models.length} compatible identifiers listed. Inference access can still fail.`);
    } catch (error) {
      if (current(generation)) message('task-model-status', error.message, true);
    } finally {
      if (current(generation)) {
        state.modelLoading = false;
        updateControls();
      }
    }
  }

  function renderHistory() {
    const buttons = state.runs.map(run => {
      const button = node('button', undefined, 'task-history-item');
      button.type = 'button';
      button.setAttribute('aria-pressed', String(run.id === state.runId));
      button.append(node('strong', run.goal.slice(0, 120)), node('span', `${labels[run.status] || run.status} · ${new Date(run.created_at).toLocaleString()}`));
      button.addEventListener('click', () => selectRun(run.id, true));
      return button;
    });
    el('task-history-list').replaceChildren(...buttons);
    el('ptab-tasks-badge').textContent = state.runs.length === 50 ? '50+' : String(state.runs.length);
  }

  async function loadHistory() {
    if (!state.project) return;
    const generation = state.generation;
    message('task-history-status', 'Loading recent runs…');
    try {
      const result = await request(endpoint(state.project), { signal: state.controller.signal });
      if (!current(generation)) return;
      state.runs = result.runs;
      const uncertain = state.uncertain.get(state.project);
      if (uncertain) {
        const match = result.runs.find(run => !uncertain.knownIds.has(run.id) && run.goal === uncertain.goal && run.provider === uncertain.provider);
        if (match) {
          state.uncertain.delete(state.project);
          state.runId = match.id;
          state.selection += 1;
          state.rendered = '';
          el('task-cost-consent').checked = false;
          message('task-submit-status', 'A matching new run was found. Its progress is shown; no task was resubmitted.');
        } else {
          message('task-submit-status', 'Submission is still unconfirmed. Refresh again, or explicitly authorize another submission knowing the original may still run.', true);
        }
        updateControls();
      }
      renderHistory();
      message('task-history-status', result.runs.length ? 'Run history loaded.' : 'No task runs yet.');
      if (!state.runId && state.runs.length) await selectRun(state.runs[0].id);
      else if (state.runId) await pollRun();
    } catch (error) {
      if (current(generation)) message('task-history-status', error.message, true);
    }
  }

  async function refresh(preferred) {
    if (!state.project) return;
    await Promise.all([loadProviders(preferred), loadHistory()]);
  }

  async function selectRun(runId, focus = false) {
    clearTimeout(state.timer);
    state.runId = runId;
    state.selection += 1;
    state.rendered = '';
    el('task-run-detail').replaceChildren();
    message('task-run-status', 'Loading run…');
    renderHistory();
    await pollRun();
    if (focus && state.project && state.runId === runId) {
      el('task-run-status').scrollIntoView({ block: 'start', behavior: 'auto' });
    }
  }

  async function pollRun() {
    clearTimeout(state.timer);
    if (!state.project || !state.runId) return;
    const generation = state.generation;
    const selection = state.selection;
    const version = ++state.pollVersion;
    let again = true;
    try {
      const run = await request(`${endpoint(state.project)}/${state.runId}`, { signal: state.controller.signal });
      if (!current(generation) || selection !== state.selection || version !== state.pollVersion) return;
      const index = state.runs.findIndex(item => item.id === run.id);
      if (index >= 0) state.runs[index] = run;
      renderRun(run);
      again = active.has(run.status);
    } catch (error) {
      if (current(generation) && selection === state.selection && version === state.pollVersion) message('task-run-status', `${error.message} Retrying status only; execution is not restarted.`, true);
    } finally {
      if (again && current(generation) && selection === state.selection && version === state.pollVersion) state.timer = setTimeout(pollRun, 2500);
    }
  }

  function statusBadge(status) {
    const known = labels[status] ? status : 'unknown';
    return node('span', labels[status] || status, `task-status task-status-${known}`);
  }

  function disclosure(key, title, children, expanded = false) {
    const details = node('details', undefined, 'task-disclosure');
    details.dataset.section = key;
    details.open = expanded;
    details.append(node('summary', title), ...children);
    return details;
  }

  function output(text) {
    // Outputs are untrusted. Text preserves code and cannot execute HTML or scripts.
    return node('pre', text || 'No output yet.', 'task-output');
  }

  function taskCard(task) {
    const card = node('article', undefined, 'task-card');
    const header = node('div', undefined, 'task-section-heading');
    header.append(node('h4', `${task.id}: ${task.title}`), statusBadge(task.status));
    const criteria = node('ul');
    for (const criterion of task.acceptance_criteria) criteria.append(node('li', criterion));
    card.append(header,
      node('p', `Model: ${task.model} · Attempts: ${task.attempts}`, 'task-help'),
      node('p', task.depends_on.length ? `Depends on: ${task.depends_on.join(', ')}` : 'Independent task — can run in parallel.', 'task-dependencies'),
      disclosure(`instructions-${task.id}`, 'Instructions and acceptance criteria', [node('p', task.instructions), criteria]),
      disclosure(`output-${task.id}`, task.status === 'completed' ? 'Worker output' : 'Output / previous draft', [output(task.output)]));
    if (task.error) card.append(node('p', task.error, 'task-error'));
    return card;
  }

  function renderRun(run) {
    const signature = JSON.stringify(run);
    message('task-run-status', `${run.status === 'completed' ? 'Reviewed draft ready' : labels[run.status] || run.status} · ${run.tasks.filter(task => task.status === 'completed').length}/${run.tasks.length} subtasks drafted · ${run.revision_count}/${run.max_revisions} revisions`);
    if (signature === state.rendered) return;
    state.rendered = signature;
    const opened = new Map([...el('task-run-detail').querySelectorAll('details')].map(item => [item.dataset.section, item.open]));
    const header = node('section', undefined, 'setup-card task-run-header');
    const heading = node('div', undefined, 'task-section-heading');
    heading.append(node('h3', 'Task run'), statusBadge(run.status));
    const usage = run.usage;
    header.append(heading, node('p', run.goal, 'task-goal-display'),
      node('p', `Provider: ${run.provider} · Lead: ${run.lead_model || 'Selecting…'} · Workers: ${run.worker_model || 'Selecting…'}`, 'task-help'),
      node('p', `Model calls: ${usage.model_calls} · Reported input tokens: ${usage.input_tokens} · Reported output tokens: ${usage.output_tokens}`, 'task-help'),
      node('p', `Run ID: ${run.id}`, 'task-help'));
    if (usage.unknown_calls) header.append(node('p', `${usage.unknown_calls} call(s) have incomplete usage data. Provider billing may include cancelled or failed calls.`, 'task-notice'));
    if (active.has(run.status)) {
      const cancel = node('button', 'Cancel run', 'btn btn-danger btn-sm');
      cancel.type = 'button';
      cancel.addEventListener('click', () => cancelRun(run.id, cancel));
      header.append(cancel);
    }
    if (run.error) header.append(node('p', run.error, 'task-error'));
    const steps = node('section', undefined, 'task-breakdown');
    steps.append(node('h3', 'Work breakdown'));
    if (!run.tasks.length) steps.append(node('p', 'The lead model has not produced a validated task breakdown yet.', 'task-help'));
    for (const task of run.tasks) steps.append(taskCard(task));
    const reviews = node('section', undefined, 'task-reviews');
    reviews.append(node('h3', 'Lead reviews'));
    if (!run.reviews.length) reviews.append(node('p', 'Review begins after the workers finish their drafts.', 'task-help'));
    for (const [index, review] of run.reviews.entries()) {
      reviews.append(disclosure(`review-${index}`, `Review ${index + 1}: ${review.approved ? 'Approved draft' : 'Revision requested'}`, [
        node('p', review.feedback), node('p', review.revision_tasks.length ? `Revise: ${review.revision_tasks.join(', ')}. Dependent tasks are regenerated too.` : 'No further revisions requested.'),
      ], true));
    }
    const events = node('ol', undefined, 'task-events');
    for (const event of run.events) events.append(node('li', `${new Date(event.time).toLocaleTimeString()} — ${event.message}`));
    const blocks = [header, steps, reviews, disclosure('events', 'Execution timeline', [events])];
    if (run.final_output) {
      const final = node('section', undefined, 'setup-card task-final');
      const copy = node('button', 'Copy result', 'btn btn-secondary btn-sm');
      copy.type = 'button';
      copy.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(run.final_output); copy.textContent = 'Copied'; }
        catch { copy.textContent = 'Clipboard unavailable — select and copy the text'; }
      });
      final.append(node('h3', 'Final reviewed draft'), node('p', 'Model-based review only. Code and simulations were not executed.', 'task-notice'), copy, output(run.final_output));
      blocks.splice(1, 0, final);
    }
    el('task-run-detail').replaceChildren(...blocks);
    for (const details of el('task-run-detail').querySelectorAll('details')) {
      if (opened.has(details.dataset.section)) details.open = opened.get(details.dataset.section);
    }
    renderHistory();
  }

  async function submit(event) {
    event.preventDefault();
    const project = state.project;
    if (!project || state.submissions.has(project) || state.modelLoading || state.uncertain.has(project)) return;
    if (!el('task-run-form').reportValidity()) return;
    const body = {
      goal: el('task-goal').value.trim(), provider: el('task-provider').value,
      lead_model: el('task-lead-model').value || null, worker_model: el('task-worker-model').value || null,
      max_tasks: Number(el('task-max-tasks').value), max_revisions: Number(el('task-max-revisions').value),
    };
    if (!body.goal || new TextEncoder().encode(JSON.stringify(body)).length > 16384) {
      message('task-submit-status', 'Enter a goal within the 16 KiB request limit. Shorten the goal if it contains many non-ASCII characters.', true);
      return;
    }
    state.submissions.add(project);
    const knownIds = new Set(state.runs.map(run => run.id));
    updateControls();
    message('task-submit-status', 'Submitting once. The run will continue if you leave this tab.');
    try {
      // Do not abort submissions on navigation: an accepted request starts paid work.
      const run = await request(endpoint(project), { method: 'POST', body });
      if (!run || typeof run.id !== 'string' || run.store_id !== project || !Array.isArray(run.tasks)) {
        throw Object.assign(new Error('Submission response was incomplete. Check recent runs before starting another task.'), { uncertain: true });
      }
      if (state.project !== project) return;
      state.runs = [run, ...state.runs.filter(item => item.id !== run.id)].slice(0, 50);
      el('task-cost-consent').checked = false;
      message('task-submit-status', 'Task accepted. Follow the breakdown and reviews on this page.');
      await selectRun(run.id, true);
    } catch (error) {
      if (error.uncertain) state.uncertain.set(project, { goal: body.goal, provider: body.provider, knownIds });
      if (state.project === project) message('task-submit-status', error.message, true);
    } finally {
      state.submissions.delete(project);
      if (state.project === project) updateControls();
    }
  }

  async function cancelRun(runId, button) {
    const project = state.project;
    if (!project || !confirm('Cancel this run? Already-issued model calls may still incur charges.')) return;
    button.disabled = true;
    const generation = state.generation;
    try {
      const run = await request(`${endpoint(project)}/${runId}/cancel`, { method: 'POST', body: {} });
      if (current(generation) && state.runId === runId) {
        clearTimeout(state.timer);
        state.selection += 1;
        renderRun(run);
      }
    } catch (error) {
      if (current(generation)) message('task-run-status', error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  function focusGoal() {
    el('task-goal').scrollIntoView({ block: 'center', behavior: 'auto' });
    el('task-goal').focus();
  }

  function init() {
    state.initialized = true;
    el('task-run-form').addEventListener('submit', submit);
    el('task-load-models').addEventListener('click', loadModels);
    el('task-provider').addEventListener('change', () => { resetModels(); updateControls(); });
    el('task-history-refresh').addEventListener('click', () => refresh());
    el('task-reconcile').addEventListener('click', loadHistory);
    el('task-allow-retry').addEventListener('click', () => {
      if (!confirm('The original task may still start and incur charges. Allow another submission anyway?')) return;
      state.uncertain.delete(state.project);
      el('task-cost-consent').checked = false;
      message('task-submit-status', 'Another submission is allowed. Review the goal and acknowledge charges again before starting.');
      updateControls();
    });
    el('task-provider-settings').addEventListener('click', event => { event.preventDefault(); navigate('/providers'); });
    window.addEventListener('pagehide', hide);
  }

  return { show, hide, refresh, focusGoal };
})();
