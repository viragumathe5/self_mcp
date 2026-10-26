/* Mock-only browser checks. Run with PLAYWRIGHT_MODULE and CHROME_PATH if needed. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

function initialStatus() {
  return {
    providers: [
      { id: 'openai', name: 'OpenAI', configured: false, source: null, default_model: null },
      { id: 'anthropic', name: 'Anthropic', configured: false, source: null, default_model: null },
    ],
    keyring_available: false, default_provider: null, onboarding_dismissed: false,
  };
}

function profile(id, extra = {}) {
  return {
    id, name: id, kind: 'ollama', base_url: 'http://localhost:11434',
    configured: true, source: 'none', default_model: '', is_local: true,
    requires_key: false, context_tokens: 32768, output_tokens: 4096,
    ...extra,
  };
}

async function fixture(browser, base, status = initialStatus(), pathname = '/providers') {
  const context = await browser.newContext();
  const page = await context.newPage();
  const errors = [];
  const calls = [];
  let hold;
  let failure;
  page.on('pageerror', error => errors.push(error.message));
  await page.route('https://**', route => route.abort());
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const body = request.postDataJSON();
    const call = { path: url.pathname, method, body };
    calls.push(call);
    if (hold && hold.path === call.path && hold.method === method) {
      const pending = hold;
      hold = null;
      pending.entered();
      await pending.wait;
    }
    let result = {};
    let code = 200;
    if (failure && failure.path === call.path && failure.method === method) {
      result = { detail: failure.message };
      code = 422;
      failure = null;
    } else if (call.path === '/api/providers') result = status;
    else if (call.path === '/api/providers/routing') {
      if (method === 'PUT') status.routing = body;
      result = status.routing || { mode: 'local_only', providers: [] };
    } else if (call.path === '/api/providers/onboarding') {
      status.onboarding_dismissed = body.dismissed;
    } else if (call.path.endsWith('/test')) {
      if (body.api_key === 'bad-fixture-key') {
        code = 401;
        result = { detail: 'Provider authentication failed.' };
      } else result = { models: [{ id: 'discovered-model', name: '<img src=x onerror=alert(1)>' }], message: 'Discovery succeeded; inference not verified.' };
    } else if (call.path === '/api/providers/profiles' && method === 'POST') {
      const id = `profile_${status.providers.length}`;
      status.providers.push(profile(id, {
        name: body.name, kind: body.kind, base_url: body.base_url,
        context_tokens: body.context_tokens, output_tokens: body.output_tokens,
        requires_key: body.requires_key || !!body.api_key, source: body.api_key ? body.storage : 'none',
        default_model: body.model, is_local: new URL(body.base_url).hostname === 'localhost',
        allowed_models: body.allowed_models,
      }));
      if (body.make_default) status.default_provider = id;
      result = { id, message: 'Profile saved <img src=x onerror=alert(1)>.' };
    } else if (call.path.startsWith('/api/providers/profiles/') && method === 'PUT') {
      const saved = status.providers.find(item => call.path.endsWith(`/${item.id}`));
      Object.assign(saved, {
        name: body.name, default_model: body.model, context_tokens: body.context_tokens,
        output_tokens: body.output_tokens, configured: true,
        allowed_models: body.allowed_models,
        requires_key: body.clear_api_key ? false : saved.requires_key || body.requires_key || !!body.api_key,
        source: body.clear_api_key ? 'none' : body.api_key ? body.storage : saved.source,
      });
      if (body.make_default) status.default_provider = saved.id;
      result = { id: saved.id, message: 'Profile updated.' };
    } else if (call.path.startsWith('/api/providers/') && method === 'DELETE') {
      status.providers = status.providers.filter(item => !call.path.endsWith(`/${item.id}`));
      result = { message: 'Profile removed.' };
    } else result = [];
    try { await route.fulfill({ status: code, json: result }); } catch { /* Browser may have cancelled the request. */ }
  });
  await page.goto(`${base}${pathname}`);
  await page.waitForFunction(() => document.querySelector('#providers-list').children.length >= 2);
  return {
    page, status, calls,
    hold(path, method = 'POST') {
      let release, entered;
      const wait = new Promise(resolve => { release = resolve; });
      const started = new Promise(resolve => { entered = resolve; });
      hold = { path, method, wait, entered };
      return { release, started };
    },
    fail(path, method, message) { failure = { path, method, message }; },
    async close() {
      assert.deepEqual(errors, [], 'No browser boot or handler errors');
      await context.close();
    },
  };
}

