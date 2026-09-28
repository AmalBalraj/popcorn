import { h } from '../core/dom.js';
import { icon } from '../core/icons.js';

/* Modal dialogs with a real focus trap, Escape-to-close and focus restore. */

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function openModal({ title, body, actions = [], wide = false, onClose, dismissible = true }) {
  const previouslyFocused = document.activeElement;
  const titleId = `modal-title-${Math.random().toString(36).slice(2, 8)}`;

  const close = () => {
    panel.remove();
    overlay.remove();
    document.body.style.removeProperty('overflow');
    if (previouslyFocused instanceof HTMLElement) previouslyFocused.focus();
    onClose?.();
  };

  const head = h('div', { class: 'modal-head' },
    h('h2', { id: titleId, text: title }),
    dismissible ? h('button', {
      class: 'icon-btn', type: 'button', 'aria-label': 'Close', onclick: close,
    }, icon('close', { size: 18 })) : null,
  );

  const bodyNode = h('div', { class: 'modal-body' });
  bodyNode.append(typeof body === 'function' ? body({ close }) : body);

  const panel = h('div', {
    class: `modal-panel ${wide ? 'is-wide' : ''}`.trim(),
    role: 'dialog',
    'aria-modal': 'true',
    'aria-labelledby': titleId,
  }, head, bodyNode);

  if (actions.length) {
    panel.append(h('div', { class: 'modal-foot' },
      actions.map((action) => h('button', {
        class: `btn ${action.primary ? 'btn-primary' : action.danger ? 'btn-danger' : 'btn-outline'}`,
        type: 'button',
        text: action.label,
        onclick: () => action.onClick?.({ close }),
      })),
    ));
  }

  const overlay = h('div', {
    class: 'modal',
    onclick: (event) => { if (dismissible && event.target === overlay) close(); },
  }, panel);

  overlay.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && dismissible) {
      event.stopPropagation();
      close();
    }
    if (event.key !== 'Tab') return;
    const items = [...panel.querySelectorAll(FOCUSABLE)].filter((node) => node.offsetParent !== null);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

  document.body.append(overlay);
  document.body.style.overflow = 'hidden';
  requestAnimationFrame(() => {
    const target = panel.querySelector('[data-autofocus]') || panel.querySelector(FOCUSABLE);
    target?.focus();
  });

  return { close, panel };
}

/** Promise-based confirmation for destructive actions. */
export function confirmDialog({ title, message, confirmLabel = 'Confirm', danger = true }) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const dialog = openModal({
      title,
      body: h('p', { class: 't-dim', text: message }),
      actions: [
        { label: 'Cancel', onClick: ({ close }) => { finish(false); close(); } },
        {
          label: confirmLabel,
          danger,
          primary: !danger,
          onClick: ({ close }) => { finish(true); close(); },
        },
      ],
      onClose: () => finish(false),
    });
    requestAnimationFrame(() => {
      dialog.panel.querySelector('.btn-danger, .btn-primary')?.focus();
    });
  });
}
