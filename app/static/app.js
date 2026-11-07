const COLLAPSE_KEY = 'papertex.collapsed';
const LIBRARY_MODE_KEY = 'papertex.libraryMode';
const LIBRARY_MODES = ['paper', 'note', 'link'];
const SORT_MODE_KEY = 'papertex.sortMode';
const SORT_MODES = ['default', 'added'];
const PROJECT_SOURCE_KEY = 'papertex.projectSource';
const PROJECT_SOURCE_LABELS = { remote: '远端', local: '本机' };

function normalizeProjectSource(value) {
  return value === 'local' ? 'local' : 'remote';
}

function projectSourceLabel(source) {
  return PROJECT_SOURCE_LABELS[normalizeProjectSource(source)];
}

function loadProjectSource() {
  try {
    return normalizeProjectSource(localStorage.getItem(PROJECT_SOURCE_KEY));
  } catch {
    return 'remote';
  }
}

function saveProjectSource(source) {
  try { localStorage.setItem(PROJECT_SOURCE_KEY, normalizeProjectSource(source)); } catch {}
}

function loadLibraryMode() {
  try {
    const v = localStorage.getItem(LIBRARY_MODE_KEY);
    if (LIBRARY_MODES.includes(v)) return v;
  } catch {}
  return 'paper';
}

function saveLibraryMode(mode) {
  try { localStorage.setItem(LIBRARY_MODE_KEY, mode); } catch {}
}

function loadSortMode() {
  try {
    const v = localStorage.getItem(SORT_MODE_KEY);
    if (SORT_MODES.includes(v)) return v;
  } catch {}
  return 'default';
}

function saveSortMode(mode) {
  try { localStorage.setItem(SORT_MODE_KEY, mode); } catch {}
}

function loadCollapsed() {
  try {
    const v = JSON.parse(localStorage.getItem(COLLAPSE_KEY) || '[]');
    return Array.isArray(v) ? v : [];
  } catch { return []; }
}

function saveCollapsed() {
  localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...state.collapsed]));
}

const PROJECT_TYPES_KEY = 'papertex.projectTypes';
const PROJECT_TYPES = [
  { id: 'pdf', label: 'PDF' },
  { id: 'png', label: 'PNG' },
  { id: 'jpg', label: 'JPG' },
  { id: 'webp', label: 'WebP' },
  { id: 'gif', label: 'GIF' },
  { id: 'md', label: 'Markdown' },
];

function loadProjectTypes() {
  const known = new Set(PROJECT_TYPES.map(t => t.id));
  try {
    const saved = JSON.parse(localStorage.getItem(PROJECT_TYPES_KEY) || 'null');
    if (Array.isArray(saved)) return new Set(saved.filter(t => known.has(t)));
  } catch {}
  return new Set(known);
}

function saveProjectTypes() {
  localStorage.setItem(PROJECT_TYPES_KEY, JSON.stringify([...state.projectTypes]));
}

const state = {
  items: [],
  remoteName: '远端',
  remoteError: '',
  search: '',
  libraryMode: loadLibraryMode(),
  sortMode: loadSortMode(),
  topicFilter: '',
  yearFilter: '',
  venueFilter: '',
  readingFilter: '', texFilter: '',
  tagFilter: new Set(),
  knowledge: [],
  knowledgeError: '',
  knowledgeDraft: null,
  research: {},
  researchRevision: '',
  researchError: '',
  metaCache: new Map(),
  onlyGaps: false,
  onlyStarred: false,
  selectMode: false,
  selected: new Set(),
  stars: new Set(),
  collapsed: new Set(loadCollapsed()),
  previewSource: null,
  current: null,
  projectsLoaded: { remote: false, local: false },
  projectsCache: { remote: [], local: [] },
  projects: [],
  refs: [],
  refsSession: null,
  project: '',
  projectSource: loadProjectSource(),
  projectSelection: { remote: '', local: '' },
  projectPane: 'refs',
  projectTypes: loadProjectTypes(),
  projectFiles: { requestId: 0, mode: 'recent', path: '', entries: [], files: [], truncated: false, loaded: false },
  pdfToken: '',
  settingsHost: '127.0.0.1',
};

const VENUES = [
  'ICML', 'ICLR', 'NeurIPS', 'AAAI', 'IJCAI', 'KDD', 'WWW', 'CVPR', 'ICCV', 'ECCV',
  'ACM-MM', 'ACL', 'EMNLP', 'NAACL', 'SIGIR', 'CIKM', 'ICDM', 'ICDE', 'SIGMOD', 'VLDB',
  'TSP', 'TKDE', 'TNNLS', 'TPAMI', 'TCOM', 'TWC', 'IoTJ', 'arXiv',
];

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));
const remoteLabel = () => state.remoteName || '远端';

const TITLE_ACRONYMS = new Set([
  'AI', 'AGI', 'ML', 'DL', 'RL', 'NLP', 'NLG', 'CV', 'LLM', 'LLMS', 'GNN', 'GNNS',
  'CNN', 'CNNS', 'RNN', 'RNNS', 'LSTM', 'GRU', 'TSF', 'IOT', 'GPU', 'CPU', 'API',
  'RAG', 'SOTA', 'ICML', 'ICLR', 'NEURIPS', 'NIPS', 'AAAI', 'IJCAI', 'KDD', 'WWW',
  'CVPR', 'ICCV', 'ECCV', 'ACL', 'EMNLP', 'NAACL', 'SIGIR', 'CIKM', 'ICDM', 'ICDE',
  'SIGMOD', 'VLDB', 'TSP', 'TKDE', 'TNNLS', 'TPAMI', 'TCOM', 'TWC', 'IOTJ', 'IMWUT',
  'UAI', 'AISTATS', 'COLT', 'JMLR', 'TMLR', 'CHI', 'ACM-MM',
]);

// Display-only title case: all-caps words become Capitalized, while acronyms,
// tokens with digits, and words already mixing cases are left untouched.
function displayTitle(value) {
  const text = String(value ?? '');
  if (!text) return '';
  return text.split(/(\s+)/).map(token => {
    if (!token.trim()) return token;
    const match = token.match(/^([^A-Za-z0-9]*)(.*?)([^A-Za-z0-9]*)$/);
    if (!match) return token;
    const [, lead, core, tail] = match;
    if (!core || core.includes('.')) return token;
    const upper = /[A-Z]/.test(core);
    const lower = /[a-z]/.test(core);
    if (upper && lower) return token;
    if (!upper) return lead + core.charAt(0).toUpperCase() + core.slice(1) + tail;
    if (/\d/.test(core)) return token;
    const possessive = core.match(/^(.*?)(['’][sS])$/);
    const base = possessive ? possessive[1] : core;
    if (TITLE_ACRONYMS.has(base.toUpperCase())) return token;
    const fixed = base.charAt(0).toUpperCase() + base.slice(1).toLowerCase() +
      (possessive ? "'s" : '');
    return lead + fixed + tail;
  }).join('');
}

const systemTheme = matchMedia('(prefers-color-scheme: dark)');
let themePreference = null;
try { themePreference = localStorage.getItem('papertex.theme'); } catch {}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const dark = theme === 'dark';
  $('#theme-toggle').textContent = dark ? '浅色模式' : '暗黑模式';
  $('#theme-toggle').setAttribute('aria-pressed', String(dark));
  $('#theme-toggle').title = dark ? '切换到浅色模式' : '切换到暗黑模式';
}

function toggleTheme() {
  themePreference = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  applyTheme(themePreference);
  try { localStorage.setItem('papertex.theme', themePreference); } catch {}
}

applyTheme(['light', 'dark'].includes(themePreference)
  ? themePreference : (systemTheme.matches ? 'dark' : 'light'));
$('#theme-toggle').onclick = toggleTheme;
systemTheme.addEventListener('change', event => {
  if (!['light', 'dark'].includes(themePreference)) applyTheme(event.matches ? 'dark' : 'light');
});

function renderViewState(target, kind, title, detail = '', retry = null) {
  target.innerHTML = `<div class="view-state ${kind}" role="${kind === 'error' ? 'alert' : 'status'}">
    ${kind === 'loading' ? '<span class="loading-ring" aria-hidden="true"></span>' : ''}
    <strong>${esc(title)}</strong>
    ${detail ? `<p>${esc(detail)}</p>` : ''}
    ${retry ? '<button type="button" class="mini">重试</button>' : ''}
  </div>`;
  if (retry) target.querySelector('button').onclick = retry;
}

/* ---------- 通知与确认 ---------- */

const TOAST_LIMIT = 4;

function toast(message, kind = 'info', timeout = 0) {
  const text = String(message ?? '').trim();
  if (!text) return null;
  const host = document.getElementById('toasts');
  if (!host) return null;
  const el = document.createElement('div');
  el.className = 'toast ' + (['ok', 'warn', 'error'].includes(kind) ? kind : 'info');
  el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  el.tabIndex = 0;
  el.textContent = text;
  host.append(el);
  while (host.children.length > TOAST_LIMIT) host.firstElementChild.remove();
  const ttl = timeout || (kind === 'error' ? 9000 : kind === 'warn' ? 7000 : 4200);
  let done = false;
  const dismiss = () => {
    if (done) return;
    done = true;
    clearTimeout(timer);
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 200);
  };
  const timer = setTimeout(dismiss, ttl);
  el.onclick = dismiss;
  el.onkeydown = event => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      dismiss();
    }
  };
  return el;
}

function confirmDialog({
  title = '确认操作',
  text = '',
  confirmLabel = '确认',
  danger = true,
  expected = null,
  inputLabel = '输入名称确认',
} = {}) {
  const modal = document.getElementById('confirm-modal');
  if (!modal || typeof modal.showModal !== 'function') return Promise.resolve(false);
  const titleEl = document.getElementById('confirm-title');
  const textEl = document.getElementById('confirm-text');
  const field = document.getElementById('confirm-field');
  const labelEl = document.getElementById('confirm-label');
  const input = document.getElementById('confirm-input');
  const hint = document.getElementById('confirm-hint');
  const ok = document.getElementById('confirm-ok');
  const cancel = document.getElementById('confirm-cancel');
  const needsText = typeof expected === 'string';
  const opener = document.activeElement;
  titleEl.textContent = title;
  textEl.textContent = text;
  textEl.classList.toggle('hidden', !text);
  field.classList.toggle('hidden', !needsText);
  ok.textContent = confirmLabel;
  ok.classList.toggle('danger', danger);
  ok.classList.toggle('primary', !danger);
  if (needsText) {
    labelEl.textContent = inputLabel;
    hint.textContent = `需与「${expected}」完全一致`;
    input.value = '';
    ok.disabled = true;
  } else {
    ok.disabled = false;
  }
  return new Promise(resolve => {
    let settled = false;
    const finish = value => {
      if (settled) return;
      settled = true;
      modal.close();
      modal.classList.add('hidden');
      document.body.classList.remove('preview-open');
      ok.onclick = cancel.onclick = input.onkeydown = input.oninput = modal.oncancel = null;
      if (opener && opener.isConnected && typeof opener.focus === 'function') opener.focus();
      resolve(value);
    };
    input.oninput = () => { if (needsText) ok.disabled = input.value !== expected; };
    input.onkeydown = event => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      if (!ok.disabled) finish(true);
    };
    ok.onclick = () => finish(true);
    cancel.onclick = () => finish(false);
    modal.oncancel = event => {
      event.preventDefault();
      finish(false);
    };
    modal.classList.remove('hidden');
    document.body.classList.add('preview-open');
    modal.showModal();
    if (needsText) input.focus();
    else cancel.focus();
  });
}

async function api(path, opts = {}) {
  const headers = new Headers(opts.headers);
  if (!['GET', 'HEAD', 'OPTIONS'].includes((opts.method || 'GET').toUpperCase())) {
    headers.set('X-Papertex-Request', '1');
  }
  const res = await fetch(path, { ...opts, headers });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const msg = (data && (data.detail || data.message)) || text || res.statusText;
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    err.status = res.status;
    err.detail = data && data.detail;
    throw err;
  }
  if (data && Array.isArray(data.warnings) && data.warnings.length) {
    toast('操作部分完成，请检查以下事项：\n' + data.warnings.join('\n'), 'warn');
  }
  if (data && data.notice) toast(data.notice, 'info');
  return data;
}

const operationViews = new WeakMap();

function elapsedText(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  return `${Math.floor(seconds / 60)}分${seconds % 60}秒`;
}

function byteText(bytes) {
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(2) + ' MiB';
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KiB';
  return bytes + ' B';
}

function operationText(progress, elapsed, sincePoll = 0, notice = '') {
  const message = progress?.status === 'completed'
    ? '服务端处理结束，等待请求响应确认'
    : (progress?.message || '正在发送请求／接收上传文件');
  const parts = [message];
  const details = progress?.details || {};
  if (details.host) parts.push(details.host);
  if (typeof details.received_bytes === 'number') {
    const size = byteText(details.received_bytes);
    if (details.total_bytes > 0) {
      const percent = Math.min(100, Math.floor(details.received_bytes / details.total_bytes * 100));
      parts.push(`已接收 ${size} / ${byteText(details.total_bytes)}（下载 ${percent}%）`);
    } else {
      parts.push(details.received_bytes ? `已接收 ${size}` : '等待来源响应，尚未收到文件数据');
    }
  }
  if (progress?.status === 'running') {
    parts.push('本阶段 ' + elapsedText(progress.stage_elapsed_seconds + sincePoll));
    if (details.timeout_seconds) parts.push(`本来源限时 ${details.timeout_seconds} 秒`);
  }
  parts.push('总耗时 ' + elapsedText(elapsed));
  if (notice) parts.push(notice);
  return parts.join(' · ');
}

async function runLongOperation(path, options, target) {
  const id = crypto.randomUUID().replaceAll('-', '');
  const start = Date.now();
  let stopped = false, progress = null, updatedAt = start, notice = '';
  let pollTimer, pollController;
  if (target) {
    operationViews.set(target, id);
    target.setAttribute('role', 'status');
  }
  const paint = () => {
    if (!stopped && target?.isConnected && operationViews.get(target) === id) {
      target.textContent = operationText(progress, (Date.now() - start) / 1000,
        (Date.now() - updatedAt) / 1000, notice);
    }
  };
  const poll = async () => {
    pollController = new AbortController();
    const timeout = setTimeout(() => pollController.abort(), 4000);
    try {
      const current = await api('/api/operations/' + id, { signal: pollController.signal, cache: 'no-store' });
      if (stopped) return;
      progress = current;
      updatedAt = Date.now();
      notice = '';
    } catch (error) {
      if (stopped) return;
      notice = error.status === 404
        ? (progress ? '进度记录已失效，服务可能重启；请先核对资料，勿重复提交' : '等待服务端接收并解析请求')
        : '暂时无法获取进度，正在重试；这不代表操作已停止';
    } finally {
      clearTimeout(timeout);
      if (!stopped) {
        paint();
        pollTimer = setTimeout(poll, 1000);
      }
    }
  };
  paint();
  const tick = setInterval(paint, 1000);
  const request = api(path + (path.includes('?') ? '&' : '?') + 'operation_id=' + id, options);
  void poll();
  try {
    return await request;
  } catch (error) {
    if (!error.status) error.message = '连接中断，操作结果尚未确认；服务端可能仍在执行，请先核对资料再重试。' + error.message;
    error.message += `（耗时 ${elapsedText((Date.now() - start) / 1000)}）`;
    throw error;
  } finally {
    stopped = true;
    clearInterval(tick);
    clearTimeout(pollTimer);
    pollController?.abort();
    if (target && operationViews.get(target) === id) operationViews.delete(target);
  }
}

$$('.tabs button').forEach(btn => btn.onclick = () => {
  $$('.tabs button').forEach(b => b.classList.toggle('active', b === btn));
  $$('.tab').forEach(t => t.classList.toggle('active', t.id === 'tab-' + btn.dataset.tab));
  history.replaceState(null, '', '#' + btn.dataset.tab);
  if (btn.dataset.mode) setLibraryMode(btn.dataset.mode);
  if (btn.dataset.tab === 'refs') initRefs();
  if (btn.dataset.tab === 'review') initReview();
  if (btn.dataset.tab === 'upload') loadOptions();
  applyListWidth(splitState.width);
});

let setupPrompted = false;

async function loadStatus() {
  try {
    const s = await api('/api/status');
    const label = s.remote_name || '远端';
    state.remoteName = label;
    PROJECT_SOURCE_LABELS.remote = label;
    const remoteBtn = $('#source-remote-btn');
    if (remoteBtn) remoteBtn.textContent = label;
    const pushBtn = $('#mirror-push-label');
    if (pushBtn) pushBtn.textContent = label + ' 缺失';
    const hint = $('#project-add-hint');
    if (hint) hint.textContent = '添加 ' + label + ' workshop 下的项目';
    $('#status .dot').className = 'dot ' + (s.ssh ? 'ok' : 'bad');
    if (!s.configured) {
      $('#status-text').textContent = '尚未配置远端服务器';
      $('#status-text').title = '';
      $('#setup-banner').classList.remove('hidden');
      $('#setup-banner-text').textContent = '尚未配置远端服务器，填写后才能同步论文与项目。';
      if (!setupPrompted) {
        setupPrompted = true;
        openSettings();
      }
      return;
    }
    $('#setup-banner').classList.add('hidden');
    $('#status-text').textContent = s.ssh
      ? `${label} 已连接 · 本机库` + (s.local_exists ? '已存在' : '未创建')
      : `${label} 连接失败`;
    $('#status-text').title = s.ssh_error || '';
  } catch (e) {
    $('#status .dot').className = 'dot bad';
    $('#status-text').textContent = '状态检查失败';
  }
}

function settingsPayload() {
  const connection = $('#settings-connection').value;
  const port = Number($('#settings-port').value.trim() || 8817);
  const sshPort = Number($('#settings-ssh-port').value.trim() || 22);
  return {
    host: state.settingsHost || '127.0.0.1',
    port: Number.isFinite(port) ? port : 0,
    local_library: $('#settings-local-library').value.trim(),
    review_space: $('#settings-review-space').value.trim(),
    remote_name: $('#settings-remote-name').value.trim(),
    ssh_alias: connection === 'alias' ? $('#settings-ssh-alias').value.trim() : '',
    ssh_host: connection === 'direct' ? $('#settings-ssh-host').value.trim() : '',
    ssh_user: connection === 'direct' ? $('#settings-ssh-user').value.trim() : '',
    ssh_port: Number.isFinite(sshPort) ? sshPort : 0,
    ssh_key: connection === 'direct' ? $('#settings-ssh-key').value.trim() : '',
    remote_library: $('#settings-remote-library').value.trim(),
    remote_workshop: $('#settings-remote-workshop').value.trim(),
  };
}

function syncSettingsConnection() {
  const alias = $('#settings-connection').value === 'alias';
  $('#settings-direct').classList.toggle('hidden', alias);
  $('#settings-alias').classList.toggle('hidden', !alias);
}

function renderSettingsChecks(result) {
  const rows = (result.checks || []).map(check => `
    <div class="settings-check ${check.ok ? 'ok' : 'fail'}">
      <b>${check.ok ? '通过' : '失败'}</b>
      <span>${esc(check.name)}${check.detail ? '：' + esc(check.detail) : ''}</span>
    </div>`).join('');
  $('#settings-checks').innerHTML = rows;
}

async function openSettings() {
  const modal = $('#settings-modal');
  $('#settings-checks').innerHTML = '';
  $('#settings-status').textContent = '正在读取配置…';
  modal.classList.remove('hidden');
  if (!modal.open) modal.showModal();
  document.body.classList.add('preview-open');
  try {
    const s = await api('/api/settings');
    state.settingsHost = s.host || '127.0.0.1';
    $('#settings-local-library').value = s.local_library || '';
    $('#settings-review-space').value = s.review_space || '';
    $('#settings-port').value = s.port || 8817;
    $('#settings-remote-name').value = s.remote_name || '';
    $('#settings-connection').value = s.connection === 'alias' ? 'alias' : 'direct';
    $('#settings-ssh-host').value = s.ssh_host || '';
    $('#settings-ssh-user').value = s.ssh_user || '';
    $('#settings-ssh-port').value = s.ssh_port || 22;
    $('#settings-ssh-key').value = s.ssh_key || '';
    $('#settings-ssh-alias').value = s.ssh_alias || '';
    $('#settings-remote-library').value = s.remote_library || '';
    $('#settings-remote-workshop').value = s.remote_workshop || '';
    syncSettingsConnection();
    $('#settings-status').textContent = s.configured ? '' : '尚未配置远端服务器';
  } catch (e) {
    $('#settings-status').textContent = '读取配置失败：' + e.message;
  }
}

function closeSettingsPanel() {
  const modal = $('#settings-modal');
  if (modal.open) modal.close();
  modal.classList.add('hidden');
  document.body.classList.remove('preview-open');
}

async function testSettings() {
  const button = $('#settings-test');
  button.disabled = true;
  $('#settings-checks').innerHTML = '';
  $('#settings-status').textContent = '正在检查连接…';
  try {
    const result = await api('/api/settings/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settingsPayload()),
    });
    renderSettingsChecks(result);
    $('#settings-status').textContent = result.ok ? '检查通过' : '有检查未通过';
  } catch (e) {
    $('#settings-status').textContent = '检查失败：' + e.message;
  } finally {
    button.disabled = false;
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const button = $('#settings-save');
  button.disabled = true;
  $('#settings-status').textContent = '正在保存…';
  try {
    const result = await api('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settingsPayload()),
    });
    if (!result.ok) throw new Error('保存失败');
    closeSettingsPanel();
    toast('设置已保存，请重启 papertex 服务后生效', 'ok');
    $('#status .dot').className = 'dot';
    $('#status-text').textContent = '设置已保存，重启后生效';
    $('#setup-banner').classList.remove('hidden');
    $('#setup-banner-text').textContent = '设置已保存，请重启 papertex 服务后生效。';
  } catch (e) {
    $('#settings-status').textContent = '保存失败：' + e.message;
  } finally {
    button.disabled = false;
  }
}

async function loadPdfToken() {  try {
    const r = await api('/api/pdf-token');
    if (typeof r.token === 'string') state.pdfToken = r.token;
  } catch {
    // 保留旧令牌；服务恢复后下次打开预览会再取一次
  }
}

function paperPdfUrl(source, topic, year, slug) {
  return '/api/pdf?source=' + encodeURIComponent(source) +
    '&topic=' + encodeURIComponent(topic) +
    '&year=' + encodeURIComponent(year) +
    '&slug=' + encodeURIComponent(slug) +
    '&token=' + encodeURIComponent(state.pdfToken || '');
}

function projectFileUrl(project, path, source = state.projectSource) {
  return '/api/project/file?project=' + encodeURIComponent(project) +
    '&source=' + encodeURIComponent(source) +
    '&path=' + encodeURIComponent(path) +
    '&token=' + encodeURIComponent(state.pdfToken || '');
}

/* ---------- 论文库 ---------- */

async function loadLibrary() {
  renderViewState($('#list'), 'loading', '正在读取论文库');
  try {
    const starsP = api('/api/stars').catch(() => ({ items: [] }));
    const [local, remote, knowledge] = await Promise.allSettled([
      api('/api/library?source=local'),
      api('/api/library?source=remote'),
      api('/api/knowledge', { cache: 'no-store' }),
      loadResearch(),
    ]);
    if (local.status === 'rejected') throw local.reason;
    state.remoteError = remote.status === 'rejected'
      ? (remote.reason?.message || '远端读取失败') : '';
    if (knowledge.status === 'fulfilled') {
      state.knowledge = Array.isArray(knowledge.value.items) ? knowledge.value.items : [];
      state.knowledgeError = knowledge.value.remote_error || '';
    } else {
      state.knowledge = [];
      state.knowledgeError = knowledge.reason?.message || '资料读取失败';
    }
    const blank = () => ({ has_pdf: false, pdf: '', has_source: false, has_note: false, entry: '', title: '', venue: '', code: '', openreview: '', doi: '', added_at: '' });
    const map = new Map();
    for (const it of local.value.items) {
      map.set(it.key, {
        key: it.key, kind: 'paper', topic: it.topic, year: it.year, slug: it.slug,
        local: {
          has_pdf: it.has_pdf, pdf: it.pdf, has_note: !!it.has_note,
          title: it.title || '', venue: it.venue || '',
          added_at: it.added_at || '',
        },
        remote: blank(),
      });
    }
    if (remote.status === 'fulfilled') {
      for (const it of remote.value.items) {
        const cur = map.get(it.key) || {
          key: it.key, kind: 'paper', topic: it.topic, year: it.year, slug: it.slug,
          local: { has_pdf: false, pdf: '', has_note: false, title: '', venue: '' }, remote: blank(),
        };
        cur.remote = {
          has_pdf: it.has_pdf, pdf: it.pdf, has_source: it.has_source, has_note: !!it.has_note,
          entry: it.entry, title: it.title, venue: it.venue, code: it.code || '',
          openreview: it.openreview || '', doi: it.doi || '', added_at: it.added_at || '',
        };
        map.set(it.key, cur);
      }
    }
    state.items = [...map.values()].sort((a, b) =>
      a.topic.toLowerCase().localeCompare(b.topic.toLowerCase()) ||
      a.year.localeCompare(b.year) || a.slug.localeCompare(b.slug));
    const stars = await starsP;
    state.stars = new Set(stars.items || []);
    populateFilters();
    renderList();
  } catch (e) {
    renderViewState($('#list'), 'error', '论文库读取失败', e.message, loadLibrary);
  }
}

/* ---------- 主题管理 ---------- */

let topicsOpener = null;

function topicNameError(name, old) {
  if (!name) return '主题名不能为空';
  if (name.length > 120) return '主题名不能超过 120 个字符';
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(name)) {
    return '只能用字母、数字、点、下划线或短横线，并以字母或数字开头';
  }
  if (name === old) return '新主题名与原名相同';
  return '';
}

function topicRowHtml(topic) {
  const name = String(topic?.name || '');
  const count = Number(topic?.count) || 0;
  const years = Array.isArray(topic?.years) ? topic.years : [];
  return `<div class="topic-row" data-topic="${esc(name)}">
    <div class="topic-main">
      <div class="slug">${esc(name)}</div>
      <div class="title">${count} 篇${years.length ? ' · ' + years.map(esc).join(', ') : ''}</div>
    </div>
    <div class="topic-actions">
      <button type="button" class="mini" data-action="rename">重命名</button>
      <button type="button" class="mini danger" data-action="delete">删除</button>
    </div>
  </div>`;
}

function renderTopics(topics) {
  const list = $('#topics-list');
  if (!topics.length) {
    renderViewState(list, 'empty', '还没有主题', '上传论文时会自动创建主题。');
    return;
  }
  list.innerHTML = topics.map(topicRowHtml).join('');
  $$('#topics-list [data-action]').forEach(button => {
    button.onclick = () => {
      const row = button.closest('.topic-row');
      if (button.dataset.action === 'rename') startTopicRename(row);
      else deleteTopic(row.dataset.topic);
    };
  });
}

async function loadTopics(force = false) {
  renderViewState($('#topics-list'), 'loading', '正在读取主题');
  try {
    const data = await api('/api/options' + (force ? '?force=1' : ''));
    renderTopics(data.topics || []);
  } catch (e) {
    renderViewState($('#topics-list'), 'error', '主题读取失败', e.message, () => loadTopics(true));
  }
}

async function openTopicsModal() {
  const modal = $('#topics-modal');
  const active = document.activeElement;
  topicsOpener = active && active !== document.body ? active : $('#topics-manage');
  $('#topics-result').textContent = '';
  modal.classList.remove('hidden');
  modal.showModal();
  document.body.classList.add('preview-open');
  $('#topics-modal-close').focus();
  await loadTopics(true);
}

function closeTopicsModal() {
  const modal = $('#topics-modal');
  if (!modal.open) return;
  modal.close();
  modal.classList.add('hidden');
  document.body.classList.remove('preview-open');
  if (topicsOpener?.isConnected) topicsOpener.focus();
}

