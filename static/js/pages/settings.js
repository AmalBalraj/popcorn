import { get, post, put, del } from '../core/api.js';
import { h, replace, clear, formatBytes } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { showToast, toastError } from '../core/toast.js';
import { confirmDialog } from '../components/modal.js';
import { statePanel } from '../components/state.js';

/* Settings.
 *
 * Everyday preferences lead; server and library administration sits behind an
 * administrator check so a member never sees controls they cannot use.
 */

const form = document.getElementById('settings-form');
const status = document.getElementById('settings-status');
const isAdmin = Boolean(document.querySelector('#library'));

const SUBTITLE_LANGUAGES = [
  ['eng', 'English'], ['mal', 'Malayalam'], ['tam', 'Tamil'], ['tel', 'Telugu'],
  ['hin', 'Hindi'], ['kan', 'Kannada'], ['ben', 'Bengali'], ['mar', 'Marathi'],
  ['pan', 'Punjabi'], ['spa', 'Spanish'], ['fre', 'French'], ['ger', 'German'],
  ['jpn', 'Japanese'], ['kor', 'Korean'], ['zho', 'Chinese'], ['ara', 'Arabic'],
];

function fillLanguages() {
  const select = document.getElementById('subtitle_language');
  const current = select.dataset.current || window.__subtitleLanguage || 'eng';
  clear(select);
  const known = new Set(SUBTITLE_LANGUAGES.map(([code]) => code));
  const options = [...SUBTITLE_LANGUAGES];
  if (current && !known.has(current)) options.push([current, current.toUpperCase()]);
  for (const [code, label] of options) {
    select.append(h('option', { value: code, selected: code === current }, label));
  }
}

async function loadSettings() {
  try {
    const settings = await get('/api/settings');
    for (const [key, value] of Object.entries(settings)) {
      const field = form.elements[key];
      if (!field) continue;
      if (field.type === 'checkbox') field.checked = Boolean(value);
      else field.value = value;
    }
    const languageSelect = document.getElementById('subtitle_language');
    languageSelect.dataset.current = settings.subtitle_language;
    fillLanguages();
  } catch (error) {
    status.textContent = error.message;
  }
}

const clipVision = document.getElementById('clip_vision');
clipVision?.addEventListener('change', () => {
  document.getElementById('clip_vision_label').textContent = clipVision.checked ? 'On' : 'Off';
});

const clipSubtitles = document.getElementById('clip_subtitle_download');
clipSubtitles?.addEventListener('change', () => {
  document.getElementById('clip_subtitle_download_label').textContent = clipSubtitles.checked ? 'On' : 'Off';
});

const automaticSubtitles = document.getElementById('subtitle_auto_download');
function updateSubtitleSwitch() {
  if (automaticSubtitles) automaticSubtitles.closest('.switch').querySelector('.switch-text').textContent = automaticSubtitles.checked ? 'On' : 'Off';
}
automaticSubtitles?.addEventListener('change', updateSubtitleSwitch);
updateSubtitleSwitch();

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = form.querySelector('button[type="submit"]');
  const data = new FormData(form);
  button.disabled = true;
  status.textContent = 'Saving…';
  try {
    const result = await put('/api/settings', {
      ...(isAdmin ? { subtitle_auto_download: data.has('subtitle_auto_download') } : {}),
      default_upload: data.get('default_upload'),
      default_source: data.get('default_source'),
      search_timeout: Number(data.get('search_timeout')),
      subtitle_language: data.get('subtitle_language'),
      subtitle_mode: data.get('subtitle_mode'),
      clip_target_count: Number(data.get('clip_target_count')),
      clip_min_seconds: Number(data.get('clip_min_seconds')),
      clip_max_seconds: Number(data.get('clip_max_seconds')),
      clip_vision: data.has('clip_vision'),
      clip_subtitle_download: data.has('clip_subtitle_download'),
      clip_whisper_model: data.get('clip_whisper_model'),
    });
    status.textContent = result.warning || 'Saved.';
    showToast(result.warning ? 'Saved in Popcorn' : 'Settings saved', {
      tone: result.warning ? 'warn' : 'success',
    });
  } catch (error) {
    status.textContent = error.message;
    toastError(error);
  } finally {
    button.disabled = false;
  }
});

