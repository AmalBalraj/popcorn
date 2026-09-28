import { get, setUnauthorizedHandler } from './api.js';
import { h, initials } from './dom.js';

/* Chrome that every page shares: header behaviour, the profile menu, install
 * prompt, the download badge, and the service worker. */

function startHeader() {
  const header = document.getElementById('app-header');
  if (!header) return;
  const overHero = header.classList.contains('is-over-hero');
  let ticking = false;

  const sync = () => {
    ticking = false;
    header.classList.toggle('is-solid', window.scrollY > (overHero ? 40 : 8));
  };
  sync();
  window.addEventListener('scroll', () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(sync);
  }, { passive: true });
}

function startProfileMenu() {
  const button = document.getElementById('profile-button');
  const menu = document.getElementById('profile-menu');
  if (!button || !menu) return;

  const close = ({ restoreFocus = false } = {}) => {
    if (menu.hidden) return;
    menu.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    if (restoreFocus) button.focus();
  };
  const open = () => {
    menu.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    menu.querySelector('.menu-item:not([hidden])')?.focus();
  };

  button.addEventListener('click', (event) => {
    event.stopPropagation();
    menu.hidden ? open() : close();
  });
  document.addEventListener('click', (event) => {
    if (!menu.hidden && !menu.contains(event.target) && event.target !== button) close();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') close({ restoreFocus: true });
  });
  // Arrow keys move between items the way a menu should.
  menu.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    const items = [...menu.querySelectorAll('.menu-item:not([hidden])')];
    const index = items.indexOf(document.activeElement);
    if (index === -1) return;
    event.preventDefault();
    const next = event.key === 'ArrowDown' ? index + 1 : index - 1;
    items[(next + items.length) % items.length].focus();
  });
}

async function startAccount() {
  const nameNode = document.getElementById('profile-name');
  const roleNode = document.getElementById('profile-role');
  const initialNode = document.getElementById('profile-initial');
  try {
    const account = await get('/api/account');
    if (nameNode) nameNode.textContent = account.username || 'Signed in';
    if (roleNode) roleNode.textContent = account.is_admin ? 'Administrator' : 'Member';
    if (initialNode) initialNode.textContent = initials(account.username);
    document.body.dataset.admin = String(Boolean(account.is_admin));
  } catch {
    if (initialNode) initialNode.textContent = '·';
  }
}

let deferredInstall = null;
function startInstall() {
  const button = document.getElementById('install-app');
  if (!button) return;
  const standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone;
  const isIos = /iphone|ipad|ipod/i.test(navigator.userAgent);

  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    deferredInstall = event;
    if (!standalone) button.hidden = false;
  });
  if (isIos && !standalone) button.hidden = false;

  button.addEventListener('click', async () => {
    if (deferredInstall) {
      deferredInstall.prompt();
      await deferredInstall.userChoice;
      deferredInstall = null;
      button.hidden = true;
    } else if (isIos) {
      window.alert('Tap Share, then choose “Add to Home Screen”.');
    }
  });
}

/** Live count of running downloads, shown on the Downloads tab. */
export function startDownloadBadge() {
  const badge = document.getElementById('tab-download-badge');
  if (!badge) return;
  let timer;

  const tick = async () => {
    if (!document.hidden) {
      try {
        const { jobs } = await get('/api/jobs?scope=active&kind=download');
        const count = (jobs || []).length;
        badge.hidden = count === 0;
        badge.textContent = count > 9 ? '9+' : String(count);
      } catch { /* the badge is decorative; failures stay silent */ }
    }
    timer = setTimeout(tick, document.hidden ? 30000 : 15000);
  };
  tick();
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) { clearTimeout(timer); tick(); }
  });
}

function startServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/service-worker.js').catch(() => {});
  });
}

export function initShell() {
  setUnauthorizedHandler(() => {
    const next = encodeURIComponent(location.pathname + location.search);
    location.href = `/login?next=${next}`;
  });
  startHeader();
  startProfileMenu();
  startAccount();
  startInstall();
  startDownloadBadge();
  startServiceWorker();
}

/** Escape text for the rare places a template string is unavoidable. */
export function escapeHtml(value) {
  const node = h('div');
  node.textContent = value ?? '';
  return node.innerHTML;
}
