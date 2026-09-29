import { h, formatBytes, parseSize } from './dom.js';
import { icon } from './icons.js';
import { get, post } from './api.js';
import { openModal } from '../components/modal.js';
import { showToast, toastError } from './toast.js';

/* Downloads.
 *
 * Torrent mechanics stay out of the way: the viewer picks a title, then a
 * quality, and watches a stage rather than a status code. Everything below
 * the resolution — codecs, swarms, release names — lives behind Advanced.
 */

/** The journey a download takes, in the order a person experiences it. */
export const STAGES = ['queued', 'resolving', 'downloading', 'identifying', 'subtitles', 'uploading', 'indexing', 'complete'];

export const STAGE_LABELS = {
  queued: 'Queued',
  resolving: 'Finding sources',
  downloading: 'Downloading',
  identifying: 'Organizing episodes',
  subtitles: 'Finding subtitles',
  needs_identification: 'Choose TV show',
  uploading: 'Moving to your library',
  indexing: 'Almost ready',
  complete: 'Ready to watch',
  cancelling: 'Stopping',
  cancelled: 'Stopped',
  interrupted: 'Interrupted',
  failed: "Couldn't finish",
};

/** Short, human explanation of what is happening right now. */
export function stageDescription(job) {
  if (job.recoverable && job.retry_at) return job.message || 'Waiting to retry this stage.';
  switch (job.status) {
    case 'queued': return 'Waiting to start.';
    case 'resolving': return 'Looking for the best available sources.';
    case 'downloading': return job.speed && job.speed !== '—'
      ? `Downloading at ${job.speed}${job.eta && job.eta !== '—' ? ` · ${job.eta} remaining` : ''}`
      : 'Downloading…';
    case 'identifying': return 'Matching the show and organizing its seasons and episodes.';
    case 'subtitles': return job.message || 'Finding subtitles that match your video release.';
    case 'needs_identification': return job.message || 'Choose the correct TV show. Finished files are kept.';
    case 'uploading': return 'Sending the finished file to your library.';
    case 'indexing': return 'Your media server is adding it to the library.';
    case 'complete': return 'Ready to play in your library.';
    case 'cancelling': return 'Stopping and cleaning up partial files.';
    case 'cancelled': return job.message || 'Stopped.';
    case 'interrupted': return job.message || 'The server restarted before this finished.';
    case 'failed': return job.message || 'Something went wrong.';
    default: return job.message || '';
  }
}

export function stageIndex(status) {
  const index = STAGES.indexOf(status);
  if (index !== -1) return index;
  if (status === 'cancelling') return 2;
  if (status === 'needs_identification') return 3;
  if (status === 'complete') return STAGES.length - 1;
  return -1;
}

export function isTerminal(status) {
  return ['complete', 'failed', 'interrupted', 'cancelled'].includes(status);
}

export function stageTrack(job) {
  const current = stageIndex(job.status);
  const failed = ['failed', 'cancelled', 'interrupted'].includes(job.status);
  const track = h('div', {
    class: `stage-track ${failed ? 'is-failed' : ''}`.trim(),
    role: 'progressbar',
    'aria-valuemin': '0',
    'aria-valuemax': String(STAGES.length),
    'aria-valuenow': String(Math.max(0, current)),
    'aria-label': STAGE_LABELS[job.status] || 'Progress',
  });
  STAGES.forEach((stage, index) => {
    const state = failed && index === Math.max(current, 0) ? 'is-current'
      : index < current ? 'is-done'
        : index === current ? 'is-current' : '';
    track.append(h('i', { class: state }));
  });
  return track;
}

/** "1080p · BluRay · 1.9 GB" — the facts people actually compare. */
export function releaseFacts(release) {
  return [release.resolution, release.source, formatBytes(release.size_bytes || parseSize(release.size))]
    .filter(Boolean)
    .join(' · ');
}

export function releaseSummary(release) {
  return releaseFacts(release) || release.title;
}

/**
 * Ask which version to download, then start it.
 *
 * The best release is preselected, so the common path is one click.
 */
