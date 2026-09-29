import { get, post, del } from '../core/api.js';
import { h, replace, clear, formatBytes, parseSize, relativeTime, joinMeta } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { statePanel, errorPanel } from '../components/state.js';
import { confirmDialog } from '../components/modal.js';
import { showToast, toastError } from '../core/toast.js';
import {
  STAGE_LABELS, stageDescription, stageTrack, isTerminal, cancelDownload, loadJobs, identifyDownload,
} from '../core/downloads.js';

/* Download manager.
 *
 * Active transfers lead with what is happening and how far along it is.
 * Anything that needs a decision — play it, retry it, remove it — is one
 * click away, and torrent internals stay out of sight.
 */

const list = document.getElementById('downloads-list');
const stateMount = document.getElementById('downloads-state');
const countLabel = document.getElementById('downloads-count');
const tabActive = document.getElementById('tab-active');
const tabHistory = document.getElementById('tab-history');

let scope = 'active';
let jobs = [];
let posters = new Map();
let timers = new Map();
let pollTimer;

/* ── Posters ────────────────────────────────────────────────────────────── */

/** Completed jobs know their library item; ask for its artwork once. */
async function resolvePosters(entries) {
  const wanted = entries
    .filter((job) => !job.poster && job.library_item_id && !posters.has(job.library_item_id))
    .map((job) => job.library_item_id);
  if (!wanted.length) return;
  await Promise.all([...new Set(wanted)].slice(0, 12).map(async (itemId) => {
    try {
      const item = await get(`/api/item/${itemId}`);
      posters.set(itemId, item.poster?.src || null);
    } catch { posters.set(itemId, null); }
  }));
}

function posterFor(job) {
  if (job.poster) return job.poster;
  if (job.library_item_id) return posters.get(job.library_item_id) || null;
  return null;
}

function displayTitle(job) {
  return job.display_title || job.title;
}

/* ── Rendering ──────────────────────────────────────────────────────────── */

function downloadRow(job) {
  const terminal = isTerminal(job.status);
  const percent = job.status === 'complete' ? 100 : (job.progress || 0);
  const poster = posterFor(job);

  const art = h('div', { class: 'download-art' });
  if (poster) {
    const img = h('img', { src: poster, alt: '', loading: 'lazy', decoding: 'async' });
    img.addEventListener('load', () => img.classList.add('is-loaded'), { once: true });
    art.append(img);
  } else {
    art.append(h('div', { class: 'card-art-fallback' },
      h('span', { class: 't-mute', style: { fontSize: '10px' }, text: '···' })));
  }

  const actions = h('div', { class: 'download-actions' });

  if (job.status === 'needs_identification') {
    actions.append(h('button', { class: 'btn btn-primary btn-sm', type: 'button',
      onclick: async () => { if (await identifyDownload(job)) await refresh(); } }, 'Identify show'));
  }

  if (job.recoverable) {
    actions.append(h('button', {
      class: 'btn btn-outline btn-sm', type: 'button',
      onclick: async () => {
        try { await post(`/api/jobs/${job.id}/retry`); await refresh(); }
        catch (error) { toastError(error); }
      },
    }, 'Retry now'));
  }

  if (!terminal) {
    actions.append(h('button', {
      class: 'btn btn-outline btn-sm',
      type: 'button',
      disabled: job.status === 'cancelling',
      onclick: async (event) => {
        const button = event.currentTarget;
        const confirmed = await confirmDialog({
          title: 'Stop this download?',
          message: job.download_complete
            ? 'Finished local files will be kept. Files already uploaded remain in your library.'
            : 'Partial files will be removed. You can start it again from Search.',
          confirmLabel: 'Stop download',
        });
        if (!confirmed) return;
        button.disabled = true;
        try {
          await cancelDownload(job.id);
          await refresh();
        } catch (error) { toastError(error); button.disabled = false; }
      },
    }, job.status === 'cancelling' ? 'Stopping…' : 'Stop'));
  }

  if (job.status === 'complete') {
    const target = job.library_item_id ? `/${job.media_kind === 'tv' ? 'show' : 'movie'}/${job.library_item_id}` : (job.media_kind === 'tv' ? '/shows' : '/movies');
    actions.append(h('a', { class: 'btn btn-primary btn-sm', href: target },
      icon('play', { size: 15 }), job.media_kind === 'tv' ? 'View episodes' : 'Play'));
  }

  if (['failed', 'cancelled', 'interrupted'].includes(job.status)) {
    actions.append(h('a', {
      class: 'btn btn-outline btn-sm',
      href: `/search?q=${encodeURIComponent(displayTitle(job))}`,
    }, 'Find again'));
  }

  if (terminal) {
    actions.append(h('button', {
      class: 'icon-btn',
      type: 'button',
      'aria-label': `Remove ${displayTitle(job)} from history`,
      title: 'Remove from history',
      onclick: async () => {
        try {
          await del(`/api/jobs/${job.id}`);
          await refresh();
          showToast('Removed from history');
        } catch (error) { toastError(error); }
      },
    }, icon('trash', { size: 16, strokeWidth: 1.6 })));
  }

  const stats = [];
  const subtitles = job.subtitles_summary;
  if (subtitles?.downloaded) stats.push(h('span', { text: `${subtitles.downloaded} subtitle file${subtitles.downloaded === 1 ? '' : 's'} added` }));
  else if (subtitles?.status === 'not_configured') stats.push(h('a', { href: '/settings#subtitles', text: 'Connect subtitle account' }));
  else if (subtitles?.no_match) stats.push(h('span', { text: 'Matching subtitles unavailable' }));
  else if (subtitles?.message) stats.push(h('span', { text: subtitles.message }));
  if (!terminal) {
    stats.push(h('strong', { text: `${percent}%` }));
    if (job.speed && job.speed !== '—') stats.push(h('span', { text: job.speed }));
    if (job.eta && job.eta !== '—') stats.push(h('span', { text: `ETA ${job.eta}` }));
    if (job.downloaded && job.total) stats.push(h('span', { text: `${job.downloaded} of ${job.total}` }));
  } else {
    if (job.size) stats.push(h('span', { text: job.size }));
    if (job.source) stats.push(h('span', { text: job.source }));
    const when = relativeTime(new Date(job.created_at * 1000));
    if (when) stats.push(h('span', { text: when }));
  }

  return h('article', { class: 'download', dataset: { status: job.status, id: job.id } },
    art,
    h('div', { class: 'download-body' },
      h('div', { class: 'download-head' },
        h('div', { class: 'download-titles' },
          h('h3', { class: 'clamp-2', text: displayTitle(job) }),
          h('p', { text: stageDescription(job) }),
        ),
        actions,
      ),
      terminal ? null : h('div', { class: 'progress' }, h('i', { style: { width: `${percent}%` } })),
      h('div', { class: 'download-stats' },
        h('span', { class: `chip ${job.status === 'complete' ? 'chip-ok' : job.status === 'failed' ? 'chip-danger' : ''}`.trim() },
          STAGE_LABELS[job.status] || job.status),
        ...stats,
      ),
      terminal ? null : stageTrack(job),
    ),
  );
}

