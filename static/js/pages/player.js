import { get, post } from '../core/api.js';
import { h, formatTimecode, replace, joinMeta } from '../core/dom.js';
import { statePanel } from '../components/state.js';
import { showToast } from '../core/toast.js';
import { previewFrame } from '../core/previews.js';
import { episodeLabel } from '../core/items.js';

/* The player.
 *
 * Negotiation, resume and progress reporting all belong to Jellyfin — this
 * module decides what to play, keeps Jellyfin's position authoritative, and
 * gets out of the way while you watch.
 */

const root = document.getElementById('player');
const video = document.getElementById('video');
const boot = document.getElementById('player-boot');
const bootText = document.getElementById('player-boot-text');
const fatal = document.getElementById('player-fatal');
const chrome = document.getElementById('player-chrome');
const scrub = document.getElementById('player-scrub');
const playedBar = document.getElementById('player-played');
const bufferBar = document.getElementById('player-buffer');
const handle = document.getElementById('player-handle');
const tooltip = document.getElementById('player-tooltip');
const previewImage = h('div', { class: 'player-preview-image', hidden: true });
const previewTime = h('span');
tooltip.append(previewImage, previewTime);
const previewSheets = new Map();
let wantedPreview = null;
function paintPreview(time) {
  const frame = previewFrame(session?.previews, time);
  wantedPreview = frame;
  previewTime.textContent = formatTimecode(time);
  previewImage.hidden = true;
  if (!frame) return;
  const paint = () => {
    if (wantedPreview?.url !== frame.url) return;
    const current = wantedPreview;
    Object.assign(previewImage.style, { width: `${current.width}px`, height: `${current.height}px`,
      backgroundImage: `url("${current.url}")`, backgroundSize: current.size, backgroundPosition: current.position });
    previewImage.hidden = false;
  };
  let image = previewSheets.get(frame.url);
  if (!image) {
    image = new Image();
    previewSheets.set(frame.url, image);
    image.onload = paint;
    image.src = frame.url;
    if (previewSheets.size > 8) previewSheets.delete(previewSheets.keys().next().value);
  }
  if (image.complete && image.naturalWidth) paint();
}
const elapsedLabel = document.getElementById('player-elapsed');
const remainingLabel = document.getElementById('player-remaining');
const toggleIcon = document.getElementById('player-toggle-icon');
const playIcon = document.getElementById('player-play-icon');
const muteIcon = document.getElementById('player-mute-icon');
const volumeSlider = document.getElementById('player-volume');
const fullscreenIcon = document.getElementById('player-fullscreen-icon');
const feedback = document.getElementById('player-feedback');
const upNextBox = document.getElementById('player-upnext');

const itemId = root.dataset.itemId;
const TIME_KEY = 'popcorn.player';
const ICON_PLAY = 'M7 4.5 19.5 12 7 19.5z';
const ICON_PAUSE = 'M8.5 5h3v14h-3zM12.5 5h3v14h-3z';

const saved = (() => {
  try { return JSON.parse(localStorage.getItem(TIME_KEY) || '{}'); } catch { return {}; }
})();

let session = null;       // the negotiated playback session
let allowHevc = true;     // cleared permanently if a HEVC stream fails to decode
let preferences = { subtitleMode: 'Default', subtitleLanguage: 'eng' };
let hls = null;
let resumeDismissed = false;
let hideTimer;
let reportTimer;
let seeking = null;       // pending seek target, in seconds
let lastReported = -1;

/* ── Browser capability probe ───────────────────────────────────────────── */

function capabilities() {
  const probe = document.createElement('video');
  const can = (type) => probe.canPlayType(type) !== '';
  // HLS is fed to the element through Media Source Extensions, so MSE — not
  // canPlayType — decides what actually plays. canPlayType answers "maybe" for
  // hardware that cannot be handed buffers from JavaScript.
  const mse = (type) => {
    try { return Boolean(window.MediaSource && window.MediaSource.isTypeSupported(type)); }
    catch { return false; }
  };
  const containers = ['mp4', 'm4v'];
  if (can('video/webm; codecs="vp9"')) containers.push('webm');

  const videoCodecs = ['h264'];
  if (can('video/mp4; codecs="av01.0.05M.08"') || can('video/webm; codecs="av01.0.05M.08"')) videoCodecs.push('av1');
  if (can('video/webm; codecs="vp9"')) videoCodecs.push('vp9');
  // Main 10 at level 5 — what a 4K film actually carries. Claiming HEVC lets
  // the server copy the stream through untouched instead of re-encoding it.
  if (allowHevc && (mse('video/mp4; codecs="hvc1.2.4.L150.B0"') || mse('video/mp4; codecs="hev1.2.4.L150.B0"'))) {
    videoCodecs.push('hevc');
  }

  const audioCodecs = ['aac', 'mp3'];
  if (can('audio/webm; codecs="opus"')) audioCodecs.push('opus', 'vorbis');
  if (can('audio/mp4; codecs="flac"')) audioCodecs.push('flac');

  return { containers, video: videoCodecs, audio: audioCodecs, max_channels: 2 };
}

