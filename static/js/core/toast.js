import { h } from './dom.js';
import { icon } from './icons.js';

/* Transient confirmations. Toasts confirm; they never carry information the
 * user has no other way to see. */

const ICON_FOR_TONE = { success: 'check', error: 'alert', warn: 'alert', info: 'info' };

function stack() {
  let node = document.getElementById('toast-stack');
  if (!node) {
    node = h('div', { class: 'toast-stack', id: 'toast-stack' });
    document.body.append(node);
  }
  return node;
}

export function showToast(message, { tone = 'success', action, duration = 4200 } = {}) {
  const node = h('div', { class: 'toast', dataset: { tone }, role: 'status' },
    icon(ICON_FOR_TONE[tone] || 'info', { size: 17 }),
    h('span', { class: 'toast-text', text: message }),
  );

  let timer;
  const dismiss = () => {
    clearTimeout(timer);
    node.classList.add('is-leaving');
    node.addEventListener('animationend', () => node.remove(), { once: true });
    setTimeout(() => node.remove(), 400);
  };

  if (action) {
    node.append(h('button', {
      class: 'toast-action',
      type: 'button',
      text: action.label,
      onclick: () => { dismiss(); action.onClick?.(); },
    }));
  }

  stack().append(node);
  if (duration) timer = setTimeout(dismiss, duration);
  node.addEventListener('click', (event) => {
    if (!event.target.closest('.toast-action')) dismiss();
  });
  return dismiss;
}

/** Shared rendering for a failed action, so retries look the same anywhere. */
export function toastError(error, { onRetry } = {}) {
  showToast(error?.message || 'Something went wrong.', {
    tone: 'error',
    action: onRetry ? { label: 'Retry', onClick: onRetry } : undefined,
    duration: onRetry ? 8000 : 5200,
  });
}
