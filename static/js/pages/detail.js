import { get, post, del } from '../core/api.js';
import { h, replace, clear, formatRuntime, formatBytes, formatDate, joinMeta, initials } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { artImage, artFrame, SIZES } from '../core/art.js';
import { card, skeletonCard } from '../components/card.js';
import { row, skeletonRow } from '../components/row.js';
import { statePanel, errorPanel } from '../components/state.js';
import { confirmDialog, openModal } from '../components/modal.js';
import { clipsSection } from '../components/clips.js';
import { showToast, toastError } from '../core/toast.js';
import { watchlistButton } from '../core/watchlist.js';
import { episodeLabel, infoParts, isResumable, playHref, progressPercent, remainingLabel } from '../core/items.js';

/* Movie, series and collection detail.
 *
 * The composition is deliberately the same for every type — wide art, poster,
 * title block, actions — so a show never feels like a different product from
 * a film. What changes is the body: episodes for a series, cast for a film.
 */

const mount = document.getElementById('detail');
const itemId = mount.dataset.itemId;
const isAdmin = document.body.dataset.admin === 'true';

let item = null;
let activeSeason = null;

/* ── Loading ────────────────────────────────────────────────────────────── */

function showSkeleton() {
  const hero = h('section', { class: 'detail-hero' },
    h('div', { class: 'detail-hero-backdrop skeleton' }),
    h('div', { class: 'detail-hero-content' },
      h('div', { class: 'detail-poster skeleton' }),
      h('div', { style: { paddingBottom: 'var(--s2)' } },
        h('div', { class: 'skeleton skeleton-title', style: { height: '46px', width: 'min(420px, 70%)' } }),
        h('div', { class: 'skeleton skeleton-text', style: { marginTop: 'var(--s4)', width: '260px' } }),
        h('div', { class: 'skeleton skeleton-text', style: { marginTop: 'var(--s5)', height: '52px', width: 'min(560px, 90%)' } }),
        h('div', { class: 'skeleton', style: { marginTop: 'var(--s5)', height: '50px', width: '300px', borderRadius: 'var(--r-sm)' } }),
      ),
    ),
  );
  replace(mount, hero, h('div', { class: 'shell' }, skeletonRow({ count: 6 })));
}

/* ── Hero ───────────────────────────────────────────────────────────────── */

function buildHero() {
  // A series has no single resume point, so its progress is never shown here.
  const resumable = item.type !== 'Series' && isResumable(item);
  const percent = progressPercent(item);
  const backdrop = artImage(item.backdrop, { alt: '', sizes: SIZES.backdrop, priority: true });
  const backdropBox = h('div', {
    class: `detail-hero-backdrop ${item.backdrop ? '' : 'is-empty'}`.trim(),
    'aria-hidden': 'true',
  }, backdrop);

  const posterNode = artImage(item.poster, { alt: '', sizes: SIZES.detail, priority: true });
  const poster = posterNode || h('div', { class: 'card-art-fallback' }, h('span', { text: item.title }));

  const metaParts = item.type === 'Series'
    ? [
      item.year,
      item.seasons?.length ? `${item.seasons.length} season${item.seasons.length === 1 ? '' : 's'}` : null,
      item.episode_count ? `${item.episode_count} episodes` : null,
      item.official_rating,
      item.genres?.slice(0, 3).join(', '),
    ]
    : infoParts(item);

  const playTarget = item.type === 'Series' && item.next_up ? item.next_up : item;

  const actions = h('div', { class: 'detail-actions' });

  if (item.type === 'Series' && !item.episode_count) {
    actions.append(h('span', { class: 'chip' }, 'No episodes yet'));
  } else {
    actions.append(h('a', {
      class: 'btn btn-primary btn-lg',
      href: playHref(playTarget),
    },
      icon(resumable ? 'resume' : 'play', { size: 18 }),
      resumable ? 'Resume' : (item.type === 'Series' ? 'Play next' : 'Play'),
    ));
  }

  if (item.type === 'Series' && item.next_up) {
    actions.append(h('a', { class: 'btn btn-secondary btn-lg', href: playHref(item.next_up) },
      icon('next', { size: 16 }),
      `Next up · ${episodeLabel(item.next_up) || item.next_up.title}`,
    ));
  }

  if (resumable) {
    actions.append(h('button', {
      class: 'btn btn-secondary btn-lg',
      type: 'button',
      onclick: () => { location.href = `${playHref(item)}?restart=1`; },
    }, icon('refresh', { size: 16 }), 'Start over'));
  }

  if (item.type === 'Movie' && item.trailers?.length) {
    actions.append(h('button', {
      class: 'btn btn-secondary btn-lg',
      type: 'button',
      onclick: () => openTrailer(item.trailers[0]),
    }, icon('film', { size: 16 }), 'Trailer'));
  }

  actions.append(watchlistButton(item, { className: 'detail-watchlist' }));

  if (item.type === 'Movie') actions.append(buildMoreMenu());

  const content = h('div', { class: 'detail-hero-content' },
    h('div', { class: 'detail-poster' }, poster),
    h('div', { class: 'detail-info' },
      item.logo
        ? h('h1', { class: 'sr-only', text: item.title })
        : h('h1', { class: 'detail-title', text: item.title }),
      item.logo
        ? h('img', { class: 'hero-logo detail-logo', src: item.logo.src, srcset: item.logo.srcset, alt: '' })
        : null,
      item.tagline ? h('p', { class: 'detail-tagline', text: item.tagline }) : null,
      h('div', { class: 'detail-meta' },
        item.community_rating
          ? h('span', { class: 'rating' }, icon('star', { size: 13 }), item.community_rating.toFixed(1))
          : null,
        ...metaParts.filter(Boolean).map((part) => h('span', { text: part })),
      ),
      item.overview ? h('p', { class: 'detail-overview clamp-4', text: item.overview }) : null,
      resumable ? h('div', { class: 'hero-progress' },
        h('div', { class: 'progress' }, h('i', { style: { width: `${percent}%` } })),
        h('div', { class: 'hero-progress-label', text: joinMeta([`${percent}% watched`, remainingLabel(item)]) }),
      ) : null,
      actions,
    ),
  );

  return h('section', { class: 'detail-hero' }, backdropBox, content);
}