function render() {
  const visible = jobs;
  clear(list);
  replace(stateMount);

  countLabel.textContent = visible.length
    ? `${visible.length} ${scope === 'active' ? 'active' : 'in history'}`
    : '';

  if (!visible.length) {
    replace(stateMount, scope === 'active'
      ? statePanel({
        tone: 'empty',
        mark: 'downloads',
        title: 'Nothing downloading right now',
        message: 'Find a film and add it — progress will show up here, and it will appear in your library when it is ready.',
        actions: [{ label: 'Find something to watch', primary: true, onClick: () => { location.href = '/search'; } }],
      })
      : statePanel({
        tone: 'empty',
        mark: 'history',
        title: 'No download history yet',
        message: 'Everything you download is listed here, whether it finished or not.',
      }));
    return;
  }

  const fragment = document.createDocumentFragment();
  visible.forEach((job) => fragment.append(downloadRow(job)));
  list.append(fragment);
}

/* ── Data ───────────────────────────────────────────────────────────────── */

async function refresh() {
  try {
    const next = await loadJobs(scope);
    await resolvePosters(next);
    jobs = next;
    render();
    schedulePolling();
  } catch (error) {
    clear(list);
    replace(stateMount, errorPanel(error, { subject: 'your downloads', onRetry: refresh }));
  }
}

/** Poll only while something is actually moving. */
function schedulePolling() {
  clearTimeout(pollTimer);
  const active = jobs.some((job) => !isTerminal(job.status));
  if (!active || scope !== 'active') return;
  pollTimer = setTimeout(() => { if (!document.hidden) refresh(); else schedulePolling(); }, 2000);
}

function selectScope(next) {
  scope = next;
  tabActive.setAttribute('aria-selected', String(next === 'active'));
  tabHistory.setAttribute('aria-selected', String(next === 'history'));
  refresh();
}

tabActive.addEventListener('click', () => selectScope('active'));
tabHistory.addEventListener('click', () => selectScope('history'));

document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });

// Land on History if there is nothing running, so the page is never empty
// for someone who just wants to see what they downloaded.
(async () => {
  try {
    const active = await loadJobs('active');
    selectScope(active.length ? 'active' : 'history');
  } catch {
    selectScope('active');
  }
})();