async function tested(page) {
  await page.locator('#provider-test').click();
  await page.waitForFunction(() => document.querySelector('#provider-test-status').textContent.includes('model identifiers loaded'));
}

async function saved(page) {
  await page.locator('#provider-save').click();
  await page.waitForFunction(() => !document.querySelector('#provider-dialog').open);
  await page.waitForFunction(() => !document.querySelector('#providers-refresh').disabled);
}

function card(page, name) {
  return page.locator('#providers-list .provider-card').filter({ has: page.getByRole('heading', { name, exact: true }) });
}

async function keylessOnboarding(browser, base) {
  const f = await fixture(browser, base, initialStatus(), '/');
  const { page, calls, status } = f;
  await page.waitForSelector('#provider-dialog[open]');
  assert.equal(await page.locator('#providers-list .provider-card').count(), 2);
  assert.deepEqual(await page.locator('#provider-select option').evaluateAll(nodes => nodes.map(node => node.value)), ['openai', 'anthropic', 'new_ollama', 'new_compatible']);
  await page.locator('#provider-select').selectOption('new_ollama');
  assert.equal(await page.locator('#provider-base-url').inputValue(), 'http://localhost:11434');
  assert.equal(await page.locator('#provider-model').getAttribute('required'), null);
  assert.equal(await page.locator('#provider-key-link').isVisible(), false);
  assert.equal(await page.locator('#provider-default').isChecked(), false);
  await tested(page);
  assert.equal(await page.locator('#provider-model').inputValue(), '', 'Discovery must not guess a model');
  await page.locator('#provider-context-tokens').fill('16384');
  await tested(page);
  const testBody = calls.filter(call => call.path === '/api/providers/profiles/test').at(-1).body;
  assert.deepEqual(testBody, {
    name: 'My Ollama', kind: 'ollama', base_url: 'http://localhost:11434', storage: 'session',
    model: '', make_default: false, context_tokens: 16384, output_tokens: 2048, requires_key: false, allowed_models: [],
  });
  await saved(page);
  await page.waitForURL(`${base}/`);
  await page.locator('#nav-providers').click();
  assert.deepEqual(calls.find(call => call.path === '/api/providers/profiles').body, testBody);
  assert.equal(status.default_provider, null);
  assert.equal(calls.some(call => call.path === '/api/providers/routing' && call.method === 'PUT'), false);
  assert.equal(await page.locator('#provider-routing-options input:checked').count(), 0);
  const localCard = card(page, 'My Ollama');
  assert.match(await localCard.textContent(), /No API key.*ollama.*localhost:11434.*16384.*2048.*not allowed/s);
  assert.equal(await page.locator('#providers-list img, #providers-status img').count(), 0);
  await localCard.getByRole('button', { name: 'Manage / test' }).click();
  assert.equal(await page.locator('#provider-base-url').evaluate(node => node.readOnly), true);
  assert.equal(await page.locator('#provider-select').inputValue(), status.providers[2].id);
  assert.equal(await page.locator('#provider-context-tokens').inputValue(), '16384');
  const testCount = calls.filter(call => call.path.endsWith('/test')).length;
  await page.locator('#provider-profile-name').fill('Renamed local');
  await page.locator('#provider-model').fill('discovered-model');
  await page.locator('#provider-allowed-models').fill('discovered-model\norg/model:free');
  await saved(page);
  assert.equal(calls.filter(call => call.path.endsWith('/test')).length, testCount, 'Existing metadata can be saved without retesting');
  await card(page, 'Renamed local').getByRole('button', { name: 'Manage / test' }).click();
  assert.equal(await page.locator('#provider-allowed-models').inputValue(), 'discovered-model\norg/model:free');
  await tested(page);
  assert.deepEqual(calls.filter(call => call.path.endsWith('/test')).at(-1), { path: `/api/providers/${status.providers[2].id}/test`, method: 'POST', body: {} });
  await page.keyboard.press('Escape');
  page.once('dialog', dialog => dialog.accept());
  await card(page, 'Renamed local').getByRole('button', { name: 'Remove configuration' }).click();
  await page.waitForFunction(() => document.querySelectorAll('#providers-list .provider-card').length === 2);
  await f.close();
}

