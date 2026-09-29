import { get, post } from '../core/api.js';
import { h, debounce, replace, clear, parseSize, formatBytes } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { card, skeletonCard } from '../components/card.js';
import { statePanel, errorPanel } from '../components/state.js';
import { row } from '../components/row.js';
import { chooseReleaseAndDownload } from '../core/downloads.js';
import { toastError } from '../core/toast.js';
import { detailHref, playHref } from '../core/items.js';

/* Search.
 *
 * One question — "do I have this, and if not can I get it?" — answered in two
 * clearly separated groups. Library results lead because they are one click
 * from playing; everything else offers an add action.
 */

const form = document.getElementById('search-form');
const input = document.getElementById('search-input');
const clearButton = document.getElementById('search-clear');
const results = document.getElementById('search-results');
const recentBox = document.getElementById('recent-searches');

const RECENT_KEY = 'popcorn.recentSearches';
const MAX_RECENT = 6;

let currentQuery = '';
let searchId = null;
let libraryTitles = new Set();

/* ── Recent searches ────────────────────────────────────────────────────── */

function readRecent() {
  try {
    const stored = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]');
    return Array.isArray(stored) ? stored.filter((entry) => typeof entry === 'string') : [];
  } catch { return []; }
}

function rememberSearch(query) {
  const trimmed = query.trim();
  if (trimmed.length < 2) return;
  const next = [trimmed, ...readRecent().filter((entry) => entry.toLowerCase() !== trimmed.toLowerCase())];
  try { localStorage.setItem(RECENT_KEY, JSON.stringify(next.slice(0, MAX_RECENT))); } catch { /* private mode */ }
}

function renderRecent() {
  const recent = readRecent();
  if (!recent.length || currentQuery) {
    recentBox.hidden = true;
    clear(recentBox);
    return;
  }
  recentBox.hidden = false;
  replace(recentBox,
    h('span', { class: 't-label', style: { alignSelf: 'center', marginRight: 'var(--s2)' }, text: 'Recent' }),
    recent.map((query) => h('button', {
      class: 'recent-chip',
      type: 'button',
      onclick: () => { input.value = query; run(query); },
    }, icon('search', { size: 13 }), query)),
    h('button', {
      class: 'recent-chip',
      type: 'button',
      title: 'Clear recent searches',
      onclick: () => { try { localStorage.removeItem(RECENT_KEY); } catch { /* ignore */ } renderRecent(); },
    }, 'Clear'),
  );
}

/* ── Result rendering ───────────────────────────────────────────────────── */

const normalise = (value) => String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();

function libraryGroup(library) {
  const movies = library.movies || [];
  const shows = library.shows || [];
  const episodes = library.episodes || [];
  const total = movies.length + shows.length + episodes.length;
  if (!total) return null;

  movies.concat(shows).forEach((item) => libraryTitles.add(`${normalise(item.title)}|${item.year || ''}`));

  const grid = h('div', { class: 'grid' });
  movies.concat(shows).forEach((item) => grid.append(card(item, { showActions: true })));

  const nodes = [
    h('div', { class: 'result-group-head' },
      h('h2', { text: 'In your library' }),
      h('span', { class: 'count', text: `${total} ${total === 1 ? 'match' : 'matches'}` }),
    ),
    grid,
  ];

  if (episodes.length) {
    nodes.push(h('div', { style: { marginTop: 'var(--s5)' } },
      h('h3', { class: 't-label', style: { marginBottom: 'var(--s3)' }, text: 'Episodes' }),
      h('div', { class: 'list' }, episodes.slice(0, 8).map((episode) => h('a', {
        class: 'list-item',
        href: playHref(episode),
      },
        h('span', { class: 'list-item-body' },
          h('strong', { text: episode.title }),
          h('small', { text: [episode.series?.name, episode.parent_index_number !== null
            ? `S${episode.parent_index_number} · E${episode.index_number}` : null].filter(Boolean).join(' · ') }),
        ),
        h('span', { class: 'icon-btn', 'aria-hidden': 'true' }, icon('play', { size: 16 })),
      ))),
    ));
  }

  return h('section', { class: 'result-group' }, nodes);
}

const INITIAL_TITLES = 6;

