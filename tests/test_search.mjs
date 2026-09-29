import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function harness() {
  const elements = new Map();
  const node = (tag = '', props = {}, ...children) => ({
    tag, ...props, children: children.flat(), handlers: {}, dataset: {},
    addEventListener(name, fn) { this.handlers[name] = fn; },
    append(...items) { this.children.push(...items.flat()); },
    querySelector() { return this.tag === 'group' ? this : null; },
    blur() {}, focus() {},
  });
  const timers = new Map();
  let timer = 0;
  const posts = [];
  const libraries = [];
  const context = vm.createContext({
    AbortController, URLSearchParams,
    document: { getElementById(id) {
      if (!elements.has(id)) elements.set(id, node());
      return elements.get(id);
    }, addEventListener() {} },
    location: { search: '' }, localStorage: { getItem() { return null; }, setItem() {} },
    setTimeout(fn) { timers.set(++timer, fn); return timer; },
    clearTimeout(id) { timers.delete(id); },
    h: node, clear(n) { n.children = []; }, replace(n, ...children) { n.children = children.flat(); },
    post(url, body, options) {
      let resolve;
      const promise = new Promise(r => { resolve = r; });
      posts.push({ body, options, resolve });
      return promise;
    },
    get(url) {
      if (url.startsWith('/api/browse')) return Promise.resolve({ items: [] });
      let resolve;
      const promise = new Promise(r => { resolve = r; });
      libraries.push({ resolve });
      return promise;
    },
    icon() {}, skeletonCard() { return node(); },
    card() { return node('group'); },
    statePanel(props) { return node('state', props); }, errorPanel() { return node('error'); },
  });
  const dom = fs.readFileSync(new URL('../static/js/core/dom.js', import.meta.url), 'utf8');
  const debounce = dom.slice(dom.indexOf('export function debounce'), dom.indexOf('export function prefersReducedMotion')).replace('export ', '');
  const source = fs.readFileSync(new URL('../static/js/pages/search.js', import.meta.url), 'utf8').replace(/^import .*;\n/gm, '');
  vm.runInContext(debounce + '\n' + source, context);
  return { elements, timers, posts, libraries, flush: async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); } };
}

test('submitting immediately cancels the pending typing search', async () => {
  const h = harness();
  const input = h.elements.get('search-input');
  input.value = 'Example';
  input.handlers.input();
  assert.equal(h.timers.size, 1);
  h.elements.get('search-form').handlers.submit({ preventDefault() {} });
  assert.equal(h.timers.size, 0);
  assert.equal(h.posts.length, 1);
  assert.equal(h.posts[0].options.signal.aborted, false);
});

test('clearing a query aborts its request and discards late results', async () => {
  const h = harness();
  await h.flush();
  h.elements.get('search-input').value = 'Example';
  h.elements.get('search-form').handlers.submit({ preventDefault() {} });
  h.elements.get('search-clear').handlers.click();
  assert.equal(h.posts[0].options.signal.aborted, true);
  await h.flush();
  const landing = h.elements.get('search-results').children[0];
  h.libraries[0].resolve({ movies: [{ title: 'Old result' }] });
  h.posts[0].resolve({ search_id: 'old', titles: [] });
  await h.flush();
  assert.equal(h.elements.get('search-results').children[0], landing);
});

test('library matches appear while release providers are still pending', async () => {
  const h = harness();
  await h.flush();
  h.elements.get('search-input').value = 'Example';
  h.elements.get('search-form').handlers.submit({ preventDefault() {} });
  h.libraries[0].resolve({ movies: [{ title: 'Example' }] });
  await h.flush();
  const results = h.elements.get('search-results');
  assert.equal(results.children.length, 2);
  assert.equal(results.children[0].querySelector(), null);
  assert.equal(results.children[1].children[0].children[0].text, 'Searching available releases…');
  h.posts[0].resolve({ search_id: 'new', titles: [] });
  await h.flush();
  assert.equal(results.children.length, 1);
});