function startTopicRename(row) {
  const topic = row.dataset.topic;
  row.innerHTML = `<form class="topic-rename" data-topic="${esc(topic)}">
    <input type="text" name="name" value="${esc(topic)}" maxlength="120" aria-label="新主题名">
    <button type="submit" class="mini primary">保存</button>
    <button type="button" class="mini ghost" data-cancel>取消</button>
    <span class="topic-rename-status muted"></span>
  </form>`;
  const form = row.querySelector('form');
  const input = form.querySelector('input');
  const status = form.querySelector('.topic-rename-status');
  input.focus();
  input.select();
  form.querySelector('[data-cancel]').onclick = () => loadTopics();
  form.onsubmit = async event => {
    event.preventDefault();
    const name = input.value.trim();
    const problem = topicNameError(name, topic);
    if (problem) {
      status.innerHTML = `<span class="error">${esc(problem)}</span>`;
      input.focus();
      return;
    }
    for (const element of form.elements) element.disabled = true;
    status.textContent = '处理中…';
    try {
      const outcome = await retryAfterResearchRefresh(() => runLongOperation('/api/topic/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic, new: name, revision: state.researchRevision }),
      }, $('#topics-result')));
      if (state.topicFilter === topic) state.topicFilter = name;
      if (state.current?.topic === topic) {
        state.current = null;
        $('#detail').innerHTML = '<p class="muted pad">主题已重命名，请重新选择论文</p>';
      }
      await Promise.all([loadTopics(true), loadLibrary(), loadOptions(true), loadResearch()]);
      $('#topics-result').innerHTML = `<span class="ok">已重命名为 ${esc(name)}</span>`;
      if (outcome.warnings?.length) toast(outcome.warnings.join('；'), 'warn');
    } catch (e) {
      $('#topics-result').textContent = '';
      status.innerHTML = `<span class="error">${esc(e.message)}</span>`;
      for (const element of form.elements) element.disabled = false;
      input.focus();
    }
  };
}

async function deleteTopic(topic) {
  const ok = await confirmDialog({
    title: `删除主题「${topic}」`,
    text: '本机和远端两侧目录都会移入回收区，不会自动清理。项目引用保留并标记缺失，星标一并移除。',
    confirmLabel: '移入回收区',
    expected: topic,
    inputLabel: '输入主题名确认',
  });
  if (!ok) return;
  try {
    await runLongOperation('/api/topic?topic=' + encodeURIComponent(topic) +
      '&confirm=' + encodeURIComponent(topic), { method: 'DELETE' }, $('#topics-result'));
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  if (state.topicFilter === topic) state.topicFilter = '';
  if (state.current?.topic === topic) {
    state.current = null;
    $('#detail').innerHTML = '<p class="muted pad">该主题已移入回收区，请重新选择论文</p>';
  }
  await Promise.all([loadTopics(true), loadLibrary(), loadOptions(true)]);
  $('#topics-result').innerHTML = '<span class="ok">已移入回收区</span>';
}

function isKnowledge(it) {
  return it?.kind === 'note' || it?.kind === 'link';
}

function itemTitle(it) {
  if (it?.kind === 'note' || it?.kind === 'link') return it.title || '';
  return it.remote?.title || it.local?.title || '';
}

function itemVenue(it) {
  const meta = it.remote?.venue || it.local?.venue;
  return meta || it.slug.split('-')[1] || '';
}

function itemAddedAt(it) {
  return it.remote?.added_at || it.local?.added_at || '';
}

function addedTime(it) {
  const t = Date.parse(itemAddedAt(it));
  return Number.isNaN(t) ? 0 : t;
}

function sortByAdded(items) {
  return [...items].sort((a, b) =>
    addedTime(b) - addedTime(a) || a.slug.localeCompare(b.slug));
}

function populateFilters() {
  const topics = [...new Set(state.items.map(i => i.topic))]
    .sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  const years = [...new Set(state.items.map(i => i.year))].sort().reverse();
  const venues = [...new Set(state.items.map(itemVenue).filter(Boolean))]
    .sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  const ts = $('#filter-topic');
  const ys = $('#filter-year');
  const vs = $('#filter-venue');
  ts.innerHTML = '<option value="">全部主题</option>' +
    topics.map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join('');
  ys.innerHTML = '<option value="">全部年份</option>' +
    years.map(y => `<option value="${esc(y)}">${esc(y)}</option>`).join('');
  vs.innerHTML = '<option value="">全部会议/期刊</option>' +
    venues.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
  if (topics.includes(state.topicFilter)) ts.value = state.topicFilter;
  else state.topicFilter = '';
  if (years.includes(state.yearFilter)) ys.value = state.yearFilter;
  else state.yearFilter = '';
  if (venues.includes(state.venueFilter)) vs.value = state.venueFilter;
  else state.venueFilter = '';
}

function knowledgeLabel(mode) {
  return mode === 'link' ? '链接' : '笔记';
}

function syncLibraryMode() {
  const mode = state.libraryMode;
  const paper = mode === 'paper';
  const label = knowledgeLabel(mode);
  const toggle = (sel, on) => $$(sel).forEach(el => el.classList.toggle('hidden', !on));
  toggle('#topics-manage, #mirror-fix, #dup-scan', paper);
  toggle('#expand-all, #collapse-all', paper && state.sortMode !== 'added');
  toggle('.filter-selects', paper);
  const sortSelect = $('#sort-mode');
  if (sortSelect) sortSelect.value = state.sortMode;
  toggle('.lib-bar-actions .bar-sep', paper);
  const gaps = $('#only-gaps')?.closest('label');
  if (gaps) gaps.classList.toggle('hidden', !paper);
  const add = $('#knowledge-add');
  if (add) {
    add.classList.toggle('hidden', paper);
    add.textContent = mode === 'link' ? '添加链接' : '添加笔记';
  }
  const search = $('#search');
  if (search) {
    search.placeholder = paper ? '搜索 slug / 标题 / 主题 / 年份'
      : `搜索${label}标题 / 来源 / 标签${mode === 'link' ? ' / 链接' : ''}`;
    search.setAttribute('aria-label', paper ? '搜索论文' : `搜索${label}`);
  }
  $$('.tabs button[data-mode]').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.mode === mode));
}

function detailPlaceholderHtml() {
  if (state.libraryMode === 'paper') {
    return '<div class="detail-placeholder"><strong>选择左侧论文查看详情</strong><p>支持 j / k 或方向键切换，Enter 打开。</p></div>';
  }
  const label = knowledgeLabel(state.libraryMode);
  return `<div class="detail-placeholder"><strong>选择左侧${label}查看详情</strong><p>支持 j / k 或方向键切换，Enter 打开。</p></div>`;
}

function setLibraryMode(mode, { keepDetail = false } = {}) {
  if (!LIBRARY_MODES.includes(mode)) return;
  if (mode === state.libraryMode) {
    syncLibraryMode();
    return;
  }
  state.libraryMode = mode;
  saveLibraryMode(mode);
  state.current = null;
  clearTimeout(searchTimer);
  state.search = '';
  $('#search').value = '';
  state.topicFilter = state.yearFilter = state.venueFilter = '';
  state.readingFilter = '';
  state.texFilter = '';
  state.tagFilter.clear();
  state.onlyGaps = false;
  const gaps = $('#only-gaps');
  if (gaps) gaps.checked = false;
  syncLibraryMode();
  renderList();
  if (!keepDetail) $('#detail').innerHTML = detailPlaceholderHtml();
}

function filtered() {
  const q = state.search.trim().toLowerCase();
  return state.items.filter(it => {
    if (state.topicFilter && it.topic !== state.topicFilter) return false;
    if (state.yearFilter && it.year !== state.yearFilter) return false;
    if (state.venueFilter && itemVenue(it) !== state.venueFilter) return false;
    if (state.onlyStarred && !state.stars.has(it.key)) return false;
    if (state.texFilter && !state.remoteError) {
      if (state.texFilter === 'yes' ? !it.remote.has_source : it.remote.has_source) return false;
    }
    if (state.readingFilter && !state.researchError) {
      const status = researchRecord(it.key).status || 'none';
      if (state.readingFilter === 'queue') {
        if (!['unread', 'reading', 'reproduce'].includes(status)) return false;
      } else if (status !== state.readingFilter) return false;
    }
    if (state.tagFilter.size && !state.researchError) {
      const tags = paperTags(it.key);
      if (![...state.tagFilter].some(tag => tags.includes(tag))) return false;
    }
    if (state.onlyGaps) {
      const l = it.local.has_pdf;
      const r = it.remote.has_pdf || it.remote.has_source;
      if (l === r) return false;
    }
    if (!q) return true;
    return (it.slug + ' ' + it.topic + ' ' + it.year + ' ' +
      itemTitle(it) + ' ' + itemVenue(it) + ' ' + paperTags(it.key).join(' '))
      .toLowerCase().includes(q);
  });
}

function filteredKnowledge() {
  const mode = state.libraryMode;
  if (mode === 'paper') return [];
  const q = state.search.trim().toLowerCase();
  return (state.knowledge || []).filter(it => {
    if (it.kind !== mode) return false;
    if (state.onlyStarred && !state.stars.has(it.key)) return false;
    if (!q) return true;
    return [it.title, it.source, it.url, ...(it.tags || [])].join(' ').toLowerCase().includes(q);
  });
}

function knowledgeItemHtml(it) {
  const active = state.current && state.current.key === it.key;
  const starred = state.stars.has(it.key);
  const picked = state.selected?.has(it.key);
  const sub = [it.source, ...(it.tags || []).slice(0, 3)].filter(Boolean).join(' · ');
  return `
    <div class="item knowledge ${active ? 'active' : ''} ${picked ? 'picked' : ''}" data-key="${esc(it.key)}"
         role="button" tabindex="0" aria-current="${active ? 'true' : 'false'}">
      ${state.selectMode ? pickBoxHtml(it.key, it.title || '资料') : ''}
      <div class="item-main">
        <div class="item-slug">${esc(it.title || '(未命名资料)')}</div>
        <div class="item-sub">${esc(sub || it.url || '')}</div>
      </div>
      <button class="star ${starred ? 'on' : ''}" data-star="${esc(it.key)}"
              aria-label="${starred ? '取消星标' : '星标'}" aria-pressed="${starred ? 'true' : 'false'}"
              title="${starred ? '取消星标' : '星标'}">${starred ? '★' : '☆'}</button>
    </div>`;
}

function compareBadges(it) {
  const l = it.local.has_pdf;
  const r = it.remote.has_pdf;
  const remoteAny = r || it.remote.has_source;
  const gapsKnown = !state.remoteError;
  return `
    ${l ? '<span class="badge pdf">本机</span>' : ''}
    ${r ? `<span class="badge pdf">${esc(remoteLabel())}</span>` : ''}
    ${it.remote.has_source ? '<span class="badge tex">TEX</span>' : ''}
    ${it.local.has_note || it.remote.has_note ? '<span class="badge note" title="已有论文笔记 note.md">Note</span>' : ''}
    ${/^https?:\/\//i.test(it.remote.code || '') ? '<span class="badge code">CODE</span>' : ''}
    ${gapsKnown && l && !remoteAny ? '<span class="badge warn">仅本机</span>' : ''}
    ${gapsKnown && !l && remoteAny ? `<span class="badge warn">仅 ${esc(remoteLabel())}</span>` : ''}`;
}

/* ---------- 资料多选与批量加入项目 ---------- */

function pickKeys() {
  const items = state.libraryMode === 'paper' ? filtered() : filteredKnowledge();
  return items.map(it => it.key);
}

function togglePick(key) {
  if (!state.selected) state.selected = new Set();
  const on = !state.selected.has(key);
  if (on) state.selected.add(key);
  else state.selected.delete(key);
  return on;
}

function pickBoxHtml(key, label) {
  const on = state.selected?.has(key);
  return `<input type="checkbox" class="pick" data-pick="${esc(key)}"${on ? ' checked' : ''}`
    + ` aria-label="选择 ${esc(label)}">`;
}

function bulkSummaryText(result = {}) {
  const added = (result.added || []).length;
  const updated = (result.updated || []).length;
  const skipped = (result.skipped || []).length;
  const failed = result.failed || [];
  const parts = [];
  if (added) parts.push(`已加入 ${added} 项`);
  if (updated) parts.push(`已更新 ${updated} 项`);
  if (skipped) parts.push(`跳过 ${skipped} 项（项目里已有）`);
  if (failed.length) {
    const first = failed[0] || {};
    const reason = first.message ? `${first.path}：${first.message}` : String(first.path || '');
    parts.push(`失败 ${failed.length} 项${reason ? `（${reason}）` : ''}`);
  }
  return parts.join('，') || '没有需要加入的内容';
}

function updateBulkBar() {
  const toggle = $('#select-toggle');
  if (toggle) {
    toggle.setAttribute('aria-pressed', state.selectMode ? 'true' : 'false');
    toggle.classList.toggle('active', state.selectMode);
    toggle.textContent = state.selectMode ? '多选中' : '多选';
  }
  const bar = $('#bulk-bar');
  if (bar) bar.classList.toggle('hidden', !state.selectMode);
  const selected = state.selected || new Set();
  const count = $('#bulk-count');
  if (count) count.textContent = `已选 ${selected.size} 项`;
  const add = $('#bulk-add-project');
  if (add) add.disabled = selected.size === 0;
}

function setSelectMode(on) {
  state.selectMode = !!on;
  if (!state.selectMode) state.selected.clear();
  updateBulkBar();
  renderList();
}

function groupPickHtml(attr, value, label) {
  return `<input type="checkbox" class="group-pick" data-pick-${attr}="${esc(value)}"`
    + ` aria-label="${esc(label)}" title="选择整个${attr === 'topic' ? '主题' : '年份'}">`;
}

function groupItemKeys(el) {
  const body = el?.nextElementSibling;
  if (!body || !body.querySelectorAll) return [];
  return [...body.querySelectorAll('.item')].map(item => item.dataset.key).filter(Boolean);
}

function applyGroupSelection(keys) {
  if (!state.selected) state.selected = new Set();
  const all = keys.length > 0 && keys.every(key => state.selected.has(key));
  for (const key of keys) {
    if (all) state.selected.delete(key);
    else state.selected.add(key);
  }
  return !all;
}

function toggleGroupPick(box) {
  const host = box.closest('.group-topic, .group-year');
  applyGroupSelection(groupItemKeys(host));
  syncPickUI();
}

function syncGroupPickUI() {
  $$('#list .group-pick').forEach(box => {
    const keys = groupItemKeys(box.closest('.group-topic, .group-year'));
    const chosen = keys.filter(key => state.selected?.has(key)).length;
    box.checked = keys.length > 0 && chosen === keys.length;
    box.indeterminate = chosen > 0 && chosen < keys.length;
  });
}

function syncPickUI() {
  $$('#list .item').forEach(el => {
    const on = state.selected?.has(el.dataset.key) ?? false;
    el.classList.toggle('picked', on);
    const box = el.querySelector('.pick');
    if (box) box.checked = on;
  });
  syncGroupPickUI();
  updateBulkBar();
}

function clearPicks() {
  if (!state.selected) state.selected = new Set();
  state.selected.clear();
  syncPickUI();
}

function selectVisible() {
  if (!state.selected) state.selected = new Set();
  for (const key of pickKeys()) state.selected.add(key);
  syncPickUI();
  toast(`已选中当前筛选的 ${state.selected.size} 项`, 'info');
}

function itemHtml(it, opts = {}) {
  if (it?.kind === 'note' || it?.kind === 'link') return knowledgeItemHtml(it);
  const title = itemTitle(it);
  const active = state.current && state.current.key === it.key;
  const picked = state.selected?.has(it.key);
  const added = opts.context ? knowledgeDate(itemAddedAt(it)) : '';
  const context = opts.context
    ? `<div class="item-sub">${esc(it.topic)} · ${esc(it.year)}${added ? ' · 添加于 ' + esc(added) : ''}</div>`
    : '';
  return `
    <div class="item ${active ? 'active' : ''} ${picked ? 'picked' : ''}" data-key="${esc(it.key)}"
         role="button" tabindex="0" aria-current="${active ? 'true' : 'false'}">
      ${state.selectMode ? pickBoxHtml(it.key, it.slug) : ''}
      <div class="item-main">
        <div class="item-slug">${esc(it.slug)}</div>
        ${title ? `<div class="item-title">${esc(displayTitle(title))}</div>` : ''}
        ${context}
        <div class="research-badges">${researchBadge(it.key)}</div>
      </div>
      <button class="star ${state.stars.has(it.key) ? 'on' : ''}" data-star="${esc(it.key)}"
              aria-label="${state.stars.has(it.key) ? '取消星标' : '星标'}"
              aria-pressed="${state.stars.has(it.key) ? 'true' : 'false'}"
              title="${state.stars.has(it.key) ? '取消星标' : '星标'}">${state.stars.has(it.key) ? '★' : '☆'}</button>
      <div class="badges">${compareBadges(it)}</div>
    </div>`;
}

function bindListItems(focusedKey) {
  $$('#list [data-toggle]').forEach(el => el.onclick = e => {
    e.stopPropagation();
    const key = el.dataset.toggle;
    if (state.collapsed.has(key)) state.collapsed.delete(key);
    else state.collapsed.add(key);
    saveCollapsed();
    renderList();
  });
  $$('#list .star').forEach(btn => btn.onclick = e => {
    e.stopPropagation();
    toggleStar(btn.dataset.star);
  });
  $$('#list .pick').forEach(box => {
    box.onclick = e => e.stopPropagation();
    box.onchange = () => {
      togglePick(box.dataset.pick);
      syncPickUI();
    };
  });
  $$('#list .group-pick').forEach(box => {
    box.onclick = e => e.stopPropagation();
    box.onchange = () => toggleGroupPick(box);
  });
  $$('#list .item').forEach(el => {
    const select = () => {
      if (state.selectMode) {
        togglePick(el.dataset.key);
        syncPickUI();
        return;
      }
      const it = state.items.find(x => x.key === el.dataset.key) ||
        (state.knowledge || []).find(x => x.key === el.dataset.key);
      if (!it) return;
      state.current = it;
      renderList();
      if (isKnowledge(it)) showKnowledgeDetail(it);
      else showDetail(it);
    };
    el.onclick = select;
    el.onkeydown = event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      select();
    };
  });
  if (focusedKey) {
    const again = $$('#list .item').find(el => el.dataset.key === focusedKey);
    if (again && again.offsetParent !== null) again.focus({ preventScroll: true });
  }
}

function renderKnowledgeList(count, starCount, focusedKey) {
  const mode = state.libraryMode;
  const label = knowledgeLabel(mode);
  const items = filteredKnowledge();
  const all = (state.knowledge || []).filter(it => it.kind === mode);
  const head = items.length === all.length
    ? `共 ${all.length} 条${label}` : `${items.length} / ${all.length} 条${label}`;
  count.textContent = state.knowledgeError
    ? `${head} · 远端离线，显示本机缓存` : head;
  count.title = state.knowledgeError || '';
  count.classList.toggle('warn-text', !!state.knowledgeError);
  if (starCount) {
    const starred = all.filter(it => state.stars.has(it.key)).length;
    starCount.textContent = starred ? `(${starred})` : '';
  }
  if (!all.length) {
    renderViewState($('#list'), 'empty', `还没有${label}`,
      mode === 'link'
        ? '点「添加链接」保存第一条链接。'
        : '点「添加笔记」新建第一条笔记，可导入 Markdown。');
    return;
  }
  if (!items.length) {
    renderViewState($('#list'), 'empty', `没有匹配的${label}`, '试试其他关键词，或取消星标筛选。');
    return;
  }
  const sorted = [...items].sort((a, b) =>
    String(b.updated_at || '').localeCompare(String(a.updated_at || '')) ||
    String(a.title || '').localeCompare(String(b.title || '')));
  $('#list').innerHTML = sorted.map(knowledgeItemHtml).join('');
  bindListItems(focusedKey);
}

function renderList() {
  updateBulkBar();
  const gapsBox = $('#only-gaps');
  if (gapsBox) {
    if (state.remoteError) {
      state.onlyGaps = false;
      gapsBox.checked = false;
    }
    gapsBox.disabled = !!state.remoteError;
  }
  const texBox = $('#filter-tex');
  if (texBox) {
    if (state.remoteError) {
      state.texFilter = '';
      texBox.value = '';
    }
    texBox.disabled = !!state.remoteError;
  }
  const count = $('#count');
  const starCount = $('#star-count');
  const focusedKey = document.activeElement?.closest?.('#list .item')?.dataset.key || '';
  renderTagFilters();
  if (state.libraryMode !== 'paper') {
    renderKnowledgeList(count, starCount, focusedKey);
    return;
  }
  const items = filtered();
  const all = state.items;
  const paperHead = items.length === all.length
    ? `${all.length} 篇论文` : `${items.length} / ${all.length} 篇论文`;
  if (state.remoteError) {
    count.textContent = `${paperHead} · 远端离线，仅显示本机镜像`;
  } else {
    const remoteAny = it => it.remote.has_pdf || it.remote.has_source;
    const both = all.filter(it => it.local.has_pdf && remoteAny(it)).length;
    const onlyL = all.filter(it => it.local.has_pdf && !remoteAny(it)).length;
    const onlyR = all.filter(it => !it.local.has_pdf && remoteAny(it)).length;
    count.textContent = `共 ${paperHead} · 两侧都有 ${both} · 仅本机 ${onlyL} · 仅 ${remoteLabel()} ${onlyR}`;
  }
  count.title = state.remoteError || '';
  count.classList.toggle('warn-text', !!state.remoteError);
  if (starCount) {
    const starred = all.filter(it => state.stars.has(it.key)).length;
    starCount.textContent = starred ? `(${starred})` : '';
  }
  if (!items.length) {
    renderViewState($('#list'), 'empty',
      all.length ? '没有匹配的论文' : '论文库还是空的',
      all.length ? '试试其他关键词，或减少筛选条件。' : '在「上传」里导入第一篇论文。');
    return;
  }

  if (state.sortMode === 'added') {
    $('#list').innerHTML = sortByAdded(items)
      .map(it => itemHtml(it, { context: true })).join('');
    bindListItems(focusedKey);
    return;
  }

  const tree = new Map();
  for (const it of items) {
    if (!tree.has(it.topic)) tree.set(it.topic, new Map());
    const years = tree.get(it.topic);
    if (!years.has(it.year)) years.set(it.year, []);
    years.get(it.year).push(it);
  }

  const searching = state.search.trim() !== '';
  $('#list').innerHTML = [...tree.entries()].map(([topic, years]) => {
    const total = [...years.values()].reduce((n, arr) => n + arr.length, 0);
    const topicOpen = searching || !state.collapsed.has(topic);
    const yearsHtml = [...years.entries()]
      .sort((a, b) => b[0].localeCompare(a[0]))
      .map(([year, arr]) => {
        const yKey = topic + '/' + year;
        const yearOpen = searching || !state.collapsed.has(yKey);
        return `
          <div class="group-year ${yearOpen ? 'open' : ''}" data-toggle="${esc(yKey)}">
            ${state.selectMode ? groupPickHtml('year', yKey, `选择 ${topic} ${year} 年`) : ''}
            <span class="chev"></span><span>${esc(year)}</span>
            <span class="count">${arr.length}</span>
          </div>
          <div class="group-items${yearOpen ? '' : ' hidden'}">${arr.map(itemHtml).join('')}</div>`;
      }).join('');
    return `
      <div class="group-topic ${topicOpen ? 'open' : ''}" data-toggle="${esc(topic)}">
        ${state.selectMode ? groupPickHtml('topic', topic, `选择主题 ${topic}`) : ''}
        <span class="chev"></span><span>${esc(topic)}</span>
        <span class="count">${total}</span>
      </div>
      <div class="group-years${topicOpen ? '' : ' hidden'}">${yearsHtml}</div>`;
  }).join('');
  bindListItems(focusedKey);
}

function visibleListItems() {
  return $$('#list .item').filter(el => el.offsetParent !== null);
}

function moveListSelection(delta) {
  const items = visibleListItems();
  if (!items.length) return;
  const index = items.indexOf(document.activeElement);
  const next = index < 0
    ? (delta > 0 ? 0 : items.length - 1)
    : Math.min(items.length - 1, Math.max(0, index + delta));
  items[next].focus();
  items[next].scrollIntoView({ block: 'nearest' });
}

function isTypingTarget(el) {
  if (!el) return false;
  return el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' ||
    el.tagName === 'SELECT' || el.isContentEditable === true;
}

const pendingSupplements = new Set();

function setSupplementDisabled(disabled) {
  $$('#btn-add-pdf, #btn-add-tex').forEach(button => {
    button.disabled = disabled;
    button.classList?.toggle('disabled', disabled);
  });
}

async function supplementFile(kind, file, it) {
  if (pendingSupplements.has(it.key)) {
    toast('这篇论文正在补传，请等待完成后再提交；本次文件尚未上传。', 'warn');
    return false;
  }
  pendingSupplements.add(it.key);
  setSupplementDisabled(true);
  const fd = new FormData();
  fd.set('topic', it.topic);
  fd.set('year', it.year);
  fd.set('slug', it.slug);
  fd.set(kind === 'pdf' ? 'pdf' : 'source_archive', file);
  const st = $('#detail-status');
  if (st) st.textContent = '上传中…';
  try {
    await runLongOperation('/api/supplement', { method: 'POST', body: fd }, st);
    state.metaCache.delete(it.key);
    await loadLibrary();
    const updated = state.items.find(x => x.key === it.key) || it;
    state.current = updated;
    renderList();
    showDetail(updated);
    const st2 = $('#detail-status');
    if (st2) {
      st2.textContent = kind === 'pdf' ? 'pdf 已更新' : 'tex 已补充到远端';
    }
    return true;
  } catch (e) {
    if (st) st.textContent = e.message;
    toast(e.message, 'error');
    return false;
  } finally {
    pendingSupplements.delete(it.key);
    if (state.current?.key === it.key) setSupplementDisabled(false);
  }
}

function editPick(kind) {
  const sel = $('#edit-' + kind);
  if (!sel) return '';
  return sel.value === '__new__' ? ($('#edit-' + kind + '-new').value || '').trim() : sel.value;
}

function editToggle(kind) {
  const sel = $('#edit-' + kind);
  const input = $('#edit-' + kind + '-new');
  if (!sel || !input) return;
  const isNew = sel.value === '__new__';
  input.classList.toggle('hidden', !isNew);
  if (isNew) input.focus();
}

const metaRequests = new Map();

function paperMeta(it) {
  if (state.metaCache.has(it.key)) return Promise.resolve(state.metaCache.get(it.key));
  if (!metaRequests.has(it.key)) {
    const request = api('/api/meta?topic=' + encodeURIComponent(it.topic) +
      '&year=' + encodeURIComponent(it.year) + '&slug=' + encodeURIComponent(it.slug))
      .then(meta => {
        const value = meta && typeof meta === 'object' ? meta : {};
        state.metaCache.set(it.key, value);
        return value;
      })
      .finally(() => metaRequests.delete(it.key));
    metaRequests.set(it.key, request);
  }
  return metaRequests.get(it.key);
}

