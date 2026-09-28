/* Minimal DOM construction and formatting helpers.
 *
 * Everything that renders user- or provider-supplied text goes through `h()`,
 * which sets textContent rather than innerHTML. Release names come from
 * torrent indexes and are not trustworthy.
 */

export function append(parent, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === true) continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

export function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class' || key === 'className') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'value' && 'value' in node) node.value = value;
    else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, value);
  }
  return append(node, children);
}

export const frag = (...children) => append(document.createDocumentFragment(), children);

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function replace(node, ...children) {
  clear(node);
  return append(node, children);
}

/* ── Formatting ─────────────────────────────────────────────────────────── */

export function formatRuntime(minutes) {
  if (!minutes || minutes < 1) return null;
  const hours = Math.floor(minutes / 60);
  const rest = Math.round(minutes % 60);
  if (!hours) return `${rest}m`;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

export function formatBytes(bytes) {
  if (!bytes || bytes < 1) return null;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const power = Math.min(Math.floor(Math.log(bytes) / Math.log(1000)), units.length - 1);
  const value = bytes / 1000 ** power;
  return `${value >= 100 || power === 0 ? Math.round(value) : value.toFixed(1)} ${units[power]}`;
}

/** Turns the free-text sizes that torrent indexes report into bytes. */
export function parseSize(size) {
  const match = String(size || '').match(/([\d.]+)\s*(B|KB|MB|GB|TB|KIB|MIB|GIB|TIB)/i);
  if (!match) return 0;
  const scale = { B: 1, KB: 1e3, MB: 1e6, GB: 1e9, TB: 1e12, KIB: 1024, MIB: 1024 ** 2, GIB: 1024 ** 3, TIB: 1024 ** 4 };
  return Number(match[1]) * (scale[match[2].toUpperCase()] || 1);
}

export function formatTimecode(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) seconds = 0;
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (value) => String(value).padStart(2, '0');
  return hours ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${minutes}:${pad(secs)}`;
}

export function formatDate(value, options = {}) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric', ...options });
}

/** "3 minutes ago" — used for download activity, never for release dates. */
export function relativeTime(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 45) return 'just now';
  const steps = [
    ['minute', 60], ['hour', 60], ['day', 24], ['week', 7], ['month', 4.35], ['year', 12],
  ];
  let amount = seconds / 60;
  let unit = 'minute';
  for (let index = 0; index < steps.length; index += 1) {
    const [name, divisor] = steps[index];
    const next = steps[index + 1];
    if (!next || amount < next[1]) { unit = name; break; }
    amount /= next[1];
    unit = next[0];
  }
  const rounded = Math.max(1, Math.round(amount));
  return `${rounded} ${unit}${rounded === 1 ? '' : 's'} ago`;
}

/** Seconds left, phrased for a progress line. */
export function remainingTime(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return null;
  const minutes = Math.round(seconds / 60);
  if (minutes < 1) return 'less than a minute left';
  if (minutes < 60) return `${minutes} min left`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}h ${rest}m left` : `${hours}h left`;
}

export function initials(name) {
  const words = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '·';
  return (words[0][0] + (words[1]?.[0] || '')).toUpperCase();
}

export function joinMeta(parts, separator = ' · ') {
  return parts.filter((part) => part !== null && part !== undefined && part !== '').join(separator);
}

/** Debounce, for search-as-you-type. */
export function debounce(fn, delay = 220) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

export function prefersReducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}