export async function chooseReleaseAndDownload({ title, year, releases, searchId, poster }) {
  const byResolution = new Map();
  for (const release of releases) {
    const key = release.resolution || 'Other';
    if (!byResolution.has(key)) byResolution.set(key, []);
    byResolution.get(key).push(release);
  }

  let chosen = releases[0];
  const identity = tvIdentityFields(title, releases.some(r => ['tv', 'episode', 'season'].includes(r.media_type)) ? 'tv' : 'auto');

  return new Promise((resolve) => {
    const dialog = openModal({
      title: `Download ${title}`,
      wide: true,
      body: ({ close }) => {
        const fragment = document.createDocumentFragment();
        fragment.append(identity.node);

        fragment.append(h('p', {
          class: 't-dim',
          style: { marginBottom: 'var(--s5)', fontSize: 'var(--t-sm)' },
          text: `Choose a version. ${releases.length} releases found${year ? ` for ${year}` : ''}.`,
        }));

        for (const [resolution, group] of byResolution) {
          const list = h('div', { class: 'release-list', role: 'radiogroup', 'aria-label': resolution });
          group.slice(0, 6).forEach((release) => {
            const option = h('button', {
              class: 'release-option',
              type: 'button',
              role: 'radio',
              'aria-checked': String(release.id === chosen.id),
              onclick: () => {
                chosen = release;
                list.querySelectorAll('.release-option').forEach((node) => {
                  node.setAttribute('aria-checked', String(node === option));
                });
              },
            },
              h('span', { class: 'release-radio', 'aria-hidden': 'true' }),
              h('span', { class: 'release-main' },
                h('span', { class: 'release-title', text: releaseFacts(release) || 'Standard' }),
                h('span', { class: 'release-sub' },
                  release.seeds > 0
                    ? `${release.seeds} seeders`
                    : 'No seeders right now',
                  release.audio ? h('span', { text: release.audio }) : null,
                  release.season_pack ? h('span', { class: 'chip chip-plain' }, 'Season pack') : null,
                ),
              ),
              h('span', { class: 'release-side' },
                h('strong', { text: formatBytes(release.size_bytes) || release.size }),
                h('span', { text: release.source || '' }),
              ),
            );
            list.append(option);
          });

          fragment.append(h('div', { class: 'quality-group' },
            h('h3', { text: resolution }),
            list,
          ));
        }

        fragment.append(h('details', { class: 'details-disclosure', style: { marginTop: 'var(--s5)' } },
          h('summary', { text: 'Advanced release details' }),
          h('pre', { text: releases.map((release, index) =>
            `${index + 1}. ${release.title}\n   ${release.seeds} seeds · ${release.peers} peers · ${release.size} · ${release.source}`,
          ).join('\n\n') }),
        ));

        return fragment;
      },
      actions: [
        { label: 'Cancel', onClick: ({ close }) => { close(); resolve(null); } },
        {
          label: 'Start download',
          primary: true,
          onClick: async ({ close }) => {
            const button = dialog.panel.querySelector('.btn-primary');
            button.disabled = true;
            const job = await startDownload({ searchId, release: chosen, title, poster, identity: identity.value() });
            if (job) { resolve(job); close(); }
            else button.disabled = false;
          },
        },
      ],
      onClose: () => resolve(null),
    });
    void dialog;
  });
}

export async function startDownload({ searchId, release, title, poster, identity = {} }) {
  try {
    const job = await post('/api/download', {
      search_id: searchId,
      result_id: release.id,
      poster: poster || '',
      ...identity,
    });
    showToast(`Downloading ${title}`, {
      tone: 'success',
      action: { label: 'View', onClick: () => { location.href = '/downloads'; } },
    });
    return job;
  } catch (error) {
    toastError(error, {
      onRetry: () => startDownload({ searchId, release, title, poster, identity }),
    });
    return null;
  }
}

export async function cancelDownload(jobId) {
  return post(`/api/jobs/${jobId}/cancel`);
}

export async function loadJobs(scope = 'all') {
  const data = await get(`/api/jobs?scope=${scope}`);
  return data.jobs || [];
}