async function openEditor(it) {
  const button = $('#btn-edit');
  const status = $('#detail-status');
  const cached = state.metaCache.has(it.key);
  if (button && !cached) {
    button.disabled = true;
    button.textContent = '读取中…';
  }
  if (status && !cached) status.textContent = '正在读取远端元数据…';
  let meta = {};
  try {
    meta = await paperMeta(it);
  } catch (e) {
    if (button) {
      button.disabled = false;
      button.textContent = '编辑';
    }
    if (status) status.textContent = '';
    toast(e.message, 'error');
    return;
  }
  const base = {
    slug: it.slug, topic: it.topic, year: Number(it.year),
    title: it.title || '', venue: it.venue || '', arxiv: '', entry: it.entry || '',
    ...meta,
  };
  const topicList = [...new Set([...state.items.map(x => x.topic), base.topic])]
    .sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  const yearList = [...new Set([...state.items.map(x => String(x.year)),
    String(new Date().getFullYear()), String(base.year)])].sort().reverse();

  $('#detail').innerHTML = `
    <h3>编辑 ${esc(it.slug)}</h3>
    <div class="card form edit-form">
      <div class="row">
        <label>主题
          <select id="edit-topic">
            ${topicList.map(t => `<option value="${esc(t)}" ${t === base.topic ? 'selected' : ''}>${esc(t)}</option>`).join('')}
            <option value="__new__">＋ 新建</option>
          </select>
          <input id="edit-topic-new" class="hidden" placeholder="新主题">
        </label>
        <label>年份
          <select id="edit-year">
            ${yearList.map(y => `<option value="${esc(y)}" ${y === String(base.year) ? 'selected' : ''}>${esc(y)}</option>`).join('')}
            <option value="__new__">＋ 新建</option>
          </select>
          <input id="edit-year-new" class="hidden" placeholder="新年份">
        </label>
      </div>
      <div class="row">
        <label>slug<input id="edit-slug" value="${esc(base.slug)}"></label>
        <label>会议/期刊<input id="edit-venue" value="${esc(base.venue)}"></label>
      </div>
      <div class="row">
        <label>标题<input id="edit-title" value="${esc(base.title)}"></label>
        <label>arXiv<input id="edit-arxiv" value="${esc(base.arxiv || '')}"></label>
      </div>
      <div class="row">
        <label>入口 tex<input id="edit-entry" value="${esc(base.entry)}" placeholder="如 source/main.tex"></label>
        <label>代码链接<input id="edit-code" value="${esc(base.code || '')}" placeholder="https://github.com/…"></label>
      </div>
      <div class="row">
        <label>OpenReview 链接<input id="edit-openreview" value="${esc(base.openreview || '')}" placeholder="https://openreview.net/forum?id=…"></label>
        <label>DOI<input id="edit-doi" value="${esc(base.doi || '')}" placeholder="10.1109/…"></label>
      </div>
      <div class="actions-row">
        <button type="button" class="mini primary" id="edit-save">保存</button>
        <button type="button" class="mini" id="edit-raw">原始 JSON</button>
        <button type="button" class="mini" id="edit-cancel">取消</button>
        <span id="edit-status" class="muted"></span>
      </div>
    </div>`;

  ['topic', 'year'].forEach(k => { $('#edit-' + k).onchange = () => editToggle(k); });
  $('#edit-cancel').onclick = () => showDetail(it);

  const collect = () => {
    let slug = $('#edit-slug').value.trim();
    const year = editPick('year');
    if (/^\d{4}-/.test(slug) && !slug.startsWith(year + '-')) slug = year + slug.slice(4);
    return {
      ...base,
      topic: editPick('topic'),
      year: Number(year),
      slug,
      title: $('#edit-title').value.trim(),
      venue: $('#edit-venue').value.trim(),
      arxiv: $('#edit-arxiv').value.trim(),
      entry: $('#edit-entry').value.trim(),
      code: $('#edit-code').value.trim(),
      openreview: $('#edit-openreview').value.trim(),
      doi: $('#edit-doi').value.trim(),
    };
  };

  const save = async (metaObj) => {
    const st = $('#edit-status');
    if (st) st.textContent = '保存中…';
    try {
      const r = await retryAfterResearchRefresh(() => runLongOperation('/api/paper/edit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          old: { topic: it.topic, year: it.year, slug: it.slug },
          meta: metaObj, revision: state.researchRevision,
        }),
      }, st));
      state.metaCache.delete(it.key);
      if (r.key) state.metaCache.delete(r.key);
      if (r.key && r.key !== it.key) await loadResearch();
      await loadLibrary();
      const updated = state.items.find(x => x.key === r.key);
      state.current = updated || null;
      renderList();
      if (updated) showDetail(updated);
      else $('#detail').innerHTML = '<p class="muted pad">已保存</p>';
      if (r.warnings?.length) toast(r.warnings.join('；'), 'warn');
    } catch (e) {
      const st2 = $('#edit-status');
      const message = researchConflict(e)
        ? '研究记录刚被其他操作（如批量导入）更新，已自动刷新；请再点一次「保存」'
        : e.message;
      if (st2) st2.innerHTML = `<span class="error">${esc(message)}</span>`;
      else toast(message, 'error');
    }
  };

  $('#edit-save').onclick = () => save(collect());

  $('#edit-raw').onclick = () => {
    const raw = JSON.stringify(collect(), null, 2);
    $('#detail').innerHTML = `
      <h3>编辑 meta.json</h3>
      <div class="card form edit-form">
        <textarea id="raw-meta" class="raw" spellcheck="false"></textarea>
        <div class="actions-row">
          <button type="button" class="mini primary" id="raw-save">保存</button>
          <button type="button" class="mini" id="raw-back">返回表单</button>
          <span id="edit-status" class="muted"></span>
        </div>
      </div>`;
    $('#raw-meta').value = raw;
    $('#raw-back').onclick = () => openEditor(it);
    $('#raw-save').onclick = () => {
      let parsed;
      try {
        parsed = JSON.parse($('#raw-meta').value);
      } catch (e) {
        $('#edit-status').innerHTML = `<span class="error">JSON 解析失败: ${esc(e.message)}</span>`;
        return;
      }
      save(parsed);
    };
  };
}

function syncStarButtons() {
  $$('[data-star]').forEach(el => {
    const on = state.stars.has(el.dataset.star);
    el.classList.toggle('on', on);
    el.setAttribute('aria-pressed', on ? 'true' : 'false');
    if (el.id === 'btn-star') el.textContent = on ? '★ 已星标' : '☆ 星标';
    else {
      el.textContent = on ? '★' : '☆';
      el.title = on ? '取消星标' : '星标';
      el.setAttribute('aria-pressed', on ? 'true' : 'false');
      el.setAttribute('aria-label', on ? '取消星标' : '星标');
    }
  });
}

async function toggleStar(key) {
  const starred = !state.stars.has(key);
  if (starred) state.stars.add(key);
  else state.stars.delete(key);
  syncStarButtons();
  try {
    await api('/api/star', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key, starred }),
    });
  } catch (e) {
    if (starred) state.stars.delete(key);
    else state.stars.add(key);
    syncStarButtons();
    toast(e.message, 'error');
  }
  $('#star-count').textContent = state.stars.size ? `(${state.stars.size})` : '';
  if (state.onlyStarred) renderList();
}

async function fetchProjects(source, force = false) {
  const src = normalizeProjectSource(source);
  if (state.projectsLoaded[src] && !force) return state.projectsCache[src];
  const data = await api('/api/projects?source=' + src);
  state.projectsCache[src] = sortProjectsByUsage(data.items || []);
  state.projectsLoaded[src] = true;
  if (src === state.projectSource) state.projects = state.projectsCache[src];
  return state.projectsCache[src];
}

async function loadProjects(force = false) {
  return fetchProjects(state.projectSource, force);
}

function invalidateProjects(source) {
  if (source) state.projectsLoaded[normalizeProjectSource(source)] = false;
  else {
    state.projectsLoaded.remote = false;
    state.projectsLoaded.local = false;
  }
}

function rememberProjectSelection() {
  state.projectSelection[state.projectSource] = state.project;
}

const PROJECT_USAGE_KEY = 'papertex.projectUsage';

function loadProjectUsage() {
  try {
    const data = JSON.parse(localStorage.getItem(PROJECT_USAGE_KEY) || '{}');
    return data && typeof data === 'object' ? data : {};
  } catch {
    return {};
  }
}

function touchProject(project) {
  if (!project) return;
  const usage = loadProjectUsage();
  usage[project] = Date.now();
  try { localStorage.setItem(PROJECT_USAGE_KEY, JSON.stringify(usage)); } catch {}
}

function sortProjectsByUsage(items) {
  const usage = loadProjectUsage();
  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => ((usage[b.item.project] || 0) - (usage[a.item.project] || 0)) ||
      (a.index - b.index))
    .map(entry => entry.item);
}

function addToProjectHtml(enabled, note = '') {
  return `
    <div class="actions-row">
      <label class="inline">加入项目
        <select id="add-proj-select" ${enabled ? '' : 'disabled'}>
          <option value="">${enabled ? '正在读取项目…' : '选择项目…'}</option>
        </select>
      </label>
      <button class="mini" id="btn-add-proj" disabled>加入</button>
      <span id="add-proj-status" class="muted">${esc(note)}</span>
    </div>`;
}

async function loadProjectChoices() {
  const sources = ['remote', 'local'];
  const results = await Promise.allSettled(sources.map(src => fetchProjects(src)));
  const choices = [];
  let html = '<option value="">选择项目…</option>';
  let failures = 0;
  results.forEach((result, index) => {
    if (result.status === 'rejected') { failures += 1; return; }
    const items = result.value;
    if (!items.length) return;
    html += `<optgroup label="${esc(projectSourceLabel(sources[index]))}">`;
    for (const item of items) {
      const id = choices.length;
      choices.push({ source: sources[index], project: item.project });
      html += `<option value="${id}">${esc(item.project)}</option>`;
    }
    html += '</optgroup>';
  });
  return { choices, html, failures };
}

async function bindAddToProject(it) {
  const sel = $('#add-proj-select');
  const btn = $('#btn-add-proj');
  const st = $('#add-proj-status');
  if (!sel || !btn) return;
  const note = st ? st.textContent : '';
  sel.disabled = true;
  btn.disabled = true;
  if (st && !note) st.textContent = '正在读取项目…';
  let choices = [];
  try {
    const loaded = await loadProjectChoices();
    choices = loaded.choices;
    sel.innerHTML = loaded.html;
    sel.disabled = false;
    if (!choices.length) {
      st.textContent = loaded.failures ? '项目读取失败，请稍后重试' : '还没有项目，先到「项目引用」添加';
      return;
    }
    btn.disabled = false;
    if (st) st.textContent = note;
  } catch (e) {
    sel.innerHTML = '<option value="">选择项目…</option>';
    sel.disabled = false;
    st.textContent = e.message;
    return;
  }
  const picked = () => choices[Number(sel.value)] || null;
  const addRef = (choice, extra) => runLongOperation('/api/refs/add', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project: choice.project, source: choice.source,
      path: it.key, ...extra }),
  }, st);
  btn.onclick = async () => {
    const choice = picked();
    if (!choice) {
      st.textContent = '先选一个项目';
      return;
    }
    const project = choice.project;
    st.textContent = '处理中…';
    try {
      let r;
      try {
        r = await addRef(choice);
      } catch (e) {
        if (!(choice.source === 'remote' && e.status === 409 && e.detail?.missing_dir)) throw e;
        st.textContent = '';
        const ok = await confirmDialog({
          title: '远端还没有这个项目目录',
          text: `workshop 下不存在 ${project}。确认后会创建该目录和 reference-tex.json，再把条目加入引用。`,
          confirmLabel: '创建目录并加入',
          danger: false,
        });
        if (!ok) {
          st.textContent = '已取消，未创建目录';
          return;
        }
        st.textContent = '正在创建目录…';
        r = await addRef(choice, { create_missing: true });
      }
      const action = r.action === 'added' ? '已加入' : '已更新';
      st.innerHTML = `<span class="ok">${action} ${esc(projectSourceLabel(choice.source))} ${esc(project)}${r.created_dir ? '（已创建目录）' : ''}</span>`;
      touchProject(project);
      invalidateProjects(choice.source);
    } catch (e) {
      if (e.status === 409 && e.detail && e.detail.existing) {
        const ex = e.detail.existing;
        const isKnowledge = String(it.key).startsWith('_knowledge/');
        const detail = isKnowledge
          ? `（${ex.kind === 'link' ? '链接' : '笔记'} · ${esc(ex.entry || ex.url || '无')}）`
          : `（entry ${esc(ex.entry || '无')} · pdf ${esc(ex.pdf || '无')}）`;
        st.innerHTML =
          `<span class="warn-text">${esc(project)} 已包含这${isKnowledge ? '条资料' : '篇论文'}</span>` +
          ` <span class="muted">${detail}</span>` +
          ' <button class="mini" id="btn-upd-ref">用库中最新信息更新</button>' +
          ' <button class="mini ghost" id="btn-cancel-ref">取消</button>';
        $('#btn-cancel-ref').onclick = () => { st.textContent = '未修改'; };
        $('#btn-upd-ref').onclick = async () => {
          st.textContent = '更新中…';
          try {
            await runLongOperation('/api/refs/add', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ project, source: choice.source, path: it.key, update: true }),
            }, st);
            st.innerHTML = `<span class="ok">已按库更新 ${esc(project)}</span>`;
            invalidateProjects(choice.source);
          } catch (e2) {
            st.innerHTML = `<span class="error">${esc(e2.message)}</span>`;
          }
        };
      } else {
        st.innerHTML = `<span class="error">${esc(e.message)}</span>`;
      }
    }
  };
}

let bulkChoices = [];

function closeBulkModal() {
  const modal = $('#bulk-modal');
  if (!modal) return;
  if (modal.open) modal.close();
  modal.classList.add('hidden');
}

async function openBulkProjectModal() {
  const paths = [...state.selected];
  const modal = $('#bulk-modal');
  const sel = $('#bulk-project');
  const st = $('#bulk-status');
  const ok = $('#bulk-ok');
  const hint = $('#bulk-modal-hint');
  if (!paths.length || !modal || !sel || !st || !ok) return;
  if (typeof modal.showModal !== 'function') {
    toast('当前浏览器不支持该对话框，请改用单个条目的「加入项目」', 'warn');
    return;
  }
  bulkChoices = [];
  if (hint) hint.textContent = `把已选的 ${paths.length} 项加入项目引用清单；项目里已有的会自动跳过。`;
  st.textContent = '正在读取项目…';
  st.classList.remove('warn-text');
  ok.disabled = true;
  ok.textContent = '加入';
  sel.disabled = true;
  sel.innerHTML = '<option value="">选择项目…</option>';
  modal.classList.remove('hidden');
  if (!modal.open) modal.showModal();
  try {
    const loaded = await loadProjectChoices();
    bulkChoices = loaded.choices;
    sel.innerHTML = loaded.html;
    if (!loaded.choices.length) {
      st.textContent = loaded.failures ? '项目读取失败，请稍后重试'
        : '还没有项目，先到「项目引用」添加';
      return;
    }
    sel.disabled = false;
    ok.disabled = false;
    st.textContent = loaded.failures ? '部分来源的项目读取失败；已有项目仍可选择' : '';
  } catch (e) {
    st.textContent = e.message;
    st.classList.add('warn-text');
  }
}

async function submitBulkAdd() {
  const sel = $('#bulk-project');
  const st = $('#bulk-status');
  const ok = $('#bulk-ok');
  if (!sel || !st || !ok) return;
  const choice = bulkChoices[Number(sel.value)] || null;
  if (!choice) {
    st.textContent = '先选一个项目';
    return;
  }
  const paths = [...state.selected];
  if (!paths.length) {
    st.textContent = '没有选中的条目';
    return;
  }
  ok.disabled = true;
  st.textContent = `正在加入 ${paths.length} 项…`;
  const send = extra => runLongOperation('/api/refs/add-batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project: choice.project, source: choice.source, paths, ...extra }),
  }, st);
  try {
    let result;
    try {
      result = await send();
    } catch (e) {
      if (!(choice.source === 'remote' && e.status === 409 && e.detail?.missing_dir)) throw e;
      st.textContent = '';
      const yes = await confirmDialog({
        title: '远端还没有这个项目目录',
        text: `workshop 下不存在 ${choice.project}。确认后会创建该目录和 reference-tex.json，再把 ${paths.length} 项加入引用。`,
        confirmLabel: '创建目录并加入',
        danger: false,
      });
      if (!yes) {
        st.textContent = '已取消，未创建目录';
        ok.disabled = false;
        return;
      }
      st.textContent = '正在创建目录…';
      result = await send({ create_missing: true });
    }
    const summary = bulkSummaryText(result);
    const failed = (result.failed || []).length;
    invalidateProjects(choice.source);
    touchProject(choice.project);
    if (state.project === choice.project && state.projectSource === choice.source) loadRefs();
    toast(`${choice.project}：${summary}`, failed ? 'warn' : 'ok', failed ? 9000 : 5000);
    state.selected.clear();
    setSelectMode(false);
    closeBulkModal();
  } catch (e) {
    st.textContent = e.message;
    st.classList.add('warn-text');
    ok.disabled = false;
  }
}

async function showDetail(it) {
  await loadPdfToken();
  const l = it.local;
  const r = it.remote;
  const remoteAny = r.has_pdf || r.has_source;
  const anyPdf = l.has_pdf || r.has_pdf;

  if (!anyPdf) state.previewSource = null;
  else {
    if (state.previewSource !== 'local' && state.previewSource !== 'remote') {
      state.previewSource = l.has_pdf ? 'local' : 'remote';
    }
    if (state.previewSource === 'local' && !l.has_pdf) state.previewSource = 'remote';
    if (state.previewSource === 'remote' && !r.has_pdf) state.previewSource = 'local';
  }

  const title = itemTitle(it);
  const proxy = {
    ...it,
    title, venue: r.venue || l.venue || '', entry: r.entry || '',
    doi: r.doi || '',
    has_pdf: anyPdf, has_source: r.has_source,
  };
  const pdfUrl = anyPdf
    ? paperPdfUrl(state.previewSource, it.topic, it.year, it.slug) : '';
  const pdfTabHref = anyPdf ? pdfViewerUrl(pdfUrl, it.slug) : '';
  const codeUrl = /^https?:\/\//i.test(r.code || '') ? r.code : '';
  const orUrl = /^https?:\/\//i.test(r.openreview || '') ? r.openreview : '';
  const doiUrl = /^10\.\d{4,9}\//.test(r.doi || '') ? 'https://doi.org/' + r.doi : '';

  $('#detail').innerHTML = `
    <h3>${esc(it.slug)}</h3>
    <div class="meta">
      ${title ? `<dl><dt>标题</dt><dd>${esc(displayTitle(title))}</dd></dl>` : ''}
      <dl><dt>位置</dt><dd>${esc(it.topic)} / ${esc(it.year)}</dd></dl>
      <dl><dt>本机</dt><dd>${l.has_pdf ? 'pdf: ' + esc(l.pdf) : '<span class="error">无</span>'}</dd></dl>
      <dl><dt>${esc(remoteLabel())}</dt><dd>${state.remoteError
        ? '<span class="error">离线，未读取</span>'
        : `${r.has_pdf ? 'pdf: ' + esc(r.pdf) : '<span class="error">无 pdf</span>'}${r.has_source ? ' · tex: ' + esc(r.entry || 'source/') : ' · 无 tex'}`}</dd></dl>
      ${codeUrl ? `<dl><dt>代码</dt><dd><a href="${esc(codeUrl)}" target="_blank" rel="noopener noreferrer">${esc(codeUrl)}</a></dd></dl>` : ''}
      ${orUrl ? `<dl><dt>OpenReview</dt><dd><a href="${esc(orUrl)}" target="_blank" rel="noopener noreferrer">${esc(orUrl)}</a></dd></dl>` : ''}
      ${doiUrl ? `<dl><dt>DOI</dt><dd><a href="${esc(doiUrl)}" target="_blank" rel="noopener noreferrer">${esc(r.doi)}</a></dd></dl>` : ''}
    </div>
    <div class="actions-row">
      ${l.has_pdf ? '<button class="mini" id="btn-open">在预览中打开</button>' : ''}
      <button class="mini star-btn ${state.stars.has(it.key) ? 'on' : ''}" id="btn-star" data-star="${esc(it.key)}">${state.stars.has(it.key) ? '★ 已星标' : '☆ 星标'}</button>
      <button class="mini danger" id="btn-del">删除(两侧)</button>
      <span id="detail-status" class="muted"></span>
      <input type="file" id="supp-pdf" accept="application/pdf" class="supp-input">
      <input type="file" id="supp-tex" class="supp-input">
    </div>
    ${remoteAny ? `
      <div class="actions-row">
        <button class="mini" id="btn-edit">编辑</button>
        <label class="mini${pendingSupplements.has(it.key) ? ' disabled' : ''}" id="btn-add-pdf" for="supp-pdf" role="button" tabindex="0">补充/替换 pdf</label>
        <label class="mini${pendingSupplements.has(it.key) ? ' disabled' : ''}" id="btn-add-tex" for="supp-tex" role="button" tabindex="0">补充/替换 tex</label>
      </div>` : `
      <div class="actions-row">
        <span class="muted">${state.remoteError ? '远端未读取，暂不可编辑远端字段或补传。' : '远端库没有对应目录，编辑远端字段与补传不可用。'}</span>
      </div>`}
    ${researchHtml(it)}
    ${paperNoteHtml(it)}
    ${addToProjectHtml(remoteAny || l.has_pdf, remoteAny || l.has_pdf ? (remoteAny ? '' : '仅本机有这篇论文，只能加入本机项目')
      : (state.remoteError ? '远端离线且本机没有 pdf，暂时无法加入项目' : '两侧都没有 pdf，无法引用'))}
    ${anyPdf ? `
      <div class="actions-row">
        <div class="seg" id="preview-seg">
          ${l.has_pdf ? `<button data-p="local" class="${state.previewSource === 'local' ? 'active' : ''}">本机 pdf</button>` : ''}
          ${r.has_pdf ? `<button data-p="remote" class="${state.previewSource === 'remote' ? 'active' : ''}">${esc(remoteLabel())} pdf</button>` : ''}
        </div>
        <a class="mini" id="btn-pdf-tab" href="${esc(pdfTabHref)}" target="_blank" rel="noopener noreferrer">在新标签页打开 PDF</a>
      </div>` : '<p class="muted pad">两侧都没有 pdf</p>'}
  `;

  const suppPdf = $('#supp-pdf');
  const suppTex = $('#supp-tex');
  const editBtn = $('#btn-edit');
  if (editBtn) editBtn.onclick = () => openEditor(proxy);
  if (remoteAny) {
    [['#btn-add-pdf', suppPdf], ['#btn-add-tex', suppTex]].forEach(([sel, input]) => {
      $(sel).onkeydown = event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          input.click();
        }
      };
    });
    suppPdf.onchange = () => {
      const file = suppPdf.files[0];
      suppPdf.value = '';
      if (file) supplementFile('pdf', file, proxy);
    };
    suppTex.onchange = () => {
      const file = suppTex.files[0];
      suppTex.value = '';
      if (file) supplementFile('source', file, proxy);
    };
  }
  const openBtn = $('#btn-open');
  if (openBtn) openBtn.onclick = async () => {
    try {
      await api('/api/open?topic=' + encodeURIComponent(it.topic) +
        '&year=' + encodeURIComponent(it.year) + '&slug=' + encodeURIComponent(it.slug),
        { method: 'POST' });
    } catch (e) { toast(e.message, 'error'); }
  };
  $('#btn-del').onclick = async () => {
    const ok = await confirmDialog({
      title: `删除 ${it.slug}`,
      text: '本机和远端两侧论文都会移入回收区，项目引用保留并标记缺失。',
      confirmLabel: '移入回收区',
      expected: it.slug,
      inputLabel: '输入 slug 确认',
    });
    if (!ok) return;
    try {
      const r = await api('/api/paper?source=both' +
        '&topic=' + encodeURIComponent(it.topic) + '&year=' + encodeURIComponent(it.year) +
        '&slug=' + encodeURIComponent(it.slug) + '&confirm=' + encodeURIComponent(it.slug),
        { method: 'DELETE' });
      state.current = null;
      $('#detail').innerHTML = '<p class="muted pad">已删除(两侧)</p>';
      loadLibrary();
      if (r.warnings?.length) toast(r.warnings.join('；'), 'warn');
      else toast('已移入本机与远端回收区', 'ok');
    } catch (e) { toast(e.message, 'error'); }
  };
  $$('#preview-seg button').forEach(b => b.onclick = () => {
    state.previewSource = b.dataset.p;
    showDetail(it);
  });
  if ($('#add-proj-select') && !$('#add-proj-select').disabled) bindAddToProject(it);
  $('#btn-star').onclick = () => toggleStar(it.key);
  bindResearch(it);
  bindPaperNote(it);
}

/* ---------- 资料（笔记/链接） ---------- */