async function credentialsAndClear(browser, base) {
  const f = await fixture(browser, base);
  const { page, calls, status } = f;
  await page.locator('#provider-add-compatible').click();
  assert.equal(await page.locator('#provider-base-url').inputValue(), 'http://localhost:1234/v1');
  await page.locator('#provider-profile-name').fill('<img src=x onerror=alert(1)>');
  await page.locator('#provider-base-url').fill('https://models.example.test/v1');
  await page.locator('#provider-requires-key').check();
  await page.locator('#provider-save').click();
  assert.match(await page.locator('#provider-form-status').textContent(), /replacement API key/);
  await page.locator('#provider-key').fill('bad-fixture-key');
  await page.locator('#provider-test').click();
  await page.waitForFunction(() => document.querySelector('#provider-test-status').textContent.includes('authentication failed'));
  await page.locator('#provider-save').click();
  await page.waitForFunction(() => !document.querySelector('#provider-test').disabled);
  assert.match(await page.locator('#provider-test-status').textContent(), /authentication failed/);
  assert.equal(calls.some(call => call.path === '/api/providers/profiles'), false);
  await page.locator('#provider-key').fill('fixture-secret');
  await tested(page);
  await page.locator('#provider-base-url').fill('https://other.example.test/v1');
  await saved(page);
  assert.equal(calls.filter(call => call.path.endsWith('/test')).at(-1).body.base_url, 'https://other.example.test/v1', 'Saving must recheck a changed endpoint');
  const remote = status.providers[2];
  assert.equal(remote.kind, 'openai_compatible');
  assert.equal(remote.source, 'session');
  assert.equal(await page.locator('#provider-key').inputValue(), '');
  assert.equal(await page.locator('#providers-list img').count(), 0);
  assert.equal(await page.evaluate(() => JSON.stringify([localStorage, sessionStorage]).includes('fixture-secret')), false);
  await card(page, remote.name).getByRole('button', { name: 'Manage / test' }).click();
  assert.equal(await page.locator('#provider-requires-key').isChecked(), true);
  assert.equal(await page.locator('#provider-key-link').getAttribute('href'), null);
  await page.locator('#provider-key').fill('replacement-fixture');
  await tested(page);
  assert.deepEqual(calls.filter(call => call.path.endsWith('/test')).at(-1).body, { api_key: 'replacement-fixture' });
  await page.locator('#provider-clear-key').check();
  await page.locator('#provider-test').click();
  assert.match(await page.locator('#provider-test-status').textContent(), /not both/);
  await page.locator('#provider-key').fill('');
  assert.equal(await page.locator('#provider-requires-key').isChecked(), false);
  assert.equal(await page.locator('#provider-requires-key').isDisabled(), true);
  await saved(page);
  const clearedTest = calls.filter(call => call.path.endsWith('/test')).at(-1);
  assert.equal(clearedTest.path, '/api/providers/profiles/test');
  assert.equal(clearedTest.body.requires_key, false);
  assert.equal('api_key' in clearedTest.body, false);
  assert.equal('clear_api_key' in clearedTest.body, false);
  const update = calls.filter(call => call.method === 'PUT').at(-1);
  assert.equal(update.body.clear_api_key, true);
  assert.equal(update.body.requires_key, false);
  assert.equal('api_key' in update.body, false);
  // Even an inconsistent/older status saying configured must not silently downgrade a lost session key.
  remote.requires_key = true;
  remote.source = 'none';
  remote.configured = true;
  await page.evaluate(() => window.ProvidersUI.refresh());
  await card(page, remote.name).getByRole('button', { name: 'Manage / test' }).click();
  assert.match(await page.locator('#provider-source').textContent(), /unavailable/);
  await page.locator('#provider-requires-key').uncheck();
  const writes = calls.filter(call => call.method === 'PUT').length;
  await page.locator('#provider-save').click();
  assert.match(await page.locator('#provider-form-status').textContent(), /replacement API key/);
  assert.equal(calls.filter(call => call.method === 'PUT').length, writes);
  await page.locator('#provider-key').fill('clear-on-close');
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#provider-key').inputValue(), '');
  await f.close();
}

