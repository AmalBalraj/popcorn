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
export const STAGES = ['queued', 'resolving', 'downloading', 'uploading', 'indexing', 'complete'];

export const STAGE_LABELS = {
  queued: 'Queued',
  resolving: 'Finding sources',
  downloading: 'Downloading',
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
  switch (job.status) {
    case 'queued': return 'Waiting to start.';
    case 'resolving': return 'Looking for the best available sources.';
    case 'downloading': return job.speed && job.speed !== '—'
      ? `Downloading at ${job.speed}${job.eta && job.eta !== '—' ? ` · ${job.eta} remaining` : ''}`
      : 'Downloading…';
    case 'uploading': return 'Sending the finished file to your library.';
    case 'indexing': return 'Your media server is adding it to the library.';
    case 'complete': return 'Ready to play in your library.';
    case 'cancelling': return 'Stopping and cleaning up partial files.';
    case 'cancelled': return 'Stopped. Partial files were removed.';
    case 'interrupted': return 'The server restarted before this finished.';
    case 'failed': return job.message || 'Something went wrong.';
    default: return job.message || '';
  }
}

export function stageIndex(status) {
  const index = STAGES.indexOf(status);
  if (index !== -1) return index;
  if (status === 'cancelling') return 2;
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
  if (releases.length === 1) {
    return startDownload({ searchId, release: releases[0], title, poster });
  }

  const byResolution = new Map();
  for (const release of releases) {
    const key = release.resolution || 'Other';
    if (!byResolution.has(key)) byResolution.set(key, []);
    byResolution.get(key).push(release);
  }

  let chosen = releases[0];

  return new Promise((resolve) => {
    const dialog = openModal({
      title: `Download ${title}`,
      wide: true,
      body: ({ close }) => {
        const fragment = document.createDocumentFragment();

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
            close();
            resolve(await startDownload({ searchId, release: chosen, title, poster }));
          },
        },
      ],
      onClose: () => resolve(null),
    });
    void dialog;
  });
}

export async function startDownload({ searchId, release, title, poster }) {
  try {
    const job = await post('/api/download', {
      search_id: searchId,
      result_id: release.id,
      poster: poster || '',
    });
    showToast(`Downloading ${title}`, {
      tone: 'success',
      action: { label: 'View', onClick: () => { location.href = '/downloads'; } },
    });
    return job;
  } catch (error) {
    toastError(error, {
      onRetry: () => startDownload({ searchId, release, title, poster }),
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