function knowledgeDate(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const pad = n => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function knowledgeSyncHtml(edit) {
  if (!edit) return '';
  if (edit.reason) {
    return `<div class="local-edit conflict">
      <div class="local-edit-text"><strong>${esc(edit.reason)}</strong><span class="muted">请检查本机 note.md 后手动处理。</span></div>
      <div class="actions-row"><button class="mini" id="btn-k-sync-pull">放弃本机文件，载入远端</button></div>
    </div>`;
  }
  const when = edit.modified_at ? `修改于 ${esc(knowledgeDate(edit.modified_at))}` : '检测到本机修改';
  if (!edit.conflict) {
    return `<div class="local-edit">
      <div class="local-edit-text"><strong>本机 note.md 有未同步的修改</strong><span class="muted">${when}，正在写回远端…</span></div>
    </div>`;
  }
  return `<div class="local-edit conflict">
    <div class="local-edit-text"><strong>本机 note.md 与远端都有更新</strong><span class="muted">${when}。请选择保留哪一份。</span></div>
    <div class="actions-row">
      <button class="mini" id="btn-k-sync-push">用本机内容覆盖远端</button>
      <button class="mini" id="btn-k-sync-pull">放弃本机修改，载入远端</button>
    </div>
  </div>`;
}

function knowledgeDetailHtml(item, source, remoteError, localEdit = null) {
  const url = /^https?:\/\//i.test(item.url || '') ? item.url : '';
  const tags = Array.isArray(item.tags) ? item.tags : [];
  const label = item.kind === 'link' ? '链接' : '笔记';
  const starred = state.stars.has(item.key);
  return `
    <h3>${esc(item.title)}</h3>
    <div class="meta">
      <dl><dt>类型</dt><dd>${label}</dd></dl>
      ${url ? `<dl><dt>链接</dt><dd><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a></dd></dl>` : ''}
      ${item.source ? `<dl><dt>来源</dt><dd>${esc(item.source)}</dd></dl>` : ''}
      ${tags.length ? `<dl><dt>标签</dt><dd>${tags.map(tag => `<span class="tag-chip ${tagTone(tag)}">${esc(tag)}</span>`).join(' ')}</dd></dl>` : ''}
      ${item.created_at ? `<dl><dt>创建</dt><dd>${esc(knowledgeDate(item.created_at))}</dd></dl>` : ''}
      ${item.updated_at ? `<dl><dt>更新</dt><dd>${esc(knowledgeDate(item.updated_at))}</dd></dl>` : ''}
      ${source === 'local' ? `<dl><dt>状态</dt><dd class="warn-text">远端离线，显示本机缓存${remoteError ? '：' + esc(remoteError) : ''}</dd></dl>` : ''}
    </div>
    ${knowledgeSyncHtml(localEdit)}
    <div class="actions-row">
      <button class="mini" id="btn-k-edit">编辑</button>
      ${url ? '<button class="mini" id="btn-k-open">新标签页打开</button>' : ''}
      ${item.kind === 'note' ? '<button class="mini" id="btn-k-open-local">用本地软件打开</button>' : ''}
      <button class="mini star-btn ${starred ? 'on' : ''}" id="btn-star" data-star="${esc(item.key)}">${starred ? '★ 已星标' : '☆ 星标'}</button>
      <button class="mini danger" id="btn-k-del">删除</button>
      <span id="detail-status" class="muted"></span>
    </div>
    ${addToProjectHtml(true, source === 'remote' ? '' : '远端离线，只能加入本机项目')}
    ${item.body
      ? `<div class="markdown-body knowledge-markdown">${PapertexMarkdown.render(item.body, { emptyText: '这份资料没有正文。' })}</div>`
      : '<p class="muted pad">没有正文</p>'}`;
}

async function syncKnowledgeLocal(item, localEdit, { mode = 'push', overwrite = false } = {}) {
  try {
    const query = '?mode=' + encodeURIComponent(mode) + (overwrite ? '&overwrite=1' : '');
    const result = await api('/api/knowledge/' + encodeURIComponent(item.id) + '/sync-local' + query,
      { method: 'POST' });
    const updated = result.item;
    const listItem = { ...updated };
    delete listItem.body;
    const index = (state.knowledge || []).findIndex(entry => entry.key === updated.key);
    if (index >= 0) state.knowledge[index] = listItem;
    const stillCurrent = state.current && state.current.id === item.id;
    if (stillCurrent) {
      state.current = updated;
      renderList();
      $('#detail').innerHTML = knowledgeDetailHtml(updated, 'remote', '');
      bindKnowledgeDetail(updated);
    } else {
      renderList();
    }
    return true;
  } catch (e) {
    if (e.status === 409 && localEdit) {
      localEdit.conflict = true;
      if (state.current && state.current.id === item.id) {
        $('#detail').innerHTML = knowledgeDetailHtml(item, 'remote', '', localEdit);
        bindKnowledgeDetail(item, localEdit);
      }
      toast('远端资料已有更新，本机修改尚未写回', 'warn');
    } else {
      toast(e.message, 'error');
    }
    return false;
  }
}

function bindKnowledgeDetail(item, localEdit = null) {
  $('#btn-k-edit').onclick = () => openKnowledgeEditor(item);
  const open = $('#btn-k-open');
  if (open) open.onclick = () => window.open(item.url, '_blank', 'noopener');
  const pushLocal = $('#btn-k-sync-push');
  if (pushLocal) pushLocal.onclick = async () => {
    const ok = await confirmDialog({
      title: '用本机内容覆盖远端？',
      text: '远端当前的修改会被本机 note.md 覆盖，操作会同步到其他设备。',
      confirmLabel: '覆盖远端',
    });
    if (ok) syncKnowledgeLocal(item, localEdit, { overwrite: true });
  };
  const pullLocal = $('#btn-k-sync-pull');
  if (pullLocal) pullLocal.onclick = () => syncKnowledgeLocal(item, localEdit, { mode: 'pull' });
  const openLocal = $('#btn-k-open-local');
  if (openLocal) openLocal.onclick = async () => {
    const status = $('#detail-status');
    status.textContent = '正在打开…';
    try {
      await api('/api/knowledge/' + encodeURIComponent(item.id) + '/open', { method: 'POST' });
      status.textContent = '';
      toast('已用本地软件打开 note.md', 'ok');
    } catch (e) {
      status.innerHTML = `<span class="error">${esc(e.message)}</span>`;
    }
  };
  if ($('#add-proj-select') && !$('#add-proj-select').disabled) bindAddToProject(item);
  $('#btn-star').onclick = () => toggleStar(item.key);
  $('#btn-k-del').onclick = async () => {
    const ok = await confirmDialog({
      title: `删除资料「${item.title}」`,
      text: '资料会移入远端回收区，可在回收区恢复或清理。本机缓存会一并删除。',
      confirmLabel: '移入回收区',
      expected: item.title,
      inputLabel: '输入资料标题确认',
    });
    if (!ok) return;
    try {
      const result = await api('/api/knowledge/' + encodeURIComponent(item.id) +
        '?confirm=' + encodeURIComponent(item.title), { method: 'DELETE' });
      state.knowledge = (state.knowledge || []).filter(entry => entry.key !== item.key);
      state.current = null;
      renderList();
      $('#detail').innerHTML = '<p class="muted pad">资料已移入回收区</p>';
      if (result.warnings?.length) toast(result.warnings.join('；'), 'warn');
      else toast('已移入远端回收区', 'ok');
    } catch (e) { toast(e.message, 'error'); }
  };
}

async function showKnowledgeDetail(it) {
  renderViewState($('#detail'), 'loading', '正在读取资料');
  try {
    const data = await api('/api/knowledge/' + encodeURIComponent(it.id), { cache: 'no-store' });
    const localEdit = data.local_edit || null;
    const item = localEdit ? { ...data.item, body: localEdit.body } : data.item;
    const index = (state.knowledge || []).findIndex(entry => entry.key === item.key);
    if (index >= 0) {
      const meta = { ...data.item };
      delete meta.body;
      state.knowledge[index] = meta;
    }
    state.current = item;
    renderList();
    $('#detail').innerHTML = knowledgeDetailHtml(item, data.source, data.remote_error, localEdit);
    bindKnowledgeDetail(item, localEdit);
    if (localEdit && !localEdit.conflict) {
      // 打开预览时把本机较新的 note.md 写回远端；失败会切换为冲突提示
      syncKnowledgeLocal(item, localEdit);
    }
  } catch (e) {
    renderViewState($('#detail'), 'error', '资料读取失败', e.message,
      () => showKnowledgeDetail(it));
  }
}

function openKnowledgeEditor(item = null) {
  state.knowledgeDraft = item;
  const modal = $('#knowledge-modal');
  const preset = item?.kind === 'link' ? 'link'
    : item ? 'note' : (state.libraryMode === 'link' ? 'link' : 'note');
  const label = preset === 'link' ? '链接' : '笔记';
  $('#knowledge-modal-title').textContent = item ? `编辑${label}` : `添加${label}`;
  $('#knowledge-kind').value = preset;
  $('#knowledge-title').value = item?.title || '';
  $('#knowledge-url').value = item?.url || '';
  $('#knowledge-source').value = item?.source || '';
  $('#knowledge-tags').value = Array.isArray(item?.tags) ? item.tags.join(', ') : '';
  $('#knowledge-body').value = item?.body || '';
  $('#knowledge-status').textContent = '';
  $('#knowledge-save').disabled = false;
  modal.classList.remove('hidden');
  document.body.classList.add('preview-open');
  modal.showModal();
  $('#knowledge-title').focus();
}

function closeKnowledgeModal() {
  const modal = $('#knowledge-modal');
  if (modal.open) modal.close();
  modal.classList.add('hidden');
  document.body.classList.remove('preview-open');
  state.knowledgeDraft = null;
}

async function submitKnowledge(event) {
  event.preventDefault();
  const status = $('#knowledge-status');
  const button = $('#knowledge-save');
  const payload = {
    kind: $('#knowledge-kind').value,
    title: $('#knowledge-title').value.trim(),
    url: $('#knowledge-url').value.trim(),
    source: $('#knowledge-source').value.trim(),
    tags: $('#knowledge-tags').value,
    body: $('#knowledge-body').value,
  };
  if (!payload.title) {
    status.innerHTML = '<span class="error">标题不能为空</span>';
    $('#knowledge-title').focus();
    return;
  }
  if (payload.kind === 'link' && !/^https?:\/\//i.test(payload.url)) {
    status.innerHTML = '<span class="error">链接资料需要 http(s) 链接</span>';
    $('#knowledge-url').focus();
    return;
  }
  const draft = state.knowledgeDraft;
  button.disabled = true;
  status.textContent = '保存中…';
  try {
    const result = await api(draft ? '/api/knowledge/' + encodeURIComponent(draft.id) : '/api/knowledge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const item = { ...result.item, body: payload.body };
    const listItem = { ...result.item };
    delete listItem.body;
    const index = (state.knowledge || []).findIndex(entry => entry.key === item.key);
    if (index >= 0) state.knowledge[index] = listItem;
    else state.knowledge.unshift(listItem);
    closeKnowledgeModal();
    const kind = result.item.kind === 'link' ? 'link' : 'note';
    if (kind !== state.libraryMode) setLibraryMode(kind, { keepDetail: true });
    state.current = item;
    renderList();
    $('#detail').innerHTML = knowledgeDetailHtml(item, 'remote', '');
    bindKnowledgeDetail(item);
    if (result.warnings?.length) toast(result.warnings.join('；'), 'warn');
    else toast(result.notice || '已保存', 'ok');
  } catch (e) {
    status.innerHTML = `<span class="error">${esc(e.message)}</span>`;
    button.disabled = false;
  }
}

/* ---------- 维护面板（镜像/查重/回收区） ---------- */

const mirrorState = { direction: 'pull', plan: { pull: [], push: [] }, selected: new Set(), loaded: false };
const dupState = { groups: [], keep: {}, drops: {}, loaded: false };
const recoveryState = { items: [], loaded: false };

function mirrorItems() {
  return mirrorState.direction === 'pull' ? mirrorState.plan.pull : mirrorState.plan.push;
}

function syncMirrorSeg() {
  $$('#mirror-seg button').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.direction === mirrorState.direction));
}

function renderMirror() {
  const list = $('#mirror-list');
  const items = mirrorItems();
  const chosen = items.filter(item => mirrorState.selected.has(item.key));
  const bytes = chosen.reduce((sum, item) => sum + item.size, 0);
  $('#mirror-count').textContent = items.length
    ? `已选 ${chosen.length} / ${items.length} 篇 · ${byteText(bytes)}` : '';
  $('#mirror-run').disabled = !chosen.length;
  const all = $('#mirror-all');
  all.checked = items.length > 0 && chosen.length === items.length;
  all.disabled = !items.length;
  if (!mirrorState.loaded) {
    renderViewState(list, 'loading', '正在比对两侧 pdf');
    return;
  }
  if (!items.length) {
    renderViewState(list, 'empty',
      mirrorState.direction === 'pull' ? '本机没有缺失的 pdf' : '远端没有缺失的 pdf',
      '只比对两侧论文的 pdf，不做内容替换；同名 pdf 会被跳过。');
    return;
  }
  list.innerHTML = items.map(item => `
    <label class="panel-row">
      <input type="checkbox" data-key="${esc(item.key)}" ${mirrorState.selected.has(item.key) ? 'checked' : ''}>
      <div class="main">
        <div class="slug">${esc(item.slug)}</div>
        <div class="title">${esc(item.topic)} / ${esc(item.year)}${item.remote_has_source ? ' · 远端已有 tex' : ''}</div>
      </div>
      <span class="meta">${byteText(item.size)}</span>
    </label>`).join('');
  $$('#mirror-list input[data-key]').forEach(box => {
    box.onchange = () => {
      if (box.checked) mirrorState.selected.add(box.dataset.key);
      else mirrorState.selected.delete(box.dataset.key);
      renderMirror();
    };
  });
}

async function loadMirror() {
  mirrorState.loaded = false;
  renderMirror();
  try {
    const plan = await api('/api/mirror/plan');
    mirrorState.plan = { pull: plan.pull || [], push: plan.push || [] };
    mirrorState.selected = new Set(mirrorItems().map(item => item.key));
    mirrorState.loaded = true;
    const pull = mirrorState.plan.pull.length;
    const push = mirrorState.plan.push.length;
    $('#mirror-result').innerHTML = pull || push
      ? `本机缺 ${pull} 篇 ·远端缺 ${push} 篇`
      : '<span class="ok">两侧已对齐</span>';
    renderMirror();
  } catch (e) {
    mirrorState.loaded = true;
    $('#mirror-result').textContent = '';
    renderViewState($('#mirror-list'), 'error', '镜像比对失败', e.message, () => loadMirror());
  }
}

async function openMirrorModal() {
  const modal = $('#mirror-modal');
  mirrorState.direction = 'pull';
  mirrorState.selected = new Set();
  syncMirrorSeg();
  $('#mirror-result').textContent = '';
  modal.classList.remove('hidden');
  modal.showModal();
  document.body.classList.add('preview-open');
  $('#mirror-modal-close').focus();
  await loadMirror();
}

function closeMirrorModal() {
  const modal = $('#mirror-modal');
  if (!modal.open) return;
  modal.close();
  modal.classList.add('hidden');
  document.body.classList.remove('preview-open');
  $('#mirror-fix')?.focus();
}

function dupReasonText(reason) {
  return { arxiv: 'arXiv 编号', title: '标题相同', md5: 'pdf 内容相同' }[reason] || reason;
}

function dupResetGroup(index, group, keep) {
  dupState.keep[index] = keep;
  dupState.drops[index] = new Set(group.items.filter(item => item.key !== keep).map(item => item.key));
}

function renderDup() {
  const list = $('#dup-list');
  if (!dupState.loaded) {
    renderViewState(list, 'loading', '正在扫描全库');
    return;
  }
  if (!dupState.groups.length) {
    renderViewState(list, 'empty', '没有发现重复论文',
      '按 arXiv 编号、规范化标题和 pdf 内容比对远端资料库。');
    return;
  }
  list.innerHTML = dupState.groups.map((group, index) => {
    const keep = dupState.keep[index];
    const drops = dupState.drops[index];
    const rows = group.items.map(item => `
      <div class="panel-row">
        <input type="radio" name="dup-keep-${index}" data-keep="${esc(item.key)}" data-group="${index}"
          aria-label="保留 ${esc(item.slug)}" ${item.key === keep ? 'checked' : ''}>
        <input type="checkbox" data-drop="${esc(item.key)}" data-group="${index}"
          aria-label="合并 ${esc(item.slug)}"
          ${item.key !== keep && drops.has(item.key) ? 'checked' : ''} ${item.key === keep ? 'disabled' : ''}>
        <div class="main">
          <div class="slug">${esc(item.slug)}${item.starred ? ' ★' : ''}</div>
          <div class="title">${esc(displayTitle(item.title || '（无标题）'))} · ${esc(item.topic)} / ${esc(item.year)}</div>
        </div>
        <span class="badges">
          ${item.has_pdf ? `<span class="badge pdf">${byteText(item.pdf_size)}</span>` : '<span class="badge warn">无 pdf</span>'}
          ${item.has_source ? '<span class="badge tex">TEX</span>' : ''}
        </span>
      </div>`).join('');
    return `<section class="panel-group">
      <div class="panel-group-head">
        <span class="badges">${group.reasons.map(r => `<span class="badge">${esc(dupReasonText(r))}</span>`).join('')}</span>
        <span>共 ${group.items.length} 篇，将合并 ${drops.size} 篇</span>
        <button type="button" class="mini primary" data-merge="${index}" ${drops.size ? '' : 'disabled'}>合并</button>
      </div>
      ${rows}
    </section>`;
  }).join('');
  $$('#dup-list input[data-keep]').forEach(radio => {
    radio.onchange = () => {
      const index = Number(radio.dataset.group);
      const oldKeep = dupState.keep[index];
      const drops = dupState.drops[index];
      drops.delete(radio.dataset.keep);
      if (oldKeep) drops.add(oldKeep);
      dupState.keep[index] = radio.dataset.keep;
      renderDup();
    };
  });
  $$('#dup-list input[data-drop]').forEach(box => {
    box.onchange = () => {
      const index = Number(box.dataset.group);
      if (box.checked) dupState.drops[index].add(box.dataset.drop);
      else dupState.drops[index].delete(box.dataset.drop);
      renderDup();
    };
  });
  $$('#dup-list button[data-merge]').forEach(button => {
    button.onclick = () => mergeDupGroup(Number(button.dataset.merge));
  });
}

async function loadDupGroups() {
  dupState.loaded = false;
  renderDup();
  try {
    const data = await runLongOperation('/api/duplicates/scan', { cache: 'no-store' }, $('#dup-result'));
    dupState.groups = data.groups || [];
    dupState.keep = {};
    dupState.drops = {};
    dupState.groups.forEach((group, index) => dupResetGroup(index, group, group.suggested));
    dupState.loaded = true;
    $('#dup-result').innerHTML = dupState.groups.length
      ? `发现 ${dupState.groups.length} 组重复`
      : '<span class="ok">没有发现重复</span>';
    renderDup();
  } catch (e) {
    dupState.loaded = true;
    $('#dup-result').textContent = '';
    renderViewState($('#dup-list'), 'error', '查重失败', e.message, () => loadDupGroups());
  }
}

async function mergeDupGroup(index) {
  const group = dupState.groups[index];
  if (!group) return;
  const keep = dupState.keep[index];
  const drops = [...dupState.drops[index]];
  if (!keep || !drops.length) return;
  const ok = await confirmDialog({
    title: '合并重复论文',
    text: `保留 ${keep}，把 ${drops.join('、')} 的资源并入保留项后移入回收区；项目引用与星标会改指向保留项。`,
    confirmLabel: '合并',
  });
  if (!ok) return;
  try {
    await runLongOperation('/api/duplicates/merge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keep, drops }),
    }, $('#dup-result'));
    loadLibrary();
    await loadDupGroups();
  } catch (e) {
    $('#dup-result').innerHTML = `<span class="error">${esc(e.message)}</span>`;
  }
}

async function openDupModal() {
  const modal = $('#dup-modal');
  $('#dup-result').textContent = '';
  dupState.groups = [];
  dupState.loaded = false;
  modal.classList.remove('hidden');
  modal.showModal();
  document.body.classList.add('preview-open');
  $('#dup-modal-close').focus();
  await loadDupGroups();
}

function closeDupModal() {
  const modal = $('#dup-modal');
  if (!modal.open) return;
  modal.close();
  modal.classList.add('hidden');
  document.body.classList.remove('preview-open');
  $('#dup-scan')?.focus();
}

const RECOVERY_KIND_TEXT = {
  paper: '论文备份', topic: '主题备份', knowledge: '资料备份', pdf_backup: 'pdf 备份',
  file_backup: '文件备份', mixed: '目录备份',
};

function recoveryTime(seconds) {
  if (!seconds) return '';
  const date = new Date(seconds * 1000);
  if (Number.isNaN(date.getTime())) return '';
  const pad = value => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function renderRecovery() {
  const list = $('#recovery-list');
  const items = recoveryState.items;
  const remoteBytes = items.filter(i => i.source === 'remote').reduce((sum, i) => sum + i.size, 0);
  const localBytes = items.filter(i => i.source === 'local').reduce((sum, i) => sum + i.size, 0);
  $('#recovery-summary').textContent = items.length
    ? `${items.length} 个条目 · ${remoteLabel()} ${byteText(remoteBytes)} · 本机 ${byteText(localBytes)}`
    : '';
  $('#recovery-purge-all').disabled = !items.length;
  if (!recoveryState.loaded) {
    renderViewState(list, 'loading', '正在读取回收区');
    return;
  }
  if (!items.length) {
    renderViewState(list, 'empty', '回收区是空的',
      '删除论文、主题或替换资料时，旧内容会移到这里，等待手工恢复或清理。');
    return;
  }
  list.innerHTML = items.map(item => {
    const kind = RECOVERY_KIND_TEXT[item.kind] || '备份';
    const name = ['paper', 'knowledge'].includes(item.kind)
      ? (item.label || item.name || '未知条目')
      : (item.name || item.label || '未知条目');
    const detail = [
      kind,
      item.kind === 'paper' && item.name && item.name !== name ? item.name : '',
      item.target ? `原位置 ${item.target}` : '',
    ].filter(Boolean).join(' · ');
    return `<div class="panel-row">
      <span class="badge ${item.source === 'remote' ? 'tex' : 'pdf'}">${item.source === 'remote' ? esc(remoteLabel()) : '本机'}</span>
      <div class="main">
        <div class="slug">${esc(name)}</div>
        <div class="title">${esc(detail)}</div>
      </div>
      <span class="meta">${esc(recoveryTime(item.created))} · ${byteText(item.size)}</span>
      ${item.restorable ? `<button type="button" class="mini" data-restore="${esc(item.source)}:${esc(item.id)}">恢复</button>` : ''}
      <button type="button" class="mini danger" data-purge="${esc(item.source)}:${esc(item.id)}">清理</button>
    </div>`;
  }).join('');
  $$('#recovery-list button[data-restore]').forEach(button => {
    button.onclick = () => restoreRecoveryItem(button.dataset.restore);
  });
  $$('#recovery-list button[data-purge]').forEach(button => {
    button.onclick = () => purgeRecoveryItems([button.dataset.purge.split(':')[0]],
      [button.dataset.purge.split(':')[1]]);
  });
}

async function loadRecovery() {
  recoveryState.loaded = false;
  renderRecovery();
  try {
    const data = await api('/api/recovery/list');
    recoveryState.items = data.items || [];
    recoveryState.loaded = true;
    $('#recovery-result').innerHTML = data.errors?.length
      ? `<span class="error">${esc(data.errors.join('；'))}</span>` : '';
    renderRecovery();
  } catch (e) {
    recoveryState.loaded = true;
    $('#recovery-result').textContent = '';
    renderViewState($('#recovery-list'), 'error', '回收区读取失败', e.message, () => loadRecovery());
  }
}

async function openRecoveryModal() {
  const modal = $('#recovery-modal');
  $('#recovery-result').textContent = '';
  recoveryState.items = [];
  recoveryState.loaded = false;
  modal.classList.remove('hidden');
  modal.showModal();
  document.body.classList.add('preview-open');
  $('#recovery-modal-close').focus();
  await loadRecovery();
}

function closeRecoveryModal() {
  const modal = $('#recovery-modal');
  if (!modal.open) return;
  modal.close();
  modal.classList.add('hidden');
  document.body.classList.remove('preview-open');
  $('#recovery-manage')?.focus();
}

async function restoreRecoveryItem(encoded) {
  const [source, id] = encoded.split(':');
  const item = recoveryState.items.find(entry => entry.source === source && entry.id === id);
  const ok = await confirmDialog({
    title: '恢复备份',
    text: item?.target
      ? `把「${item.name || item.label}」恢复到 ${item.target}？目标已存在时不会覆盖。`
      : '恢复这个备份到原位置？目标已存在时不会覆盖。',
    confirmLabel: '恢复',
    danger: false,
  });
  if (!ok) return;
  try {
    const result = await api('/api/recovery/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source, id }),
    });
    await loadRecovery();
    loadLibrary();
    $('#recovery-result').innerHTML = `<span class="ok">${esc(result.notice || '已恢复')}</span>`;
  } catch (e) {
    $('#recovery-result').innerHTML = `<span class="error">${esc(e.message)}</span>`;
  }
}

async function purgeRecoveryItems(sources, ids) {
  const source = [...new Set(sources)];
  const label = ids.length === 1 ? '清理这个备份' : `清理 ${ids.length} 个备份`;
  const ok = await confirmDialog({
    title: label,
    text: '从回收区彻底删除，无法撤销。',
    confirmLabel: '清理',
    expected: '清理',
    inputLabel: '输入「清理」确认',
  });
  if (!ok) return;
  try {
    const result = await api('/api/recovery/purge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: source.length === 2 ? 'both' : source[0], ids, confirm: '清理' }),
    });
    await loadRecovery();
    $('#recovery-result').innerHTML = `<span class="ok">${esc(result.notice || '已清理')}</span>`;
  } catch (e) {
    $('#recovery-result').innerHTML = `<span class="error">${esc(e.message)}</span>`;
  }
}

/* ---------- 上传 ---------- */

function pickValue(kind) {
  const sel = $('#' + kind + '-select');
  if (!sel) return '';
  if (sel.value === '__new__') return ($('#' + kind + '-new').value || '').trim();
  return sel.value;
}

function toggleNew(kind) {
  const sel = $('#' + kind + '-select');
  const input = $('#' + kind + '-new');
  if (!sel || !input) return;
  const isNew = sel.value === '__new__';
  input.classList.toggle('hidden', !isNew);
  if (isNew) input.focus();
}

function toggleAllNew() {
  ['topic', 'year', 'venue'].forEach(toggleNew);
}

function populateTopicSelect(topics) {
  const sel = $('#topic-select');
  const prev = sel.value;
  sel.innerHTML = '<option value="">选择主题…</option>' +
    topics.map(t => `<option value="${esc(t.name)}">${esc(t.name)} (${t.count})</option>`).join('') +
    '<option value="__new__">＋ 新建主题</option>';
  if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
  else if (!topics.length) sel.value = '__new__';
  toggleNew('topic');
}

function populateYearSelect(years) {
  const sel = $('#year-select');
  const prev = sel.value;
  const now = new Date().getFullYear();
  const list = [...new Set([...years.map(String), String(now), String(now + 1)])]
    .sort().reverse();
  sel.innerHTML = list.map(y => `<option value="${y}">${y}</option>`).join('') +
    '<option value="__new__">＋ 新建年份</option>';
  if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
  else sel.value = String(now);
  toggleNew('year');
}

function populateVenueSelect(derived) {
  const sel = $('#venue-select');
  const prev = sel.value;
  const list = [...new Set([...VENUES, ...(derived || [])])].sort((a, b) => a.localeCompare(b));
  sel.innerHTML = '<option value="">选择会议/期刊…</option>' +
    list.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('') +
    '<option value="__new__">其他…</option>';
  if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
  toggleNew('venue');
}

async function loadOptions(force = false) {
  try {
    const data = await api('/api/options' + (force ? '?force=1' : ''));
    populateTopicSelect(data.topics);
    populateYearSelect(data.years);
    populateVenueSelect(data.venues);
    composeSlug();
    state.optionsLoaded = true;
  } catch (e) {
    $('#upload-result').innerHTML = `<span class="error">${esc(e.message)}</span>`;
  }
}

function sanitizeToken(s) {
  return String(s || '').trim().replace(/\s+/g, '').replace(/[^A-Za-z0-9]/g, '');
}

function sanitizeVenue(s) {
  return String(s || '').trim().replace(/\s+/g, '-').replace(/[^A-Za-z0-9-]/g, '');
}

function composeSlug() {
  const input = $('#slug-input');
  if ($('#slug-manual').checked) return;
  const year = pickValue('year');
  const venue = sanitizeVenue(pickValue('venue'));
  const author = sanitizeToken($('#author-input').value);
  const keyword = sanitizeToken($('#keyword-input').value);
  input.value = (year && venue && author && keyword)
    ? `${year}-${venue}-${author}-${keyword}` : '';
}

function setSelectValue(selSel, value, newVal, newInputSel) {
  const sel = $(selSel);
  if ([...sel.options].some(o => o.value === value)) {
    sel.value = value;
  } else {
    sel.value = newVal;
    $(newInputSel).value = value;
    $(newInputSel).classList.remove('hidden');
  }
}

const form = $('#upload-form');
const upPdfInput = form.querySelector('input[name="pdf"]');
const upSrcInput = form.querySelector('input[name="source_archive"]');
const dropzone = $('#dropzone');
const dropStatus = $('#drop-status');

const sourceSeg = $('#source-seg');
const sourcePanes = [...document.querySelectorAll('[data-source-pane]')];

function activeSource() {
  return sourceSeg.querySelector('button.active')?.dataset.source || 'manual';
}

