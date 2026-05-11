// ── Config ──────────────────────────────────────────────────────────────────
const TOKEN_LIMIT = 2000;
marked.setOptions({ breaks: true, gfm: true });

// ── State ───────────────────────────────────────────────────────────────────
let _currentProject    = null;
let _currentProjectTab = 'docs';
let _dirty             = false;
let _setupProjects     = [];
let _configFormat      = 'opencode';

// ── Icons ───────────────────────────────────────────────────────────────────
function initIcons() { if (window.lucide) lucide.createIcons(); }

// ── Utils ───────────────────────────────────────────────────────────────────
const esc = s => String(s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;')
  .replace(/>/g,'&gt;').replace(/"/g,'&quot;');

const countTokens = t => t ? Math.max(1, Math.floor(t.length / 4)) : 0;
const tokClass    = n => n > TOKEN_LIMIT ? 'tok-over' : n > TOKEN_LIMIT * .8 ? 'tok-warn' : 'tok-ok';
const fmt         = iso => new Date(iso).toLocaleDateString('en-GB', { day:'numeric', month:'short', year:'numeric' });

function toast(msg, type = 'success') {
  const el = document.createElement('div');
  el.className = `toast-item ${type}`;
  el.textContent = msg;
  document.getElementById('toast').appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

function copy(text, label) {
  navigator.clipboard.writeText(text);
  toast(label || 'Copied to clipboard');
}

// ── Theme ────────────────────────────────────────────────────────────────────
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const icon = document.getElementById('theme-icon');
  if (icon) {
    icon.setAttribute('data-lucide', theme === 'light' ? 'moon' : 'sun');
    initIcons();
  }
}

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || 'dark';
  const next    = current === 'dark' ? 'light' : 'dark';
  localStorage.setItem('self_mcp_theme', next);
  applyTheme(next);
}

// ══ ROUTER ══════════════════════════════════════════════════════════════════
// URL scheme:
//   /                                        → projects list
//   /setup                                   → MCP setup
//   /projects/<projectId>                    → project docs tab
//   /projects/<projectId>/links              → project links tab
//   /projects/<projectId>/new               → new document editor
//   /projects/<projectId>/documents/<docId> → open document editor

function navigate(path, pushState = true) {
  if (pushState) history.pushState({ path }, '', path);
  _route(path);
}

async function _route(path) {
  // Close any open modal
  document.getElementById('project-modal').classList.remove('open');
  document.getElementById('link-modal').classList.remove('open');

  // /setup
  if (path === '/setup') {
    _showView('setup');
    loadSetup();
    return;
  }

  // /projects/<projectId>/documents/<docId>  or  /projects/<projectId>/new
  const docMatch = path.match(/^\/projects\/([^/]+)\/documents\/([^/]+)$/);
  const newMatch = path.match(/^\/projects\/([^/]+)\/new$/);

  if (docMatch || newMatch) {
    const projectId = (docMatch || newMatch)[1];
    if (!_currentProject || _currentProject.id !== projectId) {
      const project = await apiFetch(`/api/stores/${projectId}`);
      if (!project) { navigate('/'); return; }
      _currentProject = project;
      document.getElementById('project-breadcrumb-name').textContent = project.name;
    }
    _showView('project');
    showProjectTab('docs', false);
    if (newMatch) {
      _openEditorNew();
    } else {
      const docId = docMatch[2];
      const doc = await apiFetch(`/api/stores/${projectId}/documents/${docId}`);
      if (!doc) { navigate(`/projects/${projectId}`); return; }
      _openEditorDoc(doc);
    }
    return;
  }

  // /projects/<projectId>/links
  const linksMatch = path.match(/^\/projects\/([^/]+)\/links$/);
  if (linksMatch) {
    const projectId = linksMatch[1];
    if (!_currentProject || _currentProject.id !== projectId) {
      const project = await apiFetch(`/api/stores/${projectId}`);
      if (!project) { navigate('/'); return; }
      _currentProject = project;
      document.getElementById('project-breadcrumb-name').textContent = project.name;
    }
    _showView('project');
    showProjectTab('links', false);
    return;
  }

  // /projects/<projectId>/issues
  const issuesMatch = path.match(/^\/projects\/([^/]+)\/issues$/);
  if (issuesMatch) {
    const projectId = issuesMatch[1];
    if (!_currentProject || _currentProject.id !== projectId) {
      const project = await apiFetch(`/api/stores/${projectId}`);
      if (!project) { navigate('/'); return; }
      _currentProject = project;
      document.getElementById('project-breadcrumb-name').textContent = project.name;
    }
    _showView('project');
    showProjectTab('issues', false);
    return;
  }

  // /projects/<projectId>
  const projectMatch = path.match(/^\/projects\/([^/]+)$/);
  if (projectMatch) {
    const projectId = projectMatch[1];
    const project = await apiFetch(`/api/stores/${projectId}`);
    if (!project) { navigate('/'); return; }
    _currentProject = project;
    document.getElementById('project-breadcrumb-name').textContent = project.name;
    _showView('project');
    showProjectTab('docs', false);
    return;
  }

  // / (default)
  _currentProject = null;
  document.getElementById('doc-page').classList.remove('open');
  _showView('projects');
  loadProjects();
}

// ── popstate — browser back/forward ─────────────────────────────────────────
window.addEventListener('popstate', e => {
  const path = e.state?.path || window.location.pathname;
  if (document.getElementById('doc-page').classList.contains('open') && _dirty) {
    if (!confirm('You have unsaved changes. Leave anyway?')) {
      history.pushState({ path: window.location.pathname }, '', window.location.pathname);
      return;
    }
  }
  _dirty = false;
  document.getElementById('doc-page').classList.remove('open');
  _route(path);
});

// ── View switcher ────────────────────────────────────────────────────────────
function _showView(name) {
  ['projects','project','setup'].forEach(v => {
    const el = document.getElementById('view-' + v);
    if (!el) return;
    el.style.display = v === name ? (v === 'project' ? 'flex' : '') : 'none';
  });
  document.getElementById('nav-projects').classList.toggle('active', name === 'projects' || name === 'project');
  document.getElementById('nav-setup').classList.toggle('active', name === 'setup');
  initIcons();
}

// ── Nav helpers ───────────────────────────────────────────────────────────────
function goProjects() { navigate('/'); }
function goSetup()    { navigate('/setup'); }

// ── Projects list ─────────────────────────────────────────────────────────────
async function loadProjects() {
  const projects = await apiFetch('/api/stores');
  if (!projects) return;
  document.getElementById('nav-projects-badge').textContent = projects.length;
  document.getElementById('projects-meta').textContent = projects.length
    ? `${projects.length} project${projects.length > 1 ? 's' : ''}` : '';
  const grid  = document.getElementById('projects-grid');
  const empty = document.getElementById('projects-empty');
  if (!projects.length) { grid.innerHTML = ''; empty.style.display = ''; initIcons(); return; }
  empty.style.display = 'none';
  grid.innerHTML = projects.map(s => `
    <div class="project-card" onclick="navigate('/projects/${s.id}')">
      <div class="project-card-header">
        <div class="project-icon"><i data-lucide="archive" style="width:15px;height:15px"></i></div>
        <div class="project-card-actions" onclick="event.stopPropagation()">
          <button class="btn btn-ghost btn-xs" onclick="editProject('${s.id}','${esc(s.name)}','${esc(s.description||'')}')" title="Edit">
            <i data-lucide="pencil" style="width:11px;height:11px"></i>
          </button>
          <button class="btn btn-danger btn-xs" onclick="deleteProject('${s.id}')" title="Delete">
            <i data-lucide="trash-2" style="width:11px;height:11px"></i>
          </button>
        </div>
      </div>
      <div>
        <div class="project-name">${esc(s.name)}</div>
        ${s.description ? `<div class="project-desc">${esc(s.description)}</div>` : ''}
        <span class="project-slug">${esc(s.slug)}</span>
      </div>
      <div class="project-stats">
        <div class="project-stat"><i data-lucide="file-text" style="width:10px;height:10px"></i><strong>${s.doc_count}</strong> doc${s.doc_count !== 1 ? 's' : ''}</div>
        <div class="project-stat"><i data-lucide="link-2" style="width:10px;height:10px"></i><strong>${s.link_count}</strong> link${s.link_count !== 1 ? 's' : ''}</div>
        <div class="project-stat"><i data-lucide="zap" style="width:10px;height:10px"></i><strong>${s.total_tokens}</strong> tokens</div>
      </div>
      <div class="project-footer">
        <span class="project-sse"><i data-lucide="radio" style="width:10px;height:10px"></i>localhost:8001/${esc(s.slug)}/sse</span>
        <button class="btn btn-ghost btn-xs" onclick="event.stopPropagation();copyProjectConfig('${esc(s.slug)}','${esc(s.name)}')" title="Copy config">
          <i data-lucide="clipboard-copy" style="width:11px;height:11px"></i>
        </button>
      </div>
    </div>`).join('');
  initIcons();
}

// ── Project tabs ──────────────────────────────────────────────────────────────
function showProjectTab(tab, pushHistory = true) {
  _currentProjectTab = tab;
  ['docs','links','issues'].forEach(t => {
    document.getElementById('ptab-' + t).classList.toggle('active', t === tab);
    const el = document.getElementById('project-view-' + t);
    if (el) el.style.display = t === tab ? (t === 'docs' ? 'flex' : 'block') : 'none';
  });
  const acts = document.getElementById('project-topbar-actions');
  if (tab === 'docs') {
    acts.innerHTML = `<button class="btn btn-primary btn-sm" onclick="newDoc()"><i data-lucide="plus" style="width:13px;height:13px"></i> New Document</button>`;
  } else if (tab === 'links') {
    acts.innerHTML = `<button class="btn btn-primary btn-sm" onclick="openLinkModal()"><i data-lucide="plus" style="width:13px;height:13px"></i> Save Link</button>`;
  } else {
    acts.innerHTML = `<button class="btn btn-primary btn-sm" onclick="openIssueModal()"><i data-lucide="plus" style="width:13px;height:13px"></i> New Issue</button>`;
  }
  initIcons();
  if (pushHistory && _currentProject) {
    const suffix = tab === 'docs' ? '' : `/${tab}`;
    const path = `/projects/${_currentProject.id}${suffix}`;
    history.pushState({ path }, '', path);
  }
  if (tab === 'docs')   loadDocs();
  if (tab === 'links')  loadLinks();
  if (tab === 'issues') loadIssues();
}

// ── Docs ──────────────────────────────────────────────────────────────────────
async function loadDocs() {
  if (!_currentProject) return;
  const [docs, links] = await Promise.all([
    apiFetch(`/api/stores/${_currentProject.id}/documents`),
    apiFetch(`/api/stores/${_currentProject.id}/links`),
  ]);
  if (!docs || !links) return;
  document.getElementById('ptab-docs-badge').textContent  = docs.length;
  document.getElementById('ptab-links-badge').textContent = links.length;
  renderDocsGrid(docs);
  renderRightPanel(docs, links);
  initIcons();
}

function renderDocsGrid(docs) {
  const grid  = document.getElementById('docs-grid');
  const empty = document.getElementById('docs-empty');
  if (!docs.length) { grid.innerHTML = ''; empty.style.display = ''; initIcons(); return; }
  empty.style.display = 'none';
  grid.innerHTML = docs.map(d => `
    <div class="doc-card" onclick="navigate('/projects/${_currentProject.id}/documents/${d.id}')">
      <div class="doc-card-top">
        <div class="doc-card-title">${esc(d.title)}</div>
        <span class="token-chip ${tokClass(d.tokens)}">${d.tokens}t</span>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:4px;align-items:center;">
        ${d.tags.map(t => `<span class="tag">${esc(t)}</span>`).join('')}
        <span style="font-size:10px;color:var(--text3);margin-left:2px">${fmt(d.updated_at)}</span>
      </div>
      <div class="doc-card-preview">${esc(d.content.slice(0,160))}${d.content.length > 160 ? '…' : ''}</div>
      <div class="doc-card-footer">
        <span class="doc-card-hint"><i data-lucide="arrow-right" style="width:10px;height:10px"></i> Open</span>
        <button class="btn btn-danger btn-xs doc-del" onclick="event.stopPropagation();deleteDoc('${d.id}')">
          <i data-lucide="trash-2" style="width:10px;height:10px"></i>
        </button>
      </div>
    </div>`).join('');
}

function renderRightPanel(docs, links) {
  const total = docs.reduce((s, d) => s + d.tokens, 0);
  const avg   = docs.length ? Math.round(total / docs.length) : 0;
  document.getElementById('rp-docs').textContent   = docs.length;
  document.getElementById('rp-links').textContent  = links.length;
  document.getElementById('rp-tokens').textContent = total;
  document.getElementById('rp-avg').textContent    = docs.length ? avg : '—';
  const max = TOKEN_LIMIT * Math.max(docs.length, 1);
  document.getElementById('rp-fill').style.width   = Math.min(100, (total / max) * 100) + '%';
  document.getElementById('rp-budget-lbl').textContent = `${total} / ${max}`;
  document.getElementById('rp-endpoint').textContent   =
    _currentProject ? `localhost:8001/${_currentProject.slug}/sse` : '—';
  const list = document.getElementById('rp-doc-list');
  if (!docs.length) {
    list.innerHTML = '<div style="font-size:11px;color:var(--text3)">No documents yet</div>';
    return;
  }
  list.innerHTML = docs.map(d => `
    <div class="rp-doc-row" onclick="navigate('/projects/${_currentProject.id}/documents/${d.id}')">
      <span class="rp-doc-name" title="${esc(d.title)}">${esc(d.title)}</span>
      <span class="token-chip ${tokClass(d.tokens)}" style="font-size:9px;padding:1px 4px">${d.tokens}t</span>
    </div>`).join('');
}

function copyProjectSSE() {
  if (!_currentProject) return;
  copy(`http://localhost:8001/${_currentProject.slug}/sse`, 'SSE URL copied');
}

// ── Doc editor ────────────────────────────────────────────────────────────────
function newDoc() {
  if (!_currentProject) return;
  navigate(`/projects/${_currentProject.id}/new`);
}

function _openEditorNew() {
  document.getElementById('page-id').value    = '';
  document.getElementById('page-title').value = '';
  document.getElementById('page-tags').value  = '';
  document.getElementById('editor').value     = '';
  document.getElementById('md-preview').innerHTML  = '';
  document.getElementById('tags-preview').innerHTML = '';
  document.getElementById('page-del-btn').style.display = 'none';
  document.getElementById('page-dates').style.display   = 'none';
  _dirty = false;
  document.getElementById('dirty-pill').style.display = 'none';
  updateTokenBar();
  document.getElementById('doc-page').classList.add('open');
  setTimeout(() => document.getElementById('page-title').focus(), 60);
  initIcons();
}

function _openEditorDoc(d) {
  document.getElementById('page-id').value    = d.id;
  document.getElementById('page-title').value = d.title;
  document.getElementById('page-tags').value  = d.tags.join(', ');
  document.getElementById('editor').value     = d.content;
  document.getElementById('page-del-btn').style.display = '';
  document.getElementById('page-dates').style.display   = '';
  document.getElementById('page-created').textContent   = fmt(d.created_at);
  document.getElementById('page-updated').textContent   = fmt(d.updated_at);
  renderTags();
  updateTokenBar();
  renderPreview();
  _dirty = false;
  document.getElementById('dirty-pill').style.display = 'none';
  document.getElementById('doc-page').classList.add('open');
  initIcons();
}

function closePage() {
  if (_dirty && !confirm('You have unsaved changes. Discard them?')) return;
  _dirty = false;
  document.getElementById('doc-page').classList.remove('open');
  if (_currentProject) navigate(`/projects/${_currentProject.id}`);
  else navigate('/');
}

function markDirty()     { _dirty = true; document.getElementById('dirty-pill').style.display = ''; }
function onEditorInput() { markDirty(); updateTokenBar(); renderPreview(); }

function renderPreview() {
  document.getElementById('md-preview').innerHTML =
    marked.parse(document.getElementById('editor').value || '');
}

function updateTokenBar() {
  const text   = document.getElementById('editor').value;
  const tokens = countTokens(text);
  const pct    = Math.min(100, (tokens / TOKEN_LIMIT) * 100);
  const color  = tokens > TOKEN_LIMIT ? 'var(--red)' : tokens > TOKEN_LIMIT * .8 ? 'var(--amber)' : 'var(--green)';
  const bar    = document.getElementById('token-bar');
  bar.style.width      = pct + '%';
  bar.style.background = color;
  const tc = document.getElementById('token-count');
  tc.style.color  = color;
  tc.textContent  = tokens.toLocaleString();
  document.getElementById('char-count').textContent = text.length.toLocaleString();
}

function renderTags() {
  const tags = document.getElementById('page-tags').value
    .split(',').map(t => t.trim()).filter(Boolean);
  document.getElementById('tags-preview').innerHTML =
    tags.map(t => `<span class="tag">${esc(t)}</span>`).join('');
}

async function savePage() {
  if (!_currentProject) return;
  const id      = document.getElementById('page-id').value;
  const title   = document.getElementById('page-title').value.trim();
  const content = document.getElementById('editor').value.trim();
  const tags    = document.getElementById('page-tags').value.split(',').map(t => t.trim()).filter(Boolean);
  if (!title)   { toast('Title is required.', 'error'); return; }
  if (!content) { toast('Content is required.', 'error'); return; }
  if (countTokens(content) > TOKEN_LIMIT) { toast(`Over ${TOKEN_LIMIT} token limit.`, 'error'); return; }
  const url    = id ? `/api/stores/${_currentProject.id}/documents/${id}` : `/api/stores/${_currentProject.id}/documents`;
  const method = id ? 'PUT' : 'POST';
  const saved  = await apiFetch(url, { method, body: { title, content, tags } });
  if (!saved) return;
  const newPath = `/projects/${_currentProject.id}/documents/${saved.id}`;
  if (window.location.pathname !== newPath) history.replaceState({ path: newPath }, '', newPath);
  document.getElementById('page-id').value = saved.id;
  document.getElementById('page-del-btn').style.display = '';
  document.getElementById('page-dates').style.display   = '';
  document.getElementById('page-created').textContent   = fmt(saved.created_at);
  document.getElementById('page-updated').textContent   = fmt(saved.updated_at);
  _dirty = false;
  document.getElementById('dirty-pill').style.display = 'none';
  toast(id ? 'Document updated.' : 'Document created.');
  loadDocs();
}

async function deletePage() {
  const id = document.getElementById('page-id').value;
  if (!id || !_currentProject) { closePage(); return; }
  if (!confirm('Delete this document permanently?')) return;
  await apiFetch(`/api/stores/${_currentProject.id}/documents/${id}`, { method: 'DELETE', noBody: true });
  _dirty = false;
  document.getElementById('doc-page').classList.remove('open');
  navigate(`/projects/${_currentProject.id}`);
  toast('Document deleted.');
}

async function deleteDoc(id) {
  if (!_currentProject) return;
  if (!confirm('Delete this document?')) return;
  await apiFetch(`/api/stores/${_currentProject.id}/documents/${id}`, { method: 'DELETE', noBody: true });
  loadDocs();
  toast('Document deleted.');
}

// ── Links ─────────────────────────────────────────────────────────────────────
async function loadLinks() {
  if (!_currentProject) return;
  const links = await apiFetch(`/api/stores/${_currentProject.id}/links`);
  if (!links) return;
  document.getElementById('ptab-links-badge').textContent = links.length;
  const grid  = document.getElementById('links-grid');
  const empty = document.getElementById('links-empty');
  if (!links.length) { grid.innerHTML = ''; empty.style.display = ''; initIcons(); return; }
  empty.style.display = 'none';
  grid.innerHTML = links.map(l => `
    <div class="link-card">
      <div class="link-card-title">${esc(l.title)}</div>
      <div class="link-card-url"><a href="${esc(l.url)}" target="_blank">${esc(l.url)}</a></div>
      ${l.description ? `<div class="link-card-desc">${esc(l.description)}</div>` : ''}
      <div class="link-card-footer">
        <div style="display:flex;gap:4px;flex-wrap:wrap">${l.tags.map(t => `<span class="tag">${esc(t)}</span>`).join('')}</div>
        <button class="btn btn-danger btn-xs" onclick="deleteLink('${l.id}')">
          <i data-lucide="trash-2" style="width:10px;height:10px"></i>
        </button>
      </div>
    </div>`).join('');
  initIcons();
}

function openLinkModal() {
  ['lm-title','lm-url','lm-desc','lm-tags'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('link-modal').classList.add('open');
  setTimeout(() => document.getElementById('lm-title').focus(), 50);
}
function closeLinkModal() { document.getElementById('link-modal').classList.remove('open'); }

async function saveLink() {
  if (!_currentProject) return;
  const title       = document.getElementById('lm-title').value.trim();
  const url         = document.getElementById('lm-url').value.trim();
  const description = document.getElementById('lm-desc').value.trim();
  const tags        = document.getElementById('lm-tags').value.split(',').map(t => t.trim()).filter(Boolean);
  if (!title || !url) { toast('Title and URL are required.', 'error'); return; }
  const saved = await apiFetch(`/api/stores/${_currentProject.id}/links`, {
    method: 'POST', body: { title, url, description, tags }
  });
  if (!saved) return;
  closeLinkModal(); loadLinks(); toast('Link saved.');
}

async function deleteLink(id) {
  if (!_currentProject) return;
  if (!confirm('Delete this link?')) return;
  await apiFetch(`/api/stores/${_currentProject.id}/links/${id}`, { method: 'DELETE', noBody: true });
  loadLinks(); toast('Link deleted.');
}

// ── Project CRUD ──────────────────────────────────────────────────────────────
function openCreateProject() {
  document.getElementById('pm-id').value    = '';
  document.getElementById('pm-name').value  = '';
  document.getElementById('pm-desc').value  = '';
  document.getElementById('project-modal-title').textContent = 'New Project';
  document.getElementById('project-modal').classList.add('open');
  setTimeout(() => document.getElementById('pm-name').focus(), 50);
}
function editProject(id, name, desc) {
  document.getElementById('pm-id').value    = id;
  document.getElementById('pm-name').value  = name;
  document.getElementById('pm-desc').value  = desc;
  document.getElementById('project-modal-title').textContent = 'Edit Project';
  document.getElementById('project-modal').classList.add('open');
}
function closeProjectModal() { document.getElementById('project-modal').classList.remove('open'); }

async function saveProject() {
  const id   = document.getElementById('pm-id').value;
  const name = document.getElementById('pm-name').value.trim();
  const desc = document.getElementById('pm-desc').value.trim();
  if (!name) { toast('Name is required.', 'error'); return; }
  const saved = await apiFetch(id ? `/api/stores/${id}` : '/api/stores', {
    method: id ? 'PUT' : 'POST', body: { name, description: desc }
  });
  if (!saved) return;
  closeProjectModal(); loadProjects();
  toast(id ? 'Project updated.' : 'Project created.');
}

async function deleteProject(id) {
  if (!confirm('Delete this project and all its content?')) return;
  await apiFetch(`/api/stores/${id}`, { method: 'DELETE', noBody: true });
  loadProjects(); toast('Project deleted.');
}

// ── MCP Setup ─────────────────────────────────────────────────────────────────
async function loadSetup() {
  const projects = await apiFetch('/api/stores');
  if (!projects) return;
  _setupProjects = projects;
  const list = document.getElementById('setup-projects-list');
  if (!projects.length) {
    list.innerHTML = '<p style="font-size:13px;color:var(--text3)">No projects yet — create one first.</p>';
  } else {
    list.innerHTML = projects.map(s => `
      <div class="project-cfg-row">
        <div>
          <div style="font-weight:600;font-size:13px;color:var(--text)">${esc(s.name)}</div>
          <code style="font-family:var(--mono);font-size:10px;color:var(--text3)">localhost:8001/${esc(s.slug)}/sse</code>
        </div>
        <div style="display:flex;gap:5px;flex-shrink:0">
          <button class="btn btn-secondary btn-xs" onclick="copy('http://localhost:8001/${esc(s.slug)}/sse','SSE URL copied')">
            <i data-lucide="link" style="width:10px;height:10px"></i> URL
          </button>
          <button class="btn btn-secondary btn-xs" onclick="copyProjectConfig('${esc(s.slug)}','${esc(s.name)}')">
            <i data-lucide="clipboard-copy" style="width:10px;height:10px"></i> Config
          </button>
        </div>
      </div>`).join('');
  }
  setConfigFormat(_configFormat);
  initIcons();
}

function setConfigFormat(f) {
  _configFormat = f;
  ['opencode','claude','cursor'].forEach(x =>
    document.getElementById('cfg-btn-' + x).classList.toggle('active', x === f)
  );
  renderConfigOutput();
}

function renderConfigOutput() {
  const projects = _setupProjects;
  if (!projects.length) {
    document.getElementById('cfg-output').textContent = '// No projects yet — create one first.';
    document.getElementById('cfg-note').textContent   = '';
    return;
  }
  let out = '', note = '';
  if (_configFormat === 'opencode') {
    const entries = projects.map(s =>
      `    "self_mcp_${s.slug}": {\n      "type": "remote",\n      "url": "http://localhost:8001/${s.slug}/sse",\n      "enabled": true\n    }`
    ).join(',\n');
    out  = `{\n  "mcp": {\n${entries}\n  }\n}`;
    note = 'Merge the "mcp" block into ~/.config/opencode/opencode.json';
  } else if (_configFormat === 'claude') {
    const entries = projects.map(s =>
      `    "self_mcp_${s.slug}": { "url": "http://localhost:8001/${s.slug}/sse" }`
    ).join(',\n');
    out  = `{\n  "mcpServers": {\n${entries}\n  }\n}`;
    note = 'Merge into claude_desktop_config.json, then restart Claude Desktop.';
  } else {
    out  = projects.map(s => `http://localhost:8001/${s.slug}/sse`).join('\n');
    note = 'In Cursor: Settings → MCP → add each URL as a separate remote server.';
  }
  document.getElementById('cfg-output').textContent = out;
  document.getElementById('cfg-note').textContent   = note;
}

function copyGeneratedConfig() {
  const text = document.getElementById('cfg-output').textContent;
  if (!text || text.startsWith('//')) { toast('Nothing to copy.', 'error'); return; }
  navigator.clipboard.writeText(text);
  toast('Config copied to clipboard');
}

function copyProjectConfig(slug, name) {
  const snippet = `"self_mcp_${slug}": {\n  "type": "remote",\n  "url": "http://localhost:8001/${slug}/sse",\n  "enabled": true\n}`;
  navigator.clipboard.writeText(snippet);
  toast(`Config for "${name}" copied`);
}

// ── Issues ────────────────────────────────────────────────────────────────────

const PRIORITY_ORDER = { high: 0, medium: 1, low: 2 };
const STATUS_LABEL   = { open: 'Open', in_progress: 'In Progress', done: 'Done' };
const PRIORITY_LABEL = { low: 'Low', medium: 'Medium', high: 'High' };

async function loadIssues() {
  if (!_currentProject) return;
  const issues = await apiFetch(`/api/stores/${_currentProject.id}/issues`);
  if (!issues) return;
  document.getElementById('ptab-issues-badge').textContent = issues.length;
  const list  = document.getElementById('issues-list');
  const empty = document.getElementById('issues-empty');
  if (!issues.length) { list.innerHTML = ''; empty.style.display = ''; initIcons(); return; }
  empty.style.display = 'none';
  // Sort: assigned first, then by priority, then by status (open → in_progress → done)
  const sorted = [...issues].sort((a, b) => {
    if (b.assigned_to_model !== a.assigned_to_model) return b.assigned_to_model ? 1 : -1;
    return (PRIORITY_ORDER[a.priority] ?? 1) - (PRIORITY_ORDER[b.priority] ?? 1);
  });
  list.innerHTML = sorted.map(issue => renderIssueCard(issue)).join('');
  initIcons();
}

function renderIssueCard(issue) {
  const isDone     = issue.status === 'done';
  const assigned   = issue.assigned_to_model;
  const statusCls  = { open: 'issue-status-open', in_progress: 'issue-status-progress', done: 'issue-status-done' }[issue.status] || '';
  const priorityCls= { high: 'issue-priority-high', medium: 'issue-priority-medium', low: 'issue-priority-low' }[issue.priority] || '';
  const tags = (issue.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join('');

  return `
    <div class="issue-card ${isDone ? 'issue-done' : ''} ${assigned ? 'issue-assigned' : ''}" id="issue-card-${issue.id}">
      <div class="issue-card-header">
        <div class="issue-status-badge ${statusCls}">
          <i data-lucide="${isDone ? 'check-circle-2' : issue.status === 'in_progress' ? 'loader-2' : 'circle-dot'}" style="width:12px;height:12px"></i>
          ${STATUS_LABEL[issue.status] || issue.status}
        </div>
        <div class="issue-priority-badge ${priorityCls}">
          <i data-lucide="${issue.priority === 'high' ? 'chevrons-up' : issue.priority === 'low' ? 'chevrons-down' : 'minus'}" style="width:11px;height:11px"></i>
          ${PRIORITY_LABEL[issue.priority] || issue.priority}
        </div>
        <div style="flex:1"></div>
        <div class="issue-card-actions">
          <button class="btn btn-ghost btn-xs" onclick="editIssue('${issue.id}')" title="Edit">
            <i data-lucide="pencil" style="width:11px;height:11px"></i>
          </button>
          <button class="btn btn-danger btn-xs" onclick="deleteIssue('${issue.id}')" title="Delete">
            <i data-lucide="trash-2" style="width:11px;height:11px"></i>
          </button>
        </div>
      </div>
      <div class="issue-title">${esc(issue.title)}</div>
      ${issue.description ? `<div class="issue-desc">${esc(issue.description.slice(0, 200))}${issue.description.length > 200 ? '…' : ''}</div>` : ''}
      ${tags ? `<div class="issue-tags">${tags}</div>` : ''}
      <div class="issue-card-footer">
        <span class="issue-date"><i data-lucide="clock" style="width:10px;height:10px"></i> ${fmt(issue.updated_at)}</span>
        <div class="issue-assign-toggle ${assigned ? 'assigned' : ''}" onclick="toggleAssign('${issue.id}', ${assigned})" title="${assigned ? 'Click to unassign from model' : 'Click to assign to model'}">
          <div class="issue-assign-icon"><i data-lucide="${assigned ? 'bot' : 'bot'}" style="width:12px;height:12px"></i></div>
          <span>${assigned ? '✦ Assigned to model' : 'Assign to model'}</span>
          <div class="issue-assign-dot ${assigned ? 'on' : 'off'}"></div>
        </div>
      </div>
    </div>`;
}

function openIssueModal(issue = null) {
  document.getElementById('im-id').value       = issue?.id || '';
  document.getElementById('im-title').value    = issue?.title || '';
  document.getElementById('im-desc').value     = issue?.description || '';
  document.getElementById('im-status').value   = issue?.status || 'open';
  document.getElementById('im-priority').value = issue?.priority || 'medium';
  document.getElementById('im-tags').value     = (issue?.tags || []).join(', ');
  document.getElementById('issue-modal-title').textContent = issue ? 'Edit Issue' : 'New Issue';
  document.getElementById('issue-modal').classList.add('open');
  setTimeout(() => document.getElementById('im-title').focus(), 50);
}

function closeIssueModal() { document.getElementById('issue-modal').classList.remove('open'); }

async function editIssue(id) {
  const issue = await apiFetch(`/api/stores/${_currentProject.id}/issues/${id}`);
  if (issue) openIssueModal(issue);
}

async function saveIssue() {
  if (!_currentProject) return;
  const id       = document.getElementById('im-id').value;
  const title    = document.getElementById('im-title').value.trim();
  const desc     = document.getElementById('im-desc').value.trim();
  const status   = document.getElementById('im-status').value;
  const priority = document.getElementById('im-priority').value;
  const tags     = document.getElementById('im-tags').value.split(',').map(t => t.trim()).filter(Boolean);
  if (!title) { toast('Title is required.', 'error'); return; }
  const url    = id ? `/api/stores/${_currentProject.id}/issues/${id}` : `/api/stores/${_currentProject.id}/issues`;
  const method = id ? 'PUT' : 'POST';
  const saved  = await apiFetch(url, { method, body: { title, description: desc, status, priority, tags } });
  if (!saved) return;
  closeIssueModal();
  loadIssues();
  toast(id ? 'Issue updated.' : 'Issue created.');
}

async function deleteIssue(id) {
  if (!_currentProject) return;
  if (!confirm('Delete this issue permanently?')) return;
  // If it has a task doc, delete that too
  const issue = await apiFetch(`/api/stores/${_currentProject.id}/issues/${id}`);
  if (issue?.task_doc_id) {
    await apiFetch(`/api/stores/${_currentProject.id}/documents/${issue.task_doc_id}`, { method: 'DELETE', noBody: true });
  }
  await apiFetch(`/api/stores/${_currentProject.id}/issues/${id}`, { method: 'DELETE', noBody: true });
  loadIssues();
  toast('Issue deleted.');
}

async function toggleAssign(issueId, currentlyAssigned) {
  if (!_currentProject) return;
  const newAssigned = !currentlyAssigned;

  if (newAssigned) {
    // Fetch the issue to build the task doc
    const issue = await apiFetch(`/api/stores/${_currentProject.id}/issues/${issueId}`);
    if (!issue) return;

    // Create a task document in the project
    const taskTitle   = `[Issue] ${issue.title}`;
    const taskContent = buildTaskDocContent(issue);
    const taskDoc = await apiFetch(`/api/stores/${_currentProject.id}/documents`, {
      method: 'POST',
      body: { title: taskTitle, content: taskContent, tags: ['issue', 'task', ...(issue.tags || [])] },
    });
    if (!taskDoc) return;

    // Mark the issue as assigned, record the task doc id
    await apiFetch(`/api/stores/${_currentProject.id}/issues/${issueId}/assign`, {
      method: 'POST',
      body: { assigned: true, task_doc_id: taskDoc.id },
    });
    toast('Issue assigned to model — task document created.');
  } else {
    // Fetch current issue to find any linked task doc
    const issue = await apiFetch(`/api/stores/${_currentProject.id}/issues/${issueId}`);
    if (issue?.task_doc_id) {
      await apiFetch(`/api/stores/${_currentProject.id}/documents/${issue.task_doc_id}`, { method: 'DELETE', noBody: true });
    }
    await apiFetch(`/api/stores/${_currentProject.id}/issues/${issueId}/assign`, {
      method: 'POST',
      body: { assigned: false, task_doc_id: null },
    });
    toast('Issue unassigned from model — task document removed.');
  }

  loadIssues();
}

function buildTaskDocContent(issue) {
  const priorityEmoji = { high: '🔴', medium: '🟡', low: '🟢' }[issue.priority] || '⚪';
  return `# ${issue.title}

**Type:** Issue task  
**Priority:** ${priorityEmoji} ${issue.priority}  
**Status:** ${issue.status}  
**Tags:** ${(issue.tags || []).join(', ') || 'none'}

## Description

${issue.description || '_No description provided._'}

## Instructions for the AI model

Yes, I want to work on this. The user has assigned this issue to you. Please:

1. Analyse the issue described above.
2. Propose a plan before making changes.
3. Implement the solution, making minimal focused changes.
4. Update the issue status to \`done\` when complete (via the self_mcp GUI).

## Acceptance criteria

- The issue described above is resolved.
- No regressions are introduced.
- Changes are explained clearly in the conversation.
`;
}
async function apiFetch(url, opts = {}) {
  try {
    const res = await fetch(url, {
      method: opts.method || 'GET',
      headers: opts.body ? { 'Content-Type': 'application/json' } : {},
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (res.status === 204) return true;
    if (!res.ok) {
      let msg = `Error ${res.status}`;
      try { const e = await res.json(); msg = e.detail || msg; } catch {}
      toast(msg, 'error'); return null;
    }
    if (opts.noBody) return true;
    return await res.json();
  } catch {
    toast('Network error — is the server running?', 'error'); return null;
  }
}

// ── Keyboard shortcuts ────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (document.getElementById('project-modal').classList.contains('open')) { closeProjectModal(); return; }
    if (document.getElementById('link-modal').classList.contains('open'))    { closeLinkModal(); return; }
    if (document.getElementById('issue-modal').classList.contains('open'))   { closeIssueModal(); return; }
  }
  if ((e.metaKey || e.ctrlKey) && e.key === 's') {
    e.preventDefault();
    if (document.getElementById('doc-page').classList.contains('open')) savePage();
  }
});

// ── Init — boot from current URL ──────────────────────────────────────────────
(function init() {
  // Restore theme from localStorage
  const saved = localStorage.getItem('self_mcp_theme') || 'dark';
  applyTheme(saved);

  const _initPath = window.location.pathname;
  history.replaceState({ path: _initPath }, '', _initPath);
  _route(_initPath);
})();