/** Small inline icon button used by download rows. */
export function actionButton({ name, label, onClick, primary = false, danger = false, iconName }) {
  return h('button', {
    class: `btn ${primary ? 'btn-primary' : danger ? 'btn-danger' : 'btn-outline'} btn-sm`,
    type: 'button',
    title: label,
    onclick: onClick,
  }, iconName ? icon(iconName, { size: 15 }) : null, name || label);
}


function tvIdentityFields(title, initialKind = 'tv') {
  const kind = h('select', { class: 'input', 'aria-label': 'Media type' },
    h('option', { value: 'auto', text: 'Detect automatically' }),
    h('option', { value: 'tv', text: 'TV show' }),
    h('option', { value: 'movie', text: 'Movie' }));
  kind.value = initialKind;
  const query = h('input', { class: 'input', type: 'search', value: title.replace(/[._]/g, ' ').split(/\bS\d|\bSeason\b|\b20\d{2}\b/i)[0].trim(),
    placeholder: 'TV show name', 'aria-label': 'TV show name' });
  const season = h('input', { class: 'input', type: 'number', min: 0, max: 99,
    placeholder: 'Season, if filenames omit it', 'aria-label': 'Season for files without season numbers' });
  const matches = h('select', { class: 'input', 'aria-label': 'Choose the correct TV show', hidden: true });
  const notice = h('p', { class: 't-dim t-sm' });
  const lookup = h('button', { class: 'btn btn-outline btn-sm', type: 'button', text: 'Find show', onclick: async () => {
    lookup.disabled = true;
    try {
      const data = await get(`/api/tv/search?q=${encodeURIComponent(query.value)}`);
      replaceSelect(data.shows);
      notice.textContent = data.shows.length ? 'Choose the matching show. Releases from other sources will join this series.' : 'No matching show found. Try a different name.';
    } catch (error) { notice.textContent = error.message; }
    finally { lookup.disabled = false; }
  } });
  const replaceSelect = (shows) => {
    matches.replaceChildren(h('option', { value: '', text: 'Choose a show…' }),
      ...shows.map(show => h('option', { value: show.id, text: `${show.title}${show.year ? ` (${show.year})` : ''}` })));
    matches.hidden = !shows.length;
  };
  query.addEventListener('input', () => { matches.value = ''; matches.hidden = true; });
  const tv = h('div', { style: { display: 'grid', gap: 'var(--s3)', marginTop: 'var(--s3)' } }, query, lookup, matches, season, notice);
  const update = () => { tv.hidden = kind.value !== 'tv'; };
  kind.addEventListener('change', update);
  update();
  const node = h('div', { style: { marginBottom: 'var(--s5)' } }, h('label', { class: 't-label', text: 'Add to library as' }), kind, tv);
  return { node, value: () => ({ media_kind: kind.value, show_query: query.value,
    show_tmdb_id: matches.value || null, tv_season: season.value === '' ? null : Number(season.value) }) };
}

export function identifyDownload(job) {
  const identity = tvIdentityFields(job.display_title || job.title);
  const fileFields = (job.identification_files || []).map(name => {
    const season = h('input', { class: 'input', type: 'number', min: 0, max: 99, placeholder: 'Season', 'aria-label': `Season for ${name}` });
    const episode = h('input', { class: 'input', type: 'number', min: 1, max: 999, placeholder: 'Episode', 'aria-label': `Episode for ${name}` });
    identity.node.append(h('div', { style: { marginTop: 'var(--s3)' } }, h('p', { class: 't-sm', text: name }), season, episode));
    return { name, season, episode };
  });
  return new Promise(resolve => {
    openModal({ title: 'Identify TV show', body: identity.node,
      actions: [{ label: 'Cancel', onClick: ({ close }) => close() }, {
        label: 'Import episodes', primary: true, onClick: async ({ close }) => {
          try {
            const value = identity.value();
            value.episode_overrides = Object.fromEntries(fileFields.filter(f => f.season.value !== '' && f.episode.value !== '')
              .map(f => [f.name, [Number(f.season.value), Number(f.episode.value), null]]));
            await post(`/api/jobs/${job.id}/identify`, value); resolve(true); close();
          }
          catch (error) { toastError(error); }
        },
      }], onClose: () => resolve(false) });
  });
}
