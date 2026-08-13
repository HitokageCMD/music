/* tunebox — client */

const $ = (s) => document.querySelector(s);
const audio = $('#audio');

const state = {
  tab: 'recommend', // 发现 is the default landing tab
  mode: 'song', // song | lyrics
  prefs: { langs: [], genres: [], dislikes: [] },
  libFilter: 'all', // all | liked | phone | playlists  (sub-filter of the 音乐库 tab)
  discover: 0, // index into DISCOVER (0 = 为你推荐)
  playlists: [], // imported 清单 list
  openPlaylist: null, // {id, name} when viewing one playlist's tracks
  lists: { search: [], recommend: [], library: [] },
  onPhone: new Set(), // ids held in IndexedDB
  online: true,
  dl: new Map(), // id -> 0..1 while downloading
  queue: [],
  qi: -1,
  current: null,
  shuffle: false,
  repeat: 'off', // off | all | one
  radio: true,
  loading: false,
  error: null,
  seeking: false,
  stage: false,
  scene: null,
  lyrics: null,
  lyricIdx: -1,
  lyricHold: 0,
};

const esc = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// Scene = a mood, not a literal translation. Each label maps to the query that
// actually returns good songs (measured — "洗澡" literally returns kids' bath
// songs, so it maps to feel-good singalong pop instead). One-line editable.
// 发现 tab categories. First is the personalized engine; the rest are curated
// fresh / by-language / Douyin queries (measured to return good current songs).
// {YR} is replaced with the live year so "new songs" never goes stale.
const DISCOVER = [
  { label: '为你推荐', kind: 'recommend' },
  { label: '华语新歌', kind: 'search', q: '华语新歌 {YR}' },
  { label: '欧美新歌', kind: 'search', q: 'pop hits {YR}' },
  { label: '粤语', kind: 'search', q: '粤语新歌 {YR}' },
  { label: '日语', kind: 'search', q: 'j-pop hits' },
  { label: '韩语', kind: 'search', q: 'kpop {YR}' },
  { label: '抖音', kind: 'search', q: '抖音热歌' },
];

// Preferences. Dislikes are the strong lever: a hard title/artist keyword filter
// applied to all discovery + auto-radio. Language biases queries and orders the
// discover strip. Genre is a soft query hint.
const PREF_LANGS = ['华语', '欧美', '日语', '韩语', '粤语'];
const PREF_GENRES = ['流行', 'R&B', '摇滚', '电子', '民谣', '说唱', '古典', '爵士', '纯音乐'];
const DISLIKES = [
  { label: 'DJ / Remix', keys: ['dj', 'remix', '混音', '舞曲'] },
  { label: 'Live 现场', keys: ['live', '现场', '演唱会', '演唱會'] },
  { label: '翻唱 Cover', keys: ['cover', '翻唱', '翻自'] },
  { label: '纯音乐 / 伴奏', keys: ['纯音乐', '純音樂', 'instrumental', '伴奏', '钢琴版', '鋼琴版', 'piano'] },
  { label: '儿童歌', keys: ['儿童', '兒童', '童谣', '童謠', 'kids'] },
];
// Which discover category belongs to which language, for reordering by preference.
const DISCOVER_LANG = { 华语新歌: '华语', 欧美新歌: '欧美', 粤语: '粤语', 日语: '日语', 韩语: '韩语' };

const SCENES = [
  { label: '派对', q: '派对 嗨歌 party' },
  { label: '开车', q: 'road trip driving songs 兜风' },
  { label: '打扫', q: 'upbeat pop dance hits' },
  { label: '洗澡', q: 'feel good singalong pop hits' },
  { label: 'emo', q: 'emo sad songs 伤感' },
  { label: '专注', q: 'lofi study beats focus' },
  { label: '运动', q: 'workout hype gym songs' },
  { label: '睡前', q: 'chill sleep calm music' },
  { label: '咖啡馆', q: 'cafe jazz bossa nova' },
  { label: '下雨', q: 'rainy day chill 慢歌' },
  { label: '浪漫', q: '浪漫 情歌 love songs' },
  { label: '怀旧', q: '怀旧 经典 throwback 2000s' },
];

const fmt = (s) => {
  if (!isFinite(s) || s < 0) return '0:00';
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
};

async function api(path, opts) {
  const r = await fetch(path, {
    headers: { 'content-type': 'application/json' },
    ...opts,
  });
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return r.status === 204 ? null : r.json();
}

/* ---------- rendering ---------- */

function hilite(line, q) {
  const i = line.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) return esc(line);
  return esc(line.slice(0, i)) + '<b>' + esc(line.slice(i, i + q.length)) + '</b>' + esc(line.slice(i + q.length));
}

function dlBtn(t) {
  const p = state.dl.get(t.id);
  if (p !== undefined) {
    return `<button class="row-dl busy" disabled title="下载中">${Math.round(p * 100)}</button>`;
  }
  if (state.onPhone.has(t.id)) {
    return `<button class="row-dl on" data-unsave="${t.id}" title="已在手机上 — 点击移除">✓</button>`;
  }
  if (!state.online) return '<span></span>';
  return `<button class="row-dl" data-save="${t.id}" title="下载到手机">⤓</button>`;
}

function rowHTML(t, i) {
  const playing = state.current?.id === t.id;
  const onPhone = state.onPhone.has(t.id);
  const inPlaylist = state.tab === 'library' && state.libFilter === 'playlists' && state.openPlaylist;
  // Server-side delete only makes sense for the server-backed library filters.
  const del = state.tab === 'library' && state.libFilter === 'all' || (state.tab === 'library' && state.libFilter === 'liked');
  let snip = '';
  if (t.snippet) snip = `<div class="row-snip">${hilite(t.snippet, $('#q').value.trim())}</div>`;
  else if (t.because) snip = `<div class="row-snip because">因为你听了《${esc(t.because)}》</div>`;
  // "已下载" means this device can play it with the PC off — the thing that
  // actually matters on a phone. Server-side caching only buys a faster start.
  const pill = onPhone
    ? '<span class="pill on">已下载</span>'
    : (t.cached ? '<span class="pill">已缓存</span>' : '');
  // download · add-to-playlist · (remove-from-playlist | server-delete)
  const actions = [
    dlBtn(t),
    state.online ? `<button class="row-dl add" data-add="${t.id}" title="加入歌单">＋</button>` : '',
    inPlaylist ? `<button class="row-act" data-rmpl="${t.id}" title="移出此歌单">✕</button>`
      : (del ? `<button class="row-act" data-del="${t.id}" title="从服务器曲库删除">✕</button>` : ''),
  ].join('');
  return `<div class="row${playing ? ' playing' : ''}" data-i="${i}">
    <img loading="lazy" data-art="${t.id}" alt="" onerror="this.style.visibility='hidden'">
    <div class="row-main">
      <div class="row-title">${esc(t.title)}${pill}</div>
      <div class="row-artist">${esc(t.artist) || '&nbsp;'}</div>
    </div>
    <div class="row-album">${esc(t.album)}</div>
    <div class="row-dur">${fmt(t.duration)}</div>
    <div class="row-actions">${actions}</div>
    ${snip}
  </div>`;
}