function setSource(source) {
  sourceSeg.querySelectorAll('button[data-source]').forEach(btn => {
    const on = btn.dataset.source === source;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  for (const pane of sourcePanes) {
    const on = pane.dataset.sourcePane === source;
    pane.classList.toggle('hidden', !on);
    pane.querySelectorAll('input, button').forEach(el => { el.disabled = !on; });
  }
}

sourceSeg.onclick = event => {
  const button = event.target.closest('button[data-source]');
  if (button) setSource(button.dataset.source);
};
setSource('arxiv');

function showPickedFiles() {
  const pairs = [
    [upPdfInput, $('#pdf-file-name'), $('#pdf-file-slot')],
    [upSrcInput, $('#src-file-name'), $('#src-file-slot')],
  ];
  for (const [input, name, slot] of pairs) {
    const file = input.files[0];
    name.textContent = file ? file.name : '未选择';
    slot.classList.toggle('has-file', !!file);
  }
  dropStatus.textContent = '';
}

function setInputFiles(input, files) {
  const dt = new DataTransfer();
  for (const f of files) dt.items.add(f);
  input.files = dt.files;
  input.dispatchEvent(new Event('change', { bubbles: true }));
}

function classifyDrop(files) {
  const pdfs = [], archives = [], others = [];
  for (const f of files) {
    const n = f.name.toLowerCase();
    if (f.type === 'application/pdf' || n.endsWith('.pdf')) pdfs.push(f);
    else if (/\.(zip|tar\.gz|tgz|tar)$/.test(n)) archives.push(f);
    else others.push(f);
  }
  return { pdfs, archives, others };
}

['dragenter', 'dragover'].forEach(ev => dropzone.addEventListener(ev, e => {
  e.preventDefault();
  e.stopPropagation();
  dropzone.classList.add('dragover');
}));
['dragleave', 'dragend'].forEach(ev => dropzone.addEventListener(ev, e => {
  if (!dropzone.contains(e.relatedTarget)) dropzone.classList.remove('dragover');
}));
dropzone.addEventListener('drop', e => {
  e.preventDefault();
  e.stopPropagation();
  dropzone.classList.remove('dragover');
  const { pdfs, archives, others } = classifyDrop(e.dataTransfer.files);
  const msgs = [];
  if (pdfs.length) {
    setInputFiles(upPdfInput, [pdfs[0]]);
    if (pdfs.length > 1) msgs.push(`（拖入 ${pdfs.length} 个 pdf，只取 ${pdfs[0].name}）`);
  }
  if (archives.length) {
    setInputFiles(upSrcInput, [archives[0]]);
    if (archives.length > 1) msgs.push(`（拖入 ${archives.length} 个源码包，只取 ${archives[0].name}）`);
  }
  if (others.length) msgs.push('不支持: ' + others.map(f => f.name).join(', '));
  showPickedFiles();
  if (msgs.length) {
    dropStatus.textContent = [dropStatus.textContent, ...msgs].filter(Boolean).join(' · ');
  }
});
upPdfInput.onchange = showPickedFiles;
upSrcInput.onchange = showPickedFiles;


form.elements.pdf.addEventListener('change', () => {
  const f = form.elements.pdf.files[0];
  if (!f) return;
  const stem = f.name.replace(/\.pdf$/i, '');
  const m = stem.match(/^(\d{4})-([^-]+)-([^-]+)-(.+)$/);
  if (!m) return;
  const [, year, venue, author, keyword] = m;
  setSelectValue('#year-select', year, '__new__', '#year-new');
  setSelectValue('#venue-select', venue, '__new__', '#venue-new');
  $('#author-input').value = author;
  $('#keyword-input').value = keyword;
  toggleAllNew();
  composeSlug();
});

$('#slug-manual').onchange = () => {
  const manual = $('#slug-manual').checked;
  $('#slug-input').readOnly = !manual;
  if (manual) $('#slug-input').focus();
  else composeSlug();
};

['#topic-select', '#year-select', '#venue-select'].forEach(sel => {
  $(sel).onchange = () => {
    toggleAllNew();
    composeSlug();
  };
});
['#topic-new', '#year-new', '#venue-new', '#author-input', '#keyword-input'].forEach(sel => {
  $(sel).oninput = composeSlug;
});

$('#options-refresh').onclick = () => loadOptions(true);

function applyLookupFields(d, fallbackVenue = 'arXiv', opts = {}) {
  form.querySelector('input[name="title"]').value = d.title || '';
  if (!opts.keepArxiv) form.querySelector('input[name="arxiv"]').value = d.arxiv_id || '';
  $('#author-input').value = d.author || '';
  $('#keyword-input').value = d.keyword || '';
  const ys = $('#year-select');
  const y = String(d.year || '');
  if (y && [...ys.options].some(o => o.value === y)) ys.value = y;
  else if (y) {
    ys.value = '__new__';
    $('#year-new').value = y;
  }
  toggleNew('year');
  const vs = $('#venue-select');
  const v = d.venue || fallbackVenue;
  if ([...vs.options].some(o => o.value === v)) vs.value = v;
  else {
    vs.value = '__new__';
    $('#venue-new').value = v;
  }
  toggleNew('venue');
  composeSlug();
}

const bibtexInput = form.querySelector('textarea[name="bibtex"]');
const bibtexPanel = $('#bibtex-panel');
const bibtexState = $('#bibtex-state');

function applyBibtex(d, label) {
  const text = String((d && d.bibtex) || '').trim();
  if (!text) {
    bibtexPanel.classList.remove('ready');
    bibtexState.innerHTML = '<span class="muted">未自动获取，可展开手动粘贴</span>';
    if (bibtexInput.dataset.auto === '1') {
      bibtexInput.value = '';
      delete bibtexInput.dataset.auto;
    }
    return;
  }
  if (!bibtexInput.value.trim() || bibtexInput.dataset.auto === '1') {
    bibtexInput.value = text + '\n';
    bibtexInput.dataset.auto = '1';
  }
  bibtexPanel.classList.add('ready');
  bibtexState.innerHTML = `<span class="ok">已自动获取</span>` +
    `<span class="muted"> · ${esc(label)}</span>`;
}

bibtexInput.addEventListener('input', () => { bibtexInput.dataset.auto = '0'; });
$('#bibtex-copy').onclick = async () => {
  if (!bibtexInput.value.trim()) {
    toast('还没有可复制的 BibTeX', 'warn');
    return;
  }
  try {
    await navigator.clipboard.writeText(bibtexInput.value);
  } catch (e) {
    bibtexInput.select();
    document.execCommand('copy');
  }
  toast('BibTeX 已复制');
};

async function parseArxiv() {
  const raw = $('#arxiv-url').value.trim();
  const st = $('#arxiv-status');
  if (!raw) {
    st.innerHTML = '<span class="error">先填 arXiv 链接或 ID</span>';
    return null;
  }
  const button = $('#arxiv-parse');
  if (button.disabled) return null;
  button.disabled = true;
  st.textContent = '抓取中…';
  try {
    const d = await runLongOperation('/api/arxiv/preview?url=' + encodeURIComponent(raw), {}, st);
    applyLookupFields(d);
    applyBibtex(d, 'arXiv');
    st.innerHTML = `<span class="ok">${esc(displayTitle(d.title))}</span>` +
      (d.comment ? `<span class="muted"> · ${esc(d.comment)}</span>` : '') +
      `<span class="muted"> · ${esc((d.authors || []).join(', '))}</span>`;
    const dq = new URLSearchParams({
      title: d.title || '', arxiv: d.arxiv_id || '',
      slug: $('#slug-input').value, year: String(d.year || ''),
    });
    api('/api/duplicates?' + dq.toString()).then(r => {
      if (r.same_key) {
        st.innerHTML += ' <span class="warn-text">⚠ 该 slug 已存在（提交会补充/替换）</span>';
      } else if (r.duplicates.length) {
        st.innerHTML += ` <span class="warn-text">⚠ 库中已有：${esc(r.duplicates.map(x => x.key).join('、'))}</span>`;
      }
    }).catch(() => {});
    return d;
  } catch (e) {
    st.innerHTML = `<span class="error">${esc(e.message)}</span>`;
    return null;
  } finally {
    button.disabled = false;
  }
}

$('#arxiv-parse').onclick = parseArxiv;

async function refreshOpenReviewSession(notice = '') {
  const st = $('#openreview-login-status');
  const toggle = $('#openreview-login-toggle');
  try {
    const s = await api('/api/openreview/session', { cache: 'no-store' });
    if ($('#openreview-login').classList.contains('hidden')) {
      toggle.textContent = s.logged_in ? '已登录' : '登录';
    }
    st.innerHTML = (notice ? `<span class="ok">${esc(notice)}</span> ` : '') + (s.logged_in
      ? `<span class="ok">已登录${s.user ? '：' + esc(s.user) : ''}</span> <button type="button" class="mini ghost" id="openreview-logout">退出</button>`
      : '<span class="muted">未登录</span>');
    const out = $('#openreview-logout');
    if (out) out.onclick = async () => {
      try { await api('/api/openreview/login', { method: 'DELETE' }); }
      catch (e) { toast(e.message, 'error'); }
      refreshOpenReviewSession();
    };
    return s;
  } catch (e) {
    st.innerHTML = `<span class="error">${esc(e.message)}</span>`;
    return { logged_in: false };
  }
}

function showOpenReviewLogin(show) {
  $('#openreview-login').classList.toggle('hidden', !show);
  if (show) $('#openreview-login-toggle').textContent = '收起登录';
  refreshOpenReviewSession();
}

$('#openreview-login-toggle').onclick = () =>
  showOpenReviewLogin($('#openreview-login').classList.contains('hidden'));

async function loginOpenReview() {
  const user = $('#openreview-user').value.trim();
  const pass = $('#openreview-pass').value;
  const st = $('#openreview-login-status');
  const button = $('#openreview-login-btn');
  if (button.disabled) return;
  if (!user || !pass) {
    st.innerHTML = '<span class="error">填写邮箱/用户名和密码</span>';
    return;
  }
  button.disabled = true;
  st.textContent = '登录中…';
  try {
    await api('/api/openreview/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: user, password: pass }),
    });
    await refreshOpenReviewSession('登录成功，可以再点「抓取信息」');
  } catch (e) {
    st.innerHTML = `<span class="error">${esc(e.message)}</span>`;
  } finally {
    $('#openreview-pass').value = '';
    button.disabled = false;
  }
}

$('#openreview-login-btn').onclick = loginOpenReview;
['#openreview-user', '#openreview-pass'].forEach(sel => {
  $(sel).onkeydown = event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      loginOpenReview();
    }
  };
});

async function parseOpenReview() {
  const raw = $('#openreview-url').value.trim();
  const st = $('#openreview-status');
  if (!raw) {
    st.innerHTML = '<span class="error">先填 OpenReview 链接</span>';
    return null;
  }
  const button = $('#openreview-parse');
  if (button.disabled) return null;
  button.disabled = true;
  st.textContent = '抓取中…';
  try {
    const d = await runLongOperation('/api/openreview/preview?url=' + encodeURIComponent(raw), {}, st);
    applyLookupFields(d, 'OpenReview');
    applyBibtex(d, 'OpenReview');
    if (d.openreview_url) $('#openreview-url').value = d.openreview_url;
    st.innerHTML = `<span class="ok">${esc(displayTitle(d.title))}</span>` +
      (d.venue_raw ? `<span class="muted"> · ${esc(d.venue_raw)}</span>` : '') +
      `<span class="muted"> · ${esc((d.authors || []).join(', '))}</span>`;
    const dq = new URLSearchParams({
      title: d.title || '', arxiv: d.arxiv_id || '', openreview: d.openreview_url || '',
      slug: $('#slug-input').value, year: String(d.year || ''),
    });
    api('/api/duplicates?' + dq.toString()).then(r => {
      if (r.same_key) {
        st.innerHTML += ' <span class="warn-text">⚠ 该 slug 已存在（提交会补充/替换）</span>';
      } else if (r.duplicates.length) {
        st.innerHTML += ` <span class="warn-text">⚠ 库中已有：${esc(r.duplicates.map(x => x.key).join('、'))}</span>`;
      }
    }).catch(() => {});
    return d;
  } catch (e) {
    if (e.status === 401 || (e.status === 403 && /人机验证|登录/.test(e.message))) {
      showOpenReviewLogin(true);
    }
    st.innerHTML = `<span class="error">${esc(e.message)}</span>`;
    return null;
  } finally {
    button.disabled = false;
  }
}

$('#openreview-parse').onclick = parseOpenReview;
refreshOpenReviewSession();

let lastIeeeDoi = '';

async function parseIeee() {
  const raw = $('#ieee-url').value.trim();
  const st = $('#ieee-status');
  if (!raw) {
    st.innerHTML = '<span class="error">先填 IEEE 链接或 DOI</span>';
    return null;
  }
  const button = $('#ieee-parse');
  if (button.disabled) return null;
  button.disabled = true;
  st.textContent = '抓取中…（需要能访问 IEEE 的校园网/VPN）';
  try {
    const d = await runLongOperation('/api/ieee/preview?ref=' + encodeURIComponent(raw), {}, st);
    applyLookupFields(d, 'IEEE', { keepArxiv: true });
    applyBibtex(d, 'IEEE / Crossref');
    lastIeeeDoi = d.doi || '';
    st.innerHTML = `<span class="ok">${esc(displayTitle(d.title))}</span>` +
      (d.venue_raw && d.venue_raw !== d.venue ? `<span class="muted"> · ${esc(d.venue_raw)}</span>` : '') +
      `<span class="muted"> · ${esc((d.authors || []).join(', '))}</span>` +
      (d.doi ? `<span class="muted"> · DOI ${esc(d.doi)}</span>` : '') +
      (d.early_access ? ' <span class="warn-text">早期访问版本</span>' : '');
    const dq = new URLSearchParams({
      title: d.title || '', doi: d.doi || '', arxiv: form.querySelector('input[name="arxiv"]').value.trim(),
      slug: $('#slug-input').value, year: String(d.year || ''),
    });
    api('/api/duplicates?' + dq.toString()).then(r => {
      if (r.same_key) {
        st.innerHTML += ' <span class="warn-text">⚠ 该 slug 已存在（提交会补充/替换）</span>';
      } else if (r.duplicates.length) {
        st.innerHTML += ` <span class="warn-text">⚠ 库中已有：${esc(r.duplicates.map(x => x.key).join('、'))}</span>`;
      }
    }).catch(() => {});
    return d;
  } catch (e) {
    st.innerHTML = `<span class="error">${esc(e.message)}</span>`;
    return null;
  } finally {
    button.disabled = false;
  }
}

$('#ieee-parse').onclick = parseIeee;

function uploadError(msg) {
  $('#upload-result').innerHTML = `<span class="error">${esc(msg)}</span>`;
}

function reasonText(reasons) {
  const map = { arxiv: 'arXiv ID 相同', openreview: 'OpenReview 相同', doi: 'DOI 相同', title: '标题相同', slug: 'slug 相同' };
  return (reasons || []).map(r => map[r] || r).join('、');
}

function showDupPrompt(dups, fd, message, okLabel = '仍然上传') {
  const rows = dups.map(d =>
    `<div class="dup-row"><span class="slug">${esc(d.key)}</span>` +
    `${d.title ? ' · ' + esc(displayTitle(d.title)) : ''} ` +
    `<span class="warn-text">(${esc(reasonText(d.reasons))})</span></div>`).join('');
  $('#upload-result').innerHTML = `
    <div class="dup-box">
      <div class="warn-text">${esc(message)}</div>
      ${rows}
      <div class="dup-actions">
        <button type="button" class="mini" id="dup-continue">${esc(okLabel)}</button>
        <button type="button" class="mini ghost" id="dup-cancel">取消</button>
      </div>
    </div>`;
  $('#dup-continue').onclick = () => {
    fd.set('force', 'true');
    doUpload(fd);
  };
  $('#dup-cancel').onclick = () => { $('#upload-result').textContent = '已取消'; };
}

async function gotoUploaded(key) {
  document.querySelector('.tabs button[data-tab="library"][data-mode="paper"]').click();
  await loadLibrary();
  const it = state.items.find(x => x.key === key);
  if (!it) return;
  state.collapsed.delete(it.topic);
  state.collapsed.delete(it.topic + '/' + it.year);
  saveCollapsed();
  state.current = it;
  renderList();
  showDetail(it);
  const el = document.querySelector(`#list .item[data-key="${CSS.escape(key)}"]`);
  if (el) el.scrollIntoView({ block: 'center' });
}

let uploadPending = false;

async function doUpload(fd) {
  if (uploadPending) return;
  uploadPending = true;
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;
  $('#upload-result').textContent = '上传中…';
  try {
    const r = await runLongOperation('/api/upload', { method: 'POST', body: fd }, $('#upload-result'));
    const got = [r.pdf_fetched ? 'PDF' : '', r.source_fetched ? '源码' : ''].filter(Boolean).join(' + ');
    const key = `${r.meta.topic}/${r.meta.year}/${r.meta.slug}`;
    const warns = (r.warnings || []).map(w => `<span class="warn-text">⚠ ${esc(w)}</span>`).join(' ');
    $('#upload-result').innerHTML =
      `<span class="ok">完成 → ${esc(r.remote_path)}${r.entry ? ' · 入口 ' + esc(r.entry) : ''}` +
      `${got ? ' · 已自动抓取 ' + got : ''}</span> ` +
      '<button type="button" class="mini" id="goto-uploaded">在论文库查看</button>' +
      (warns ? `<div class="muted">${warns}</div>` : '');
    $('#goto-uploaded').onclick = () => gotoUploaded(key);
    form.reset();
    $('#arxiv-status').textContent = '';
    $('#openreview-status').textContent = '';
    $('#ieee-status').textContent = '';
    lastIeeeDoi = '';
    showOpenReviewLogin(false);
    dropStatus.textContent = '';
    $('#slug-manual').checked = false;
    $('#slug-input').readOnly = true;
    $('#slug-input').value = '';
    ['#topic-new', '#year-new', '#venue-new'].forEach(s => $(s).classList.add('hidden'));
    loadOptions(true);
    await Promise.all([loadLibrary(), loadResearch()]);
  } catch (e) {
    if (e.status === 409 && e.detail && e.detail.duplicates) {
      showDupPrompt(e.detail.duplicates, fd, e.detail.message || '库里可能已有相同论文');
      return;
    }
    uploadError(e.message);
  } finally {
    uploadPending = false;
    submit.disabled = false;
  }
}

form.onsubmit = async (ev) => {
  ev.preventDefault();
  if (uploadPending) return;
  const source = activeSource();
  const arxivUrl = source === 'arxiv' ? $('#arxiv-url').value.trim() : '';
  const orUrl = source === 'openreview' ? $('#openreview-url').value.trim() : '';
  const ieeeUrl = source === 'ieee' ? $('#ieee-url').value.trim() : '';
  if (!arxivUrl && orUrl && !$('#slug-input').value.trim()) {
    const meta = await parseOpenReview();
    if (!meta) return;
  }
  if (arxivUrl && !$('#slug-input').value.trim()) {
    const meta = await parseArxiv();
    if (!meta) return;
  }
  if (!arxivUrl && !orUrl && ieeeUrl && !$('#slug-input').value.trim()) {
    const meta = await parseIeee();
    if (!meta) return;
  }
  composeSlug();
  const topic = pickValue('topic');
  const year = pickValue('year');
  const venue = pickValue('venue');
  const slug = $('#slug-input').value.trim();
  if (!topic) return uploadError('请选择主题或新建主题');
  if (!/^\d{4}$/.test(year)) return uploadError('请选择年份');
  if (!venue) return uploadError('请选择会议/期刊');
  if (!slug) return uploadError('slug 为空:填写会议/作者/关键词,或勾选「手动修改」');
  if (!slug.startsWith(year + '-')) return uploadError('slug 的年份前缀与所选年份不一致');

  const fd = new FormData(form);
  fd.set('topic', topic);
  fd.set('year', year);
  fd.set('venue', venue);
  fd.set('slug', slug);
  fd.set('copy_local', form.querySelector('input[name="copy_local"]').checked ? 'true' : 'false');
  fd.set('fetch_source', form.querySelector('input[name="fetch_source"]').checked ? 'true' : 'false');
  const hasPdf = fd.get('pdf') && fd.get('pdf').size > 0;
  const hasSrc = fd.get('source_archive') && fd.get('source_archive').size > 0;
  if (!hasPdf && !hasSrc && !arxivUrl && !orUrl && !ieeeUrl) {
    return uploadError('至少选择一个文件，或填 arXiv / OpenReview / IEEE 链接');
  }

  $('#upload-result').textContent = '检查重复…';
  const q = new URLSearchParams({
    title: (fd.get('title') || '').trim(),
    arxiv: (fd.get('arxiv') || '').trim(),
    openreview: orUrl,
    doi: source === 'ieee' ? lastIeeeDoi : '',
    slug, topic, year,
  });
  try {
    const dup = await api('/api/duplicates?' + q.toString());
    if (dup.same_key) {
      showDupPrompt(
        [{ key: `${topic}/${year}/${slug}`, title: (fd.get('title') || '').trim(), reasons: ['slug'] }],
        fd, '该 slug 已存在，继续会补充/替换这篇的 pdf 或 tex', '继续补充');
      return;
    }
    if (dup.duplicates.length) {
      showDupPrompt(dup.duplicates, fd, '库里可能已有相同论文，确认仍要新建一条？');
      return;
    }
  } catch (e) {
    // 检查失败不阻断上传，交给服务端再判一次
  }
  doUpload(fd);
};

/* ---------- 项目引用 ---------- */

async function initRefs() {
  if (!state.projects.length) renderViewState($('#project-list'), 'loading', '正在读取项目');
  $('#project-add-status').textContent = '';
  renderProjectSource();
  try {
    const items = await loadProjects(true);
    renderProjects();
    if (!items.length) {
      state.project = '';
      state.refs = [];
      state.refsSession = null;
      $('#refs-project').textContent = '';
      $('#refs-file').textContent = '';
      $('#refs-current').innerHTML = '<p class="muted pad">还没有项目，先在左侧添加</p>';
      resetProjectFiles('还没有项目，先在左侧添加');
      return;
    }
    const previous = state.project;
    if (!state.project || !items.some(p => p.project === state.project)) {
      state.project = items[0].project;
    }
    state.projectSelection[state.projectSource] = state.project;
    if (previous !== state.project || !state.projectFiles.loaded) {
      resetProjectFiles(state.projectPane === 'files' ? '读取中…' : '选择左侧项目查看文件');
    }
    renderProjects();
    loadRefs();
    if (state.projectPane === 'files') loadProjectFiles();
  } catch (e) {
    renderViewState($('#project-list'), 'error', '项目读取失败', e.message, initRefs);
  }
}

function renderProjectSource() {
  const source = state.projectSource;
  $$('#project-source-seg button').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.source === source));
  $('#project-add-hint').textContent = source === 'local'
    ? '添加本机绝对路径下已存在的项目目录'
    : '添加远端 workshop 下的项目';
  $('#project-new').placeholder = source === 'local'
    ? '如 /home/you/projects/example-project'
    : '如 projects/example-project';
}

async function switchProjectSource(source) {
  const next = normalizeProjectSource(source);
  if (next === state.projectSource) return;
  rememberProjectSelection();
  state.projectSource = next;
  saveProjectSource(next);
  state.projects = state.projectsCache[next] || [];
  state.project = state.projectSelection[next] || '';
  state.refs = [];
  state.refsSession = null;
  resetProjectFiles('选择左侧项目查看文件');
  $('#refs-result').textContent = '';
  await initRefs();
}

function renderProjects() {
  const box = $('#project-list');
  if (!state.projects.length) {
    renderViewState(box, 'empty', '还没有项目', '在上方输入项目路径即可添加。');
    return;
  }
  box.innerHTML = sortProjectsByUsage(state.projects).map(p => `
    <div class="item ${state.project === p.project ? 'active' : ''}" data-project="${esc(p.project)}">
      <div class="item-main">
        <div class="slug">${esc(p.project)}</div>
        <div class="title">${p.exists ? (p.count ? '已引用论文' : '尚无引用') : '目录不存在'}</div>
      </div>
      <div class="badges">
        ${p.exists ? '' : '<span class="badge warn">缺失</span>'}
        ${p.has_refs ? `<span class="badge tex">${p.count}</span>` : '<span class="badge">空</span>'}
      </div>
    </div>`).join('');
  $$('#project-list .item').forEach(el => el.onclick = () => selectProject(el.dataset.project));
}

function selectProject(project) {
  state.project = project;
  touchProject(project);
  state.projects = sortProjectsByUsage(state.projects);
  state.projectsCache[state.projectSource] = state.projects;
  state.projectSelection[state.projectSource] = project;
  renderProjects();
  resetProjectFiles(state.projectPane === 'files' ? '读取中…' : '选择左侧项目查看文件');
  loadRefs();
  if (state.projectPane === 'files') loadProjectFiles();
}

async function loadRefs() {
  if (!state.project) return;
  const session = {
    project: state.project, source: state.projectSource,
    revision: null, queue: Promise.resolve(), sequence: 0, failed: false,
  };
  state.refsSession = session;
  state.refs = [];
  $('#refs-project').textContent = session.project;
  $('#refs-file').textContent = '';
  renderViewState($('#refs-current'), 'loading', '正在读取项目引用');
  $('#refs-result').textContent = '';
  try {
    const data = await api('/api/refs?project=' + encodeURIComponent(session.project) +
      '&source=' + encodeURIComponent(session.source));
    if (state.refsSession !== session) return;
    state.refs = data.references || [];
    session.revision = data.revision;
    $('#refs-file').textContent = data.file + (data.exists ? '' : '（文件不存在，首次加入引用时会创建）');
    renderRefs();
    $('#refs-result').textContent = '';
  } catch (e) {
    if (state.refsSession === session) {
      renderViewState($('#refs-current'), 'error', '引用读取失败', e.message, loadRefs);
    }
  }
}

async function saveRefs(msg) {
  const session = state.refsSession;
  if (!session || session.revision === null || session.failed) {
    $('#refs-result').textContent = '尚未加载或上次保存失败，请保留输入并重新加载项目后再保存';
    return null;
  }
  const references = structuredClone(state.refs);
  const sequence = ++session.sequence;
  $('#refs-result').textContent = '保存中…';
  session.queue = session.queue.then(async () => {
    if (session.failed) return null;
    try {
      const r = await api('/api/refs', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project: session.project, source: session.source,
          references, revision: session.revision }),
      });
      session.revision = r.revision;
      if (state.refsSession === session && sequence === session.sequence && msg) {
        $('#refs-result').innerHTML = `<span class="ok">${esc(msg)}</span>`;
      }
      invalidateProjects(session.source);
      return r;
    } catch (e) {
      session.failed = true;
      if (state.refsSession === session) {
        $('#refs-result').innerHTML = `<span class="error">${esc(e.message)}（未保存，请保留输入）</span>`;
      }
      return null;
    }
  });
  return session.queue;
}

function pdfViewerUrl(pdfUrl, title) {
  return '/pdf-view.html?title=' + encodeURIComponent(title || 'PDF') +
    '&src=' + encodeURIComponent(pdfUrl);
}

async function openPdfTab(urlFactory, title) {
  await loadPdfToken();
  const anchor = document.createElement('a');
  anchor.href = pdfViewerUrl(urlFactory(), title);
  anchor.target = '_blank';
  anchor.rel = 'noopener noreferrer';
  anchor.style.display = 'none';
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
}

let activePreview = null;
let imageScale = 1;
let imageFits = true;

async function openImagePreview(title, urlFactory) {
  const opener = activePreview?.opener || document.activeElement;
  if (activePreview) closePreview();
  const modal = $('#image-modal');
  const status = $('#image-preview-status');
  const media = $('#image-modal-img');
  const session = { modal, opener };
  activePreview = session;
  $('#image-modal-title').textContent = title;
  media.classList.add('hidden');
  renderViewState(status, 'loading', '正在加载预览', '正在从远端读取文件…');
  modal.classList.remove('hidden');
  modal.showModal();
  document.body.classList.add('preview-open');
  $('#image-modal-close').focus();
  imageFits = true;
  media.alt = title;
  setImageControls(false);
  await loadPdfToken();
  if (activePreview !== session) return;
  const fail = message => {
    if (activePreview !== session) return;
    media.classList.add('hidden');
    renderViewState(status, 'error', '预览加载失败', message,
      () => openImagePreview(title, urlFactory));
  };
  media.onload = () => {
    if (activePreview !== session) return;
    status.innerHTML = '';
    media.classList.remove('hidden');
    setImageControls(true);
    fitImage();
  };
  media.onerror = () => fail('请检查网络连接和文件是否存在，然后重试。');
  media.src = urlFactory();
}

function closePreview() {
  if (!activePreview) return;
  const { modal, opener } = activePreview;
  activePreview = null;
  const media = $('#image-modal-img');
  media.onload = null;
  media.onerror = null;
  media.removeAttribute('src');
  modal.close();
  modal.classList.add('hidden');
  document.body.classList.remove('preview-open');
  if (opener?.isConnected) opener.focus();
}

function setImageControls(enabled) {
  for (const id of ['image-zoom-in', 'image-zoom-out', 'image-fit', 'image-actual']) {
    $('#' + id).disabled = !enabled;
  }
  if (!enabled) $('#image-zoom-value').textContent = '等待加载';
}

function setImageScale(scale, fit = false) {
  const image = $('#image-modal-img');
  if (!image.naturalWidth || image.classList.contains('hidden')) return;
  imageFits = fit;
  imageScale = fit ? scale : Math.max(0.1, Math.min(4, scale));
  image.style.width = Math.round(image.naturalWidth * imageScale) + 'px';
  $('#image-zoom-value').textContent = Math.round(imageScale * 100) + '%';
  $('#image-fit').setAttribute('aria-pressed', String(fit));
  $('#image-zoom-in').disabled = imageScale >= 4;
  $('#image-zoom-out').disabled = imageScale <= 0.1;
}

function fitImage() {
  const image = $('#image-modal-img');
  const box = $('#image-modal .image-body');
  if (!image.naturalWidth) return;
  setImageScale(Math.min(1, Math.max(1, box.clientWidth - 40) / image.naturalWidth,
    Math.max(1, box.clientHeight - 40) / image.naturalHeight), true);
  box.scrollTop = 0;
  box.scrollLeft = 0;
}

