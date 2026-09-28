import { get } from '../core/api.js';
import { h, replace } from '../core/dom.js';
import { heroSection } from '../components/hero.js';
import { row, skeletonRow } from '../components/row.js';
import { statePanel, errorPanel } from '../components/state.js';

/* Home.
 *
 * Answers "what should I watch right now" first — resume, then what's new,
 * then everything else — and only falls back to browsing when the library
 * has nothing to offer.
 */

const heroMount = document.getElementById('home-hero');
const rowsMount = document.getElementById('home-rows');
const emptyMount = document.getElementById('home-empty');

function showSkeleton() {
  const fragment = document.createDocumentFragment();
  fragment.append(skeletonRow({ wide: true, count: 3 }));
  fragment.append(skeletonRow({ count: 7 }));
  fragment.append(skeletonRow({ count: 7 }));
  replace(rowsMount, fragment);
}

async function load() {
  showSkeleton();
  replace(emptyMount);
  replace(rowsMount).append(skeletonRow({ count: 7 }));

  let data;
  try {
    data = await get('/api/home');
  } catch (error) {
    replace(rowsMount);
    replace(heroMount);
    replace(emptyMount, errorPanel(error, {
      subject: 'your library',
      onRetry: load,
    }));
    return;
  }

  replace(heroMount);
  if (data.hero?.length) heroMount.append(heroSection(data.hero));

  const nodes = (data.rows || []).map((entry) => row({
    title: entry.title,
    items: entry.items,
    kind: entry.kind === 'wide' ? 'wide' : 'poster',
    href: entry.href || null,
    priorityCount: entry.kind === 'wide' ? 3 : 5,
  }));

  if (!nodes.length) {
    replace(rowsMount);
    replace(emptyMount, statePanel({
      tone: 'empty',
      mark: 'library',
      title: 'Your library is empty',
      message: 'Find a film, download it, and it will appear here ready to watch.',
      actions: [
        { label: 'Find something to watch', primary: true, onClick: () => { location.href = '/search'; } },
      ],
    }));
    return;
  }

  replace(rowsMount, nodes);
  // A film can be downloaded and land in the library while Home is open.
  if (data.empty) {
    replace(emptyMount, statePanel({
      tone: 'empty',
      title: 'Nothing to show yet',
      message: 'Downloaded titles appear here once your media server has indexed them.',
    }));
  }
}

load();

// Refresh when returning to the tab, so a finished download shows up.
let lastLoad = Date.now();
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && Date.now() - lastLoad > 60000) {
    lastLoad = Date.now();
    load();
  }
});
