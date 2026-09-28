import { h } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { artFrame, SIZES } from '../core/art.js';
import { cardMeta, detailHref, playHref, progressPercent } from '../core/items.js';
import { watchlistButton } from '../core/watchlist.js';

/* A poster card.
 *
 * Deliberately restrained: artwork, title, one line of facts, and a progress
 * bar when there is progress to show. Badges are limited to one, and only
 * when it changes what the user should do next.
 */

export function card(item, { kind = 'poster', priority = false, flag = null, sizes, showActions = true } = {}) {
  const wide = kind === 'wide';
  const href = detailHref(item);
  const percent = progressPercent(item);
  const art = wide ? (item.thumb || item.backdrop || item.poster) : item.poster;

  const artBox = artFrame(art, {
    alt: '',
    sizes: sizes || (wide ? SIZES.wide : SIZES.row),
    priority,
    fallbackTitle: item.title,
    fallbackArt: wide ? item.poster : null,
  });

  if (percent > 0 && percent < 98) {
    artBox.append(h('div', { class: 'card-progress', 'aria-hidden': 'true' },
      h('i', { style: { width: `${percent}%` } }),
    ));
  }
  if (flag) {
    artBox.append(h('span', { class: `chip card-flag ${flag.tone ? `chip-${flag.tone}` : ''}`.trim() },
      flag.label,
    ));
  }

  const link = h('a', { class: 'card', href, 'aria-label': ariaLabel(item) },
    artBox,
    h('div', { class: 'card-body' },
      h('h3', { class: 'card-title clamp-2', text: item.title }),
      cardMeta(item) ? h('div', { class: 'card-meta' },
        h('span', { class: 'truncate', text: cardMeta(item) }),
      ) : null,
    ),
  );

  const holder = h('article', { class: `card-holder ${wide ? 'card-wide' : ''}`.trim() }, link);

  if (showActions) {
    const actions = h('div', { class: 'card-actions' },
      h('button', {
        class: 'btn-icon is-primary',
        type: 'button',
        title: percent > 1 ? 'Resume' : 'Play',
        'aria-label': `${percent > 1 ? 'Resume' : 'Play'} ${item.title}`,
        onclick: (event) => {
          event.preventDefault();
          event.stopPropagation();
          location.href = playHref(item);
        },
      }, icon(percent > 1 ? 'resume' : 'play', { size: 16 })),
      watchlistButton(item),
    );
    holder.append(h('div', { class: 'card-overlay', 'aria-hidden': 'false' }, actions));
  }

  return holder;
}

function ariaLabel(item) {
  const meta = cardMeta(item);
  const percent = progressPercent(item);
  const parts = [item.title];
  if (item.type === 'Episode') parts.push(`Episode ${item.index_number}`);
  if (meta) parts.push(meta);
  if (percent > 1) parts.push(`${percent}% watched`);
  return parts.join(', ');
}

/** A card-shaped placeholder, so rows and grids hold their layout. */
export function skeletonCard({ wide = false } = {}) {
  return h('div', { class: `skeleton-card ${wide ? 'is-wide' : ''}`.trim(), 'aria-hidden': 'true' },
    h('div', { class: 'skeleton skeleton-art' }),
    h('div', { class: 'skeleton skeleton-line' }),
    h('div', { class: 'skeleton skeleton-line short' }),
  );
}
