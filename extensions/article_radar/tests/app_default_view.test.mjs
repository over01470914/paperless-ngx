import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

// Model the browser rule that exposed the bug: option.value falls back to text.
class Element {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.className = '';
    this._text = '';
    this._valueAttribute = undefined;
    this.attributes = new Map();
    this.listeners = new Map();
    this.checked = false;
    this.href = '';
    this.target = '';
    this.rel = '';
  }
  get textContent() {
    return this._text + this.children.map(child => child.textContent).join('');
  }
  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }
  get value() {
    if (this.tagName === 'OPTION') {
      return this._valueAttribute === undefined ? this.textContent : this._valueAttribute;
    }
    if (this.tagName === 'SELECT') {
      return this._valueAttribute === undefined ? (this.children[0]?.value ?? '') : this._valueAttribute;
    }
    return this._valueAttribute ?? '';
  }
  set value(value) { this._valueAttribute = String(value); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = [...children]; this._valueAttribute = undefined; }
  addEventListener(type, listener) { this.listeners.set(type, listener); }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === 'value') this.value = value;
  }
  showModal() { this.open = true; }
  close() { this.open = false; }
}

const ids = ['search', 'category', 'freshness', 'actionable', 'map-view', 'list-view',
  'close-detail', 'results', 'status', 'count', 'detail', 'detail-title', 'detail-body'];
const elements = new Map(ids.map(id => [id, new Element(
  id === 'category' || id === 'freshness' ? 'select' : id === 'detail' ? 'dialog' : 'div',
)]));
const initialAll = new Element('option');
initialAll.textContent = '所有類別';
initialAll.value = '';
elements.get('category').append(initialAll);
const initialFreshness = new Element('option');
initialFreshness.textContent = '所有時效';
initialFreshness.value = '';
elements.get('freshness').append(initialFreshness);

const document = {
  getElementById(id) {
    assert.ok(elements.has(id), `unexpected DOM id: ${id}`);
    return elements.get(id);
  },
  createElement(tag) { return new Element(tag); },
};

const fieldIds = { wx_source_id: 1, wx_source_url: 2, wx_category: 3,
  wx_summary: 4, wx_actionable: 5, wx_shelf_life: 6, wx_publish_date: 7 };
function article(id, category) {
  const values = {
    wx_source_id: `synthetic-${id}`,
    wx_source_url: `https://example.invalid/synthetic-${id}`,
    wx_category: category,
    wx_summary: `Synthetic summary ${id}`,
    wx_actionable: 'high',
    wx_shelf_life: 'evergreen',
    wx_publish_date: '2026-01-01',
  };
  return { id, title: `Synthetic title ${id}`, correspondent: 10,
    custom_fields: Object.entries(values).map(([name, value]) => ({ field: fieldIds[name], value })) };
}

const firstDocsPath = '/api/documents/?page_size=100&fields=id,title,correspondent,custom_fields';
const secondDocsPath = '/api/documents/?page=2';
const responses = new Map([
  ['/api/custom_fields/?page_size=100', { results: Object.entries(fieldIds).map(([name, id]) => ({ id, name })), next: null }],
  ['/api/correspondents/?page_size=100', { results: [{ id: 10, name: 'Synthetic account' }], next: null }],
  [firstDocsPath, { results: [article(1, 'category-a'), article(2, 'category-b')],
    next: `http://127.0.0.1:4386${secondDocsPath}` }],
  [secondDocsPath, { results: [article(3, 'category-c')], next: null }],
]);
const calls = [];
async function fetch(path) {
  calls.push(path);
  assert.ok(responses.has(path), `unexpected API path: ${path}`);
  return { ok: true, status: 200, redirected: false,
    headers: { get: name => name.toLowerCase() === 'content-type' ? 'application/json' : null },
    json: async () => responses.get(path) };
}

function articleNodes(root) {
  return (root.className === 'article' ? 1 : 0) + root.children.reduce((count, child) => count + articleNodes(child), 0);
}

async function main() {
  const appPath = process.env.RADAR_APP_JS || fileURLToPath(new URL('../static/app.js', import.meta.url));
  vm.runInNewContext(readFileSync(appPath, 'utf8'), {
    document, fetch, location: { origin: 'http://radar.invalid:4387' }, URL,
  }, { filename: appPath });
  for (let attempt = 0; attempt < 100 && !/\d+ \/ 3 篇/.test(elements.get('count').textContent); attempt++) {
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.ok(calls.includes(secondDocsPath), 'the second native document page was not fetched');
  const firstOption = elements.get('category').children[0];
  assert.equal(firstOption.value, '', `default category option.value = ${JSON.stringify(firstOption.value)} (expected "")`);
  assert.equal(elements.get('count').textContent, '3 / 3 篇', 'default category filter excluded articles');
  assert.equal(articleNodes(elements.get('results')), 3, 'default map did not render all articles');
  console.log('PASS app_default_view: option.value=""; 3 / 3 篇; 3 article nodes; 2 document pages');
}

main().catch(error => {
  console.error(`FAIL app_default_view: ${error.message}`);
  process.exitCode = 1;
});
