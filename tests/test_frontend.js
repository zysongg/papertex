const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');
const functions = source.slice(source.indexOf('async function loadRefs()'), source.indexOf('async function openPdfTab'));

function themeFixture(saved, dark = false, blocked = false) {
  const storage = new Map(saved === null ? [] : [['papertex.theme', saved]]);
  const button = { setAttribute(name, value) { this[name] = value; } };
  const root = { dataset: {} };
  let onChange;
  const c = vm.createContext({
    document: { documentElement: root },
    $: () => button,
    localStorage: {
      getItem(key) { if (blocked) throw new Error('blocked'); return storage.get(key) ?? null; },
      setItem(key, value) { if (blocked) throw new Error('blocked'); storage.set(key, value); },
    },
    matchMedia: () => ({ matches: dark, addEventListener: (_, callback) => { onChange = callback; } }),
  });
  vm.runInContext(source.slice(source.indexOf('const systemTheme'), source.indexOf('async function api(')), c);
  return { c, root, button, storage, change: value => onChange({ matches: value }) };
}

test('theme follows the system until a manual choice is made', () => {
  const f = themeFixture(null, true);
  assert.equal(f.root.dataset.theme, 'dark');
  f.change(false);
  assert.equal(f.root.dataset.theme, 'light');
  f.button.onclick();
  assert.equal(f.root.dataset.theme, 'dark');
  assert.equal(f.button['aria-pressed'], 'true');
  assert.equal(f.button.textContent, '浅色模式');
  assert.equal(f.storage.get('papertex.theme'), 'dark');
  f.change(false);
  assert.equal(f.root.dataset.theme, 'dark');
  f.button.onclick();
  assert.equal(f.root.dataset.theme, 'light');
  assert.equal(f.button['aria-pressed'], 'false');
});

test('saved theme overrides system settings and survives initialization', () => {
  assert.equal(themeFixture('light', true).root.dataset.theme, 'light');
  assert.equal(themeFixture('dark', false).root.dataset.theme, 'dark');
  assert.equal(themeFixture('invalid', true).root.dataset.theme, 'dark');
});

test('theme toggle still works when local storage is unavailable', () => {
  const f = themeFixture(null, false, true);
  f.button.onclick();
  assert.equal(f.root.dataset.theme, 'dark');
});

function topicFixture() {
  const c = vm.createContext({ esc: text => String(text ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch])) });
  vm.runInContext(source.slice(source.indexOf('function topicNameError'),
    source.indexOf('async function loadTopics')), c);
  return c;
}

test('topic names reject empty, unchanged and unsafe values', () => {
  const c = topicFixture();
  assert.match(c.topicNameError('', 'TS'), /不能为空/);
  assert.match(c.topicNameError('TS', 'TS'), /相同/);
  assert.match(c.topicNameError('../escape', 'TS'), /只能用/);
  assert.match(c.topicNameError('x'.repeat(121), 'TS'), /120/);
  assert.equal(c.topicNameError('New_TS-2.0', 'TS'), '');
});

test('topic rows escape names and summarize years', () => {
  const c = topicFixture();
  const html = c.topicRowHtml({ name: 'A&B<"x">', count: 3, years: ['2025', '2024'] });
  assert.ok(html.includes('data-topic="A&amp;B&lt;&quot;x&quot;&gt;"'));
  assert.ok(html.includes('3 篇 · 2025, 2024'));
  assert.ok(!html.includes('<"x">'));
  assert.ok(c.topicRowHtml({ name: 'TS', count: 0, years: [] }).includes('0 篇'));
});

function listItemFixture() {
  const c = vm.createContext({
    state: {
      current: null, items: [], stars: new Set(), research: {}, researchRevision: '', researchError: '',
      readingFilter: '', texFilter: '', search: '', topicFilter: '', yearFilter: '', venueFilter: '',
      onlyStarred: false, onlyGaps: false, remoteError: '',
      libraryMode: 'paper', tagFilter: new Set(),
      knowledge: [],
      remoteName: 'research-server',
    },
  });
  vm.runInContext(source.slice(source.indexOf('const esc ='), source.indexOf('const systemTheme')), c);
  const researchStart = source.indexOf('const READING_LABELS');
  const researchEnd = source.indexOf('/* ---------- 列表 / 详情分隔条 ---------- */', researchStart);
  assert.ok(researchStart >= 0 && researchEnd > researchStart, 'research helper source boundaries exist');
  vm.runInContext(source.slice(researchStart, researchEnd), c);
  vm.runInContext(source.slice(source.indexOf('function itemTitle'), source.indexOf('function renderList')), c);
  return c;
}