/** Every cover in the app goes through here: local blob if we have one, API if not.
 *  One function on purpose — three copies of this drifted apart once already and
 *  the stage kept showing a broken image offline. */
async function setArt(img, id) {
  if (!id) { img.removeAttribute('src'); return; }
  const local = state.onPhone.has(id) ? await Offline.artURL(id) : null;
  if (local) {
    img.src = local;
    // Release on failure too — an errored image never fires load, and the URL
    // would pin its blob for the life of the document.
    const free = () => URL.revokeObjectURL(local);
    img.addEventListener('load', free, { once: true });
    img.addEventListener('error', free, { once: true });
  } else {
    img.src = `/api/art/${id}`;
  }
}

async function paintArt(root) {
  for (const img of root.querySelectorAll('img[data-art]')) {
    const id = img.dataset.art;
    delete img.dataset.art;
    setArt(img, id);
  }
}

const EMPTY = {
  search: ['搜点什么', '上面输入歌名、歌手或专辑，或点一个场景'],
  lyrics: ['没找到', '换一句试试，或者确认一下歌词记对了没有'],
  recommend: ['还没法推荐', '先搜几首、听几首、收藏几首——推荐是照着你的口味长出来的；或者点上面的新歌分类'],
  discover: ['这个分类暂时没歌', '换一个分类试试'],
  all: ['音乐库还是空的', '播放过的歌会自动存下来，之后秒开'],
  liked: ['还没有收藏', '点播放器上的收藏键收藏当前这首'],
  phone: ['这台设备上还没有歌', '点任意一首右边的下载键存到本机，之后电脑关机、飞行模式都能听'],
  playlists: ['还没有清单', '上面粘一个 YouTube / B站 合集链接，导入成清单'],
  emptyPlaylist: ['这个清单是空的', '可能里面的内容被地域限制了'],
  offlineSearch: ['离线中', '连不上服务器。切到「音乐库 › 已下载」照常能听'],
};

/* ---------- preferences ---------- */

function loadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem('tunebox_prefs') || 'null');
    if (p) state.prefs = { langs: p.langs || [], genres: p.genres || [], dislikes: p.dislikes || [] };
  } catch {}
}
function savePrefs() { localStorage.setItem('tunebox_prefs', JSON.stringify(state.prefs)); }
function prefsSet() { return localStorage.getItem('tunebox_prefs') !== null; }

// Dislikes → a hard title/artist keyword filter over all discovery + auto-radio.
function _dislikeKeys() {
  const on = new Set(state.prefs.dislikes);
  return DISLIKES.filter((d) => on.has(d.label)).flatMap((d) => d.keys);
}
function passesPrefs(t) {
  const keys = _dislikeKeys();
  if (!keys.length) return true;
  const hay = `${t.title || ''} ${t.artist || ''}`.toLowerCase();
  return !keys.some((k) => hay.includes(k));
}
function applyPrefs(list) { return (list || []).filter(passesPrefs); }

// Soft query hint: one language (only if a single one is chosen) + first genre.
function prefHint() {
  const parts = [];
  if (state.prefs.langs.length === 1) parts.push(state.prefs.langs[0]);
  if (state.prefs.genres.length) parts.push(state.prefs.genres[0]);
  return parts.join(' ');
}

// Rough language of a track from its script. CJK can't split 华语/粤语, so both
// map to '华语' — good enough to bias recommendations by language preference.
function trackLang(t) {
  const s = `${t.title || ''}${t.artist || ''}`;
  if (/[가-힯]/.test(s)) return '韩语';   // hangul
  if (/[぀-ヿ]/.test(s)) return '日语';   // kana
  if (/[一-鿿]/.test(s)) return '华语';   // CJK (华语/粤语)
  return '欧美';                                   // latin/other
}
function langAllowed() {
  const p = state.prefs.langs;
  if (!p.length) return null; // no language preference = no filter
  const allow = new Set();
  if (p.includes('华语') || p.includes('粤语')) allow.add('华语');
  if (p.includes('欧美')) allow.add('欧美');
  if (p.includes('日语')) allow.add('日语');
  if (p.includes('韩语')) allow.add('韩语');
  return allow;
}
// Bias a list toward preferred languages, but only if that leaves enough — never
// empty the view because of a language filter.
function biasByLang(list, floor = 6) {
  const allow = langAllowed();
  if (!allow) return list;
  const kept = list.filter((t) => allow.has(trackLang(t)));
  return kept.length >= floor ? kept : list;
}

/* ---------- settings overlay ---------- */

function renderSettingsChips() {
  const chip = (v, on, g) =>
    `<button data-pref="${g}" data-val="${esc(v)}" class="${on ? 'on' : ''}">${esc(v)}</button>`;
  $('#prefLang').innerHTML = PREF_LANGS.map((l) => chip(l, state.prefs.langs.includes(l), 'langs')).join('');
  $('#prefGenre').innerHTML = PREF_GENRES.map((g) => chip(g, state.prefs.genres.includes(g), 'genres')).join('');
  $('#prefDislike').innerHTML = DISLIKES.map((d) => chip(d.label, state.prefs.dislikes.includes(d.label), 'dislikes')).join('');
}