function availableGroup(titles) {
  if (!titles?.length) return null;

  const list = h('div', { class: 'add-list' });
  const section = h('section', { class: 'result-group' });
  const moreWrap = h('div', { style: { display: 'flex', justifyContent: 'center', marginTop: 'var(--s5)' } });

  let expanded = false;

  const paint = () => {
    clear(list);
    clear(moreWrap);
    const visible = expanded ? titles : titles.slice(0, INITIAL_TITLES);
    visible.forEach((title, index) => list.append(buildAddRow(title, index)));
    if (!expanded && titles.length > INITIAL_TITLES) {
      moreWrap.append(h('button', {
        class: 'btn btn-outline',
        type: 'button',
        text: `Show ${titles.length - INITIAL_TITLES} more`,
        onclick: () => { expanded = true; paint(); },
      }));
    }
  };

  section.append(
    h('div', { class: 'result-group-head' },
      h('h2', { text: 'Not in your library' }),
      h('span', { class: 'count', text: `${titles.length} ${titles.length === 1 ? 'title' : 'titles'} available to add` }),
    ),
    list,
    moreWrap,
  );

  paint();
  return section;
}

function buildAddRow(title, index) {
  {
    const owned = libraryTitles.has(`${normalise(title.title)}|${title.year || ''}`);
    const art = h('div', { class: 'add-row-art' });

    const row = h('div', { class: 'add-row' },
      art,
      h('div', { class: 'add-row-body' },
        h('div', { class: 'add-row-title clamp-2', text: title.title }),
        h('div', { class: 'add-row-meta' },
          title.year ? h('span', { class: 't-mute t-xs', text: title.year }) : null,
          ...title.qualities.slice(0, 4).map((quality) => h('span', { class: 'chip chip-plain' }, quality)),
          title.releases.some((release) => release.season_pack) ? h('span', { class: 'chip' }, 'Season pack') : null,
          owned ? h('span', { class: 'chip chip-ok' }, 'Already in library') : null,
        ),
        h('div', { class: 'add-row-facts' },
          [
            `${title.release_count} release${title.release_count === 1 ? '' : 's'}`,
            formatBytes(title.size_bytes) ? `from ${formatBytes(title.size_bytes)}` : null,
            title.top_seeds > 0 ? `${title.top_seeds} seeders` : 'no seeders',
          ].filter(Boolean).join(' · '),
        ),
      ),
      h('div', { class: 'add-row-actions' },
        owned ? h('a', { class: 'btn btn-outline btn-sm', href: '/downloads' }, 'Downloads') : null,
        h('button', {
          class: 'btn btn-primary btn-sm',
          type: 'button',
          onclick: async (event) => {
            const button = event.currentTarget;
            button.disabled = true;
            const job = await chooseReleaseAndDownload({
              title: title.title,
              year: title.year,
              releases: title.releases,
              searchId,
              poster: row.dataset.poster || '',
            });
            button.disabled = false;
            if (job) loadActiveHint();
          },
        }, icon('download', { size: 15 }), 'Add'),
      ),
    );

    // Posters for titles that aren't in the library yet arrive separately so
    // the result list is never held up by an artwork lookup.
    if (index < 12) {
      const query = new URLSearchParams({ q: title.title });
      if (title.year) query.set('y', String(title.year));
      get(`/api/search/art?${query}`).then((data) => {
        if (!data.poster) return;
        row.dataset.poster = data.poster;
        const img = h('img', { src: data.poster, alt: '', loading: 'lazy', decoding: 'async' });
        img.addEventListener('load', () => img.classList.add('is-loaded'), { once: true });
        art.append(img);
      }).catch(() => {});
    }

    return row;
  }
}

function loadActiveHint() {
  // The Downloads tab badge updates itself; nothing else to do here.
}

function renderEmpty(query) {
  replace(results, statePanel({
    tone: 'empty',
    mark: 'search',
    title: `Nothing found for “${query}”`,
    message: 'Try a shorter title, or check the spelling. Release indexes vary in how they name things.',
    actions: [
      { label: 'Browse your library', onClick: () => { location.href = '/movies'; } },
    ],
  }));
}

/**
 * What to show before anyone types.
 *
 * An empty search box is a dead end, so the landing state offers the two
 * things a viewer might actually want: what they were looking for before, and
 * what arrived recently.
 */