$('#image-zoom-in').onclick = () => setImageScale(imageScale * 1.25);
$('#image-zoom-out').onclick = () => setImageScale(imageScale / 1.25);
$('#image-fit').onclick = fitImage;
$('#image-actual').onclick = () => setImageScale(1);
window.addEventListener('resize', () => {
  if (activePreview && imageFits) fitImage();
});
(() => {
  const modal = $('#image-modal');
  $('#image-modal-close').onclick = closePreview;
  modal.onclick = e => { if (e.target === modal) closePreview(); };
  modal.oncancel = e => { e.preventDefault(); closePreview(); };
})();
document.addEventListener('keydown', e => {
  if (!activePreview) return;
  if (e.key === 'Escape') { e.preventDefault(); closePreview(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const action = {
    '+': () => setImageScale(imageScale * 1.25),
    '=': () => setImageScale(imageScale * 1.25),
    '-': () => setImageScale(imageScale / 1.25),
    '0': () => setImageScale(1),
    f: fitImage,
  }[e.key.toLowerCase()];
  if (action) { e.preventDefault(); action(); }
});

/* ---------- 项目文件 ---------- */

function projectUpPath(path) {
  const parts = String(path || '').split('/').filter(Boolean);
  parts.pop();
  return parts.join('/');
}

function projectTimeText(ts) {
  const date = new Date(Number(ts || 0) * 1000);
  if (!ts || Number.isNaN(date.getTime())) return '';
  const pad = n => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function resetProjectFiles(message = '选择左侧项目查看文件') {
  const requestId = state.projectFiles.requestId + 1;
  state.projectFiles = {
    requestId, mode: 'recent', path: '', entries: [], files: [], truncated: false, loaded: false,
  };
  renderViewState($('#files-list'), 'empty', message);
  $('#files-path').textContent = '';
  $('#files-result').textContent = '';
  $('#files-recent').classList.remove('active');
  $('#files-root').classList.remove('active');
  $('#files-up').disabled = true;
}

function renderProjectTypes() {
  $('#files-types').innerHTML = PROJECT_TYPES.map(t => `
    <label class="type-chip${state.projectTypes.has(t.id) ? ' on' : ''}">
      <input type="checkbox" data-type="${t.id}"${state.projectTypes.has(t.id) ? ' checked' : ''}> ${t.label}
    </label>`).join('');
  $$('#files-types input[data-type]').forEach(input => {
    input.onchange = () => {
      if (input.checked) state.projectTypes.add(input.dataset.type);
      else state.projectTypes.delete(input.dataset.type);
      saveProjectTypes();
      input.closest('label').classList.toggle('on', input.checked);
      if (state.project && state.projectPane === 'files') loadProjectFiles();
    };
  });
}

function setProjectPane(pane) {
  state.projectPane = pane === 'files' ? 'files' : 'refs';
  $$('#project-pane-seg button').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.pane === state.projectPane));
  $('#refs-pane').classList.toggle('hidden', state.projectPane !== 'refs');
  $('#files-pane').classList.toggle('hidden', state.projectPane !== 'files');
  if (state.projectPane === 'files' && state.project && !state.projectFiles.loaded) loadProjectFiles();
}

async function loadProjectFiles(mode = state.projectFiles.mode, path = state.projectFiles.path) {
  if (!state.project) return;
  const files = state.projectFiles;
  const requestId = ++files.requestId;
  const project = state.project;
  const source = state.projectSource;
  files.mode = mode === 'dir' ? 'dir' : 'recent';
  files.path = files.mode === 'dir' ? String(path || '').replace(/^\/+|\/+$/g, '') : '';
  files.loaded = false;
  $('#files-result').textContent = '';
  renderFileNavigation();
  renderViewState($('#files-list'), 'loading', '正在读取项目文件', '只读访问，不会修改项目内容。');
  const types = encodeURIComponent([...state.projectTypes].join(','));
  try {
    const base = files.mode === 'recent'
      ? `/api/project/recent?project=${encodeURIComponent(project)}&source=${encodeURIComponent(source)}&types=${types}`
      : `/api/project/files?project=${encodeURIComponent(project)}&source=${encodeURIComponent(source)}&path=${encodeURIComponent(files.path)}&types=${types}`;
    const data = await api(base);
    if (requestId !== state.projectFiles.requestId || project !== state.project ||
        source !== state.projectSource) return;
    if (files.mode === 'recent') {
      files.entries = [];
      files.files = data.files || [];
      files.truncated = false;
    } else {
      const entries = data.entries || [];
      files.entries = entries.filter(entry => entry.kind === 'dir');
      files.files = entries.filter(entry => entry.kind === 'file');
      files.truncated = !!data.truncated;
    }
    files.loaded = true;
    renderProjectFiles();
  } catch (e) {
    if (requestId !== state.projectFiles.requestId || project !== state.project ||
        source !== state.projectSource) return;
    renderViewState($('#files-list'), 'error', '文件读取失败', e.message, () => loadProjectFiles());
  }
}

function projectEntryPath(file, directory) {
  return file.path || [directory, file.name].filter(Boolean).join('/');
}

function projectBreadcrumbs(path) {
  const parts = String(path || '').split('/').filter(Boolean);
  return [{ label: '项目根目录', path: '' }, ...parts.map((label, i) => ({
    label, path: parts.slice(0, i + 1).join('/'),
  }))];
}

function renderFileNavigation() {
  const files = state.projectFiles;
  $('#files-recent').classList.toggle('active', files.mode === 'recent');
  $('#files-root').classList.toggle('active', files.mode === 'dir' && !files.path);
  $('#files-up').disabled = !(files.mode === 'dir' && files.path);
  const crumbs = projectBreadcrumbs(files.path);
  if (files.mode === 'recent') crumbs.push({ label: '最近修改', path: null });
  $('#files-path').innerHTML = crumbs.map((crumb, i) =>
    i === crumbs.length - 1
      ? `<span aria-current="page" tabindex="-1">${esc(crumb.label)}</span>`
      : `<button type="button" class="breadcrumb" data-path="${esc(crumb.path)}">${esc(crumb.label)}</button><span aria-hidden="true">/</span>`
  ).join('');
  $$('#files-path button').forEach(button => button.onclick = () => browseProjectPath(button.dataset.path));
}

function renderProjectFiles() {
  const files = state.projectFiles;
  renderFileNavigation();
  $('#files-result').textContent = files.truncated ? '条目过多，已截断显示' : `${files.files.length} 个文件`;

  const rows = [];
  if (files.mode === 'dir' && files.path) {
    rows.push(`<div class="file-row" data-up="1">
      <span class="file-icon">↰</span>
      <button type="button" class="file-entry">返回上一级</button>
    </div>`);
  }
  for (const dir of files.entries) {
    rows.push(`<div class="file-row" data-dir="${esc(dir.name)}">
      <span class="file-icon">▸</span>
      <button type="button" class="file-entry">${esc(dir.name)}</button>
    </div>`);
  }
  for (const file of files.files) {
    const path = projectEntryPath(file, files.path);
    const name = file.name || path.split('/').pop();
    const dir = files.mode === 'recent' ? path.slice(0, Math.max(0, path.lastIndexOf('/'))) : '';
    const meta = [dir, byteText(file.size), projectTimeText(file.mtime)]
      .filter(Boolean).map(esc).join(' · ');
    rows.push(`<div class="file-row" data-file="${esc(path)}" data-type="${esc(file.type)}">
      <span class="badge ${file.type === 'pdf' ? 'pdf' : (file.type === 'md' ? 'note' : 'tex')}">${esc(String(file.type || '').toUpperCase())}</span>
      <div class="file-main">
        <div class="slug">${esc(name)}</div>
        <div class="title">${meta}</div>
      </div>
      <button type="button" class="mini" aria-label="预览 ${esc(name)}">预览</button>
    </div>`);
  }
  $('#files-list').innerHTML = rows.join('');
  if (!files.entries.length && !files.files.length) {
    const empty = document.createElement('div');
    $('#files-list').append(empty);
    renderViewState(empty, 'empty', state.projectTypes.size ? '没有可显示的文件' : '没有选择文件类型',
      state.projectTypes.size ? '试试其他目录或文件类型。最近修改不含 references 和隐藏目录。' : '勾选上方的文件类型后即可查看。');
  }
  const upRow = $('#files-list [data-up]');
  if (upRow) upRow.onclick = () => browseProjectPath(projectUpPath(state.projectFiles.path));
  $$('#files-list [data-dir]').forEach(row => {
    row.onclick = () => {
      const base = state.projectFiles.path;
      browseProjectPath(base ? base + '/' + row.dataset.dir : row.dataset.dir);
    };
  });
  $$('#files-list [data-file]').forEach(row => {
    row.onclick = () => {
      const path = row.dataset.file;
      const name = path.split('/').pop();
      row.querySelector('button').focus();
      if (row.dataset.type === 'pdf') openProjectPdf(path, name);
      else if (row.dataset.type === 'md') openProjectMarkdown(path, name);
      else openProjectImage(path, name);
    };
  });
}

async function browseProjectPath(path) {
  const project = state.project;
  await loadProjectFiles('dir', path);
  if (project === state.project && state.projectFiles.mode === 'dir' && state.projectFiles.path === path) {
    $('#files-path [aria-current]')?.focus();
  }
}

function showRecentProjectFiles() {
  loadProjectFiles('recent', '');
}

async function openProjectPdf(path, title) {
  const project = state.project;
  const source = state.projectSource;
  await openPdfTab(() => projectFileUrl(project, path, source), title || path);
}

async function openProjectImage(path, title) {
  const project = state.project;
  const source = state.projectSource;
  return openImagePreview(title || path, () => projectFileUrl(project, path, source));
}

let activeMarkdown = null;

async function openProjectMarkdown(path, title) {
  const project = state.project;
  const source = state.projectSource;
  const opener = activeMarkdown?.opener || document.activeElement;
  if (activeMarkdown) closeMarkdownPreview();
  const modal = $('#markdown-modal');
  const body = $('#markdown-modal-body');
  const session = { modal, opener, requestId: 0 };
  activeMarkdown = session;
  $('#markdown-modal-title').textContent = title || path.split('/').pop() || 'Markdown';
  $('#markdown-modal-meta').textContent = path;
  renderViewState(body, 'loading', '正在读取 Markdown', '只读预览，不会修改项目文件。');
  modal.classList.remove('hidden');
  modal.showModal();
  document.body.classList.add('preview-open');
  $('#markdown-modal-close').focus();
  const load = async () => {
    const requestId = ++session.requestId;
    renderViewState(body, 'loading', '正在读取 Markdown', '只读预览，不会修改项目文件。');
    try {
      const url = `/api/project/markdown?project=${encodeURIComponent(project)}` +
        `&source=${encodeURIComponent(source)}&path=${encodeURIComponent(path)}`;
      const data = await api(url);
      if (activeMarkdown !== session || requestId !== session.requestId) return;
      $('#markdown-modal-meta').textContent = [path, byteText(data.size || 0)].join(' · ');
      body.innerHTML = PapertexMarkdown.render(data.text, {
        emptyText: '这个 Markdown 文件没有正文。',
      });
    } catch (e) {
      if (activeMarkdown !== session || requestId !== session.requestId) return;
      renderViewState(body, 'error', 'Markdown 读取失败', e.message, load);
    }
  };
  await load();
}

function closeMarkdownPreview() {
  if (!activeMarkdown) return;
  const { modal, opener } = activeMarkdown;
  activeMarkdown = null;
  modal.close();
  modal.classList.add('hidden');
  $('#markdown-modal-body').innerHTML = '';
  document.body.classList.remove('preview-open');
  if (opener?.isConnected) opener.focus();
}

(() => {
  const modal = $('#markdown-modal');
  $('#markdown-modal-close').onclick = closeMarkdownPreview;
  modal.onclick = e => { if (e.target === modal) closeMarkdownPreview(); };
  modal.oncancel = e => { e.preventDefault(); closeMarkdownPreview(); };
})();

function refRow(r, i) {
  const cur = r.current || {};
  if (String(r.path || '').startsWith('_knowledge/')) {
    const kind = cur.kind || (r.entry === 'note.md' ? 'note' : (r.url ? 'link' : ''));
    const isNote = kind === 'note';
    const url = /^https?:\/\//i.test(r.url || cur.url || '') ? (r.url || cur.url) : '';
    const badges = [
      r.missing ? '<span class="badge warn">路径缺失</span>' : '',
      r.stale ? '<span class="badge warn">库有更新</span>' : '',
      isNote ? '<span class="badge note">笔记</span>' : (url ? '<span class="badge tex">链接</span>' : ''),
    ].join('');
    return `
    <div class="ref-row">
      <div class="path">
        <div class="slug">${esc(r.title || cur.title || r.path)}</div>
        <div class="muted">${esc(r.path)}</div>
        ${isNote ? '<div class="muted">entry: note.md</div>' : ''}
        ${url ? `<div class="muted">链接: <a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a></div>` : ''}
        ${r.stale ? `<div class="muted">库中最新: ${isNote ? 'entry note.md' : '链接 ' + esc(cur.url || '—')}</div>` : ''}
      </div>
      <div class="badges">${badges}</div>
      <input class="note" data-note="${i}" value="${esc(r.note || '')}" placeholder="备注">
      ${(isNote && !r.missing) ? `<button type="button" class="mini note-toggle" data-note-toggle="${i}" aria-expanded="false" aria-controls="note-panel-${i}">预览笔记</button>` : ''}
      ${(isNote && !r.missing) ? `<button class="mini" data-open-local="${i}">用本地软件打开</button>` : ''}
      <button class="mini" data-sync="${i}" ${r.missing ? 'disabled' : ''}>按库更新</button>
      <button class="mini danger" data-remove="${i}">移除</button>
      ${(isNote && !r.missing) ? `<div class="note-panel hidden" id="note-panel-${i}" role="region" aria-label="${esc(r.title || cur.title || '笔记')}">
        <div class="markdown-body"></div>
      </div>` : ''}
    </div>`;
  }
  const refCode = /^https?:\/\//i.test(r.code || '') ? r.code : '';
  const curCode = /^https?:\/\//i.test(cur.code || '') ? cur.code : '';
  const code = refCode || curCode;
  const refOr = /^https?:\/\//i.test(r.openreview || '') ? r.openreview : '';
  const curOr = /^https?:\/\//i.test(cur.openreview || '') ? cur.openreview : '';
  const orUrl = refOr || curOr;
  const badges = [
    r.missing ? '<span class="badge warn">路径缺失</span>' : '',
    r.stale ? '<span class="badge warn">库有更新</span>' : '',
    cur.entry ? '<span class="badge tex">TEX</span>' : '',
    cur.pdf ? '<span class="badge pdf">PDF</span>' : '',
    code ? '<span class="badge code">CODE</span>' : '',
  ].join('');
  return `
    <div class="ref-row">
      <div class="path">
        <div class="slug">${esc(r.path)}</div>
        ${(r.title || cur.title) ? `<div class="title">${esc(displayTitle(r.title || cur.title))}</div>` : ''}
        <div class="muted">entry: ${r.entry ? esc(r.entry) : '—'} · pdf: ${r.pdf ? esc(r.pdf) : '—'}</div>
        ${code ? `<div class="muted">code: <a href="${esc(code)}" target="_blank" rel="noopener noreferrer">${esc(code)}</a></div>` : ''}
        ${orUrl ? `<div class="muted">openreview: <a href="${esc(orUrl)}" target="_blank" rel="noopener noreferrer">${esc(orUrl)}</a></div>` : ''}
        ${r.stale ? `<div class="muted">库中最新: entry ${esc(cur.entry || '—')} · pdf ${esc(cur.pdf || '—')}${curCode && curCode !== refCode ? ' · code ' + esc(curCode) : ''}</div>` : ''}
      </div>
      <div class="badges">${badges}</div>
      <input class="note" data-note="${i}" value="${esc(r.note || '')}" placeholder="备注">
      ${(!r.missing && (cur.pdf || r.pdf)) ? `<button class="mini" data-preview="${i}">预览 pdf</button>` : ''}
      <button class="mini" data-sync="${i}" ${r.missing ? 'disabled' : ''}>按库更新</button>
      <button class="mini danger" data-remove="${i}">移除</button>
    </div>`;
}

async function loadNotePreview(panel, body, id) {
  body.innerHTML = '<p class="muted">正在读取笔记…</p>';
  try {
    const data = await api('/api/knowledge/' + encodeURIComponent(id));
    const item = data.item || {};
    body.innerHTML = PapertexMarkdown.render(item.body || '', {
      emptyText: '这篇笔记还没有正文，可以点「用本地软件打开」继续写。',
    });
    panel.dataset.loaded = '1';
  } catch (e) {
    renderViewState(body, 'error', '笔记读取失败', e.message,
      () => loadNotePreview(panel, body, id));
  }
}

function toggleNotePreview(i, btn) {
  const panel = document.getElementById('note-panel-' + i);
  if (!panel) return;
  const expanded = btn.getAttribute('aria-expanded') === 'true';
  if (expanded) {
    panel.classList.add('hidden');
    btn.setAttribute('aria-expanded', 'false');
    btn.textContent = '预览笔记';
    return;
  }
  panel.classList.remove('hidden');
  btn.setAttribute('aria-expanded', 'true');
  btn.textContent = '收起笔记';
  if (panel.dataset.loaded === '1') return;
  const id = String(state.refs[i]?.path || '').split('/')[1] || '';
  if (!id) {
    panel.querySelector('.markdown-body').innerHTML =
      '<p class="muted">无法识别这篇笔记的资料编号。</p>';
    return;
  }
  if (panel.dataset.loading === '1') return;
  panel.dataset.loading = '1';
  loadNotePreview(panel, panel.querySelector('.markdown-body'), id)
    .finally(() => { delete panel.dataset.loading; });
}

function renderRefs() {
  if (!state.refs.length) {
    renderViewState($('#refs-current'), 'empty', '这个项目还没有引用',
      '去「资料库」选中论文或资料，在详情里点「加入项目」。');
    return;
  }
  $('#refs-current').innerHTML = state.refs.map(refRow).join('');

  $$('#refs-current input[data-note]').forEach(inp => {
    inp.onchange = async () => {
      const i = Number(inp.dataset.note);
      state.refs[i].note = inp.value.trim();
      await saveRefs('备注已保存');
    };
  });
  $$('#refs-current button[data-preview]').forEach(btn => {
    btn.onclick = () => {
      const ref = state.refs[Number(btn.dataset.preview)];
      const parts = String(ref.path || '').split('/');
      if (parts.length !== 3) return;
      const [topic, year, slug] = parts;
      const source = state.refsSession?.source || 'remote';
      openPdfTab(() => paperPdfUrl(source, topic, year, slug), slug);
    };
  });
  $$('#refs-current button[data-note-toggle]').forEach(btn => {
    btn.onclick = () => toggleNotePreview(Number(btn.dataset.noteToggle), btn);
  });
  $$('#refs-current button[data-open-local]').forEach(btn => {
    btn.onclick = async () => {
      const ref = state.refs[Number(btn.dataset.openLocal)];
      const id = String(ref.path).split('/')[1] || '';
      btn.disabled = true;
      try {
        await api('/api/knowledge/' + encodeURIComponent(id) + '/open', { method: 'POST' });
        toast('已用本地软件打开 note.md', 'ok');
      } catch (e) {
        toast(e.message, 'error');
      } finally {
        btn.disabled = false;
      }
    };
  });
  $$('#refs-current button[data-sync]').forEach(btn => {
    btn.onclick = async () => {
      const ref = state.refs[Number(btn.dataset.sync)];
      $('#refs-result').textContent = '更新中…';
      try {
        const r = await runLongOperation('/api/refs/sync', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project: state.project, source: state.projectSource,
            path: ref.path }),
        }, $('#refs-result'));
        invalidateProjects(state.projectSource);
        await loadRefs();
        $('#refs-result').innerHTML = r.updated.length
          ? '<span class="ok">已按库更新</span>'
          : '<span class="muted">库中信息没有变化</span>';
      } catch (e) {
        $('#refs-result').innerHTML = `<span class="error">${esc(e.message)}</span>`;
      }
    };
  });
  $$('#refs-current button[data-remove]').forEach(btn => {
    btn.onclick = async () => {
      const i = Number(btn.dataset.remove);
      const ref = state.refs[i];
      const ok = await confirmDialog({
        title: '移除引用',
        text: `从项目 ${state.project} 移除 ${ref.path}？只改引用列表，不会删除库中内容。`,
        confirmLabel: '移除',
      });
      if (!ok) return;
      const session = state.refsSession;
      state.refs.splice(i, 1);
      const r = await saveRefs();
      if (state.refsSession !== session) return;
      if (r) {
        renderRefs();
        invalidateProjects(state.projectSource);
        $('#refs-result').innerHTML = '<span class="ok">已移除</span>';
      } else {
        await loadRefs();
      }
    };
  });
}

/* ---------- 绑定 ---------- */

let searchTimer = null;

function syncSearch() {
  clearTimeout(searchTimer);
  state.search = $('#search').value;
}

$('#search').oninput = e => {
  const value = e.target.value;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.search = value;
    renderList();
  }, 120);
};
$('#search').onkeydown = e => {
  if (e.key === 'Enter') {
    e.preventDefault();
    syncSearch();
    renderList();
  } else if (e.key === 'Escape' && e.target.value) {
    e.stopPropagation();
    e.target.value = '';
    syncSearch();
    renderList();
  }
};
$('#filter-topic').onchange = e => { syncSearch(); state.topicFilter = e.target.value; renderList(); };
$('#filter-year').onchange = e => { syncSearch(); state.yearFilter = e.target.value; renderList(); };
$('#filter-venue').onchange = e => { syncSearch(); state.venueFilter = e.target.value; renderList(); };
$('#sort-mode').onchange = e => {
  state.sortMode = SORT_MODES.includes(e.target.value) ? e.target.value : 'default';
  saveSortMode(state.sortMode);
  syncLibraryMode();
  renderList();
};
$('#refresh').onclick = loadLibrary;
$('#knowledge-add').onclick = () => openKnowledgeEditor(null);
$('#knowledge-form').onsubmit = submitKnowledge;
$('#knowledge-cancel').onclick = closeKnowledgeModal;
$('#knowledge-modal-close').onclick = closeKnowledgeModal;
$('#knowledge-modal').onclick = e => {
  if (e.target === $('#knowledge-modal')) closeKnowledgeModal();
};
$('#knowledge-modal').oncancel = e => {
  e.preventDefault();
  closeKnowledgeModal();
};
(() => {
  const input = $('#knowledge-md');
  $('#knowledge-md-btn').onkeydown = e => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      input.click();
    }
  };
  input.onchange = async () => {
    const file = input.files[0];
    input.value = '';
    if (!file) return;
    const status = $('#knowledge-status');
    try {
      const text = await file.text();
      if (text.length > 100000) {
        status.innerHTML = '<span class="error">Markdown 文件超过 100000 字符，未导入</span>';
        return;
      }
      $('#knowledge-body').value = text;
      const title = $('#knowledge-title');
      if (!title.value.trim()) {
        title.value = file.name.replace(/\.(md|markdown|txt)$/i, '').slice(0, 200);
      }
      status.innerHTML = `<span class="ok">已导入 ${esc(file.name)}（${text.length} 字符）</span>`;
    } catch (e) {
      status.innerHTML = `<span class="error">读取文件失败: ${esc(e.message)}</span>`;
    }
  };
})();
$('#topics-manage').onclick = openTopicsModal;
$('#topics-modal-close').onclick = closeTopicsModal;
$('#topics-modal').onclick = e => {
  if (e.target === $('#topics-modal')) closeTopicsModal();
};
$('#topics-modal').oncancel = e => {
  e.preventDefault();
  closeTopicsModal();
};
$('#expand-all').onclick = () => {
  state.collapsed.clear();
  saveCollapsed();
  renderList();
};
$('#collapse-all').onclick = () => {
  for (const it of state.items) {
    state.collapsed.add(it.topic);
    state.collapsed.add(it.topic + '/' + it.year);
  }
  saveCollapsed();
  renderList();
};
$('#only-gaps').onchange = e => { state.onlyGaps = e.target.checked; renderList(); };
$('#only-starred').onchange = e => { state.onlyStarred = e.target.checked; renderList(); };
$('#select-toggle').onclick = () => setSelectMode(!state.selectMode);
$('#bulk-exit').onclick = () => setSelectMode(false);
$('#bulk-none').onclick = clearPicks;
$('#bulk-all').onclick = selectVisible;
$('#bulk-add-project').onclick = openBulkProjectModal;
$('#bulk-cancel').onclick = closeBulkModal;
$('#bulk-ok').onclick = submitBulkAdd;
$('#bulk-modal').oncancel = e => {
  e.preventDefault();
  closeBulkModal();
};
$('#filter-reading').onchange = e => { state.readingFilter = e.target.value; renderList(); };
$('#filter-tex').onchange = e => { state.texFilter = e.target.value; renderList(); };
$('#refs-export-bib').onclick = exportProjectBibliography;

$$('#project-source-seg button').forEach(btn =>
  btn.onclick = () => switchProjectSource(btn.dataset.source));

$('#project-add').onclick = async () => {
  const input = $('#project-new');
  const source = state.projectSource;
  const raw = input.value.trim();
  const project = source === 'local'
    ? raw.replace(/\/+$/g, '')
    : raw.replace(/^\/+|\/+$/g, '');
  const st = $('#project-add-status');
  if (!project) {
    st.innerHTML = '<span class="error">先填项目路径</span>';
    return;
  }
  const addProject = extra => api('/api/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project, source, ...extra }),
  });
  st.textContent = '添加中…';
  try {
    let r;
    try {
      r = await addProject();
    } catch (e) {
      if (!(source === 'remote' && e.status === 404 && e.detail?.missing_dir)) throw e;
      st.textContent = '';
      const ok = await confirmDialog({
        title: '远端还没有这个目录',
        text: `workshop 下不存在 ${project}。仍要登记吗？登记后可以先浏览；第一次加入引用时会再次确认并创建目录和 reference-tex.json。`,
        confirmLabel: '仍然登记',
        danger: false,
      });
      if (!ok) {
        st.textContent = '已取消，未登记';
        return;
      }
      st.textContent = '登记中…';
      r = await addProject({ allow_missing: true });
    }
    input.value = '';
    st.innerHTML = r.missing
      ? `<span class="ok">已登记 ${esc(r.project)}</span><span class="muted">（远端目录尚不存在，首次加入引用时会创建）</span>`
      : `<span class="ok">已添加 ${esc(r.project)}${r.created ? '（已创建 reference-tex.json）' : ''}</span>`;
    invalidateProjects(source);
    await loadProjects(true);
    selectProject(r.project);
  } catch (e) {
    if (e.status === 404 && e.detail?.missing_dir) {
      st.innerHTML = `<span class="error">本机目录不存在：${esc(project)}（请先创建目录，再回来登记）</span>`;
    } else {
      st.innerHTML = `<span class="error">${esc(e.message)}</span>`;
    }
  }
};

$('#project-new').onkeydown = e => {
  if (e.key === 'Enter') {
    e.preventDefault();
    $('#project-add').click();
  }
};

$('#refs-remove-project').onclick = async () => {
  if (!state.project) return;
  const ok = await confirmDialog({
    title: '移出项目列表',
    text: `从列表移除 ${state.project}？只移除列表，不会删除项目里的 reference-tex.json。`,
    confirmLabel: '移出列表',
  });
  if (!ok) return;
  try {
    await api('/api/projects?project=' + encodeURIComponent(state.project) +
      '&source=' + encodeURIComponent(state.projectSource), { method: 'DELETE' });
    invalidateProjects(state.projectSource);
    state.projectSelection[state.projectSource] = '';
    state.project = '';
    state.refs = [];
    await loadProjects(true);
    renderProjects();
    $('#refs-project').textContent = '';
    $('#refs-file').textContent = '';
    $('#refs-current').innerHTML = '<p class="muted pad">先在左侧添加或选择项目</p>';
    resetProjectFiles();
    $('#refs-result').innerHTML = '<span class="ok">已移出列表</span>';
  } catch (e) {
    $('#refs-result').innerHTML = `<span class="error">${esc(e.message)}</span>`;
  }
};

$$('#project-pane-seg button').forEach(btn => btn.onclick = () => setProjectPane(btn.dataset.pane));
$('#files-recent').onclick = showRecentProjectFiles;
$('#files-root').onclick = () => browseProjectPath('');
$('#files-up').onclick = () => browseProjectPath(projectUpPath(state.projectFiles.path));
$('#files-refresh').onclick = () => loadProjectFiles();

$('#refs-sync-all').onclick = async () => {
  if (!state.project) return;
  $('#refs-result').textContent = '更新中…';
  try {
    const r = await runLongOperation('/api/refs/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project: state.project, source: state.projectSource }),
    }, $('#refs-result'));
    invalidateProjects(state.projectSource);
    await loadRefs();
    const parts = [`已按库更新 ${r.updated.length} 条`];
    if (r.missing.length) parts.push(`${r.missing.length} 条在库中缺失`);
    $('#refs-result').innerHTML = `<span class="${r.missing.length ? 'error' : 'ok'}">${esc(parts.join('，'))}</span>`;
  } catch (e) {
    $('#refs-result').innerHTML = `<span class="error">${esc(e.message)}</span>`;
  }
};

$('#mirror-fix').onclick = openMirrorModal;
$('#mirror-modal-close').onclick = closeMirrorModal;
$('#mirror-modal').onclick = e => { if (e.target === $('#mirror-modal')) closeMirrorModal(); };
$('#mirror-modal').oncancel = e => { e.preventDefault(); closeMirrorModal(); };
$$('#mirror-seg button').forEach(btn => btn.onclick = () => {
  mirrorState.direction = btn.dataset.direction;
  mirrorState.selected = new Set(mirrorItems().map(item => item.key));
  syncMirrorSeg();
  renderMirror();
});
$('#mirror-all').onchange = e => {
  mirrorState.selected = e.target.checked
    ? new Set(mirrorItems().map(item => item.key)) : new Set();
  renderMirror();
};
$('#mirror-run').onclick = async () => {
  const keys = mirrorItems()
    .filter(item => mirrorState.selected.has(item.key))
    .map(item => item.key);
  if (!keys.length) return;
  $('#mirror-run').disabled = true;
  try {
    await runLongOperation('/api/mirror/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ direction: mirrorState.direction, keys }),
    }, $('#mirror-result'));
    loadLibrary();
    await loadMirror();
  } catch (e) {
    $('#mirror-result').innerHTML = `<span class="error">${esc(e.message)}</span>`;
  } finally {
    renderMirror();
  }
};

$('#dup-scan').onclick = openDupModal;
$('#dup-modal-close').onclick = closeDupModal;
$('#dup-modal').onclick = e => { if (e.target === $('#dup-modal')) closeDupModal(); };
$('#dup-modal').oncancel = e => { e.preventDefault(); closeDupModal(); };
$('#dup-rescan').onclick = () => loadDupGroups();