function openSettings(firstRun = false) {
  renderSettingsChips();
  $('#settingsTitle').textContent = firstRun ? '先设置一下口味（随时可改）' : '设置';
  // Update control only means something in the APK (the native bridge). In the
  // PWA there's nothing to self-update — a refresh already gets the latest.
  const inApp = !!(window.tunebox && window.tunebox.checkUpdate);
  const btn = $('#checkUpdateBtn');
  if (inApp) {
    $('#updateHint').textContent = '会每天自动检查，也可手动';
    btn.disabled = false;
    btn.textContent = '检查更新';
  } else {
    $('#updateHint').textContent = '网页版刷新即最新（自更新只在 App 里）';
    btn.disabled = true;
  }
  $('#settings').hidden = false;
}

function closeSettings() {
  $('#settings').hidden = true;
  savePrefs();
  // Re-run whatever's on screen so the new prefs take effect immediately.
  if (state.tab === 'recommend') runDiscover(state.discover);
  else if (state.tab === 'library') loadLibrary();
  else if (state.scene) { const s = SCENES.find((x) => x.label === state.scene); if (s) runScene(s); }
}

function render() {
  const list = $('#list');
  // The playlists LIST (cards) is its own view, not a track list.
  const inPlaylists = state.tab === 'library' && state.libFilter === 'playlists';
  if (inPlaylists && !state.openPlaylist && !state.loading) { renderPlaylists(); return; }

  if (state.loading) { list.innerHTML = '<div class="spinner"></div>'; return; }
  if (state.error) {
    list.innerHTML = `<div class="empty"><b class="err">出错了</b>${esc(state.error)}</div>`;
    return;
  }
  // A back chip sits above the tracks when a playlist is open.
  const back = inPlaylists && state.openPlaylist
    ? `<button class="plback" id="plback">‹ ${esc(state.openPlaylist.name)}</button>` : '';
  const items = state.lists[state.tab];
  if (!items.length) {
    let key = state.tab;
    if (state.tab === 'search') {
      if (!state.online) key = 'offlineSearch';
      else if (state.mode === 'lyrics' && $('#q').value.trim()) key = 'lyrics';
    } else if (state.tab === 'library') {
      key = state.libFilter; // all | liked | phone
    } else if (state.tab === 'recommend' && state.discover !== 0) {
      key = 'discover'; // a curated category, not personalized
    } else if (inPlaylists && state.openPlaylist) {
      key = 'emptyPlaylist';
    }
    const [a, b] = EMPTY[key];
    list.innerHTML = back + `<div class="empty"><b>${a}</b>${b}</div>`;
    return;
  }
  list.innerHTML = back + items.map(rowHTML).join('');
  paintArt(list);
}

function renderNP() {
  const t = state.current;
  $('#npTitle').textContent = t ? t.title : '还没在放什么';
  $('#npArtist').textContent = t ? t.artist || '' : '搜一首歌开始';
  setArt($('#cover'), t?.id);
  $('#likeBtn').classList.toggle('on', !!t?.liked);
  $('#shuffleBtn').classList.toggle('on', state.shuffle);
  $('#repeatBtn').classList.toggle('on', state.repeat !== 'off');
  $('#repeatBtn').textContent = state.repeat === 'one' ? '↻¹' : '↻';
}

async function refreshStats() {
  const u = await Offline.usage();
  const phone = u.tracks ? `本机 ${u.tracks} 首 · ${(u.bytes / 1e6).toFixed(0)} MB` : '';
  if (!state.online) { $('#stats').textContent = phone ? `离线 · ${phone}` : '离线'; return; }
  try {
    const s = await api('/api/stats');
    const server = `服务器 ${s.tracks} 首`;
    $('#stats').textContent = phone ? `${phone} · ${server}` : server;
  } catch {
    $('#stats').textContent = phone;
  }
  // "全部下载" only on the server-backed 全部 view, and only if something isn't yet local.
  $('#saveAll').hidden = !(state.tab === 'library' && state.libFilter === 'all'
    && state.lists.library.some((t) => !state.onPhone.has(t.id)));
}

/* ---------- data ---------- */

// Shares arrive as "【标题-哔哩哔哩】 https://b23.tv/xxx" — pull the URL out of
// whatever text was pasted, then decide if it's an importable collection link.
function importableUrl(q) {
  const m = String(q).match(/https?:\/\/[^\s]+/i);
  const u = m ? m[0].replace(/[)\]，。、]+$/, '') : '';
  return u && /(youtube\.com|youtu\.be|bilibili\.com|b23\.tv)/i.test(u) ? u : null;
}

async function doSearch(q) {
  const url = importableUrl(q);
  if (url) return importUrl(url);
  state.tab = 'search';
  state.scene = null; // a typed search clears any active scene
  state.loading = true; state.error = null;
  syncTabs(); render();
  const path = state.mode === 'lyrics' ? '/api/search/lyrics' : '/api/search';
  try {
    state.lists.search = applyPrefs(await api(`${path}?q=${encodeURIComponent(q)}`));
  } catch (e) {
    state.error = e.message;
  }
  state.loading = false;
  render();
}

async function runScene(s) {
  state.tab = 'search';
  state.mode = 'song';
  state.scene = s.label;
  $('#q').value = '';
  $('#q').placeholder = `${s.label} — 换个心情？`;
  document.querySelectorAll('#mode button').forEach((b) =>
    b.classList.toggle('on', b.dataset.mode === 'song'));
  renderScenes();
  state.loading = true; state.error = null;
  syncTabs(); render();
  const q = `${s.q} ${prefHint()}`.trim(); // bias the scene by language/genre prefs
  try {
    state.lists.search = applyPrefs(await api(`/api/search?q=${encodeURIComponent(q)}&limit=40`));
  } catch (e) {
    state.error = e.message;
  }
  state.loading = false;
  render();
}

function renderScenes() {
  const box = $('#scenes');
  // Only on the search tab, song mode — a discovery strip, not a permanent fixture.
  const show = state.tab === 'search' && state.mode === 'song';
  box.hidden = !show;
  if (!show) return;
  box.innerHTML = SCENES.map((s, i) =>
    `<button class="scene${state.scene === s.label ? ' on' : ''}" data-scene="${i}">${esc(s.label)}</button>`).join('');
}

async function loadList(tab) {
  if (tab === 'library') return loadLibrary();
  if (tab === 'recommend') return runDiscover(state.discover);
}