/* Automatic subtitle downloads use one administrator-owned provider account. */
const subtitleAccountForm = document.getElementById('subtitle-account-form');
let subtitlePoll;
function renderSubtitleAccount(data) {
  document.getElementById('subtitle-account-status').textContent = data.connected
    ? `Connected as ${data.username}.` : 'Connect your account to activate automatic subtitle downloads.';
  document.getElementById('subtitle-disconnect').disabled = !data.connected;
  const scan = data.scan || {};
  document.getElementById('subtitle-scan').disabled = !data.connected || scan.status === 'running';
  const label = document.getElementById('subtitle-scan-status');
  if (scan.status === 'running') label.textContent = `Checking ${scan.checked || 0} of ${scan.total || 0} videos… ${scan.downloaded || 0} subtitles added.`;
  else if (scan.status === 'complete') label.textContent = `${scan.downloaded || 0} subtitles added; ${scan.existing || 0} already had subtitles; ${scan.no_match || 0} had no matching release.`;
  else label.textContent = scan.message || '';
  clearTimeout(subtitlePoll);
  if (scan.status === 'running') subtitlePoll = setTimeout(loadSubtitleAccount, 3000);
}
async function loadSubtitleAccount() {
  if (!subtitleAccountForm) return;
  try { renderSubtitleAccount(await get('/api/subtitles/account')); }
  catch (error) { document.getElementById('subtitle-account-status').textContent = error.message; }
}
subtitleAccountForm?.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = subtitleAccountForm.querySelector('button[type=submit]');
  const label = document.getElementById('subtitle-account-status');
  const values = Object.fromEntries(new FormData(subtitleAccountForm));
  button.disabled = true;
  label.textContent = 'Checking your OpenSubtitles account…';
  try {
    const data = await post('/api/subtitles/account', values);
    subtitleAccountForm.reset();
    if (automaticSubtitles) { automaticSubtitles.checked = true; updateSubtitleSwitch(); }
    renderSubtitleAccount(data);
    showToast(data.message);
    await loadSubtitleAccount();
  } catch (error) { label.textContent = error.message; }
  finally { button.disabled = false; }
});
document.getElementById('subtitle-disconnect')?.addEventListener('click', async () => {
  try { await del('/api/subtitles/account'); await loadSubtitleAccount(); }
  catch (error) { document.getElementById('subtitle-account-status').textContent = error.message; }
});
document.getElementById('subtitle-scan')?.addEventListener('click', async (event) => {
  event.currentTarget.disabled = true;
  try { await post('/api/subtitles/scan'); await loadSubtitleAccount(); }
  catch (error) { document.getElementById('subtitle-scan-status').textContent = error.message; event.currentTarget.disabled = false; }
});
loadSubtitleAccount();
window.addEventListener('pagehide', () => clearTimeout(subtitlePoll));

/* ── Account ────────────────────────────────────────────────────────────── */

