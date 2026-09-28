import { h } from '../core/dom.js';
import { icon, ICON_NAMES } from '../core/icons.js';

/* Empty, error and unavailable states.
 *
 * The message always says what happened and what to do next. Technical causes
 * stay behind a disclosure — available, never in the way.
 */

/* Named marks map to icon-registry entries; anything else falls through to a
   sensible default rather than rendering an empty circle. */
const MARKS = {
  empty: 'inbox',
  error: 'alert',
  offline: 'offline',
  unavailable: 'film',
  search: 'search',
  downloads: 'download',
  shows: 'tv',
  episodes: 'tv',
  library: 'library',
  history: 'history',
  bookmark: 'bookmark',
  film: 'film',
};

export function statePanel({
  tone = 'empty',
  mark,
  title,
  message,
  actions = [],
  detail = '',
  compact = false,
}) {
  const panel = h('div', { class: 'state', dataset: { tone }, style: compact ? { padding: 'var(--s7) var(--s4)' } : null });

  const markName = MARKS[mark] || (mark && ICON_NAMES.has(mark) ? mark : null) || MARKS[tone] || 'info';
  panel.append(h('div', { class: 'state-mark' }, icon(markName, { size: 24 })));
  panel.append(h('h2', { text: title }));
  if (message) panel.append(h('p', { text: message }));

  if (actions.length) {
    panel.append(h('div', { class: 'state-actions' },
      actions.map((action) => h('button', {
        class: `btn ${action.primary ? 'btn-primary' : 'btn-outline'}`,
        type: 'button',
        text: action.label,
        onclick: action.onClick,
      })),
    ));
  }

  if (detail) {
    panel.append(h('details', { class: 'details-disclosure' },
      h('summary', { text: 'Technical details' }),
      h('pre', { text: detail }),
    ));
  }
  return panel;
}

/** Maps an ApiError onto the right reassuring message. */
export function errorPanel(error, { onRetry, subject = 'your library' } = {}) {
  if (error?.offline) {
    return statePanel({
      tone: 'offline',
      title: "You're offline",
      message: 'Reconnect and try again — nothing has been lost.',
      detail: error.detail,
      actions: onRetry ? [{ label: 'Try again', primary: true, onClick: onRetry }] : [],
    });
  }
  if (error?.serverDown) {
    return statePanel({
      tone: 'error',
      title: "Couldn't reach your media server",
      message: `Popcorn is running, but ${subject} isn't responding right now. Downloads and search still work.`,
      detail: error.detail || error.message,
      actions: onRetry ? [{ label: 'Retry', primary: true, onClick: onRetry }] : [],
    });
  }
  return statePanel({
    tone: 'error',
    title: 'Something went wrong',
    message: error?.message || 'That could not be loaded.',
    detail: error?.detail,
    actions: onRetry ? [{ label: 'Try again', primary: true, onClick: onRetry }] : [],
  });
}

/** Inline placeholder used while a page's first payload is in flight. */
export function loadingPanel(label = 'Loading…') {
  return h('div', { class: 'state' },
    h('div', { class: 'state-mark' }, h('span', { class: 'spinner' })),
    h('p', { class: 't-dim', text: label }),
  );
}
