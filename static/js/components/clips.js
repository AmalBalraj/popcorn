import { get, post } from '../core/api.js';
import { h, replace, clear, relativeTime, joinMeta } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { openModal, confirmDialog } from '../components/modal.js';
import { showToast, toastError } from '../core/toast.js';

/* AI clips, on the movie page.
 *
 * Generating clips is a background job on the server — subtitles, a model,
 * ffmpeg, then an upload — so this section never waits for one: it starts the
 * job, then polls while the page is open and picks the result up whenever the
 * viewer comes back, even days later. Nothing here blocks on the work.
 */

const POLL_MS = 2500;

/** The job's own stage names, said the way a viewer would say them. */
const STATE_LABELS = {
  QUEUED: 'Queued',
  EXTRACTING_SUBTITLES: 'Analyzing',
  TRANSCRIBING: 'Analyzing',
  ANALYZING_TRANSCRIPT: 'Analyzing',
  FINDING_SCENES: 'Analyzing',
  RANKING_SCENES: 'Analyzing',
  GENERATING_CLIPS: 'Generating',
  UPLOADING: 'Saving',
  COMPLETED: 'Completed',
  FAILED: 'Failed',
  CANCELLED: 'Stopped',
  cancelling: 'Stopping',
};

const WORKING_STATES = new Set([
  'QUEUED', 'EXTRACTING_SUBTITLES', 'TRANSCRIBING', 'ANALYZING_TRANSCRIPT',
  'FINDING_SCENES', 'RANKING_SCENES', 'GENERATING_CLIPS', 'UPLOADING', 'cancelling',
]);

const CATEGORY_LABELS = {
  tension: 'Tension', humour: 'Funny', iconic: 'Iconic', conflict: 'Conflict',
  reveal: 'Reveal', twist: 'Twist', suspense: 'Suspense', inspirational: 'Inspiring',
  romance: 'Romance', action: 'Action', emotional: 'Emotional', other: 'Moment',
};

/** 01:12:31 — clip ranges read as positions in the film, not durations. */
function clock(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const pad = (value) => String(value).padStart(2, '0');
  return `${pad(Math.floor(total / 3600))}:${pad(Math.floor((total % 3600) / 60))}:${pad(total % 60)}`;
}

function shortDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

function stateLabel(data) {
  const state = data?.state;
  if (!state) return 'Not generated';
  if (state === 'GENERATING_CLIPS') {
    const total = data.total || data.settings?.target_count || 0;
    return total ? `Generating ${data.done || 0} / ${total}` : 'Generating';
  }
  return STATE_LABELS[state] || state;
}

/** The line under the status: what is happening, in the server's own words. */
function statusDetail(data) {
  if (!data?.state) {
    return data?.llm_configured === false
      ? 'Clip generation needs a model credential on the server.'
      : 'Popcorn can read this film’s subtitles and cut the moments worth sharing.';
  }
  if (data.state === 'COMPLETED') {
    return `${data.clips.length} clip${data.clips.length === 1 ? '' : 's'} · ${data.message}`;
  }
  return data.message || STATE_LABELS[data.state] || '';
}

