import { get } from '../core/api.js';
import { h, replace, clear } from '../core/dom.js';
import { card, skeletonCard } from '../components/card.js';
import { statePanel, errorPanel } from '../components/state.js';

/* Movies and TV Shows.
 *
 * A filterable grid over the whole library. Genres are offered only when the
 * server says they exist, so an empty library never shows dead controls.
 */

const PAGE_SIZE = 60;
const root = document.querySelector('[data-library-type]');
const type = root.dataset.libraryType;
const fixedFilter = root.dataset.filter || null;

const grid = document.getElementById('browse-grid');
const genreStrip = document.getElementById('browse-genres');
const stateMount = document.getElementById('browse-state');
const sortSelect = document.getElementById('browse-sort');
const countLabel = document.getElementById('browse-count');
const moreButton = document.getElementById('browse-more');
const summary = document.getElementById('browse-summary');

const params = new URLSearchParams(location.search);
let genre = params.get('genre') || null;
let sort = params.get('sort') || 'added';
let items = [];
let total = 0;
let loading = false;

sortSelect.value = sort;

function emptyState() {
  if (fixedFilter === 'watchlist') {
    return statePanel({
      tone: 'empty',
      mark: 'bookmark',
      title: 'Your watchlist is empty',
      message: 'Add titles with the bookmark button — they will be waiting here when you want them.',
      actions: [{ label: 'Browse movies', primary: true, onClick: () => { location.href = '/movies'; } }],
    });
  }
  if (type === 'Series') {
    return statePanel({
      tone: 'empty',
      mark: 'shows',
      title: 'No TV shows yet',
      message: 'Download a series and it will appear here with its seasons and episodes ready to play.',
      actions: [{ label: 'Find a show', primary: true, onClick: () => { location.href = '/search'; } }],
    });
  }
  return statePanel({
    tone: 'empty',
    mark: 'library',
    title: 'Nothing here yet',
    message: 'Your library is empty. Find something to watch and download it — it will show up here automatically.',
    actions: [{ label: 'Find something to watch', primary: true, onClick: () => { location.href = '/search'; } }],
  });
}

function renderGenres(genres) {
  if (!genres?.length) return;
  const nodes = [h('button', {
    class: 'genre-chip',
    type: 'button',
    text: 'All',
    'aria-pressed': String(!genre),
    onclick: () => selectGenre(null),
  })];
  for (const entry of genres) {
    nodes.push(h('button', {
      class: 'genre-chip',
      type: 'button',
      text: entry.name,
      'aria-pressed': String(genre === entry.name),
      onclick: () => selectGenre(entry.name),
    }));
  }
  replace(genreStrip, nodes);
}

function selectGenre(next) {
  genre = next;
  [...genreStrip.querySelectorAll('.genre-chip')].forEach((chip) => {
    chip.setAttribute('aria-pressed', String(chip.textContent === (genre || 'All')));
  });
  const url = new URL(location.href);
  if (genre) url.searchParams.set('genre', genre);
  else url.searchParams.delete('genre');
  history.replaceState(null, '', url);
  load({ reset: true });
}

function renderGrid({ append = false } = {}) {
  if (!append) clear(grid);
  const fragment = document.createDocumentFragment();
  items.forEach((item) => fragment.append(card(item, { sizes: undefined })));
  grid.append(fragment);
  grid.setAttribute('aria-busy', 'false');
}

async function load({ reset = false } = {}) {
  if (loading) return;
  loading = true;

  if (reset) {
    items = [];
    replace(stateMount);
    clear(grid);
    grid.setAttribute('aria-busy', 'true');
    const placeholders = document.createDocumentFragment();
    for (let index = 0; index < 12; index += 1) placeholders.append(skeletonCard());
    grid.append(placeholders);
  }

  moreButton.disabled = true;
  try {
    const query = new URLSearchParams({ type, sort, start: String(items.length), limit: String(PAGE_SIZE) });
    if (genre) query.set('genre', genre);
    if (fixedFilter) query.set('filter', fixedFilter);
    const data = await get(`/api/browse?${query}`);

    total = data.total;
    items = items.concat(data.items);
    if (reset) renderGenres(data.genres);
    renderGrid({ append: !reset });

    countLabel.textContent = total === 1 ? '1 title' : `${total} titles`;
    if (summary && !genre) {
      summary.textContent = type === 'Series'
        ? `Every series in your library, with seasons and episodes ready to play.`
        : `Everything you own, ready to play.`;
    }
    moreButton.hidden = items.length >= total;

    if (!items.length) replace(stateMount, emptyState());
  } catch (error) {
    clear(grid);
    grid.setAttribute('aria-busy', 'false');
    replace(stateMount, errorPanel(error, { subject: 'your library', onRetry: () => load({ reset: true }) }));
    moreButton.hidden = true;
  } finally {
    loading = false;
    moreButton.disabled = false;
  }
}

sortSelect.addEventListener('change', () => {
  sort = sortSelect.value;
  const url = new URL(location.href);
  url.searchParams.set('sort', sort);
  history.replaceState(null, '', url);
  load({ reset: true });
});

moreButton.addEventListener('click', () => load({ append: true }));

load({ reset: true });
