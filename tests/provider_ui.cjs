/* Run with PLAYWRIGHT_MODULE set to an installed playwright package if needed. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const staticRoot = path.resolve(__dirname, '../src/static');
const project = {
  id: 'a7c81063-d071-43d2-9933-e9c845860001', slug: 'personal',
  name: "A user's project", description: 'Existing content',
  doc_count: 0, link_count: 0, total_tokens: 0,
};

function initialStatus(environment = false) {
  return {
    providers: [
      { id: 'openai', name: 'OpenAI', configured: environment, source: environment ? 'environment' : null, default_model: null },
      { id: 'anthropic', name: 'Anthropic', configured: false, source: null, default_model: null },
    ],
    keyring_available: false, default_provider: null, onboarding_dismissed: false,
  };
}

async function mockAPI(page, status) {
  const calls = [];
  await page.route('https://**', route => route.abort());
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const body = request.postDataJSON();
    const method = request.method();
    calls.push({ path: url.pathname, method, body });
    let result = {};
    let code = 200;
    if (url.pathname === '/api/providers') result = status;
    else if (url.pathname === '/api/providers/onboarding') {
      status.onboarding_dismissed = body.dismissed;
      result = { onboarding_dismissed: body.dismissed };
    } else if (url.pathname.endsWith('/test')) {
      if (body.api_key === 'bad-test-key') {
        code = 401;
        result = { detail: 'Provider authentication failed.' };
      } else {
        result = { models: [{ id: 'test-model', name: 'Test model' }], message: 'Authentication and model listing succeeded. This does not verify inference access.' };
      }
    } else if (url.pathname.startsWith('/api/providers/')) {
      const provider = status.providers.find(item => url.pathname.endsWith(`/${item.id}`));
      if (method === 'PUT') {
        provider.configured = true;
        provider.source = body.api_key ? body.storage : provider.source;
        provider.default_model = body.model;
        if (body.make_default) status.default_provider = provider.id;
      } else if (method === 'DELETE') {
        provider.configured = false;
        provider.source = null;
        provider.default_model = null;
      }
      result = { message: 'Provider settings updated.' };
    } else if (url.pathname === '/api/stores') result = [project];
    else if (url.pathname === `/api/stores/${project.id}`) result = project;
    else result = [];
    await route.fulfill({ status: code, json: result });
  });
  return calls;
}

async function checkOnboarding(browser, base) {
  const context = await browser.newContext({ viewport: { width: 390, height: 650 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const status = initialStatus();
  const calls = await mockAPI(page, status);
  await page.goto(base);
  await page.waitForSelector('#provider-dialog[open]');
  assert.equal(await page.locator('#project-tabs .project-tab').count(), 4);
  assert.equal(await page.locator('#provider-storage').inputValue(), 'session');
  assert.equal(await page.locator('#provider-storage option[value=keyring]').isDisabled(), true);
  assert.equal(await page.locator('#provider-key').getAttribute('type'), 'password');
  const scrollable = await page.locator('#provider-dialog .modal-body').evaluate(node => {
    node.scrollTop = node.scrollHeight;
    return node.scrollHeight > node.clientHeight && node.scrollTop > 0;
  });
  assert.equal(scrollable, true, 'Short viewports must allow scrolling to all fields');

  await page.locator('#provider-key').fill('bad-test-key');
  await page.locator('#provider-save').click();
  await page.waitForFunction(() => document.querySelector('#provider-test-status').textContent.includes('authentication failed'));
  assert.equal(calls.some(call => call.method === 'PUT'), false);
  await page.locator('#provider-key').fill('safe-fixture-key');
  await page.locator('#provider-save').click();
  await page.waitForFunction(() => !document.querySelector('#provider-dialog').open);
  assert.equal(await page.locator('#provider-key').inputValue(), '');
  assert.equal(status.default_provider, 'openai');
  assert.equal(calls.find(call => call.method === 'PUT').body.model, '', 'A new user needs no model name');
  assert.equal(calls.filter(call => call.path.endsWith('/test')).length, 2);
  assert.equal(status.providers[0].source, 'session');
  assert.equal(calls.filter(call => call.method === 'PUT').length, 1);
  assert.equal(await page.evaluate(() => JSON.stringify(localStorage).includes('safe-fixture-key')), false);

  await page.locator('#nav-providers').click();
  await page.waitForSelector('#view-providers', { state: 'visible' });
  assert.equal(await page.locator('#providers-list .provider-card').count(), 2);
  await page.getByRole('button', { name: 'Manage / test' }).click();
  await page.locator('#provider-key').fill('clear-on-close');
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#provider-key').inputValue(), '');
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: 'Remove configuration' }).click();
  await page.waitForFunction(() => document.querySelectorAll('#providers-list .btn-danger').length === 0);

  await page.locator('#nav-projects').click();
  await page.waitForSelector('.project-card');
  await page.locator('.project-card button[title=Edit]').click();
  assert.equal(await page.locator('#pm-name').inputValue(), project.name, 'Quoted names must retain valid edit handlers');
  await page.keyboard.press('Escape');
  await page.locator('.project-card').click();
  await page.waitForSelector('#view-project', { state: 'visible' });
  for (const tab of ['links', 'issues', 'docs']) {
    await page.locator(`#ptab-${tab}`).click();
    await page.waitForSelector(`#project-view-${tab}`, { state: 'visible' });
  }
  assert.deepEqual(errors, []);
  await context.close();
}

async function checkSkipAndEnvironment(browser, base) {
  const context = await browser.newContext();
  const page = await context.newPage();
  const status = initialStatus();
  const calls = await mockAPI(page, status);
  await page.goto(base);
  await page.waitForSelector('#provider-dialog[open]');
  await page.locator('#provider-skip').click();
  await page.waitForFunction(() => !document.querySelector('#provider-dialog').open);
  assert.equal(status.onboarding_dismissed, true);
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#providers-list').children.length === 2);
  assert.equal(await page.locator('#provider-dialog').evaluate(node => node.open), false);
  status.onboarding_dismissed = false;
  status.providers[0].configured = true;
  status.providers[0].source = 'environment';
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#providers-list').textContent.includes('Environment variable'));
  assert.equal(await page.locator('#provider-dialog').evaluate(node => node.open), false);
  assert.equal(calls.some(call => call.path.endsWith('/test')), false, 'Startup must never test existing credentials automatically');
  await page.goto(`${base}/providers`);
  await page.getByRole('button', { name: 'Manage / test' }).click();
  assert.equal(await page.locator('#provider-key-field').isVisible(), false);
  await page.locator('#provider-model').fill('test-model');
  await page.locator('#provider-save').click();
  await page.waitForFunction(() => !document.querySelector('#provider-dialog').open);
  assert.equal('api_key' in calls.find(call => call.method === 'PUT').body, false);
  await context.close();
}

async function main() {
  const base = 'http://127.0.0.1:8000'; // Intercepted; no server or listening socket.
  let browser;
  try {
    browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_PATH });
    await require('./browser_fixture.cjs').interceptFiles(browser, base);
    await checkOnboarding(browser, base);
    await checkSkipAndEnvironment(browser, base);
    console.log('Provider UI checks passed: setup, test errors, save, clear secrets, remove, skip, environment detection, mobile scrolling, existing tabs.');
  } finally {
    if (browser) await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