async function runDiscover(i) {
  state.discover = i;
  state.tab = 'recommend';
  const d = DISCOVER[i];
  renderDiscover();
  state.loading = true; state.error = null;
  render();
  try {
    if (d.kind === 'recommend') {
      // 为你推荐 honors prefs: drop disliked, then bias toward preferred language.
      state.lists.recommend = biasByLang(applyPrefs(await api('/api/recommend')));
    } else {
      const q = d.q.replace('{YR}', new Date().getFullYear());
      state.lists.recommend = applyPrefs(await api(`/api/search?q=${encodeURIComponent(q)}&limit=40`));
    }
  } catch (e) {
    state.error = e.message;
  }
  state.loading = false;
  render();
}

function renderDiscover() {
  const box = $('#discover');
  box.hidden = state.tab !== 'recommend';
  if (box.hidden) return;
  // Float the user's preferred-language categories up (为你推荐 stays first).
  const pref = new Set(state.prefs.langs);
  const order = DISCOVER.map((_, i) => i).sort((a, b) => {
    if (a === 0 || b === 0) return a - b;
    return (pref.has(DISCOVER_LANG[DISCOVER[a].label]) ? 0 : 1)
         - (pref.has(DISCOVER_LANG[DISCOVER[b].label]) ? 0 : 1);
  });
  box.innerHTML = order.map((i) =>
    `<button class="scene${state.discover === i ? ' on' : ''}" data-disc="${i}">${esc(DISCOVER[i].label)}</button>`).join('');
}

async function loadLibrary() {
  // 已下载 is device-local (IndexedDB, works offline); 全部/收藏 come from the server.
  if (state.libFilter === 'phone') {
    state.lists.library = await Offline.list();
    render();
    refreshStats();
    return;
  }
  if (state.libFilter === 'playlists') return loadPlaylists();
  state.loading = true; state.error = null;
  render();
  try {
    const q = state.libFilter === 'liked' ? '?liked=true' : '';
    state.lists.library = await api(`/api/library${q}`);
  } catch (e) {
    state.error = e.message;
  }
  state.loading = false;
  render();
  refreshStats(); // updates the 全部下载 button for the new filter
}

/* ---------- imported playlists (清单) ---------- */

async function loadPlaylists() {
  if (state.openPlaylist) {
    // Viewing one playlist's tracks.
    state.loading = true; state.error = null; render();
    try {
      state.lists.library = await api(`/api/playlist/${state.openPlaylist.id}`);
    } catch (e) { state.error = e.message; }
    state.loading = false; render();
    return;
  }
  // The list of playlists.
  try { state.playlists = await api('/api/playlists'); }
  catch (e) { state.playlists = []; }
  render();
}

async function importUrl(url) {
  const btn = $('#importBtn');
  if (btn) { btn.disabled = true; btn.textContent = '导入中…'; }
  $('#list').innerHTML = '<div class="empty"><b>导入中…</b>正在读取合集清单，稍等</div>';
  try {
    const r = await api('/api/import', { method: 'POST', body: JSON.stringify({ url }) });
    $('#importUrl').value = '';
    $('#q').value = '';
    // Land inside the freshly imported playlist, wherever the import was triggered from.
    state.tab = 'library';
    state.libFilter = 'playlists';
    state.openPlaylist = { id: r.id, name: r.name };
    syncTabs();
    document.querySelectorAll('#subfilters button[data-filter]').forEach((x) =>
      x.classList.toggle('on', x.dataset.filter === 'playlists'));
    syncChrome();
    await loadPlaylists();
  } catch (e) {
    alert(`导入失败：${e.message}`);
    render();
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '导入'; }
  }
}

async function deletePlaylist(id, name) {
  if (!confirm(`删除清单「${name}」？(不影响已下载到本机的歌)`)) return;
  await api(`/api/playlist/${id}`, { method: 'DELETE' });
  loadPlaylists();
}

function openPlaylist(pl) {
  state.openPlaylist = { id: pl.id, name: pl.name };
  syncChrome(); // hide the import bar while inside a playlist
  loadPlaylists();
}

function closePlaylist() {
  state.openPlaylist = null;
  syncChrome();
  loadPlaylists();
}

function renderPlaylists() {
  const list = $('#list');
  if (!state.playlists.length) {
    const [a, b] = EMPTY.playlists;
    list.innerHTML = `<div class="empty"><b>${a}</b>${b}</div>`;
    return;
  }
  list.innerHTML = state.playlists.map((p) => `
    <div class="plcard" data-open="${p.id}">
      <div class="plcard-name">${esc(p.name)}</div>
      <span class="plcard-meta">${p.count} 首 · <span class="plcard-src">${p.source === 'bili' ? 'B站' : 'YT'}</span></span>
      <button class="row-act" data-delpl="${p.id}" data-name="${esc(p.name)}" title="删除清单">✕</button>
    </div>`).join('');
}

/* ---------- add a song to a playlist ---------- */

let _pickTrack = null;

async function openPicker(track) {
  _pickTrack = track;
  let pls = [];
  try { pls = await api('/api/playlists'); } catch {}
  const mine = pls.filter((p) => p.source === 'user' || !p.source);
  $('#pickerList').innerHTML = mine.length
    ? mine.map((p) => `<button data-pl="${p.id}">${esc(p.name)}<span class="cnt">${p.count} 首</span></button>`).join('')
    : '<div class="empty-hint">还没有自建歌单，下面新建一个</div>';
  $('#pickerName').value = '';
  $('#picker').hidden = false;
}

function closePicker() { $('#picker').hidden = true; _pickTrack = null; }

async function addToPlaylist(pid) {
  if (!_pickTrack) return;
  try {
    await api(`/api/playlist/${pid}/add`, { method: 'POST', body: JSON.stringify({ track: _pickTrack }) });
  } catch (e) { alert('加入失败：' + e.message); return; }
  closePicker();
}

async function createAndAdd(name) {
  try {
    const p = await api('/api/playlist', { method: 'POST', body: JSON.stringify({ name }) });
    await api(`/api/playlist/${p.id}/add`, { method: 'POST', body: JSON.stringify({ track: _pickTrack }) });
  } catch (e) { alert('新建失败：' + e.message); return; }
  closePicker();
}

async function removeFromPlaylist(trackId) {
  if (!state.openPlaylist) return;
  await api(`/api/playlist/${state.openPlaylist.id}/track/${trackId}`, { method: 'DELETE' });
  loadPlaylists();
}