async function routing(browser, base) {
  const status = initialStatus();
  status.providers.push(profile('local'), profile('remote', { is_local: false, kind: 'openai_compatible', base_url: 'https://models.example.test/v1' }), profile('lost', { configured: false, requires_key: true }));
  status.routing = { mode: 'local_only', providers: ['lost', 'unknown-id'] };
  const f = await fixture(browser, base, status);
  const { page, calls } = f;
  const option = id => page.locator(`#provider-routing-options input[value="${id}"]`);
  assert.equal(await option('openai').isDisabled(), true);
  assert.equal(await option('lost').isDisabled(), false);
  assert.equal(await option('lost').isChecked(), true);
  assert.equal(await option('unknown-id').count(), 0);
  await option('lost').uncheck();
  await option('local').check();
  await page.evaluate(() => window.ProvidersUI.refresh());
  assert.equal(await option('local').isChecked(), true, 'Background status refresh preserves routing edits');
  assert.equal(await option('lost').isChecked(), false);
  await page.locator('#provider-routing-save').click();
  await page.waitForFunction(() => document.querySelector('#provider-routing-status').textContent.includes('Usage preferences saved'));
  assert.deepEqual(status.routing, { mode: 'local_only', providers: ['local'] });
  assert.equal(await option('lost').isDisabled(), true);
  await option('remote').check();
  await page.locator('#provider-routing-mode').selectOption('balanced');
  const writes = () => calls.filter(call => call.path === '/api/providers/routing' && call.method === 'PUT');
  const before = writes().length;
  page.once('dialog', async dialog => {
    assert.equal(dialog.type(), 'confirm');
    assert.match(dialog.message(), /generated context.*charge.*not guarantee/);
    await dialog.dismiss();
  });
  await page.locator('#provider-routing-save').click();
  assert.equal(writes().length, before);
  const pending = f.hold('/api/providers/routing', 'PUT');
  page.once('dialog', dialog => dialog.accept());
  await page.locator('#provider-routing-save').click();
  await pending.started;
  assert.equal(await page.locator('#provider-routing-mode').isDisabled(), true);
  assert.equal(await option('local').isDisabled(), true);
  assert.equal(await page.locator('#provider-routing-save').isDisabled(), true);
  pending.release();
  await page.waitForFunction(() => document.querySelector('#provider-routing-status').textContent.includes('Usage preferences saved'));
  assert.deepEqual(status.routing, { mode: 'balanced', providers: ['local', 'remote'] });
  assert.match(await card(page, 'remote').textContent(), /allowed by your saved/);
  let confirmations = 0;
  const dismissDialog = dialog => { confirmations++; return dialog.dismiss(); };
  page.on('dialog', dismissDialog);
  await page.locator('#provider-routing-save').click();
  await page.waitForFunction(() => document.querySelector('#provider-routing-status').textContent.includes('Usage preferences saved'));
  assert.equal(confirmations, 0, 'Unchanged remote permission needs no repeated confirmation');
  await page.locator('#provider-routing-mode').selectOption('quality');
  await page.locator('#provider-routing-save').click();
  assert.equal(confirmations, 1, 'Changed remote routing mode requires confirmation');
  page.off('dialog', dismissDialog);
  await page.locator('#providers-refresh').click();
  await page.waitForFunction(() => !document.querySelector('#providers-refresh').disabled);
  assert.equal(await page.locator('#provider-routing-mode').inputValue(), 'balanced', 'Explicit refresh resets unsaved edits');
  await option('remote').uncheck();
  f.fail('/api/providers/routing', 'PUT', 'Mock routing save failed.');
  await page.locator('#provider-routing-save').click();
  await page.waitForFunction(() => document.querySelector('#provider-routing-status').textContent.includes('Mock routing save failed'));
  assert.equal(await page.locator('#provider-routing-mode').isDisabled(), false);
  assert.equal(await option('remote').isChecked(), false);
  await page.locator('#provider-routing-save').click();
  await page.waitForFunction(() => document.querySelector('#provider-routing-status').textContent.includes('Usage preferences saved'));
  await option('remote').check();
  page.once('dialog', dialog => dialog.accept());
  await page.locator('#provider-routing-save').click();
  await page.waitForFunction(() => document.querySelector('#provider-routing-status').textContent.includes('Usage preferences saved'));
  assert.deepEqual(status.routing.providers, ['local', 'remote'], 'Expanded remote allowlist is saved after confirmation');
  await f.close();
}