test('list entries are focusable and lead with the slug in the merged view', () => {
  const c = listItemFixture();
  const item = (title, localPdf = true, remotePdf = true) => ({
    key: 'TS/2024/x', slug: '2024-TIT-Li-x',
    local: { has_pdf: localPdf, pdf: 'paper.pdf', title: '', venue: '' },
    remote: {
      has_pdf: remotePdf, pdf: 'paper.pdf', has_source: false, entry: '',
      title, venue: 'TIT',
    },
  });
  const html = c.itemHtml(item('A <b> study'));
  assert.match(html, /role="button"/);
  assert.match(html, /tabindex="0"/);
  assert.match(html, /aria-current="false"/);
  assert.match(html, /A &lt;B&gt; Study/);
  assert.doesNotMatch(html, /<b>/);
  assert.ok(html.indexOf('item-slug') < html.indexOf('item-title'));
  const bare = c.itemHtml(item(''));
  assert.doesNotMatch(bare, /item-title/);
  assert.match(c.itemHtml(item('t', true, false)), /仅本机/);
  assert.match(c.itemHtml(item('t', false, true)), /仅 research-server/);
  const withCode = item('t');
  withCode.remote.code = 'https://github.com/u/r';
  assert.match(c.itemHtml(withCode), /badge code">CODE/);
  withCode.remote.code = 'javascript:alert(1)';
  assert.doesNotMatch(c.itemHtml(withCode), /CODE/);
  c.state.current = { key: 'TS/2024/x' };
  assert.match(c.itemHtml(item('t')), /aria-current="true"/);
});

test('displayTitle title-cases words without damaging acronyms, units or mixed case', () => {
  const c = vm.createContext({});
  vm.runInContext(source.slice(source.indexOf('const esc ='), source.indexOf('const systemTheme')), c);
  assert.equal(
    c.displayTitle('MIXMODEL: EXTREME LOW RESOURCE MULTIVARI-ATE TIME SERIES FORECASTING WITH 0.1K PARAME-TERS'),
    'Mixmodel: Extreme Low Resource Multivari-ate Time Series Forecasting With 0.1K Parame-ters');
  assert.equal(c.displayTitle('LLM AND GNN FOR ICLR'), 'LLM And GNN For ICLR');
  assert.equal(c.displayTitle('MixModel: a Study'), 'MixModel: A Study');
  assert.equal(c.displayTitle('0.1K PARAMS in v2.0'), '0.1K Params In v2.0');
  assert.equal(c.displayTitle(''), '');
  assert.equal(c.displayTitle(null), '');
});

test('paper titles are display-cased in lists while itemTitle keeps the stored value', () => {
  const c = listItemFixture();
  const it = {
    key: 'TS/2024/x', slug: '2024-TIT-Li-x',
    local: { has_pdf: true, pdf: 'paper.pdf', title: '', venue: '' },
    remote: {
      has_pdf: true, pdf: 'paper.pdf', has_source: false, entry: '',
      title: 'MIXMODEL: A STUDY', venue: 'TIT',
    },
  };
  assert.match(c.itemHtml(it), /Mixmodel: A Study/);
  assert.equal(c.itemTitle(it), 'MIXMODEL: A STUDY');
});

test('added-time sort puts newest first and leaves undated papers last', () => {
  const c = listItemFixture();
  const item = (slug, added) => ({
    key: 'TS/2024/' + slug, slug, topic: 'TS', year: '2024',
    local: { has_pdf: true, pdf: 'paper.pdf', title: '', venue: '', added_at: '' },
    remote: {
      has_pdf: true, pdf: 'paper.pdf', has_source: false, entry: '',
      title: '', venue: '', added_at: added,
    },
  });
  const sorted = c.sortByAdded([
    item('b', '2026-01-01T00:00:00+00:00'),
    item('a', '2026-03-01T00:00:00Z'),
    item('c', ''),
    item('d', '2026-02-01T00:00:00+00:00'),
    item('e', '2026-03-01T00:00:00+00:00'),
  ]);
  assert.deepEqual(Array.from(sorted, x => x.slug), ['a', 'e', 'd', 'b', 'c']);
});

test('added-time view adds topic, year and date context under the title', () => {
  const c = listItemFixture();
  const added = '2026-03-01T12:00:00+00:00';
  const start = source.indexOf('function knowledgeDate');
  vm.runInContext(source.slice(start, source.indexOf('function knowledgeDetailHtml', start)), c);
  const it = {
    key: 'TS/2026/2026-ICML-Li-x', slug: '2026-ICML-Li-x', topic: 'TS', year: '2026',
    local: { has_pdf: true, pdf: 'paper.pdf', title: '', venue: '', added_at: '' },
    remote: {
      has_pdf: true, pdf: 'paper.pdf', has_source: false, entry: '',
      title: 'A title', venue: 'ICML', added_at: added,
    },
  };
  const html = c.itemHtml(it, { context: true });
  assert.ok(html.includes('TS · 2026 · 添加于 ' + c.knowledgeDate(added)));
  assert.doesNotMatch(c.itemHtml(it), /添加于/);
});

function projectOrderFixture(usage = {}, blocked = false) {
  const store = new Map(usage === null ? [] : [['papertex.projectUsage', JSON.stringify(usage)]]);
  const c = vm.createContext({
    localStorage: {
      getItem(key) { if (blocked) throw new Error('blocked'); return store.get(key) ?? null; },
      setItem(key, value) { if (blocked) throw new Error('blocked'); store.set(key, value); },
    },
  });
  vm.runInContext(source.slice(source.indexOf('const PROJECT_USAGE_KEY'),
    source.indexOf('function addToProjectHtml')), c);
  return { c, store };
}

test('projects sort by most recent use and keep stored order for ties', () => {
  const { c } = projectOrderFixture({ c: 100, a: 300 });
  const items = [{ project: 'a' }, { project: 'b' }, { project: 'c' }];
  assert.deepEqual(c.sortProjectsByUsage(items).map(x => x.project), ['a', 'c', 'b']);
  c.touchProject('b');
  assert.deepEqual(c.sortProjectsByUsage(items).map(x => x.project), ['b', 'a', 'c']);
  assert.deepEqual(c.sortProjectsByUsage([{ project: 'x' }, { project: 'y' }])
    .map(x => x.project), ['x', 'y']);
});

test('project usage falls back to stored order when local storage fails', () => {
  const { c } = projectOrderFixture({}, true);
  const items = [{ project: 'a' }, { project: 'b' }];
  assert.deepEqual(c.sortProjectsByUsage(items).map(x => x.project), ['a', 'b']);
  c.touchProject('b');
  assert.deepEqual(c.sortProjectsByUsage(items).map(x => x.project), ['a', 'b']);
});

test('merged items use the cached local title and hide gap badges when research-server is offline', () => {  const c = listItemFixture();
  const it = {
    key: 'TS/2024/x', slug: '2024-TIT-Li-x',
    local: { has_pdf: true, pdf: 'paper.pdf', title: 'Cached title', venue: 'TIT' },
    remote: { has_pdf: false, pdf: '', has_source: false, entry: '', title: '', venue: '' },
  };
  assert.equal(c.itemTitle(it), 'Cached title');
  assert.match(c.itemHtml(it), /仅本机/);
  c.state.remoteError = 'research-server 读取失败';
  assert.doesNotMatch(c.itemHtml(it), /仅本机/);
});

function knowledgeFixture(mode) {
  const c = vm.createContext({
    state: {
      libraryMode: mode, search: '', onlyStarred: false, stars: new Set(), current: null,
      knowledge: [
        { key: '_knowledge/note1', id: 'note1', kind: 'note', title: 'A <b> note', source: 'blog', url: 'https://x.test/n', tags: ['时序'] },
        { key: '_knowledge/link1', id: 'link1', kind: 'link', title: 'A link', source: 'zhihu', url: 'https://x.test/l', tags: [] },
      ],
    },
  });
  vm.runInContext(source.slice(source.indexOf('const esc ='), source.indexOf('const systemTheme')), c);
  vm.runInContext(source.slice(source.indexOf('function filteredKnowledge'), source.indexOf('function compareBadges')), c);
  return c;
}

test('knowledge views only list their own kind', () => {
  assert.deepEqual(Array.from(knowledgeFixture('note').filteredKnowledge(), it => it.kind), ['note']);
  assert.deepEqual(Array.from(knowledgeFixture('link').filteredKnowledge(), it => it.kind), ['link']);
  assert.equal(knowledgeFixture('paper').filteredKnowledge().length, 0);
});

test('knowledge search covers source, tags and url, and honors stars', () => {
  const notes = knowledgeFixture('note');
  notes.state.search = '时序';
  assert.equal(notes.filteredKnowledge().length, 1);
  notes.state.search = 'x.test/n';
  assert.equal(notes.filteredKnowledge().length, 1);
  const links = knowledgeFixture('link');
  links.state.search = 'zhihu';
  assert.equal(links.filteredKnowledge().length, 1);
  links.state.search = '';
  links.state.onlyStarred = true;
  assert.equal(links.filteredKnowledge().length, 0);
  links.state.stars.add('_knowledge/link1');
  assert.equal(links.filteredKnowledge().length, 1);
});

test('knowledge rows escape titles and drop the redundant kind badge', () => {
  const c = knowledgeFixture('note');
  const html = c.knowledgeItemHtml(c.state.knowledge[0]);
  assert.match(html, /A &lt;b&gt; note/);
  assert.doesNotMatch(html, /<b>/);
  assert.doesNotMatch(html, /class="badge/);
  assert.match(html, /blog/);
});

test('knowledge local edit banner offers push and pull for conflicts', () => {
  const c = vm.createContext({ esc: text => String(text ?? '') });
  vm.runInContext(source.slice(source.indexOf('function knowledgeDate'),
    source.indexOf('function knowledgeDetailHtml')), c);
  assert.equal(c.knowledgeSyncHtml(null), '');
  const syncing = c.knowledgeSyncHtml({ modified_at: '2026-11-05T10:44:00Z', conflict: false });
  assert.match(syncing, /本机 note\.md 有未同步的修改/);
  assert.match(syncing, /正在写回远端/);
  const conflict = c.knowledgeSyncHtml({ modified_at: '2026-11-05T10:44:00Z', conflict: true });
  assert.match(conflict, /btn-k-sync-push/);
  assert.match(conflict, /btn-k-sync-pull/);
  assert.match(conflict, /class="local-edit conflict"/);
  const unreadable = c.knowledgeSyncHtml({ conflict: true, reason: '本机 note.md 不是 UTF-8 文本，未自动写回' });
  assert.match(unreadable, /不是 UTF-8 文本/);
  assert.doesNotMatch(unreadable, /btn-k-sync-push/);
  assert.match(unreadable, /btn-k-sync-pull/);
});

test('project ref rows show a safe code link and hide unsafe schemes', () => {
  const c = vm.createContext({});
  vm.runInContext(source.slice(source.indexOf('const esc ='), source.indexOf('const systemTheme')), c);
  const start = source.indexOf('function refRow');
  vm.runInContext(source.slice(start, source.indexOf('function renderRefs', start)), c);
  const base = {
    path: 'TS/2026/x', pdf: 'x.pdf', current: { entry: '', pdf: 'x.pdf', title: '', code: 'https://github.com/u/r' },
  };
  const html = c.refRow(base, 0);
  assert.match(html, /badge code">CODE/);
  assert.match(html, /href="https:\/\/github.com\/u\/r"/);
  const unsafe = c.refRow({ ...base, code: 'javascript:alert(1)', current: { ...base.current, code: '' } }, 0);
  assert.doesNotMatch(unsafe, /CODE|javascript:/);
});

test('paper rows label the note badge Note like the TEX and CODE badges', () => {
  const c = vm.createContext({ esc: text => String(text ?? ''), remoteLabel: () => 'P6000',
    state: { remoteError: '' } });
  vm.runInContext(source.slice(
    source.indexOf('function compareBadges'), source.indexOf('function itemHtml')), c);
  const it = { local: { has_pdf: true, has_note: true },
    remote: { has_pdf: true, has_source: true, has_note: true, code: 'https://github.com/u/r' } };
  const html = c.compareBadges(it);
  assert.match(html, /class="badge note" title="已有论文笔记 note\.md">Note</);
  assert.match(html, /badge tex">TEX/);
  assert.doesNotMatch(html, /badge note">笔记/);
  assert.doesNotMatch(c.compareBadges({ local: { has_pdf: false, has_note: false },
    remote: { has_pdf: false, has_source: false, has_note: false } }), /badge note/);
});

test('note refs get a collapsible preview and local-open action', () => {
  const c = vm.createContext({});
  vm.runInContext(source.slice(source.indexOf('const esc ='), source.indexOf('const systemTheme')), c);
  const start = source.indexOf('function refRow');
  vm.runInContext(source.slice(start, source.indexOf('function renderRefs', start)), c);
  const base = {
    path: '_knowledge/' + 'a'.repeat(32), entry: 'note.md',
    current: { kind: 'note', entry: 'note.md', title: '一篇笔记' },
  };
  const html = c.refRow(base, 3);
  assert.match(html, /class="badge note">笔记/);
  assert.match(html, /data-note-toggle="3" aria-expanded="false" aria-controls="note-panel-3">预览笔记/);
  assert.match(html, /id="note-panel-3" role="region"/);
  assert.match(html, /class="markdown-body"/);
  assert.match(html, /data-open-local="3">用本地软件打开/);
  assert.match(html, /entry: note\.md/);

  const missing = c.refRow({ ...base, missing: true, current: {} }, 0);
  assert.doesNotMatch(missing, /data-note-toggle|note-panel|data-open-local/);
  const link = c.refRow({
    path: '_knowledge/' + 'b'.repeat(32), url: 'https://example.com',
    current: { kind: 'link', url: 'https://example.com', title: '链接' },
  }, 0);
  assert.doesNotMatch(link, /data-note-toggle|note-panel|data-open-local/);
});

test('markdown renderer uses the caller empty-state text and escapes it', () => {
  const markdownSource = fs.readFileSync(path.join(__dirname, '../app/static/markdown.js'), 'utf8');
  const c = vm.createContext({ window: {} });
  vm.runInContext(markdownSource, c);
  const md = c.window.PapertexMarkdown;
  assert.equal(md.render('', { emptyText: '还没有正文' }), '<p class="muted">还没有正文</p>');
  assert.equal(md.render('   ', { emptyText: '<b>x</b>' }), '<p class="muted">&lt;b&gt;x&lt;/b&gt;</p>');
  assert.equal(md.render(''), '<p class="muted">没有正文</p>');
});

test('note preview toggles inline, keeps the loaded body and collapses without refetching', async () => {
  const classes = new Set(['note-panel', 'hidden']);
  const body = { innerHTML: '' };
  const panel = {
    dataset: {},
    classList: {
      add: name => classes.add(name),
      remove: name => classes.delete(name),
      contains: name => classes.has(name),
    },
    querySelector: () => body,
  };
  const button = {
    attrs: { 'aria-expanded': 'false' }, textContent: '',
    getAttribute(name) { return this.attrs[name]; },
    setAttribute(name, value) { this.attrs[name] = value; },
  };
  let calls = 0;
  const c = vm.createContext({
    state: { refs: [{ path: '_knowledge/' + 'a'.repeat(32), entry: 'note.md' }] },
    document: { getElementById: id => (id === 'note-panel-0' ? panel : null) },
    api: async () => { calls += 1; return { item: { body: '# 标题' } }; },
    PapertexMarkdown: { render: text => `<h1>${text}</h1>` },
    renderViewState: () => {},
    encodeURIComponent,
  });
  vm.runInContext(source.slice(source.indexOf('async function loadNotePreview'),
    source.indexOf('function renderRefs')), c);

  c.toggleNotePreview(0, button);
  assert.equal(classes.has('hidden'), false);
  assert.equal(button.textContent, '收起笔记');
  assert.equal(button.attrs['aria-expanded'], 'true');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(body.innerHTML, '<h1># 标题</h1>');
  assert.equal(calls, 1);

  c.toggleNotePreview(0, button);
  assert.equal(classes.has('hidden'), true);
  assert.equal(button.textContent, '预览笔记');
  c.toggleNotePreview(0, button);
  assert.equal(calls, 1);
  assert.equal(body.innerHTML, '<h1># 标题</h1>');
});

// Minimal selector stubs: real research helpers run in vm; no HTML parser or network.
function researchNode() {
  const children = new Map();
  const classes = new Set();
  return {
    innerHTML: '', textContent: '', value: '', disabled: false, isConnected: true,
    classList: {
      add: name => classes.add(name),
      remove: name => classes.delete(name),
      contains: name => classes.has(name),
    },
    elements: { status: { value: '' }, note: { value: '' }, label: { value: '' } },
    querySelector(selector) {
      if (!children.has(selector)) children.set(selector, researchNode());
      return children.get(selector);
    },
    querySelectorAll() { return this.matches || []; },
    focus() { this.focused = true; },
    select() { this.selected = true; },
  };
}

function researchFixture(api = async () => { throw new Error('unexpected API request'); }) {
  const c = listItemFixture();
  const root = researchNode();
  const notices = [];
  Object.assign(c, {
    api, $: selector => root.querySelector(selector),
    renderList() {}, showDetail() {}, syncSearch() {},
    toast: (message, kind) => notices.push({ message, kind }),
    confirmDialog: async () => true,
    navigator: { clipboard: { writeText: async () => { throw new Error('clipboard denied'); } } },
    renderViewState(target, kind, title, detail) {
      target.innerHTML = kind + ': ' + title + (detail || '');
    },
  });
  return { c, root, notices };
}

function researchItem(name) {
  return {
    key: 'TS/2026/' + name, slug: '2026-TIT-' + name, topic: 'TS', year: '2026',
    local: { has_pdf: true, title: name, venue: 'TIT' },
    remote: { has_pdf: false, has_source: false },
  };
}

const submitEvent = () => ({ preventDefault() {} });
const citationData = () => ({
  fields: { entry_type: 'article', title: 'A study', authors: 'A and B', year: '2026' },
  warnings: [], cite: '\\cite{paper}', bibtex: '@article{paper, title={A study}}',
});

test('reading filters include every state, queue and unmarked papers', () => {
  const c = listItemFixture();
  const statuses = ['unread', 'reading', 'read', 'reproduce', 'none', 'absent'];
  c.state.items = statuses.map(researchItem);
  for (const status of statuses.slice(0, 5)) {
    c.state.research['TS/2026/' + status] = { status: status === 'none' ? '' : status };
  }
  const names = () => Array.from(c.filtered(), it => it.local.title);
  assert.deepEqual(names(), statuses);
  for (const status of statuses.slice(0, 4)) {
    c.state.readingFilter = status;
    assert.deepEqual(names(), [status]);
  }
  c.state.readingFilter = 'queue';
  assert.deepEqual(names(), ['unread', 'reading', 'reproduce']);
  c.state.readingFilter = 'none';
  assert.deepEqual(names(), ['none', 'absent']);
  assert.equal(Object.keys(c.researchRecord('unknown')).length, 0);
  assert.equal(Object.hasOwn(c.state.research, 'unknown'), false);
});

test('TeX filter narrows by remote source and is ignored while research-server is offline', () => {
  const c = listItemFixture();
  const withTex = researchItem('with-tex');
  const withoutTex = researchItem('without-tex');
  withTex.remote.has_source = true;
  c.state.items = [withTex, withoutTex];
  c.state.texFilter = 'yes';
  assert.deepEqual(Array.from(c.filtered(), it => it.key), [withTex.key]);
  c.state.texFilter = 'no';
  assert.deepEqual(Array.from(c.filtered(), it => it.key), [withoutTex.key]);
  c.state.remoteError = 'research-server 读取失败';
  assert.equal(c.filtered().length, 2, 'unknown source state must not hide papers');
});

test('reading queue intersects stars, topic, year, venue, search and gaps', () => {
  const c = listItemFixture();
  const included = researchItem('matching');
  const variants = [
    included, { ...researchItem('not-starred') },
    { ...researchItem('wrong-topic'), topic: 'CV' },
    { ...researchItem('wrong-year'), year: '2025' },
    { ...researchItem('wrong-venue'), local: { ...included.local, venue: 'ICML' } },
    { ...researchItem('no-gap'), remote: { has_pdf: true, has_source: false } },
    researchItem('wrong-search'), researchItem('already-read'),
  ];
  c.state.items = variants;
  for (const it of variants) {
    c.state.research[it.key] = { status: it.local.title === 'already-read' ? 'read' : 'reading' };
    if (!it.key.endsWith('not-starred')) c.state.stars.add(it.key);
  }
  Object.assign(c.state, {
    readingFilter: 'queue', onlyStarred: true, topicFilter: 'TS', yearFilter: '2026',
    venueFilter: 'TIT', onlyGaps: true,
  });
  assert.deepEqual(Array.from(c.filtered(), it => it.key), [included.key, 'TS/2026/wrong-search']);
  c.state.search = 'MATCHING';
  assert.deepEqual(Array.from(c.filtered(), it => it.key), [included.key]);
});

test('list and research HTML show reading/version badges and escape untrusted values', () => {
  const c = listItemFixture();
  const unsafe = '<img src=x onerror="bad()">&\'';
  const it = researchItem(unsafe);
  c.state.research[it.key] = { status: 'reading', version_group: unsafe, note: '</textarea>' + unsafe };
  const html = c.itemHtml(it);
  assert.match(html, /reading-badge">在读/);
  assert.match(html, /关联版本/);
  assert.match(html, /&lt;img src=x onerror=&quot;bad\(\)&quot;&gt;&amp;&#39;/);
  assert.doesNotMatch(html, /<img/);
  const detail = c.researchHtml(it);
  assert.match(detail, /value="reading" selected/);
  assert.match(detail, /&lt;\/textarea&gt;&lt;img/);
  assert.doesNotMatch(detail, /<img/);
  for (const [status, label] of Object.entries({ unread: '待读', reading: '在读', read: '已读', reproduce: '待复现' })) {
    c.state.research[it.key].status = status;
    assert.ok(c.researchBadge(it.key).includes(label));
  }
  c.state.research[it.key].status = unsafe;
  assert.doesNotMatch(c.researchBadge(it.key), /<img|reading-badge/);
});

test('paper tags filter with OR semantics and extend the search index', () => {
  const c = listItemFixture();
  const items = ['moe-ts', 'moe', 'diffusion', 'untagged'].map(researchItem);
  c.state.items = items;
  c.state.research[items[0].key] = { tags: ['MoE', '时序'] };
  c.state.research[items[1].key] = { tags: ['MoE'] };
  c.state.research[items[2].key] = { tags: '损坏记录' };
  const titles = () => Array.from(c.filtered(), it => it.local.title);
  c.state.tagFilter.add('MoE');
  assert.deepEqual(titles(), ['moe-ts', 'moe']);
  c.state.tagFilter.add('时序');
  assert.deepEqual(titles(), ['moe-ts', 'moe'], '命中任一标签即保留');
  c.state.tagFilter.delete('MoE');
  assert.deepEqual(titles(), ['moe-ts']);
  c.state.tagFilter.clear();
  c.state.tagFilter.add('损坏记录');
  assert.deepEqual(titles(), [], '非数组的标签记录视作没有标签');
  c.state.tagFilter.clear();
  c.state.search = 'moe';
  assert.deepEqual(titles(), ['moe-ts', 'moe'], '关键词搜索也能命中标签');
  c.state.search = '';
  c.state.tagFilter.add('MoE');
  c.state.researchError = 'research-server 读取失败';
  assert.deepEqual(titles(), ['moe-ts', 'moe', 'diffusion', 'untagged']);
});

test('list badges always show tags and highlight filter hits', () => {
  const c = listItemFixture();
  const it = researchItem('paper');
  c.state.research[it.key] = { tags: ['<b>MoE', '时序'] };
  const tone = tag => c.tagTone(tag);
  const plain = c.researchBadge(it.key);
  assert.ok(plain.includes(`badge item-tag ${tone('<b>MoE')}">#&lt;b&gt;MoE`));
  assert.ok(plain.includes(`badge item-tag ${tone('时序')}">#时序`));
  assert.doesNotMatch(plain, /tag-hit|<b>/);
  c.state.tagFilter.add('<b>MoE');
  const html = c.researchBadge(it.key);
  assert.ok(html.includes(`${tone('<b>MoE')} tag-hit">#&lt;b&gt;MoE`));
  assert.ok(html.includes(`${tone('时序')}">#时序`), '未命中的标签照常显示，但不高亮');
  assert.match(c.itemHtml(it), /research-badges">[\s\S]*#时序/);
  c.state.researchError = 'offline';
  assert.equal(c.researchBadge(it.key), '');
});

test('tag colours are stable per tag text and stay within the palette', () => {
  const c = listItemFixture();
  const tones = ['MoE', 'moe', '时序', 'Contrastive', '扩散模型', '"><x>', ''].map(tag => c.tagTone(tag));
  for (const t of tones) assert.match(t, /^tag-c[0-9]$/);
  assert.equal(c.tagTone('MoE'), c.tagTone('MoE'));
  assert.equal(new Set(['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'].map(c.tagTone)).size > 3, true, '不同标签分散到多种颜色');
});
test('tag filter bar orders by usage, hides when unusable and toggles on click', () => {
  const { c, root } = researchFixture();
  const host = root.querySelector('#tag-filters');
  host.matches = [{ dataset: { tag: 'MoE' } }, { dataset: { tag: '时序' } }];
  c.state.research['TS/2026/a'] = { tags: ['MoE', '时序'] };
  c.state.research['TS/2026/b'] = { tags: ['MoE'] };
  c.state.research['TS/2026/c'] = { tags: ['<img src=x>'] };
  c.state.tagFilter.add('已删除的标签');
  c.renderTagFilters();
  assert.equal(host.classList.contains('hidden'), false);
  assert.match(host.innerHTML, /tag-filter-label/);
  assert.ok(host.innerHTML.indexOf('>MoE <') < host.innerHTML.indexOf('>时序 <'), '用量多的标签在前');
  assert.match(host.innerHTML,
    /data-tag="MoE"[\s\S]*?aria-pressed="false"[\s\S]*?>MoE <span class="muted">2<\/span>/);
  assert.equal(c.state.tagFilter.size, 0, '失效的自动选择要清掉');
  assert.match(host.innerHTML, /data-tag="&lt;img src=x&gt;"/);
  assert.doesNotMatch(host.innerHTML, /<img/);
  host.matches[0].onclick();
  assert.equal(c.state.tagFilter.has('MoE'), true);
  host.matches[0].onclick();
  assert.equal(c.state.tagFilter.has('MoE'), false, '再次点击取消');
  c.state.libraryMode = 'note';
  c.renderTagFilters();
  assert.equal(host.innerHTML, '');
  assert.equal(host.classList.contains('hidden'), true);
  c.state.libraryMode = 'paper';
  c.state.researchError = 'offline';
  c.renderTagFilters();
  assert.equal(host.classList.contains('hidden'), true, '读取失败时不展示陈旧标签');
  c.state.researchError = '';
  c.state.research = {};
  c.renderTagFilters();
  assert.equal(host.classList.contains('hidden'), true, '还没有标签时不占位');
});

test('tag editor adds typed tags, removes chips and posts the whole list', async () => {
  const writes = [];
  const { c } = researchFixture(async (url, options) => {
    if (!options) return { items: {}, revision: 'v1' };
    const payload = JSON.parse(options.body);
    writes.push(payload);
    return { items: { [payload.key]: { tags: payload.tags } }, revision: 'v2' };
  });
  const it = researchItem('a');
  c.state.items = [it];
  c.state.research = { [it.key]: { tags: ['MoE'] } };
  c.state.researchRevision = 'v1';
  const panel = researchNode();
  const editor = panel.querySelector('#tag-editor');
  editor.matches = [{ dataset: { tag: 'MoE' } }];
  c.bindTagEditor(panel, it);
  assert.match(editor.innerHTML, /class="tag-chip removable tag-c[0-9]"/);
  assert.match(editor.innerHTML, /aria-label="移除标签 MoE"/);
  assert.match(panel.querySelector('#tag-options').innerHTML, /<option value="MoE">/);
  const input = panel.querySelector('#tag-input');
  const result = panel.querySelector('#tag-result');
  input.value = ' 时序， diffusion ';
  await panel.querySelector('#tag-form').onsubmit(submitEvent());
  assert.deepEqual(writes[0].tags, ['MoE', '时序', 'diffusion']);
  assert.equal(writes[0].revision, 'v1');
  assert.equal(result.textContent, '已保存');
  assert.equal(input.value, '');
  assert.equal(input.disabled, false);
  assert.match(editor.innerHTML, /时序/);
  await editor.matches[0].onclick();
  assert.deepEqual(writes[1].tags, ['时序', 'diffusion'], '移除只发剩余标签');
  assert.equal(writes[1].revision, 'v2');
  input.value = '  ， ';
  await panel.querySelector('#tag-form').onsubmit(submitEvent());
  assert.equal(writes.length, 2, '空标签不发请求');
  assert.equal(result.textContent, '标签不能为空');
});

test('research load failure disables editing/filtering without treating stale data as current', async () => {
  const calls = [];
  let offline = true;
  const { c } = researchFixture(async (url, options) => {
    calls.push({ url, options });
    if (offline) throw new Error('<offline>');
    return { items: {}, revision: 'fresh' };
  });
  const it = researchItem('paper');
  c.state.items = [it];
  c.state.research[it.key] = { status: 'read', version_group: 'old' };
  c.state.readingFilter = 'queue';
  await c.loadResearch();
  assert.equal(c.$('#filter-reading').disabled, true);
  assert.equal(c.state.researchError, '<offline>');
  assert.equal(c.researchBadge(it.key), '');
  assert.match(c.researchHtml(it), /&lt;offline&gt;/);
  assert.doesNotMatch(c.researchHtml(it), /<form|<input|<textarea|<button/);
  assert.equal(c.filtered().length, 1, 'unavailable research must not hide library papers');
  await assert.rejects(c.saveResearch('/api/reading', { key: it.key }), /成功读取/);
  assert.equal(calls.length, 1, 'blocked save sends no write');
  offline = false;
  await c.loadResearch();
  assert.equal(c.$('#filter-reading').disabled, false);
  assert.equal(c.state.researchError, '');
  assert.equal(c.state.researchRevision, 'fresh');
});

test('research saves prevent concurrent writes and advance revision after success', async () => {
  const pending = deferred();
  const calls = [];
  const { c } = researchFixture((url, options) => {
    calls.push({ url, options, payload: JSON.parse(options.body) });
    return calls.length === 1 ? pending.promise : Promise.resolve({ items: {}, revision: 'v3' });
  });
  c.state.researchRevision = 'v1';
  const payload = { key: 'TS/2026/paper', status: 'reading', note: 'input' };
  const first = c.saveResearch('/api/reading', payload);
  await assert.rejects(c.saveResearch('/api/citation', { key: payload.key }), /正在保存/);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(calls[0].options.headers['Content-Type'], 'application/json');
  assert.deepEqual(calls[0].payload, { ...payload, revision: 'v1' });
  assert.equal(Object.hasOwn(payload, 'revision'), false);
  pending.resolve({ items: { [payload.key]: payload }, revision: 'v2' });
  await first;
  assert.equal(c.state.researchRevision, 'v2');
  assert.equal(c.researchRecord(payload.key).note, 'input');
  await c.saveResearch('/api/reading', payload);
  assert.equal(calls[1].payload.revision, 'v2');
});

test('409 reloads research and throws without retrying the write or mutating input', async () => {
  const calls = [];
  const input = { key: 'TS/2026/paper', status: 'reading', note: 'unsaved input' };
  const { c } = researchFixture(async (url, options) => {
    calls.push({ url, options });
    if (options) throw Object.assign(new Error('revision conflict'), { status: 409 });
    return { items: { [input.key]: { note: 'another tab' } }, revision: 'v2' };
  });
  c.state.researchRevision = 'v1';
  await assert.rejects(c.saveResearch('/api/reading', input), /revision conflict.*当前输入保留/);
  assert.deepEqual(calls.map(call => call.url), ['/api/reading', '/api/research']);
  assert.equal(input.note, 'unsaved input');
  assert.equal(c.state.researchRevision, 'v2');
  assert.equal(c.researchRecord(input.key).note, 'another tab');
  // A later explicit save is permitted; only automatic retries are forbidden.
  c.api = async () => ({ items: {}, revision: 'v3' });
  await c.saveResearch('/api/reading', input);
});

test('paper edits retry once after refreshing a stale research revision', async () => {
  const calls = [];
  const { c } = researchFixture(async url => {
    calls.push(url);
    return { items: {}, revision: 'fresh' };
  });
  c.state.researchRevision = 'stale';
  let attempts = 0;
  const result = await c.retryAfterResearchRefresh(async () => {
    attempts += 1;
    if (attempts === 1) {
      throw Object.assign(new Error('研究记录已改变或缺少 revision，请刷新后重试（耗时 0分8秒）'), { status: 409 });
    }
    return { key: 'TS/2026/paper' };
  });
  assert.deepEqual(result, { key: 'TS/2026/paper' });
  assert.equal(attempts, 2, '刷新后只重试一次');
  assert.deepEqual(calls, ['/api/research']);
  assert.equal(c.state.researchRevision, 'fresh', '重试前先缓存最新 revision');
});

test('paper edits keep other conflicts and failures untouched', async () => {
  const { c } = researchFixture(async () => {
    throw new Error('unexpected API request');
  });
  let attempts = 0;
  const rename = async () => {
    attempts += 1;
    throw Object.assign(new Error('目标目录已存在（耗时 0分1秒）'), { status: 409 });
  };
  await assert.rejects(c.retryAfterResearchRefresh(rename), /目标目录已存在/);
  assert.equal(attempts, 1, '目录冲突不能重试');
  let offline = 0;
  const stale = async () => {
    offline += 1;
    throw Object.assign(new Error('研究记录已改变，请刷新后重试'), { status: 409 });
  };
  await assert.rejects(c.retryAfterResearchRefresh(stale), /研究记录已改变/);
  assert.equal(offline, 1);
});

test('paper edits surface the conflict when research cannot be reloaded', async () => {
  const { c } = researchFixture(async () => {
    throw new Error('offline');
  });
  let attempts = 0;
  const run = async () => {
    attempts += 1;
    throw Object.assign(new Error('研究记录已改变，请刷新后重试'), { status: 409 });
  };
  await assert.rejects(c.retryAfterResearchRefresh(run), /研究记录已改变/);
  assert.equal(attempts, 1, '刷新失败时不再重试');
  assert.equal(c.state.researchError, 'offline');
});

test('reading form retains edits made during a conflicting save and re-enables submit', async () => {
  const pending = deferred();
  let writes = 0;
  const { c } = researchFixture(async (url, options) => {
    if (options) { writes++; return pending.promise; }
    return { items: {}, revision: 'v2' };
  });
  const it = researchItem('paper');
  c.state.items = [it];
  c.bindResearch(it);
  const form = c.$('#research-panel').querySelector('#reading-form');
  form.elements.status.value = 'reading';
  form.elements.note.value = 'first draft';
  const save = form.onsubmit(submitEvent());
  assert.equal(form.querySelector('button').disabled, true);
  form.elements.note.value = 'newer draft';
  pending.reject(Object.assign(new Error('revision conflict'), { status: 409 }));
  await save;
  assert.equal(writes, 1);
  assert.equal(form.elements.note.value, 'newer draft');
  assert.equal(form.elements.status.value, 'reading');
  assert.equal(form.querySelector('button').disabled, false);
  assert.match(c.$('#research-panel').querySelector('#reading-result').textContent, /当前输入保留/);
});

test('refreshing global research cannot authorize a stale reading form overwrite', async () => {
  const writes = [];
  const it = researchItem('paper');
  const { c } = researchFixture(async (url, options) => {
    if (options) {
      writes.push(JSON.parse(options.body));
      throw Object.assign(new Error('revision conflict'), { status: 409 });
    }
    return { items: { [it.key]: { status: 'read', note: '另一窗口的新记录' } }, revision: 'v2' };
  });
  c.state.researchRevision = 'v1';
  c.state.items = [it];
  c.bindResearch(it);
  const panel = c.$('#research-panel');
  const form = panel.querySelector('#reading-form');
  form.elements.status.value = 'reading';
  form.elements.note.value = '旧表单输入';
  await c.loadResearch();
  await form.onsubmit(submitEvent());
  assert.equal(writes[0].revision, 'v1');
  assert.equal(writes.length, 1);
  assert.equal(form.elements.note.value, '旧表单输入');
  assert.match(panel.querySelector('#reading-result').textContent, /另一窗口的新记录/);
});

test('citation and version forms submit the revision belonging to their displayed data', async () => {
  const writes = [];
  const { c } = researchFixture(async (url, options) => {
    if (options) {
      writes.push(JSON.parse(options.body));
      throw Object.assign(new Error('revision conflict'), { status: 409 });
    }
    if (url.startsWith('/api/citation')) return citationData();
    return { items: {}, revision: 'v2' };
  });
  c.FormData = class { [Symbol.iterator]() { return [['title', 'old']][Symbol.iterator](); } };
  const it = researchItem('paper'), other = researchItem('other');
  c.state.items = [it, other];
  c.state.researchRevision = 'v1';
  const citationHost = researchNode(), versionHost = researchNode();
  await c.loadCitation(it, citationHost);
  c.renderVersions(it, versionHost);
  versionHost.querySelector('#version-target').value = other.key;
  await c.loadResearch();
  await citationHost.querySelector('#citation-form').onsubmit(submitEvent());
  await versionHost.querySelector('#version-form').onsubmit(submitEvent());
  assert.deepEqual(writes.map(p => p.revision), ['v1', 'v1']);
  assert.equal(writes.length, 2);
});

test('version members retain missing papers, isolate groups and escape member keys', () => {
  const { c } = researchFixture();
  const it = researchItem('paper'), other = researchItem('other');
  const missing = 'TS/2026/<missing>"';
  c.state.items = [it, other];
  c.state.research = {
    [it.key]: { version_group: 'g', version_label: 'arxiv' },
    [other.key]: { version_group: 'g', version_label: 'journal' },
    [missing]: { version_group: 'g', version_label: 'conference' },
    unrelated: { version_group: 'elsewhere' },
  };
  const members = c.versionMembers(it.key);
  assert.deepEqual(Array.from(members, member => member.key), [it.key, other.key, missing]);
  assert.equal(members[2].item, undefined);
  assert.equal(c.versionMembers('unmarked').length, 0);
  const host = researchNode();
  c.renderVersions(it, host);
  for (const label of ['arXiv 版', '期刊版', '会议版']) assert.ok(host.innerHTML.includes(label));
  assert.match(host.innerHTML, /库中缺失/);
  assert.match(host.innerHTML, /&lt;missing&gt;&quot;/);
  assert.doesNotMatch(host.innerHTML, /<missing>/);
  c.state.remoteError = 'offline';
  c.renderVersions(it, host);
  assert.match(host.innerHTML, /未核实/);
  assert.doesNotMatch(host.innerHTML, /库中缺失/);
});

test('citation escapes fields, warnings, cite and BibTeX, then selects text if clipboard fails', async () => {
  const unsafe = '<img src=x onerror="bad()">&\'';
  const data = citationData();
  data.fields.title = unsafe;
  data.warnings = [unsafe];
  data.cite = '\\cite{' + unsafe + '}';
  data.bibtex = '</textarea>' + unsafe;
  const urls = [];
  const { c, notices } = researchFixture(async url => { urls.push(url); return data; });
  const host = researchNode(), it = researchItem('paper & more');
  await c.loadCitation(it, host);
  assert.equal(urls[0], '/api/citation?key=' + encodeURIComponent(it.key));
  assert.doesNotMatch(host.innerHTML, /<img/);
  assert.match(host.innerHTML, /value="&lt;img/);
  assert.match(host.innerHTML, /id="citation-warnings">&lt;img/);
  assert.match(host.innerHTML, /id="citation-cite" readonly value="\\cite\{&lt;img/);
  assert.match(host.innerHTML, /&lt;\/textarea&gt;&lt;img/);
  await host.querySelector('#copy-bib').onclick();
  const output = host.querySelector('#citation-bib');
  assert.equal(output.value, data.bibtex);
  assert.equal(output.focused, true);
  assert.equal(output.selected, true);
  assert.equal(notices.at(-1).kind, 'warn');
  let copied;
  c.navigator.clipboard.writeText = async text => { copied = text; };
  await host.querySelector('#copy-cite').onclick();
  assert.equal(copied, data.cite);
  assert.equal(notices.at(-1).kind, 'ok');
  host.querySelector('#citation-form').oninput();
  assert.equal(host.querySelector('#copy-cite').disabled, true);
  assert.equal(host.querySelector('#copy-bib').disabled, true);
  assert.match(host.querySelector('#citation-result').textContent, /未保存/);
});

test('late citation success or failure cannot render into a detached detail host', async () => {
  for (const fails of [false, true]) {
    const pending = deferred();
    const { c } = researchFixture(() => pending.promise);
    const host = researchNode();
    const loading = c.loadCitation(researchItem('paper'), host);
    host.isConnected = false;
    host.innerHTML = 'detached detail';
    if (fails) pending.reject(new Error('late error'));
    else pending.resolve(citationData());
    await loading;
    assert.equal(host.innerHTML, 'detached detail');
  }
});

test('citation save completion does not refetch or render a detached detail host', async () => {
  const pending = deferred(), calls = [];
  const { c } = researchFixture((url, options) => {
    calls.push({ url, options });
    return options ? pending.promise : Promise.resolve(citationData());
  });
  c.FormData = class {
    constructor(form) { this.values = Object.entries(form.elements).map(([key, input]) => [key, input.value]); }
    [Symbol.iterator]() { return this.values[Symbol.iterator](); }
  };
  const host = researchNode();
  await c.loadCitation(researchItem('paper'), host);
  const form = host.querySelector('#citation-form');
  form.elements = { title: { value: 'edited title' }, entry_type: { value: 'article' } };
  const saving = form.onsubmit(submitEvent());
  assert.equal(form.querySelector('button').disabled, true);
  assert.equal(calls[1].url, '/api/citation');
  assert.deepEqual(JSON.parse(calls[1].options.body).fields, { title: 'edited title', entry_type: 'article' });
  host.isConnected = false;
  host.innerHTML = 'detached detail';
  pending.resolve({ items: {}, revision: 'v2' });
  await saving;
  assert.equal(host.innerHTML, 'detached detail');
  assert.equal(calls.length, 2, 'no reload of citation after detail was closed');
});

test('version actions only POST link/unlink and never delete papers or rerender a detached host', async () => {
  for (const action of ['link', 'unlink']) {
    const pending = deferred(), calls = [];
    const { c } = researchFixture((url, options) => {
      calls.push({ url, options, payload: JSON.parse(options.body) });
      return pending.promise;
    });
    const it = researchItem('paper'), other = researchItem('other');
    c.state.items = [it, other];
    c.state.researchRevision = 'v1';
    c.state.research = { [it.key]: { version_group: 'g' }, [other.key]: { version_group: 'g' } };
    const host = researchNode();
    const unlink = { dataset: { versionUnlink: other.key }, disabled: false };
    host.querySelectorAll = selector => selector === '[data-version-unlink]' ? [unlink] : [];
    c.renderVersions(it, host);
    host.querySelector('#version-target').value = other.key;
    host.querySelector('#version-target-label').value = 'journal';
    host.querySelector('#version-form').elements.label.value = 'conference';
    const operation = action === 'link'
      ? host.querySelector('#version-form').onsubmit(submitEvent()) : unlink.onclick();
    await flush();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, '/api/versions/' + action);
    assert.equal(calls[0].options.method, 'POST');
    assert.deepEqual(calls[0].payload, action === 'link'
      ? { key: it.key, other_key: other.key, label: 'conference', other_label: 'journal', revision: 'v1' }
      : { key: other.key, revision: 'v1' });
    host.isConnected = false;
    host.innerHTML = 'detached detail';
    pending.resolve({ items: {}, revision: 'v2' });
    await operation;
    assert.equal(host.innerHTML, 'detached detail');
    assert.equal(calls.length, 1, 'no follow-up deletion or other write');
    assert.deepEqual(c.state.items, [it, other]);
  }
});

function confirmFixture() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) {
      elements.set(id, {
        id, textContent: '', value: '', disabled: false, isConnected: false,
        classList: {
          set: new Set(),
          add(name) { this.set.add(name); },
          remove(name) { this.set.delete(name); },
          toggle(name, on) { if (on) this.set.add(name); else this.set.delete(name); },
        },
        focus() {}, close() { this.closed = true; }, showModal() { this.opened = true; },
      });
    }
    return elements.get(id);
  };
  const c = vm.createContext({
    document: {
      getElementById: element,
      activeElement: { isConnected: false },
      body: { classList: { add() {}, remove() {} } },
    },
  });
  vm.runInContext(source.slice(source.indexOf('function confirmDialog'),
    source.indexOf('async function api(')), c);
  return { c, element };
}

test('typed confirmation unlocks only for the exact name and restores state', async () => {
  const { c, element } = confirmFixture();
  const modal = element('confirm-modal');
  const input = element('confirm-input');
  const ok = element('confirm-ok');
  const pending = c.confirmDialog({ text: '移入回收区', expected: 'TS/2024/x' });
  assert.equal(modal.opened, true);
  assert.equal(ok.disabled, true);
  input.value = 'TS/2024/';
  input.oninput();
  assert.equal(ok.disabled, true);
  input.value = 'TS/2024/x';
  input.oninput();
  assert.equal(ok.disabled, false);
  ok.onclick();
  assert.equal(await pending, true);
  assert.equal(modal.closed, true);
  assert.ok(modal.classList.set.has('hidden'));
});

test('confirming can be cancelled by button, escape or backdrop-less close', async () => {
  const { c, element } = confirmFixture();
  const cancelPending = c.confirmDialog({ text: '确定？' });
  element('confirm-cancel').onclick();
  assert.equal(await cancelPending, false);

  const escapePending = c.confirmDialog({ text: '确定？' });
  let prevented = false;
  element('confirm-modal').oncancel({ preventDefault() { prevented = true; } });
  assert.equal(await escapePending, false);
  assert.equal(prevented, true);
});

function toastFixture() {
  const host = {
    children: [],
    get firstElementChild() { return this.children[0] || null; },
    append(el) { this.children.push(el); },
  };
  const timers = [];
  const c = vm.createContext({
    document: {
      getElementById: () => host,
      createElement: () => {
        const classes = new Set();
        return {
          className: '', textContent: '', tabIndex: 0,
          classList: {
            has: name => classes.has(name),
            add: name => classes.add(name),
            remove: name => classes.delete(name),
          },
          setAttribute() {},
          remove() { const i = host.children.indexOf(this); if (i >= 0) host.children.splice(i, 1); },
        };
      },
    },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout() {},
  });
  vm.runInContext(source.slice(source.indexOf('const TOAST_LIMIT'), source.indexOf('function confirmDialog')), c);
  return { c, host, timers };
}

test('toasts stack up to a limit, map severity and dismiss on click', () => {
  const { c, host, timers } = toastFixture();
  c.toast('已保存', 'ok');
  c.toast('部分完成', 'warn');
  c.toast('失败', 'error');
  c.toast('提示');
  assert.equal(host.children.length, 4);
  assert.equal(host.children[0].className, 'toast ok');
  assert.equal(host.children[0].textContent, '已保存');
  assert.equal(host.children[1].className, 'toast warn');
  assert.equal(host.children[2].className, 'toast error');
  const oldest = host.children[0];
  const newest = c.toast('第五个', 'ok');
  assert.equal(host.children.length, 4);
  assert.ok(!host.children.includes(oldest));
  assert.ok(host.children.includes(newest));
  assert.equal(c.toast('   '), null);
  c.toast('可关闭');
  const target = host.children[host.children.length - 1];
  target.onclick();
  assert.equal(target.classList.has('leaving'), true);
  timers[timers.length - 1].fn();
  assert.ok(!host.children.includes(target));
});

function fixture(api) {
  const elements = new Map();
  const context = vm.createContext({
    state: { project: 'first', projectSource: 'remote', refs: [], refsSession: null },
    $: selector => {
      if (!elements.has(selector)) elements.set(selector, { textContent: '', innerHTML: '' });
      return elements.get(selector);
    },
    api,
    structuredClone,
    esc: text => text,
    renderRefs() {},
    renderViewState(target, kind, title, detail) { target.textContent = title + (detail || ''); },
    invalidateProjects() {},
  });
  vm.runInContext(functions, context);
  return context;
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const document = () => ({ references: [{ path: 'TS/2026/paper', note: 'original' }], revision: 'v1', file: 'reference-tex.json', exists: true });

function progressFixture(api) {
  const timers = new Map();
  let nextTimer = 0;
  const schedule = (callback, delay) => { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; };
  const c = vm.createContext({
    api, Date, AbortController,
    crypto: { randomUUID: () => '01234567-89ab-cdef-0123-456789abcdef' },
    setTimeout: schedule, setInterval: schedule,
    clearTimeout: id => timers.delete(id), clearInterval: id => timers.delete(id),
  });
  vm.runInContext(source.slice(source.indexOf('const operationViews'), source.indexOf("$$('.tabs button')")), c);
  return { c, timers, target: { textContent: '', isConnected: true, setAttribute() {} } };
}

const flush = () => new Promise(resolve => setImmediate(resolve));

test('pdf viewer URLs carry the per-process token', () => {
  const c = vm.createContext({ state: { pdfToken: 'tok/123' }, encodeURIComponent });
  vm.runInContext(source.slice(source.indexOf('function paperPdfUrl'), source.indexOf('async function loadLibrary()')), c);
  const url = new URL('http://localhost:8817' + c.paperPdfUrl('remote', 'DataCompression', '2025', '2025-TIT-Zhang-Rate'));
  assert.equal(url.pathname, '/api/pdf');
  assert.equal(url.searchParams.get('source'), 'remote');
  assert.equal(url.searchParams.get('topic'), 'DataCompression');
  assert.equal(url.searchParams.get('year'), '2025');
  assert.equal(url.searchParams.get('slug'), '2025-TIT-Zhang-Rate');
  assert.equal(url.searchParams.get('token'), 'tok/123');
});

test('failed token refresh keeps the previous token', async () => {
  const c = vm.createContext({
    state: { pdfToken: 'old' },
    api: async () => { throw new Error('offline'); },
  });
  vm.runInContext(source.slice(source.indexOf('async function loadPdfToken'), source.indexOf('function paperPdfUrl')), c);
  await c.loadPdfToken();
  assert.equal(c.state.pdfToken, 'old');
});

function previewFixture(loadToken = async () => {}) {
  const elements = new Map();
  const c = vm.createContext({
    document: { activeElement: { isConnected: true, focus() { this.restored = true; } }, body: { classList: { add() {}, remove() {} } } },
    $: selector => {
      if (!elements.has(selector)) {
        const classes = new Set(['hidden']);
        elements.set(selector, {
          src: '', style: {}, clientWidth: 600, clientHeight: 400,
          classList: { add: v => classes.add(v), remove: v => classes.delete(v), contains: v => classes.has(v) },
          showModal() { this.open = true; }, close() { this.open = false; }, focus() {},
          setAttribute(name, value) { this[name] = value; }, removeAttribute(name) { delete this[name]; },
        });
      }
      return elements.get(selector);
    },
    loadPdfToken: loadToken,
    renderViewState(target, kind, title, detail, retry) { Object.assign(target, { kind, title, detail, retry }); },
  });
  vm.runInContext(source.slice(source.indexOf('let activePreview'), source.indexOf("$('#image-zoom-in').onclick")), c);
  return c;
}

test('image preview refreshes the token before building its URL and restores focus', async () => {
  let token = 'stale';
  const c = previewFixture(async () => { token = 'fresh'; });
  await c.openImagePreview('标题', () => 'url?token=' + token);
  assert.equal(c.$('#image-modal-title').textContent, '标题');
  assert.equal(c.$('#image-modal-img').src, 'url?token=fresh');
  c.$('#image-modal-img').onload();
  assert.equal(c.$('#image-modal-img').classList.contains('hidden'), false);
  assert.equal(c.$('#image-modal').open, true);
  c.closePreview();
  assert.equal(c.$('#image-modal').open, false);
  assert.equal(c.document.activeElement.restored, true);
});

test('closing image preview during token refresh cancels the pending media load', async () => {
  const pending = deferred();
  const c = previewFixture(() => pending.promise);
  const opening = c.openImagePreview('标题', () => 'obsolete-url');
  c.closePreview();
  pending.resolve();
  await opening;
  assert.equal(c.$('#image-modal-img').src, undefined);
  assert.equal(c.$('#image-modal').open, false);
});

test('PDF previews open a new tab after refreshing the token, titled by slug', async () => {
  const clicks = [];
  let token = 'stale';
  const c = vm.createContext({
    state: { pdfToken: 'stale' },
    loadPdfToken: async () => { token = 'fresh'; },
    encodeURIComponent,
    document: {
      body: { append() {} },
      createElement: () => ({
        style: {},
        click() { clicks.push({ href: this.href, target: this.target, rel: this.rel }); },
        remove() {},
      }),
    },
  });
  vm.runInContext(source.slice(source.indexOf('function pdfViewerUrl'), source.indexOf('let activePreview')), c);
  await c.openPdfTab(() => '/api/pdf?token=' + token, '2026-AAA-Liu-TDBA');
  assert.deepEqual(clicks, [{
    href: '/pdf-view.html?title=2026-AAA-Liu-TDBA&src=' + encodeURIComponent('/api/pdf?token=fresh'),
    target: '_blank',
    rel: 'noopener noreferrer',
  }]);
});

test('image zoom has bounds and fit accounts for viewport padding', () => {
  const c = previewFixture();
  const image = c.$('#image-modal-img');
  image.naturalWidth = 1000;
  image.naturalHeight = 1000;
  image.classList.remove('hidden');
  c.setImageScale(10);
  assert.equal(image.style.width, '4000px');
  c.setImageScale(0.001);
  assert.equal(image.style.width, '100px');
  c.fitImage();
  assert.equal(image.style.width, '360px');
  assert.equal(c.$('#image-fit')['aria-pressed'], 'true');
});

test('breadcrumbs and directory previews retain complete relative paths', () => {
  const c = vm.createContext({});
  vm.runInContext(source.slice(source.indexOf('function projectEntryPath'), source.indexOf('function renderFileNavigation')), c);
  assert.deepEqual(Array.from(c.projectBreadcrumbs('paper/figures/fig1'), x => x.path), ['', 'paper', 'paper/figures', 'paper/figures/fig1']);
  assert.equal(c.projectEntryPath({ name: 'main.pdf' }, 'paper'), 'paper/main.pdf');
  assert.equal(c.projectEntryPath({ path: 'docs/image.png' }, ''), 'docs/image.png');
});

test('view states escape server messages and expose an explicit retry button', () => {
  const button = {};
  const target = { innerHTML: '', querySelector: () => button };
  const c = vm.createContext({});
  vm.runInContext(source.slice(source.indexOf('const esc ='), source.indexOf('const systemTheme')), c);
  vm.runInContext(source.slice(source.indexOf('function renderViewState'), source.indexOf('async function api(')), c);
  let retries = 0;
  c.renderViewState(target, 'error', '读取失败', '<img src=x onerror=alert(1)>', () => retries++);
  assert.match(target.innerHTML, /role="alert"/);
  assert.doesNotMatch(target.innerHTML, /<img/);
  assert.match(target.innerHTML, /&lt;img/);
  button.onclick();
  assert.equal(retries, 1);
});

function filesFixture(api) {
  const elements = new Map();
  const c = vm.createContext({
    state: { project: 'first', projectTypes: new Set(['pdf']), projectFiles: { requestId: 0, mode: 'recent', path: '' } },
    api,
    $: s => { if (!elements.has(s)) elements.set(s, {}); return elements.get(s); },
    renderViewState(target, kind, title, detail, retry) { Object.assign(target, { kind, title, detail, retry }); },
    renderFileNavigation() {}, renderProjectFiles() {},
  });
  vm.runInContext(source.slice(source.indexOf('async function loadProjectFiles'), source.indexOf('function projectEntryPath')), c);
  return c;
}

test('late file listings cannot overwrite a newer directory', async () => {
  const pending = deferred();
  const c = filesFixture(url => url.includes('path=old') ? pending.promise : Promise.resolve({ entries: [{ kind: 'file', name: 'fresh.pdf' }] }));
  const old = c.loadProjectFiles('dir', 'old');
  await c.loadProjectFiles('dir', 'new');
  pending.resolve({ entries: [{ kind: 'file', name: 'stale.pdf' }] });
  await old;
  assert.equal(c.state.projectFiles.path, 'new');
  assert.equal(c.state.projectFiles.files[0].name, 'fresh.pdf');
});

test('file retry keeps the selected directory and types', async () => {
  const urls = [];
  const c = filesFixture(async url => {
    urls.push(url);
    if (urls.length === 1) throw new Error('offline');
    return { entries: [] };
  });
  await c.loadProjectFiles('dir', 'paper/figures');
  assert.equal(c.$('#files-list').kind, 'error');
  await c.$('#files-list').retry();
  assert.equal(urls[0], urls[1]);
  assert.equal(c.state.projectFiles.loaded, true);
});

test('project file URLs carry the token and encode the relative path', () => {
  const c = vm.createContext({ state: { pdfToken: 'tok/123' }, encodeURIComponent });
  vm.runInContext(source.slice(source.indexOf('function projectFileUrl'), source.indexOf('async function loadLibrary()')), c);
  const url = new URL('http://localhost:8817' + c.projectFileUrl('projects/example-project', 'paper/main.pdf'));
  assert.equal(url.pathname, '/api/project/file');
  assert.equal(url.searchParams.get('project'), 'projects/example-project');
  assert.equal(url.searchParams.get('path'), 'paper/main.pdf');
  assert.equal(url.searchParams.get('token'), 'tok/123');
});

test('project type selection persists and rejects unknown types', () => {
  const storage = new Map();
  const saved = value => storage.set('papertex.projectTypes', value);
  const c = vm.createContext({
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
    },
    state: { projectTypes: new Set() },
  });
  vm.runInContext(source.slice(source.indexOf('const PROJECT_TYPES_KEY'), source.indexOf('const state = {')), c);
  assert.deepEqual([...c.loadProjectTypes()], ['pdf', 'png', 'jpg', 'webp', 'gif', 'md']);
  saved('not json');
  assert.deepEqual([...c.loadProjectTypes()], ['pdf', 'png', 'jpg', 'webp', 'gif', 'md']);
  saved(JSON.stringify(['pdf', 'gif', 'tiff', 'md']));
  assert.deepEqual([...c.loadProjectTypes()], ['pdf', 'gif', 'md']);
  saved('[]');
  assert.deepEqual([...c.loadProjectTypes()], []);
  c.state.projectTypes = new Set(['png', 'webp']);
  c.saveProjectTypes();
  assert.deepEqual(JSON.parse(storage.get('papertex.projectTypes')), ['png', 'webp']);
});

test('up navigation removes exactly one path segment', () => {
  const c = vm.createContext({});
  vm.runInContext(source.slice(source.indexOf('function projectUpPath'), source.indexOf('function projectTimeText')), c);
  assert.equal(c.projectUpPath('paper/figures/out'), 'paper/figures');
  assert.equal(c.projectUpPath('paper'), '');
  assert.equal(c.projectUpPath(''), '');
  assert.equal(c.projectUpPath('/paper/figures/'), 'paper');
});

test('long operations display actual download progress and stop all timers after success', async () => {
  const response = deferred();
  const calls = [];
  const { c, timers, target } = progressFixture((url, options) => {
    calls.push({ url, options });
    if (!url.startsWith('/api/operations/')) return response.promise;
    return Promise.resolve({ status: 'running', message: '下载 arXiv 源码', stage_elapsed_seconds: 3,
      details: { host: 'arxiv.org', received_bytes: 1048576, total_bytes: 2097152 } });
  });
  const running = c.runLongOperation('/api/upload', { method: 'POST' }, target);
  await flush();
  assert.match(target.textContent, /下载 arXiv 源码.*arxiv.org.*1.00 MiB \/ 2.00 MiB.*50%.*总耗时/);
  assert.match(calls[0].url, /operation_id=0123456789abcdef0123456789abcdef/);
  assert.equal(calls[1].options.cache, 'no-store');
  response.resolve({ ok: true });
  assert.equal((await running).ok, true);
  assert.equal(timers.size, 0);
  assert.equal(calls[1].options.signal.aborted, true);
});

test('late polling responses cannot overwrite the final operation result', async () => {
  const response = deferred(), poll = deferred();
  const { c, timers, target } = progressFixture(url => url.startsWith('/api/operations/') ? poll.promise : response.promise);
  const running = c.runLongOperation('/api/supplement', { method: 'POST' }, target);
  response.resolve({ ok: true });
  await running;
  target.textContent = '操作完成';
  poll.resolve({ message: 'obsolete phase', status: 'running', details: {}, stage_elapsed_seconds: 10 });
  await flush();
  assert.equal(target.textContent, '操作完成');
  assert.equal(timers.size, 0);
});

test('polling failure is visible without aborting or retrying the write', async () => {
  const response = deferred();
  let writes = 0;
  const { c, timers, target } = progressFixture(url => {
    if (url.startsWith('/api/operations/')) return Promise.reject(new Error('offline'));
    writes++;
    return response.promise;
  });
  const running = c.runLongOperation('/api/upload', { method: 'POST' }, target);
  await flush();
  assert.match(target.textContent, /暂时无法获取进度.*不代表操作已停止/);
  assert.equal(writes, 1);
  response.resolve({ ok: true });
  await running;
  assert.equal(timers.size, 0);
});

test('transport failure warns about uncertain outcome and cleans up polling', async () => {
  const { c, timers, target } = progressFixture(url => url.startsWith('/api/operations/')
    ? Promise.resolve({ message: 'pending', status: 'running', stage_elapsed_seconds: 0, details: {} })
    : Promise.reject(new Error('network error')));
  await assert.rejects(c.runLongOperation('/api/upload', { method: 'POST' }, target), /结果尚未确认.*核对资料.*耗时/);
  assert.equal(timers.size, 0);
});

test('completed polling status still waits for the original request confirmation', async () => {
  const response = deferred();
  const { c, target } = progressFixture(url => url.startsWith('/api/operations/')
    ? Promise.resolve({ message: '操作完成', status: 'completed', details: {} }) : response.promise);
  const running = c.runLongOperation('/api/upload', { method: 'POST' }, target);
  await flush();
  assert.match(target.textContent, /等待请求响应确认/);
  assert.doesNotMatch(target.textContent, /操作完成/);
  response.reject(new Error('network error'));
  await assert.rejects(running, /结果尚未确认/);
});

test('supplement entries disable while pending and reject a second file explicitly', async () => {
  const response = deferred();
  const buttons = [{ disabled: false }, { disabled: false }];
  const alerts = [];
  const item = { key: 'TS/2024/paper', topic: 'TS', year: '2024', slug: 'paper' };
  let writes = 0;
  const c = vm.createContext({
    FormData: class { set() {} },
    state: { current: item, items: [item], metaCache: new Map() },
    $: () => ({ textContent: '' }), $$: () => buttons,
    runLongOperation: () => { writes++; return response.promise; },
    loadLibrary: async () => {}, renderList() {}, showDetail() {},
    toast: (message, kind) => alerts.push({ message, kind }),
  });
  vm.runInContext(source.slice(source.indexOf('const pendingSupplements'), source.indexOf('function editPick(')), c);
  const first = c.supplementFile('pdf', {}, item);
  assert.ok(buttons.every(b => b.disabled));
  assert.equal(await c.supplementFile('source', {}, item), false);
  assert.match(alerts[0].message, /本次文件尚未上传/);
  assert.equal(alerts[0].kind, 'warn');
  assert.equal(writes, 1);
  response.resolve({ ok: true });
  assert.equal(await first, true);
  assert.ok(buttons.every(b => !b.disabled));
});

test('paper metadata is cached and a failed read can be retried', async () => {
  let calls = 0;
  let fail = true;
  const c = vm.createContext({
    state: { metaCache: new Map() },
    api: async () => {
      calls++;
      if (fail) throw new Error('offline');
      return { title: 'cached' };
    },
  });
  vm.runInContext(source.slice(source.indexOf('const metaRequests'), source.indexOf('async function openEditor')), c);
  const it = { key: 'TS/2024/paper', topic: 'TS', year: '2024', slug: 'paper' };
  await assert.rejects(c.paperMeta(it), /offline/);
  const [first, second] = await Promise.all([(fail = false, c.paperMeta(it)), c.paperMeta(it)]);
  assert.equal(calls, 2);
  assert.equal(first, second);
  assert.equal((await c.paperMeta(it)).title, 'cached');
  assert.equal(calls, 2);
});

test('unknown download size does not produce a made-up percentage', () => {
  const { c } = progressFixture(() => {});
  const text = c.operationText({ status: 'running', message: '下载', stage_elapsed_seconds: 1,
    details: { received_bytes: 1000, total_bytes: null } }, 65);
  assert.doesNotMatch(text, /%/);
  assert.match(text, /已接收.*总耗时 1分5秒/);
});

test('rapid saves serialize snapshots and advance the server revision', async () => {
  const first = deferred();
  const writes = [];
  const c = fixture(async (url, options) => {
    if (!options) return document();
    writes.push(JSON.parse(options.body));
    return writes.length === 1 ? first.promise : { revision: 'v3' };
  });
  await c.loadRefs();
  c.state.refs[0].note = 'first';
  const a = c.saveRefs('saved');
  c.state.refs[0].note = 'second';
  const b = c.saveRefs('saved');
  await Promise.resolve();
  assert.equal(writes.length, 1);
  assert.equal(writes[0].references[0].note, 'first');
  first.resolve({ revision: 'v2' });
  await Promise.all([a, b]);
  assert.equal(writes[1].revision, 'v2');
  assert.equal(writes[1].references[0].note, 'second');
  assert.equal(c.state.refsSession.revision, 'v3');
  assert.match(c.$('#refs-result').innerHTML, /saved/);
});

test('conflicts stop queued writes and preserve unsaved input and error', async () => {
  const first = deferred();
  let calls = 0;
  const c = fixture(async (url, options) => {
    if (!options) return document();
    calls++;
    return first.promise;
  });
  await c.loadRefs();
  c.state.refs[0].note = 'first';
  const a = c.saveRefs('saved');
  c.state.refs[0].note = 'second';
  const b = c.saveRefs('saved');
  first.reject(new Error('revision conflict'));
  assert.deepEqual(await Promise.all([a, b]), [null, null]);
  assert.equal(calls, 1);
  assert.equal(c.state.refs[0].note, 'second');
  assert.match(c.$('#refs-result').innerHTML, /revision conflict.*未保存/);
});

test('late loads cannot attach another project data to the selected project', async () => {
  const first = deferred();
  const c = fixture(url => url.endsWith('first') ? first.promise : Promise.resolve({ ...document(), revision: 'second' }));
  const a = c.loadRefs();
  c.state.project = 'second';
  await c.loadRefs();
  first.resolve(document());
  await a;
  assert.equal(c.state.refsSession.project, 'second');
  assert.equal(c.state.refsSession.revision, 'second');
  assert.equal(c.$('#refs-project').textContent, 'second');
});

test('pending save responses cannot overwrite a newly selected project revision', async () => {
  const write = deferred();
  const c = fixture(async (url, options) => options ? write.promise : document());
  await c.loadRefs();
  const saved = c.saveRefs('saved first');
  c.state.project = 'second';
  await c.loadRefs();
  write.resolve({ revision: 'old-project-v2' });
  await saved;
  assert.equal(c.state.refsSession.project, 'second');
  assert.equal(c.state.refsSession.revision, 'v1');
  assert.doesNotMatch(c.$('#refs-result').innerHTML, /saved first/);
});

function maintenanceFixture() {
  const c = vm.createContext({});
  vm.runInContext(source.slice(source.indexOf('const mirrorState'),
    source.indexOf('function syncMirrorSeg')), c);
  vm.runInContext(source.slice(source.indexOf('function dupReasonText'),
    source.indexOf('function renderDup')), c);
  vm.runInContext(source.slice(source.indexOf('const RECOVERY_KIND_TEXT'),
    source.indexOf('function renderRecovery')), c);
  return c;
}

test('mirror panel and duplicate groups default to the safe selection', () => {
  const c = maintenanceFixture();
  const run = code => vm.runInContext(code, c);
  run('mirrorState.plan = { pull: [{ key: "A" }], push: [{ key: "B" }] }');
  run('mirrorState.direction = "pull"');
  assert.equal(run('mirrorItems()[0].key'), 'A');
  run('mirrorState.direction = "push"');
  assert.equal(run('mirrorItems()[0].key'), 'B');
  run('dupResetGroup(0, { items: [{ key: "A" }, { key: "B" }, { key: "C" }] }, "B")');
  assert.equal(run('dupState.keep[0]'), 'B');
  assert.equal(run('[...dupState.drops[0]].sort().join(",")'), 'A,C');
  assert.equal(run('dupReasonText("md5")'), 'pdf 内容相同');
  assert.equal(run('dupReasonText("custom")'), 'custom');
});

test('recovery timestamps are local and tolerate missing values', () => {
  const c = maintenanceFixture();
  const run = code => vm.runInContext(code, c);
  assert.equal(run('recoveryTime(0)'), '');
  assert.equal(run('recoveryTime(null)'), '');
  assert.match(run('recoveryTime(1700000000)'), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  assert.equal(run('RECOVERY_KIND_TEXT.paper'), '论文备份');
});

function settingsFixture() {
  const elements = new Map();
  const element = (selector) => {
    if (!elements.has(selector)) {
      const classes = new Set();
      elements.set(selector, {
        value: '', textContent: '', innerHTML: '', disabled: false,
        classList: {
          add: name => classes.add(name),
          remove: name => classes.delete(name),
          toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
          contains: name => classes.has(name),
        },
      });
    }
    return elements.get(selector);
  };
  const c = vm.createContext({
    state: { settingsHost: '::1' },
    $: element,
    esc: value => String(value ?? '').replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch])),
    api: async () => ({}),
    toast: () => {},
    document: { body: { classList: { add() {}, remove() {} } } },
  });
  vm.runInContext(source.slice(source.indexOf('function settingsPayload'),
    source.indexOf('async function loadPdfToken')), c);
  return { c, element, run: code => vm.runInContext(code, c) };
}

test('settings payload keeps only the selected SSH connection mode', () => {
  const f = settingsFixture();
  f.element('#settings-port').value = '8817';
  f.element('#settings-ssh-port').value = '2200';
  f.element('#settings-local-library').value = '~/Library';
  f.element('#settings-remote-name').value = 'Lab';
  f.element('#settings-remote-library').value = '/srv/lib';
  f.element('#settings-remote-workshop').value = '/srv/proj';

  f.element('#settings-connection').value = 'direct';
  f.element('#settings-ssh-host').value = '192.0.2.10';
  f.element('#settings-ssh-user').value = 'me';
  f.element('#settings-ssh-key').value = '~/.ssh/id_ed25519';
  f.element('#settings-ssh-alias').value = 'stale';
  let payload = f.run('settingsPayload()');
  assert.equal(payload.host, '::1');
  assert.equal(payload.ssh_host, '192.0.2.10');
  assert.equal(payload.ssh_user, 'me');
  assert.equal(payload.ssh_alias, '');
  assert.equal(payload.ssh_port, 2200);

  f.element('#settings-connection').value = 'alias';
  f.element('#settings-ssh-alias').value = 'lab';
  payload = f.run('settingsPayload()');
  assert.equal(payload.ssh_alias, 'lab');
  assert.equal(payload.ssh_host, '');
  assert.equal(payload.ssh_user, '');
  assert.equal(payload.ssh_key, '');
});

test('settings connection switch toggles the relevant field group', () => {
  const f = settingsFixture();
  f.element('#settings-connection').value = 'alias';
  f.run('syncSettingsConnection()');
  assert.ok(f.element('#settings-alias').classList.contains('hidden') === false);
  assert.ok(f.element('#settings-direct').classList.contains('hidden'));
  f.element('#settings-connection').value = 'direct';
  f.run('syncSettingsConnection()');
  assert.ok(f.element('#settings-direct').classList.contains('hidden') === false);
  assert.ok(f.element('#settings-alias').classList.contains('hidden'));
});

test('settings checks render pass and failure rows without markup injection', () => {
  const f = settingsFixture();
  f.run(`renderSettingsChecks({ checks: [
    { name: 'SSH 连接', ok: true, detail: 'lab' },
    { name: '<img>', ok: false, detail: '/srv/<x>' },
  ] })`);
  const html = f.element('#settings-checks').innerHTML;
  assert.match(html, /settings-check ok/);
  assert.match(html, /settings-check fail/);
  assert.match(html, /&lt;img&gt;/);
  assert.doesNotMatch(html, /<img>/);
});

function pdfViewFixture(search) {
  const page = fs.readFileSync(path.join(__dirname, '../app/static/pdf-view.html'), 'utf8');
  const script = page.slice(page.indexOf('<script>') + 8, page.indexOf('</script>'));
  const frame = { src: '', title: '', removed: false, remove() { this.removed = true; } };
  const appended = [];
  const c = vm.createContext({
    URLSearchParams,
    location: { search },
    document: {
      title: '',
      body: { append(el) { appended.push(el.textContent); } },
      createElement: () => ({ textContent: '' }),
      getElementById: id => (id === 'pdf-frame' ? frame : null),
    },
  });
  vm.runInContext(script, c);
  return { c, frame, appended };
}

test('pdf viewer page titles the tab and only embeds internal PDF endpoints', () => {
  const ok = pdfViewFixture('?title=2026-AAA-Liu-TDBA&src=' + encodeURIComponent('/api/pdf?token=t'));
  assert.equal(ok.c.document.title, '2026-AAA-Liu-TDBA');
  assert.equal(ok.frame.src, '/api/pdf?token=t');
  assert.equal(ok.frame.removed, false);

  const project = pdfViewFixture('?title=main.pdf&src=' + encodeURIComponent('/api/project/file?path=paper/main.pdf'));
  assert.equal(project.frame.src, '/api/project/file?path=paper/main.pdf');

  const bad = pdfViewFixture('?title=x&src=' + encodeURIComponent('https://example.com/a.pdf'));
  assert.equal(bad.frame.removed, true);
  assert.deepEqual(bad.appended, ['无效的预览地址']);
});

const reviewSlice = source.slice(source.indexOf('const reviewState'),
  source.indexOf('/* ---------- 审稿空间结束 ---------- */'));

function reviewFixture() {
  const elements = new Map();
  const element = (name) => {
    const key = String(name).startsWith('#') ? String(name) : '#' + String(name);
    if (!elements.has(key)) {
      const classes = new Set();
      const children = [];
      elements.set(key, {
        value: '', textContent: '', innerHTML: '', disabled: false, open: false,
        dataset: {}, files: [], children,
        classList: {
          add: name2 => classes.add(name2),
          remove: name2 => classes.delete(name2),
          toggle: (name2, on) => (on ? classes.add(name2) : classes.delete(name2)),
          contains: name2 => classes.has(name2),
        },
        click() { children.push('click'); },
        focus() {}, select() { children.push('select'); },
        addEventListener(type, handler) { children.push(type + ':' + (handler ? 'handler' : 'none')); },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        showModal() { this.open = true; },
        close() { this.open = false; },
      });
    }
    return elements.get(key);
  };
  const calls = [];
  const stub = { result: { revision: 'rev-2' }, error: null };
  const c = vm.createContext({
    state: { pdfToken: 'tok' },
    $: selector => element(selector),
    $$: () => [],
    document: {
      getElementById: id => element(id),
      querySelector: selector => element(selector),
      querySelectorAll: () => [],
      createElement: () => ({ style: {}, click() {}, remove() {} }),
      body: { classList: { add() {}, remove() {} }, append() {} },
      activeElement: null,
    },
    esc: value => String(value ?? '').replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch])),
    byteText: bytes => bytes + ' B',
    renderViewState: () => {},
    api: async (path, options) => {
      calls.push({ path, options });
      if (stub.error) throw stub.error;
      return stub.result;
    },
    toast: () => {},
    confirmDialog: async () => true,
    openPdfTab: async () => {},
    FormData: function FormData() { this.appended = []; this.append = (k, v) => this.appended.push([k, v]); },
    URL: { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} },
    Blob: function Blob(parts) { this.parts = parts; },
    setTimeout, clearTimeout,
  });
  vm.runInContext(reviewSlice, c);
  return { c, element, calls, stub, run: code => vm.runInContext(code, c) };
}