/* ── Loading a stream ───────────────────────────────────────────────────── */

/**
 * Ask Jellyfin for a playable stream.
 *
 * Note there is no start offset: Jellyfin always returns a full-length VOD
 * playlist whose timeline matches the media timeline, so resuming is done by
 * seeking the element. Asking the server to start mid-file would restart the
 * playlist at zero and desynchronise both subtitles and progress reporting.
 */
async function negotiate({ audioIndex = null, maxBitrate = null, maxHeight = null } = {}) {
  const body = { capabilities: capabilities() };
  const source = new URLSearchParams(location.search).get('source');
  if (source && /^[a-fA-F0-9-]{32,36}$/.test(source)) body.media_source_id = source;
  if (audioIndex !== null) body.audio_index = audioIndex;
  if (maxBitrate) body.max_bitrate = maxBitrate;
  if (maxHeight !== null) body.max_height = maxHeight;
  return post(`/api/play/${itemId}`, body);
}

/**
 * A browser can accept HEVC in the capability probe and still fail to decode
 * it. When that happens, stop claiming HEVC and negotiate again so the server
 * transcodes instead — once per session, and never while already retrying.
 */
async function retryWithoutHevc() {
  if (!allowHevc) return false;
  allowHevc = false;
  const at = video.currentTime;
  const wasPlaying = !video.paused;
  if (hls) { hls.destroy(); hls = null; }
  bootText.textContent = 'Restarting…';
  boot.hidden = false;
  boot.classList.remove('is-hidden');
  try {
    const quality = qualityFor(saved.maxBitrate);
    session = await negotiate({ maxBitrate: quality.bitrate, maxHeight: quality.height });
    await attachSource(session);
    addTextTracks();
    await onceLoaded();
    if (at > 0) video.currentTime = at;
    startedReported = false;
    reportStarted();
    if (wasPlaying) video.play().catch(() => {});
    showToast('Converting this one on the fly');
    return true;
  } catch {
    return false;
  } finally {
    boot.classList.add('is-hidden');
  }
}

function attachSource(target) {
  const url = target.url;
  if (hls) { hls.destroy(); hls = null; }

  if (target.mode === 'direct') {
    video.src = url;
    return Promise.resolve();
  }

  if (window.Hls && window.Hls.isSupported()) {
    hls = new window.Hls({
      enableWorker: true,
      lowLatencyMode: false,
      backBufferLength: 90,
      maxBufferLength: 60,
    });
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(window.Hls.Events.ERROR, async (_event, data) => {
      if (!data.fatal) return;
      if (data.type === 'mediaError' && hls) { hls.recoverMediaError(); return; }
      if (await retryWithoutHevc()) return;
      fail("Playback stopped unexpectedly", 'The stream could not be decoded. Try a different quality.');
    });
    return Promise.resolve();
  }

  // Safari and iOS play HLS natively.
  if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = url;
    return Promise.resolve();
  }

  return Promise.reject(new Error('This browser cannot play this stream.'));
}

/* ── Controls ───────────────────────────────────────────────────────────── */

let pointerOverControls = false;

function showControls() {
  chrome.dataset.visible = 'true';
  root.dataset.controls = 'visible';
  clearTimeout(hideTimer);
  if (!video.paused) hideTimer = setTimeout(hideControls, 2800);
}

function hideControls() {
  if (seeking !== null) return;
  // Never hide out from under a pointer that is using the controls, or a
  // keyboard focus that is inside them.
  if (pointerOverControls) return;
  if (document.activeElement && chrome.contains(document.activeElement)) return;
  chrome.dataset.visible = 'false';
  root.dataset.controls = 'hidden';
}

function setPlayingIcon() {
  const path = video.paused ? ICON_PLAY : ICON_PAUSE;
  toggleIcon.setAttribute('d', path);
  playIcon.setAttribute('d', path);
  document.getElementById('player-toggle').setAttribute('aria-label', video.paused ? 'Play' : 'Pause');
  document.getElementById('player-play').setAttribute('aria-label', video.paused ? 'Play' : 'Pause');
}

function flash(message) {
  feedback.textContent = message;
  feedback.classList.add('is-on');
  clearTimeout(flash.timer);
  flash.timer = setTimeout(() => feedback.classList.remove('is-on'), 700);
}

