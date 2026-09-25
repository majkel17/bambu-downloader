/* Bambu Downloader frontend */
'use strict';

let pendingTfaKey = null;
let loginEmail = '';
let loginRegion = 'global';
let eventTimer = null;

// ------------------------------------------------------------ CSP-safe events
// The server sends a strict CSP (script-src 'self'; no 'unsafe-inline'), so
// inline onclick/onkeydown/oninput/onchange attributes NEVER run — buttons
// render but do nothing (that's the "can't click anything" bug). All wiring
// lives here instead: addEventListener for static markup, event delegation
// (data-action attributes) for dynamically rendered lists.
function wireEvents() {
  // Enter-to-submit for single-input forms.
  for (const [inputId, action] of [
    ['modelUrl', 'download'], ['collUrl', 'add-collection'],
    ['loginPassword', 'login'], ['otpCode', 'verify'],
  ]) {
    const input = document.getElementById(inputId);
    if (input) input.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') handleAction(action, ev.target);
    });
  }
  // Library search box: filter on every keystroke; Clear button resets it.
  const libSearch = document.getElementById('libSearch');
  // Server-side search, debounced so a query is sent ~250 ms after typing stops.
  let searchTimer = null;
  if (libSearch) libSearch.addEventListener('input', () => {
    updateLibraryRoute();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { libShown = LIB_PAGE; loadModels(); }, 250);
  });
  const evKind = document.getElementById('evKind');
  if (evKind) evKind.addEventListener('change', () => {
    activityKind = evKind.value;
    history.replaceState(null, '', '#activity' + (activityKind ? '?kind=' + activityKind : ''));
    loadEvents();
  });
  // Static buttons.
  for (const el of document.querySelectorAll('[data-action]')) {
    el.addEventListener('click', (ev) => handleAction(el.dataset.action, ev.target));
  }
  // Delegated clicks for lists rendered as innerHTML (survive re-renders).
  for (const [containerId, names] of [
    ['labelChips', ['set-lib-filter']],
    ['modelGrid', ['download-file']],
    ['skippedList', ['retry-skipped']],
    ['mineList', ['follow-mine']],
    ['collList', ['sync-now', 'coll-toggle', 'coll-remove']],
  ]) {
    const host = document.getElementById(containerId);
    if (host) host.addEventListener('click', (ev) => {
      const el = ev.target.closest('[data-action]');
      if (el && host.contains(el) && names.includes(el.dataset.action)) {
        handleAction(el.dataset.action, el);
      }
    });
  }
  // Collection rows: interval/plates selects + overlay backdrop close.
  const collList = document.getElementById('collList');
  if (collList) {
    collList.addEventListener('change', (ev) => {
      const row = ev.target.closest('.coll-item');
      if (!row) return;
      const cid = row.dataset.id;
      if (ev.target.matches('.interval input')) setInterval_(cid, ev.target.value);
      if (ev.target.matches('.interval select')) setPlatesMode(cid, ev.target.value);
    });
  }
  const removeModal = document.getElementById('removeModal');
  if (removeModal) removeModal.addEventListener('click', (ev) => {
    if (ev.target === ev.currentTarget) closeRemoveModal();  // backdrop click
  });
}

// Dispatch a data-action click to its handler. Used for both static buttons
// and delegated list rows; ev is only needed for focus restoration.
function handleAction(action, el) {
  const actions = {
    'tab': () => navigate(el.dataset.tab),
    'download': () => doDownload(),
    'lib-clear': () => { document.getElementById('libSearch').value = ''; updateLibraryRoute(); libShown = LIB_PAGE; loadModels(); },
    'load-more': () => loadMoreModels(),
    'set-lib-filter': () => setLibFilter(el.dataset.label),
    'download-file': () => downloadFile(el.dataset.id, el.dataset.name, el),
    'add-collection': () => addCollection(),
    'refresh-mine': () => refreshMyCollections(),
    'follow-mine': () => followMine(el.dataset.cid, el.dataset.slug),
    'sync-now': () => syncNow(el.dataset.cid),
    'retry-skipped': () => retrySkipped(el.dataset.did),
    'coll-toggle': () => toggleColl(el.dataset.cid, el.dataset.enable === 'true'),
    'coll-remove': () => removeColl(el.dataset.cid),
    'modal-cancel': () => closeRemoveModal(),
    'modal-remove-keep': () => doRemoveColl(false),
    'modal-remove-delete': () => doRemoveColl(true),
    'login': () => doLogin(),
    'verify': () => doVerify(),
    'token-login': () => doTokenLogin(),
    'refresh-status': () => loadAll(),
    'api-save': () => saveApiKey(),
    'api-clear': () => clearApiKey(),
  };
  const fn = actions[action];
  if (fn) fn();
}

