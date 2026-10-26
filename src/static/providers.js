/* Provider onboarding is independent of project content and agent execution. */
window.ProvidersUI = (() => {
  const keyPages = {
    openai: 'https://platform.openai.com/api-keys',
    anthropic: 'https://console.anthropic.com/settings/keys',
  };
  const sourceLabels = {
    environment: 'Environment variable',
    keyring: 'OS credential store',
    session: 'Server memory (until restart or reload)',
    none: 'No API key (keyless endpoint)',
  };
  const state = {
    status: null, onboarding: false, busy: false, tested: false, revision: 0,
    controller: null, refreshRevision: 0, routingDirty: false, routingBusy: false,
    routing: { mode: 'local_only', providers: [] },
  };
  const element = id => document.getElementById(id);
  const isBuiltin = provider => provider && ['openai', 'anthropic'].includes(provider.id);
  const kindOf = provider => provider?.kind || (isBuiltin(provider) ? provider.id : 'openai_compatible');
  const isProfile = () => !isBuiltin(selectedProvider());

  async function request(path = '', method = 'GET', body, signal) {
    let response;
    try {
      response = await fetch(`/api/providers${path}`, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: 'no-store',
        signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(45000)]) : AbortSignal.timeout(45000),
      });
    } catch {
      throw new Error('Provider settings could not be reached. Check the server and try again.');
    }
    let data;
    try { data = await response.json(); } catch {
      throw new Error('The server returned an invalid provider response.');
    }
    if (!response.ok) {
      throw new Error(typeof data.detail === 'string' ? data.detail : 'Provider request failed.');
    }
    return data;
  }

  function message(id, text, error = false) {
    const node = element(id);
    node.textContent = text;
    node.classList.toggle('provider-error', error);
  }

  function selectedProvider() {
    return state.status?.providers.find(provider => provider.id === element('provider-select').value);
  }

  function renderSelect() {
    const select = element('provider-select');
    const previous = select.value;
    const choices = state.status.providers.map(provider => [provider.id, provider.name]);
    choices.push(['new_ollama', 'Add Ollama (no API key needed)'], ['new_compatible', 'Add OpenAI-compatible endpoint']);
    select.replaceChildren(...choices.map(([value, name]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = name;
      return option;
    }));
    select.value = choices.some(([value]) => value === previous) ? previous : choices[0][0];
    // A profile removed by another client must not leave its credential in a different provider's form.
    if (select.value !== previous && element('provider-dialog').open) resetForm();
  }

  function syncControls() {
    element('provider-storage').disabled = state.busy || !element('provider-key').value.trim();
    element('provider-storage').querySelector('[value="keyring"]').disabled = !state.status?.keyring_available;
    element('provider-requires-key').disabled = state.busy || element('provider-clear-key').checked;
  }

  function setBusy(busy) {
    state.busy = busy;
    element('provider-form').setAttribute('aria-busy', String(busy));
    element('provider-form').querySelectorAll('button, input, select').forEach(control => {
      control.disabled = busy;
    });
    syncControls();
  }

  function invalidateTest() {
    state.tested = false;
    state.revision += 1;
    element('provider-models').replaceChildren();
    message('provider-test-status', '');
    message('provider-form-status', '');
    if (element('provider-clear-key').checked) element('provider-requires-key').checked = false;
    syncControls();
  }

  function resetForm() {
    state.revision += 1;
    state.controller?.abort();
    state.controller = null;
    state.tested = false;
    const provider = selectedProvider();
    const custom = isProfile();
    const fromEnvironment = provider?.source === 'environment';
    element('provider-key').value = '';
    element('provider-key-field').hidden = fromEnvironment;
    element('provider-storage-field').hidden = fromEnvironment;
    const keyLink = element('provider-key-link');
    keyLink.hidden = custom;
    if (custom) keyLink.removeAttribute('href');
    else keyLink.href = keyPages[provider.id];
    element('provider-profile-fields').hidden = !custom;
    element('provider-profile-name').value = provider?.name || (element('provider-select').value === 'new_ollama' ? 'My Ollama' : 'Compatible endpoint');
    element('provider-profile-name').required = custom;
    element('provider-base-url').value = provider?.base_url || (element('provider-select').value === 'new_ollama' ? 'http://localhost:11434' : 'http://localhost:1234/v1');
    element('provider-base-url').readOnly = custom && !!provider;
    element('provider-base-url').required = custom;
    element('provider-requires-key').checked = provider?.requires_key ?? false;
    element('provider-clear-key-field').hidden = !custom || !provider;
    element('provider-clear-key').checked = false;
    element('provider-context-tokens').value = provider?.context_tokens ?? 8192;
    element('provider-allowed-models').value = (provider?.allowed_models || []).join('\n');
    element('provider-output-tokens').max = '1999999';
    element('provider-output-tokens').value = provider?.output_tokens ?? 2048;
    element('provider-model').required = false;
    element('provider-model').value = provider?.default_model || '';
    element('provider-models').replaceChildren();
    element('provider-default').checked = custom
      ? !!provider && state.status.default_provider === provider.id
      : !state.status.default_provider || state.status.default_provider === provider?.id;
    const keyringAvailable = state.status.keyring_available;
    element('provider-storage').querySelector('[value="keyring"]').disabled = !keyringAvailable;
    element('provider-storage').value = keyringAvailable ? 'keyring' : 'session';
    element('provider-storage').disabled = true;
    message('provider-source', provider?.requires_key && (!provider.configured || provider.source === 'none')
      ? 'The required API key is unavailable (server-memory keys are lost on restart). Enter and test a replacement, or explicitly clear the saved key to use this endpoint without authentication.'
      : provider?.configured
        ? `Configured: ${sourceLabels[provider.source] || 'Existing credential'}. ${fromEnvironment ? 'Unset the environment variable and restart the API server to replace this key.' : 'Stored keys are never displayed.'}`
        : custom ? 'Test this endpoint before saving. An API key and default model are optional for keyless endpoints.' : 'No credential is configured for this provider.');
    message('provider-storage-help', (custom ? 'Keyless endpoints need no credential storage. ' : '') + (keyringAvailable
      ? 'Storage applies only when you enter a new key. Server-memory keys are shared by all local clients.'
      : 'A secure OS credential store is unavailable or locked. Use server memory or an environment variable for new keys. No plaintext fallback is used.'));
    message('provider-test-status', '');
    message('provider-form-status', '');
    setBusy(false);
  }

  function close() {
    state.revision += 1;
    state.controller?.abort();
    state.controller = null;
    element('provider-key').value = '';
    state.tested = false;
    element('provider-dialog').close();
    setBusy(false);
  }

  async function dismiss() {
    if (state.busy) return;
    if (state.onboarding) {
      const revision = state.revision;
      state.controller = new AbortController();
      setBusy(true);
      try {
        await request('/onboarding', 'POST', { dismissed: true }, state.controller.signal);
        if (revision !== state.revision) return;
        state.status.onboarding_dismissed = true;
      } catch (error) {
        if (revision !== state.revision) return;
        setBusy(false);
        message('provider-form-status', error.message, true);
        return;
      }
    }
    close();
  }

  function open(providerId = 'openai', onboarding = false) {
    if (!state.status || state.busy) return;
    if (!['new_ollama', 'new_compatible'].includes(providerId) && !state.status.providers.some(provider => provider.id === providerId)) return;
    renderSelect();
    state.onboarding = onboarding;
    element('provider-select').value = providerId;
    element('provider-dialog-title').textContent = onboarding ? 'Set up your AI provider' : 'Provider settings';
    element('provider-skip').textContent = onboarding ? 'Set up later' : 'Cancel';
    resetForm();
    element('provider-dialog').showModal();
    element('provider-select').focus();
  }

  function renderCards() {
    const list = element('providers-list');
    list.replaceChildren();
    for (const provider of state.status.providers) {
      const card = document.createElement('section');
      card.className = 'setup-card provider-card';
      const title = document.createElement('h2');
      title.className = 'setup-card-title';
      title.textContent = `${provider.name}${state.status.default_provider === provider.id ? ' — Default' : ''}`;
      const source = document.createElement('p');
      source.textContent = provider.requires_key && provider.source === 'none'
        ? 'Required API key unavailable. Connect to enter and test a replacement, or explicitly clear it for keyless access.'
        : provider.configured
        ? `Configured: ${sourceLabels[provider.source] || 'Existing credential'}. Connection has not been tested in this view.`
        : isBuiltin(provider) ? 'Not configured. Add a key or set the provider environment variable.' : 'Not configured. A required API key is unavailable; connect to replace it.';
      const details = document.createElement('p');
      details.textContent = `Kind: ${kindOf(provider)} · Base URL: ${provider.base_url || 'Built-in endpoint'}${isBuiltin(provider) ? '' : ` · Context: ${provider.context_tokens ?? 8192} tokens · Output: ${provider.output_tokens ?? 2048} tokens`} · ${(provider.requires_key ?? isBuiltin(provider)) ? 'API key required' : 'Keyless access allowed'}`;
      const routing = document.createElement('p');
      routing.textContent = routingDescription(provider);
      const model = document.createElement('p');
      model.textContent = `Default model: ${provider.default_model || 'Automatic — chosen when you start a task'}${isBuiltin(provider) ? '' : ` · Approved models: ${provider.allowed_models?.length ? provider.allowed_models.join(', ') : 'All compatible discovered models'}`}`;
      const actions = document.createElement('div');
      actions.className = 'provider-actions';
      const configure = document.createElement('button');
      configure.className = 'btn btn-primary btn-sm';
      configure.textContent = provider.configured ? 'Manage / test' : 'Connect provider';
      configure.addEventListener('click', () => open(provider.id));
      actions.append(configure);
      if (!isBuiltin(provider) || provider.configured || provider.default_model) {
        const remove = document.createElement('button');
        remove.className = 'btn btn-danger btn-sm';
        remove.textContent = provider.source === 'environment' ? 'Clear saved preferences' : 'Remove configuration';
        remove.addEventListener('click', () => removeProvider(provider, remove));
        actions.append(remove);
      }
      card.append(title, source, details, model, routing, actions);
      list.append(card);
    }
  }

  async function refresh(resetRouting = false) {
    const revision = ++state.refreshRevision;
    element('providers-refresh').disabled = true;
    try {
      const status = await request();
      if (revision !== state.refreshRevision) return null;
      state.status = status;
      state.routing = status.routing || { mode: 'local_only', providers: [] };
      if (resetRouting) state.routingDirty = false;
      renderSelect();
      renderCards();
      renderRouting();
      message('providers-status', 'Provider status loaded. Keys detected here are not automatically tested or sent to a provider.');
      return state.status;
    } catch (error) {
      if (revision === state.refreshRevision) message('providers-status', error.message, true);
      return null;
    } finally {
      if (revision === state.refreshRevision) element('providers-refresh').disabled = false;
    }
  }

  async function removeProvider(provider, button) {
    const warning = provider.source === 'environment'
      ? 'Clear saved preferences and app-managed keys? The environment credential will remain active until you unset it and restart the API server.'
      : `Remove app-managed credentials and model preferences for ${provider.name}?`;
    if (!confirm(warning)) return;
    button.disabled = true;
    try {
      const result = await request(`/${encodeURIComponent(provider.id)}`, 'DELETE');
      await refresh();
      message('providers-status', result.message);
    } catch (error) {
      message('providers-status', error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  function profileBody() {
    const provider = selectedProvider();
    const key = element('provider-key').value.trim();
    return {
      name: element('provider-profile-name').value.trim(),
      kind: provider ? kindOf(provider) : element('provider-select').value === 'new_ollama' ? 'ollama' : 'openai_compatible',
      base_url: provider?.base_url || element('provider-base-url').value.trim(),
      ...(key ? { api_key: key } : {}),
      storage: key ? element('provider-storage').value : 'session',
      model: element('provider-model').value.trim(),
      make_default: element('provider-default').checked,
      context_tokens: Number(element('provider-context-tokens').value),
      output_tokens: Number(element('provider-output-tokens').value),
      allowed_models: element('provider-allowed-models').value.split('\n').map(value => value.trim()).filter(Boolean),
      requires_key: element('provider-clear-key').checked ? false : element('provider-requires-key').checked,
    };
  }

  function validateConnection() {
    const provider = selectedProvider();
    const key = element('provider-key').value.trim();
    if (!isProfile()) return !key && !provider.configured ? 'Enter and test an API key first.' : '';
    const body = profileBody();
    const clear = element('provider-clear-key').checked;
    if (clear && key) return 'Choose either a replacement API key or clearing the saved key, not both.';
    if (!body.name || !body.base_url) return 'Enter a connection name and API base URL.';
    if (body.allowed_models.length > 50 || new Set(body.allowed_models).size !== body.allowed_models.length
      || body.allowed_models.some(value => !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(value))) {
      return 'Enter up to 50 unique valid model IDs, one per line.';
    }
    if (!Number.isInteger(body.context_tokens) || body.context_tokens < 2 || body.context_tokens > 2000000
      || !Number.isInteger(body.output_tokens) || body.output_tokens < 1 || body.output_tokens >= body.context_tokens) {
      return 'Use whole-number token budgets, with output smaller than context (maximum context: 2000000).';
    }
    const missingKey = !provider?.configured || provider.source === 'none';
    if (!key && !clear && (body.requires_key || provider?.requires_key) && missingKey) {
      return 'Enter and test a replacement API key, or explicitly clear the saved key for keyless access.';
    }
    return '';
  }

  async function testConnection() {
    if (state.busy) return;
    const provider = selectedProvider();
    const key = element('provider-key').value.trim();
    const error = validateConnection();
    if (error) {
      message('provider-test-status', error, true);
      return;
    }
    const revision = state.revision;
    state.tested = false;
    state.controller = new AbortController();
    setBusy(true);
    message('provider-test-status', 'Testing authentication and loading model identifiers…');
    try {
      const unsaved = isProfile() && (!provider || element('provider-clear-key').checked);
      const result = await request(unsaved ? '/profiles/test' : `/${encodeURIComponent(provider.id)}/test`, 'POST',
        unsaved ? profileBody() : key ? { api_key: key } : {}, state.controller.signal);
      if (revision !== state.revision) return;
      const options = result.models.map(model => {
        const option = document.createElement('option');
        option.value = model.id;
        option.label = model.name;
        return option;
      });
      element('provider-models').replaceChildren(...options);
      state.tested = true;
      message('provider-test-status', `${result.message || 'Connection test succeeded.'} ${options.length} model identifiers loaded.`);
    } catch (error) {
      if (revision === state.revision) message('provider-test-status', error.message, true);
    } finally {
      if (revision === state.revision) setBusy(false);
    }
  }

  async function save(event) {
    event.preventDefault();
    if (state.busy) return;
    const provider = selectedProvider();
    const key = element('provider-key').value.trim();
    const model = element('provider-model').value.trim();
    const custom = isProfile();
    const clear = custom && element('provider-clear-key').checked;
    const error = validateConnection();
    if (error) {
      message('provider-form-status', error, true);
      return;
    }
    if ((key || custom && (!provider || clear)) && !state.tested) {
      // One submit connects a new provider. A failed or cancelled test never saves.
      const revision = state.revision;
      await testConnection();
      if (revision !== state.revision || !state.tested) return;
    }
    if (model && !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(model)) {
      message('provider-form-status', 'Choose or enter a valid model identifier.', true);
      return;
    }
    const revision = state.revision;
    const onboardingProfile = custom && !provider && state.onboarding;
    state.controller = new AbortController();
    setBusy(true);
    message('provider-form-status', 'Saving provider configuration…');
    try {
      const body = custom ? { ...profileBody(), ...(provider ? { clear_api_key: clear } : {}) } : {
        ...(key ? { api_key: key } : {}),
        storage: element('provider-storage').value,
        model,
        make_default: element('provider-default').checked,
      };
      const path = custom ? provider ? `/profiles/${encodeURIComponent(provider.id)}` : '/profiles' : `/${provider.id}`;
      const result = await request(path, custom && !provider ? 'POST' : 'PUT', body, state.controller.signal);
      if (revision !== state.revision) return;
      close();
      const closedRevision = state.revision;
      await refresh();
      if (closedRevision !== state.revision) return;
      message('providers-status', result.message);
      if (typeof toast === 'function') toast('Connected. Open a project → Tasks to send your first message.');
      if (onboardingProfile && typeof navigate === 'function') navigate('/');
    } catch (error) {
      if (revision === state.revision) {
        message('provider-form-status', error.message, true);
        setBusy(false);
      }
    }
  }

  function routingDescription(provider) {
    if (!state.routing.providers.includes(provider.id)) return 'Automatic routing: not allowed.';
    if (state.routing.mode === 'local_only' && provider.is_local !== true) return 'Automatic routing: selected, but blocked by Local only mode.';
    if (!provider.configured) return 'Automatic routing: allowed, but unavailable until configured.';
    return 'Automatic routing: allowed by your saved usage preferences.';
  }

  function routingSelection() {
    return {
      mode: element('provider-routing-mode').value,
      providers: [...element('provider-routing-options').querySelectorAll('input:checked')].map(input => input.value)
        .filter(id => state.status.providers.some(provider => provider.id === id)),
    };
  }

  function setRoutingBusy(busy) {
    state.routingBusy = busy;
    element('provider-routing-form').setAttribute('aria-busy', String(busy));
    element('provider-routing-mode').disabled = busy || !state.status;
    element('provider-routing-options').disabled = busy || !state.status;
    element('provider-routing-save').disabled = busy || !state.status;
  }

  function renderRouting() {
    const selection = state.routingDirty ? routingSelection() : state.routing;
    element('provider-routing-mode').value = selection.mode;
    const legend = document.createElement('legend');
    legend.textContent = 'Providers the automatic workflow may use';
    const options = state.status.providers.map(provider => {
      const label = document.createElement('label');
      label.className = 'provider-default';
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.value = provider.id;
      checkbox.checked = selection.providers.includes(provider.id);
      checkbox.disabled = !provider.configured && !state.routing.providers.includes(provider.id);
      const text = document.createElement('span');
      text.textContent = `${provider.name} — ${provider.is_local === true ? 'Local' : 'Remote; may incur charges'}${provider.configured ? '' : ' (unavailable)'} `;
      label.append(checkbox, text);
      return label;
    });
    element('provider-routing-options').replaceChildren(legend, ...options);
    setRoutingBusy(state.routingBusy);
    if (!state.routingDirty && !state.routingBusy) {
      message('provider-routing-status', 'Saved permissions shown. Routing preferences are configuration, not a price or model-quality guarantee.');
    }
  }

  async function saveRouting(event) {
    event.preventDefault();
    if (!state.status || state.routingBusy) return;
    const selection = routingSelection();
    const remote = selection.providers.filter(id => state.status.providers.some(provider => provider.id === id && provider.is_local !== true));
    if (selection.mode !== 'local_only' && remote.length && (state.routing.mode !== selection.mode
      || remote.some(id => !state.routing.providers.includes(id)))) {
      if (!confirm('Allow automatic routing to the selected remote providers? They may receive your goal and generated context and may charge for usage. These preferences do not guarantee free usage or a particular price.')) {
        message('provider-routing-status', 'Remote authorization was cancelled. Your changes have not been saved.');
        return;
      }
    }
    setRoutingBusy(true);
    message('provider-routing-status', 'Saving usage preferences…');
    try {
      const result = await request('/routing', 'PUT', selection);
      state.routing = result;
      state.status.routing = result;
      state.routingDirty = false;
      await refresh();
      message('provider-routing-status', 'Usage preferences saved. Only selected providers are allowed; Local only mode blocks remote providers. This is not a price guarantee.');
    } catch (error) {
      message('provider-routing-status', error.message, true);
    } finally {
      setRoutingBusy(false);
    }
  }

  async function init() {
    setRoutingBusy(false);
    element('providers-refresh').addEventListener('click', () => refresh(true));
    element('provider-add-ollama').addEventListener('click', () => open('new_ollama'));
    element('provider-add-compatible').addEventListener('click', () => open('new_compatible'));
    element('provider-routing-form').addEventListener('submit', saveRouting);
    element('provider-routing-form').addEventListener('change', () => {
      state.routingDirty = true;
      message('provider-routing-status', 'Unsaved usage preferences. Remote providers may receive task context and charge for usage.');
    });
    element('provider-select').addEventListener('change', resetForm);
    element('provider-key').addEventListener('input', invalidateTest);
    for (const id of ['provider-profile-name', 'provider-base-url', 'provider-context-tokens', 'provider-output-tokens']) {
      element(id).addEventListener('input', invalidateTest);
    }
    for (const id of ['provider-requires-key', 'provider-clear-key']) {
      element(id).addEventListener('change', () => { if (isProfile()) invalidateTest(); });
    }
    element('provider-test').addEventListener('click', testConnection);
    element('provider-form').addEventListener('submit', save);
    element('provider-close').addEventListener('click', dismiss);
    element('provider-skip').addEventListener('click', dismiss);
    element('provider-dialog').addEventListener('cancel', event => {
      event.preventDefault();
      dismiss();
    });
    element('provider-dialog').addEventListener('close', () => {
      if (!element('provider-dialog').open && (state.controller || state.busy || state.tested || element('provider-key').value)) {
        state.revision += 1;
        state.controller?.abort();
        state.controller = null;
        element('provider-key').value = '';
        state.tested = false;
        setBusy(false);
      }
    });
    window.addEventListener('pagehide', close);
    const status = await refresh();
    if (status && !status.providers.some(provider => provider.configured) && !status.onboarding_dismissed && location.pathname !== '/providers') {
      open('openai', true);
    }
  }

  return { init, refresh, close };
})();