$('#pickerClose').addEventListener('click', closePicker);
$('#picker').addEventListener('click', (e) => {
  if (e.target === $('#picker')) { closePicker(); return; }
  const b = e.target.closest('[data-pl]');
  if (b) addToPlaylist(+b.dataset.pl);
});
$('#pickerNew').addEventListener('submit', (e) => {
  e.preventDefault();
  const name = $('#pickerName').value.trim();
  if (name) createAndAdd(name);
});

/* ---------- on-device downloads ---------- */

async function saveToPhone(t) {
  if (state.dl.has(t.id) || state.onPhone.has(t.id)) return;
  state.dl.set(t.id, 0);
  render();
  try {
    await Offline.save(t, (p) => {
      state.dl.set(t.id, p);
      const b = document.querySelector(`.row-dl.busy`);
      if (b) b.textContent = Math.round(p * 100);
    });
    // Ask on the first save, not at boot: at boot there was nothing to lose yet,
    // and the browser weighs the request against how much you use the app.
    if (!state.onPhone.size) Offline.persist();
    state.onPhone.add(t.id);
  } catch (e) {
    alert(`「${t.title}」下载失败：${e.message}`);
  } finally {
    state.dl.delete(t.id);
  }
  render();
  refreshStats();
}

async function removeFromPhone(id) {
  await Offline.remove(id);
  state.onPhone.delete(id);
  if (state.tab === 'library' && state.libFilter === 'phone') {
    state.lists.library = await Offline.list();
  }
  render();
  refreshStats();
}

async function saveAll() {
  const items = state.lists[state.tab].filter((t) => !state.onPhone.has(t.id));
  if (!items.length) return;
  const mb = items.reduce((s, t) => s + (t.size || t.duration * 16000), 0) / 1e6;
  if (!confirm(`下载 ${items.length} 首到本机？大约 ${mb.toFixed(0)} MB。`)) return;
  for (const t of items) await saveToPhone(t); // serial: don't saturate a phone's uplink
}

/* ---------- server reachability ---------- */

async function ping() {
  try {
    const r = await fetch('/api/stats', { cache: 'no-store', signal: AbortSignal.timeout(4000) });
    return r.ok;
  } catch { return false; }
}

async function checkOnline() {
  const was = state.online;
  state.online = navigator.onLine ? await ping() : false;
  if (was === state.online) return;
  document.body.classList.toggle('offline', !state.online);
  // Nothing else is reachable offline, so land on the device downloads.
  if (!state.online && !(state.tab === 'library' && state.libFilter === 'phone')) {
    state.tab = 'library';
    state.libFilter = 'phone';
    syncTabs();
    syncChrome();
    loadLibrary();
  }
  render();
  refreshStats();
}

/* ---------- playback ---------- */

function playFrom(list, i) {
  state.queue = list.slice();
  state.qi = i;
  loadTrack(state.queue[i]);
}

let srcURL = null; // live blob URL, if any — must be revoked or the blob leaks

async function loadTrack(t) {
  state.current = t;
  renderNP();
  render();
  updateMediaSession(t);

  if (state.online) {
    // Hand the server the good search metadata before it starts downloading,
    // so the library shows a real artist instead of a channel name.
    api('/api/track', { method: 'POST', body: JSON.stringify(t) }).catch(() => {});
  }
  loadLyrics(t.id);
  if (state.stage) paintStage(t);

  if (srcURL) { URL.revokeObjectURL(srcURL); srcURL = null; }
  const local = state.onPhone.has(t.id) ? await Offline.audioURL(t.id) : null;
  if (state.current?.id !== t.id) { // beaten by a newer click
    if (local) URL.revokeObjectURL(local);
    return;
  }
  srcURL = local;
  audio.src = local || `/api/stream/${t.id}`;

  try {
    await audio.play();
  } catch (e) {
    if (e.name !== 'AbortError') console.warn('play blocked:', e.message);
  }
  if (!local && state.online) pollProgress(t.id);
  else $('#dlState').textContent = local ? '本机' : '';
}

function paintStage(t) {
  $('#stageTitle').textContent = t.title;
  $('#stageArtist').textContent = t.artist || '';
  setArt($('#stageCover'), t.id);
}

function pickNext() {
  if (state.shuffle && state.queue.length > 1) {
    let i;
    do { i = Math.floor(Math.random() * state.queue.length); } while (i === state.qi);
    return i;
  }
  return state.qi + 1;
}

async function next(auto = false) {
  if (!state.queue.length) return;
  if (auto && state.repeat === 'one') {
    audio.currentTime = 0;
    audio.play();
    return;
  }
  const i = pickNext();
  if (i < state.queue.length) {
    state.qi = i;
    loadTrack(state.queue[i]);
    return;
  }
  // Ran off the end of the queue.
  if (state.repeat === 'all') { state.qi = 0; loadTrack(state.queue[0]); return; }
  if (auto && state.radio && state.current) {
    try {
      const more = applyPrefs(await api(`/api/related/${state.current.id}`));
      const have = new Set(state.queue.map((t) => t.id));
      const fresh = more.filter((t) => !have.has(t.id));
      if (fresh.length) {
        state.queue.push(...fresh);
        state.qi += 1;
        loadTrack(state.queue[state.qi]);
        return;
      }
    } catch (e) {
      console.warn('radio failed:', e.message);
    }
  }
  audio.pause();
}

function prev() {
  if (audio.currentTime > 3) { audio.currentTime = 0; return; }
  if (state.qi > 0) { state.qi -= 1; loadTrack(state.queue[state.qi]); }
  else audio.currentTime = 0;
}

/* ---------- lyrics ---------- */

async function loadLyrics(vid, force = false) {
  state.lyrics = null;
  state.lyricIdx = -1;
  $('#lyricBtn').classList.remove('on');
  if (state.stage) renderLyrics(true);

  if (!force) {
    // Lyrics ride along with the download, so they work on a plane too.
    const saved = await Offline.lyrics(vid);
    if (saved && state.current?.id === vid) {
      state.lyrics = saved;
      $('#lyricBtn').classList.toggle('on', !!(saved.synced.length || saved.plain));
      if (state.stage) renderLyrics();
      return;
    }
  }
  try {
    const ly = await api(`/api/lyrics/${vid}${force ? '?force=true' : ''}`);
    if (state.current?.id !== vid) return; // track moved on while we waited
    state.lyrics = ly;
  } catch {
    state.lyrics = { synced: [], plain: '', source: 'none' };
  }
  $('#lyricBtn').classList.toggle('on', !!(state.lyrics.synced.length || state.lyrics.plain));
  if (state.stage) renderLyrics();
}