function setFatal(panel) {
  replace(fatal, panel);
  fatal.hidden = false;
  boot.classList.add('is-hidden');
  chrome.dataset.visible = 'false';
}

function fail(title, message, detail) {
  setFatal(statePanel({
    tone: 'error',
    title,
    message,
    detail,
    actions: [
      { label: 'Try again', primary: true, onClick: () => location.reload() },
      { label: 'Back to library', onClick: () => { location.href = session?.player?.item?.series ? `/show/${session.player.item.series.id}` : '/movies'; } },
    ],
  }));
}

/* ── Scrub bar ──────────────────────────────────────────────────────────── */

function duration() {
  return Number.isFinite(video.duration) && video.duration > 0 ? video.duration : 0;
}

function paintProgress() {
  const total = duration();
  const current = seeking !== null ? seeking : video.currentTime;
  const percent = total ? (current / total) * 100 : 0;
  playedBar.style.width = `${percent}%`;
  handle.style.left = `${percent}%`;
  scrub.setAttribute('aria-valuenow', String(Math.round(percent)));
  scrub.setAttribute('aria-valuetext', `${formatTimecode(current)} of ${formatTimecode(total)}`);

  elapsedLabel.textContent = formatTimecode(current);
  remainingLabel.textContent = total ? `-${formatTimecode(Math.max(0, total - current))}` : '0:00';

  if (video.buffered.length) {
    const end = video.buffered.end(video.buffered.length - 1);
    bufferBar.style.width = total ? `${(end / total) * 100}%` : '0%';
  }
}

function pointerTime(event) {
  const rect = scrub.getBoundingClientRect();
  const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
  return { ratio, time: ratio * duration() };
}

scrub.addEventListener('pointermove', (event) => {
  if (!duration()) return;
  const { ratio, time } = pointerTime(event);
  tooltip.hidden = false;
  paintPreview(time);
  const rect = scrub.getBoundingClientRect();
  const x = Math.min(Math.max(event.clientX - rect.left, session?.previews ? 106 : 40), rect.width - (session?.previews ? 106 : 40));
  tooltip.style.left = `${x}px`;
  if (seeking !== null) {
    seeking = time;
    paintProgress();
  }
});

scrub.addEventListener('pointerleave', () => { tooltip.hidden = true; });

scrub.addEventListener('pointerdown', (event) => {
  if (!duration()) return;
  scrub.setPointerCapture(event.pointerId);
  scrub.classList.add('is-scrubbing');
  seeking = pointerTime(event).time;
  tooltip.hidden = false;
  paintPreview(seeking);
  paintProgress();
  showControls();
});

scrub.addEventListener('pointerup', (event) => {
  if (seeking === null) return;
  scrub.classList.remove('is-scrubbing');
  const target = seeking;
  seeking = null;
  try { video.currentTime = target; } catch { /* not seekable yet */ }
  paintProgress();
  reportProgress(true);
  showControls();
  void event;
});

scrub.addEventListener('keydown', (event) => {
  const step = event.shiftKey ? 60 : 10;
  if (event.key === 'ArrowRight') { seekBy(step); event.preventDefault(); }
  if (event.key === 'ArrowLeft') { seekBy(-step); event.preventDefault(); }
  if (event.key === 'Home') { video.currentTime = 0; event.preventDefault(); }
  if (event.key === 'End') { video.currentTime = duration(); event.preventDefault(); }
});

/* ── Actions ────────────────────────────────────────────────────────────── */

function togglePlay() {
  if (video.paused) video.play().catch(() => {});
  else video.pause();
}

function seekBy(seconds) {
  if (!duration()) return;
  video.currentTime = Math.min(Math.max(0, video.currentTime + seconds), duration());
  flash(`${seconds > 0 ? '+' : ''}${seconds}s`);
  showControls();
}

function setVolume(value, { persist = true } = {}) {
  video.volume = Math.min(1, Math.max(0, value));
  video.muted = video.volume === 0;
  volumeSlider.value = String(video.volume);
  if (persist) {
    try { localStorage.setItem(TIME_KEY, JSON.stringify({ ...saved, volume: video.volume })); } catch { /* ignore */ }
    saved.volume = video.volume;
  }
  paintVolumeIcon();
}

function paintVolumeIcon() {
  const level = video.muted ? 0 : video.volume;
  const path = level === 0
    ? 'M11 5 6.5 8.8H3v6.4h3.5L11 19zM16 9.5l5 5m0-5-5 5'
    : level < 0.5
      ? 'M11 5 6.5 8.8H3v6.4h3.5L11 19zM15.5 9.2a4 4 0 0 1 0 5.6'
      : 'M11 5 6.5 8.8H3v6.4h3.5L11 19zM15.5 9.2a4 4 0 0 1 0 5.6M18 6.6a8 8 0 0 1 0 10.8';
  muteIcon.setAttribute('d', path);
  document.getElementById('player-mute').setAttribute('aria-label', level === 0 ? 'Unmute' : 'Mute');
}