function buildMoreMenu() {
  const wrap = h('div', { class: 'menu-wrap' });
  const button = h('button', {
    class: 'btn btn-secondary btn-lg btn-icon',
    type: 'button',
    'aria-label': 'More actions',
    'aria-haspopup': 'menu',
    'aria-expanded': 'false',
  }, icon('more', { size: 18, strokeWidth: 2.6 }));

  const menu = h('div', { class: 'menu', role: 'menu', hidden: true });
  const close = () => { menu.hidden = true; button.setAttribute('aria-expanded', 'false'); };

  menu.append(
    h('button', {
      class: 'menu-item', type: 'button', role: 'menuitem',
      onclick: async () => {
        close();
        try {
          const played = !item.user?.played;
          await post(`/api/item/${item.id}/played`, { played });
          showToast(played ? 'Marked as watched' : 'Marked as unwatched');
          item.user.played = played;
          if (played) item.user.percent = 100;
          reload();
        } catch (error) { toastError(error); }
      },
    }, icon('check', { size: 17 }), item.user?.played ? 'Mark as unwatched' : 'Mark as watched'),

    isAdmin ? h('div', { class: 'menu-sep' }) : null,

    isAdmin ? h('button', {
      class: 'menu-item', type: 'button', role: 'menuitem',
      onclick: () => { close(); openMatch(); },
    }, icon('search', { size: 17 }), 'Match to a different film…') : null,

    isAdmin ? h('button', {
      class: 'menu-item is-danger', type: 'button', role: 'menuitem',
      onclick: async () => {
        close();
        const confirmed = await confirmDialog({
          title: 'Delete from library?',
          message: `“${item.title}” and its media file will be permanently removed. This cannot be undone.`,
          confirmLabel: 'Delete',
        });
        if (!confirmed) return;
        try {
          await del(`/api/library/movies/${item.id}`);
          showToast('Removed from your library');
          setTimeout(() => { location.href = '/movies'; }, 600);
        } catch (error) { toastError(error); }
      },
    }, icon('trash', { size: 17 }), 'Delete from library') : null,
  );

  button.addEventListener('click', (event) => {
    event.stopPropagation();
    menu.hidden = !menu.hidden;
    button.setAttribute('aria-expanded', String(!menu.hidden));
    if (!menu.hidden) menu.querySelector('.menu-item')?.focus();
  });
  document.addEventListener('click', (event) => {
    if (!menu.hidden && !wrap.contains(event.target)) close();
  });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });

  wrap.append(button, menu);
  return wrap;
}