const SOURCE_NAME = { lrclib: 'LRCLIB', ytmusic: 'YouTube Music' };

function renderLyrics(loading = false) {
  const box = $('#lyrics');
  const ly = state.lyrics;

  if (loading || !ly) {
    box.className = 'stage-lyrics';
    box.innerHTML = '<div class="spinner"></div>';
    return;
  }
  $('#stageSrc').textContent = SOURCE_NAME[ly.source] ? `歌词来自 ${SOURCE_NAME[ly.source]}` : '';

  if (ly.synced.length) {
    box.className = 'stage-lyrics';
    box.innerHTML = ly.synced.map((l) => `<p data-t="${l.t}">${esc(l.text) || '♪'}</p>`).join('');
    state.lyricIdx = -1;
    syncLyrics(true);
    return;
  }
  if (ly.plain) {
    box.className = 'stage-lyrics plain';
    box.innerHTML = ly.plain.split('\n').map((l) => `<p>${esc(l) || '&nbsp;'}</p>`).join('');
    return;
  }
  box.className = 'stage-lyrics';
  box.innerHTML = `<div class="stage-empty"><b>没有歌词</b>LRCLIB 和 YouTube Music 都没有这一首
    <button id="reLyric">重新找一次</button></div>`;
}

// ~200ms of lead. LRC stamps mark when a line starts being sung; lighting it up
// at exactly that moment leaves no time to read it first.
const LEAD_MS = 200;

function syncLyrics(jump = false) {
  const ly = state.lyrics;
  if (!ly || !ly.synced.length) return;
  const ms = audio.currentTime * 1000 + LEAD_MS;

  let i = -1;
  for (let k = 0; k < ly.synced.length; k++) {
    if (ly.synced[k].t <= ms) i = k; else break;
  }
  if (i === state.lyricIdx && !jump) return;
  state.lyricIdx = i;

  const box = $('#lyrics');
  const kids = box.children;
  for (let k = 0; k < kids.length; k++) {
    kids[k].classList.toggle('on', k === i);
    kids[k].classList.toggle('done', k < i);
  }
  if (i < 0) return;
  // Don't yank the view back while they're reading somewhere else.
  if (!jump && Date.now() < state.lyricHold) return;
  const el = kids[i];
  if (!el) return;
  box.scrollTo({
    top: el.offsetTop - box.clientHeight / 2 + el.offsetHeight / 2,
    behavior: jump ? 'auto' : 'smooth',
  });
}

let rafId = null;
function lyricLoop() {
  if (!audio.paused) syncLyrics();
  rafId = requestAnimationFrame(lyricLoop);
}
// rAF is the smooth driver but it is not guaranteed: the browser stops it dead
// whenever the page isn't being composited (measured — 0 ticks/sec on a hidden
// page), and some embedded webviews never run it at all. `timeupdate` is a media
// event, so it keeps firing regardless; it's coarse at ~4Hz but it means the
// lyrics can never simply freeze. See the timeupdate handler below.

function openStage() {
  if (!state.current) return;
  state.stage = true;
  $('#stage').hidden = false;
  paintStage(state.current);
  renderLyrics(!state.lyrics);
  // timeupdate only fires ~4x/sec — following along with that is visibly jerky.
  // rAF costs nothing here since it only runs while the stage is open.
  if (!rafId) lyricLoop();
}

function closeStage() {
  state.stage = false;
  $('#stage').hidden = true;
  if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
}

$('#lyricBtn').addEventListener('click', () => (state.stage ? closeStage() : openStage()));
$('#stageClose').addEventListener('click', closeStage);
$('#cover').addEventListener('click', () => (state.stage ? closeStage() : openStage()));

$('#lyrics').addEventListener('click', (e) => {
  if (e.target.id === 'reLyric') { loadLyrics(state.current.id, true); return; }
  const p = e.target.closest('p[data-t]');
  if (!p) return;
  audio.currentTime = +p.dataset.t / 1000;
  state.lyricHold = 0;
  syncLyrics(true);
  if (audio.paused) audio.play();
});

// Any manual scroll parks the autoscroll for a few seconds.
for (const ev of ['wheel', 'touchmove']) {
  $('#lyrics').addEventListener(ev, () => { state.lyricHold = Date.now() + 4000; }, { passive: true });
}

// Coming back from a locked screen, the highlight is correct (timeupdate kept
// running) but the scroll is wherever the song was when you left: smooth
// scrolling is animation-driven and does nothing at all while hidden (measured).
// Snap to the right place instead of waiting for the next line to land.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && state.stage) syncLyrics(true);
});

/* ---------- MediaSession: this is what makes background play work ----------
   Without it, a phone browser suspends audio the moment you leave the tab or
   lock the screen. With it, the OS treats us as a real media app: lock-screen
   art, headset buttons, and Bluetooth prev/next all route here.            */

function updateMediaSession(t) {
  if (!('mediaSession' in navigator)) return;
  navigator.mediaSession.metadata = new MediaMetadata({
    title: t.title || '',
    artist: t.artist || '',
    album: t.album || '',
    artwork: [96, 192, 256, 512].map((s) => ({
      src: `/api/art/${t.id}`,
      sizes: `${s}x${s}`,
      type: 'image/jpeg',
    })),
  });
}

function syncPosition() {
  if (!('mediaSession' in navigator) || !navigator.mediaSession.setPositionState) return;
  if (!isFinite(audio.duration) || audio.duration <= 0) return;
  try {
    navigator.mediaSession.setPositionState({
      duration: audio.duration,
      playbackRate: audio.playbackRate,
      position: Math.min(audio.currentTime, audio.duration),
    });
  } catch {}
}