function toggleFullscreen() {
  const target = root;
  if (document.fullscreenElement) document.exitFullscreen();
  else target.requestFullscreen?.().catch(() => {});
}

function paintFullscreenIcon() {
  const on = Boolean(document.fullscreenElement);
  fullscreenIcon.setAttribute('d', on
    ? 'M9 4v3.5A1.5 1.5 0 0 1 7.5 9H4M20 9h-3.5A1.5 1.5 0 0 1 15 7.5V4M15 20v-3.5a1.5 1.5 0 0 1 1.5-1.5H20M4 15h3.5A1.5 1.5 0 0 1 9 16.5V20'
    : 'M4 9V5.5A1.5 1.5 0 0 1 5.5 4H9M15 4h3.5A1.5 1.5 0 0 1 20 5.5V9M20 15v3.5a1.5 1.5 0 0 1-1.5 1.5H15M9 20H5.5A1.5 1.5 0 0 1 4 18.5V15');
}

/* ── Menus ──────────────────────────────────────────────────────────────── */

const MENUS = [
  { button: 'player-speed-button', title: 'Speed', build: buildSpeedMenu },
  { button: 'player-audio-button', title: 'Audio', build: buildAudioMenu },
  { button: 'player-subs-button', title: 'Subtitles', build: buildSubtitleMenu },
  { button: 'player-quality-button', title: 'Quality', build: buildQualityMenu },
];

let openMenu = null;

function closeMenus() {
  if (!openMenu) return;
  openMenu.menu.remove();
  openMenu.button.setAttribute('aria-expanded', 'false');
  openMenu = null;
}

function closeMenusOnOutside(event) {
  if (openMenu && !openMenu.wrap.contains(event.target)) closeMenus();
}

document.addEventListener('click', closeMenusOnOutside);

for (const spec of MENUS) {
  const button = document.getElementById(spec.button);
  const wrap = button.closest('.player-menu-wrap');
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    const wasOpen = openMenu?.button === button;
    closeMenus();
    if (wasOpen) return;
    const menu = spec.build();
    menu.className = 'player-menu';
    wrap.append(menu);
    button.setAttribute('aria-expanded', 'true');
    openMenu = { button, menu, wrap };
    menu.querySelector('button')?.focus();
  });
}

function menuSection(title, options) {
  const fragment = document.createDocumentFragment();
  if (title) fragment.append(h('h4', { text: title }));
  options.forEach((option) => {
    if (!option) return;
    fragment.append(h('button', {
      type: 'button',
      role: option.role || 'menuitemradio',
      'aria-checked': String(Boolean(option.checked)),
      onclick: () => { closeMenus(); option.onSelect?.(); },
    }, option.label));
  });
  return fragment;
}

const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];

function buildSpeedMenu() {
  return h('div', {}, menuSection('Playback speed', SPEEDS.map((speed) => ({
    label: speed === 1 ? 'Normal' : `${speed}×`,
    checked: Math.abs(video.playbackRate - speed) < 0.01,
    onSelect: () => {
      video.playbackRate = speed;
      try { localStorage.setItem(TIME_KEY, JSON.stringify({ ...saved, speed })); } catch { /* ignore */ }
      saved.speed = speed;
      showToast(speed === 1 ? 'Normal speed' : `${speed}× speed`);
    },
  }))));
}

function buildAudioMenu() {
  const tracks = session?.audio || [];
  if (tracks.length < 2) {
    return h('div', {}, h('h4', { text: 'Audio' }),
      h('p', { class: 'player-menu-note', text: 'This title has a single audio track.' }));
  }
  return h('div', {}, menuSection('Audio', tracks.map((track) => ({
    label: joinMeta([track.title, track.channels]),
    checked: track.index === (saved.audioIndex ?? session.audio.find((entry) => entry.is_default)?.index),
    onSelect: () => switchAudio(track),
  }))));
}

function buildSubtitleMenu() {
  const tracks = session?.subtitles || [];
  const current = saved.subtitleIndex ?? null;
  const options = [{
    label: 'Off',
    checked: current === null,
    onSelect: () => applySubtitle(null),
  }, ...tracks.map((track) => ({
    label: track.title,
    checked: track.index === current,
    onSelect: () => applySubtitle(track),
  }))];
  return h('div', {}, menuSection('Subtitles', options));
}