test('review deadlines distinguish overdue, due today and unset dates', () => {
  const f = reviewFixture();
  assert.equal(f.run('reviewDeadlineText({ due: "" })'), '未设截止日期');
  assert.equal(f.run('reviewDeadlineText({ due: "2026-11-20" })'), '截止 2026-11-20');
  assert.match(f.run('reviewDeadlineText({ due: "2026-11-01", days_left: -3, overdue: true })'),
    /已逾期 3 天/);
  assert.equal(f.run('reviewDeadlineText({ due: "2026-11-03", days_left: 0, overdue: false })'),
    '今天 2026-11-03 截止');
  assert.equal(f.run('reviewDeadlineText({ due: "2026-11-20", days_left: 17, overdue: false })'),
    '剩 17 天 · 2026-11-20');
});

test('review rows escape stored text and flag the active task', () => {
  const f = reviewFixture();
  f.run('reviewState.key = "ICML/1"');
  const html = f.run(`reviewRowHtml({ key: 'ICML/1', venue: 'ICML', submission: '1',
    status_label: '撰写中', title: '<img src=x>', due: '2026-12-01', days_left: 5,
    overdue: false, has_manuscript: false })`);
  assert.match(html, /class="review-row active"/);
  assert.match(html, /data-review-key="ICML\/1"/);
  assert.match(html, /&lt;img src=x&gt;/);
  assert.doesNotMatch(html, /<img>/);
  assert.match(html, /badge warn">无稿件/);
  const other = f.run(`reviewRowHtml({ key: 'ICML/2', venue: 'ICML', submission: '2',
    status_label: '待读', title: '', due: '', overdue: false, has_manuscript: true })`);
  assert.doesNotMatch(other, /active/);
  assert.match(other, /未填稿件标题/);
});

test('review detail renders escaped metadata form and gates file actions', () => {
  const f = reviewFixture();
  f.run('reviewState.statuses = [{ value: "pending", label: "待读" }, { value: "writing", label: "撰写中" }]');
  const data = {
    task: { venue: 'ICML', submission: '1', key: 'ICML/1', title: '<b>T</b>', venue_label: '',
      round: 'R1', due: '2026-12-01', status: 'writing', rating: '4', confidence: '', note: '',
      days_left: 5, overdue: false, purged_at: '' },
    review: '正文 <script>', revision: 'rev-1',
    files: [{ name: 'manuscript.pdf', size: 2048 }, { name: 'supplement/附录.txt', size: 10 }],
  };
  f.run('reviewState.revision = "rev-1"');
  const html = f.run(`reviewDetailHtml(${JSON.stringify(data)})`);
  assert.match(html, /&lt;b&gt;T&lt;\/b&gt;/);
  assert.doesNotMatch(html, /<b>T<\/b>/);
  assert.match(html, /<option value="writing" selected>撰写中<\/option>/);
  assert.match(html, /id="rv-editor"[^>]*>正文 &lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /id="rv-view-seg"/);
  assert.match(html, /id="rv-preview" class="markdown-body review-preview hidden"/);
  assert.match(html, /替换稿件/);
  assert.match(html, /data-review-open="manuscript\.pdf"/);
  assert.doesNotMatch(html, /data-review-open="supplement/);
  assert.match(html, /2048 B/);

  const empty = f.run(`reviewDetailHtml({ task: ${JSON.stringify(data.task)},
    review: '', revision: 'rev-1', files: [] })`);
  assert.match(empty, /上传稿件/);
  assert.match(empty, /id="rv-open" class="mini ghost" disabled/);
  assert.match(empty, /id="rv-purge" class="mini ghost" disabled/);
});

test('review export mirrors the server header block and drops empty fields', () => {
  const f = reviewFixture();
  const text = f.run(`reviewExportText({ venue: 'ICML', submission: '7', title: 'P',
    round: '', due: '2026-12-01', rating: '4', confidence: '' }, '  ## Summary\\n正文  ')`);
  assert.equal(text, '# ICML / 7\n稿件：P\n截止：2026-12-01\n评分：4\n\n## Summary\n正文\n');
  assert.equal(f.run(`reviewExportText({ venue: 'a', submission: '1', title: '', round: '',
    due: '', rating: '', confidence: '' }, '')`), '# a / 1\n');
});

test('review autosave sends the observed revision and pauses on conflict', async () => {
  const f = reviewFixture();
  f.run('reviewState.key = "ICML/1"; reviewState.revision = "rev-1"; reviewState.review = "旧"');
  f.element('#rv-editor').value = '新评审内容';
  await f.run('saveReviewBody()');
  assert.equal(f.calls[0].path, '/api/review/task');
  const body = JSON.parse(f.calls[0].options.body);
  assert.equal(body.key, 'ICML/1');
  assert.equal(body.review, '新评审内容');
  assert.equal(body.expected, 'rev-1');
  assert.equal(body.fields, undefined);
  assert.equal(f.run('reviewState.revision'), 'rev-2');
  assert.equal(f.run('reviewState.conflict'), false);

  f.element('#rv-editor').value = '再改一次';
  f.stub.error = { status: 409, message: 'conflict' };
  await f.run('saveReviewBody()');
  assert.equal(f.run('reviewState.conflict'), true);
  assert.equal(f.element('#rv-save').textContent, '保存冲突');
  const before = f.calls.length;
  f.element('#rv-editor').value = '不应继续发送';
  await f.run('saveReviewBody()');
  assert.equal(f.calls.length, before);
});

test('review field saves keep the editor content untouched', async () => {
  const f = reviewFixture();
  f.run('reviewState.key = "ICML/1"');
  f.element('#rvf-title').value = '  改后的标题  ';
  f.element('#rvf-due').value = '2026-12-05';
  f.element('#rvf-status').value = 'submitted';
  f.run('reviewState.items = []; reviewState.loaded = true; reviewState.detailLoaded = true');
  await f.run('saveReviewFields()');
  const body = JSON.parse(f.calls[0].options.body);
  assert.equal(f.calls[0].path, '/api/review/task');
  assert.deepEqual(body, { key: 'ICML/1', fields: {
    title: '改后的标题', venue_label: '', round: '', due: '2026-12-05', status: 'submitted',
    rating: '', confidence: '', note: '' } });
  assert.equal(body.review, undefined);
});

test('review field changes flush unsaved editor text before saving metadata', async () => {
  const f = reviewFixture();
  f.run('reviewState.key = "ICML/1"; reviewState.revision = "rev-1"; reviewState.review = "旧"');
  f.element('#rv-editor').value = '还没落盘的评审意见';
  f.run('queueReviewSave()');
  assert.equal(f.calls.length, 0);
  f.element('#rvf-due').value = '2026-12-05';
  f.run('reviewState.items = []; reviewState.loaded = true; reviewState.detailLoaded = true');
  await f.run('saveReviewFields()');
  assert.deepEqual(f.calls.map(call => call.path),
                   ['/api/review/task', '/api/review/task', '/api/review']);
  const first = JSON.parse(f.calls[0].options.body);
  assert.equal(first.review, '还没落盘的评审意见');
  assert.equal(first.expected, 'rev-1');
  assert.equal(first.fields, undefined);
  const second = JSON.parse(f.calls[1].options.body);
  assert.equal(second.review, undefined);
  assert.deepEqual(second.fields, {
    title: '', venue_label: '', round: '', due: '2026-12-05', status: '',
    rating: '', confidence: '', note: '' });
  assert.equal(f.run('reviewState.dirtyBody'), false);

  f.calls.length = 0;
  f.run('reviewState.key = "ICML/1"; reviewState.review = "旧内容"; queueReviewSave();');
  f.element('#rv-editor').value = '切换任务前未保存的意见';
  await f.run('selectReview("ICML/2")');
  const flushed = JSON.parse(f.calls[0].options.body);
  assert.equal(f.calls[0].path, '/api/review/task');
  assert.equal(flushed.key, 'ICML/1');
  assert.equal(flushed.review, '切换任务前未保存的意见');
});

test('review manuscript upload uses the local endpoint and overwrite only when a file exists', async () => {
  const f = reviewFixture();
  const input = f.element('#review-pdf-input');
  input.files = [{ name: 'paper.pdf' }];
  input.value = 'C:\\fakepath\\paper.pdf';
  f.c.input = input;
  f.run("reviewState.key = 'ICML/1'; reviewState.files = []; uploadReviewManuscript({ target: input });");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.calls[0].path, '/api/review/manuscript');
  const form = f.calls[0].options.body;
  assert.deepEqual(form.appended[0], ['key', 'ICML/1']);
  assert.deepEqual(form.appended[1], ['overwrite', 'false']);
  assert.deepEqual(form.appended[2], ['pdf', input.files[0]]);
  assert.equal(input.value, '');

  f.calls.length = 0;
  f.run('reviewState.key = "ICML/1"; reviewState.files = [{ name: "manuscript.pdf", size: 1 }]; uploadReviewManuscript({ target: input });');
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(f.calls[0].options.body.appended[1], ['overwrite', 'true']);

  f.calls.length = 0;
  f.run('reviewState.files = []; uploadReviewManuscript({ target: { files: [], value: "" } });');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.calls.length, 0);
});