async function cancellation(browser, base) {
  const f = await fixture(browser, base);
  const { page } = f;
  await page.locator('#provider-add-compatible').click();
  await page.locator('#provider-key').fill('cancelled-fixture-secret');
  let pending = f.hold('/api/providers/profiles/test');
  await page.locator('#provider-save').click();
  await pending.started;
  await page.evaluate(() => window.ProvidersUI.close());
  assert.equal(await page.locator('#provider-key').inputValue(), '');
  await page.locator('#provider-add-ollama').click();
  pending.release();
  await page.waitForFunction(() => !document.querySelector('#provider-test').disabled);
  assert.equal(await page.locator('#provider-models option').count(), 0);
  await tested(page);
  pending = f.hold('/api/providers/profiles');
  await page.locator('#provider-save').click();
  await pending.started;
  await page.evaluate(() => window.ProvidersUI.close());
  await page.locator('#provider-add-compatible').click();
  pending.release();
  await page.locator('#provider-profile-name').fill('New in-progress form');
  // Force a subsequent status round trip so the released response has been handled.
  await page.evaluate(() => window.ProvidersUI.refresh());
  assert.equal(await page.locator('#provider-dialog').evaluate(node => node.open), true);
  assert.equal(await page.locator('#provider-profile-name').inputValue(), 'New in-progress form');
  assert.equal(await page.locator('#provider-key').inputValue(), '');
  assert.equal(await page.locator('#provider-select').inputValue(), 'new_compatible');
  const external = profile('externally_removed');
  f.status.providers.push(external);
  await page.evaluate(() => window.ProvidersUI.refresh());
  await page.locator('#provider-select').selectOption(external.id);
  await page.locator('#provider-key').fill('never-send-to-another-provider');
  pending = f.hold(`/api/providers/${external.id}/test`);
  await page.locator('#provider-save').click();
  await pending.started;
  f.status.providers = f.status.providers.filter(provider => provider.id !== external.id);
  await page.evaluate(() => window.ProvidersUI.refresh());
  pending.release();
  assert.equal(await page.locator('#provider-select').inputValue(), 'openai');
  assert.equal(await page.locator('#provider-key').inputValue(), '', 'Removing the selected profile cannot transfer its secret to the fallback selection');
  assert.equal(await page.locator('#provider-test').isDisabled(), false);
  assert.equal(await page.locator('#provider-profile-fields').isVisible(), false);
  await f.close();
}

async function main() {
  const staticRoot = path.resolve(__dirname, '../src/static');
  const base = 'http://127.0.0.1:8000'; // Intercepted; no server or listening socket.
  let browser;
  try {
    browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_PATH });
    await require('./browser_fixture.cjs').interceptFiles(browser, base);
    await keylessOnboarding(browser, base);
    await credentialsAndClear(browser, base);
    await routing(browser, base);
    await cancellation(browser, base);
    console.log('Profile UI checks passed: keyless onboarding, custom CRUD, credential tests/clearing, lost keys, safe DOM, routing consent/revocation/edits/errors, stale request cancellation.');
  } finally {
    if (browser) await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