// `token` is what gets remembered; `height` is the ceiling the server encodes
// to. "Auto" caps at 1080p because encoding larger costs the server far more
// time to start for no visible gain on a laptop screen.
const QUALITIES = [
  { label: 'Auto', token: null, bitrate: null, height: 1080 },
  { label: 'Original quality', token: 'original', bitrate: null, height: 0 },
  { label: '1080p · 10 Mbps', token: 10_000_000, bitrate: 10_000_000, height: 1080 },
  { label: '720p · 4 Mbps', token: 4_000_000, bitrate: 4_000_000, height: 720 },
  { label: '480p · 2 Mbps', token: 2_000_000, bitrate: 2_000_000, height: 480 },
];

const qualityFor = (token) => QUALITIES.find((quality) => quality.token === (token ?? null)) || QUALITIES[0];

function buildQualityMenu() {
  const current = saved.maxBitrate ?? null;
  const fragment = menuSection('Quality', QUALITIES.map((quality) => ({
    label: quality.label,
    checked: current === quality.token,
    onSelect: () => switchQuality(quality.token),
  })));
  if (session?.video_reencoded) {
    fragment.append(h('p', {
      class: 'player-menu-note',
      text: 'This browser cannot play the file as-is, so the picture is being converted on the fly.',
    }));
  }
  return h('div', {}, fragment);
}

/* ── Switching mid-playback ─────────────────────────────────────────────── */

async function reloadStream({ audioIndex = null, qualityToken = null, note }) {
  const at = video.currentTime;
  const wasPlaying = !video.paused;
  const previous = { audioIndex: saved.audioIndex, maxBitrate: saved.maxBitrate };
  if (audioIndex !== null) saved.audioIndex = audioIndex;
  else if (note === 'audio') saved.audioIndex = undefined;
  if (note === 'quality') saved.maxBitrate = qualityToken;
  try { localStorage.setItem(TIME_KEY, JSON.stringify(saved)); } catch { /* ignore */ }

  bootText.textContent = 'Switching…';
  boot.hidden = false;
  boot.classList.remove('is-hidden');
  try {
    const quality = qualityFor(saved.maxBitrate);
    session = await negotiate({
      audioIndex: note === 'audio' ? audioIndex : null,
      maxBitrate: note === 'quality' ? quality.bitrate : null,
      maxHeight: note === 'quality' ? quality.height : null,
    });
    await attachSource(session);
    addTextTracks();
    await onceLoaded();
    video.currentTime = at;
    startedReported = false;
    reportStarted();
    if (wasPlaying) video.play().catch(() => {});
    if (note === 'audio') showToast('Audio track changed');
    if (note === 'quality') showToast(quality.bitrate || quality.height ? 'Quality changed' : 'Quality set to auto');
  } catch (error) {
    saved.audioIndex = previous.audioIndex;
    saved.maxBitrate = previous.maxBitrate;
    try { localStorage.setItem(TIME_KEY, JSON.stringify(saved)); } catch { /* ignore */ }
    showToast(error.message || 'Could not switch stream.', { tone: 'error' });
  } finally {
    boot.classList.add('is-hidden');
  }
}

const switchAudio = (track) => reloadStream({ audioIndex: track.index, note: 'audio' });
const switchQuality = (token) => reloadStream({ qualityToken: token, note: 'quality' });

/** Subtitles are sidecar WebVTT tracks, so switching is local. */
function applySubtitle(track, { silent = false } = {}) {
  const tracks = video.textTracks;
  for (let index = 0; index < tracks.length; index += 1) {
    tracks[index].mode = 'disabled';
  }
  if (track) {
    const match = [...tracks].find((entry) => String(entry.id) === String(track.index));
    if (match) {
      match.mode = 'showing';
      if (!silent) showToast(`Subtitles: ${track.title}`);
    } else {
      if (!silent) showToast('That subtitle track is not available for this stream.', { tone: 'warn' });
      return;
    }
  } else if (!silent) {
    showToast('Subtitles off');
  }
  saved.subtitleIndex = track ? track.index : null;
  try { localStorage.setItem(TIME_KEY, JSON.stringify(saved)); } catch { /* ignore */ }
  reportProgress(true);
}

/* ── Reporting ──────────────────────────────────────────────────────────── */

function reportBody() {
  return {
    media_source_id: session?.media_source_id,
    play_session_id: session?.play_session_id,
    position_ticks: Math.max(0, Math.round(video.currentTime * 10_000_000)),
    is_paused: video.paused,
    is_muted: video.muted,
    method: session?.method,
    volume: Math.round(video.volume * 100),
    audio_index: saved.audioIndex ?? null,
    subtitle_index: saved.subtitleIndex ?? null,
  };
}