document.getElementById('password-form')?.addEventListener('submit', async (event) => {
  event.preventDefault();
  const passwordForm = event.currentTarget;
  const result = passwordForm.querySelector('.form-status');
  const data = new FormData(passwordForm);
  const button = passwordForm.querySelector('button');
  button.disabled = true;
  result.textContent = 'Changing…';
  try {
    const response = await post('/api/account/password', {
      current_password: data.get('current_password'),
      new_password: data.get('new_password'),
    });
    passwordForm.reset();
    result.textContent = response.message;
    showToast('Password changed');
  } catch (error) {
    result.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

document.getElementById('clear-history')?.addEventListener('click', async (event) => {
  const button = event.currentTarget;
  const confirmed = await confirmDialog({
    title: 'Clear watch history?',
    message: 'Played flags and resume positions will be removed for every device you watch on. Your files are not touched.',
    confirmLabel: 'Clear history',
  });
  if (!confirmed) return;
  button.disabled = true;
  try {
    const result = await post('/api/account/history/clear');
    showToast(`Cleared ${result.cleared} item${result.cleared === 1 ? '' : 's'}`);
  } catch (error) {
    toastError(error);
  } finally {
    button.disabled = false;
  }
});

/* ── Server ─────────────────────────────────────────────────────────────── */

async function loadStats() {
  const summary = document.getElementById('stats-summary');
  const folders = document.getElementById('stats-folders');
  if (!summary) return;
  try {
    const stats = await get('/api/library/stats');
    const parts = [
      `${stats.counts.Movie || 0} films`,
      stats.counts.Series ? `${stats.counts.Series} series` : null,
      stats.counts.Episode ? `${stats.counts.Episode} episodes` : null,
      stats.total_size ? `${formatBytes(stats.total_size)} on disk` : null,
    ].filter(Boolean);
    summary.textContent = parts.join(' · ');
    replace(folders,
      h('div', { text: stats.library_remote }),
      h('div', { class: 't-mute', text: stats.library_path }),
    );
  } catch (error) {
    summary.textContent = error.message;
  }
}

document.getElementById('refresh-library')?.addEventListener('click', async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  const label = button.textContent;
  button.textContent = 'Rescanning…';
  try {
    await post('/api/library/trickplay');  // reuses the authenticated library ping
  } catch { /* the rescan endpoint below is the real action */ }
  try {
    await post('/api/library/refresh');
    showToast('Library rescan started');
  } catch (error) {
    toastError(error);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
});

document.getElementById('trickplay')?.addEventListener('click', async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const result = await post('/api/library/trickplay');
    showToast(result.message, { tone: result.ok ? 'success' : 'warn' });
  } catch (error) {
    toastError(error);
  } finally {
    button.disabled = false;
  }
});

/* ── Library maintenance ────────────────────────────────────────────────── */

async function loadMovies() {
  const list = document.getElementById('movie-list');
  if (!list) return;
  try {
    const data = await get('/api/library/movies');
    if (!data.movies.length) {
      replace(list, statePanel({
        tone: 'empty', compact: true, mark: 'library',
        title: 'No films in the library yet',
        message: 'Downloads appear here once your media server has indexed them.',
      }));
      return;
    }
    replace(list, data.movies.map((movie) => h('div', { class: 'maintenance-item' },
      h('div', { class: 'maintenance-art' }, icon('film', { size: 18 })),
      h('div', {},
        h('strong', { text: movie.name }),
        h('small', { text: movie.year ? `${movie.year}${movie.has_poster ? '' : ' · no poster'}` : 'Year unknown' }),
      ),
      h('button', {
        class: 'btn btn-quiet-danger',
        type: 'button',
        onclick: async (event) => {
          // Capture the element now: `currentTarget` is null once dispatch
          // finishes, which is long before the confirmation resolves.
          const button = event.currentTarget;
          const confirmed = await confirmDialog({
            title: 'Delete this film?',
            message: `“${movie.name}” and its media file will be permanently deleted. This cannot be undone.`,
            confirmLabel: 'Delete',
          });
          if (!confirmed) return;
          button.disabled = true;
          try {
            await del(`/api/library/movies/${movie.id}`);
            showToast('Deleted');
            loadMovies();
          } catch (error) { toastError(error); button.disabled = false; }
        },
      }, 'Delete'),
    )));
  } catch (error) {
    replace(list, statePanel({
      tone: 'error', compact: true, mark: 'alert',
      title: 'Could not read the library', message: error.message, detail: error.detail,
    }));
  }
}

document.getElementById('repair-posters')?.addEventListener('click', async (event) => {
  const button = event.currentTarget;
  const result = document.getElementById('poster-status');
  button.disabled = true;
  result.textContent = 'Repairing…';
  try {
    const data = await post('/api/library/posters/repair');
    result.textContent = `Repaired ${data.repaired} poster${data.repaired === 1 ? '' : 's'}` +
      (data.unmatched?.length ? `, ${data.unmatched.length} unmatched` : '');
    loadMovies();
  } catch (error) {
    result.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

/* ── Metadata matching ──────────────────────────────────────────────────── */

document.getElementById('scan-metadata')?.addEventListener('click', async (event) => {
  const button = event.currentTarget;
  const result = document.getElementById('metadata-results');
  const label = document.getElementById('metadata-status');
  button.disabled = true;
  label.textContent = 'Asking your metadata provider…';
  replace(result);
  try {
    const data = await post('/api/library/metadata/scan');
    if (!data.items.length) {
      label.textContent = data.skipped.length
        ? `Everything else is complete. ${data.skipped.length} item${data.skipped.length === 1 ? '' : 's'} could not be matched automatically.`
        : 'Every title already has full details.';
      if (data.skipped.length) renderSkipped(result, data.skipped);
      return;
    }
    label.textContent = `${data.items.length} title${data.items.length === 1 ? '' : 's'} could be improved.`;
    renderProposals(result, data);
  } catch (error) {
    label.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

function renderSkipped(mount, skipped) {
  replace(mount, h('div', { class: 'maintenance-list' }, skipped.map((entry) =>
    h('div', { class: 'maintenance-item' },
      h('div', { class: 'maintenance-art' }, icon('alert', { size: 18 })),
      h('div', {},
        h('strong', { text: entry.name }),
        h('small', { text: entry.reason === 'episode'
          ? 'Looks like a TV episode filed as a film — importing it as a series would be the right fix.'
          : 'No match found for this title.' }),
      ),
      null,
    ))));
}

function renderProposals(mount, data) {
  const list = h('div', {});
  const selections = new Map();

  data.items.forEach((entry) => {
    const top = entry.candidates[0];
    if (!top) return;
    const checked = { value: true };

    list.append(h('div', { class: 'proposal' },
      top.image
        ? h('img', { class: 'proposal-art', src: top.image, alt: '', loading: 'lazy' })
        : h('div', { class: 'proposal-art' }),
      h('div', {},
        h('div', { class: 'proposal-change' },
          h('span', { class: 'from', text: entry.name }),
          h('span', { text: '→' }),
          h('span', { class: 'to', text: `${top.name}${top.year ? ` (${top.year})` : ''}` }),
        ),
        h('p', { class: 'proposal-overview', text: top.overview || 'No description available.' }),
        h('div', { style: { marginTop: 'var(--s3)', display: 'flex', alignItems: 'center', gap: 'var(--s3)' } },
          h('label', { class: 'switch' },
            h('input', {
              type: 'checkbox',
              checked: true,
              onchange: (event) => { checked.value = event.currentTarget.checked; },
            }),
            h('span', { class: 'switch-track' }),
            h('span', { class: 'switch-text' }, h('small', { text: 'Apply this match' })),
          ),
          entry.candidates.length > 1
            ? h('span', { class: 't-mute t-xs', text: `${entry.candidates.length} possible matches` })
            : null,
        ),
      ),
    ));

    selections.set(entry.id, { entry, candidate: top, checked });
  });

  const applyButton = h('button', { class: 'btn btn-primary', type: 'button' }, 'Apply selected');
  applyButton.addEventListener('click', async () => {
    applyButton.disabled = true;
    let applied = 0;
    for (const [itemId, selection] of selections) {
      if (!selection.checked.value) continue;
      applyButton.textContent = `Applying ${applied + 1} of ${selections.size}…`;
      try {
        await post('/api/library/metadata/apply', {
          item_id: itemId,
          candidate: selection.candidate.result,
        });
        applied += 1;
      } catch (error) {
        toastError(error);
      }
    }
    applyButton.disabled = false;
    applyButton.textContent = 'Apply selected';
    document.getElementById('metadata-status').textContent =
      `${applied} title${applied === 1 ? '' : 's'} updated.`;
    showToast(`${applied} title${applied === 1 ? '' : 's'} updated`);
    clear(mount);
    loadMovies();
  });

  mount.append(list, h('div', { style: { marginTop: 'var(--s5)' } }, applyButton));
}

/* ── Members ────────────────────────────────────────────────────────────── */

async function loadAccounts() {
  const list = document.getElementById('account-list');
  if (!list) return;
  try {
    const data = await get('/api/accounts');
    replace(list, data.users.map((user) => h('div', { class: 'maintenance-item' },
      h('div', { class: 'maintenance-art' }, icon('user', { size: 18 })),
      h('div', {},
        h('strong', { text: user.name }),
        h('small', { text: user.is_current ? 'You · signed in' : user.is_admin ? 'Administrator' : 'Member' }),
      ),
      user.is_current ? null : h('button', {
        class: 'btn btn-quiet-danger',
        type: 'button',
        onclick: async (event) => {
          const button = event.currentTarget;
          const confirmed = await confirmDialog({
            title: `Remove ${user.name}?`,
            message: 'Their account, watch history and resume positions are deleted. Their files stay in the library.',
            confirmLabel: 'Remove member',
          });
          if (!confirmed) return;
          button.disabled = true;
          try { await del(`/api/accounts/${user.id}`); loadAccounts(); showToast('Member removed'); }
          catch (error) { toastError(error); button.disabled = false; }
        },
      }, 'Remove'),
    )));
  } catch (error) {
    replace(list, statePanel({
      tone: 'error', compact: true, mark: 'alert',
      title: 'Could not load members', message: error.message,
    }));
  }
}

document.getElementById('account-form')?.addEventListener('submit', async (event) => {
  event.preventDefault();
  const accountForm = event.currentTarget;
  const result = accountForm.querySelector('.form-status');
  const button = accountForm.querySelector('button');
  const data = new FormData(accountForm);
  button.disabled = true;
  result.textContent = 'Creating…';
  try {
    await post('/api/accounts', { name: data.get('name'), password: data.get('password') });
    accountForm.reset();
    result.textContent = 'Member created.';
    showToast('Member created');
    loadAccounts();
  } catch (error) {
    result.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

/* ── Section highlighting ───────────────────────────────────────────────── */

function trackSections() {
  const links = [...document.querySelectorAll('.settings-nav a')];
  const sections = links
    .map((link) => document.querySelector(link.getAttribute('href')))
    .filter(Boolean);
  if (!sections.length || !('IntersectionObserver' in window)) return;

  const observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting)
      .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
    if (!visible) return;
    links.forEach((link) => {
      link.setAttribute('aria-current', String(link.getAttribute('href') === `#${visible.target.id}`));
    });
  }, { rootMargin: '-20% 0px -70% 0px' });

  sections.forEach((section) => observer.observe(section));
}

/* ── Start ──────────────────────────────────────────────────────────────── */

fillLanguages();   // usable immediately, refined once the saved value arrives
loadSettings();
loadStats();
if (isAdmin) {
  loadMovies();
  loadAccounts();
}
trackSections();
