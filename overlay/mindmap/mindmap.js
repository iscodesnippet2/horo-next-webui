// Agent mind map. Reads /api/horo/overview: selecting a card never loads a
// transcript or touches the active profile, so it stays instant however large
// the sessions behind it are. Actions are scoped per request by X-Horo-Profile;
// only opening the workspace, which is upstream's chat view, switches profile.
const MM_STATUS = {
  running: {label: '執行中', color: '#1d4ed8', bg: '#eff6ff'},
  waiting: {label: '等待你', color: '#b45309', bg: '#fffbeb'},
  failed: {label: '失敗', color: '#6d28d9', bg: '#f5f3ff'},
  paused: {label: '已暫停', color: '#475569', bg: '#f1f5f9'},
  done: {label: '已完成', color: '#64748b', bg: '#f8fafc'},
};
const MM_STORE_KEY = 'horo-mindmap-v1';
const MM_POLL_MS = 15000; // Sessions outside this server emit no event.
const MM_CARD = {w: 480, gap: 14};
const MM_TREE = {center: 56, profile: 210, session: 570, width: 830};
const MM_RADIAL = {center: 940, profile: 240, session: 620, width: 1880};

const mm = {
  data: null, version: '', mounted: false, active: false, source: null, poll: null, debounce: null, inFlight: false,
  selected: null, expanded: {}, hours: 72, filter: 'all', layout: 'radial', showLinks: false, centered: false,
  seen: {}, baseline: 0, cards: new Map(), cardKeys: new Map(),
  replyOpen: false, replyText: '', busy: false,
};

function _mmLoadStore() {
  try {
    const stored = JSON.parse(localStorage.getItem(MM_STORE_KEY) || '{}');
    mm.seen = stored.seen || {};
    mm.baseline = stored.baseline || 0;
  } catch (_e) { /* Unread markers are a convenience; start empty. */ }
  // A first visit has nothing to compare with: everything flagged would mean nothing.
  if (!mm.baseline) {
    mm.baseline = Date.now() / 1000;
    _mmSaveStore();
  }
}