function initMediaSession() {
  if (!('mediaSession' in navigator)) return;
  const handlers = {
    play: () => audio.play(),
    pause: () => audio.pause(),
    stop: () => { audio.pause(); audio.currentTime = 0; },
    previoustrack: () => prev(),
    nexttrack: () => next(),
    seekbackward: (d) => { audio.currentTime = Math.max(0, audio.currentTime - (d.seekOffset || 10)); },
    seekforward: (d) => { audio.currentTime = Math.min(audio.duration, audio.currentTime + (d.seekOffset || 10)); },
    seekto: (d) => {
      if (d.fastSeek && audio.fastSeek) audio.fastSeek(d.seekTime);
      else audio.currentTime = d.seekTime;
      syncPosition();
    },
  };
  for (const [k, fn] of Object.entries(handlers)) {
    try { navigator.mediaSession.setActionHandler(k, fn); } catch {}
  }
}

/* ---------- native (APK) media session bridge ----------
   The WebView's own MediaSession doesn't surface lock-screen/notification
   controls, so in the APK we push state to Android (window.tunebox.updateMedia)
   and Android runs these globals when its buttons are pressed. No-ops in the PWA. */

window.__tuneboxNext = () => next();
window.__tuneboxPrev = () => prev();
window.__tuneboxSeek = (ms) => { if (isFinite(audio.duration)) audio.currentTime = ms / 1000; };

let _lastPush = 0;
function pushMedia(force) {
  const b = window.tunebox;
  if (!b || !b.updateMedia) return; // PWA: nothing native to drive
  const now = Date.now();
  if (!force && now - _lastPush < 1500) return; // throttle position updates
  _lastPush = now;
  const t = state.current;
  try {
    b.updateMedia(JSON.stringify({
      title: t ? t.title : '',
      artist: t ? (t.artist || '') : '',
      art: t ? `${location.origin}/api/art/${t.id}` : '',
      playing: !audio.paused,
      position: Math.round((audio.currentTime || 0) * 1000),
      duration: Math.round((audio.duration || 0) * 1000),
    }));
  } catch {}
}

audio.addEventListener('play', () => pushMedia(true));
audio.addEventListener('pause', () => pushMedia(true));
audio.addEventListener('loadedmetadata', () => pushMedia(true));
audio.addEventListener('timeupdate', () => pushMedia(false));

/* ---------- cache progress ---------- */

let pollTimer = null;
function pollProgress(vid) {
  clearTimeout(pollTimer);
  const tick = async () => {
    if (state.current?.id !== vid) { $('#dlState').textContent = ''; return; }
    try {
      const p = await api(`/api/progress/${vid}`);
      if (p.state === 'downloading') {
        $('#dlState').textContent = `缓存中 ${p.pct}%`;
        pollTimer = setTimeout(tick, 1200);
      } else {
        $('#dlState').textContent = p.state === 'cached' ? '已离线' : '';
        if (p.state === 'cached') {
          if (state.current) state.current.cached = true;
          setTimeout(() => { if (state.current?.id === vid) $('#dlState').textContent = ''; }, 2500);
          refreshStats();
        }
      }
    } catch {}
  };
  tick();
}

/* ---------- wiring ---------- */

function syncTabs() {
  document.querySelectorAll('.tabs button').forEach((b) =>
    b.classList.toggle('on', b.dataset.tab === state.tab));
}

$('#searchForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const q = $('#q').value.trim();
  if (q) doSearch(q);
});

const PLACEHOLDER = {
  song: '搜歌手/歌名，或粘 YouTube·B站 合集链接导入…',
  lyrics: '输入一句歌词，找出是哪首歌…',
};

document.querySelectorAll('#mode button').forEach((b) =>
  b.addEventListener('click', () => {
    state.mode = b.dataset.mode;
    document.querySelectorAll('#mode button').forEach((x) => x.classList.toggle('on', x === b));
    $('#q').placeholder = PLACEHOLDER[state.mode];
    renderScenes(); // scenes only make sense in song mode
    save();
    const q = $('#q').value.trim();
    if (q) doSearch(q);
    else $('#q').focus();
  }));

document.querySelectorAll('.tabs button[data-tab]').forEach((b) =>
  b.addEventListener('click', () => {
    state.tab = b.dataset.tab;
    syncTabs();
    syncChrome();
    if (state.tab === 'search') render();
    else loadList(state.tab);
  }));

// Sub-filters of the 音乐库 tab: 全部 / 收藏 / 已下载.
document.querySelectorAll('#subfilters button[data-filter]').forEach((b) =>
  b.addEventListener('click', () => {
    state.libFilter = b.dataset.filter;
    state.openPlaylist = null; // leaving/entering a filter drops any open playlist
    document.querySelectorAll('#subfilters button[data-filter]').forEach((x) =>
      x.classList.toggle('on', x === b));
    syncChrome();
    loadLibrary();
  }));

// Show the scene strip on search/song, the discover strip on 发现,
// the sub-filters on 音乐库, the import bar on 音乐库›歌单 (list view).
function syncChrome() {
  renderScenes();
  renderDiscover();
  $('#subfilters').hidden = state.tab !== 'library';
  $('#importbar').hidden = !(state.tab === 'library' && state.libFilter === 'playlists' && !state.openPlaylist);
}

$('#importbar').addEventListener('submit', (e) => {
  e.preventDefault();
  const url = $('#importUrl').value.trim();
  if (url) importUrl(url);
});

$('#scenes').addEventListener('click', (e) => {
  const chip = e.target.closest('[data-scene]');
  if (chip) runScene(SCENES[+chip.dataset.scene]);
});

$('#settingsBtn').addEventListener('click', () => openSettings());
$('#settingsClose').addEventListener('click', closeSettings);
$('#checkUpdateBtn').addEventListener('click', () => {
  if (window.tunebox && window.tunebox.checkUpdate) window.tunebox.checkUpdate();
});
$('#settings').addEventListener('click', (e) => {
  if (e.target === $('#settings')) { closeSettings(); return; } // click backdrop = done
  const b = e.target.closest('[data-pref]');
  if (!b) return;
  const arr = state.prefs[b.dataset.pref];
  const i = arr.indexOf(b.dataset.val);
  if (i >= 0) arr.splice(i, 1); else arr.push(b.dataset.val);
  b.classList.toggle('on');
  savePrefs();
});

$('#discover').addEventListener('click', (e) => {
  const chip = e.target.closest('[data-disc]');
  if (chip) runDiscover(+chip.dataset.disc);
});

