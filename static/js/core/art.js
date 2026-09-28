import { h } from './dom.js';

/* Artwork.
 *
 * Every image fades in once decoded and degrades to a deliberate-looking
 * surface instead of a broken-image icon. The frame reserves its aspect ratio
 * before the bytes arrive, so rows never reflow as posters load.
 */

export function artImage(art, { alt = '', sizes, priority = false, className = '' } = {}) {
  if (!art?.src) return null;
  const img = h('img', {
    src: art.src,
    srcset: art.srcset || null,
    sizes: sizes || null,
    alt,
    class: className,
    decoding: 'async',
    loading: priority ? 'eager' : 'lazy',
    fetchpriority: priority ? 'high' : null,
  });
  const reveal = () => img.classList.add('is-loaded');
  if (img.complete && img.naturalWidth) reveal();
  else img.addEventListener('load', reveal, { once: true });
  return img;
}

export function artFallback(title, poster = null) {
  const node = h('div', { class: 'card-art-fallback' });
  // A portrait poster inside a landscape box looks far better than a caption,
  // when the item has one.
  if (poster?.src) {
    node.classList.add('has-poster');
    node.append(h('img', { src: poster.src, alt: '', loading: 'lazy', decoding: 'async' }));
  }
  node.append(h('span', { text: title || 'No artwork' }));
  return node;
}

/**
 * A fixed-ratio artwork box. Returns a frame that already occupies the right
 * space, whether or not art exists yet.
 */
export function artFrame(art, { alt = '', sizes, priority = false, fallbackTitle = '', fallbackArt = null, className = '' } = {}) {
  const frame = h('div', { class: `card-art ${className}`.trim() });
  const substitute = () => {
    if (frame.querySelector('.card-art-fallback')) return;
    frame.append(artFallback(fallbackTitle, fallbackArt));
  };
  const img = artImage(art, { alt, sizes, priority });
  if (!img) {
    substitute();
    return frame;
  }
  img.addEventListener('error', () => { img.remove(); substitute(); }, { once: true });
  frame.append(img);
  return frame;
}

/** Standard `sizes` hints so the browser picks a sensible srcset candidate. */
export const SIZES = {
  row: '(max-width: 860px) 40vw, 184px',
  grid: '(max-width: 600px) 44vw, (max-width: 1200px) 22vw, 184px',
  wide: '(max-width: 860px) 62vw, 340px',
  backdrop: '100vw',
  detail: '(max-width: 720px) 118px, 230px',
  episode: '(max-width: 720px) 116px, 168px',
};