function clipCard(clip) {
  const art = h('div', { class: 'clip-art' });
  if (clip.thumb_url) {
    const image = h('img', { src: clip.thumb_url, alt: '', loading: 'lazy', decoding: 'async' });
    image.addEventListener('load', () => image.classList.add('is-loaded'), { once: true });
    art.append(image);
  } else {
    art.append(h('div', { class: 'card-art-fallback' }, icon('film', { size: 22 })));
  }
  art.append(h('span', { class: 'clip-length', text: shortDuration(clip.duration) }));
  art.append(h('button', {
    class: 'clip-play', type: 'button',
    'aria-label': `Play ${clip.title}`,
    onclick: () => playClip(clip),
  }, icon('play', { size: 20 })));

  const score = Math.round(clip.scores?.interest ?? clip.scores?.final ?? 0);
  const meta = [
    h('span', { class: 'chip chip-plain' }, CATEGORY_LABELS[clip.category] || 'Moment'),
    h('span', { class: 'clip-score' },
      icon('star', { size: 12 }), `Interesting score: ${score}`),
  ];

  const why = [clip.summary, clip.reason].filter(Boolean);

  return h('article', { class: 'clip-card' },
    art,
    h('div', { class: 'clip-body' },
      h('span', { class: 'clip-time', text: `${clock(clip.start)} → ${clock(clip.end)}` }),
      h('h3', { class: 'clip-title clamp-2', text: clip.title }),
      clip.hook ? h('p', { class: 'clip-hook clamp-2', text: `“${clip.hook}”` }) : null,
      h('div', { class: 'clip-meta' }, ...meta),
      why.length ? h('details', { class: 'clip-why' },
        h('summary', { text: 'Why this clip' }),
        h('p', { text: why.join(' ') }),
      ) : null,
      h('div', { class: 'clip-actions' },
        h('button', { class: 'btn btn-secondary btn-sm', type: 'button', onclick: () => playClip(clip) },
          icon('play', { size: 15 }), 'Play'),
        h('a', {
          class: 'btn btn-outline btn-sm', href: clip.download_url, download: '',
          title: `Download ${clip.title}`,
        }, icon('download', { size: 15 }), 'Download'),
      ),
    ),
  );
}

function playClip(clip) {
  const video = h('video', {
    class: 'clip-player',
    src: clip.stream_url,
    controls: true,
    autoplay: true,
    playsinline: true,
    preload: 'auto',
    poster: clip.thumb_url || undefined,
  });
  const dialog = openModal({
    title: clip.title,
    wide: true,
    body: h('div', { class: 'clip-modal' },
      video,
      h('p', { class: 'clip-modal-meta' },
        `${clock(clip.start)} → ${clock(clip.end)}`,
        clip.hook ? h('span', { class: 'clip-modal-hook', text: ` · “${clip.hook}”` }) : null),
      clip.summary ? h('p', { class: 't-dim', text: clip.summary }) : null,
    ),
    actions: [
      { label: 'Download', onClick: () => { window.location.href = clip.download_url; } },
    ],
    onClose: () => { video.pause(); video.removeAttribute('src'); video.load(); },
  });
  // Autoplay is a request, not a right: browsers with sound blocked should
  // still show a clip that starts when the viewer presses play.
  video.play?.().catch(() => {});
  requestAnimationFrame(() => video.focus?.());
  return dialog;
}

