import { h, formatRuntime, joinMeta } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { artImage, SIZES } from '../core/art.js';
import { detailHref, infoParts, isResumable, playHref, progressPercent, remainingLabel } from '../core/items.js';
import { watchlistButton } from '../core/watchlist.js';

/* The featured band at the top of Home.
 *
 * One title is presented at a time with real wide art behind it. Rotation is
 * slow, pauses the moment the viewer shows interest, and is disabled entirely
 * when they have asked for reduced motion.
 */

const ROTATE_MS = 9000;

export function heroSection(items, { onSelect } = {}) {
  if (!items?.length) return null;

  const backdropLayer = h('div', { class: 'hero-backdrop', 'aria-hidden': 'true' });
  const backdrops = items.map((item, index) => {
    const img = artImage(item.backdrop, { alt: '', sizes: SIZES.backdrop, priority: index === 0 });
    if (img) backdropLayer.append(img);
    return img;
  });

  const content = h('div', { class: 'hero-inner' });
  const dots = h('div', { class: 'hero-dots', role: 'tablist', 'aria-label': 'Featured titles' });

  const section = h('section', { class: 'hero', 'aria-label': 'Featured' },
    backdropLayer, content, items.length > 1 ? dots : null,
  );

  let index = -1;
  let timer;
  let userEngaged = false;

  const buttons = items.map((item, position) => h('button', {
    type: 'button',
    role: 'tab',
    'aria-label': item.title,
    'aria-current': 'false',
    onclick: () => { engage(); show(position); },
  }));
  buttons.forEach((button) => dots.append(button));

  function engage() {
    userEngaged = true;
    clearTimeout(timer);
  }

  function show(next) {
    if (next === index) return;
    index = next;
    const item = items[index];

    backdrops.forEach((img, position) => img?.classList.toggle('is-loaded', position === index));
    buttons.forEach((button, position) => {
      button.setAttribute('aria-current', String(position === index));
    });

    const resumable = isResumable(item);
    const percent = progressPercent(item);
    const parts = infoParts(item);

    replaceContent(content, [
      item.logo
        ? h('h1', { class: 'sr-only', text: item.title })
        : h('h1', { class: 'hero-title', text: item.title }),
      item.logo
        ? h('img', { class: 'hero-logo', src: item.logo.src, srcset: item.logo.srcset, alt: '', fetchpriority: 'high' })
        : null,

      h('div', { class: 'hero-meta' },
        item.community_rating
          ? h('span', { class: 'rating' }, icon('star', { size: 13 }), item.community_rating.toFixed(1))
          : null,
        ...parts.map((part) => h('span', { text: part })),
      ),

      item.overview ? h('p', { class: 'hero-overview clamp-3', text: item.overview }) : null,

      resumable ? h('div', { class: 'hero-progress' },
        h('div', { class: 'progress' }, h('i', { style: { width: `${percent}%` } })),
        h('div', { class: 'hero-progress-label', text: joinMeta([`${percent}% watched`, remainingLabel(item)]) }),
      ) : null,

      h('div', { class: 'hero-actions' },
        h('a', {
          class: 'btn btn-primary btn-lg',
          href: playHref(item),
          onclick: engage,
        }, icon(resumable ? 'resume' : 'play', { size: 18 }), resumable ? 'Resume' : 'Play'),
        h('a', {
          class: 'btn btn-secondary btn-lg',
          href: detailHref(item),
          onclick: engage,
        }, icon('info', { size: 18 }), 'More info'),
        watchlistButton(item, { className: 'hero-watchlist' }),
      ),
    ]);

    onSelect?.(item, index);
  }

  function schedule() {
    clearTimeout(timer);
    if (userEngaged || items.length < 2) return;
    timer = setTimeout(() => {
      if (!document.hidden) show((index + 1) % items.length);
      schedule();
    }, ROTATE_MS);
  }

  // Anyone who interacts with the hero has made a choice; stop moving it.
  section.addEventListener('pointerdown', engage, { once: true });
  section.addEventListener('keydown', engage, { once: true });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !userEngaged) schedule();
  });

  show(0);
  if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) schedule();

  return section;
}

function replaceContent(node, children) {
  while (node.firstChild) node.removeChild(node.firstChild);
  children.filter(Boolean).forEach((child) => node.append(child));
}