test('review review root path is fenced inside the local review directory', () => {
  const f = reviewFixture();
  assert.equal(f.run('reviewManuscriptUrl("ICML/1", "manuscript.pdf")'),
    '/api/review/pdf?key=ICML%2F1&name=manuscript.pdf&token=tok');
});

test('review tab, splitter and dialogs are wired in the page shell', () => {
  const html = fs.readFileSync(path.join(__dirname, '../app/static/index.html'), 'utf8');
  assert.match(html, /<button data-tab="review">审稿<\/button>/);
  assert.ok(html.indexOf('id="tab-refs"') < html.indexOf('id="tab-review"'));
  assert.match(html, /id="review-layout"[^>]*>[\s\S]*?id="review-splitter"/);
  for (const id of ['review-list', 'review-detail', 'review-new', 'review-trash-btn',
                    'review-create-modal', 'review-trash-modal', 'review-export-modal',
                    'review-pdf-input', 'settings-review-space']) {
    assert.ok(html.includes(`id="${id}"`), id);
  }
  assert.match(html, /id="review-pdf-input"[^>]*class="hidden"/);
});

const splitterSlice = source.slice(
  source.indexOf('/* ---------- 列表 / 详情分隔条 ---------- */'),
  source.indexOf('/* ---------- 审稿空间 ---------- */'));