let startedReported = false;

/** Jellyfin needs a playback session before progress means anything. */
async function reportStarted() {
  if (!session || startedReported) return;
  startedReported = true;
  try {
    await post(`/api/play/${itemId}/started`, reportBody());
  } catch { /* playback continues regardless */ }
}

async function reportProgress(force = false) {
  if (!session) return;
  const at = Math.round(video.currentTime);
  if (!force && Math.abs(at - lastReported) < 5) return;
  lastReported = at;
  try {
    await post(`/api/play/${itemId}/progress`, reportBody());
  } catch { /* a dropped keep-alive must never interrupt playback */ }
}

function reportStopped(final = false) {
  if (!session) return;
  const body = reportBody();
  if (final) {
    body.position_ticks = Math.max(0, Math.round((video.duration || video.currentTime) * 10_000_000));
  }
  const url = `/api/play/${itemId}/stopped`;
  const json = JSON.stringify(body);
  // sendBeacon is the only report the browser guarantees to deliver while the
  // page is going away. A keepalive fetch here is usually dropped, which left
  // Jellyfin holding a stale NowPlayingItem and blocked deleting the file.
  try {
    if (navigator.sendBeacon?.(url, new Blob([json], { type: 'application/json' }))) return;
  } catch { /* fall through to fetch */ }
  try {
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: json,
      keepalive: true,
      credentials: 'same-origin',
    }).catch(() => {});
  } catch { /* ignore */ }
}

/* ── Up next ────────────────────────────────────────────────────────────── */

let upNextTimer;

function offerNextEpisode() {
  const next = session?.next_up;
  if (!next) {
    reportStopped(true);
    showControls();
    return;
  }
  reportStopped(true);

  let countdown = 10;
  const paint = () => {
    replace(upNextBox,
      h('span', { class: 't-label', text: `Up next in ${countdown}s` }),
      h('div', { class: 'player-upnext-body' },
        h('div', { class: 'player-upnext-art' },
          next.thumb?.src || next.backdrop?.src
            ? h('img', { src: (next.thumb || next.backdrop).src, alt: '' })
            : null,
        ),
        h('div', {},
          h('h3', { class: 'clamp-2', text: next.title }),
          h('p', { text: joinMeta([episodeLabel(next), next.runtime_minutes ? `${next.runtime_minutes}m` : null]) }),
        ),
      ),
      h('div', { class: 'player-upnext-actions' },
        h('a', { class: 'btn btn-primary btn-sm', href: `/play/${next.id}` }, 'Play now'),
        h('button', {
          class: 'btn btn-outline btn-sm',
          type: 'button',
          onclick: () => { clearInterval(upNextTimer); upNextBox.hidden = true; },
        }, 'Cancel'),
      ),
    );
  };

  upNextBox.hidden = false;
  paint();
  upNextTimer = setInterval(() => {
    countdown -= 1;
    if (countdown <= 0) {
      clearInterval(upNextTimer);
      location.href = `/play/${next.id}`;
      return;
    }
    paint();
  }, 1000);
}

/* ── Events ─────────────────────────────────────────────────────────────── */

function onceLoaded() {
  return new Promise((resolve, reject) => {
    if (video.readyState >= 1) { resolve(); return; }
    video.addEventListener('loadedmetadata', resolve, { once: true });
    video.addEventListener('error', () => reject(new Error('The stream could not be loaded.')), { once: true });
  });
}

video.addEventListener('play', () => { setPlayingIcon(); showControls(); reportStarted(); });
video.addEventListener('pause', () => { setPlayingIcon(); showControls(); reportProgress(true); });
video.addEventListener('timeupdate', paintProgress);
video.addEventListener('progress', paintProgress);
video.addEventListener('ended', offerNextEpisode);
video.addEventListener('error', async () => {
  if (!session) return;
  if (await retryWithoutHevc()) return;
  fail('This title could not be played', 'The media server may be busy, or the file may have moved.');
});

// Pointer movement anywhere brings the controls back; the chrome itself is
// pointer-events:none while hidden, so this has to live on the root.
root.addEventListener('pointermove', showControls);
document.querySelector('.player-bottom').addEventListener('pointerenter', () => {
  pointerOverControls = true;
  showControls();
});
document.querySelector('.player-bottom').addEventListener('pointerleave', () => {
  pointerOverControls = false;
  showControls();
});
document.querySelector('.player-top').addEventListener('pointerenter', () => {
  pointerOverControls = true;
  showControls();
});
document.querySelector('.player-top').addEventListener('pointerleave', () => {
  pointerOverControls = false;
  showControls();
});