async function renderLanding() {
  replace(results);
  renderRecent();

  const mount = h('section', { class: 'result-group' });
  replace(results, mount);

  try {
    const [recent, popular] = await Promise.all([
      get('/api/browse?type=Movie&sort=added&limit=18'),
      get('/api/browse?type=Series&sort=added&limit=6'),
    ]);

    if (!recent.items.length && !popular.items.length) {
      replace(mount, statePanel({
        tone: 'empty',
        mark: 'library',
        title: 'Nothing in your library yet',
        message: 'Search above to find a film, then add it. It will appear here once it has downloaded.',
        actions: [{ label: 'How downloads work', onClick: () => { location.href = '/downloads'; } }],
      }));
      return;
    }

    const nodes = [];
    if (recent.items.length) {
      nodes.push(row({
        title: 'Recently added to your library',
        items: recent.items,
        href: '/movies',
        hrefLabel: 'All movies',
        priorityCount: 5,
      }));
    }
    if (popular.items.length) {
      nodes.push(row({
        title: 'TV shows',
        items: popular.items,
        href: '/shows',
        hrefLabel: 'All shows',
      }));
    }
    replace(mount, h('div', { style: { marginTop: 'calc(var(--s5) * -1)' } }, nodes));
  } catch {
    // A failure to load suggestions must not look like a failed search.
    replace(mount);
  }
}

/* ── Search execution ───────────────────────────────────────────────────── */

let controller = null;
let torrentToken = 0;

async function run(query) {
  debouncedRun.cancel();
  controller?.abort();
  const token = ++torrentToken;
  searchId = null;
  const trimmed = query.trim();
  currentQuery = trimmed;
  clearButton.hidden = !trimmed;
  renderRecent();

  if (trimmed.length < 2) {
    renderLanding();
    return;
  }

  controller = new AbortController();

  // Library results are local and fast; show them while the indexes are still
  // being scraped.
  const skeleton = h('section', { class: 'result-group' },
    h('div', { class: 'result-group-head' }, h('h2', { text: 'Searching…' })),
    h('div', { class: 'grid' }, Array.from({ length: 6 }, () => skeletonCard())),
  );
  replace(results, skeleton);

  const torrentPromise = post('/api/search', { query: trimmed }, { signal: controller.signal })
    .then((data) => { if (token === torrentToken) searchId = data.search_id; return data; })
    .catch((error) => (error.name === 'AbortError' ? null : { error }));

  let library = null;
  try {
    library = await get(`/api/search/library?q=${encodeURIComponent(trimmed)}`, { signal: controller.signal });
  } catch (error) {
    if (error.name === 'AbortError') return;
    library = { error };
  }
  if (token !== torrentToken) return;

  const nodes = [];
  libraryTitles = new Set();

  if (library?.error) {
    nodes.push(h('section', { class: 'result-group' },
      errorPanel(library.error, { subject: 'your library' }),
    ));
  } else {
    const group = libraryGroup(library);
    if (group) nodes.push(group);
  }

  const pending = h('section', { class: 'result-group' },
    h('div', { class: 'result-group-head' }, h('h2', { text: 'Searching available releases…' })),
  );
  replace(results, [...nodes, pending]);

  const torrents = await torrentPromise;
  if (token !== torrentToken) return;

  if (torrents?.error) {
    nodes.push(h('section', { class: 'result-group' },
      errorPanel(torrents.error, { subject: 'the release indexes' }),
    ));
  } else if (torrents?.titles?.length) {
    const group = availableGroup(torrents.titles);
    if (group) nodes.push(group);
  }

  const hasAnything = nodes.some((node) => node.querySelector?.('.grid, .add-list'));
  if (!hasAnything && !library?.error && !torrents?.error) {
    renderEmpty(trimmed);
    return;
  }
  replace(results, nodes);
  rememberSearch(trimmed);
}

/* ── Wiring ─────────────────────────────────────────────────────────────── */

form.addEventListener('submit', (event) => {
  event.preventDefault();
  input.blur();
  run(input.value);
});

const debouncedRun = debounce(() => run(input.value), 420);
input.addEventListener('input', () => {
  clearButton.hidden = !input.value.trim();
  if (input.value.trim().length < 2) {
    run(input.value);
    return;
  }
  debouncedRun();
});

clearButton.addEventListener('click', () => {
  input.value = '';
  run('');
  input.focus();
});

document.addEventListener('keydown', (event) => {
  if (event.key === '/' && document.activeElement !== input) {
    event.preventDefault();
    input.focus();
  }
});

const initial = new URLSearchParams(location.search).get('q');
if (initial) {
  input.value = initial;
  run(initial);
} else {
  renderLanding();
}