function splitterFixture(widths, innerWidth = 0) {
  const setVars = [];
  const attrs = {};
  const layoutEls = widths.map(clientWidth => ({ clientWidth }));
  const c = vm.createContext({
    $: selector => selector === '#library-layout' ? layoutEls[0] : null,
    $$: selector => selector === '.layout' ? layoutEls : [{
      setAttribute: (name, value) => { attrs[name] = String(value); },
      addEventListener() {},
    }],
    document: {
      documentElement: { style: { setProperty: (name, value) => setVars.push([name, value]) } },
    },
    window: { innerWidth, addEventListener() {} },
    localStorage: { getItem: () => null, setItem() {} },
  });
  vm.runInContext(splitterSlice, c);
  return { run: code => vm.runInContext(code, c), setVars, attrs };
}

test('splitter clamps the list width against the visible workspace', () => {
  // 审稿页激活时资料库工作区的 clientWidth 为 0，上限只能量当前可见的那个布局
  const f = splitterFixture([0, 1100]);
  assert.equal(f.run('splitterMax()'), 808);
  f.run('applyListWidth(3000)');
  assert.deepEqual(f.setVars[f.setVars.length - 1], ['--list-width', '808px']);
  assert.equal(f.attrs['aria-valuemax'], '808');
  f.run('applyListWidth(100)');
  assert.deepEqual(f.setVars[f.setVars.length - 1], ['--list-width', '240px']);

  const narrow = splitterFixture([400, 0]);
  assert.equal(narrow.run('splitterMax()'), 240, 'never below the minimum width');

  const collapsed = splitterFixture([0, 0], 1000);
  assert.equal(collapsed.run('splitterMax()'), 644, 'falls back to the window width');
});