// ---------------------------------------------------------------- helpers
// API key (only needed when the server sets BND_API_KEY). Stored locally.
function getApiKey() { return localStorage.getItem('bnd_api_key') || ''; }
function setApiKey(k) { k ? localStorage.setItem('bnd_api_key', k) : localStorage.removeItem('bnd_api_key'); }

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  const key = getApiKey();
  if (key) headers['X-API-Key'] = key;
  const res = await fetch(path, { ...opts, headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (res.status === 401 && !key && !path.includes('/api/auth')) {
    // Server requires an API key we don't have — ask once, then retry.
    const k = prompt('This server requires an API key (BND_API_KEY). Enter it:');
    if (k) { setApiKey(k.trim()); return api(path, opts); }
  }
  if (!res.ok) {
    throw new Error((data && data.detail) || `HTTP ${res.status}`);
  }
  return data;
}

function toast(msg, kind = '') {
  const wrap = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), kind === 'err' ? 8000 : 4000);
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const time = d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  // The log now spans weeks: prefix the date for anything not from today.
  return d.toDateString() === new Date().toDateString()
    ? time
    : d.toLocaleDateString([], {day: '2-digit', month: '2-digit'}) + ' ' + time;
}

function fmtBytes(n) {
  if (!n && n !== 0) return '';
  if (n > 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  if (n > 1024) return (n / 1024).toFixed(0) + ' KB';
  return n + ' B';
}

// ------------------------------------------------------------------ routing
// The active tab (and the library's filter/search) live in the URL hash —
// #library?label=…&q=… — so a refresh or a bookmark lands on the same view.
// Tab clicks add a history entry (back/forward switch tabs); filter and
// search updates replace it, so typing doesn't flood the history.
const TABS = ['download', 'library', 'collections', 'activity', 'settings'];

function parseRoute() {
  const [name, query] = location.hash.slice(1).split('?');
  return { tab: TABS.includes(name) ? name : 'download', params: new URLSearchParams(query || '') };
}

function navigate(tab) {
  if (location.hash.slice(1).split('?')[0] === tab) showTab(tab);
  else location.hash = tab;  // -> hashchange -> applyRoute
}

function applyRoute() {
  const { tab, params } = parseRoute();
  if (tab === 'library') {
    libFilter.label = params.get('label');
    libShown = LIB_PAGE;
    document.getElementById('libSearch').value = params.get('q') || '';
  }
  if (tab === 'activity') {
    activityKind = params.get('kind') || '';
    document.getElementById('evKind').value = activityKind;
  }
  showTab(tab);
}

// Mirror the library filter/search into the hash without a history entry.
function updateLibraryRoute() {
  const params = new URLSearchParams();
  if (libFilter.label) params.set('label', libFilter.label);
  const q = document.getElementById('libSearch').value.trim();
  if (q) params.set('q', q);
  const qs = params.toString();
  history.replaceState(null, '', '#library' + (qs ? '?' + qs : ''));
}

function showTab(name) {
  for (const tab of document.querySelectorAll('.tab')) tab.hidden = true;
  document.getElementById('tab-' + name).hidden = false;
  for (const btn of document.querySelectorAll('nav.tabs button')) {
    btn.classList.toggle('active', btn.dataset.tab === name);
  }
  if (name === 'activity') startEventPolling();
  else stopEventPolling();
  if (name === 'library') { loadModels(); loadLabels(); }
  if (name === 'collections') { startMinePolling(); loadCollections(); loadSkipped(); }
  else stopMinePolling();
}

// ------------------------------------------------------------------ status
async function loadStatus() {
  try {
    const s = await api('/api/status');
    const badge = document.getElementById('authBadge');
    if (s.authenticated) {
      badge.textContent = '✓ ' + (s.email || 'signed in');
      badge.className = 'auth-badge on';
    } else if (s.token_invalid) {
      badge.textContent = 'Sign-in expired';
      badge.className = 'auth-badge off';
    } else {
      badge.textContent = 'Not signed in';
      badge.className = 'auth-badge off';
    }
    document.getElementById('sysInfo').textContent =
      `Downloads directory: ${s.download_dir} · ${s.model_count} models · ` +
      `${s.collection_count} collections · scheduler ${s.scheduler.running ? 'running' : 'stopped'}`;
    // Download queue: queued/active items (Semaphore(2) can hold 2 active
    // plus any number queued behind it during a sync).
    const q = s.downloads || [];
    const queueBadge = document.getElementById('dlQueueBadge');
    if (queueBadge) {
      const active = q.filter(d => d.state === 'active').length;
      const queued = q.length - active;
      queueBadge.hidden = q.length === 0;
      queueBadge.textContent = q.length
        ? `⬇ ${active ? active + ' active' : ''}${active && queued ? ' · ' : ''}${queued ? queued + ' queued' : ''}`
        : '';
    }
  } catch (e) {
    console.error('status failed', e);
  }
}

// --------------------------------------------------------------- downloads
async function doDownload() {
  const url = document.getElementById('modelUrl').value.trim();
  if (!url) return;
  const btn = document.getElementById('dlBtn');
  const result = document.getElementById('dlResult');
  btn.disabled = true;
  btn.textContent = 'Downloading…';
  result.innerHTML = '';
  try {
    const r = await api('/api/download', { method: 'POST', body: { url } });
    if (r.status === 'exists') {
      result.innerHTML = `<p class="muted">✓ Already in your library — not duplicated.</p>`;
    } else {
      result.innerHTML = `<p class="muted">✓ Downloaded <b>${esc(r.title)}</b> — ${fmtBytes(r.size)}<br>Saved to <code>${esc(r.path)}</code></p>`;
      toast(`Downloaded “${r.title}”`, 'ok');
    }
    document.getElementById('modelUrl').value = '';
    loadStatus();
  } catch (e) {
    result.innerHTML = `<p class="muted" style="color:var(--red)">✗ ${esc(e.message)}</p>`;
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Download';
  }
}

// ----------------------------------------------------------------- library
let libFilter = { label: null };  // null = All
// Library pagination: page size + "shown so far" (reset on filter change).
const LIB_PAGE = 60;
let libShown = LIB_PAGE;

async function loadLabels() {
  try {
    const labels = await api('/api/model-labels');
    const bar = document.getElementById('labelChips');
    const total = labels.reduce((s, l) => s + l.count, 0);
    const chips = [{ label: 'All', count: total, active: libFilter.label === null }]
      .concat(labels.map(l => ({ ...l, active: libFilter.label === l.label })));
    bar.innerHTML = chips.map(c =>
      `<button class="chip ${c.active ? 'active' : ''}" data-action="set-lib-filter" data-label="${esc(c.label)}">${esc(c.label)}<span class="n">${c.count}</span></button>`
    ).join('');
  } catch (e) { console.error(e); }
}

function setLibFilter(label) {
  libFilter.label = label === 'All' ? null : label;
  libShown = LIB_PAGE;  // new filter -> back to the first page
  updateLibraryRoute();
  loadModels();
  loadLabels();
}

function modelCard(m) {
  const mwUrl = `https://makerworld.com/en/models/${m.design_id}${m.slug ? '-' + m.slug : ''}`;
  const img = m.cover_url
    ? `<img loading="lazy" src="/thumb?url=${encodeURIComponent(m.cover_url)}" alt="" class="cover-img">`
    : `<div class="cover-fallback">🖨</div>`;
  const origin = m.collection_title
    ? `<span class="tag origin" title="${esc(m.collection_title)}">🗂 ${esc(m.collection_title)}</span>`
    : `<span class="tag origin" title="Downloaded by URL, not via a collection">🏷 Manual download</span>`;
  const creator = m.creator ? `by ${esc(m.creator)}` : '';
  const date = m.created_at ? new Date(m.created_at).toLocaleDateString() : '';
  return `
  <div class="model-card">
    <a class="cover" href="${esc(mwUrl)}" target="_blank" rel="noopener noreferrer" title="View on MakerWorld">${img}</a>
    <div class="meta title" title="${esc(m.title)}">${esc(m.title)}</div>
    <div class="meta sub">
      ${creator ? `<div class="creator">${creator}</div>` : ''}
      ${date ? `<div class="date">${date}</div>` : ''}
      design #${m.design_id}${m.profile_id ? ' · plate #' + m.profile_id : ''}<br>
      ${fmtBytes(m.file_size)} · <span title="${esc(m.file_path)}">${esc(m.filename)}</span>
    </div>
    <div class="meta labels">
      ${origin}
    </div>
    <div class="meta actions">
      <button class="ghost" data-action="download-file" data-id="${m.id}" data-name="${esc(m.filename)}">⬇ Download</button>
      <a class="mw-link" href="${esc(mwUrl)}" target="_blank" rel="noopener noreferrer">↗ MakerWorld</a>
    </div>
  </div>`;
}

// Save a stored model file to the user's device. Without an API key a plain
// link streams straight to disk; with one, the X-API-Key header can't ride
// on a link, so the file is fetched into a blob first.
async function downloadFile(id, name, btn) {
  const url = `/api/models/${id}/file`;
  const save = (href) => {
    const a = document.createElement('a');
    a.href = href;
    a.download = name || '';
    document.body.appendChild(a);
    a.click();
    a.remove();
  };
  const key = getApiKey();
  if (!key) { save(url); return; }
  btn.disabled = true;
  try {
    const res = await fetch(url, { headers: { 'X-API-Key': key } });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { detail = (await res.json()).detail || detail; } catch (e) { /* not JSON */ }
      throw new Error(detail);
    }
    const blobUrl = URL.createObjectURL(await res.blob());
    save(blobUrl);
    setTimeout(() => URL.revokeObjectURL(blobUrl), 60000);
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

// Query for /api/models: label filter + search (server-side, so it covers
// the whole library, not just the pages loaded so far).
function libQuery(offset) {
  const params = new URLSearchParams();
  if (libFilter.label) params.set('label', libFilter.label);
  const q = document.getElementById('libSearch').value.trim();
  if (q) params.set('q', q);
  params.set('limit', String(LIB_PAGE));
  params.set('offset', String(offset));
  return '/api/models?' + params.toString();
}

// Responses can arrive out of order while typing; only the latest counts.
let libRequestSeq = 0;

async function loadModels() {
  const seq = ++libRequestSeq;
  try {
    const r = await api(libQuery(0));
    if (seq !== libRequestSeq) return;  // a newer search superseded this one
    const grid = document.getElementById('modelGrid');
    const empty = document.getElementById('libEmpty');
    const q = document.getElementById('libSearch').value.trim();
    document.getElementById('libCount').textContent = `${r.total} models`;
    empty.hidden = r.models.length > 0;
    empty.textContent = q ? `No models match “${q}”.` : 'Nothing downloaded yet.';
    grid.innerHTML = r.models.map(modelCard).join('');
    libShown = r.models.length;
    updateLoadMore(r.total);
  } catch (e) {
    toast(e.message, 'err');
  }
}

// Re-fetch the next page and append its cards (Load more button).
async function loadMoreModels() {
  const btn = document.getElementById('libMore');
  if (!btn) return;
  btn.disabled = true;
  try {
    const seq = libRequestSeq;
    const r = await api(libQuery(libShown));
    if (seq !== libRequestSeq) return;  // filter/search changed meanwhile
    const grid = document.getElementById('modelGrid');
    grid.insertAdjacentHTML('beforeend', r.models.map(modelCard).join(''));
    libShown += r.models.length;
    updateLoadMore(r.total);
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

function updateLoadMore(total) {
  const btn = document.getElementById('libMore');
  if (!btn) return;
  const done = libShown >= total;
  btn.hidden = done;
  if (!done) btn.textContent = `Load more (${libShown}/${total})`;
}

// ----------------------------------------------------------------- collections
let mineTimer = null;

async function addCollection() {
  const url = document.getElementById('collUrl').value.trim();
  if (!url) return;
  const interval = parseInt(document.getElementById('collInterval').value, 10);
  try {
    const c = await api('/api/collections', { method: 'POST', body: { url, sync_interval_minutes: interval } });
    toast(`Following “${c.title}”`, 'ok');
    document.getElementById('collUrl').value = '';
    loadCollections();
    loadMyCollections();
    loadStatus();
  } catch (e) {
    toast(e.message, 'err');
  }
}

// Own MakerWorld collections (hourly-refreshed cache; UI re-polls every 5 min).
async function loadMyCollections() {
  try {
    const r = await api('/api/my-collections');
    const el = document.getElementById('mineList');
    const empty = document.getElementById('mineEmpty');
    const meta = document.getElementById('mineMeta');
    const hint = document.getElementById('mineHint');
    if (!r.authenticated) {
      empty.hidden = false;
      empty.textContent = 'Sign in (Settings) to see your own collections here.';
      meta.textContent = '';
      hint.textContent = '';
      el.innerHTML = '';
      return;
    }
    empty.hidden = r.collections.length > 0;
    if (!r.collections.length) empty.textContent = 'Nothing cached yet — click Refresh, or wait for the next hourly fetch.';
    const when = r.fetched_at ? new Date(r.fetched_at).toLocaleString() : null;
    meta.textContent = when ? `updated ${when}` : 'not fetched yet';
    const rows = r.collections.map(c => {
      const pct = c.design_count ? Math.round(100 * c.downloaded_count / c.design_count) : 0;
      const state = c.downloaded
        ? '<span class="mine-check">✓ all downloaded</span>'
        : (c.downloaded_count > 0
            ? `<span class="mine-partial">✓ ${c.downloaded_count}/${c.design_count} downloaded</span>`
            : `<span class="mine-none">${c.design_count} models · none downloaded</span>`);
      const follow = c.followed
        ? `<span class="tag ok">following</span>`
        : `<button class="ghost" data-action="follow-mine" data-cid="${c.collection_id}" data-slug="${esc(c.slug || String(c.collection_id))}">Follow</button>`;
      const mw = `https://makerworld.com/en/collections/${c.collection_id}${c.slug ? '-' + c.slug : ''}`;
      return `
      <div class="mine-item">
        <a class="title" href="${esc(mw)}" target="_blank" rel="noopener noreferrer">${c.title ? esc(c.title) : 'Collection ' + c.collection_id}</a>
        ${state}
        <div class="mine-progress" title="${pct}% downloaded"><div style="width:${pct}%"></div></div>
        ${follow}
      </div>`;
    }).join('');
    el.innerHTML = rows;
  } catch (e) {
    toast(e.message, 'err');
  }
}

// Follow one of your own collections without typing its URL.
async function followMine(cid, slug) {
  try {
    const url = `https://makerworld.com/en/collections/${cid}-${slug}`;
    await api('/api/collections', { method: 'POST', body: { url, sync_interval_minutes: 360 } });
    toast('Collection followed', 'ok');
    loadCollections();
    loadMyCollections();
    loadStatus();
  } catch (e) { toast(e.message, 'err'); }
}

// Manual refresh of the own-collections cache (normally fetched hourly).
async function refreshMyCollections() {
  try {
    await api('/api/my-collections/refresh', { method: 'POST' });
    toast('Your collections refreshed', 'ok');
    loadMyCollections();
  } catch (e) { toast(e.message, 'err'); }
}

function startMinePolling() {
  loadMyCollections();
  if (!mineTimer) mineTimer = setInterval(loadMyCollections, 5 * 60 * 1000);
}
function stopMinePolling() {
  if (mineTimer) { clearInterval(mineTimer); mineTimer = null; }
}

async function loadCollections() {
  try {
    const list = await api('/api/collections');
    const el = document.getElementById('collList');
    const empty = document.getElementById('collEmpty');
    empty.hidden = list.length > 0;
    el.innerHTML = list.map(c => `
      <div class="coll-item" data-id="${c.collection_id}">
        <span class="title">${esc(c.title || 'Collection ' + c.collection_id)}</span>
        <span class="tag ${c.enabled ? 'ok' : 'warn'}">${c.enabled ? 'active' : 'paused'}</span>
        <span class="muted">${c.last_sync_at ? 'last sync ' + new Date(c.last_sync_at).toLocaleString() : 'never synced'}</span>
        <span class="muted">${c.last_sync_new != null ? c.last_sync_new : 0} new last time</span>
        <span class="interval">
          <input type="number" value="${c.sync_interval_minutes}" min="15" step="15"> min
        </span>
        <span class="interval">
          <select title="Which plates to download on sync">
            <option value="default" ${c.plates_mode !== 'all' ? 'selected' : ''}>default plate</option>
            <option value="all" ${c.plates_mode === 'all' ? 'selected' : ''}>all plates</option>
          </select>
        </span>
        <button class="ghost" data-action="sync-now" data-cid="${c.collection_id}">Sync now</button>
        <button class="ghost" data-action="coll-toggle" data-cid="${c.collection_id}" data-enable="${c.enabled ? 'false' : 'true'}">${c.enabled ? 'Pause' : 'Resume'}</button>
        <button class="danger" data-action="coll-remove" data-cid="${c.collection_id}">Remove</button>
      </div>`).join('');
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function setInterval_(cid, minutes) {
  try {
    await api(`/api/collections/${cid}`, { method: 'PATCH', body: { sync_interval_minutes: parseInt(minutes, 10) || 360 } });
    toast('Interval updated', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

async function setPlatesMode(cid, mode) {
  try {
    await api(`/api/collections/${cid}`, { method: 'PATCH', body: { plates_mode: mode } });
    toast(mode === 'all' ? 'Sync will download every plate' : 'Sync will download the default plate', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

async function toggleColl(cid, enabled) {
  try {
    await api(`/api/collections/${cid}`, { method: 'PATCH', body: { enabled } });
    loadCollections();
  } catch (e) { toast(e.message, 'err'); }
}

// Unfollow dialog: three-way choice (this is destructive, so no bare confirm()).
let pendingRemoveCid = null;

async function removeColl(cid) {
  pendingRemoveCid = cid;
  let text = 'Stop following this collection?';
  try {
    const r = await api(`/api/models?collection_id=${cid}`);
    text = `Stop following this collection? It has ${r.total} downloaded model${r.total === 1 ? '' : 's'} in your library.`;
  } catch (e) { /* count is cosmetic */ }
  document.getElementById('removeModalText').textContent = text;
  document.getElementById('removeModal').hidden = false;
}

function closeRemoveModal() {
  document.getElementById('removeModal').hidden = true;
  pendingRemoveCid = null;
}

async function doRemoveColl(deleteFiles) {
  const cid = pendingRemoveCid;
  if (!cid) return;
  closeRemoveModal();
  try {
    const r = await api(`/api/collections/${cid}?delete_files=${deleteFiles}`, { method: 'DELETE' });
    toast(deleteFiles
      ? (r.deleted_files
          ? `Unfollowed — deleted ${r.deleted_files} file${r.deleted_files === 1 ? '' : 's'} from disk`
          : 'Unfollowed — no files found to delete')
      : 'Collection unfollowed — files kept', 'ok');
    loadCollections();
    loadStatus();
  } catch (e) { toast(e.message, 'err'); }
}

// Models syncs gave up on (BND_MAX_DOWNLOAD_ATTEMPTS model-caused failures).
async function loadSkipped() {
  try {
    const r = await api('/api/skipped-models');
    const card = document.getElementById('skippedCard');
    card.hidden = r.models.length === 0;
    document.getElementById('skippedCount').textContent = r.models.length || '';
    document.getElementById('skippedList').innerHTML = r.models.map(m => {
      const mw = `https://makerworld.com/en/models/${m.design_id}`;
      const coll = m.collection_title ? ` · 🗂 ${esc(m.collection_title)}` : '';
      return `
      <div class="coll-item">
        <a class="title" href="${esc(mw)}" target="_blank" rel="noopener noreferrer">Model #${m.design_id}</a>
        <span class="muted">${m.attempts} attempts · last ${new Date(m.last_attempt_at).toLocaleString()}${coll}</span>
        <span class="muted" style="flex-basis:100%">${esc(m.last_error || '')}</span>
        <button class="ghost" data-action="retry-skipped" data-did="${m.design_id}">Retry on next sync</button>
      </div>`;
    }).join('');
  } catch (e) { console.error(e); }
}

async function retrySkipped(did) {
  try {
    await api(`/api/skipped-models/${did}/retry`, { method: 'POST' });
    toast('Will retry on the next sync', 'ok');
    loadSkipped();
  } catch (e) { toast(e.message, 'err'); }
}

async function syncNow(cid) {
  try {
    const r = await api(`/api/collections/${cid}/sync`, { method: 'POST' });
    toast(r.started ? 'Sync started — watch Activity' : 'Sync already running', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

// ---------------------------------------------------------------- activity
let activityKind = '';  // '' = all, else download / sync / error

async function loadEvents() {
  try {
    const kind = activityKind ? '&kind=' + encodeURIComponent(activityKind) : '';
    const r = await api('/api/events?limit=200' + kind);
    const el = document.getElementById('eventList');
    const empty = document.getElementById('evEmpty');
    empty.hidden = r.events.length > 0;
    el.innerHTML = r.events.map(ev => `
      <div class="event ${ev.kind === 'error' ? 'error' : ''}">
        <time>${fmtTime(ev.ts)}</time>${esc(ev.message)}
      </div>`).join('');
  } catch (e) {
    console.error(e);
  }
}

function startEventPolling() {
  loadEvents();
  // One timer only: re-entering the tab used to stack another interval.
  if (!eventTimer) eventTimer = setInterval(loadEvents, 4000);
}
function stopEventPolling() {
  if (eventTimer) { clearInterval(eventTimer); eventTimer = null; }
}

// -------------------------------------------------------------------- auth
async function doLogin() {
  const email = document.getElementById('loginEmail').value.trim();
  const password = document.getElementById('loginPassword').value;
  const region = document.getElementById('loginRegion').value;
  const btn = document.getElementById('loginBtn');
  if (!email || !password) { toast('Email and password required', 'err'); return; }
  btn.disabled = true;
  try {
    const r = await api('/api/auth/login', { method: 'POST', body: { email, password, region } });
    if (r.step === 'done') {
      toast('Signed in to MakerWorld', 'ok');
      resetLogin();
      loadStatus();
    } else if (r.step === 'email_code') {
      loginEmail = email; loginRegion = region;
      showStep2('Check your email for a 6-digit code.');
      pendingTfaKey = null;
    } else if (r.step === 'totp') {
      loginEmail = email; loginRegion = region;
      pendingTfaKey = r.tfa_key;
      showStep2('Enter the code from your authenticator app.');
    }
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

function showStep2(hint) {
  document.getElementById('loginStep1').hidden = true;
  document.getElementById('loginStep2').hidden = false;
  document.getElementById('stepBadge2').classList.add('on');
  document.getElementById('step2Hint').textContent = hint;
  document.getElementById('otpCode').focus();
}

function resetLogin() {
  document.getElementById('loginStep1').hidden = false;
  document.getElementById('loginStep2').hidden = true;
  document.getElementById('stepBadge2').classList.remove('on');
  document.getElementById('loginPassword').value = '';
  document.getElementById('otpCode').value = '';
  pendingTfaKey = null;
}

async function doVerify() {
  const code = document.getElementById('otpCode').value.trim();
  if (!code) return;
  try {
    await api('/api/auth/verify', {
      method: 'POST',
      body: { email: loginEmail, code, tfa_key: pendingTfaKey || '', region: loginRegion },
    });
    toast('Signed in to MakerWorld', 'ok');
    resetLogin();
    loadStatus();
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function doTokenLogin() {
  const token = document.getElementById('tokenInput').value.trim();
  if (!token) return;
  try {
    await api('/api/auth/token', { method: 'POST', body: { access_token: token } });
    toast('Token accepted — signed in', 'ok');
    document.getElementById('tokenInput').value = '';
    loadStatus();
  } catch (e) {
    toast(e.message, 'err');
  }
}

// ---------------------------------------------------------------- startup
// Broken covers (404/502) should disappear instead of showing a broken-image
// icon; the .cover-fallback underneath shows through. Listener is delegated on
// the grid — CSP forbids inline onerror attributes.
const modelGrid = document.getElementById('modelGrid');
if (modelGrid) modelGrid.addEventListener('error', (ev) => {
  const t = ev.target;
  if (t && t.classList && t.classList.contains('cover-img')) t.remove();
}, true);

function saveApiKey() {
  setApiKey(document.getElementById('apiKeyInput').value.trim());
  toast('API key saved', 'ok');
  loadStatus();
}
function clearApiKey() {
  setApiKey('');
  document.getElementById('apiKeyInput').value = '';
  toast('API key cleared', 'ok');
}

async function loadAll() {
  await loadStatus();
}

wireEvents();
window.addEventListener('hashchange', applyRoute);
loadAll();
setInterval(loadStatus, 30000);

// PWA service worker registration (moved here from an inline script — CSP
// blocks inline scripts, which is also why ALL handlers are wired via JS).
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

// PWA share-target: ?url=... shared from Bambu Handy / Android
const params = new URLSearchParams(location.search);
const sharedUrl = params.get('url');
if (sharedUrl) {
  // Drop ?url= so a refresh doesn't re-submit the shared link.
  history.replaceState(null, '', '/#download');
  showTab('download');
  document.getElementById('modelUrl').value = sharedUrl;
  doDownload();
} else {
  applyRoute();
}