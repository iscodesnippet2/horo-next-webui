// Agent mind map. Reads /api/horo/overview: selecting a card never loads a
// transcript or touches the active profile, so it stays instant however large
// the sessions behind it are. Requests for another profile are scoped by
// X-Horo-Profile; only opening the workspace, upstream's chat view in a modal,
// switches profile.
const MM_STATUS = {
  running: {label: '執行中', color: '#1d4ed8', bg: '#eff6ff'},
  waiting: {label: '等待你', color: '#b45309', bg: '#fffbeb'},
  failed: {label: '失敗', color: '#6d28d9', bg: '#f5f3ff'},
  paused: {label: '已暫停', color: '#475569', bg: '#f1f5f9'},
  // Not running, not waiting: says nothing about whether the work succeeded.
  idle: {label: '', color: '#64748b', bg: '#f8fafc'},
};
const MM_STORE_KEY = 'horo-mindmap-v1';
// Read by upstream's sessions.js (horo_overlay.py) for its first transcript window.
const MM_TAIL_KEY = 'horo-initial-messages';
const MM_TAILS = [10, 20, 30, 50, 100];
const MM_POLL_MS = 15000; // Sessions outside this server emit no event.
const MM_CARD = {w: 480, gap: 14};
// Profile nodes are at most 220px wide: the floating centre and the cards clear them.
const MM_TREE = {center: 52, profile: 222, session: 584, width: 844};
// The centre sits this far down the map in view, a little above the middle, whatever the tree's size.
const MM_ME_AT = 0.42;
// Longest first: the centre's left arrow lengthens the range, the right one shortens it.
const MM_HOURS = [168, 72, 48, 24, 6];
const MM_RADIAL = {center: 940, profile: 240, session: 620, width: 1880};
const MM_CLOSE_ICON = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
const MM_PENCIL_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
// The rail stays usable: every tab in it leaves the map, which closes the workspace.
const MM_BEHIND_MODAL = ['.sidebar', '.app-titlebar', '.rightpanel', '#mainMindmap'];

const mm = {
  data: null, version: '', mounted: false, active: false, source: null, poll: null, debounce: null, inFlight: false,
  selected: null, order: [], expanded: {}, hours: 72, filter: 'all', layout: 'tree', showLinks: false, centered: false,
  seen: {}, baseline: 0, cards: new Map(), cardKeys: new Map(),
  goalOpen: false, busy: false, modal: null, drafts: {}, trunk: null, holding: false,
};

function _mmLoadStore() {
  try {
    const stored = JSON.parse(localStorage.getItem(MM_STORE_KEY) || '{}');
    mm.seen = stored.seen || {};
    mm.baseline = stored.baseline || 0;
    if (MM_HOURS.includes(stored.hours)) mm.hours = stored.hours;
  } catch (_e) { /* Unread markers are a convenience; start empty. */ }
  // A first visit has nothing to compare with: everything flagged would mean nothing.
  if (!mm.baseline) {
    mm.baseline = Date.now() / 1000;
    _mmSaveStore();
  }
}

function _mmSaveStore() {
  try {
    localStorage.setItem(MM_STORE_KEY, JSON.stringify({seen: mm.seen, baseline: mm.baseline, hours: mm.hours}));
  } catch (_e) { /* Private window or blocked storage. */ }
}