$('#list').addEventListener('click', async (e) => {
  if (e.target.closest('#plback')) { closePlaylist(); return; }
  const delpl = e.target.closest('[data-delpl]');
  if (delpl) { e.stopPropagation(); deletePlaylist(+delpl.dataset.delpl, delpl.dataset.name); return; }
  const openCard = e.target.closest('[data-open]');
  if (openCard) { openPlaylist(state.playlists.find((p) => p.id === +openCard.dataset.open)); return; }

  const save = e.target.closest('[data-save]');
  if (save) {
    e.stopPropagation();
    const row = save.closest('.row');
    saveToPhone(state.lists[state.tab][+row.dataset.i]);
    return;
  }
  const unsave = e.target.closest('[data-unsave]');
  if (unsave) { e.stopPropagation(); removeFromPhone(unsave.dataset.unsave); return; }

  const add = e.target.closest('[data-add]');
  if (add) {
    e.stopPropagation();
    openPicker(state.lists[state.tab][+add.closest('.row').dataset.i]);
    return;
  }
  const rmpl = e.target.closest('[data-rmpl]');
  if (rmpl) { e.stopPropagation(); removeFromPlaylist(rmpl.dataset.rmpl); return; }

  const del = e.target.closest('[data-del]');
  if (del) {
    e.stopPropagation();
    await api(`/api/library/${del.dataset.del}`, { method: 'DELETE' });
    loadList(state.tab);
    refreshStats();
    return;
  }
  const row = e.target.closest('.row');
  if (row) playFrom(state.lists[state.tab], +row.dataset.i);
});

$('#saveAll').addEventListener('click', saveAll);
window.addEventListener('online', checkOnline);
window.addEventListener('offline', checkOnline);

$('#playBtn').addEventListener('click', () => (audio.paused ? audio.play() : audio.pause()));
$('#nextBtn').addEventListener('click', () => next());
$('#prevBtn').addEventListener('click', prev);

$('#shuffleBtn').addEventListener('click', () => {
  state.shuffle = !state.shuffle;
  save(); renderNP();
});

$('#repeatBtn').addEventListener('click', () => {
  state.repeat = { off: 'all', all: 'one', one: 'off' }[state.repeat];
  save(); renderNP();
});

$('#likeBtn').addEventListener('click', async () => {
  if (!state.current) return;
  const liked = !state.current.liked;
  state.current.liked = liked;
  renderNP();
  await api(`/api/like/${state.current.id}?liked=${liked}`, { method: 'POST' });
  if (state.tab === 'library' && state.libFilter === 'liked') loadLibrary();
});

$('#radio').addEventListener('change', (e) => { state.radio = e.target.checked; save(); });

$('#vol').addEventListener('input', (e) => { audio.volume = e.target.value / 100; save(); });

$('#seek').addEventListener('input', () => { state.seeking = true; });
$('#seek').addEventListener('change', (e) => {
  if (isFinite(audio.duration)) audio.currentTime = (e.target.value / 1000) * audio.duration;
  state.seeking = false;
  syncPosition();
});

audio.addEventListener('play', () => {
  $('#playBtn').textContent = '⏸';
  if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
});
audio.addEventListener('pause', () => {
  $('#playBtn').textContent = '▶';
  if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
});
audio.addEventListener('ended', () => next(true));
audio.addEventListener('loadedmetadata', () => { $('#dur').textContent = fmt(audio.duration); syncPosition(); });
audio.addEventListener('error', () => {
  if (audio.src) $('#dlState').textContent = '播放失败';
});

audio.addEventListener('timeupdate', () => {
  $('#cur').textContent = fmt(audio.currentTime);
  if (!state.seeking && isFinite(audio.duration) && audio.duration > 0) {
    const pct = (audio.currentTime / audio.duration) * 100;
    $('#seek').value = Math.round(pct * 10);
    $('#player').style.setProperty('--pct', pct + '%');
  }
  syncPosition();
  if (state.stage) syncLyrics(); // rAF's safety net — see lyricLoop
});

audio.addEventListener('progress', () => {
  const b = $('#buffer');
  if (!isFinite(audio.duration) || !audio.buffered.length) { b.style.width = '0'; return; }
  b.style.width = (audio.buffered.end(audio.buffered.length - 1) / audio.duration) * 100 + '%';
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && state.stage) { closeStage(); return; }
  // tagName, not matches(): e.target isn't always an Element, and Document has
  // no matches() — one throw here kills every shortcut for that keypress.
  const tag = e.target?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA') return;
  const k = e.key;
  if (k === 'l') { state.stage ? closeStage() : openStage(); }
  else if (k === ' ') { e.preventDefault(); audio.paused ? audio.play() : audio.pause(); }
  else if (k === 'ArrowRight' && e.shiftKey) next();
  else if (k === 'ArrowLeft' && e.shiftKey) prev();
  else if (k === 'ArrowRight') audio.currentTime += 5;
  else if (k === 'ArrowLeft') audio.currentTime -= 5;
  else if (k === '/') { e.preventDefault(); $('#q').focus(); }
});

/* ---------- prefs ---------- */

function save() {
  localStorage.setItem('tunebox', JSON.stringify({
    vol: audio.volume, shuffle: state.shuffle, repeat: state.repeat,
    radio: state.radio, mode: state.mode,
  }));
}

function load() {
  try {
    const p = JSON.parse(localStorage.getItem('tunebox') || '{}');
    audio.volume = p.vol ?? 1;
    $('#vol').value = Math.round((p.vol ?? 1) * 100);
    state.shuffle = !!p.shuffle;
    state.repeat = p.repeat || 'off';
    state.radio = p.radio !== false;
    state.mode = p.mode === 'lyrics' ? 'lyrics' : 'song';
    $('#radio').checked = state.radio;
    $('#q').placeholder = PLACEHOLDER[state.mode];
    document.querySelectorAll('#mode button').forEach((b) =>
      b.classList.toggle('on', b.dataset.mode === state.mode));
  } catch {}
}

/* ---------- boot ---------- */

(async () => {
  loadPrefs();
  load();
  initMediaSession();
  renderNP();

  syncTabs();   // reflect the default 发现 tab
  syncChrome();

  state.onPhone = await Offline.ids();
  if (state.onPhone.size) Offline.persist(); // only worth asking once there's something to lose

  await checkOnline();
  if (state.online) loadList(state.tab); // 发现 → load discover; checkOnline handles the offline landing
  refreshStats();

  // First run: onboard the taste settings so discovery starts personalized.
  if (!prefsSet()) openSettings(true);

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
})();