root.addEventListener('pointerdown', (event) => {
  // Tap the bare video: toggle controls, or play/pause on a second tap.
  if (event.target !== video) return;
  if (chrome.dataset.visible === 'true') {
    if (event.pointerType === 'mouse') togglePlay();
  } else {
    showControls();
  }
});
root.addEventListener('dblclick', (event) => {
  if (event.target === video) toggleFullscreen();
});

// Double-tap to seek on touch, with a visible confirmation.
let lastTap = { time: 0, x: 0 };
video.addEventListener('pointerup', (event) => {
  if (event.pointerType !== 'touch') return;
  const now = Date.now();
  const rect = root.getBoundingClientRect();
  if (now - lastTap.time < 320 && Math.abs(event.clientX - lastTap.x) < 60) {
    const side = event.clientX - rect.left < rect.width / 2 ? -1 : 1;
    seekBy(side * 10);
    lastTap = { time: 0, x: 0 };
    return;
  }
  lastTap = { time: now, x: event.clientX };
});

document.addEventListener('keydown', (event) => {
  if (event.target.matches('input, textarea')) return;
  const key = event.key.toLowerCase();
  switch (key) {
    case ' ':
    case 'k':
      event.preventDefault(); togglePlay(); break;
    case 'arrowright': event.preventDefault(); seekBy(event.shiftKey ? 60 : 10); break;
    case 'arrowleft': event.preventDefault(); seekBy(event.shiftKey ? -60 : -10); break;
    case 'arrowup': event.preventDefault(); setVolume(video.volume + 0.05); flashVolume(); break;
    case 'arrowdown': event.preventDefault(); setVolume(video.volume - 0.05); flashVolume(); break;
    case 'j': event.preventDefault(); seekBy(-10); break;
    case 'l': event.preventDefault(); seekBy(10); break;
    case 'f': event.preventDefault(); toggleFullscreen(); break;
    case 'm': event.preventDefault(); video.muted = !video.muted; paintVolumeIcon(); flash(video.muted ? 'Muted' : 'Unmuted'); break;
    case 'c': event.preventDefault(); document.getElementById('player-subs-button').click(); break;
    case 'escape':
      if (openMenu) { closeMenus(); break; }
      if (!document.fullscreenElement) location.href = session?.player?.item?.series
        ? `/show/${session.player.item.series.id}` : '/movies';
      break;
    case 'n': if (session?.next_up) location.href = `/play/${session.next_up.id}`; break;
    default:
      if (/^[0-9]$/.test(key) && duration()) {
        event.preventDefault();
        video.currentTime = duration() * (Number(key) / 10);
      }
  }
  showControls();
});

function flashVolume() {
  flash(`${Math.round(video.volume * 100)}%`);
}

document.addEventListener('fullscreenchange', () => { paintFullscreenIcon(); showControls(); });
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    reportProgress(true);
    clearTimeout(reportTimer);
    // Hidden *and* paused means the viewer has walked away — report the stop so
    // Jellyfin does not keep the item as now-playing. Hidden while still
    // playing is someone listening in another tab, so leave that session alone.
    if (video.paused) reportStopped();
  } else {
    startReporting();
  }
});
window.addEventListener('pagehide', () => reportStopped(false));
window.addEventListener('beforeunload', () => reportStopped(false));

function startReporting() {
  clearInterval(reportTimer);
  reportTimer = setInterval(() => {
    if (!document.hidden && !video.paused) { reportStarted(); reportProgress(); }
  }, 10000);
}

/* ── Wiring ─────────────────────────────────────────────────────────────── */

document.getElementById('player-toggle').addEventListener('click', togglePlay);
document.getElementById('player-play').addEventListener('click', togglePlay);
document.getElementById('player-back-10').addEventListener('click', () => seekBy(-10));
document.getElementById('player-forward-10').addEventListener('click', () => seekBy(10));
document.getElementById('player-mute').addEventListener('click', () => {
  video.muted = !video.muted;
  paintVolumeIcon();
  flash(video.muted ? 'Muted' : 'Unmuted');
});
volumeSlider.addEventListener('input', (event) => setVolume(Number(event.target.value), { persist: false }));
volumeSlider.addEventListener('change', (event) => setVolume(Number(event.target.value)));
document.getElementById('player-fullscreen').addEventListener('click', toggleFullscreen);
document.getElementById('player-back').addEventListener('click', () => {
  if (session?.player?.item?.series?.id) location.href = `/show/${session.player.item.series.id}`;
  else if (history.length > 1) history.back();
  else location.href = '/movies';
});
document.getElementById('player-next').addEventListener('click', () => {
  if (session?.next_up) location.href = `/play/${session.next_up.id}`;
});
if (document.pictureInPictureEnabled) {
  const pipButton = document.getElementById('player-pip');
  pipButton.hidden = false;
  pipButton.addEventListener('click', () => {
    if (document.pictureInPictureElement) document.exitPictureInPicture();
    else video.requestPictureInPicture().catch(() => {});
  });
}

