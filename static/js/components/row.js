import { h } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { card, skeletonCard } from './card.js';

/* A horizontally scrolling shelf.
 *
 * The track bleeds to the viewport edge while its first card stays aligned
 * with the page gutter. Arrow buttons only appear for pointer devices; touch
 * users swipe, and the snap points keep that feeling deliberate.
 */

export function row({ title, items, kind = 'poster', href = null, hrefLabel = 'See all', priorityCount = 0, flagFor = null }) {
  const wide = kind === 'wide';
  const track = h('div', {
    class: 'row-track',
    role: 'list',
    tabindex: '-1',
  });

  items.forEach((item, index) => {
    const node = card(item, {
      kind,
      priority: index < priorityCount,
      flag: flagFor ? flagFor(item) : null,
    });
    node.setAttribute('role', 'listitem');
    track.append(node);
  });

  const prev = h('button', {
    class: 'row-nav prev', type: 'button', 'aria-label': `Scroll ${title} left`,
    onclick: () => scrollBy(-1),
  }, icon('chevronLeft', { size: 18 }));

  const next = h('button', {
    class: 'row-nav next', type: 'button', 'aria-label': `Scroll ${title} right`,
    onclick: () => scrollBy(1),
  }, icon('chevronRight', { size: 18 }));

  function scrollBy(direction) {
    const amount = Math.max(240, track.clientWidth * 0.86) * direction;
    track.scrollBy({ left: amount, behavior: 'smooth' });
  }

  function syncArrows() {
    const maxScroll = track.scrollWidth - track.clientWidth;
    prev.disabled = track.scrollLeft <= 4;
    next.disabled = track.scrollLeft >= maxScroll - 4;
  }

  track.addEventListener('scroll', () => requestAnimationFrame(syncArrows), { passive: true });
  // Card widths settle after images decode; re-check once the row is laid out.
  requestAnimationFrame(syncArrows);
  if ('ResizeObserver' in window) new ResizeObserver(syncArrows).observe(track);

  const header = h('div', { class: 'row-head' },
    h('h2', { text: title }),
    href ? h('a', { class: 'row-link', href, text: hrefLabel }) : null,
  );

  return h('section', { class: 'row', 'aria-label': title },
    header,
    h('div', { class: 'bleed', style: { position: 'relative' } }, prev, track, next),
  );
}

export function skeletonRow({ wide = false, count = 6 } = {}) {
  const track = h('div', { class: 'row-track', 'aria-hidden': 'true' });
  for (let index = 0; index < count; index += 1) track.append(skeletonCard({ wide }));
  return h('section', { class: 'row' },
    h('div', { class: 'row-head' }, h('div', { class: 'skeleton skeleton-title' })),
    h('div', { class: 'bleed' }, track),
  );
}
