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
  };
  const state = { status: null, onboarding: false, busy: false, tested: false, revision: 0 };
  const element = id => document.getElementById(id);

  async function request(path = '', method = 'GET', body) {
    let response;
    try {
      response = await fetch(`/api/providers${path}`, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: 'no-store',
        signal: AbortSignal.timeout(45000),
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

  function setBusy(busy) {
    state.busy = busy;
    element('provider-form').setAttribute('aria-busy', String(busy));
    element('provider-form').querySelectorAll('button, input, select').forEach(control => {
      control.disabled = busy;
    });
    if (!busy) element('provider-storage').disabled = !element('provider-key').value;
  }

  function invalidateTest() {
    state.tested = false;
    state.revision += 1;
    element('provider-models').replaceChildren();
    message('provider-test-status', '');
    message('provider-form-status', '');
    element('provider-storage').disabled = !element('provider-key').value;
  }

  function resetForm() {
    state.revision += 1;
    state.tested = false;
    const provider = selectedProvider();
    const fromEnvironment = provider?.source === 'environment';
    element('provider-key').value = '';
    element('provider-key-field').hidden = fromEnvironment;
    element('provider-storage-field').hidden = fromEnvironment;
    element('provider-key-link').href = keyPages[element('provider-select').value];
    element('provider-model').value = provider?.default_model || '';
    element('provider-models').replaceChildren();
    element('provider-default').checked = !state.status.default_provider || state.status.default_provider === provider?.id;
    const keyringAvailable = state.status.keyring_available;
    element('provider-storage').querySelector('[value="keyring"]').disabled = !keyringAvailable;
    element('provider-storage').value = keyringAvailable ? 'keyring' : 'session';
    element('provider-storage').disabled = true;
    message('provider-source', provider?.configured
      ? `Configured through ${sourceLabels[provider.source]}. ${fromEnvironment ? 'Unset the environment variable and restart the API server to replace this key.' : 'Stored keys are never displayed.'}`
      : 'No credential is configured for this provider.');
    message('provider-storage-help', keyringAvailable
      ? 'Storage applies only when you enter a new key. Server-memory keys are shared by all local clients.'
      : 'A secure OS credential store is unavailable or locked. Use server memory or an environment variable. No plaintext fallback is used.');
    message('provider-test-status', '');
    message('provider-form-status', '');
  }

  function close() {
    state.revision += 1;
    element('provider-key').value = '';
    state.tested = false;
    element('provider-dialog').close();
    setBusy(false);
  }

  async function dismiss() {
    if (state.busy) return;
    if (state.onboarding) {
      setBusy(true);
      try {
        await request('/onboarding', 'POST', { dismissed: true });
        state.status.onboarding_dismissed = true;
      } catch (error) {
        setBusy(false);
        message('provider-form-status', error.message, true);
        return;
      }
    }
    close();
  }

  function open(providerId = 'openai', onboarding = false) {
    if (!state.status || state.busy) return;
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
      source.textContent = provider.configured
        ? `Configured: ${sourceLabels[provider.source]}. Connection has not been tested in this view.`
        : 'Not configured. Add a key or set the provider environment variable.';
      const model = document.createElement('p');
      model.textContent = `Default model: ${provider.default_model || 'Not selected'}`;
      const actions = document.createElement('div');
      actions.className = 'provider-actions';
      const configure = document.createElement('button');
      configure.className = 'btn btn-primary btn-sm';
      configure.textContent = provider.configured ? 'Manage / test' : 'Connect provider';
      configure.addEventListener('click', () => open(provider.id));
      actions.append(configure);
      if (provider.configured || provider.default_model) {
        const remove = document.createElement('button');
        remove.className = 'btn btn-danger btn-sm';
        remove.textContent = provider.source === 'environment' ? 'Clear saved preferences' : 'Remove configuration';
        remove.addEventListener('click', () => removeProvider(provider, remove));
        actions.append(remove);
      }
      card.append(title, source, model, actions);
      list.append(card);
    }
  }

  async function refresh() {
    element('providers-refresh').disabled = true;
    try {
      state.status = await request();
      renderCards();
      message('providers-status', 'Provider status loaded. Keys detected here are not automatically tested or sent to a provider.');
      return state.status;
    } catch (error) {
      message('providers-status', error.message, true);
      return null;
    } finally {
      element('providers-refresh').disabled = false;
    }
  }

  async function removeProvider(provider, button) {
    const warning = provider.source === 'environment'
      ? 'Clear saved preferences and app-managed keys? The environment credential will remain active until you unset it and restart the API server.'
      : `Remove app-managed credentials and model preferences for ${provider.name}?`;
    if (!confirm(warning)) return;
    button.disabled = true;
    try {
      const result = await request(`/${provider.id}`, 'DELETE');
      await refresh();
      message('providers-status', result.message);
    } catch (error) {
      message('providers-status', error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  async function testConnection() {
    if (state.busy) return;
    const provider = selectedProvider();
    const key = element('provider-key').value.trim();
    if (!key && !provider.configured) {
      message('provider-test-status', 'Enter an API key first.', true);
      return;
    }
    const revision = state.revision;
    state.tested = false;
    setBusy(true);
    message('provider-test-status', 'Testing authentication and loading model identifiers…');
    try {
      const result = await request(`/${provider.id}/test`, 'POST', key ? { api_key: key } : {});
      if (revision !== state.revision) return;
      state.tested = true;
      const options = result.models.map(model => {
        const option = document.createElement('option');
        option.value = model.id;
        option.label = model.name;
        return option;
      });
      element('provider-models').replaceChildren(...options);
      message('provider-test-status', `${result.message} ${options.length} model identifiers loaded.`);
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
    if (!key && !provider.configured) {
      message('provider-form-status', 'Enter and test an API key first.', true);
      return;
    }
    if (key && !state.tested) {
      message('provider-form-status', 'Test this new key before saving it.', true);
      return;
    }
    if (!/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(model)) {
      message('provider-form-status', 'Choose or enter a valid model identifier.', true);
      return;
    }
    const revision = state.revision;
    setBusy(true);
    message('provider-form-status', 'Saving provider configuration…');
    try {
      const result = await request(`/${provider.id}`, 'PUT', {
        ...(key ? { api_key: key } : {}),
        storage: element('provider-storage').value,
        model,
        make_default: element('provider-default').checked,
      });
      if (revision !== state.revision) return;
      close();
      await refresh();
      message('providers-status', result.message);
      if (typeof toast === 'function') toast('Provider settings saved.');
    } catch (error) {
      if (revision === state.revision) {
        message('provider-form-status', error.message, true);
        setBusy(false);
      }
    }
  }

  async function init() {
    element('providers-refresh').addEventListener('click', refresh);
    element('provider-select').addEventListener('change', resetForm);
    element('provider-key').addEventListener('input', invalidateTest);
    element('provider-test').addEventListener('click', testConnection);
    element('provider-form').addEventListener('submit', save);
    element('provider-close').addEventListener('click', dismiss);
    element('provider-skip').addEventListener('click', dismiss);
    element('provider-dialog').addEventListener('cancel', event => {
      event.preventDefault();
      dismiss();
    });
    element('provider-dialog').addEventListener('close', () => { element('provider-key').value = ''; });
    window.addEventListener('pagehide', close);
    const status = await refresh();
    if (status && !status.providers.some(provider => provider.configured) && !status.onboarding_dismissed && location.pathname !== '/providers') {
      open('openai', true);
    }
  }

  return { init, refresh, close };
})();