$('#recovery-manage').onclick = openRecoveryModal;
$('#recovery-modal-close').onclick = closeRecoveryModal;
$('#recovery-modal').onclick = e => { if (e.target === $('#recovery-modal')) closeRecoveryModal(); };
$('#recovery-modal').oncancel = e => { e.preventDefault(); closeRecoveryModal(); };
$('#recovery-refresh').onclick = () => loadRecovery();
$('#recovery-purge-all').onclick = () => {
  if (!recoveryState.items.length) return;
  purgeRecoveryItems(recoveryState.items.map(item => item.source),
    recoveryState.items.map(item => item.id));
};

document.addEventListener('keydown', event => {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (isTypingTarget(event.target)) return;
  if (document.querySelector('dialog[open]')) return;
  if (!$('#tab-library').classList.contains('active')) return;
  const onItem = event.target instanceof Element && event.target.closest('#list .item');
  if (event.key === '/') {
    event.preventDefault();
    $('#search').focus();
    $('#search').select();
    return;
  }
  if (event.key === 'j' || (event.key === 'ArrowDown' && onItem)) {
    event.preventDefault();
    moveListSelection(1);
    return;
  }
  if (event.key === 'k' || (event.key === 'ArrowUp' && onItem)) {
    event.preventDefault();
    moveListSelection(-1);
  }
});

const READING_LABELS = { '': '未标记', unread: '待读', reading: '在读', read: '已读', reproduce: '待复现' };
const VERSION_LABELS = { '': '未标注', arxiv: 'arXiv 版', conference: '会议版', journal: '期刊版', other: '其他版本' };
let researchSaving = false;

function researchRecord(key) {
  return state.research[key] || {};
}

function paperTags(key) {
  const tags = researchRecord(key).tags;
  return Array.isArray(tags) ? tags : [];
}

const TAG_TONES = 10;

function tagTone(tag) {
  let hash = 0;
  for (const ch of String(tag)) hash = (hash * 31 + ch.codePointAt(0)) >>> 0;
  return 'tag-c' + (hash % TAG_TONES);
}

function allPaperTags() {
  const counts = new Map();
  Object.keys(state.research).forEach(key => paperTags(key).forEach(tag =>
    counts.set(tag, (counts.get(tag) || 0) + 1)));
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'zh-CN'));
}

function renderTagFilters() {
  const host = $('#tag-filters');
  if (!host) return;
  if (state.libraryMode !== 'paper') {
    host.innerHTML = '';
    host.classList.add('hidden');
    return;
  }
  const tags = state.researchError ? [] : allPaperTags();
  const known = new Set(tags.map(entry => entry[0]));
  [...state.tagFilter].filter(tag => !known.has(tag)).forEach(tag => state.tagFilter.delete(tag));
  if (!tags.length) {
    host.innerHTML = '';
    host.classList.add('hidden');
    return;
  }
  host.classList.remove('hidden');
  host.innerHTML = '<span class="tag-filter-label">标签</span>' +
    tags.map(([tag, count]) => `<button type="button" class="tag-chip pick ${tagTone(tag)}" data-tag="${esc(tag)}"
      aria-pressed="${state.tagFilter.has(tag) ? 'true' : 'false'}"
      title="${count} 篇">${esc(tag)} <span class="muted">${count}</span></button>`).join('') +
    (state.tagFilter.size
      ? '<button type="button" class="mini link" id="tag-clear">清除标签筛选</button>' : '');
  host.querySelectorAll('[data-tag]').forEach(btn => {
    btn.onclick = () => {
      const tag = btn.dataset.tag;
      if (state.tagFilter.has(tag)) state.tagFilter.delete(tag);
      else state.tagFilter.add(tag);
      syncSearch();
      renderList();
    };
  });
  const clear = host.querySelector('#tag-clear');
  if (clear) clear.onclick = () => { state.tagFilter.clear(); syncSearch(); renderList(); };
}

function bindTagEditor(panel, it) {
  const editor = panel.querySelector('#tag-editor');
  const form = panel.querySelector('#tag-form');
  const input = panel.querySelector('#tag-input');
  const result = panel.querySelector('#tag-result');
  if (!editor || !form) return;
  const paint = () => {
    const tags = paperTags(it.key);
    editor.innerHTML = tags.length
      ? tags.map(tag => `<button type="button" class="tag-chip removable ${tagTone(tag)}" data-tag="${esc(tag)}"
          aria-label="移除标签 ${esc(tag)}" title="点击移除">${esc(tag)} ✕</button>`).join('')
      : '<span class="muted">还没有标签</span>';
    panel.querySelector('#tag-options').innerHTML = allPaperTags()
      .map(([tag]) => `<option value="${esc(tag)}"></option>`).join('');
    editor.querySelectorAll('[data-tag]').forEach(btn => {
      btn.onclick = async () => {
        await save(paperTags(it.key).filter(tag => tag !== btn.dataset.tag));
      };
    });
  };
  const save = async tags => {
    input.disabled = true;
    result.textContent = '保存中…';
    try {
      await saveResearch('/api/tags', { key: it.key, tags });
      result.textContent = '已保存';
    } catch (e) {
      result.textContent = e.message;
    } finally {
      input.disabled = false;
      input.value = '';
      paint();
      input.focus();
    }
  };
  form.onsubmit = async event => {
    event.preventDefault();
    const typed = input.value.split(/[,，]/).map(part => part.trim()).filter(Boolean);
    if (!typed.length) {
      result.textContent = '标签不能为空';
      return;
    }
    await save(paperTags(it.key).concat(typed));
  };
  paint();
}

function researchBadge(key) {
  if (state.researchError) return '';
  const record = researchRecord(key);
  const label = READING_LABELS[record.status];
  const tags = paperTags(key).map(tag => {
    const hit = state.tagFilter.has(tag);
    return `<span class="badge item-tag ${tagTone(tag)}${hit ? ' tag-hit' : ''}">#${esc(tag)}</span>`;
  });
  return (record.status && label ? `<span class="badge reading-badge">${esc(label)}</span>` : '') +
    (record.version_group ? '<span class="badge">关联版本</span>' : '') + tags.join('');
}

async function loadResearch() {
  try {
    const data = await api('/api/research');
    state.research = data.items;
    state.researchRevision = data.revision;
    state.researchError = '';
  } catch (e) {
    state.researchError = e.message;
    toast('阅读记录读取失败，暂不可编辑：' + e.message, 'error');
  }
  const filter = $('#filter-reading');
  filter.disabled = !!state.researchError;
  renderTagFilters();
}

async function saveResearch(url, payload, revision = state.researchRevision) {
  if (researchSaving) throw new Error('另一条研究记录正在保存，请稍后再试');
  if (state.researchError) throw new Error('请先刷新并成功读取研究记录');
  researchSaving = true;
  try {
    const result = await api(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...payload, revision }),
    });
    state.research = result.items;
    state.researchRevision = result.revision;
    renderList();
    return result;
  } catch (e) {
    if (e.status === 409) {
      await loadResearch();
      throw Object.assign(new Error(e.message + '；已刷新记录，当前输入保留，请核对后再提交。'), { status: 409 });
    }
    throw e;
  } finally {
    researchSaving = false;
  }
}

function researchConflict(error) {
  return !!error && error.status === 409 && /研究记录已改变|缺少 revision/.test(String(error.message || ''));
}

// 论文编辑与主题重命名在改动目录名时会迁移研究记录，服务端在写入任何文件之前就用 revision 校验拦下，
// 刷新记录后重试一次不会产生重复写入，可避免用户整页刷新、重填表单。
async function retryAfterResearchRefresh(run) {
  try {
    return await run();
  } catch (error) {
    if (!researchConflict(error)) throw error;
    await loadResearch();
    if (state.researchError) throw error;
    return await run();
  }
}

function researchOptions(labels, selected) {
  return Object.entries(labels).map(([value, label]) =>
    `<option value="${esc(value)}" ${value === selected ? 'selected' : ''}>${esc(label)}</option>`).join('');
}

function researchHtml(it) {
  if (state.researchError) return `<p class="error pad">研究记录暂不可用：${esc(state.researchError)}，请刷新重试。</p>`;
  const record = researchRecord(it.key);
  return `<div id="research-panel" class="research-panel">
    <details open class="research-section">
      <summary>阅读记录</summary>
      <form id="reading-form" class="research-form">
        <label>阅读状态<select name="status">${researchOptions(READING_LABELS, record.status || '')}</select></label>
        <label>一句话结论<textarea name="note" rows="2" maxlength="1000" placeholder="这篇论文对我的研究有什么用？">${esc(record.note || '')}</textarea></label>
        <div class="actions-row"><button class="mini primary" type="submit">保存阅读记录</button><span class="muted" role="status" id="reading-result"></span></div>
      </form>
    </details>
    <details open class="research-section">
      <summary>标签 · 跨主题横切，可多值</summary>
      <div class="research-form tag-editor-wrap">
        <div class="tag-editor" id="tag-editor"></div>
        <form id="tag-form" class="tag-add">
          <input id="tag-input" list="tag-options" maxlength="40" autocomplete="off"
                 placeholder="新增标签，回车保存" aria-label="新增标签">
          <button class="mini primary" type="submit">添加</button>
        </form>
        <datalist id="tag-options"></datalist>
        <span class="muted" role="status" id="tag-result"></span>
      </div>
    </details>
    <details id="citation-section" class="research-section">
      <summary>引用 · BibTeX / LaTeX</summary>
      <div id="citation-content" class="research-form"></div>
    </details>
    <details id="versions-section" class="research-section">
      <summary>版本关联 · 各版本独立保留</summary>
      <div id="versions-content" class="research-form"></div>
    </details>
  </div>`;
}

function bindResearch(it) {
  const panel = $('#research-panel');
  if (!panel) return;
  const form = panel.querySelector('#reading-form');
  let revision = state.researchRevision;
  form.onsubmit = async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    const result = panel.querySelector('#reading-result');
    const payload = { key: it.key, status: form.elements.status.value, note: form.elements.note.value };
    button.disabled = true;
    form.elements.status.disabled = true;
    form.elements.note.readOnly = true;
    result.textContent = '保存中…';
    try {
      await saveResearch('/api/reading', payload, revision);
      revision = state.researchRevision;
      result.textContent = form.elements.note.value === payload.note && form.elements.status.value === payload.status
        ? '已保存' : '上次提交已保存，当前仍有未保存修改';
    } catch (e) {
      result.textContent = e.message;
      if (e.status === 409) {
        revision = state.researchRevision;
        const latest = researchRecord(it.key);
        result.textContent += ` 最新记录：${READING_LABELS[latest.status || '']}；${latest.note || '无结论'}。`;
      }
    }
    finally {
      button.disabled = false;
      form.elements.status.disabled = false;
      form.elements.note.readOnly = false;
    }
  };
  form.oninput = () => { panel.querySelector('#reading-result').textContent = '尚未保存'; };
  bindTagEditor(panel, it);
  const section = panel.querySelector('#citation-section');
  let loaded = false;
  section.ontoggle = () => {
    if (section.open && !loaded) {
      loaded = true;
      loadCitation(it, panel.querySelector('#citation-content'));
    }
  };
  renderVersions(it, panel.querySelector('#versions-content'));
  const versions = panel.querySelector('#versions-section');
  versions.ontoggle = () => {
    if (versions.open) renderVersions(it, panel.querySelector('#versions-content'));
  };
}

/* ---------- 论文笔记（note.md） ---------- */

function noteQueryParams(it) {
  return '?topic=' + encodeURIComponent(it.topic) +
    '&year=' + encodeURIComponent(it.year) + '&slug=' + encodeURIComponent(it.slug);
}

function noteQuery(it) {
  return '/api/paper/note' + noteQueryParams(it);
}

function paperNoteHtml(it) {
  return `<div class="research-panel paper-note" id="paper-note-panel" data-key="${esc(it.key)}">
    <details open class="research-section">
      <summary>论文笔记 · Markdown<span class="note-path">${esc(it.slug)}/note.md</span></summary>
      <div class="research-form">
        <div id="paper-note-body" class="paper-note-body"><p class="muted">正在读取笔记…</p></div>
        <div class="actions-row">
          <button class="mini primary" id="panel-note-edit" type="button" disabled>写笔记</button>
          <button class="mini" id="panel-note-open" type="button" disabled title="用系统默认编辑器打开本机镜像里的 note.md">用本地软件打开</button>
          <button class="mini" id="panel-note-delete" type="button" disabled>删除笔记</button>
          <span class="muted" role="status" id="paper-note-status"></span>
        </div>
      </div>
    </details>
  </div>`;
}

function bindPaperNote(it) {
  const panel = $('#paper-note-panel');
  if (!panel) return;
  const body = panel.querySelector('#paper-note-body');
  const status = panel.querySelector('#paper-note-status');
  const editBtn = panel.querySelector('#panel-note-edit');
  const openBtn = panel.querySelector('#panel-note-open');
  const delBtn = panel.querySelector('#panel-note-delete');
  const note = { text: '', revision: 'missing', hasNote: false, source: 'remote',
    byteSize: 0, recovery: '', remoteError: '' };

  const paint = () => {
    const editable = !state.remoteError && !note.remoteError;
    editBtn.disabled = !editable;
    openBtn.disabled = !note.hasNote;
    delBtn.disabled = !editable || !note.hasNote;
    editBtn.textContent = note.hasNote ? '编辑笔记' : '写笔记';
    editBtn.title = editable ? '' : '远端离线，暂时只能查看本机缓存副本';
    let main;
    if (note.hasNote) {
      main = `<div class="markdown-body paper-note-md">${PapertexMarkdown.render(note.text, { emptyText: '' })}</div>`;
    } else if (note.recovery) {
      main = `<p class="muted">远端还没有 note.md，但本机留有旧副本。</p>
        <button class="mini" id="note-restore" type="button">用本机副本恢复</button>`;
    } else {
      main = '<p class="muted">还没有笔记。点「写笔记」记录结论、方法要点或复现坑点，保存为远端论文目录下的 note.md。</p>';
    }
    const warn = note.remoteError
      ? `<p class="warn-text">${esc(note.remoteError)}，当前显示的是本机缓存副本。</p>` : '';
    const meta = note.hasNote
      ? `<div class="muted note-meta">${note.source === 'local' ? '本机缓存' : '远端'} · ${byteText(note.byteSize)}</div>` : '';
    body.innerHTML = main + warn + meta;
    const restore = body.querySelector('#note-restore');
    if (restore) restore.onclick = () => openNoteEditor(it, note, { text: note.recovery, revision: 'missing' });
  };

  note.afterSave = () => {
    paint();
    const entry = state.items.find(x => x.key === it.key);
    if (entry) {
      entry.local.has_note = note.hasNote;
      if (entry.remote) entry.remote.has_note = note.hasNote;
      renderList();
    }
    status.textContent = '已保存 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
  };

  editBtn.onclick = () => openNoteEditor(it, note);
  openBtn.onclick = async () => {
    openBtn.disabled = true;
    try {
      await api('/api/paper/note/open' + noteQueryParams(it), { method: 'POST' });
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      openBtn.disabled = !note.hasNote;
    }
  };
  delBtn.onclick = async () => {
    const ok = await confirmDialog({
      title: `删除 ${it.slug} 的笔记`,
      text: '远端论文目录下的 note.md 会被删除，本机镜像同步删除；论文本身不受影响。',
      confirmLabel: '删除笔记',
    });
    if (!ok) return;
    delBtn.disabled = true;
    status.textContent = '删除中…';
    try {
      const data = await api('/api/paper/note', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic: it.topic, year: it.year, slug: it.slug, text: '', revision: note.revision }),
      });
      Object.assign(note, { text: '', revision: data.revision || 'missing', hasNote: false,
        byteSize: 0, recovery: '', remoteError: '', source: 'remote' });
      note.afterSave();
      status.textContent = '已删除 note.md';
    } catch (e) {
      status.textContent = e.message;
      paint();
    }
  };

  const load = async () => {
    body.innerHTML = '<p class="muted">正在读取笔记…</p>';
    try {
      const data = await api(noteQuery(it), { cache: 'no-store' });
      if (!panel.isConnected) return;
      Object.assign(note, {
        text: data.text || '',
        revision: data.revision || 'missing',
        hasNote: !!data.has_note,
        source: data.source || 'remote',
        byteSize: data.byte_size || 0,
        recovery: data.recovery_text || '',
        remoteError: data.remote_error ? `远端读取失败（${data.remote_error}）` : '',
      });
      paint();
    } catch (e) {
      if (!panel.isConnected) return;
      body.innerHTML = `<p class="warn-text">笔记读取失败：${esc(e.message)}</p>`;
      editBtn.disabled = true;
      delBtn.disabled = true;
    }
  };
  load();
}

let activeNoteEditor = null;

function notePreviewHtml(text) {
  if (!text.trim()) return '<p class="muted">左侧输入 Markdown，这里实时预览；支持 $…$ 行内公式与 $$…$$ 块级公式。</p>';
  return PapertexMarkdown.render(text, { emptyText: '' });
}

function closeNoteEditor() {
  const session = activeNoteEditor;
  if (!session) return;
  activeNoteEditor = null;
  clearTimeout(session.timer);
  const { modal, textarea } = session;
  if (modal.open) modal.close();
  modal.classList.add('hidden');
  document.body.classList.remove('preview-open');
  textarea.oninput = null;
  textarea.onkeydown = null;
  modal.onclick = null;
  modal.oncancel = null;
  if (session.opener && session.opener.isConnected) session.opener.focus();
}

function openNoteEditor(it, note, overrides = {}) {
  closeNoteEditor();
  const modal = $('#note-modal');
  const textarea = $('#note-text');
  const preview = $('#note-preview');
  const status = $('#note-status');
  const save = $('#note-save');
  const del = $('#note-delete');
  const timer = { id: null };
  const session = {
    modal, textarea, timer,
    revision: overrides.revision || note.revision,
    opener: document.activeElement,
  };
  activeNoteEditor = session;
  $('#note-modal-title').textContent = `论文笔记 · ${it.slug}`;
  $('#note-modal-meta').textContent = `${it.topic}/${it.year}/${it.slug}/note.md`;
  textarea.value = typeof overrides.text === 'string' ? overrides.text : note.text;
  textarea.maxLength = 200000;
  status.textContent = '';
  save.disabled = false;
  del.disabled = false;
  del.classList.toggle('hidden', !note.hasNote);
  $('#note-form').onsubmit = event => {
    event.preventDefault();
    submitNote();
  };
  const refresh = () => { preview.innerHTML = notePreviewHtml(textarea.value); };

  async function saveNote() {
    const text = textarea.value;
    save.disabled = true;
    del.disabled = true;
    textarea.readOnly = true;
    status.textContent = '保存中…';
    try {
      const data = await api('/api/paper/note', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic: it.topic, year: it.year, slug: it.slug, text, revision: session.revision }),
      });
      Object.assign(note, {
        text: data.text || '',
        revision: data.revision || 'missing',
        hasNote: !!data.has_note,
        source: 'remote',
        byteSize: data.byte_size || 0,
        recovery: '',
        remoteError: '',
      });
      session.revision = note.revision;
      closeNoteEditor();
      note.afterSave?.();
    } catch (e) {
      if (e.status === 409) {
        status.textContent = e.message + ' 正在获取最新版本号…';
        try {
          const latest = await api(noteQuery(it), { cache: 'no-store' });
          session.revision = latest.revision || 'missing';
          status.textContent = '远端已有新版本，已取到最新版本号；确认内容后再点保存会覆盖它。';
        } catch (err) {
          status.textContent = '无法获取最新版本：' + err.message;
        }
      } else {
        status.textContent = e.message;
      }
      save.disabled = false;
      del.disabled = false;
      textarea.readOnly = false;
    }
  }

  async function submitNote() {
    if (!textarea.value.trim() && (note.hasNote || session.revision !== 'missing')) {
      const ok = await confirmDialog({
        title: '清空笔记',
        text: '内容为空，保存会删除远端 note.md（并同步删除本机镜像）。',
        confirmLabel: '清空并删除',
      });
      if (!ok) return;
    }
    await saveNote();
  }

  del.onclick = async () => {
    const ok = await confirmDialog({
      title: `删除 ${it.slug} 的笔记`,
      text: '远端论文目录下的 note.md 会被删除，本机镜像同步删除；论文本身不受影响。',
      confirmLabel: '删除笔记',
    });
    if (!ok) return;
    textarea.value = '';
    await saveNote();
  };
  $('#note-cancel').onclick = closeNoteEditor;
  $('#note-modal-close').onclick = closeNoteEditor;
  modal.onclick = event => { if (event.target === modal) closeNoteEditor(); };
  modal.oncancel = event => { event.preventDefault(); closeNoteEditor(); };
  textarea.oninput = () => {
    status.textContent = '尚未保存';
    clearTimeout(timer.id);
    timer.id = setTimeout(refresh, 180);
  };
  textarea.onkeydown = event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
      event.preventDefault();
      if (!save.disabled) submitNote();
    }
  };
  refresh();
  modal.classList.remove('hidden');
  document.body.classList.add('preview-open');
  modal.showModal();
  textarea.focus();
  textarea.setSelectionRange(0, 0);
}

async function copyCitation(text, output) {
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制', 'ok');
  } catch {
    output.value = text;
    output.focus();
    output.select();
    toast('无法访问剪贴板，已选中文字，请按 ⌘C / Ctrl+C 复制', 'warn');
  }
}

async function loadCitation(it, host) {
  const revision = state.researchRevision;
  renderViewState(host, 'loading', '正在读取书目信息');
  try {
    const data = await api('/api/citation?key=' + encodeURIComponent(it.key));
    if (!host.isConnected) return;
    const fields = data.fields;
    const inputs = [
      ['title', '标题', 2000], ['authors', '完整作者（用 and 分隔）', 4000],
      ['year', '出版年份', 4], ['venue', '会议或期刊全称', 500],
      ['doi', 'DOI', 200], ['arxiv', 'arXiv 编号', 100],
    ];
    host.innerHTML = `<p class="muted">书目信息单独保存在本机；缺项不会猜测，请核对后用于正式论文。引用键使用论文 slug，改名后随之更新；同名时自动附加主题或年份。</p>
      <form id="citation-form" class="research-form">
        <label>文献类型<select name="entry_type">${researchOptions({ misc: '其他 / 预印本', article: '期刊论文', inproceedings: '会议论文' }, fields.entry_type)}</select></label>
        ${inputs.map(([key, label, max]) => `<label>${label}<input name="${key}" maxlength="${max}" value="${esc(fields[key] || '')}"></label>`).join('')}
        <div class="actions-row"><button type="submit" class="mini">保存书目信息</button><button type="button" class="mini ghost" id="citation-reload">重新读取</button><span class="muted" role="status" id="citation-result"></span></div>
      </form>
      <p class="${data.warnings.length ? 'warn-text' : 'muted'}" id="citation-warnings">${esc(data.warnings.join('；') || '基本字段齐全，请核对出版信息。')}</p>
      <label>LaTeX 引用<input id="citation-cite" readonly value="${esc(data.cite)}"></label>
      <div class="actions-row"><button type="button" class="mini" id="copy-cite">复制引用命令</button><button type="button" class="mini" id="copy-bib">复制 BibTeX</button></div>
      <label>BibTeX<textarea id="citation-bib" rows="7" readonly spellcheck="false">${esc(data.bibtex)}</textarea></label>`;
    host.querySelector('#citation-reload').onclick = async () => {
      if (!await confirmDialog({ title: '重新读取书目', text: '将丢弃本表单尚未保存的输入，读取最新书目信息。', confirmLabel: '重新读取' })) return;
      await loadResearch();
      if (host.isConnected) loadCitation(it, host);
    };
    host.querySelector('#copy-cite').onclick = () => copyCitation(data.cite, host.querySelector('#citation-cite'));
    host.querySelector('#copy-bib').onclick = () => copyCitation(data.bibtex, host.querySelector('#citation-bib'));
    const form = host.querySelector('#citation-form');
    const copyButtons = [host.querySelector('#copy-cite'), host.querySelector('#copy-bib')];
    form.oninput = () => {
      copyButtons.forEach(button => { button.disabled = true; });
      host.querySelector('#citation-result').textContent = '有未保存修改，请保存后复制';
    };
    form.onsubmit = async event => {
      event.preventDefault();
      const button = form.querySelector('button');
      const result = host.querySelector('#citation-result');
      button.disabled = true;
      result.textContent = '保存中…';
      const values = Object.fromEntries(new FormData(form));
      const controls = Array.from(form.elements);
      controls.forEach(control => { control.disabled = true; });
      try {
        await saveResearch('/api/citation', { key: it.key, fields: values }, revision);
        if (host.isConnected) await loadCitation(it, host);
      } catch (e) { result.textContent = e.message + (e.status === 409 ? ' 请先点「重新读取」核对最新书目。' : ''); }
      finally {
        controls.forEach(control => { control.disabled = false; });
        button.disabled = false;
      }
    };
  } catch (e) {
    if (host.isConnected) renderViewState(host, 'error', '书目读取失败', e.message, () => loadCitation(it, host));
  }
}

function versionMembers(key) {
  const group = researchRecord(key).version_group;
  if (!group) return [];
  return Object.entries(state.research).filter(([, record]) => record.version_group === group)
    .map(([memberKey, record]) => ({ key: memberKey, record, item: state.items.find(item => item.key === memberKey) }));
}

function renderVersions(it, host) {
  const revision = state.researchRevision;
  const record = researchRecord(it.key);
  const members = versionMembers(it.key);
  host.innerHTML = `<p class="muted">只建立关系，不移动、不覆盖、不合并文件。arXiv、会议版、期刊版可独立保存。</p>
    <div class="version-members">${members.length ? members.map(member => `<div class="version-row">
      <div><strong>${esc(VERSION_LABELS[member.record.version_label] || '未标注')}</strong> · ${esc(member.key)}
      ${member.key === it.key ? '（当前）' : ''}${!member.item ? `<span class="warn-text"> · ${state.remoteError ? '未核实' : '库中缺失'}</span>` : ''}</div>
      <div class="actions-row">${member.item && member.key !== it.key ? `<button type="button" class="mini" data-version-open="${esc(member.key)}">查看</button>` : ''}
      <button type="button" class="mini ghost" data-version-unlink="${esc(member.key)}">解除关联</button></div>
    </div>`).join('') : '<p class="muted">尚未关联其他版本</p>'}</div>
    <form id="version-form" class="research-form">
      <label>当前论文版本<select name="label">${researchOptions(VERSION_LABELS, record.version_label || '')}</select></label>
      <label>搜索要关联的论文<input type="search" id="version-search" placeholder="slug / 标题 / 主题"></label>
      <label>关联论文<select name="other_key" id="version-target" required></select></label>
      <label>对方版本<select name="other_label" id="version-target-label">${researchOptions(VERSION_LABELS, '')}</select></label>
      <div class="actions-row"><button type="submit" class="mini">关联 / 更新标注</button><button type="button" class="mini ghost" id="versions-reload">刷新关系</button><span role="status" class="muted" id="version-result"></span></div>
    </form>`;
  host.querySelector('#versions-reload').onclick = async () => {
    await loadResearch();
    if (host.isConnected) renderVersions(it, host);
  };
  const form = host.querySelector('#version-form');
  const target = host.querySelector('#version-target');
  const otherLabel = host.querySelector('#version-target-label');
  const updateLabel = () => { otherLabel.value = researchRecord(target.value).version_label || ''; };
  const populate = () => {
    const q = host.querySelector('#version-search').value.trim().toLowerCase();
    const candidates = state.items.filter(item => item.key !== it.key &&
      (item.key + ' ' + itemTitle(item)).toLowerCase().includes(q));
    const previous = target.value;
    target.innerHTML = '<option value="">选择一个版本…</option>' + candidates.map(item =>
      `<option value="${esc(item.key)}">${esc(item.key)}${itemTitle(item) ? ' · ' + esc(displayTitle(itemTitle(item))) : ''}</option>`).join('');
    if (candidates.some(item => item.key === previous)) target.value = previous;
    updateLabel();
  };
  populate();
  host.querySelector('#version-search').oninput = populate;
  target.onchange = updateLabel;
  form.onsubmit = async event => {
    event.preventDefault();
    const result = host.querySelector('#version-result');
    const button = form.querySelector('button');
    button.disabled = true;
    result.textContent = '保存中…';
    try {
      await saveResearch('/api/versions/link', { key: it.key, other_key: target.value,
        label: form.elements.label.value, other_label: otherLabel.value }, revision);
      if (host.isConnected) renderVersions(it, host);
      toast('版本已关联，文件未改动', 'ok');
    } catch (e) { result.textContent = e.message + (e.status === 409 ? ' 请先点「刷新关系」。' : ''); }
    finally { button.disabled = false; }
  };
  host.querySelectorAll('[data-version-open]').forEach(button => {
    button.onclick = () => {
      const item = state.items.find(item => item.key === button.dataset.versionOpen);
      if (!item) return;
      state.current = item;
      state.previewSource = null;
      renderList();
      showDetail(item);
    };
  });
  host.querySelectorAll('[data-version-unlink]').forEach(button => {
    button.onclick = async () => {
      const key = button.dataset.versionUnlink;
      if (!await confirmDialog({ title: '解除版本关联', text: `解除 ${key} 的版本关系？论文文件和阅读记录均保留。`, confirmLabel: '解除关联' })) return;
      button.disabled = true;
      try {
        await saveResearch('/api/versions/unlink', { key }, revision);
        if (host.isConnected) renderVersions(it, host);
        toast('已解除关联，文件未改动', 'ok');
      } catch (e) { toast(e.message, 'error'); }
      finally { button.disabled = false; }
    };
  });
}