/* Re-matching a film that was matched to the wrong one.
 *
 * A release folder says what it is — "… Spa (2026) Malayalam HQ HDRip …" — and
 * that is not what the library shows once the wrong film has been applied to
 * it. So the search starts from the file's own name, and the viewer can
 * rewrite it and pick from the answers.
 */
async function openMatch() {
  const input = h('input', {
    class: 'input', type: 'search', spellcheck: 'false',
    placeholder: 'Film title, year, language',
    'aria-label': 'Search for the film',
  });
  const status = h('p', { class: 't-mute t-xs' });
  const results = h('div', { class: 'match-list' });

  const search = async (query) => {
    status.textContent = 'Searching…';
    clear(results);
    try {
      const data = await post('/api/library/metadata/find', { item_id: item.id, query: query || '' });
      if (!input.value) input.value = data.suggested_query || '';
      if (!data.candidates.length) {
        status.textContent = 'Nothing found. Try the name as the release has it, with its year and language.';
        return;
      }
      status.textContent = `${data.candidates.length} possible match${data.candidates.length === 1 ? '' : 'es'} — check the year and the story before applying.`;
      data.candidates.forEach((candidate) => results.append(option(candidate)));
    } catch (error) {
      status.textContent = error.message;
    }
  };

  const option = (candidate) => h('div', { class: 'match-option' },
    candidate.image
      ? h('img', { src: candidate.image, alt: '', loading: 'lazy', decoding: 'async' })
      : h('div', { class: 'match-fallback' }, icon('film', { size: 18 })),
    h('div', { class: 'match-body' },
      h('strong', { text: joinMeta([candidate.name, candidate.year], ' · ') }),
      candidate.overview ? h('p', { class: 'clamp-3', text: candidate.overview }) : null,
    ),
    h('button', {
      class: 'btn btn-primary btn-sm', type: 'button',
      onclick: async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        button.textContent = 'Applying…';
        try {
          await post('/api/library/metadata/apply', { item_id: item.id, candidate: candidate.result });
          showToast(`Matched to ${candidate.name}`);
          setTimeout(reload, 800);
        } catch (error) {
          toastError(error);
          button.disabled = false;
          button.textContent = 'Apply';
        }
      },
    }, 'Apply'),
  );

  openModal({
    title: `Match “${item.title}” to a film`,
    wide: true,
    body: h('div', { class: 'match-panel' },
      h('p', { class: 't-dim', text: 'The film you pick replaces this title\u2019s details, artwork and poster. '
        + 'The file itself is never touched.' }),
      h('div', { class: 'match-search' },
        input,
        h('button', { class: 'btn btn-primary', type: 'button', onclick: () => search(input.value) }, 'Search'),
      ),
      status,
      results,
    ),
  });
  requestAnimationFrame(() => input.focus());
  search('');
}

