/* Serve test pages inside Playwright routing. Never open a listening socket. */
const fs = require('node:fs/promises');
const path = require('node:path');

exports.interceptFiles = async (browser, base) => {
  const newContext = browser.newContext.bind(browser);
  browser.newContext = async options => {
    const context = await newContext(options);
    await context.route(`${base}/**`, async route => {
      const pathname = new URL(route.request().url()).pathname;
      // Unexpected API calls must fail rather than be served a fake HTML success.
      if (pathname.startsWith('/api/') || pathname.startsWith('/mcp/')) {
        await route.fulfill({ status: 501, json: { detail: 'Unmocked request in browser check.' } });
        return;
      }
      const filename = pathname.startsWith('/static/') ? path.basename(pathname) : 'index.html';
      try {
        const data = await fs.readFile(path.join(__dirname, '../src/static', filename));
        await route.fulfill({ body: data, contentType: { '.js': 'application/javascript', '.css': 'text/css', '.html': 'text/html' }[path.extname(filename)] || 'text/plain' });
      } catch { await route.fulfill({ status: 404 }); }
    });
    return context;
  };
};