async function exportProjectBibliography() {
  if (!state.project) { toast('先选择一个项目', 'warn'); return; }
  const project = state.project;
  const button = $('#refs-export-bib');
  button.disabled = true;
  try {
    const data = await api('/api/project/bibliography?project=' + encodeURIComponent(project) +
      '&source=' + encodeURIComponent(state.projectSource));
    if (data.warnings.length) {
      const ok = await confirmDialog({ title: '导出前核对',
        text: `${project}：可导出 ${data.count} 条。\n${data.warnings.join('\n')}`,
        confirmLabel: '仍然导出' });
      if (!ok) return;
    }
    if (!data.count) { toast('没有可导出的参考文献', 'warn'); return; }
    const url = URL.createObjectURL(new Blob([data.bibtex], { type: 'application/x-bibtex;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'references.bib';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`已导出 ${project} 的 ${data.count} 条文献，项目文件未改动`, 'ok');
  } catch (e) { toast(e.message, 'error'); }
  finally { button.disabled = false; }
}

/* ---------- 列表 / 详情分隔条 ---------- */

const SPLIT_KEY = 'papertex.listWidth';
const SPLIT_DEFAULT = 340;
const SPLIT_MIN = 240;
const SPLIT_LIMIT = 1200;
const SPLIT_DETAIL_MIN = 280;
const SPLIT_GUTTER = 12;
const splitState = { width: SPLIT_DEFAULT };

function splitterMax() {
  // 隐藏的标签页 clientWidth 为 0，只能量当前可见的那个工作区
  const layout = $$('.layout').find(el => el.clientWidth > 0) || $('#library-layout');
  const width = layout && layout.clientWidth > 0 ? layout.clientWidth : window.innerWidth - 64;
  return Math.max(SPLIT_MIN,
    Math.min(SPLIT_LIMIT, Math.round(width - SPLIT_GUTTER - SPLIT_DETAIL_MIN)));
}

function applyListWidth(px, save = false) {
  splitState.width = Math.min(splitterMax(), Math.max(SPLIT_MIN, Math.round(px)));
  document.documentElement.style.setProperty('--list-width', splitState.width + 'px');
  $$('.splitter').forEach(bar => {
    bar.setAttribute('aria-valuenow', splitState.width);
    bar.setAttribute('aria-valuemin', SPLIT_MIN);
    bar.setAttribute('aria-valuemax', splitterMax());
  });
  if (save) {
    try { localStorage.setItem(SPLIT_KEY, String(splitState.width)); } catch {}
  }
}

function initSplitter() {
  const bars = $$('.splitter');
  if (!bars.length) return;
  let saved = SPLIT_DEFAULT;
  try { saved = Number(localStorage.getItem(SPLIT_KEY)) || SPLIT_DEFAULT; } catch {}
  applyListWidth(saved);

  bars.forEach(bar => {
    const layout = bar.parentElement;
    let dragLeft = 0;
    const onMove = event => applyListWidth(event.clientX - dragLeft);
    const stop = () => {
      bar.classList.remove('dragging');
      document.body.classList.remove('col-resizing');
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', stop);
      window.removeEventListener('pointercancel', stop);
      applyListWidth(splitState.width, true);
    };
    bar.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.preventDefault();
      dragLeft = layout.getBoundingClientRect().left;
      bar.classList.add('dragging');
      document.body.classList.add('col-resizing');
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', stop);
      window.addEventListener('pointercancel', stop);
      bar.focus();
    });
    bar.addEventListener('dblclick', () => applyListWidth(SPLIT_DEFAULT, true));
    bar.addEventListener('keydown', event => {
      const step = event.shiftKey ? 64 : 16;
      if (event.key === 'ArrowLeft') applyListWidth(splitState.width - step, true);
      else if (event.key === 'ArrowRight') applyListWidth(splitState.width + step, true);
      else if (event.key === 'Home') applyListWidth(SPLIT_MIN, true);
      else if (event.key === 'End') applyListWidth(splitterMax(), true);
      else return;
      event.preventDefault();
    });
  });
  window.addEventListener('resize', () => applyListWidth(splitState.width));
}

/* ---------- 审稿空间 ---------- */

const reviewState = {
  items: [], root: '', templates: [], statuses: [], loaded: false,
  key: '', task: null, files: [], revision: '', detailLoaded: false,
  timer: 0, pending: null, conflict: false, savedAt: '', dirtyBody: false,
  trash: [], trashLoaded: false,
};

function reviewManuscriptUrl(key, name) {
  return '/api/review/pdf?key=' + encodeURIComponent(key) +
    '&name=' + encodeURIComponent(name) +
    '&token=' + encodeURIComponent(state.pdfToken || '');
}

function reviewDeadlineText(item) {
  if (!item.due) return '未设截止日期';
  if (typeof item.days_left !== 'number') return '截止 ' + item.due;
  if (item.overdue) return `已逾期 ${Math.abs(item.days_left)} 天`;
  if (item.days_left === 0) return `今天 ${item.due} 截止`;
  return `剩 ${item.days_left} 天 · ${item.due}`;
}

function reviewRowHtml(item) {
  const badges = [
    `<span class="badge pdf">${esc(item.status_label)}</span>`,
    item.has_manuscript ? '' : '<span class="badge warn">无稿件</span>',
  ].join('');
  const sub = [item.title || '未填稿件标题', reviewDeadlineText(item)].join(' · ');
  return `<button type="button" class="review-row${item.key === reviewState.key ? ' active' : ''}"
      data-review-key="${esc(item.key)}">
    <span class="slug">${esc(item.venue)} / ${esc(item.submission)}</span>
    <span class="muted">${esc(sub)}</span>
    <span class="badges">${badges}</span>
  </button>`;
}

function renderReviewList() {
  const list = $('#review-list');
  if (!reviewState.loaded) {
    renderViewState(list, 'loading', '正在读取审稿任务');
    return;
  }
  if (!reviewState.items.length) {
    renderViewState(list, 'empty', '还没有审稿任务',
      '点「新建任务」按「会议 / 投稿号」建一个目录，稿件与评审文本都只写在本机。');
    return;
  }
  list.innerHTML = reviewState.items.map(reviewRowHtml).join('');
  list.querySelectorAll('[data-review-key]').forEach(btn => {
    btn.onclick = () => selectReview(btn.dataset.reviewKey);
  });
}

function reviewFieldHtml(id, label, value, kind) {
  if (kind === 'status') {
    const options = reviewState.statuses.map(s =>
      `<option value="${esc(s.value)}"${s.value === value ? ' selected' : ''}>${esc(s.label)}</option>`).join('');
    return `<label>${esc(label)}<select id="${id}">${options}</select></label>`;
  }
  return `<label>${esc(label)}<input id="${id}" value="${esc(value)}"${kind === 'date'
    ? ' type="date"' : ''} autocomplete="off"></label>`;
}

function reviewFileRowHtml(file) {
  const isPdf = /\.pdf$/i.test(file.name);
  return `<div class="review-file">
    <span class="badge ${isPdf ? 'tex' : 'code'}">${isPdf ? 'PDF' : '文件'}</span>
    <span class="slug">${esc(file.name)}</span>
    <span class="muted">${byteText(file.size)}</span>
    ${isPdf ? `<button type="button" class="mini ghost" data-review-open="${esc(file.name)}">打开</button>` : ''}
  </div>`;
}

function reviewDetailHtml(data) {
  const task = data.task;
  const files = data.files || [];
  const fileRows = files.length
    ? files.map(reviewFileRowHtml).join('')
    : '<p class="muted">还没有稿件文件。稿件只写在本机任务目录，不进论文库。</p>';
  return `<div class="pad review-detail">
    <div class="review-task-head">
      <span class="slug">${esc(task.venue)} / ${esc(task.submission)}</span>
      <span class="muted">${esc(reviewDeadlineText(task))}${task.purged_at ? ' · 稿件已清除' : ''}</span>
    </div>
    <div class="review-meta form">
      ${reviewFieldHtml('rvf-title', '稿件标题', task.title, 'text')}
      ${reviewFieldHtml('rvf-venue_label', '会议全称', task.venue_label, 'text')}
      <div class="row">
        ${reviewFieldHtml('rvf-round', '轮次', task.round, 'text')}
        ${reviewFieldHtml('rvf-due', '截止日期', task.due, 'date')}
        ${reviewFieldHtml('rvf-status', '状态', task.status, 'status')}
      </div>
      <div class="row">
        ${reviewFieldHtml('rvf-rating', '评分', task.rating, 'text')}
        ${reviewFieldHtml('rvf-confidence', '置信度', task.confidence, 'text')}
      </div>
      <label>备注<textarea id="rvf-note" rows="2">${esc(task.note)}</textarea></label>
    </div>
    <div class="toolbar inner review-actions">
      <button type="button" id="rv-upload" class="mini">${files.length ? '替换稿件' : '上传稿件'}</button>
      <button type="button" id="rv-open" class="mini ghost" ${files.length ? '' : 'disabled'}>打开稿件</button>
      <button type="button" id="rv-purge" class="mini ghost" ${files.length ? '' : 'disabled'}>清除稿件</button>
      <button type="button" id="rv-export" class="mini ghost">导出可粘贴文本</button>
      <button type="button" id="rv-reload" class="mini ghost">重新载入</button>
      <button type="button" id="rv-delete" class="mini ghost danger">删除任务</button>
      <span id="rv-save" class="muted"></span>
    </div>
    <div class="review-files">${fileRows}</div>
    <div class="review-editor-head">
      <span class="muted">评审文本 · 本机 review.md</span>
      <div class="seg" id="rv-view-seg" aria-label="评审文本视图">
        <button type="button" data-view="edit" class="active" aria-pressed="true">编辑</button>
        <button type="button" data-view="preview" aria-pressed="false">预览</button>
      </div>
    </div>
    <textarea id="rv-editor" class="review-editor" spellcheck="false"
      aria-label="评审文本，保存在本机 review.md">${esc(data.review)}</textarea>
    <div id="rv-preview" class="markdown-body review-preview hidden" tabindex="0"></div>
  </div>`;
}

function reviewFieldsPayload() {
  const read = id => {
    const el = document.getElementById(id);
    return el ? el.value.trim() : '';
  };
  return {
    title: read('rvf-title'), venue_label: read('rvf-venue_label'),
    round: read('rvf-round'), due: read('rvf-due'), status: read('rvf-status'),
    rating: read('rvf-rating'), confidence: read('rvf-confidence'), note: read('rvf-note'),
  };
}

function reviewSaveText() {
  reviewState.savedAt = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  $('#rv-save').textContent = `已保存 ${reviewState.savedAt}`;
}

async function saveReviewBody() {
  const editor = $('#rv-editor');
  const target = $('#rv-save');
  if (!editor || !target || reviewState.conflict) return;
  const body = editor.value;
  reviewState.dirtyBody = false;
  if (body === reviewState.review) return;
  target.textContent = '正在保存…';
  const request = api('/api/review/task', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key: reviewState.key, review: body, expected: reviewState.revision }),
  }).then(result => {
    reviewState.revision = result.revision;
    reviewState.review = body;
    reviewState.conflict = false;
    if (document.activeElement !== editor) reviewSaveText();
    else setTimeout(reviewSaveText, 0);
    return result;
  }).catch(error => {
    if (error.status === 409) {
      reviewState.conflict = true;
      target.textContent = '保存冲突';
      toast('服务端评审文本已变化，自动保存已暂停。请先复制本地内容，再点「重新载入」。', 'warn');
    } else {
      reviewState.dirtyBody = true;
      target.textContent = '保存失败';
      toast('评审文本保存失败：' + error.message, 'error');
    }
    return null;
  });
  reviewState.pending = request;
  return request;
}

async function saveReviewFields() {
  const target = $('#rv-save');
  if (!target || reviewState.conflict) return;
  clearTimeout(reviewState.timer);
  if (reviewState.dirtyBody) await saveReviewBody();
  else if (reviewState.pending) await reviewState.pending;
  target.textContent = '正在保存…';
  try {
    const result = await api('/api/review/task', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: reviewState.key, fields: reviewFieldsPayload() }),
    });
    reviewState.task = result.task;
    reviewState.revision = result.revision;
    reviewSaveText();
    await loadReviewList();
  } catch (e) {
    target.textContent = '保存失败';
    toast('审稿信息保存失败：' + e.message, 'error');
  }
}

function queueReviewSave() {
  clearTimeout(reviewState.timer);
  reviewState.dirtyBody = true;
  const target = $('#rv-save');
  if (target) target.textContent = '有改动待保存…';
  reviewState.timer = setTimeout(() => { saveReviewBody(); }, 1200);
}

function bindReviewDetail() {
  const editor = $('#rv-editor');
  if (editor) {
    editor.addEventListener('input', queueReviewSave);
    editor.addEventListener('blur', () => { clearTimeout(reviewState.timer); saveReviewBody(); });
  }
  $$('#rv-view-seg button').forEach(btn => {
    btn.onclick = () => setReviewView(btn.dataset.view);
  });
  ['rvf-title', 'rvf-venue_label', 'rvf-round', 'rvf-due', 'rvf-status',
   'rvf-rating', 'rvf-confidence', 'rvf-note'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', () => { clearTimeout(reviewState.timer); saveReviewFields(); });
  });
  $('#rv-upload').onclick = () => $('#review-pdf-input').click();
  $('#rv-open').onclick = () => openReviewPdf(reviewState.files[0]?.name);
  $('#rv-reload').onclick = () => selectReview(reviewState.key, true);
  $('#rv-export').onclick = () => openReviewExport();
  $('#rv-purge').onclick = purgeReviewManuscript;
  $('#rv-delete').onclick = deleteReviewTask;
  $$('#review-detail [data-review-open]').forEach(btn => {
    btn.onclick = () => openReviewPdf(btn.dataset.reviewOpen);
  });
}

async function setReviewView(view) {
  const editor = $('#rv-editor');
  const preview = $('#rv-preview');
  if (!editor || !preview) return;
  const target = view === 'preview' ? 'preview' : 'edit';
  if (target === 'preview' && reviewState.dirtyBody) await saveReviewBody();
  if (target === 'preview') {
    preview.innerHTML = PapertexMarkdown.render(editor.value, {
      emptyText: '评审文本为空。切换回编辑模式开始撰写。',
    });
  }
  editor.classList.toggle('hidden', target === 'preview');
  preview.classList.toggle('hidden', target !== 'preview');
  $$('#rv-view-seg button').forEach(btn => {
    const active = btn.dataset.view === target;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  if (target === 'preview') preview.focus();
  else editor.focus();
}

async function openReviewPdf(name) {  const target = name || (reviewState.files[0] || {}).name;
  if (!target) return;
  await openPdfTab(() => reviewManuscriptUrl(reviewState.key, target), reviewState.key);
}

async function selectReview(key, force) {
  if (!key) return;
  clearTimeout(reviewState.timer);
  if (reviewState.pending) await reviewState.pending;
  if (reviewState.dirtyBody) await saveReviewBody();
  if (!force && key === reviewState.key && reviewState.detailLoaded) return;
  reviewState.key = key;
  renderReviewList();
  $('#review-current').textContent = key;
  const host = $('#review-detail');
  renderViewState(host, 'loading', '正在读取任务');
  try {
    const data = await api('/api/review/task?key=' + encodeURIComponent(key));
    reviewState.task = data.task;
    reviewState.review = data.review;
    reviewState.revision = data.revision;
    reviewState.files = data.files || [];
    reviewState.detailLoaded = true;
    reviewState.conflict = false;
    reviewState.dirtyBody = false;
    host.innerHTML = reviewDetailHtml(data);
    bindReviewDetail();
    reviewSaveText();
  } catch (e) {
    renderViewState(host, 'error', '读取失败', e.message, () => selectReview(key, true));
  }
}

function reviewExportText(task, body) {
  const head = [`# ${task.venue} / ${task.submission}`];
  if (task.title) head.push('稿件：' + task.title);
  if (task.round) head.push('轮次：' + task.round);
  if (task.due) head.push('截止：' + task.due);
  const scores = [];
  if (task.rating) scores.push('评分：' + task.rating);
  if (task.confidence) scores.push('置信度：' + task.confidence);
  if (scores.length) head.push(scores.join(' / '));
  return [head.join('\n'), (body || '').trim()].filter(Boolean).join('\n\n') + '\n';
}

function openReviewExport() {
  const editor = $('#rv-editor');
  const text = reviewExportText(reviewState.task || {}, editor ? editor.value : reviewState.review);
  $('#review-export-text').value = text;
  $('#review-export-status').textContent = '';
  const modal = $('#review-export-modal');
  modal.classList.remove('hidden');
  document.body.classList.add('preview-open');
  modal.showModal();
  $('#review-export-text').select();
}

async function copyReviewExport() {
  const area = $('#review-export-text');
  const status = $('#review-export-status');
  try {
    await navigator.clipboard.writeText(area.value);
    status.textContent = '已复制到剪贴板';
  } catch {
    area.select();
    status.textContent = '内容已全选，请按 ⌘C 手动复制';
  }
}

function downloadReviewExport() {
  const name = String(reviewState.key || 'review').replace(/[^\w.-]+/g, '_') + '.md';
  const url = URL.createObjectURL(new Blob([$('#review-export-text').value],
    { type: 'text/markdown;charset=utf-8' }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = name;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  $('#review-export-status').textContent = '已下载 ' + name;
}

async function uploadReviewManuscript(event) {
  const file = event.target.files && event.target.files[0];
  event.target.value = '';
  if (!file || !reviewState.key) return;
  const target = $('#rv-save');
  if (target) target.textContent = '正在上传稿件…';
  const form = new FormData();
  form.append('key', reviewState.key);
  form.append('overwrite', reviewState.files.some(f => f.name === 'manuscript.pdf') ? 'true' : 'false');
  form.append('pdf', file);
  try {
    const result = await api('/api/review/manuscript', { method: 'POST', body: form });
    toast('稿件已写入本机：' + (result.file ? result.file.name : ''), 'ok');
    await selectReview(reviewState.key, true);
    await loadReviewList();
  } catch (e) {
    if (target) target.textContent = '上传失败';
    toast('稿件上传失败：' + e.message, 'error');
  }
}

async function purgeReviewManuscript() {
  const ok = await confirmDialog({
    title: '清除稿件',
    text: '会删除本任务的 manuscript.pdf 与 supplement/ 目录，评审文本与审稿信息保留。'
      + '部分会议要求审稿结束后销毁稿件。',
    confirmLabel: '清除稿件',
    expected: '清除稿件',
    inputLabel: '输入「清除稿件」以确认',
  });
  if (!ok) return;
  try {
    const result = await api('/api/review/purge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: reviewState.key, confirm: '清除稿件' }),
    });
    toast(result.notice || '稿件已清除', 'ok');
    await selectReview(reviewState.key, true);
    await loadReviewList();
  } catch (e) {
    toast('清除稿件失败：' + e.message, 'error');
  }
}

async function deleteReviewTask() {
  const ok = await confirmDialog({
    title: '删除审稿任务',
    text: `会把 ${reviewState.key} 整目录移入本机审稿回收区，可随时恢复；彻底删除需在回收区再确认一次。`,
    confirmLabel: '移入回收区',
    expected: '删除',
    inputLabel: '输入「删除」以确认',
  });
  if (!ok) return;
  try {
    const result = await api('/api/review?key=' + encodeURIComponent(reviewState.key) +
      '&confirm=' + encodeURIComponent('删除'), { method: 'DELETE' });
    toast(result.notice || '任务已移入回收区', 'ok');
    reviewState.key = '';
    reviewState.detailLoaded = false;
    $('#review-current').textContent = '';
    $('#review-detail').innerHTML = '<p class="muted pad">先在左侧新建或选择审稿任务</p>';
    await loadReviewList();
  } catch (e) {
    toast('删除失败：' + e.message, 'error');
  }
}

function reviewTrashRowHtml(item) {
  const files = (item.files || []).map(f => f.name).join('、');
  return `<div class="panel-row">
    <span class="badge tex">回收</span>
    <div class="main">
      <div class="slug">${esc(item.venue)} / ${esc(item.submission)}</div>
      <div class="muted">${esc(item.title || '未填稿件标题')}${files ? ' · ' + esc(files) : ''}</div>
    </div>
    <button type="button" class="mini" data-trash-restore="${esc(item.trash_id)}">恢复</button>
    <button type="button" class="mini danger" data-trash-purge="${esc(item.trash_id)}">彻底删除</button>
  </div>`;
}

async function loadReviewTrash() {
  const list = $('#review-trash-list');
  $('#review-trash-result').textContent = '正在读取…';
  try {
    const data = await api('/api/review/trash');
    reviewState.trash = data.items || [];
    reviewState.trashLoaded = true;
    $('#review-trash-result').textContent = `${reviewState.trash.length} 个任务`;
    list.innerHTML = reviewState.trash.length
      ? reviewState.trash.map(reviewTrashRowHtml).join('')
      : '<p class="muted pad">回收区是空的。</p>';
    list.querySelectorAll('[data-trash-restore]').forEach(btn => {
      btn.onclick = () => restoreReviewTask(btn.dataset.trashRestore);
    });
    list.querySelectorAll('[data-trash-purge]').forEach(btn => {
      btn.onclick = () => purgeReviewTask(btn.dataset.trashPurge);
    });
  } catch (e) {
    $('#review-trash-result').textContent = '读取失败';
    toast('回收区读取失败：' + e.message, 'error');
  }
}

async function restoreReviewTask(trashId) {
  try {
    const result = await api('/api/review/trash/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ trash_id: trashId }),
    });
    toast(result.notice || '任务已恢复', 'ok');
    await loadReviewTrash();
    await loadReviewList();
  } catch (e) {
    toast('恢复失败：' + e.message, 'error');
  }
}

async function purgeReviewTask(trashId) {
  const ok = await confirmDialog({
    title: '彻底删除审稿任务',
    text: '稿件与评审文本会从本机审稿目录永久删除，不可恢复。',
    confirmLabel: '彻底删除',
    expected: '清理',
    inputLabel: '输入「清理」以确认',
  });
  if (!ok) return;
  try {
    const result = await api('/api/review/trash?trash_id=' + encodeURIComponent(trashId) +
      '&confirm=' + encodeURIComponent('清理'), { method: 'DELETE' });
    toast(result.notice || '已彻底删除', 'ok');
    await loadReviewTrash();
    await loadReviewList();
  } catch (e) {
    toast('彻底删除失败：' + e.message, 'error');
  }
}

function openReviewTrash() {
  const modal = $('#review-trash-modal');
  modal.classList.remove('hidden');
  document.body.classList.add('preview-open');
  modal.showModal();
  loadReviewTrash();
}

function openReviewCreate() {
  $('#review-create-status').textContent = '';
  ['review-venue', 'review-submission', 'review-title', 'review-venue-label', 'review-round',
   'review-due'].forEach(id => { document.getElementById(id).value = ''; });
  $('#review-status-select').innerHTML = reviewState.statuses.map(s =>
    `<option value="${esc(s.value)}">${esc(s.label)}</option>`).join('');
  $('#review-template-select').innerHTML = reviewState.templates.map(t =>
    `<option value="${esc(t.name)}">${esc(t.name)}</option>`).join('');
  const modal = $('#review-create-modal');
  modal.classList.remove('hidden');
  document.body.classList.add('preview-open');
  modal.showModal();
  $('#review-venue').focus();
}

async function createReviewTask(event) {
  event.preventDefault();
  const button = event.target.querySelector('button[type="submit"]');
  button.disabled = true;
  $('#review-create-status').textContent = '正在创建…';
  try {
    const result = await api('/api/review', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        venue: $('#review-venue').value.trim(),
        submission: $('#review-submission').value.trim(),
        title: $('#review-title').value.trim(),
        venue_label: $('#review-venue-label').value.trim(),
        round: $('#review-round').value.trim(),
        due: $('#review-due').value,
        status: $('#review-status-select').value,
        template: $('#review-template-select').value,
      }),
    });
    const modal = $('#review-create-modal');
    modal.close();
    modal.classList.add('hidden');
    document.body.classList.remove('preview-open');
    toast(result.notice || '任务已创建', 'ok');
    await loadReviewList();
    await selectReview(result.task.key, true);
  } catch (e) {
    $('#review-create-status').textContent = '创建失败：' + e.message;
  } finally {
    button.disabled = false;
  }
}

async function loadReviewList() {
  try {
    const data = await api('/api/review');
    const keepKey = reviewState.key;
    reviewState.items = data.items || [];
    reviewState.root = data.root || '';
    reviewState.templates = data.templates || [];
    reviewState.statuses = data.statuses || [];
    reviewState.loaded = true;
    $('#review-root-text').textContent = `本机目录 ${reviewState.root} · ${reviewState.items.length} 个任务`;
    $('#review-trash-btn').textContent = data.trash_count ? `回收区 ${data.trash_count}` : '回收区';
    renderReviewList();
    if (keepKey && !reviewState.items.some(i => i.key === keepKey)) {
      reviewState.key = '';
      reviewState.detailLoaded = false;
      $('#review-current').textContent = '';
      $('#review-detail').innerHTML = '<p class="muted pad">任务已不在列表中，请重新选择。</p>';
    }
  } catch (e) {
    reviewState.loaded = true;
    renderViewState($('#review-list'), 'error', '审稿任务读取失败', e.message, loadReviewList);
  }
}

function initReview() {
  if (!reviewState.loaded) loadReviewList();
}

function closeReviewModal(modal) {
  if (modal.open) modal.close();
  modal.classList.add('hidden');
  document.body.classList.remove('preview-open');
}

/* ---------- 审稿空间结束 ---------- */

$('#settings-open').onclick = openSettings;
$('#setup-banner-open').onclick = openSettings;
$('#settings-modal-close').onclick = closeSettingsPanel;
$('#settings-connection').onchange = syncSettingsConnection;
$('#settings-test').onclick = testSettings;
$('#settings-form').onsubmit = saveSettings;
$('#settings-modal').addEventListener('cancel', () => {
  $('#settings-modal').classList.add('hidden');
  document.body.classList.remove('preview-open');
});

$('#review-new').onclick = openReviewCreate;
$('#review-trash-btn').onclick = openReviewTrash;
$('#review-refresh').onclick = async () => {
  await loadReviewList();
  if (reviewState.key) await selectReview(reviewState.key, true);
};
$('#review-create-form').onsubmit = createReviewTask;
$('#review-create-close').onclick = () => closeReviewModal($('#review-create-modal'));
$('#review-trash-close').onclick = () => closeReviewModal($('#review-trash-modal'));
$('#review-export-close').onclick = () => closeReviewModal($('#review-export-modal'));
$('#review-export-copy').onclick = copyReviewExport;
$('#review-export-download').onclick = downloadReviewExport;
$('#review-pdf-input').onchange = uploadReviewManuscript;
['review-create-modal', 'review-trash-modal', 'review-export-modal'].forEach(id => {
  const modal = document.getElementById(id);
  modal.addEventListener('cancel', () => closeReviewModal(modal));
});

renderProjectTypes();
loadStatus();
initSplitter();
syncLibraryMode();
$('#detail').innerHTML = detailPlaceholderHtml();
loadPdfToken().finally(() => {
  loadLibrary();
  loadOptions();
});
if (['library', 'upload', 'refs', 'review'].includes(location.hash.slice(1))) {
  const hash = location.hash.slice(1);
  const selector = hash === 'library'
    ? `.tabs button[data-tab="library"][data-mode="${state.libraryMode}"]`
    : `.tabs button[data-tab="${hash}"]`;
  const btn = document.querySelector(selector);
  if (btn) btn.click();
}
