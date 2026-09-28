import { post } from './api.js';
import { showToast, toastError } from './toast.js';
import { icon } from './icons.js';

/* Watchlist toggling is optimistic: the icon flips immediately and reverts if
 * the server disagrees. Shared by cards, detail pages and the player. */

const pending = new Set();

export function watchlistButton(item, { className = '' } = {}) {
  const active = Boolean(item.user?.favorite);
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `btn-icon ${className}`.trim();
  button.classList.toggle('is-on', active);
  button.dataset.watchlist = item.id;
  button.setAttribute('aria-pressed', String(active));
  button.title = active ? 'Remove from watchlist' : 'Add to watchlist';
  button.setAttribute('aria-label', button.title);
  button.append(icon('bookmark', { size: 17 }));
  button.addEventListener('click', (event) => {
    event.preventDefault();
    event.stopPropagation();
    toggleWatchlist(item, button);
  });
  return button;
}

export async function toggleWatchlist(item, button) {
  if (pending.has(item.id)) return;
  pending.add(item.id);

  const next = !button.classList.contains('is-on');
  const apply = (value) => {
    button.classList.toggle('is-on', value);
    button.setAttribute('aria-pressed', String(value));
    button.title = value ? 'Remove from watchlist' : 'Add to watchlist';
    button.setAttribute('aria-label', button.title);
    // Keep any other card for the same title in step.
    document.querySelectorAll(`[data-watchlist="${item.id}"]`).forEach((peer) => {
      if (peer !== button) {
        peer.classList.toggle('is-on', value);
        peer.setAttribute('aria-pressed', String(value));
      }
    });
    if (item.user) item.user.favorite = value;
  };

  apply(next);
  try {
    await post(`/api/item/${item.id}/favorite`, { favorite: next });
    showToast(next ? 'Added to your watchlist' : 'Removed from your watchlist');
  } catch (error) {
    apply(!next);
    toastError(error);
  } finally {
    pending.delete(item.id);
  }
}