test('paper note dialog is present in the page shell and wired into the detail pane', () => {
  const html = fs.readFileSync(path.join(__dirname, '../app/static/index.html'), 'utf8');
  for (const id of ['note-modal', 'note-modal-title', 'note-modal-meta', 'note-form', 'note-text',
                    'note-preview', 'note-status', 'note-save', 'note-delete', 'note-cancel',
                    'note-modal-close']) {
    assert.ok(html.includes(`id="${id}"`), id);
  }
  assert.match(html, /id="note-text"[^>]*maxlength="200000"/);
  assert.match(html, /id="note-modal"[^>]*class="modal hidden"/);
  assert.match(source, /\$\{paperNoteHtml\(it\)\}/);
  assert.match(source, /\/api\/paper\/note\/open' \+ noteQueryParams\(it\)/);
  assert.match(source, /openBtn\.disabled = !note\.hasNote/);
  assert.match(source, /openBtn\.onclick = async/);
  assert.match(source, /bindPaperNote\(it\);\n\}/);
});

const noteSlice = source.slice(
  source.indexOf('function noteQueryParams'),
  source.indexOf('function closeNoteEditor'));

test('paper note helpers build escaped markup and encode coordinates', () => {
  const c = vm.createContext({
    esc: text => String(text ?? '').replace(/[&<>"']/g, ch => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch])),
    PapertexMarkdown: { render: (text, opts) => `<md>${text}</md>` },
  });
  vm.runInContext(noteSlice, c);
  const run = code => vm.runInContext(code, c);
  assert.equal(
    run('noteQuery({ topic: "ML", year: "2026", slug: "a b" })'),
    '/api/paper/note?topic=ML&year=2026&slug=a%20b');
  assert.equal(
    run("'/api/paper/note/open' + noteQueryParams({ topic: 'ML', year: '2026', slug: 'a b' })"),
    '/api/paper/note/open?topic=ML&year=2026&slug=a%20b');
  const panel = run('paperNoteHtml({ slug: "<x>" })');
  assert.match(panel, /id="paper-note-panel"/);
  assert.match(panel, /&lt;x&gt;\/note\.md/);
  assert.doesNotMatch(panel, /<x>/);
  assert.match(panel, /id="panel-note-open"/);
  assert.match(panel, /用本地软件打开/);
  assert.match(run('notePreviewHtml("")'), /左侧输入 Markdown/);
  assert.equal(run('notePreviewHtml("# hi")'), '<md># hi</md>');
});

function selectItem() {
  return {
    key: 'TS/2024/x', slug: '2024-TIT-Li-x',
    local: { has_pdf: true, pdf: 'paper.pdf', title: '', venue: '' },
    remote: {
      has_pdf: true, pdf: 'paper.pdf', has_source: false, entry: '',
      title: 'A study', venue: 'TIT',
    },
  };
}

test('select mode adds a labelled pick box and marks chosen rows', () => {
  const c = listItemFixture();
  assert.doesNotMatch(c.itemHtml(selectItem()), /class="pick"/);
  c.state.selectMode = true;
  c.state.selected = new Set(['TS/2024/x']);
  const html = c.itemHtml(selectItem());
  assert.match(html, /class="item\s+picked"/);
  assert.match(html, /class="pick" data-pick="TS\/2024\/x" checked aria-label="选择 2024-TIT-Li-x"/);
  const other = c.itemHtml({ ...selectItem(), key: 'TS/2024/y', slug: '2024-TIT-Li-y' });
  assert.match(other, /data-pick="TS\/2024\/y"/);
  assert.doesNotMatch(other, /data-pick="TS\/2024\/y" checked/);
});

test('select mode marks knowledge rows and escapes pick attributes', () => {
  const c = listItemFixture();
  c.state.selectMode = true;
  c.state.selected = new Set(['_knowledge/abc']);
  const html = c.knowledgeItemHtml({
    key: '_knowledge/abc', kind: 'note', title: 'A <b> note', source: 'note', tags: [],
  });
  assert.match(html, /class="item knowledge\s+picked"/);
  assert.match(html, /data-pick="_knowledge\/abc" checked aria-label="选择 A &lt;b&gt; note"/);
  assert.doesNotMatch(html, /<b> note/);
  const escaped = c.pickBoxHtml('a"b<', '"q"');
  assert.match(escaped, /data-pick="a&quot;b&lt;"/);
  assert.match(escaped, /aria-label="选择 &quot;q&quot;"/);
});

test('bulk summary reports added, updated, skipped and the first failure', () => {
  const c = listItemFixture();
  assert.equal(c.bulkSummaryText({ added: ['a'], skipped: ['b'] }),
    '已加入 1 项，跳过 1 项（项目里已有）');
  assert.equal(c.bulkSummaryText({ updated: ['a'] }), '已更新 1 项');
  assert.equal(c.bulkSummaryText({}), '没有需要加入的内容');
  assert.match(c.bulkSummaryText({ added: ['a'], failed: [{ path: 'TS/2024/m', message: '库中没有这篇论文' }] }),
    /已加入 1 项，失败 1 项（TS\/2024\/m：库中没有这篇论文）/);
  assert.match(c.bulkSummaryText({ failed: [{ path: 'x' }] }), /失败 1 项（x）/);
});

function bulkFixture({ responses = [], confirm = false } = {}) {
  const elements = new Map();
  const element = () => ({
    textContent: '', innerHTML: '', value: '', disabled: false, open: false,
    classList: {
      set: new Set(),
      add(name) { this.set.add(name); },
      remove(name) { this.set.delete(name); },
      toggle(name, on) {
        if (on === undefined ? !this.set.has(name) : on) this.set.add(name);
        else this.set.delete(name);
      },
      contains(name) { return this.set.has(name); },
    },
    showModal() { this.open = true; },
    close() { this.open = false; },
  });
  const $ = selector => {
    if (!elements.has(selector)) elements.set(selector, element());
    return elements.get(selector);
  };
  const calls = [], toasts = [], closes = [], modes = [];
  const c = vm.createContext({
    state: { project: 'demo', projectSource: 'remote',
             selected: new Set(['TS/2024/a', 'TS/2024/b']) },
    $,
    runLongOperation: (path, options, target) => {
      calls.push({ path, options, target });
      const next = responses.shift();
      if (next instanceof Error) return Promise.reject(next);
      return Promise.resolve(next || { added: ['TS/2024/a'] });
    },
    bulkSummaryText: result => JSON.stringify(result),
    loadProjectChoices: async () => ({
      choices: [{ source: 'remote', project: 'demo' }],
      html: '<option value="0">demo</option>', failures: 0,
    }),
    confirmDialog: async () => confirm,
    toast: (message, kind) => toasts.push({ message, kind }),
    invalidateProjects() {}, touchProject() {}, loadRefs() {},
    setSelectMode: on => modes.push(on),
    closeBulkModal: () => closes.push(true),
    esc: text => String(text ?? ''),
  });
  vm.runInContext(source.slice(source.indexOf('let bulkChoices'),
    source.indexOf('async function showDetail')), c);
  return { c, $, calls, toasts, closes, modes, run: code => vm.runInContext(code, c) };
}

test('bulk add posts one batch request and clears the selection', async () => {
  const f = bulkFixture({ responses: [{ added: ['TS/2024/a'], skipped: ['TS/2024/b'], failed: [] }] });
  f.run('bulkChoices = [{ source: "remote", project: "demo" }]');
  f.$('#bulk-project').value = '0';
  await f.run('submitBulkAdd()');
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].path, '/api/refs/add-batch');
  assert.deepEqual(JSON.parse(f.calls[0].options.body), {
    project: 'demo', source: 'remote', paths: ['TS/2024/a', 'TS/2024/b'] });
  assert.equal(f.c.state.selected.size, 0);
  assert.deepEqual(f.modes, [false]);
  assert.ok(f.$('#bulk-modal').classList.contains('hidden'), 'dialog is closed after the batch');
  assert.deepEqual(f.toasts.map(t => t.kind), ['ok']);
});