export function clipsSection({ itemId }) {
  const section = h('section', { class: 'detail-block clips-block', id: 'clips' });
  const head = h('div', { class: 'clips-head' },
    h('div', {},
      h('h2', { text: 'AI clips' }),
      h('p', { class: 'clips-blurb',
        text: 'Short, self-contained moments from this film, found by reading its subtitles.' }),
    ),
  );
  const statusRow = h('div', { class: 'clips-status' });
  const list = h('div', { class: 'clip-grid' });

  let data = null;
  let busy = false;

  async function start(regenerate) {
    if (busy) return;
    if (regenerate) {
      const confirmed = await confirmDialog({
        title: 'Regenerate clips?',
        message: 'The clips saved for this film will be replaced. The scenes already found for it are '
          + 'reused, so this is quick and costs nothing extra.',
        confirmLabel: 'Regenerate',
        danger: false,
      });
      if (!confirmed) return;
    }
    busy = true;
    render();
    try {
      await post(`/api/clips/${itemId}/generate`, {});
      showToast('Generating clips in the background — you can leave this page', {
        tone: 'success',
      });
      await load();
    } catch (error) {
      toastError(error);
    } finally {
      busy = false;
      render();
    }
  }

  async function stop() {
    const jobId = data?.job_id;
    if (!jobId) return;
    try {
      await post(`/api/jobs/${jobId}/cancel`, {});
      showToast('Stopping clip generation');
      await load();
    } catch (error) { toastError(error); }
  }

  function render() {
    if (!data) {
      replace(statusRow, h('span', { class: 'chip' }, 'Checking…'));
      return;
    }
    const working = WORKING_STATES.has(data.state) || busy;
    const hasClips = data.clips.length > 0;
    const blocked = !data.can_generate && !working;

    const chipClass = data.state === 'COMPLETED' ? 'chip-ok'
      : data.state === 'FAILED' ? 'chip-danger'
        : working ? 'chip-warn' : '';

    const actions = [];
    if (working) {
      if (data.can_stop) {
        actions.push(h('button', {
          class: 'btn btn-outline', type: 'button', onclick: stop,
        }, icon('close', { size: 16 }), 'Stop'));
      }
    } else {
      actions.push(h('button', {
        class: `btn ${hasClips ? 'btn-secondary' : 'btn-primary'}`,
        type: 'button',
        // Nothing to offer when the server has said why it cannot cut clips.
        disabled: blocked,
        onclick: () => start(hasClips),
      }, icon(hasClips ? 'refresh' : 'film', { size: 16 }),
      hasClips ? 'Regenerate clips' : 'Generate clips'));
    }

    replace(statusRow,
      h('div', { class: 'clips-status-main' },
        h('span', { class: `chip ${chipClass}`.trim() }, stateLabel(data)),
        h('span', { class: 'clips-status-text', text: statusDetail(data) }),
        // The raw cause belongs behind a disclosure, never in front of it.
        data.detail && data.state === 'FAILED'
          ? h('span', { class: 'clips-detail', text: data.detail })
          : null,
      ),
      h('div', { class: 'clips-actions' }, ...actions),
      working && data.total
        ? h('div', { class: 'progress clips-progress' },
          h('i', { style: { width: `${Math.round((data.done / data.total) * 100)}%` } }))
        : (working ? h('div', { class: 'progress is-indeterminate clips-progress' }, h('i')) : null),
      blocked && data.reason
        ? h('p', { class: 'clips-reason', text: data.reason })
        : null,
      data.notes?.length
        ? h('details', { class: 'clips-notes' },
          h('summary', { text: 'What happened' }),
          h('ul', {}, data.notes.map((note) => h('li', { text: note }))))
        : null,
    );

    clear(list);
    if (hasClips) {
      data.clips.forEach((clip) => list.append(clipCard(clip)));
      if (data.analysis?.transcript_detail) {
        list.append(h('p', { class: 'clips-source',
          text: joinMeta([
            `Scenes found from ${data.analysis.transcript_detail}`,
            data.analysis.updated_at ? `analysed ${relativeTime(new Date(data.analysis.updated_at * 1000))}` : null,
          ]) }));
      }
    }
  }

  async function load() {
    try {
      data = await get(`/api/clips/${itemId}`);
    } catch (error) {
      if (error.notFound) {
        // Not a title this server can cut clips from: say nothing at all.
        section.hidden = true;
        return;
      }
      section.hidden = false;
      replace(statusRow, h('span', { class: 'clips-status-text', text: error.message }));
      return;
    }
    section.hidden = false;
    render();
    schedule();
  }

  let timer;
  function schedule() {
    clearTimeout(timer);
    if (!data || !WORKING_STATES.has(data.state)) return;
    // Poll while the viewer is here and the job is moving; a hidden tab asks
    // again the moment it is looked at, rather than every few seconds forever.
    timer = setTimeout(() => {
      if (document.hidden) schedule();
      else load();
    }, POLL_MS);
  }

  document.addEventListener('visibilitychange', () => {
    // A section that has been re-rendered away must not keep polling.
    if (!section.isConnected) return;
    if (!document.hidden && data && WORKING_STATES.has(data.state)) load();
  });

  section.append(head, statusRow, list);
  load();
  return section;
}