function openTrailer(trailer) {
  const url = trailer.url?.replace('watch?v=', 'embed/');
  const panel = h('div', { class: 'modal', onclick: (event) => { if (event.target === panel) close(); } });
  const close = () => { panel.remove(); document.body.style.removeProperty('overflow'); };
  panel.append(h('div', { class: 'modal-panel is-wide', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Trailer' },
    h('div', { class: 'modal-head' },
      h('h2', { text: trailer.name || 'Trailer' }),
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Close', onclick: close }, icon('close', { size: 18 })),
    ),
    h('div', { style: { aspectRatio: '16 / 9', background: '#000' } },
      h('iframe', {
        src: url, title: trailer.name || 'Trailer', allow: 'autoplay; encrypted-media',
        allowfullscreen: 'true', style: { width: '100%', height: '100%', border: '0' },
      }),
    ),
    h('div', { class: 'modal-foot' },
      h('a', { class: 'btn btn-outline', href: trailer.url, target: '_blank', rel: 'noopener noreferrer' },
        'Open on YouTube'),
    ),
  ));
  document.body.append(panel);
  document.body.style.overflow = 'hidden';
  document.addEventListener('keydown', function onKey(event) {
    if (event.key === 'Escape') { close(); document.removeEventListener('keydown', onKey); }
  });
}

/* ── Body sections ──────────────────────────────────────────────────────── */

function castBlock() {
  if (!item.cast?.length) return null;
  const strip = h('div', { class: 'cast-strip' });
  for (const person of item.cast) {
    strip.append(h('div', { class: 'cast-member' },
      person.image
        ? h('img', { src: person.image, alt: '', loading: 'lazy', decoding: 'async' })
        : h('div', { class: 'cast-fallback', text: initials(person.name) }),
      h('strong', { text: person.name }),
      person.role ? h('small', { text: person.role }) : null,
    ));
  }
  return h('section', { class: 'detail-block' }, h('h2', { text: 'Cast' }), strip);
}

function detailFacts() {
  const facts = [];
  const add = (term, value) => { if (value) facts.push([term, value]); };

  add('Released', formatDate(item.premiere_date));
  add('Runtime', formatRuntime(item.runtime_minutes));
  if (item.media?.resolution) {
    add('Video', joinMeta([item.media.resolution, item.media.video_codec, item.media.hdr], ' · '));
  }
  if (item.media?.audio?.length) {
    add('Audio', item.media.audio
      .map((track) => joinMeta([track.language, track.codec, track.channels], ' '))
      .join(', '));
  }
  if (item.media?.subtitle_count) {
    add('Subtitles', joinMeta([
      `${item.media.subtitle_count} track${item.media.subtitle_count === 1 ? '' : 's'}`,
      item.media.subtitle_languages?.join(', '),
    ]));
  }
  add('File', joinMeta([item.media?.container, formatBytes(item.media?.size)], ' · '));
  if (item.directors?.length) add('Director', item.directors.join(', '));
  if (item.writers?.length) add('Writer', item.writers.join(', '));
  if (item.studios?.length) add('Studio', item.studios.join(', '));
  if (item.original_title && item.original_title !== item.title) add('Original title', item.original_title);

  if (!facts.length) return null;
  return h('section', { class: 'detail-block' },
    h('h2', { text: 'Details' }),
    h('dl', { class: 'facts' }, facts.map(([term, value]) =>
      h('div', { class: 'fact' }, h('dt', { text: term }), h('dd', { text: value })),
    )),
  );
}

function versionsBlock() {
  if (!item.versions?.length) return null;
  return h('section', { class: 'detail-block' },
    h('h2', { text: 'Versions' }),
    h('div', { class: 'list' }, item.versions.map((version) =>
      h('div', { class: 'list-item' },
        h('div', { class: 'list-item-body' },
          h('strong', { text: version.name || joinMeta([version.video, version.container], ' · ') || 'Version' }),
          h('small', { text: joinMeta([version.video, formatBytes(version.size), formatRuntime(version.runtime_minutes)]) }),
        ),
        h('a', { class: 'btn btn-sm btn-outline', href: `${playHref(item)}?source=${encodeURIComponent(version.id)}` }, 'Play'),
      ),
    )),
  );
}

function episodesBlock() {
  if (item.type !== 'Series') return null;
  if (!item.episodes?.length) {
    return h('section', { class: 'detail-block' },
      h('h2', { text: 'Episodes' }),
      statePanel({
        tone: 'empty', compact: true, mark: 'tv',
        title: 'No episodes found',
        message: 'Your media server has not matched any episode files to this series yet.',
      }),
    );
  }

  const withNumbers = item.episodes.filter((episode) => episode.parent_index_number !== null);
  const seasons = item.seasons?.length
    ? item.seasons
    : [...new Set(withNumbers.map((episode) => episode.parent_index_number))]
      .sort((a, b) => a - b)
      .map((index) => ({ index, name: `Season ${index}` }));

  const current = seasons.find((season) => season.index === activeSeason)
    || seasons.find((season) => season.index === item.next_up?.parent_index_number)
    || seasons.find((season) => season.index > 0)
    || seasons[0];
  activeSeason = current?.index ?? null;

  const tabs = h('div', { class: 'segmented season-tabs', role: 'tablist', 'aria-label': 'Seasons' });
  seasons.forEach((season) => {
    tabs.append(h('button', {
      type: 'button',
      role: 'tab',
      text: season.name || `Season ${season.index}`,
      'aria-selected': String(season.index === activeSeason),
      onclick: () => { activeSeason = season.index; rerenderBody(); },
    }));
  });

  const visible = withNumbers
    .filter((episode) => episode.parent_index_number === activeSeason)
    .sort((a, b) => (a.index_number || 0) - (b.index_number || 0));

  const list = h('div', { class: 'episode-list' });
  if (!visible.length) {
    list.append(statePanel({
      tone: 'empty', compact: true, mark: 'tv',
      title: 'Nothing in this season',
      message: 'This season has no episodes your media server recognises yet.',
    }));
  }
  visible.forEach((episode) => list.append(episodeRow(episode)));

  return h('section', { class: 'detail-block' },
    h('h2', { text: 'Episodes' }),
    seasons.length > 1 ? tabs : null,
    list,
  );
}

function episodeRow(episode) {
  const percent = progressPercent(episode);
  const thumb = artFrame(episode.thumb || episode.poster, {
    alt: '', sizes: SIZES.episode, fallbackTitle: '',
  });
  thumb.append(h('div', { class: 'episode-play' }, h('span', {}, icon('play', { size: 15 }))));
  if (percent > 0 && percent < 98) {
    thumb.append(h('div', { class: 'card-progress' }, h('i', { style: { width: `${percent}%` } })));
  }

  return h('a', {
    class: `episode ${episode.user?.played ? 'is-played' : ''}`.trim(),
    href: playHref(episode),
    'aria-label': `Play ${episodeLabel(episode) || ''} ${episode.title}`.trim(),
  },
    thumb,
    h('div', { class: 'episode-body' },
      h('div', { class: 'episode-head' },
        h('span', { class: 'episode-num', text: episodeLabel(episode) || '' }),
        h('span', { class: 'episode-title clamp-2', text: episode.title }),
        episode.user?.played ? h('span', { class: 'chip chip-ok' }, 'Watched') : null,
      ),
      episode.overview ? h('p', { class: 'episode-desc clamp-2', text: episode.overview }) : null,
      h('div', { class: 'episode-meta' },
        episode.runtime_minutes ? h('span', { text: formatRuntime(episode.runtime_minutes) }) : null,
        episode.premiere_date ? h('span', { text: formatDate(episode.premiere_date) }) : null,
      ),
    ),
    h('div', { class: 'episode-side' },
      h('span', { class: 'icon-btn', 'aria-hidden': 'true' }, icon(percent > 1 ? 'resume' : 'play', { size: 18 })),
    ),
  );
}

function collectionBlock() {
  if (item.type !== 'BoxSet' || !item.items?.length) return null;
  const grid = h('div', { class: 'grid' });
  item.items.forEach((child) => grid.append(card(child)));
  return h('section', { class: 'detail-block' },
    h('h2', { text: `In this collection · ${item.items.length}` }),
    grid,
  );
}

function bodySections() {
  const main = h('div', { class: 'detail-main' });
  const side = h('aside', { class: 'detail-side' });

  // Clips lead the body for a film: they are the one thing on this page that
  // is about watching the movie rather than filing it.
  if (item.type === 'Movie') main.append(clipsSection({ itemId }));
  const episodes = episodesBlock();
  if (episodes) main.append(episodes);
  const collection = collectionBlock();
  if (collection) main.append(collection);
  const cast = castBlock();
  if (cast) main.append(cast);

  const facts = detailFacts();
  if (facts) side.append(facts);
  const versions = versionsBlock();
  if (versions) side.append(versions);

  if (!main.childNodes.length && !side.childNodes.length) return null;
  return h('div', { class: 'detail-body' }, main, side);
}

/* ── Composition ────────────────────────────────────────────────────────── */

function render() {
  document.title = `${item.title} — Popcorn`;
  const nodes = [buildHero()];
  const body = bodySections();
  if (body) nodes.push(body);
  nodes.push(h('div', { class: 'shell', id: 'detail-similar' }));
  replace(mount, nodes);
  loadSimilar();
}

function rerenderBody() {
  const existing = mount.querySelector('.detail-body');
  const fresh = bodySections();
  if (existing && fresh) existing.replaceWith(fresh);
  else if (fresh) mount.append(fresh);
}

async function loadSimilar() {
  const target = document.getElementById('detail-similar');
  if (!target) return;
  target.append(skeletonRow({ count: 6 }));
  try {
    const data = await get(`/api/item/${itemId}/similar`);
    clear(target);
    if (data.items?.length) {
      target.append(row({ title: 'More like this', items: data.items }));
    }
  } catch {
    clear(target); // recommendations are a bonus; never surface their failure
  }
}

function reload() {
  clear(mount);
  load();
}

async function load() {
  showSkeleton();
  try {
    item = await get(`/api/item/${itemId}`);
    render();
  } catch (error) {
    replace(mount, errorPanel(error, {
      subject: 'this title',
      onRetry: load,
    }));
  }
}

load();