test('bulk add confirms before creating a missing remote project directory', async () => {
  const missing = new Error('远端上还没有项目目录');
  missing.status = 409;
  missing.detail = { missing_dir: true, project: 'demo' };
  const f = bulkFixture({ responses: [missing, { added: ['TS/2024/a'], skipped: [], failed: [], created_dir: true }],
    confirm: true });
  f.run('bulkChoices = [{ source: "remote", project: "demo" }]');
  f.$('#bulk-project').value = '0';
  await f.run('submitBulkAdd()');
  assert.equal(f.calls.length, 2);
  assert.equal(JSON.parse(f.calls[1].options.body).create_missing, true);
  assert.equal(f.c.state.selected.size, 0);
});

test('cancelling the missing directory prompt keeps the selection and the dialog open', async () => {
  const missing = new Error('远端上还没有项目目录');
  missing.status = 409;
  missing.detail = { missing_dir: true, project: 'demo' };
  const f = bulkFixture({ responses: [missing], confirm: false });
  f.run('bulkChoices = [{ source: "remote", project: "demo" }]');
  f.$('#bulk-project').value = '0';
  await f.run('submitBulkAdd()');
  assert.equal(f.calls.length, 1);
  assert.match(f.$('#bulk-status').textContent, /已取消，未创建目录/);
  assert.equal(f.$('#bulk-ok').disabled, false);
  assert.equal(f.c.state.selected.size, 2);
  assert.ok(!f.$('#bulk-modal').classList.contains('hidden'), 'dialog stays open for a retry');
});

