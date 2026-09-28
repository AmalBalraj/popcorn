/* Inline icon set.
 *
 * One stroked style at 24×24 so icons stay consistent everywhere. `filled`
 * paths are drawn with fill instead of stroke.
 */

const ICONS = {
  play: { d: 'M7 4.5 19.5 12 7 19.5z', filled: true },
  pause: { d: 'M8.5 5h3v14h-3zM12.5 5h3v14h-3z', filled: true },
  stop: { d: 'M6.5 6.5h11v11h-11z', filled: true },
  resume: { d: 'M12 3a9 9 0 1 1-8.5 6M12 3v5m0-5H7' },
  next: { d: 'M6 5.5 15 12l-9 6.5zM17.5 5.5v13', filled: true },
  previous: { d: 'M18 5.5 9 12l9 6.5zM6.5 5.5v13', filled: true },
  plus: { d: 'M12 5v14M5 12h14' },
  minus: { d: 'M5 12h14' },
  check: { d: 'm5 13 4.5 4.5L19 7' },
  close: { d: 'M6 6l12 12M18 6 6 18' },
  info: { d: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 7.75v.5' },
  search: { d: 'M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-3.5-3.5' },
  star: { d: 'm12 3.7 2.6 5.4 5.9.8-4.3 4.1 1 5.9-5.2-2.8-5.2 2.8 1-5.9L3.5 9.9l5.9-.8z', filled: true },
  bookmark: { d: 'M6.5 3.5h11a1 1 0 0 1 1 1v16l-6.5-4-6.5 4v-16a1 1 0 0 1 1-1z' },
  download: { d: 'M12 4v11m-4-4 4 4 4-4M5 20h14' },
  downloadDone: { d: 'M12 4v9m-4-4 4 4 4-4M5 20h14' },
  chevronLeft: { d: 'm14.5 5-7 7 7 7' },
  chevronRight: { d: 'm9.5 5 7 7-7 7' },
  chevronDown: { d: 'm5 9 7 7 7-7' },
  chevronUp: { d: 'm5 15 7-7 7 7' },
  more: { d: 'M5.5 12h.01M12 12h.01M18.5 12h.01' },
  gear: { d: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM12 2.5v3m0 13v3M2.5 12h3m13 0h3M5.2 5.2l2.1 2.1m9.4 9.4 2.1 2.1M18.8 5.2l-2.1 2.1M7.3 16.7l-2.1 2.1' },
  volume: { d: 'M11 5 6.5 8.8H3v6.4h3.5L11 19zM15.5 9.2a4 4 0 0 1 0 5.6M18 6.6a8 8 0 0 1 0 10.8' },
  volumeLow: { d: 'M11 5 6.5 8.8H3v6.4h3.5L11 19zM15.5 9.2a4 4 0 0 1 0 5.6' },
  volumeMute: { d: 'M11 5 6.5 8.8H3v6.4h3.5L11 19zM16 9.5l5 5m0-5-5 5' },
  subtitles: { d: 'M3.5 5.5h17a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1h-17a1 1 0 0 1-1-1v-11a1 1 0 0 1 1-1zM6 14h5M13.5 14H18M6 10.5h3M11.5 10.5H18' },
  audio: { d: 'M4 9.5v5M7.5 6.5v11M11 9v6M14.5 4.5v15M18 8v8M21 10.5v3' },
  speed: { d: 'M12 21a9 9 0 1 0-9-9M12 12l4.5-4.5M12 21h9' },
  fullscreen: { d: 'M4 9V5.5A1.5 1.5 0 0 1 5.5 4H9M15 4h3.5A1.5 1.5 0 0 1 20 5.5V9M20 15v3.5a1.5 1.5 0 0 1-1.5 1.5H15M9 20H5.5A1.5 1.5 0 0 1 4 18.5V15' },
  fullscreenExit: { d: 'M9 4v3.5A1.5 1.5 0 0 1 7.5 9H4M20 9h-3.5A1.5 1.5 0 0 1 15 7.5V4M15 20v-3.5a1.5 1.5 0 0 1 1.5-1.5H20M4 15h3.5A1.5 1.5 0 0 1 9 16.5V20' },
  pip: { d: 'M4 5.5h16a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-11a1 1 0 0 1 1-1zM13 12.5h6.5V17H13z' },
  alert: { d: 'M12 3.5 21.5 20h-19zM12 10v4M12 17v.5' },
  clock: { d: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7.5V12l3 2' },
  trash: { d: 'M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13M10.5 11v5.5M13.5 11v5.5' },
  refresh: { d: 'M20 12a8 8 0 1 1-2.5-5.8M20 4v4h-4' },
  offline: { d: 'M3 3l18 18M8.5 15.5a5 5 0 0 1 7 0M5 12a10 10 0 0 1 4-2.4M19 12a10 10 0 0 0-4-2.4M12 19h.01' },
  film: { d: 'M3.5 4.5h17a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-17a1 1 0 0 1-1-1v-13a1 1 0 0 1 1-1zM7.5 4.5v15M16.5 4.5v15M2.5 9h5M2.5 15h5M16.5 9h5M16.5 15h5' },
  tv: { d: 'M2.5 7h19a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1h-19a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1zM8 3l4 3 4-3' },
  inbox: { d: 'M3.5 13.5h4l1.5 3h6l1.5-3h4M3.5 13.5 6 5h12l2.5 8.5v4a1 1 0 0 1-1 1h-15a1 1 0 0 1-1-1z' },
  library: { d: 'M4 4.5h3v15H4zM9.5 4.5h3v15h-3zM15 5l3.2-.7 3 14.7-3.2.7z' },
  folder: { d: 'M3.5 6.5h6l2 2.5h9a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1h-17a1 1 0 0 1-1-1v-10a1 1 0 0 1 1-1z' },
  user: { d: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4.5 20.5a7.5 7.5 0 0 1 15 0' },
  users: { d: 'M9 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM1.5 20.5a7.5 7.5 0 0 1 15 0M16.5 4.6a4 4 0 0 1 0 7.6M18 13.5a7.5 7.5 0 0 1 4.5 7' },
  lock: { d: 'M6.5 10.5h11a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1zM8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5' },
  server: { d: 'M3.5 4.5h17v6h-17zM3.5 13.5h17v6h-17zM7 7.5h.01M7 16.5h.01' },
  filter: { d: 'M3.5 5.5h17l-6.5 8v5.5l-4 2v-7.5z' },
  grid: { d: 'M4 4.5h6v6H4zM14 4.5h6v6h-6zM4 13.5h6v6H4zM14 13.5h6v6h-6z' },
  history: { d: 'M3.5 12a8.5 8.5 0 1 0 2.6-6.1M3.5 4v4.5H8M12 8v4.5l3 1.8' },
};

/** Names the registry knows, for callers that accept an icon name. */
export const ICON_NAMES = new Set(Object.keys(ICONS));

export function icon(name, { size = 20, className = '', strokeWidth = 1.8 } = {}) {
  // Fall back rather than returning nothing: an unknown name should never
  // leave a hole in the interface.
  const spec = ICONS[name] || ICONS.info;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', size);
  svg.setAttribute('height', size);
  svg.setAttribute('aria-hidden', 'true');
  if (className) svg.setAttribute('class', className);
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', spec.d);
  if (spec.filled) {
    path.setAttribute('fill', 'currentColor');
    if (spec.d.includes('z') && name === 'play') path.setAttribute('stroke-linejoin', 'round');
  } else {
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-width', String(strokeWidth));
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('stroke-linejoin', 'round');
  }
  svg.append(path);
  return svg;
}
