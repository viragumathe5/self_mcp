/* Project-scoped workflow UI. Polling never starts or retries model work. */
window.TasksUI = (() => {
  const active = new Set(['queued', 'discovering', 'planning', 'running', 'reviewing']);
  const labels = {
    queued: 'Queued', discovering: 'Discovering models', planning: 'Lead planning',
    pending: 'Waiting for dependencies', running: 'Working', reviewing: 'Lead reviewing',
    completed: 'Completed', needs_review: 'Draft · needs review', partial: 'Partial results', failed: 'Needs attention', cancelled: 'Cancelled', interrupted: 'Interrupted',
  };
  const state = {
    project: null, generation: 0, selection: 0, controller: null, timer: null,
    runId: null, runs: [], providers: [], routing: { mode: 'local_only', providers: [] }, modelLoading: false, initialized: false,
    rendered: '', pollVersion: 0, drafts: new Map(), submissions: new Set(), uncertain: new Map(), ready: false,
    selectedRun: null, settingsRunId: null, cancellations: new Set(),
    usage: null, nextOffset: null, pricingSignature: '', savingPrice: false,
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
    if (el(id).textContent !== text) el(id).textContent = text;
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
      throw Object.assign(new Error('Request could not be confirmed. Check the server and refresh task status before submitting again.'), { uncertain: true });
    }
    let result;
    try { result = await response.json(); } catch {
      throw Object.assign(new Error('The server returned an invalid task response. Refresh task status before retrying a submission.'), { uncertain: true });
    }
    if (!response.ok) throw Object.assign(new Error(typeof result.detail === 'string' ? result.detail : 'Task request failed.'), { uncertain: response.status >= 500, status: response.status });
    return result;
  }

  function current(generation) {
    return state.project !== null && generation === state.generation;
  }

  function resetModels() {
    message('task-direct-status', '');
    for (const [id, label] of [['task-lead-model', 'Automatic lead selection'], ['task-worker-model', 'Automatic selection for each task'], ['task-direct-model', 'Automatic selection']]) {
      el(id).replaceChildren(new Option(label, ''));
    }
    message('task-model-status', 'Models are chosen for each task, with approved alternatives if a model is unavailable. Choosing a specific model pins that role to it.');
  }

  function updateControls() {
    const run = state.selectedRun;
    el('task-saved-run').disabled = state.submissions.has(state.project) || state.uncertain.has(state.project);
    el('task-usage-summary').hidden = !run;
    el('task-run-actions').hidden = !run;
    el('task-cancel').hidden = !run || !active.has(run.status);
    el('task-cancel').disabled = state.cancellations.has(`${state.project}/${state.runId}`);
    el('task-run-problem').hidden = !run?.error;
    message('task-run-error', run?.error || '');
    message('task-run-recovery', run?.error ? (run.error_action || 'Your saved work is retained. Send a follow-up to continue this task, or change the model in Settings.') : '');
    if (run) {
      el('task-state-badge').replaceChildren(statusBadge(hasPartialResults(run) ? 'partial' : run.status === 'completed' && run.review_approved === false ? 'needs_review' : run.status));
      el('task-details-open').textContent = run.mode === 'direct' ? 'Model & run details →'
        : `Work details · ${run.tasks.filter(task => task.status === 'completed').length}/${run.tasks.length} steps →`;
    }
    const direct = el('task-mode').value === 'direct';
    const automatic = el('task-provider').value === 'auto';
    const configured = automatic ? eligibleProviders().length > 0
      : state.providers.some(provider => provider.id === el('task-provider').value && provider.configured);
    const submitting = state.submissions.has(state.project);
    const working = state.runId && (!state.selectedRun || active.has(state.selectedRun.status));
    el('task-submit').disabled = !state.ready || !configured || submitting || working || state.modelLoading || state.uncertain.has(state.project);
    el('task-uncertain-actions').hidden = submitting || !state.uncertain.has(state.project);
    el('task-allow-retry').textContent = state.uncertain.get(state.project)?.runId ? 'Retry the same message safely' : 'Allow another submission';
    el('task-allow-retry').disabled = submitting;
    el('task-submit').textContent = submitting ? 'Sending…' : !state.ready ? 'Connecting…' : working ? 'Working…' : state.runId ? 'Send follow-up' : direct ? 'Ask model' : 'Start task';
    el('task-goal').placeholder = state.runId ? 'What should we change or add? Earlier messages and results are included.' : 'Describe what you need…';
    el('task-conversation-hint').textContent = state.runId ? 'Continuing this task · earlier messages and results are included.' : 'A new task starts with this message.';
    el('task-goal').disabled = submitting;
    el('task-mode').disabled = submitting || state.modelLoading;
    el('task-mode-help').textContent = direct ? 'One model · no team review' : 'Plan, draft, and review';
    el('task-direct-model-row').hidden = !direct;
    el('task-direct-status').hidden = !direct;
    el('task-delegate-settings').hidden = direct;
    for (const id of ['task-max-tasks', 'task-max-revisions', 'task-schema-repair']) el(id).disabled = direct;
    el('task-run-form').setAttribute('aria-busy', String(submitting));
    el('task-load-models').disabled = !configured || submitting || state.modelLoading;
    el('task-direct-load').textContent = state.modelLoading ? 'Loading…' : 'Load models';
    el('task-direct-load').disabled = !configured || submitting || state.modelLoading;
    el('task-provider').disabled = submitting || state.modelLoading;
    for (const id of ['task-lead-model', 'task-worker-model', 'task-direct-model']) el(id).disabled = submitting || state.modelLoading || automatic;
  }

  function eligibleProviders() {
    return state.providers.filter(provider => provider.configured && state.routing.providers.includes(provider.id)
      && (state.routing.mode !== 'local_only' || provider.is_local === true));
  }

  function providerName(id) {
    return state.providers.find(provider => provider.id === id)?.name || id;
  }

  function updateProviderNotice() {
    const id = el('task-provider').value;
    if (id === 'auto') {
      const eligible = eligibleProviders();
      message('task-provider-status', eligible.length
        ? `Your team can use ${eligible.map(provider => provider.name).join(', ')}.`
        : 'No configured providers match your automatic routing permissions. Configure a local endpoint or change your usage preferences; no other provider will be selected automatically.', !eligible.length);
      message('task-model-status', 'Your approved models share the work: compact models for simple tasks, coding models for implementation, and stronger models for reasoning. Unavailable models can be replaced automatically.');
    } else {
      const provider = state.providers.find(item => item.id === id);
      message('task-provider-status', provider?.configured
        ? `${provider.name} · ${provider.is_local ? 'Runs locally' : 'Hosted models'}`
        : 'Configure a provider in Settings before starting a task.', !provider?.configured);
    }
  }

  // Tab-scoped recovery: keep unsent text and exact pending IDs across refreshes.
  // No credentials or permission checkbox are persisted. Storage may be unavailable.
  function persistProject(project) {
    const pending = state.uncertain.get(project);
    try {
      sessionStorage.setItem(`self-mcp.workspace.${project}`, JSON.stringify({
        draft: state.drafts.get(project),
        pending: pending ? { ...pending, knownIds: [...pending.knownIds] } : null,
      }));
    } catch { /* The workspace still works with in-memory recovery. */ }
  }

  function restoreProject(project) {
    if (state.drafts.has(project)) return;
    try {
      const saved = JSON.parse(sessionStorage.getItem(`self-mcp.workspace.${project}`) || 'null');
      if (saved?.draft && typeof saved.draft.goal === 'string') state.drafts.set(project, saved.draft);
      if (saved?.pending?.body && Array.isArray(saved.pending.knownIds)) {
        const pending = saved.pending;
        // Rebuild the URL from this project; never trust a stored request destination.
        pending.url = pending.runId ? `${endpoint(project)}/${encodeURIComponent(pending.runId)}/messages` : endpoint(project);
        pending.knownIds = new Set(pending.knownIds);
        state.uncertain.set(project, pending);
      }
    } catch { /* Ignore invalid or unavailable tab storage. */ }
  }

  function rememberAccepted(project, run, goal) {
    state.uncertain.delete(project);
    const draft = state.drafts.get(project);
    if (draft) state.drafts.set(project, { ...draft, runId: run.id, goal: draft.goal.trim() === goal ? '' : draft.goal });
    persistProject(project);
  }

  function captureDraft() {
    if (!state.project) return;
    state.drafts.set(state.project, {
      goal: el('task-goal').value, provider: el('task-provider').value,
      maxTasks: el('task-max-tasks').value, maxRevisions: el('task-max-revisions').value,
      leadModel: el('task-lead-model').value, workerModel: el('task-worker-model').value,
      includeContext: el("task-project-context").checked, schemaRepair: el("task-schema-repair").checked,
      runId: state.runId, mode: el('task-mode').value, directModel: el('task-direct-model').value,
      timeout: el('task-timeout').value, outputBudget: el('task-output-budget').value, reasoning: el('task-reasoning').value,
    });
    persistProject(state.project);
  }

  function hide() {
    captureDraft();
    state.generation += 1;
    state.selection += 1;
    state.controller?.abort();
    clearTimeout(state.timer);
    el('task-details-panel').close();
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
    state.selectedRun = null;
    state.usage = null;
    state.nextOffset = null;
    state.pricingSignature = '';
    el('task-price-model').replaceChildren();
    message('task-price-status', '');
    el('task-saved-run').replaceChildren(new Option('Loading…', ''));
    el('task-more-runs').hidden = true;
    state.ready = false;
    restoreProject(project.id);
    const draft = state.drafts.get(project.id);
    state.runId = draft?.runId || null;
    state.settingsRunId = draft?.runId || null;
    el('task-run-form').reset();
    el('task-goal').value = draft?.goal || '';
    el('task-mode').value = draft?.mode || 'delegate';
    el('task-timeout').value = draft?.timeout || '';
    el('task-output-budget').value = draft?.outputBudget || '';
    el('task-reasoning').value = draft?.reasoning || 'auto';
    el('task-details-content').replaceChildren();
    el('task-project-context').checked = draft?.includeContext || false;
    el('task-schema-repair').checked = draft?.schemaRepair ?? true;
    el('task-max-tasks').value = draft?.maxTasks || '6';
    el('task-max-revisions').value = draft?.maxRevisions || '2';
    el('task-provider').replaceChildren(new Option('Loading providers…', ''));
    el('task-run-detail').replaceChildren();
    el('task-empty-state').hidden = Boolean(state.runId);
    el('task-advanced').open = false;
    resetModels();
    for (const [id, model] of [['task-lead-model', draft?.leadModel], ['task-worker-model', draft?.workerModel], ['task-direct-model', draft?.directModel]]) {
      if (model) {
        el(id).append(new Option(`${model} (rechecked at task start)`, model));
        el(id).value = model;
      }
    }
    updateControls();
    message('task-submit-status', state.uncertain.has(project.id)
      ? 'A previous submission was not confirmed. Check task status before sending again.' : '', state.uncertain.has(project.id));
    message('task-run-status', '');
    await refresh(draft?.provider);
  }

  async function loadProviders(preferred) {
    const generation = state.generation;
    try {
      const status = await request('/api/providers', { signal: state.controller.signal });
      if (!current(generation)) return;
      const previous = preferred || el('task-provider').value;
      state.providers = status.providers;
      state.routing = status.routing || { mode: 'local_only', providers: [] };
      const automatic = new Option('Automatic · approved connections', 'auto');
      automatic.disabled = eligibleProviders().length === 0;
      el('task-provider').replaceChildren(automatic, ...status.providers.map(provider => {
        const option = new Option(`${provider.name}${provider.configured ? '' : ' — not configured'}`, provider.id);
        option.disabled = !provider.configured;
        return option;
      }));
      const configured = status.providers.filter(provider => provider.configured);
      el('task-provider').value = (previous === 'auto' && !automatic.disabled ? 'auto' : configured.find(provider => provider.id === previous)?.id)
        || (state.routing.providers.length ? 'auto' : '')
        || configured.find(provider => provider.id === status.default_provider)?.id || configured[0]?.id || '';
      if (state.runId && previous) {
        if (![...el('task-provider').options].some(option => option.value === previous)) {
          const missing = new Option(`${providerName(previous)} (unavailable)`, previous); missing.disabled = true;
          el('task-provider').append(missing);
        }
        el('task-provider').value = previous;
      }
      if (previous !== el('task-provider').value) resetModels();
      updateProviderNotice();
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
    const pinned = ['task-lead-model', 'task-worker-model', 'task-direct-model'].map(id => [id, el(id).value]);
    state.modelLoading = true;
    resetModels();
    updateControls();
    message('task-model-status', 'Discovering accessible text-generation models…');
    message('task-direct-status', 'Loading available models…');
    try {
      const options = await request(`${endpoint(state.project)}/model-options`, {
        method: 'POST', body: { provider }, signal: state.controller.signal,
      });
      if (!current(generation) || el('task-provider').value !== provider) return;
      message('task-direct-status', provider === 'auto' ? 'Automatic selection uses your approved connections. Choose a connection to select a specific model.' : `${options.models.length} models available. A selected model stays pinned.`);
      if (provider === 'auto') {
        const pool = (options.worker_candidates || []).map(item => `${providerName(item.provider)} / ${item.model}${item.is_local ? ' (local endpoint)' : ' (remote)'}`).join('; ');
        message('task-model-status', `${options.reason} Lead: ${providerName(options.lead_provider)} / ${options.lead_model}. Default workers: ${providerName(options.worker_provider)} / ${options.worker_model}. Offered worker models: ${pool || 'none'}. ${(options.warnings || []).join(' ')}`);
      } else {
        for (const id of ['task-lead-model', 'task-worker-model', 'task-direct-model']) {
          for (const model of options.models) el(id).append(new Option(`${model.name} (${model.id})`, model.id));
        }
        message('task-model-status', `${options.reason} Recommended lead: ${options.lead_model || 'choose explicitly'}; workers: ${options.worker_model || 'choose explicitly'}. ${options.models.length} compatible identifiers listed. Inference access can still fail.`);
      }
    } catch (error) {
      if (current(generation)) { message('task-model-status', error.message, true); message('task-direct-status', error.message, true); }
    } finally {
      if (current(generation)) {
        for (const [id, model] of pinned) {
          if (model && el('task-provider').value === provider) {
            if (![...el(id).options].some(option => option.value === model)) el(id).append(new Option(`${model} (rechecked when sent)`, model));
            el(id).value = model;
          }
        }
        state.modelLoading = false;
        updateControls();
      }
    }
  }

  async function loadWorkspace() {
    if (!state.project) return;
    const generation = state.generation;
    const selection = state.selection;
    state.ready = false;
    updateControls();
    message('task-run-status', 'Checking your task…');
    try {
      const result = await request(endpoint(state.project), { signal: state.controller.signal });
      if (!current(generation) || selection !== state.selection) return;
      state.runs = result.runs;
      state.nextOffset = result.next_offset ?? null;
      state.ready = true;
      const uncertain = state.uncertain.get(state.project);
      if (uncertain) {
        const match = uncertain.runId
          ? result.runs.find(run => run.id === uncertain.runId && [run, ...(run.turns || [])].some(turn => turn.turn_id === uncertain.body.message_id))
          : result.runs.find(run => !uncertain.knownIds.has(run.id) && run.goal === uncertain.goal && run.provider === uncertain.provider);
        if (match) {
          rememberAccepted(state.project, match, uncertain.body.goal);
          state.runId = match.id;
          state.selection += 1;
          state.rendered = '';
          el('task-cost-consent').checked = false;
          if (el('task-goal').value.trim() === uncertain.body.goal) el('task-goal').value = '';
          message('task-submit-status', 'Message confirmed. Your saved task is shown; no work was resubmitted.');
        } else {
          message('task-submit-status', uncertain.runId ? 'Message not yet confirmed. Check status or retry the same message safely.' : 'Submission is still unconfirmed. Check status before allowing another submission; the original may still run.', true);
        }
        updateControls();
      }
      el('ptab-tasks-badge').textContent = state.runId ? '1' : '0';
      if (!state.runId && state.runs.length) await selectRun(state.runs[0].id);
      else if (state.runId) await pollRun();
      else message('task-run-status', 'Ready for your first message.');
      renderRunChoices();
      updateControls();
      captureDraft();
    } catch (error) {
      if (current(generation)) {
        state.ready = false;
        message('task-run-status', `${error.message} Use Refresh status to reconnect to your saved task.`, true);
        updateControls();
      }
    }
  }

  async function refresh(preferred) {
    if (!state.project) return;
    const generation = state.generation;
    await loadProviders(preferred);
    if (current(generation)) await loadWorkspace();
  }

  async function selectRun(runId, focus = false) {
    clearTimeout(state.timer);
    state.runId = runId;
    state.selectedRun = null;
    state.usage = null;
    state.pricingSignature = '';
    message('task-usage-summary', 'Loading usage…');
    state.settingsRunId = null;
    el('task-details-panel').close();
    state.selection += 1;
    state.rendered = '';
    el('task-run-detail').replaceChildren();
    el('task-empty-state').hidden = true;
    message('task-run-status', 'Loading task…');
    el('ptab-tasks-badge').textContent = state.runId ? '1' : '0';
    updateControls();
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
      const url = `${endpoint(state.project)}/${state.runId}`;
      const run = await request(url, { signal: state.controller.signal });
      // Read accounting after the run, so a final answer cannot retain an earlier usage snapshot.
      const usage = await request(`${url}/usage`, { signal: state.controller.signal }).catch(() => null);
      if (!current(generation) || selection !== state.selection || version !== state.pollVersion) return;
      const index = state.runs.findIndex(item => item.id === run.id);
      if (index >= 0) state.runs[index] = run;
      else state.runs.unshift(run);
      state.usage = usage?.total ? usage : null;
      state.ready = true;
      renderRun(run);
      renderRunChoices();
      captureDraft();
      again = active.has(run.status);
    } catch (error) {
      if (current(generation) && selection === state.selection && version === state.pollVersion) {
        state.ready = false;
        message('task-run-status', `${error.message} Reconnecting; your work is not restarted.`, true);
        updateControls();
      }
    } finally {
      if (again && current(generation) && selection === state.selection && version === state.pollVersion) state.timer = setTimeout(pollRun, 2500);
    }
  }

  function statusBadge(status) {
    const known = labels[status] ? status : 'unknown';
    return node('span', labels[status] || status, `task-status task-status-${known}`);
  }

  function dated(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? 'Date unavailable' : date.toLocaleString();
  }

  function renderRunChoices() {
    el('task-saved-run').replaceChildren(...(state.runs.length ? state.runs.map(run =>
      new Option(`${(run.root_goal || run.goal).slice(0, 65)} · ${dated(run.created_at)} · ${run.id.slice(0, 8)}`, run.id))
      : [new Option('No saved task', '')]));
    el('task-saved-run').value = state.runId || '';
    el('task-more-runs').hidden = state.nextOffset === null;
  }

  async function loadOlderRuns() {
    const generation = state.generation;
    const button = el('task-more-runs');
    button.disabled = true;
    try {
      const result = await request(`${endpoint(state.project)}?offset=${state.nextOffset}`, { signal: state.controller.signal });
      if (!current(generation)) return;
      for (const run of result.runs) if (!state.runs.some(item => item.id === run.id)) state.runs.push(run);
      state.nextOffset = result.next_offset ?? null;
      renderRunChoices();
    } catch (error) {
      if (current(generation)) message('task-submit-status', `Could not load older tasks. ${error.message}`, true);
    } finally { button.disabled = false; }
  }

  const number = value => Number(value || 0).toLocaleString();
  function usageText(value) {
    if (!value) return 'Usage unavailable; refresh to retry.';
    const cost = value.estimated_cost_usd == null ? 'cost unavailable' : `est. $${value.estimated_cost_usd.toFixed(6)} USD${value.unpriced_calls ? ' (partial)' : ''}`;
    return `${number(value.input_tokens)} input + ${number(value.output_tokens)} output tokens · ${number(value.model_calls)} calls · ${cost}${value.unknown_calls ? ` · ${value.unknown_calls} calls with missing token counts` : ''}`;
  }

  function usageDetails() {
    const usage = state.usage;
    el('task-pricing-form').hidden = !usage?.models.some(row => row.provider && row.model);
    message('task-usage-summary', `Whole task: ${usageText(usage?.total)}`);
    const contents = [node('p', `Whole task: ${usageText(usage?.total)}`)];
    if (!usage) return disclosure('usage', 'Tokens & cost', contents, true);
    contents.push(node('p', `This message: ${usageText(usage.turns.at(-1))}`),
      node('p', `All saved tasks in this project: ${usageText(usage.project_total)}`),
      node('p', 'Token counts are provider-reported. Cost is an estimate using your configured prices, not a provider invoice. Retries and revisions are included.', 'task-help'));
    if (usage.total.unpriced_calls) contents.push(node('p', `${usage.total.unpriced_calls} calls have no complete cost estimate. Add known model prices below. Older calls without model records cannot be priced reliably.`, 'task-help'));
    if (usage.unattributed_tokens?.input_tokens || usage.unattributed_tokens?.output_tokens) contents.push(node('p',
      `${number(usage.unattributed_tokens.input_tokens)} input and ${number(usage.unattributed_tokens.output_tokens)} output tokens are included in the task total but lack individual call records.`, 'task-help'));
    for (const row of usage.models) contents.push(node('p', `${row.provider ? providerName(row.provider) : 'Older calls'} / ${row.model || 'model not recorded'} · ${row.stages.join(', ')}\n${usageText(row)}`));
    contents.push(disclosure('usage-calls', `Individual calls · ${usage.calls.length}`, usage.calls.map(call => node('p',
      `#${call.call_id} · ${call.stage}${call.task_id ? ` · ${call.task_id}` : ''} · ${call.model || 'Model not recorded'}\nInput: ${call.input_tokens ?? 'unknown'} · Output: ${call.output_tokens ?? 'unknown'} · ${call.estimated_cost_usd == null ? 'Cost unavailable' : `Est. $${call.estimated_cost_usd.toFixed(6)} USD`}`))));
    const models = usage.models.filter(row => row.provider && row.model);
    const signature = JSON.stringify(models.map(row => [row.provider, row.model]));
    if (signature !== state.pricingSignature) {
      state.pricingSignature = signature;
      const previous = el('task-price-model').value;
      el('task-price-model').replaceChildren(...models.map(row => new Option(`${providerName(row.provider)} / ${row.model}`, JSON.stringify([row.provider, row.model]))));
      if ([...el('task-price-model').options].some(option => option.value === previous)) el('task-price-model').value = previous;
      fillPrice();
    }
    el('task-price-save').disabled = state.savingPrice || !models.length;
    return disclosure('usage', 'Tokens & cost', contents, true);
  }

  function fillPrice() {
    const selected = el('task-price-model').value;
    const row = state.usage?.models.find(item => JSON.stringify([item.provider, item.model]) === selected);
    el('task-price-input').value = row?.rates?.input_per_million ?? '';
    el('task-price-output').value = row?.rates?.output_per_million ?? '';
  }

  async function savePrice(event) {
    event.preventDefault();
    const generation = state.generation, runId = state.runId;
    const [provider, model] = JSON.parse(el('task-price-model').value);
    const button = el('task-price-save');
    if (state.savingPrice) return;
    state.savingPrice = true;
    button.disabled = true;
    try {
      await request(`${endpoint(state.project)}/pricing`, { method: 'PUT', body: { provider, model,
        input_per_million: Number(el('task-price-input').value), output_per_million: Number(el('task-price-output').value) } });
      if (current(generation) && state.runId === runId) {
        message('task-price-status', 'Prices saved. Estimates include past recorded calls in this project.');
        await pollRun();
      }
    } catch (error) { if (current(generation)) message('task-price-status', error.message, true); }
    finally { state.savingPrice = false; button.disabled = !state.usage?.models.some(row => row.model && row.provider); }
  }

  function disclosure(key, title, children, expanded = false) {
    const details = node('details', undefined, 'task-disclosure');
    details.dataset.section = key;
    details.open = expanded;
    details.append(node('summary', title), ...children);
    return details;
  }

  function copyButton(text, label = 'Copy result') {
    const button = node('button', label, 'btn btn-secondary btn-sm');
    button.type = 'button';
    button.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(text); button.textContent = 'Copied'; }
      catch { button.textContent = 'Select the text to copy'; }
    });
    return button;
  }

  function output(text) {
    const content = text || 'No output yet.';
    // An allowlist keeps model HTML inert and prevents images/embeds loading remotely.
    if (!window.marked || !window.DOMPurify) return node('pre', content, 'task-output');
    const view = node('div', undefined, 'task-prose');
    view.innerHTML = DOMPurify.sanitize(marked.parse(content), {
      ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'del', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'hr', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a'],
      ALLOWED_ATTR: ['href', 'title'], SANITIZE_NAMED_PROPS: true,
    });
    for (const link of view.querySelectorAll('a')) {
      const href = link.getAttribute('href') || '';
      if (!/^https?:\/\//i.test(href)) link.removeAttribute('href');
      else { link.target = '_blank'; link.rel = 'noopener noreferrer'; }
    }
    for (const pre of view.querySelectorAll('pre')) {
      const block = node('div', undefined, 'task-code-block');
      const toolbar = node('div', undefined, 'task-code-toolbar');
      toolbar.append(node('span', 'Code'), copyButton(pre.textContent, 'Copy code'));
      pre.replaceWith(block);
      block.append(toolbar, pre);
    }
    return view;
  }

  function taskCard(task, defaultProvider, scope = '') {
    const card = node('details', undefined, 'task-card');
    card.dataset.section = `${scope}task-${task.id}`;
    const header = node('summary');
    header.append(node('h4', task.title), statusBadge(task.status));
    const criteria = node('ol');
    for (const criterion of task.acceptance_criteria) criteria.append(node('li', criterion));
    card.append(header,
      node('p', task.depends_on.length ? `Depends on: ${task.depends_on.join(', ')}` : 'Can work independently', 'task-dependencies'),
      node('p', `Provider: ${providerName(task.provider || defaultProvider)} · Model: ${task.model} · Attempts: ${task.attempts}`, 'task-help'),
      ...(task.routing_reason ? [node('p', task.routing_reason, 'task-help')] : []),
      node('p', task.instructions), node('p', 'Acceptance checks · set by the lead', 'task-help'),
      node('p', 'These check the requested draft, not executed or verified results. Optional improvements should not block acceptance.', 'task-help'), criteria, output(task.output));
    const older = (task.output_versions || []).filter(version => version.version !== task.output_version);
    if (older.length) card.append(disclosure(`${scope}versions-${task.id}`, `Earlier drafts · ${older.length}`, older.map(version =>
      disclosure(`${scope}version-${task.id}-${version.version}`, `Version ${version.version} · ${version.model || 'Worker'}`,
        [copyButton(version.output, 'Copy draft'), output(version.output)]))));
    if (task.error) card.append(node('p', task.error, 'task-error'));
    return card;
  }

  function savedDrafts(run) {
    return (run.tasks || []).map(task => ({ ...task,
      savedOutput: task.output || (task.output_versions || []).slice().reverse().find(version => version.output)?.output,
    })).filter(task => task.savedOutput);
  }

  function hasPartialResults(run) {
    return !active.has(run.status) && !run.final_output && savedDrafts(run).length > 0;
  }

  function knowledgeDetails(run, prefix = '') {
    const reads = run.knowledge_retrievals || [];
    if (!reads.length) return null;
    const calls = reads.map(read => {
      const content = [node('p', `${read.tokens || 0} context tokens (cl100k_base) · ${read.retrieval_ms || 0} ms local retrieval · ${read.status.replaceAll('_', ' ')}`, 'task-help')];
      if (read.status === 'unavailable' || read.status === 'skipped_budget') content.push(node('p', read.notice, 'task-notice'));
      for (const source of read.results || []) {
        content.push(disclosure(`${prefix}source-${read.call_id}-${source.source_id}`, source.title, [
          node('p', `${source.kind.replaceAll('_', ' ')} · ${source.review_status.replaceAll('_', ' ')}${source.version ? ` · version ${source.version}` : ''}${source.message_number ? ` · message ${source.message_number}${source.is_current_message === false ? ' (earlier)' : ''}` : ''}`, 'task-help'),
          ...(source.model ? [node('p', `${providerName(source.provider)} / ${source.model}`, 'task-help')] : []),
          node('p', source.source_id, 'task-help'),
          node('pre', source.excerpt, 'task-output'),
          node('p', `Source characters ${source.offset}–${source.end_offset}${source.excerpt_truncated ? ' · excerpt; full source is available through MCP' : ''}`, 'task-help'),
        ]));
      }
      if (!read.results?.length) content.push(node('p', 'No additional project passages were included in this call.', 'task-help'));
      return disclosure(`${prefix}knowledge-call-${read.call_id}`, `Call ${read.call_id} · ${read.stage}${read.task_id ? ` · ${read.task_id}` : ''} · ${read.results?.length || 0} sources`, content);
    });
    return disclosure(`${prefix}knowledge`, `Knowledge used · ${reads.length} calls`, [
      node('p', 'Only this project is searched. These are the excerpts supplied to each model, not proof that the model used them. Model drafts and reviews are not verified facts. Retrieval adds no model calls; included context counts toward provider input usage.', 'task-help'), ...calls,
    ]);
  }

  function renderRun(run) {
    state.selectedRun = run;
    const savedIndex = state.runs.findIndex(item => item.id === run.id);
    if (savedIndex >= 0) state.runs[savedIndex] = run;
    if (state.settingsRunId !== run.id) {
      applyRunSettings(run);
      state.settingsRunId = run.id;
    }
    updateControls();
    const direct = run.mode === 'direct';
    const usageBlock = usageDetails();
    const signature = JSON.stringify([run, state.usage]);
    const done = run.tasks.filter(task => task.status === 'completed').length;
    const summaries = {
      queued: 'Your task is queued.', discovering: 'Finding the models available to your team.',
      planning: 'Your lead is breaking the goal into steps.',
      running: direct ? 'Your model is working. The answer will appear when ready.' : `${run.tasks.find(task => task.status === 'running')?.title || 'Working'} · ${done} of ${run.tasks.length} steps complete${run.revision_count ? ' · Refining after review' : ''}.`,
      reviewing: 'Your lead is checking the work and combining the result.',
      completed: run.review_approved === false ? 'Final draft saved. Review notes remain; you can continue with a follow-up.' : direct ? 'Answer saved.' : 'Your reviewed draft is ready.', failed: 'Stopped before a final answer was saved. Open “Why this stopped” for the cause and next step.',
      cancelled: 'Cancelled. Your work so far is saved.', interrupted: 'Interrupted. Your work so far is saved.',
    };
    message('task-run-status', hasPartialResults(run)
      ? `${savedDrafts(run).length} worker draft(s) saved. ${run.status === 'cancelled' ? 'Cancelled' : run.stopped_stage === 'reviewing' ? 'Review could not finish' : 'Work stopped'}; available results are shown above. You can continue with a follow-up.`
      : summaries[run.status] || run.status);
    el('task-empty-state').hidden = true;
    if (signature === state.rendered) return;
    state.rendered = signature;
    const opened = new Map([...el('task-details-content').querySelectorAll('details'), ...el('task-run-detail').querySelectorAll('details')].map(item => [item.dataset.section, item.open]));
    const focusedSection = document.activeElement?.closest('details')?.dataset.section;
    const header = node('section', undefined, 'setup-card task-run-header');
    const heading = node('div', undefined, 'task-section-heading');
    heading.append(node('h3', run.root_goal || run.goal, 'task-goal-display'));
    header.append(heading);
    header.append(node('p', `Task ${run.id.slice(0, 8)} · Message ${(run.turns?.length || 0) + 1} · ${dated(run.turn_started_at || run.created_at)}`, 'task-help'));
    const steps = node('section', undefined, 'task-breakdown');
    if (!run.tasks.length) steps.append(node('p', 'The work breakdown will appear after planning.', 'task-help'));
    for (const task of run.tasks) steps.append(taskCard(task, run.worker_provider || run.provider));
    const reviews = node('section', undefined, 'task-reviews');
    if (!run.reviews.length) reviews.append(node('p', 'Review starts when the drafts are ready.', 'task-help'));
    for (const [index, review] of run.reviews.entries()) {
      reviews.append(disclosure(`review-${index}`, `Review ${index + 1}: ${review.approved ? 'Approved draft' : 'Revision requested'}`, [
        node('p', review.feedback),
        ...(review.task_reviews || []).map(item => disclosure(`review-${index}-${item.task_id}`, `${item.task_id} · ${item.verdict === 'keep' ? 'Keep' : 'Revise'}`, [
          node('p', item.reason), ...(item.preserve || []).map(text => node('p', `Preserve: ${text}`, 'task-help')),
          ...(item.criterion_index ? [node('p', `Acceptance check ${item.criterion_index}`, 'task-help')] : []),
          ...(item.evidence ? [node('p', `Evidence: ${item.evidence}`)] : []),
          ...(item.change ? [node('p', `Change: ${item.change}`)] : []),
        ])),
        ...(review.decisions || []).map(text => node('p', `Decision: ${text}`, 'task-help')),
        ...(review.suggestions || []).map(text => node('p', `Optional improvement: ${text}`, 'task-help')),
        node('p', review.revision_tasks.length ? `Revise: ${review.revision_tasks.join(', ')}. Dependent tasks are regenerated too.` : 'No further revisions requested.'),
      ], true));
    }
    const events = node('ol', undefined, 'task-events');
    for (const event of run.events) events.append(node('li', `${new Date(event.time).toLocaleTimeString()} — ${event.message}`));
    const usage = run.usage;
    const technical = [
      node('p', `${direct ? 'Model' : 'Lead'}: ${providerName(run.lead_provider || run.provider)} / ${run.lead_model || 'Selecting…'}`),
      ...(!direct ? [node('p', `Default workers: ${providerName(run.worker_provider || run.provider)} / ${run.worker_model || 'Selecting…'}`),
        node('p', `Revisions: ${run.revision_count}/${run.max_revisions}`)] : []),
      node('p', `Routing: ${run.routing_mode || 'single provider'}`),
      node('p', `Automatic model switches: ${run.fallback_count || 0}`),
      node('p', `Model calls: ${usage.model_calls} · Reported input tokens: ${usage.input_tokens} · Reported output tokens: ${usage.output_tokens}`),
      node('p', `Run ID: ${run.id}`),
      node('p', `Messages: ${(run.turns?.length || 0) + 1} · Calls for this message: ${usage.model_calls - (run.turn_start_usage?.model_calls || 0)}`),
    ];
    if (run.inference_settings) technical.push(node('p', `Call timeout: ${run.inference_settings.timeout_seconds}s · Output budget: ${run.inference_settings.output_tokens} tokens · Reasoning: ${run.inference_settings.reasoning_effort || 'Provider default'}`));
    if (usage.unknown_calls) technical.push(node('p', `${usage.unknown_calls} call(s) have incomplete usage data. Failed or cancelled calls may still be billed.`));
    if (run.project_context) technical.push(disclosure('context', 'Attached project knowledge', [node('pre', JSON.stringify(run.project_context, null, 2), 'task-output')]));
    const blocks = [header];
    const exchanges = [];
    for (const [index, turn] of (run.turns || []).entries()) {
      const turnUsage = state.usage?.turns.find(item => item.turn_id === (turn.turn_id || turn.id));
      const contents = [node('p', `${dated(turn.turn_started_at || turn.created_at)} · ${labels[turn.status] || turn.status}${turn.review_approved === false ? ' · needs review' : ''}`, 'task-help'),
        node('p', turn.goal, 'task-message-user'), node('p', usageText(turnUsage), 'task-help')];
      if (turn.final_output) contents.push(copyButton(turn.final_output, 'Copy saved answer'), output(turn.final_output));
      if (turn.tasks?.length) contents.push(disclosure(`turn-work-${turn.turn_id || index}`, 'Saved worker drafts', turn.tasks.map(task => taskCard(task, turn.worker_provider || turn.provider, `turn-${turn.turn_id || index}-`))));
      if (turn.error) contents.push(node('p', turn.error, 'task-error'));
      const knowledge = knowledgeDetails(turn, `turn-${turn.turn_id || index}-`);
      if (knowledge) contents.push(knowledge);
      const exchange = disclosure(`turn-${turn.turn_id || index}`, `${index + 1}. ${turn.goal} · ${dated(turn.turn_started_at || turn.created_at)}`, contents);
      exchange.classList.add('task-exchange');
      exchanges.push(exchange);
    }
    if (exchanges.length) blocks.push(disclosure('conversation', `Previous messages in this task · ${exchanges.length}`, exchanges));
    if (run.turns?.length) blocks.push(node('p', run.goal, 'task-message-user task-current-message'));
    if (run.history_context?.omitted_turns) blocks.push(node('p', `Using the original goal and ${run.history_context.included_turns} earlier exchanges that fit this model. ${run.history_context.omitted_turns} older exchange(s) remain saved in the details drawer.`, 'task-help'));
    if (hasPartialResults(run)) {
      const drafts = savedDrafts(run);
      const partial = node('section', undefined, 'task-partial');
      const heading = node('div', undefined, 'task-section-heading');
      const disclaimer = 'Saved worker drafts — incomplete and not approved. Code has not been executed or verified.';
      const combined = [disclaimer, ...drafts.map(task => `## ${task.title}\n\n${task.savedOutput}`)].join('\n\n');
      heading.append(node('h3', 'Available results · needs review'), copyButton(combined, 'Copy all drafts'));
      partial.append(heading, node('p', disclaimer, 'task-notice'));
      for (const task of drafts) {
        const draft = node('section', undefined, 'task-saved-draft');
        const title = node('div', undefined, 'task-section-heading');
        title.append(node('h4', task.title), copyButton(task.savedOutput, 'Copy draft'));
        draft.append(title);
        if (task.status !== 'completed') draft.append(node('p', 'Last saved version; the latest attempt did not finish.', 'task-help'));
        draft.append(output(task.savedOutput));
        partial.append(draft);
      }
      const review = run.reviews.at(-1);
      if (review && !review.approved) partial.append(disclosure('partial-review', 'Unresolved review notes', [
        node('p', review.feedback), ...(review.task_reviews || []).filter(item => item.verdict === 'revise').map(item =>
          node('p', `${item.task_id}: ${item.reason}${item.change ? ` Suggested repair: ${item.change}` : ''}`)),
      ]));
      blocks.push(partial);
    }
    const lastAnswer = [...(run.turns || [])].reverse().find(turn => turn.final_output);
    const visibleAnswer = run.final_output || lastAnswer?.final_output;
    if (visibleAnswer) {
      const final = node('section', undefined, 'setup-card task-final');
      const title = node('div', undefined, 'task-section-heading');
      title.append(node('h3', run.final_output ? (run.review_approved === false ? 'Final draft · needs review' : direct ? 'Answer' : 'Reviewed result') : 'Last saved answer'), copyButton(visibleAnswer));
      final.append(title);
      if (!run.final_output) final.append(node('p', active.has(run.status) ? 'Updating this answer with your latest message…' : 'Your last answer is preserved. Send a follow-up to continue.', 'task-help'));
      if (!run.final_output) final.append(node('p', `From message ${(run.turns || []).indexOf(lastAnswer) + 1} · ${dated(lastAnswer.turn_started_at || lastAnswer.created_at)}. This answer predates your latest request.`, 'task-help'));
      final.append(output(visibleAnswer));
      blocks.push(final);
    }
    const details = direct ? [] : [disclosure('breakdown', `Work breakdown · ${done}/${run.tasks.length} steps`, [steps], true),
      disclosure('reviews', `Reviews · ${run.reviews.length}`, [reviews])];
    details.unshift(usageBlock);
    const knowledge = knowledgeDetails(run);
    if (knowledge) details.push(knowledge);
    if (run.shared_memory) {
      const memory = run.shared_memory;
      details.push(disclosure('shared-memory', 'Shared context between models', [
        node('p', 'Review decisions are model proposals. Your latest message takes precedence.', 'task-help'),
        ...(memory.decisions || []).map(item => node('p', item.decision)),
        node('p', `${memory.prior_work?.length || 0} earlier worker excerpts included. ${memory.omitted_artifacts || 0} older drafts remain saved.`, 'task-help'),
      ]));
    }
    details.push(disclosure('technical' , 'Models & usage', technical, direct), disclosure('events', 'Activity log', [events]));
    el('task-run-detail').replaceChildren(...blocks);
    el('task-details-content').replaceChildren(...details);
    for (const details of [...el('task-details-content').querySelectorAll('details'), ...el('task-run-detail').querySelectorAll('details')]) {
      // Keep expanded details stable while progress updates arrive.
      if (opened.has(details.dataset.section)) details.open = opened.get(details.dataset.section);
      if (focusedSection === details.dataset.section && el('task-details-panel').open) details.querySelector('summary')?.focus({ preventScroll: true });
    }
    el('ptab-tasks-badge').textContent = state.runId ? '1' : '0';
  }

  function applyRunSettings(run) {
    el('task-mode').value = run.mode || 'delegate';
    if (![...el('task-provider').options].some(option => option.value === run.provider)) {
      const missing = new Option(`${providerName(run.provider)} (unavailable)`, run.provider); missing.disabled = true;
      el('task-provider').append(missing);
    }
    el('task-provider').value = run.provider;
    resetModels();
    for (const [id, model, pinned] of [['task-direct-model', run.lead_model, run.lead_model_pinned],
      ['task-lead-model', run.lead_model, run.lead_model_pinned], ['task-worker-model', run.worker_model, run.worker_model_pinned]]) {
      if (model && pinned !== false && run.provider !== 'auto') {
        el(id).append(new Option(model, model)); el(id).value = model;
      }
    }
    el('task-project-context').checked = run.include_project_context || false;
    el('task-schema-repair').checked = Boolean(run.schema_retries);
    el('task-max-tasks').value = run.max_tasks || 6;
    el('task-max-revisions').value = run.max_revisions ?? 2;
    el('task-timeout').value = run.timeout_seconds || '';
    el('task-output-budget').value = run.output_tokens || '';
    el('task-reasoning').value = run.reasoning_effort || 'auto';
    el('task-cost-consent').checked = false;
    updateProviderNotice();
  }

  async function submit(event) {
    event.preventDefault();
    const project = state.project;
    if (!project || !state.ready || state.submissions.has(project) || state.modelLoading || state.uncertain.has(project)) return;
    if (state.runId && (!state.selectedRun || active.has(state.selectedRun.status))) return;
    if (!el('task-run-form').reportValidity()) return;
    const body = {
      mode: el('task-mode').value,
      goal: el('task-goal').value.trim(), provider: el('task-provider').value,
      lead_model: el('task-provider').value === 'auto' ? null : el(el('task-mode').value === 'direct' ? 'task-direct-model' : 'task-lead-model').value || null,
      worker_model: el('task-provider').value === 'auto' || el('task-mode').value === 'direct' ? null : el('task-worker-model').value || null,
      include_project_context: el("task-project-context").checked,
      schema_retries: el('task-mode').value === 'direct' ? 0 : el("task-schema-repair").checked ? 1 : 0,
      max_tasks: el('task-mode').value === 'direct' ? 1 : Number(el('task-max-tasks').value),
      max_revisions: el('task-mode').value === 'direct' ? 0 : Number(el('task-max-revisions').value),
      timeout_seconds: el('task-timeout').value ? Number(el('task-timeout').value) : null,
      output_tokens: el('task-output-budget').value ? Number(el('task-output-budget').value) : null,
      reasoning_effort: el('task-reasoning').value,
    };
    const runId = state.runId;
    const selection = state.selection;
    if (runId) {
      body.message_id = crypto.randomUUID();
      body.expected_turn_id = state.selectedRun.turn_id || state.selectedRun.id;
    }
    const url = runId ? `${endpoint(project)}/${runId}/messages` : endpoint(project);
    if (!body.goal || new TextEncoder().encode(JSON.stringify(body)).length > 16384) {
      message('task-submit-status', 'Enter a goal within the 16 KiB request limit. Shorten the goal if it contains many non-ASCII characters.', true);
      return;
    }
    const knownIds = new Set(state.runs.map(run => run.id));
    state.submissions.add(project);
    state.uncertain.set(project, { goal: body.goal, provider: body.provider, knownIds, runId, body, url });
    captureDraft();
    updateControls();
    message('task-submit-status', 'Submitting once. The run will continue if you leave this tab.');
    try {
      // Do not abort submissions on navigation: an accepted request starts paid work.
      const run = await request(url, { method: 'POST', body });
      if (!run || typeof run.id !== 'string' || run.store_id !== project || !Array.isArray(run.tasks)) {
        throw Object.assign(new Error('Submission response was incomplete. Check task status before sending again.'), { uncertain: true });
      }
      rememberAccepted(project, run, body.goal);
      if (state.project !== project) return;
      state.runs = [run, ...state.runs.filter(item => item.id !== run.id)].slice(0, 50);
      if (state.selection !== selection) { el('ptab-tasks-badge').textContent = state.runId ? '1' : '0'; return; }
      el('task-cost-consent').checked = false;
      el('task-goal').value = '';
      el('task-advanced').open = false;
      message('task-submit-status', runId ? 'Follow-up added to this task. Earlier messages are included.' : 'Task started. Send follow-ups here to build on it.');
      await selectRun(run.id, true);
    } catch (error) {
      if (!error.uncertain) state.uncertain.delete(project);
      persistProject(project);
      if (runId && error.status === 409 && state.project === project) await loadWorkspace();
      if (state.project === project) message('task-submit-status', error.message, true);
    } finally {
      state.submissions.delete(project);
      if (state.project === project) updateControls();
    }
  }

  async function cancelRun(runId) {
    const project = state.project;
    const key = `${project}/${runId}`;
    if (!project || !runId || state.cancellations.has(key) || !confirm('Cancel this run? Already-issued model calls may still incur charges.')) return;
    state.cancellations.add(key);
    updateControls();
    const generation = state.generation;
    try {
      const run = await request(`${endpoint(project)}/${runId}/cancel`, { method: 'POST', body: {} });
      const usage = await request(`${endpoint(project)}/${runId}/usage`).catch(() => null);
      if (current(generation) && state.runId === runId) {
        state.usage = usage?.total ? usage : null;
        clearTimeout(state.timer);
        state.selection += 1;
        renderRun(run);
      }
    } catch (error) {
      if (current(generation)) message('task-run-status', error.message, true);
    } finally {
      state.cancellations.delete(key);
      if (current(generation)) updateControls();
    }
  }

  async function retryFollowup() {
    const project = state.project;
    const pending = state.uncertain.get(project);
    if (!pending?.runId || state.submissions.has(project)) return;
    state.submissions.add(project);
    updateControls();
    try {
      // Reuse the exact message ID/body. The API returns the accepted turn without replaying inference.
      const run = await request(pending.url, { method: 'POST', body: pending.body });
      if (run?.id !== pending.runId || run.store_id !== project || !Array.isArray(run.tasks)) throw new Error('The server did not confirm this follow-up. Refresh task status.');
      rememberAccepted(project, run, pending.body.goal);
      if (state.project === project) {
        state.runs = [run, ...state.runs.filter(item => item.id !== run.id)];
        if (state.runId === run.id) {
          if (el('task-goal').value.trim() === pending.body.goal) el('task-goal').value = '';
          el('task-cost-consent').checked = false;
          await selectRun(run.id);
        } else el('ptab-tasks-badge').textContent = state.runId ? '1' : '0';
        message('task-submit-status', 'Follow-up confirmed. The same message cannot start duplicate work.');
      }
    } catch (error) {
      if (error.status === 409 || error.status === 422) {
        state.uncertain.delete(project);
        persistProject(project);
        if (state.project === project) await loadWorkspace();
      }
      if (state.project === project) message('task-submit-status', error.message, true);
    } finally {
      state.submissions.delete(project);
      if (state.project === project) updateControls();
    }
  }

  function focusGoal() {
    el('task-goal').scrollIntoView({ block: 'center', behavior: 'auto' });
    el('task-goal').focus();
  }

  function init() {
    state.initialized = true;
    el('task-run-form').addEventListener('submit', submit);
    el('task-run-form').addEventListener('invalid', event => {
      if (el('task-advanced').contains(event.target)) el('task-advanced').open = true;
    }, true);
    el('task-load-models').addEventListener('click', loadModels);
    el('task-direct-load').addEventListener('click', () => { if (el('task-provider').value === 'auto') el('task-advanced').open = true; loadModels(); });
    el('task-mode').addEventListener('change', () => { updateControls(); el('task-cost-consent').checked = false; });
    el('task-run-form').addEventListener('input', captureDraft);
    el('task-run-form').addEventListener('change', captureDraft);
    el('task-goal').addEventListener('keydown', event => {
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter' && !event.isComposing && !el('task-submit').disabled) {
        event.preventDefault();
        el('task-run-form').requestSubmit();
      }
    });
    el('task-details-close').addEventListener('click', () => el('task-details-panel').close());
    el('task-details-open').addEventListener('click', () => el('task-details-panel').showModal());
    el('task-cancel').addEventListener('click', () => cancelRun(state.runId));
    el('task-details-panel').addEventListener('close', () => {
      if (state.project && state.selectedRun) el('task-details-open').focus({ preventScroll: true });
    });
    el('task-provider').addEventListener('change', () => { resetModels(); updateControls(); updateProviderNotice(); el('task-cost-consent').checked = false; });
    el('task-refresh').addEventListener('click', () => refresh());
    el('task-saved-run').addEventListener('change', async () => {
      if (!el('task-saved-run').value || el('task-saved-run').value === state.runId) return;
      // Unsent text belongs to the current task. Avoid silently sending it to another one.
      if (el('task-goal').value.trim()) {
        el('task-saved-run').value = state.runId;
        message('task-submit-status', 'Send or clear your unsent message before switching tasks.', true);
        return;
      }
      el('task-cost-consent').checked = false;
      await selectRun(el('task-saved-run').value);
      captureDraft();
      renderRunChoices();
    });
    el('task-more-runs').addEventListener('click', loadOlderRuns);
    el('task-price-model').addEventListener('change', fillPrice);
    el('task-pricing-form').addEventListener('submit', savePrice);
    el('task-reconcile').addEventListener('click', loadWorkspace);
    el('task-allow-retry').addEventListener('click', () => {
      if (state.uncertain.get(state.project)?.runId) { retryFollowup(); return; }
      if (!confirm('The original task may still start and incur charges. Allow another submission anyway?')) return;
      state.uncertain.delete(state.project);
      persistProject(state.project);
      el('task-cost-consent').checked = false;
      message('task-submit-status', 'Another submission is allowed. Review the goal and acknowledge charges again before starting.');
      updateControls();
    });
    el('task-provider-settings').addEventListener('click', event => { event.preventDefault(); navigate('/providers'); });
    window.addEventListener('pagehide', hide);
  }

  return { show, hide, refresh, focusGoal };
})();
