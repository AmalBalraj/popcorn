import { formatBytes, formatRuntime, joinMeta } from './dom.js';

/* Shared derivations for library items, so every screen labels things the
   same way. */

export function detailHref(item) {
  if (!item) return '/';
  if (item.type === 'Series') return `/show/${item.id}`;
  if (item.type === 'BoxSet') return `/collection/${item.id}`;
  if (item.type === 'Episode' && item.series?.id) {
    return `/show/${item.series.id}?episode=${item.id}`;
  }
  return `/movie/${item.id}`;
}

/** Where the Play button goes: straight into the player, never a detour. */
export function playHref(item) {
  return item?.id ? `/play/${item.id}` : '/';
}

export function episodeLabel(item) {
  const season = item?.parent_index_number;
  const episode = item?.index_number;
  if (season === null || season === undefined || episode === null || episode === undefined) return null;
  return `S${season} · E${episode}`;
}

export function cardMeta(item) {
  if (item.type === 'Episode') {
    return joinMeta([
      episodeLabel(item),
      formatRuntime(item.runtime_minutes),
    ]) || null;
  }
  if (item.type === 'Series') {
    return joinMeta([
      item.year,
      item.user?.unplayed_count ? `${item.user.unplayed_count} unwatched` : null,
    ]) || 'TV series';
  }
  return joinMeta([item.year, formatRuntime(item.runtime_minutes)]) || null;
}

/** The one-line facts strip used on heroes and detail pages. */
export function infoParts(item, { includeGenres = true } = {}) {
  const parts = [
    item.year,
    formatRuntime(item.runtime_minutes),
    item.official_rating,
    item.media?.resolution,
    includeGenres ? item.genres?.slice(0, 3).join(', ') : null,
  ];
  return parts.filter((part) => part !== null && part !== undefined && part !== '');
}

export function progressPercent(item) {
  const percent = item?.user?.percent || 0;
  return Math.min(100, Math.max(0, percent));
}

/** True when resuming makes more sense than starting over. */
export function isResumable(item, { minimumPercent = 2, maximumPercent = 96 } = {}) {
  const percent = progressPercent(item);
  return percent >= minimumPercent && percent <= maximumPercent;
}

export function remainingLabel(item) {
  const runtime = item?.runtime_minutes;
  const percent = progressPercent(item);
  if (!runtime || !percent) return null;
  const left = Math.round(runtime * (1 - percent / 100));
  if (left < 1) return null;
  return `${formatRuntime(left)} left`;
}

export function sizeLabel(bytes) {
  return formatBytes(bytes);
}

/** Release rows carry a resolution we can surface as a quality chip. */
export function qualityFromRelease(title) {
  const match = String(title || '').match(/\b(2160p|1440p|1080p|720p|480p|4K)\b/i);
  if (!match) return null;
  const value = match[1].toLowerCase();
  return value === '2160p' || value === '4k' ? '4K' : value;
}