function _mmSaveStore() {
  try {
    localStorage.setItem(MM_STORE_KEY, JSON.stringify({seen: mm.seen, baseline: mm.baseline}));
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

function _mmFaded(s) {
  const inTime = s.pinned || (Date.now() / 1000 - s.last_activity_at) <= mm.hours * 3600;
  const match = mm.filter === 'all' || (mm.filter === 'need' && _mmBlocked(s)) || (mm.filter === 'changed' && _mmChanged(s));
  return !inTime || !match;
}

function _mmSession(id) {
  return mm.data && mm.data.sessions.find(s => s.id === id);
}

function _mmSummaryRows(parent, s) {
  const blocked = _mmBlocked(s);
  const color = blocked ? MM_STATUS[s.status].color : '';
  [['目標', s.goal], ['做到哪', s.progress], [blocked ? '卡點' : '下一步', s.next]].forEach(([label, value], i) => {
    const head = _mmEl('span', '', label);
    const body = _mmEl('span', i === 2 && blocked ? 'mm-blocked' : '', value);
    if (i === 2 && blocked) { head.style.color = color; body.style.color = color; }
    parent.append(head, body);
  });
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
  card.setAttribute('aria-label', `${s.title}，${meta.label}${changed ? '，有新變化' : ''}`);
  const dot = _mmEl('span', 'mm-dot');
  dot.style.background = meta.color;
  dot.setAttribute('aria-hidden', 'true');
  const body = _mmEl('span', 'mm-card-body');
  const title = _mmEl('span', 'mm-card-title', `${s.pinned ? '◆ ' : ''}${s.title}`);
  const line = _mmEl('span', 'mm-card-meta', `${meta.label} · ${_mmAgo(s.last_activity_at)}`);
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
  const height = Math.max(420, ...columns.map(columnHeight)) + 80;
  canvas.style.width = `${geo.width}px`;
  canvas.style.height = `${height}px`;
  svg.setAttribute('width', geo.width);
  svg.setAttribute('height', height);

  const cx = geo.center, cy = height / 2;
  _mmPlace($('mmMe'), cx - 46, cy - 46);
  const anchors = {};
  columns.forEach((column, ci) => {
    const side = tree || ci === 1 ? 1 : -1;
    let y = (height - columnHeight(column)) / 2;
    column.forEach(g => {
      const px = tree ? geo.profile : cx + side * geo.profile;
      const sx = tree ? geo.session : cx + side * geo.session;
      const py = y + g.height / 2;
      _mmPath(svg, _mmLine(cx, cy, px, py, tree));
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
  if (mm.showLinks) _mmDrawLinks(svg, canvas, anchors, tree);
  if (!mm.centered) {
    const view = $('mmMap');
    view.scrollLeft = tree ? 0 : cx - view.clientWidth / 2;
    mm.centered = true;
  }
}

function _mmProfileNode(g) {
  const node = _mmEl('div', 'mm-node mm-profile mm-profile-node');
  node.style.zIndex = '2';
  const pill = _mmEl('div', 'mm-profile-pill', g.name);
  pill.append(_mmEl('small', '', `hermes · ${g.name} · ${g.active} 進行中`));
  node.append(pill);
  if (g.doneCount) {
    const open = !!mm.expanded[g.name];
    const toggle = _mmEl('button', 'mm-done-toggle', open ? '收合已完成' : `+${g.doneCount} 個已完成`);
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
  // A live refresh must not take the box away from someone typing in it.
  if (document.activeElement && document.activeElement.id === 'mmReply' && box.contains(document.activeElement) && mm.replyOpen) return;
  const s = _mmSession(mm.selected);
  if (!s) {
    box.replaceChildren(_mmEl('div', 'mm-empty', '點選 session 查看摘要。'));
    return;
  }
  const meta = MM_STATUS[s.status];
  const wrap = _mmEl('div', 'mm-detail');
  const top = _mmEl('div', 'mm-detail-top');
  const heading = _mmEl('div');
  const badge = _mmEl('span', 'mm-badge', `${meta.label} · 最後活動 ${_mmAgo(s.last_activity_at)}`);
  badge.style.color = meta.color;
  badge.style.background = meta.bg;
  heading.append(_mmEl('span', 'mm-sub', `hermes · ${s.profile} · ${s.source}`),
    _mmEl('h2', '', `${s.pinned ? '◆ ' : ''}${s.title}`), badge);
  const close = _mmEl('button', 'mm-btn mm-close', '×');
  close.type = 'button';
  close.setAttribute('aria-label', '關閉');
  close.onclick = () => { mm.selected = null; _mmRender(); };
  top.append(heading, close);
  const list = _mmEl('dl');
  const blocked = _mmBlocked(s);
  [['目標', s.goal], ['做到哪', s.progress], [blocked ? '卡點' : '下一步', s.next]].forEach(([label, value]) => {
    list.append(_mmEl('dt', '', label), _mmEl('dd', '', value));
  });
  wrap.append(top, list);
  if (s.read_only) {
    wrap.append(_mmEl('span', 'mm-note', `這個 session 來自 ${s.source}，要繼續對話請開啟工作區。`));
  }
  if (s.events.length) {
    const events = _mmEl('div', 'mm-events');
    events.append(_mmEl('strong', '', '最近事件'));
    s.events.forEach(e => events.append(_mmEl('span', '', `${e.role === 'user' ? '你' : 'agent'}：${e.text}`)));
    wrap.append(events);
  }
  wrap.append(_mmActions(s));
  if (mm.replyOpen) wrap.append(_mmReplyBox(s));
  box.replaceChildren(wrap);
}

function _mmButton(label, onClick, primary) {
  const btn = _mmEl('button', `mm-btn${primary ? ' mm-primary' : ''}`, label);
  btn.type = 'button';
  btn.disabled = mm.busy;
  btn.onclick = onClick;
  return btn;
}

function _mmActions(s) {
  const row = _mmEl('div', 'mm-actions');
  if (!s.read_only) {
    row.append(_mmButton('回覆', () => { mm.replyOpen = !mm.replyOpen; _mmDetail(); $('mmReply')?.focus(); }, true));
  }
  if (s.stream_id) row.append(_mmButton('停止', () => _mmStop(s)));
  const open = _mmButton('開啟工作區', () => _mmOpen(s), s.read_only);
  // Upstream indexes external sessions on first lookup; do that before the click lands.
  if (s.read_only) open.onpointerenter = () => _mmWarm(s);
  row.append(open);
  row.append(_mmButton('分叉', () => _mmFork(s)));
  if (!s.read_only) row.append(_mmButton(s.pinned ? '取消釘選' : '釘選', () => _mmPin(s)));
  return row;
}

function _mmWarm(s) {
  _mmRequest(s, `/api/session?session_id=${encodeURIComponent(s.id)}&messages=0&resolve_model=0`).catch(() => {});
}

function _mmReplyBox(s) {
  const box = _mmEl('div', 'mm-reply');
  const label = _mmEl('label', '', '回覆給這個 session');
  label.htmlFor = 'mmReply';
  const text = _mmEl('textarea');
  text.id = 'mmReply';
  text.rows = 3;
  text.value = mm.replyText;
  text.oninput = () => { mm.replyText = text.value; };
  box.append(label, text, _mmButton('送出', () => _mmReply(s), true));
  return box;
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

function _mmReply(s) {
  const message = mm.replyText.trim();
  if (!message) return;
  return _mmAct('回覆失敗', async () => {
    await _mmRequest(s, '/api/chat/start', {session_id: s.id, message, profile: s.profile, goal_continuation_id: ''});
    mm.replyText = '';
    mm.replyOpen = false;
    showToast('已送出回覆');
  });
}

function _mmStop(s) {
  return _mmAct('停止失敗', () => _mmRequest(s, `/api/chat/cancel?stream_id=${encodeURIComponent(s.stream_id)}`));
}

function _mmPin(s) {
  return _mmAct('釘選失敗', () => _mmRequest(s, '/api/session/pin', {session_id: s.id, pinned: !s.pinned}));
}

function _mmFork(s) {
  return _mmAct('分叉失敗', async () => {
    const data = await _mmRequest(s, '/api/session/branch', {session_id: s.id});
    const id = data.session_id || (data.session && data.session.session_id);
    if (!id) throw new Error('伺服器沒有回傳新 session');
    mm.selected = id;
    mm.seen[id] = Date.now() / 1000;
    mm.replyOpen = true;
    mm.replyText = '';
    _mmSaveStore();
    showToast('已分叉出新 session');
  });
}

function _mmShowBack(show) {
  let back = $('mmBack');
  if (show && !back) {
    back = _mmEl('button', 'mm-btn', '← 返回心智圖');
    back.id = 'mmBack';
    back.type = 'button';
    back.onclick = () => switchPanel('mindmap');
    document.body.append(back);
  }
  if (back) back.hidden = !show;
}

// The workspace is upstream's chat view, with its tool cards, approvals and
// streaming. Leaving the panel's DOM in place is what makes the way back instant.
async function _mmOpen(s) {
  mm.seen[s.id] = s.last_activity_at;
  _mmSaveStore();
  await switchPanel('chat');
  _mmShowBack(true);
  try {
    if (s.read_only) {
      // Same as the sidebar: a failed import still opens read-only.
      await _mmRequest(s, '/api/session/import_cli', {session_id: s.id}).catch(() => {});
    }
    await loadSession(s.id);
  } catch (e) {
    showToast(`無法開啟工作區：${e.message || e}`, 4000);
  }
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
  const links = $('mmLinks');
  links.textContent = `分叉線：${mm.showLinks ? '顯示' : '隱藏'}`;
  links.setAttribute('aria-pressed', String(mm.showLinks));
  $('mmHoursLabel').textContent = mm.hours;
  $('mmMeHours').textContent = `近 ${mm.hours} 小時`;
}

function _mmGroups() {
  const sessions = mm.data.sessions;
  return mm.data.profiles
    .map(name => {
      const all = sessions.filter(s => s.profile === name);
      const done = all.filter(s => s.status === 'done');
      const open = !!mm.expanded[name];
      return {
        name, doneCount: done.length, active: all.length - done.length,
        visible: all.filter(s => s.status !== 'done' || open || s.id === mm.selected),
        total: all.length,
      };
    })
    .filter(g => g.total > 0);
}

function _mmRender() {
  if (!mm.data) return;
  const errors = $('mmErrors');
  errors.hidden = !mm.data.errors.length;
  errors.textContent = mm.data.errors.length
    ? `無法讀取：${mm.data.errors.map(e => `${e.profile}（${e.error}）`).join('、')}`
    : '';
  const groups = _mmGroups();
  $('mmEmpty').hidden = groups.length > 0;
  $('mmMap').hidden = !groups.length;
  _mmSyncCards(groups.flatMap(g => g.visible));
  _mmLayout(groups);
  _mmHeader();
  _mmDetail();
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
  if (mm.selected !== id) { mm.replyOpen = false; mm.replyText = ''; }
  mm.selected = id;
  mm.seen[id] = s.last_activity_at;
  _mmSaveStore();
  _mmRender();
}

function _mmMarkAllRead() {
  (mm.data ? mm.data.sessions : []).forEach(s => { mm.seen[s.id] = s.last_activity_at; });
  _mmSaveStore();
  _mmRender();
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
        <button type="button" class="mm-pill" data-mm-layout="tree">樹狀 →</button>
      </div>
      <button type="button" class="mm-pill" id="mmLinks"></button>
      <div class="mm-group" role="group" aria-label="過濾">
        <button type="button" class="mm-pill" data-mm-filter="all"></button>
        <button type="button" class="mm-pill" data-mm-filter="need"></button>
        <button type="button" class="mm-pill" data-mm-filter="changed"></button>
      </div>
      <button type="button" class="mm-btn" id="mmReadAll">全部標為已讀</button>
    </header>
    <div class="mm-errors" id="mmFailed" role="alert" hidden></div>
    <div class="mm-errors" id="mmErrors" role="alert" hidden></div>
    <div class="mm-body">
      <section class="mm-left">
        <div class="mm-empty" id="mmEmpty" hidden>近 72 小時沒有有人參與的 session。</div>
        <div class="mm-scroll" id="mmMap">
          <div class="mm-canvas" id="mmCanvas">
            <svg class="mm-edges" id="mmEdges" aria-hidden="true"></svg>
            <div class="mm-node mm-me" id="mmMe">我<small id="mmMeHours"></small></div>
          </div>
        </div>
        <div class="mm-slider">
          <label for="mmHours">時間範圍：顯示最近 <span id="mmHoursLabel"></span> 小時有活動的 session（其餘變淡）</label>
          <input id="mmHours" type="range" min="6" max="72" step="6" value="72">
          <div class="mm-ticks"><span>3 天</span><span>2 天</span><span>1 天</span><span>6 小時 · 現在</span></div>
        </div>
      </section>
      <aside class="mm-side" id="mmDetail"></aside>
    </div>`;
  root.addEventListener('click', e => {
    const card = e.target.closest('.mm-card');
    if (card) {
      _mmSelect(card.dataset.sid);
      if (e.ctrlKey || e.metaKey) _mmOpen(_mmSession(card.dataset.sid));
      return;
    }
    const filter = e.target.closest('[data-mm-filter]');
    if (filter) { mm.filter = filter.dataset.mmFilter; return _mmRender(); }
    const layout = e.target.closest('[data-mm-layout]');
    if (layout) { mm.layout = layout.dataset.mmLayout; mm.centered = false; return _mmRender(); }
    if (e.target.id === 'mmLinks') { mm.showLinks = !mm.showLinks; return _mmRender(); }
    if (e.target.id === 'mmReadAll') return _mmMarkAllRead();
  });
  $('mmHours').addEventListener('input', e => { mm.hours = Number(e.target.value); _mmRender(); });
}

// The panel keeps its DOM while hidden, so returning to the map repaints nothing.
function showMindmap() {
  _mmMount();
  mm.active = true;
  _mmShowBack(false);
  _mmStartLive();
  if (mm.data) _mmRender();
  return loadMindmap();
}

function hideMindmap() {
  mm.active = false;
  _mmStopLive();
  _mmShowBack(false);
}

document.addEventListener('keydown', e => {
  if (e.key !== 'Escape' || !$('mmBack') || $('mmBack').hidden) return;
  const target = document.activeElement;
  if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
  switchPanel('mindmap');
});