function _mmEl(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function _mmAgo(at) {
  const hours = (Date.now() / 1000 - at) / 3600;
  if (hours < 1) return '剛剛';
  if (hours < 24) return `${Math.round(hours)} 小時前`;
  return `${Math.floor(hours / 24)} 天 ${Math.round(hours % 24)} 小時前`;
}

function _mmChanged(s) {
  const seen = mm.seen[s.id];
  return s.last_activity_at > (seen === undefined ? mm.baseline : seen);
}

function _mmBlocked(s) { return s.status === 'waiting' || s.status === 'failed'; }

// The time range decides what is shown; what is live, pinned or selected always is.
function _mmInRange(s) {
  return s.pinned || s.status === 'running' || s.status === 'waiting' || s.id === mm.selected
    || (Date.now() / 1000 - s.last_activity_at) <= mm.hours * 3600;
}

function _mmFaded(s) {
  const match = mm.filter === 'all' || (mm.filter === 'need' && _mmBlocked(s)) || (mm.filter === 'changed' && _mmChanged(s));
  return !_mmInRange(s) || !match;
}

function _mmWhen(s, prefix) {
  const label = MM_STATUS[s.status].label;
  return `${label ? `${label} · ` : ''}${prefix}${_mmAgo(s.last_activity_at)}`;
}

function _mmSession(id) {
  return mm.data && mm.data.sessions.find(s => s.id === id);
}

function _mmSummaryRows(parent, s) {
  parent.append(_mmEl('span', '', '進度'), _mmEl('span', '', s.progress));
  if (!s.blocker) return;
  const head = _mmEl('span', '', '卡點');
  const body = _mmEl('span', 'mm-blocked', s.blocker);
  head.style.color = body.style.color = MM_STATUS[s.status].color;
  parent.append(head, body);
}

function _mmCard(s) {
  const meta = MM_STATUS[s.status];
  const node = _mmEl('div', 'mm-node');
  const card = _mmEl('button', 'mm-card');
  card.type = 'button';
  card.dataset.sid = s.id;
  card.style.borderColor = meta.color;
  card.style.background = meta.bg;
  const changed = _mmChanged(s);
  card.setAttribute('aria-label', `${s.goal}，${_mmWhen(s, '')}${changed ? '，有新變化' : ''}`);
  const dot = _mmEl('span', 'mm-dot');
  dot.style.background = meta.color;
  dot.setAttribute('aria-hidden', 'true');
  const body = _mmEl('span', 'mm-card-body');
  const title = _mmEl('span', 'mm-card-title', `${s.pinned ? '◆ ' : ''}${s.goal}`);
  const line = _mmEl('span', 'mm-card-meta', _mmWhen(s, ''));
  line.style.color = meta.color;
  const sum = _mmEl('span', 'mm-card-sum');
  _mmSummaryRows(sum, s);
  body.append(title, line, sum);
  card.append(dot, body);
  node.append(card);
  if (changed) {
    const bang = _mmEl('span', 'mm-bang', '!');
    bang.setAttribute('aria-hidden', 'true');
    node.append(bang);
  }
  return node;
}

function _mmCardKey(s) {
  // The relative time is part of the key on purpose: it is what the card shows.
  return JSON.stringify([s, _mmChanged(s), _mmAgo(s.last_activity_at)]);
}

// Reuse a card whose content did not change. Rebuilding only what moved is what
// keeps a live update from costing a full repaint of the map.
function _mmSyncCards(visible) {
  const canvas = $('mmCanvas');
  const wanted = new Set(visible.map(s => s.id));
  for (const [id, node] of mm.cards) {
    if (!wanted.has(id)) { node.remove(); mm.cards.delete(id); mm.cardKeys.delete(id); }
  }
  for (const s of visible) {
    const key = _mmCardKey(s);
    if (mm.cardKeys.get(s.id) === key) continue;
    const fresh = _mmCard(s);
    const old = mm.cards.get(s.id);
    if (old) old.replaceWith(fresh); else canvas.append(fresh);
    mm.cards.set(s.id, fresh);
    mm.cardKeys.set(s.id, key);
  }
}

function _mmPlace(node, x, y) {
  node.style.transform = `translate(${Math.round(x)}px,${Math.round(y)}px)`;
}

function _mmPath(svg, d, className) {
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', d);
  if (className) path.setAttribute('class', className);
  svg.append(path);
}

function _mmLine(x1, y1, x2, y2, tree) {
  if (!tree) return `M${x1} ${y1}L${x2} ${y2}`;
  const mid = (x1 + x2) / 2;
  return `M${x1} ${y1}H${mid}V${y2}H${x2}`;
}

// Heights are measured from the rendered cards, so summaries of any length never overlap.
function _mmLayout(groups) {
  const canvas = $('mmCanvas');
  const svg = $('mmEdges');
  const tree = mm.layout === 'tree';
  const geo = tree ? MM_TREE : MM_RADIAL;
  svg.replaceChildren();
  canvas.querySelectorAll('.mm-profile-node,.mm-link-label').forEach(el => el.remove());

  const stacks = groups.map(g => {
    const heights = g.visible.map(s => mm.cards.get(s.id).offsetHeight + MM_CARD.gap);
    return {...g, heights, height: Math.max(heights.reduce((a, b) => a + b, 0), 130)};
  });
  const columns = tree ? [stacks]
    : [stacks.filter((_g, i) => i % 2 === 0), stacks.filter((_g, i) => i % 2 === 1)];
  const columnHeight = c => c.reduce((h, g) => h + g.height, 0) + Math.max(c.length - 1, 0) * 30;
  const view = $('mmMap');
  // At least the view, so the centre can sit at its place there however small the tree.
  const height = Math.max(420 + 80, view.clientHeight, ...columns.map(c => columnHeight(c) + 80));
  // A tree that fits is drawn level with the centre; a taller one fills the canvas.
  const level = view.clientHeight * MM_ME_AT;
  canvas.style.width = `${geo.width}px`;
  canvas.style.height = `${height}px`;
  svg.setAttribute('width', geo.width);
  svg.setAttribute('height', height);

  const cx = geo.center, cy = height / 2;
  mm.trunk = {cx, height, tree, ends: []};
  const anchors = {};
  columns.forEach((column, ci) => {
    const side = tree || ci === 1 ? 1 : -1;
    const h = columnHeight(column);
    let y = Math.min(Math.max(level - h / 2, 40), height - 40 - h);
    column.forEach(g => {
      const px = tree ? geo.profile : cx + side * geo.profile;
      const sx = tree ? geo.session : cx + side * geo.session;
      const py = y + g.height / 2;
      _mmPath(svg, '', 'mm-trunk');
      mm.trunk.ends.push({px, py});
      const profile = _mmProfileNode(g);
      canvas.append(profile);
      _mmPlace(profile, px - profile.offsetWidth / 2, py - profile.offsetHeight / 2);
      let sy = y + (g.height - g.heights.reduce((a, b) => a + b, 0)) / 2;
      g.visible.forEach((s, i) => {
        const node = mm.cards.get(s.id);
        const mid = sy + g.heights[i] / 2;
        const inner = side > 0 ? sx - MM_CARD.w / 2 : sx + MM_CARD.w / 2;
        _mmPlace(node, sx - MM_CARD.w / 2, mid - node.offsetHeight / 2);
        node.style.opacity = _mmFaded(s) ? '0.22' : '1';
        node.style.zIndex = mm.selected === s.id ? '5' : '3';
        const card = node.firstChild;
        card.classList.toggle('mm-selected', mm.selected === s.id);
        card.classList.toggle('mm-pulse', s.status === 'waiting' && !_mmFaded(s));
        _mmPath(svg, _mmLine(px, py, inner, mid, tree));
        anchors[s.id] = {x: sx, y: mid, side};
        sy += g.heights[i];
      });
      y += g.height + 30;
    });
  });
  // Arrow keys walk the cards in the order they are drawn: down each column.
  mm.order = columns.flat().flatMap(g => g.visible.map(s => s.id));
  if (mm.showLinks) _mmDrawLinks(svg, canvas, anchors, tree);
  if (!mm.centered) {
    view.scrollLeft = tree ? 0 : cx - view.clientWidth / 2;
    mm.centered = true;
  }
  _mmFloatMe();
}

// The centre floats at the middle of what is in view, so a taller map after a
// range change never pushes it out of sight. Only its own edges are redrawn.
function _mmFloatMe() {
  const t = mm.trunk, view = $('mmMap');
  if (!t || !view) return;
  const half = 46;
  const cy = Math.min(Math.max(view.scrollTop + view.clientHeight * MM_ME_AT, half), t.height - half);
  _mmPlace($('mmMe'), t.cx - half, cy - half);
  $('mmEdges').querySelectorAll('.mm-trunk').forEach((path, i) => {
    const end = t.ends[i];
    path.setAttribute('d', _mmLine(t.cx, cy, end.px, end.py, t.tree));
  });
  // Nothing in the range: the centre stays, to step to a wider one, with no branches.
  const empty = $('mmEmpty');
  if (!empty.hidden) _mmPlace(empty, t.cx + half + 24, cy - empty.offsetHeight / 2);
}

function _mmProfileNode(g) {
  const node = _mmEl('div', 'mm-node mm-profile mm-profile-node');
  node.style.zIndex = '2';
  const pill = _mmEl('div', 'mm-profile-pill', g.name);
  pill.append(_mmEl('small', '', `hermes · ${g.name} · ${g.active} 執行中`));
  pill.tabIndex = 0;
  pill.setAttribute('role', 'button');
  pill.setAttribute('aria-label', `在 ${g.name} 開新會話`);
  pill.title = '雙擊開新會話';
  pill.ondblclick = () => _mmNewSession(g.name);
  pill.onkeydown = e => {
    if (e.key !== 'Enter' || e.isComposing) return;
    e.preventDefault();
    _mmNewSession(g.name);
  };
  node.append(pill);
  if (g.olderCount) {
    const open = !!mm.expanded[g.name];
    const toggle = _mmEl('button', 'mm-done-toggle', open ? '收合較早的' : `+${g.olderCount} 個較早的`);
    toggle.type = 'button';
    toggle.setAttribute('aria-expanded', String(open));
    toggle.onclick = () => { mm.expanded[g.name] = !open; _mmRender(); };
    node.append(toggle);
  }
  return node;
}

function _mmDrawLinks(svg, canvas, anchors, tree) {
  mm.data.links.forEach((link, i) => {
    const a = anchors[link.from], b = anchors[link.to];
    if (!a || !b) return;
    let lx, ly;
    if (tree || a.side === b.side) {
      const side = tree ? 1 : a.side;
      const edge = a.x + side * MM_CARD.w / 2;
      const out = edge + side * (16 + i * 16);
      _mmPath(svg, `M${edge} ${a.y}H${out}V${b.y}H${edge}`, 'mm-link');
      lx = out; ly = (a.y + b.y) / 2;
    } else {
      _mmPath(svg, `M${a.x} ${a.y}L${b.x} ${b.y}`, 'mm-link');
      lx = (a.x + b.x) / 2; ly = (a.y + b.y) / 2;
    }
    const label = _mmEl('span', 'mm-node mm-link-label', '分叉');
    label.style.left = `${lx}px`;
    label.style.top = `${ly}px`;
    label.style.zIndex = '1';
    canvas.append(label);
  });
}

function _mmDetail() {
  const box = $('mmDetail');
  // A live refresh must not take the box away from someone typing in it;
  // choosing another card, or none, still does.
  const typing = document.activeElement && document.activeElement.id === 'mmGoal';
  if (typing && box.dataset.sid === mm.selected) return;
  const s = _mmSession(mm.selected);
  box.dataset.sid = s ? s.id : '';
  box.hidden = !s;
  if (!s) {
    box.replaceChildren();
    return;
  }
  const meta = MM_STATUS[s.status];
  const wrap = _mmEl('div', 'mm-detail');
  const top = _mmEl('div', 'mm-detail-top');
  const heading = _mmEl('div');
  const badge = _mmEl('span', 'mm-badge', _mmWhen(s, '最後活動 '));
  badge.style.color = meta.color;
  badge.style.background = meta.bg;
  const title = _mmEl('h2', '', `${s.pinned ? '◆ ' : ''}${s.goal} `);
  const edit = _mmEl('button', 'mm-goal-edit');
  edit.type = 'button';
  edit.setAttribute('aria-label', '編輯目標');
  edit.setAttribute('aria-expanded', String(mm.goalOpen));
  edit.title = '編輯目標';
  edit.innerHTML = MM_PENCIL_ICON;
  edit.onclick = () => { mm.goalOpen = !mm.goalOpen; _mmDetail(); $('mmGoal')?.select(); };
  title.append(edit);
  heading.append(_mmEl('span', 'mm-sub', `hermes · ${s.profile} · ${s.source}`), badge, title);
  const close = _mmEl('button', 'mm-btn mm-close');
  close.type = 'button';
  close.setAttribute('aria-label', '取消選取');
  close.innerHTML = MM_CLOSE_ICON;
  close.onclick = _mmDeselect;
  top.append(heading, close);
  const list = _mmEl('dl');
  const progress = _mmEl('dd');
  progress.append(_mmProgressLink(s));
  list.append(_mmEl('dt', '', '進度'), progress);
  if (s.blocker) list.append(_mmEl('dt', '', '卡點'), _mmEl('dd', '', s.blocker));
  wrap.append(top);
  if (mm.goalOpen) wrap.append(_mmGoalBox(s));
  wrap.append(list);
  if (s.events.length) {
    const events = _mmEl('div', 'mm-events');
    events.append(_mmEl('strong', '', '最近事件'));
    s.events.forEach(e => events.append(_mmEl('span', '', `${e.role === 'user' ? '你' : 'agent'}：${e.text}`)));
    wrap.append(events);
  }
  box.replaceChildren(wrap);
}

function _mmButton(label, onClick, primary) {
  const btn = _mmEl('button', `mm-btn${primary ? ' mm-primary' : ''}`, label);
  btn.type = 'button';
  btn.disabled = mm.busy;
  btn.onclick = onClick;
  return btn;
}

// The progress line is the way into the workspace, for every source alike.
function _mmProgressLink(s) {
  const link = _mmEl('button', 'mm-progress-link', s.progress);
  link.type = 'button';
  link.title = '開啟工作區';
  link.setAttribute('aria-label', `開啟工作區：${s.progress}`);
  link.onclick = () => _mmOpen(s);
  // Upstream indexes external sessions on first lookup; do that before the click lands.
  if (s.read_only) link.onpointerenter = () => _mmWarm(s);
  return link;
}

function _mmWarm(s) {
  _mmRequest(s, `/api/session?session_id=${encodeURIComponent(s.id)}&messages=0&resolve_model=0`).catch(() => {});
}

function _mmGoalBox(s) {
  const box = _mmEl('div', 'mm-goal-form');
  const label = _mmEl('label', '', '目標（清空會回到 session 標題）');
  label.htmlFor = 'mmGoal';
  const input = _mmEl('input');
  input.id = 'mmGoal';
  input.type = 'text';
  input.maxLength = 120;
  input.value = s.goal;
  input.onkeydown = e => { if (e.key === 'Enter') _mmSaveGoal(s, input.value); };
  box.append(label, input, _mmButton('儲存', () => _mmSaveGoal(s, input.value), true));
  return box;
}

function _mmSaveGoal(s, value) {
  return _mmAct('儲存目標失敗', async () => {
    // Unchanged text stays out of the store, so a later retitle still shows.
    const goal = value.trim() === s.title ? '' : value;
    await _mmRequest(s, '/api/horo/goal', {session_id: s.id, goal});
    mm.goalOpen = false;
  });
}

function _mmRequest(s, path, body) {
  const headers = {'Content-Type': 'application/json', 'X-Horo-Profile': s.profile};
  return api(path, body ? {method: 'POST', body: JSON.stringify(body), headers} : {headers});
}

// One action at a time; the outcome is on the map, so the map is what refreshes.
async function _mmAct(failure, action) {
  if (mm.busy) return;
  mm.busy = true;
  _mmDetail();
  try {
    await action();
  } catch (e) {
    showToast(`${failure}：${e.message || e}`, 4000);
  } finally {
    mm.busy = false;
    await loadMindmap();
    _mmDetail();
  }
}

function _mmModalBar(s) {
  const meta = MM_STATUS[s.status];
  const dot = _mmEl('span', 'mm-dot');
  dot.style.background = meta.color;
  dot.setAttribute('aria-hidden', 'true');
  const heading = _mmEl('div', 'mm-modal-heading');
  heading.append(_mmEl('span', 'mm-sub', `hermes · ${s.profile} · 工作區`), _mmEl('strong', '', s.goal));
  const badge = _mmEl('span', 'mm-badge', _mmWhen(s, ''));
  badge.style.color = meta.color;
  badge.style.background = meta.bg;
  const close = _mmEl('button', 'mm-btn mm-close');
  close.id = 'mmModalClose';
  close.type = 'button';
  close.setAttribute('aria-label', '關閉工作區');
  close.innerHTML = MM_CLOSE_ICON;
  close.onclick = _mmCloseModal;
  const centre = _mmEl('div', 'mm-modal-centre');
  centre.append(dot, heading, badge);
  $('mmModalBar').replaceChildren(centre, close);
}

// Up at once, content hidden until the session it belongs to has loaded.
function _mmOpenBar(s) {
  let bar = $('mmModalBar');
  if (!bar) {
    bar = _mmEl('div');
    bar.id = 'mmModalBar';
    // Inside the map's own column, so the workspace covers the map and not the rail.
    document.querySelector('main.main').append(bar);
  }
  _mmModalBar(s);
  document.body.classList.add('mm-loading');
  _mmSetModal(true);
  $('mmModalClose').focus();
}

function _mmSetModal(open) {
  document.body.classList.toggle('mm-modal', open);
  // Everything behind the modal stays alive but out of reach, Tab included.
  MM_BEHIND_MODAL.forEach(selector => {
    const el = document.querySelector(selector);
    if (el) el.inert = open;
  });
}

// The workspace is upstream's chat view laid over the map, with its tool cards,
// approvals and streaming. The map keeps its DOM and scroll position underneath,
// so closing repaints nothing. Upstream's panel stays 'mindmap' throughout.
async function _mmOpen(s) {
  mm.seen[s.id] = s.last_activity_at;
  mm.modal = s.id;
  _mmSaveStore();
  _mmOpenBar(s);
  try {
    // The card's profile is where state.db holds the session; nothing else decides.
    await _mmEnsureProfile(s.profile);
    // Only a first open waits for the copy out of state.db; later ones open what
    // the WebUI holds and catch up behind it.
    if (s.read_only && !s.imported) await _mmImport(s);
    // Never follow the session to another profile: that is the card being wrong.
    await loadSession(s.id, {skipProfileResolve: true});
    if (!S.session || S.session.session_id !== s.id) {
      // loadSession reports on the page, not to its caller: ask once more for the reason.
      await api(`/api/session?session_id=${encodeURIComponent(s.id)}&messages=0&resolve_model=0`);
      throw new Error('載入的不是這個 session');
    }
    // The composer must never sit under another session's title, or another profile.
    const owner = S.session.profile || 'default';
    if (owner !== s.profile) throw new Error(_mmMismatch(s.profile, owner));
    document.body.classList.remove('mm-loading');
  } catch (e) {
    if (mm.modal === s.id) _mmCloseModal();
    const other = _sessionProfileMismatchFromError(e);
    showToast(`無法開啟工作區：${other ? _mmMismatch(s.profile, other.profile) : e.message || e}`, 5000);
    return;
  }
  if (s.read_only && s.imported) _mmCatchUp(s);
}

// Same as the sidebar: a failed import still opens read-only, unless the WebUI
// already filed the session under another profile.
function _mmImport(s) {
  return _mmRequest(s, '/api/session/import_cli', {session_id: s.id}).catch(e => {
    if (_sessionProfileMismatchFromError(e)) throw e;
  });
}

// Messages the CLI added since the WebUI's copy. import_cli answers with the
// whole transcript, so its answer is not shown: when the copy grew, the session
// reloads with the usual tail window.
async function _mmCatchUp(s) {
  const before = Number(S.session.message_count || 0);
  const current = () => mm.modal === s.id && S.session && S.session.session_id === s.id && !S.busy;
  try {
    await _mmImport(s);
    const {session} = await _mmRequest(s, `/api/session?session_id=${encodeURIComponent(s.id)}&messages=0&resolve_model=0`);
    if (!current() || !(Number(session.message_count) > before)) return;
    await loadSession(s.id, {force: true, keepStaleUntilLoaded: true, skipProfileResolve: true});
  } catch (_e) { /* What is shown is the WebUI's copy; the next open tries again. */ }
}

function _mmMismatch(card, owner) {
  return `卡片在 ${card}，WebUI 卻把這個會話記在 ${owner}`;
}

// Every tab of this WebUI shares one profile cookie, so another tab switching
// profile leaves this one's S.activeProfile stale, and upstream skips a switch to
// the profile it believes is already active. Ask the server, then switch for real.
async function _mmEnsureProfile(name) {
  const server = await api('/api/profile/active');
  if (server.name === name && S.activeProfile === name) return;
  if (S.activeProfile === name) S.activeProfile = server.name;
  await _switchProfileForSessionLoad(name);
  if (S.activeProfile !== name) throw new Error(`無法切換到 ${name}`);
}

// Back in this tab with a workspace open: another tab may have moved the cookie,
// and the next message would go out under its profile.
async function _mmHoldProfile() {
  if (mm.holding || !mm.modal || document.hidden || !S.session || S.session.session_id !== mm.modal) return;
  const profile = S.session.profile || 'default';
  // Focus and visibilitychange arrive together.
  mm.holding = true;
  try {
    await _mmEnsureProfile(profile);
  } catch (e) {
    showToast(`工作區的 profile ${profile} 已被其他分頁切走，且切不回來：${e.message || e}`, 6000);
  } finally {
    mm.holding = false;
  }
}

// A new session in a profile, opened straight in the workspace. Its card turns
// up once state.db has it, after the first message. An empty one the map made
// before is reused, as upstream's New Chat does, so double-clicks add no clutter.
async function _mmNewSession(profile) {
  if (mm.modal) return;
  const draft = {id: '', profile, status: 'idle', goal: '新會話', last_activity_at: Date.now() / 1000};
  // Not a session id yet: nothing on the map answers to it.
  const pending = mm.modal = `new:${profile}`;
  _mmOpenBar(draft);
  try {
    await _mmEnsureProfile(profile);
    const reused = await _mmReuseDraft(profile);
    if (!reused) {
      // The profile's own workspace, not that of whatever session was open last.
      S._profileSwitchWorkspace = null;
      const {last} = await api('/api/workspaces');
      const options = {worktree: false};
      if (last) options.workspace = last;
      // A project filter left in the sidebar must not file the session under it.
      if (_activeProject) options.project_id = null;
      await newSession(false, options);
    }
    if (!S.session || (S.session.profile || 'default') !== profile) throw new Error('新會話不在這個 profile');
    mm.drafts[profile] = S.session.session_id;
    // Closed while it loaded: the session stays, the workspace does not reopen.
    if (mm.modal !== pending) return;
    mm.modal = S.session.session_id;
    document.body.classList.remove('mm-loading');
    $('msg')?.focus();
  } catch (e) {
    if (mm.modal === pending) _mmCloseModal();
    showToast(`無法開新會話：${e.message || e}`, 4000);
  }
}

async function _mmReuseDraft(profile) {
  const sid = mm.drafts[profile];
  if (!sid) return false;
  if (S.session && S.session.session_id === sid) return _currentSessionIsReusableEmptyChat();
  try {
    const {session} = await api(`/api/session?session_id=${encodeURIComponent(sid)}&messages=0&resolve_model=0`);
    if (!_isRestorableNewChatDraftSession(session)) throw new Error('used');
    await loadSession(sid, {skipLineageResolve: true});
    return !!(S.session && S.session.session_id === sid);
  } catch (_e) {
    delete mm.drafts[profile];
    return false;
  }
}

function _mmCloseModal() {
  if (!mm.modal) return;
  const id = mm.modal;
  mm.modal = null;
  document.body.classList.remove('mm-loading');
  _mmSetModal(false);
  document.querySelector(`.mm-card[data-sid="${CSS.escape(id)}"]`)?.focus({preventScroll: true});
  // The session just left is the one selected; a new session's card arrives with this refresh.
  loadMindmap().then(() => {
    if (mm.selected === id || mm.modal || !_mmSession(id)) return;
    _mmSelect(id);
    _mmCardEl(id)?.focus({preventScroll: true});
  });
}

function _mmHeader() {
  const sessions = mm.data ? mm.data.sessions : [];
  const changed = sessions.filter(_mmChanged).length;
  const need = sessions.filter(_mmBlocked).length;
  $('mmChangedCount').textContent = changed;
  const labels = {all: '全部', need: `需要我 (${need})`, changed: `有變化 (${changed})`};
  document.querySelectorAll('[data-mm-filter]').forEach(btn => {
    btn.textContent = labels[btn.dataset.mmFilter];
    btn.setAttribute('aria-pressed', String(mm.filter === btn.dataset.mmFilter));
  });
  document.querySelectorAll('[data-mm-layout]').forEach(btn => {
    btn.setAttribute('aria-pressed', String(mm.layout === btn.dataset.mmLayout));
  });
  document.querySelectorAll('[data-mm-hours]').forEach(btn => {
    btn.setAttribute('aria-pressed', String(mm.hours === Number(btn.dataset.mmHours)));
  });
  const range = _mmRange();
  $('mmMeHours').textContent = range;
  $('mmMe').setAttribute('aria-label', `時間範圍：${range}`);
  const at = MM_HOURS.indexOf(mm.hours);
  $('mmMeLonger').disabled = at <= 0;
  $('mmMeShorter').disabled = at >= MM_HOURS.length - 1;
}

// The range is kept per browser, so a reload shows what was last chosen.
function _mmSetHours(hours) {
  mm.hours = hours;
  _mmSaveStore();
  _mmRender();
}

function _mmRange() {
  return `近 ${mm.hours % 24 ? `${mm.hours} 小時` : `${mm.hours / 24} 天`}`;
}

// A profile with nothing in the time range is left off, older sessions and all.
function _mmGroups() {
  const sessions = mm.data.sessions;
  return mm.data.profiles
    .map(name => {
      const all = sessions.filter(s => s.profile === name);
      const older = all.filter(s => !_mmInRange(s));
      const open = !!mm.expanded[name];
      return {
        name, olderCount: older.length, active: all.filter(s => s.status === 'running').length,
        visible: all.filter(s => open || _mmInRange(s)),
        inRange: all.length - older.length,
      };
    })
    .filter(g => g.inRange > 0);
}

function _mmRender() {
  if (!mm.data) return;
  const errors = $('mmErrors');
  errors.hidden = !mm.data.errors.length;
  errors.textContent = mm.data.errors.length
    ? `無法讀取：${mm.data.errors.map(e => `${e.profile}（${e.error}）`).join('、')}`
    : '';
  const groups = _mmGroups();
  $('mmEmpty').textContent = `${_mmRange()}沒有有人參與的 session。`;
  $('mmEmpty').hidden = groups.length > 0;
  _mmSyncCards(groups.flatMap(g => g.visible));
  _mmLayout(groups);
  _mmHeader();
  _mmDetail();
  const open = mm.modal && _mmSession(mm.modal);
  if (open) _mmModalBar(open);
}

async function loadMindmap() {
  if (mm.inFlight) return;
  mm.inFlight = true;
  try {
    const url = `/api/horo/overview${mm.version ? `?since=${encodeURIComponent(mm.version)}` : ''}`;
    const data = await api(url);
    mm.version = data.version;
    if (!data.unchanged) {
      mm.data = data;
      _mmRender();
    }
    $('mmFailed').hidden = true;
  } catch (e) {
    // Keep showing the last good map, but never let it pass for live data.
    const failed = $('mmFailed');
    failed.textContent = `無法更新心智圖：${e.message || e}。畫面是上次取得的資料。`;
    failed.hidden = false;
  } finally {
    mm.inFlight = false;
  }
}

function _mmSelect(id) {
  const s = _mmSession(id);
  if (!s) return;
  if (mm.selected !== id) mm.goalOpen = false;
  mm.selected = id;
  mm.seen[id] = s.last_activity_at;
  _mmSaveStore();
  _mmRender();
}

function _mmDeselect() {
  if (!mm.selected) return;
  mm.selected = null;
  mm.goalOpen = false;
  _mmRender();
}

function _mmCardEl(id) {
  return document.querySelector(`.mm-card[data-sid="${CSS.escape(id)}"]`);
}

function _mmStep(by) {
  const at = mm.order.indexOf(mm.selected);
  const next = mm.order[Math.min(Math.max(at < 0 ? 0 : at + by, 0), mm.order.length - 1)];
  if (!next || next === mm.selected) return;
  _mmSelect(next);
  const card = _mmCardEl(next);
  card?.focus({preventScroll: true});
  card?.scrollIntoView({block: 'nearest', inline: 'nearest'});
}

function _mmScheduleRefresh() {
  clearTimeout(mm.debounce);
  mm.debounce = setTimeout(loadMindmap, 250);
}

function _mmStartLive() {
  if (mm.source) return;
  // One stream for the whole map, never one per session: the browser allows six
  // connections to this origin, and ordinary requests need them too.
  mm.source = new EventSource('api/sessions/events');
  mm.source.addEventListener('sessions_changed', _mmScheduleRefresh);
  mm.poll = setInterval(loadMindmap, MM_POLL_MS);
}

function _mmStopLive() {
  if (mm.source) { mm.source.close(); mm.source = null; }
  clearInterval(mm.poll);
  clearTimeout(mm.debounce);
}

function _mmMount() {
  if (mm.mounted) return;
  mm.mounted = true;
  _mmLoadStore();
  const root = $('mainMindmap');
  root.innerHTML = `
    <header class="mm-head">
      <div class="mm-title"><h1>Agent 心智圖</h1><span>上次查看後有 <strong id="mmChangedCount">0</strong> 個節點變化</span></div>
      <div class="mm-group" role="group" aria-label="版面">
        <button type="button" class="mm-pill" data-mm-layout="radial">放射</button>
        <button type="button" class="mm-pill" data-mm-layout="tree">樹狀</button>
      </div>
      <div class="mm-group" role="group" aria-label="過濾">
        <button type="button" class="mm-pill" data-mm-filter="all"></button>
        <button type="button" class="mm-pill" data-mm-filter="need"></button>
        <button type="button" class="mm-pill" data-mm-filter="changed"></button>
      </div>
      <div class="mm-group mm-hours" role="group" aria-label="時間範圍：較早有活動的 session 會變淡">
        <button type="button" class="mm-pill" data-mm-hours="168">7 天</button>
        <button type="button" class="mm-pill" data-mm-hours="72">3 天</button>
        <button type="button" class="mm-pill" data-mm-hours="48">2 天</button>
        <button type="button" class="mm-pill" data-mm-hours="24">1 天</button>
        <button type="button" class="mm-pill" data-mm-hours="6">6 小時</button>
      </div>
      <label class="mm-tail" title="會話開啟時先載入的訊息數，聊天頁也適用；更早的往上捲才載入">載入最近
        <select id="mmTail">${MM_TAILS.map(n => `<option value="${n}">${n}</option>`).join('')}</select> 則</label>
    </header>
    <div class="mm-errors" id="mmFailed" role="alert" hidden></div>
    <div class="mm-errors" id="mmErrors" role="alert" hidden></div>
    <div class="mm-body">
      <section class="mm-left">
        <div class="mm-scroll" id="mmMap">
          <div class="mm-canvas" id="mmCanvas">
            <div class="mm-node mm-empty" id="mmEmpty" hidden></div>
            <svg class="mm-edges" id="mmEdges" aria-hidden="true"></svg>
            <div class="mm-node mm-me" id="mmMe" role="group" aria-label="時間範圍">
              <button type="button" class="mm-me-step mm-me-longer" id="mmMeLonger" data-mm-step="-1" title="拉長時間範圍" aria-label="拉長時間範圍">‹</button>
              <button type="button" class="mm-me-step mm-me-shorter" id="mmMeShorter" data-mm-step="1" title="縮短時間範圍" aria-label="縮短時間範圍">›</button>
              <span class="mm-me-core" aria-hidden="true">我<small id="mmMeHours"></small></span>
            </div>
          </div>
        </div>
      </section>
      <aside class="mm-side" id="mmDetail"></aside>
    </div>`;
  const tail = $('mmTail');
  tail.value = String(_INITIAL_MSG_LIMIT);
  tail.onchange = () => {
    _INITIAL_MSG_LIMIT = Number(tail.value);
    try {
      localStorage.setItem(MM_TAIL_KEY, tail.value);
    } catch (_e) { /* Holds for this page only. */ }
  };
  let floating = 0;
  const float = () => { cancelAnimationFrame(floating); floating = requestAnimationFrame(_mmFloatMe); };
  $('mmMap').addEventListener('scroll', float, {passive: true});
  // The canvas is sized to the view, so a new size lays the map out again.
  let sizing = 0;
  window.addEventListener('resize', () => {
    cancelAnimationFrame(sizing);
    sizing = requestAnimationFrame(() => { if (mm.active) _mmRender(); });
  });
  root.addEventListener('click', e => {
    const card = e.target.closest('.mm-card');
    if (card) {
      _mmSelect(card.dataset.sid);
      card.scrollIntoView({block: 'nearest', inline: 'nearest'});
      if (e.ctrlKey || e.metaKey) _mmOpen(_mmSession(card.dataset.sid));
      return;
    }
    const filter = e.target.closest('[data-mm-filter]');
    if (filter) { mm.filter = filter.dataset.mmFilter; return _mmRender(); }
    const layout = e.target.closest('[data-mm-layout]');
    if (layout) { mm.layout = layout.dataset.mmLayout; mm.centered = false; return _mmRender(); }
    const hours = e.target.closest('[data-mm-hours]');
    if (hours) return _mmSetHours(Number(hours.dataset.mmHours));
    // Each half of the centre steps one range longer (left) or shorter (right),
    // stopping at the ends.
    const step = e.target.closest('[data-mm-step]');
    if (step) {
      const at = MM_HOURS.indexOf(mm.hours) + Number(step.dataset.mmStep);
      return _mmSetHours(MM_HOURS[Math.min(Math.max(at, 0), MM_HOURS.length - 1)]);
    }
    // Clicking anywhere that is not a control lets go of the selection.
    if (!e.target.closest('button,input,textarea,select,label,a,#mmDetail')) _mmDeselect();
  });
  // The first click of the two has already selected the card.
  root.addEventListener('dblclick', e => {
    const card = e.target.closest('.mm-card');
    const s = card && _mmSession(card.dataset.sid);
    if (s && !mm.modal) _mmOpen(s);
  });
}

// The panel keeps its DOM while hidden, so returning to the map repaints nothing.
function showMindmap() {
  _mmMount();
  mm.active = true;
  _mmStartLive();
  if (mm.data) _mmRender();
  return loadMindmap();
}

function hideMindmap() {
  mm.active = false;
  _mmCloseModal();
  _mmStopLive();
}

// The map sits above Chat in the navigation. Upstream's saved tab order puts
// every other tab after Chat, both at first paint and whenever it is reapplied.
function _mmNavFirst() {
  ['.rail', '.sidebar-nav'].forEach(selector => {
    const nav = document.querySelector(selector);
    const chat = nav && nav.querySelector('.nav-tab[data-panel="chat"]');
    const map = nav && nav.querySelector('.nav-tab[data-panel="mindmap"]');
    if (chat && map && map.nextElementSibling !== chat) nav.insertBefore(map, chat);
  });
}

document.addEventListener('DOMContentLoaded', () => {
  _mmNavFirst();
  if (typeof window._applyTabOrder !== 'function') return;
  const apply = window._applyTabOrder;
  window._applyTabOrder = function (...args) {
    const result = apply.apply(this, args);
    _mmNavFirst();
    return result;
  };
});

// The map's own tab, pressed in a workspace, goes back to the map. Upstream's
// handler would fold the sidebar instead, as for any tab pressed twice.
document.addEventListener('click', e => {
  if (!mm.modal || !e.target.closest('.rail .nav-tab[data-panel="mindmap"]')) return;
  e.stopPropagation();
  _mmCloseModal();
}, true);

window.addEventListener('focus', _mmHoldProfile);
document.addEventListener('visibilitychange', _mmHoldProfile);

// With a card selected: arrows move the selection, Enter opens the workspace,
// Esc lets go. In the workspace, Esc closes it and keeps the card selected, so
// the arrows pick the next one straight away.
document.addEventListener('keydown', e => {
  if (!mm.active || e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.isComposing) return;
  const target = document.activeElement;
  const typing = target && (/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName) || target.isContentEditable);
  if (mm.modal) {
    // A half-written message is worth more than a shortcut.
    if (e.key === 'Escape' && !(typing && target.value)) { e.preventDefault(); _mmCloseModal(); }
    return;
  }
  if (typing || !mm.selected) return;
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    _mmStep(e.key === 'ArrowDown' ? 1 : -1);
  } else if (e.key === 'Enter') {
    // Enter on another button presses that button.
    if (target && target.closest('button:not(.mm-card)')) return;
    e.preventDefault();
    _mmOpen(_mmSession(mm.selected));
  } else if (e.key === 'Escape') {
    e.preventDefault();
    _mmDeselect();
  }
});