/* ── Start ──────────────────────────────────────────────────────────────── */

async function start() {
  try {
    const settings = await get('/api/settings');
    preferences = {
      subtitleMode: settings.subtitle_mode || 'Default',
      subtitleLanguage: (settings.subtitle_language || 'eng').toLowerCase(),
    };
  } catch { /* fall back to the media's own default track */ }

  setVolume(saved.volume ?? 1, { persist: false });
  video.playbackRate = saved.speed || 1;

  try {
    // Ask for the stream without a start position so the server can tell us
    // where the viewer left off. The remembered quality still applies.
    const quality = qualityFor(saved.maxBitrate);
    session = await negotiate({ maxBitrate: quality.bitrate, maxHeight: quality.height });
  } catch (error) {
    fail(
      error.serverDown ? "Couldn't reach your media server" : 'This title could not be played',
      error.message || 'The media server did not respond.',
      error.detail,
    );
    return;
  }

  renderHeader();
  const restart = new URLSearchParams(location.search).get('restart') === '1';
  const startSeconds = restart ? 0 : (session.start_ticks || 0) / 10_000_000;

  try {
    await attachSource(session);
  } catch (error) {
    fail('This browser cannot play this title', error.message, 'Try a different browser, or use Open Jellyfin from the profile menu.');
    return;
  }

  addTextTracks();
  try {
    await onceLoaded();
  } catch {
    fail('This title could not be played', 'The media server may be busy, or the file may have moved.');
    return;
  }

  if (startSeconds > 5) video.currentTime = startSeconds;
  boot.classList.add('is-hidden');
  video.play().catch(() => {
    // Autoplay is often blocked; show controls so the viewer can start it.
    showControls();
  });
  setPlayingIcon();
  paintProgress();
  startReporting();
  showControls();

  if (startSeconds > 5 && !restart) {
    showResumeHint(startSeconds);
  }
}

function showResumeHint(seconds) {
  const hint = h('div', { class: 'player-toast' },
    `Resuming from ${formatTimecode(seconds)}`,
    h('button', {
      class: 'btn btn-sm btn-outline',
      type: 'button',
      style: { marginLeft: 'var(--s3)' },
      text: 'Start over',
      onclick: () => {
        video.currentTime = 0;
        hint.remove();
        resumeDismissed = true;
        reportProgress(true);
      },
    }),
  );
  root.append(hint);
  setTimeout(() => { if (!resumeDismissed) hint.remove(); }, 7000);
}

/** The subtitle the viewer's settings ask for, if they have not chosen one. */
function preferredSubtitleTrack() {
  const tracks = session?.subtitles || [];
  if (!tracks.length) return null;
  if (preferences.subtitleMode === 'None') return null;
  if (preferences.subtitleMode === 'OnlyForced') {
    return tracks.find((track) => track.is_forced) || null;
  }
  const language = (preferences.subtitleLanguage || '').toLowerCase();
  return tracks.find((track) => track.language === language)
    || tracks.find((track) => track.is_default)
    || tracks[0]
    || null;
}

function addTextTracks() {
  video.querySelectorAll('track').forEach((track) => track.remove());
  for (const track of session.subtitles || []) {
    const element = document.createElement('track');
    element.kind = 'subtitles';
    element.id = String(track.index);
    element.label = track.title;
    element.srclang = track.language || 'und';
    element.src = track.url;
    if (track.is_default) element.default = true;
    video.append(element);
  }
  // An explicit choice always wins; otherwise follow the viewer's settings.
  requestAnimationFrame(() => {
    if (saved.subtitleIndex !== undefined && saved.subtitleIndex !== null) {
      const match = (session.subtitles || []).find((track) => track.index === saved.subtitleIndex);
      applySubtitle(match || null, { silent: true });
      return;
    }
    applySubtitle(preferredSubtitleTrack(), { silent: true });
  });
}

function renderHeader() {
  const item = session.player?.item;
  if (!item) return;
  document.title = `${item.title} — Popcorn`;
  document.getElementById('player-title').textContent = item.title;
  const parts = item.type === 'Episode'
    ? [item.series?.name, episodeLabel(item)]
    : [item.year, item.media?.resolution];
  document.getElementById('player-subtitle').textContent = joinMeta(parts.filter(Boolean));

  if (session.next_up) {
    const nextButton = document.getElementById('player-next');
    nextButton.hidden = false;
    nextButton.title = `Next: ${session.next_up.title}`;
  }
}

start();