test('bulk dialog disables confirm when no project exists and enables it otherwise', async () => {
  const f = bulkFixture();
  f.run('state.selected = new Set(["TS/2024/a"])');
  await f.run('openBulkProjectModal()');
  assert.equal(f.$('#bulk-modal').open, true);
  assert.match(f.$('#bulk-modal-hint').textContent, /已选的 1 项/);
  assert.equal(f.$('#bulk-ok').disabled, false);
  assert.match(f.$('#bulk-project').innerHTML, /<option value="0">demo<\/option>/);
  f.c.loadProjectChoices = async () => ({ choices: [], html: '<option value="">选择项目…</option>', failures: 0 });
  await f.run('openBulkProjectModal()');
  assert.equal(f.$('#bulk-status').textContent, '还没有项目，先到「项目引用」添加');
  assert.equal(f.$('#bulk-ok').disabled, true);
});

test('multi-select toolbar, bulk bar and dialog are wired in the page shell', () => {
  const html = fs.readFileSync(path.join(__dirname, '../app/static/index.html'), 'utf8');
  assert.match(html, /id="select-toggle"[^>]*aria-pressed="false"/);
  assert.match(html, /id="bulk-bar"[^>]*class="bulk-bar hidden"/);
  for (const id of ['bulk-count', 'bulk-all', 'bulk-none', 'bulk-add-project', 'bulk-exit',
                    'bulk-modal', 'bulk-modal-hint', 'bulk-project', 'bulk-status',
                    'bulk-ok', 'bulk-cancel']) {
    assert.ok(html.includes(`id="${id}"`), id);
  }
  assert.match(source, /'\/api\/refs\/add-batch'/);
  assert.match(source, /\$\('#bulk-ok'\)\.onclick = submitBulkAdd/);
});

test('group selection toggles every key in the group and reports the new state', () => {
  const c = listItemFixture();
  assert.equal(c.applyGroupSelection(['TS/2024/a', 'TS/2024/b']), true);
  assert.deepEqual([...c.state.selected].sort(), ['TS/2024/a', 'TS/2024/b']);
  assert.equal(c.applyGroupSelection(['TS/2024/a']), false);
  assert.deepEqual([...c.state.selected].sort(), ['TS/2024/b']);
  assert.equal(c.applyGroupSelection(['TS/2023/c']), true);
  assert.deepEqual([...c.state.selected].sort(), ['TS/2023/c', 'TS/2024/b']);
  assert.equal(c.applyGroupSelection([]), true, 'an empty group reports no change to selected-all');
});

test('group pick boxes carry escaped topic and year keys', () => {
  const c = listItemFixture();
  assert.match(c.groupPickHtml('topic', 'TS/2024', '选择主题 TS/2024'),
    /class="group-pick" data-pick-topic="TS\/2024" aria-label="选择主题 TS\/2024" title="选择整个主题"/);
  const escaped = c.groupPickHtml('year', 'a"b', '选择 a"b 2024 年');
  assert.match(escaped, /data-pick-year="a&quot;b"/);
  assert.match(escaped, /aria-label="选择 a&quot;b 2024 年"/);
  assert.match(escaped, /title="选择整个年份"/);
});

test('topic and year headers expose group pick boxes in select mode only', () => {
  assert.match(source, /\$\{state\.selectMode \? groupPickHtml\('topic', topic, `选择主题 \$\{topic\}`\) : ''\}/);
  assert.match(source, /\$\{state\.selectMode \? groupPickHtml\('year', yKey, `选择 \$\{topic\} \$\{year\} 年`\) : ''\}/);
  assert.match(source, /\$\$\('#list \.group-pick'\)\.forEach/);
  assert.match(source, /box\.indeterminate = chosen > 0 && chosen < keys\.length/);
});
