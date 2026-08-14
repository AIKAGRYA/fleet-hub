/* Fleet Hub v0.6 — one ES module, zero build.
   store → api → sse → router → views. XSS-safe el() everywhere:
   every dynamic string lands via textContent, never interpolated markup. */

'use strict';

// ---------------------------------------------------------------- base

// Works under /fleet/ or any prefix: path up to the trailing slash.
const BASE = location.pathname.replace(/[^/]*$/, '');

const ACK_COPY = {
  PUBLISH_ACCEPTED: 'accepted by broker',
  NO_ACK: 'sent (no broker ack)',
};

// ---------------------------------------------------------------- el()

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'dataset') { for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = dv; }
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : String(v));
  }
  appendChildren(node, children);
  return node;
}

function appendChildren(node, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false || c === '') continue;
    if (Array.isArray(c)) appendChildren(node, c);
    else node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

// ---------------------------------------------------------------- time

function relTime(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '—';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

// Server timestamps arrive as iso strings (rows, chat) or unix-second floats
// (raw frames, presence SSE). Normalize everything to iso before display.
function normTs(v) {
  if (v === null || v === undefined || v === '') return null;
  if (typeof v === 'number' && Number.isFinite(v)) return new Date(v * 1000).toISOString();
  return String(v);
}

// A span the 30s ticker keeps fresh via [data-ts].
function relSpan(ts, cls = '') {
  const iso = normTs(ts);
  if (!iso) return el('span', { class: cls }, 'never');
  return el('span', { class: cls, dataset: { ts: iso } }, relTime(iso));
}

function startTicker() {
  setInterval(() => {
    document.querySelectorAll('[data-ts]').forEach((n) => {
      n.textContent = relTime(n.dataset.ts);
    });
  }, 30000);
}

function fmtNum(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
  return Number(n).toLocaleString('en-US');
}

// ---------------------------------------------------------------- store

const store = {
  state: {
    route: { name: 'now', uid: null },
    authenticated: false,
    conn: 'off',              // off | live | retry
    health: null,             // /api/health payload
    vision: null,             // /api/vision payload
    roster: [],               // presence rows, active + archived
    nodes: null,              // /api/nodes payload (lazy)
    chat: [],                 // chat messages (msg objects, mutated in place)
    dms: {},                  // uid -> messages[]
    raw: [],                  // raw frames, cap 400
    unseen: 0,
  },
  subs: new Map(),
  on(key, fn) {
    if (!this.subs.has(key)) this.subs.set(key, new Set());
    this.subs.get(key).add(fn);
    return () => this.subs.get(key).delete(fn);
  },
  emit(key, payload) {
    const set = this.subs.get(key);
    if (set) for (const fn of [...set]) { try { fn(payload); } catch (e) { console.error(e); } }
  },
};

const chatIndex = new Map();          // msg_id -> msg object
const dmIndexes = new Map();          // uid -> Map(msg_id -> msg)

function dmList(uid) {
  if (!store.state.dms[uid]) store.state.dms[uid] = [];
  return store.state.dms[uid];
}
function dmIndex(uid) {
  if (!dmIndexes.has(uid)) dmIndexes.set(uid, new Map());
  return dmIndexes.get(uid);
}

// Append-or-reconcile; pending bubbles resolve by msg_id (POST ack or SSE echo).
function applyChat(m) {
  const known = m.msg_id ? chatIndex.get(m.msg_id) : null;
  if (known) {
    known.from = m.from ?? known.from;
    known.text = m.text ?? known.text;
    known.ts = m.ts ?? known.ts;
    known.pending = false;
    known.failed = false;
    store.emit('chat', { kind: 'update', msg: known });
  } else {
    if (m.msg_id) chatIndex.set(m.msg_id, m);
    store.state.chat.push(m);
    store.emit('chat', { kind: 'append', msg: m });
  }
}

function applyDm(uid, m) {
  const idx = dmIndex(uid);
  const known = m.msg_id ? idx.get(m.msg_id) : null;
  if (known) {
    known.from = m.from ?? known.from;
    known.text = m.text ?? known.text;
    known.ts = m.ts ?? known.ts;
    known.pending = false;
    known.failed = false;
    store.emit('dm:' + uid, { kind: 'update', msg: known });
  } else {
    if (m.msg_id) idx.set(m.msg_id, m);
    dmList(uid).push(m);
    store.emit('dm:' + uid, { kind: 'append', msg: m });
  }
}

function replaceChat(messages) {
  const keepers = store.state.chat.filter((m) => m.pending || m.failed);
  chatIndex.clear();
  store.state.chat = [];
  for (const m of messages) {
    if (m.msg_id) chatIndex.set(m.msg_id, m);
    store.state.chat.push(m);
  }
  for (const m of keepers) {
    if (m.msg_id && chatIndex.has(m.msg_id)) continue;
    if (m.msg_id) chatIndex.set(m.msg_id, m);
    store.state.chat.push(m);
  }
  store.emit('chat', { kind: 'reset' });
}

function replaceDms(uid, messages) {
  const keepers = dmList(uid).filter((m) => m.pending || m.failed);
  const idx = dmIndex(uid);
  idx.clear();
  store.state.dms[uid] = [];
  for (const m of messages) {
    if (m.msg_id) idx.set(m.msg_id, m);
    store.state.dms[uid].push(m);
  }
  for (const m of keepers) {
    if (m.msg_id && idx.has(m.msg_id)) continue;
    if (m.msg_id) idx.set(m.msg_id, m);
    store.state.dms[uid].push(m);
  }
  store.emit('dm:' + uid, { kind: 'reset' });
}

function pushRaw(frame) {
  store.state.raw.push(frame);
  if (store.state.raw.length > 400) store.state.raw.splice(0, store.state.raw.length - 400);
  store.emit('raw', frame);
}

function mergePresence(p) {
  const row = store.state.roster.find((r) => r.uid === p.uid);
  if (!row) return;
  row.last_heard = normTs(p.last_heard) ?? row.last_heard;
  row.last_addressed = normTs(p.last_addressed) ?? row.last_addressed;
  if (p.contact) row.contact = p.contact;
  if (p.freshness) row.freshness = p.freshness;
  store.emit('roster');
}

function findAgent(key) {
  if (!key) return null;
  const k = String(key).toLowerCase();
  return store.state.roster.find((r) =>
    r.uid.toLowerCase() === k ||
    (r.callsign || '').toLowerCase() === k ||
    (r.display_name || '').toLowerCase() === k) || null;
}

function activeAgents() {
  return store.state.roster.filter((r) => r.seat !== 'archived');
}
function archivedAgents() {
  return store.state.roster.filter((r) => r.seat === 'archived');
}

// ---------------------------------------------------------------- api

async function api(path, opts = {}) {
  const res = await fetch(BASE + path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (res.status === 401) {
    onUnauthorized();
    throw new Error('unauthorized');
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j && j.detail) detail = String(j.detail);
    } catch { /* non-JSON error body */ }
    throw new Error(detail);
  }
  return res.json();
}

// ---------------------------------------------------------------- sse

const sse = {
  es: null,
  epoch: null,

  open() {
    this.close();
    const es = new EventSource(BASE + 'events/stream');
    this.es = es;

    es.onopen = () => setConn('live');
    es.onerror = () => setConn('retry');

    es.addEventListener('hello', (e) => {
      setConn('live');
      try {
        const d = JSON.parse(e.data);
        if (this.epoch && d.epoch !== this.epoch) refetchCore();
        this.epoch = d.epoch;
      } catch { /* ignore malformed hello */ }
    });

    es.addEventListener('reset', () => { refetchCore(); });

    es.addEventListener('chat', (e) => {
      try {
        const m = JSON.parse(e.data);
        const isEcho = m.msg_id && chatIndex.has(m.msg_id);
        applyChat(m);
        if (!isEcho && m.from !== 'operator') notify(m.from || 'fleet', m.text || '');
      } catch { /* drop malformed frame */ }
    });

    es.addEventListener('dm', (e) => {
      try {
        const m = JSON.parse(e.data);
        if (!m.uid) return;
        const isEcho = m.msg_id && dmIndex(m.uid).has(m.msg_id);
        applyDm(m.uid, { msg_id: m.msg_id, from: m.from, text: m.text, ts: m.ts });
        if (!isEcho && m.from !== 'operator') notify(`${m.from || m.uid} (dm)`, m.text || '');
      } catch { /* drop malformed frame */ }
    });

    es.addEventListener('raw', (e) => {
      try { pushRaw(JSON.parse(e.data)); } catch { /* drop */ }
    });

    es.addEventListener('presence', (e) => {
      try { mergePresence(JSON.parse(e.data)); } catch { /* drop */ }
    });

    es.addEventListener('health', (e) => {
      pulseConnDot();
      try {
        const d = JSON.parse(e.data);
        const h = store.state.health;
        if (h) {
          if (h.broker) {
            h.broker.connected = d.connected;
            if (d.last_seq !== undefined) h.broker.last_seq = d.last_seq;
          }
          if (h.monitor && d.monitor_ok !== undefined) h.monitor.ok = d.monitor_ok;
          h.ok = Boolean(d.connected && h.auth_configured);
          store.emit('health');
        }
      } catch { /* drop */ }
    });
  },

  close() {
    if (this.es) { try { this.es.close(); } catch { /* already closed */ } }
    this.es = null;
  },

  closed() {
    return !this.es || this.es.readyState === EventSource.CLOSED;
  },
};

function setConn(state) {
  store.state.conn = state;
  const dot = document.getElementById('conn-dot');
  const label = document.getElementById('conn-label');
  dot.className = `conn-dot conn-dot--${state}`;
  label.textContent = state === 'live' ? 'live' : state === 'retry' ? 'retry' : 'off';
}

// One pulse when reality moves (server heartbeat); never an infinite animation.
function pulseConnDot() {
  const dot = document.getElementById('conn-dot');
  dot.classList.remove('pulse');
  void dot.offsetWidth;
  dot.classList.add('pulse');
}

// ---------------------------------------------------------------- notify seam

function notify(title, body) {
  if (!document.hidden) return;
  store.state.unseen += 1;
  if (navigator.setAppBadge) { try { navigator.setAppBadge(store.state.unseen); } catch { /* no badge */ } }
  if ('Notification' in window && Notification.permission === 'granted') {
    try {
      new Notification(String(title), { body: String(body).slice(0, 140), tag: 'fleet-hub' });
    } catch { /* Notification constructor may be unavailable */ }
  }
}

function clearUnseen() {
  store.state.unseen = 0;
  if (navigator.clearAppBadge) { try { navigator.clearAppBadge(); } catch { /* no badge */ } }
}

// ---------------------------------------------------------------- send

async function postSend(msg, uid) {
  try {
    const res = await api('api/send', {
      method: 'POST',
      body: JSON.stringify({ text: msg.text, to: msg.to, msg_id: msg.msg_id }),
    });
    if (!res.ok) throw new Error(res.error || 'send failed');
    msg.pending = false;
    msg.failed = false;
    msg.tier = res.ack_tier || null;
    msg.seq = res.seq ?? null;
  } catch (e) {
    msg.pending = false;
    msg.failed = true;
    msg.error = e.message;
  }
  if (uid) store.emit('dm:' + uid, { kind: 'update', msg });
  else store.emit('chat', { kind: 'update', msg });
}

function sendMessage(text, row) {
  const msg = {
    msg_id: crypto.randomUUID(),
    from: 'operator',
    text,
    ts: new Date().toISOString(),
    to: row ? (row.callsign || row.uid) : null,
    pending: true,
    failed: false,
    tier: null,
  };
  if (row) {
    dmIndex(row.uid).set(msg.msg_id, msg);
    dmList(row.uid).push(msg);
    store.emit('dm:' + row.uid, { kind: 'append', msg });
    postSend(msg, row.uid);
  } else {
    chatIndex.set(msg.msg_id, msg);
    store.state.chat.push(msg);
    store.emit('chat', { kind: 'append', msg });
    postSend(msg, null);
  }
}

function retrySend(msg, uid) {
  if (!msg.failed) return;
  msg.failed = false;
  msg.pending = true;
  if (uid) store.emit('dm:' + uid, { kind: 'update', msg });
  else store.emit('chat', { kind: 'update', msg });
  postSend(msg, uid);
}

// Command bar grammar: "/dm <callsign> message" → DM; anything else → current target.
// Returns false to keep the input contents (error case).
function handleCommand(rawText, currentRow, showError) {
  const text = rawText.trim();
  if (!text) return false;
  if (text.startsWith('/dm')) {
    const m = text.match(/^\/dm\s+(\S+)\s+([\s\S]+)$/);
    if (!m) { showError('usage: /dm <callsign> message'); return false; }
    const row = findAgent(m[1]);
    if (!row) {
      const known = activeAgents().map((r) => r.callsign || r.uid).join(', ');
      showError(`unknown callsign "${m[1]}" — active: ${known || 'none'}`);
      return false;
    }
    sendMessage(m[2].trim(), row);
    if (!currentRow || currentRow.uid !== row.uid) location.hash = '#/agent/' + encodeURIComponent(row.uid);
    return true;
  }
  if (text.startsWith('/')) { showError('unknown command — only /dm <callsign> message'); return false; }
  sendMessage(text, currentRow || null);
  return true;
}

// ---------------------------------------------------------------- shared view pieces

function provenance(...parts) {
  return el('footer', { class: 'provenance' }, `source: ${parts.join(' · ')}`);
}

function sectionEyebrow(text) {
  return el('h2', { class: 'eyebrow section-eyebrow' }, text);
}

function freshnessDot(row) {
  const f = row.seat === 'archived' ? 'archived' : (row.freshness || 'never');
  return el('span', { class: `dot dot--${f}`, 'aria-hidden': 'true' });
}

function twoSignalLine(row) {
  // "heard 3m · addressed 40s" — the two signals, never conflated.
  return el('span', {},
    'heard ', relSpan(row.last_heard),
    ' · addressed ', relSpan(row.last_addressed));
}

function commandBar({ placeholder, currentRow }) {
  const err = el('div', { class: 'cmd-err', hidden: true });
  let errTimer = null;
  const showError = (text) => {
    err.textContent = text;
    err.hidden = false;
    clearTimeout(errTimer);
    errTimer = setTimeout(() => { err.hidden = true; }, 6000);
  };
  const input = el('input', {
    class: 'cmd-input', type: 'text', enterkeyhint: 'send',
    autocapitalize: 'off', autocomplete: 'off', spellcheck: 'false',
    placeholder,
    'aria-label': 'Message',
    oninput: () => { err.hidden = true; },
  });
  const form = el('form', {
    class: 'cmd',
    onsubmit: (e) => {
      e.preventDefault();
      const ok = handleCommand(input.value, currentRow, showError);
      if (ok) input.value = '';
      input.focus();
    },
  },
    el('span', { class: 'cmd-prompt mono', 'aria-hidden': 'true' }, '>'),
    input,
    el('button', { class: 'cmd-send', type: 'submit', 'aria-label': 'Send' }, '↑'),
  );
  return el('div', { class: 'cmd-wrap' }, form, err);
}

// Incremental feed: keyed by msg_id, autoscroll only when already at bottom,
// pending bubbles reconcile in place. Never a whole-feed markup wipe per message.
function feedController(feedEl, renderMsg, emptyNode) {
  const byId = new Map();
  let emptyShown = null;

  const nearBottom = () =>
    feedEl.scrollHeight - feedEl.scrollTop - feedEl.clientHeight < 60;
  const toBottom = () => { feedEl.scrollTop = feedEl.scrollHeight; };

  function showEmpty() {
    if (emptyShown) return;
    emptyShown = emptyNode();
    feedEl.append(emptyShown);
  }
  function hideEmpty() {
    if (emptyShown) { emptyShown.remove(); emptyShown = null; }
  }

  function append(msg, stickOverride) {
    hideEmpty();
    const node = renderMsg(msg);
    if (msg.msg_id) byId.set(msg.msg_id, node);
    const stick = stickOverride !== undefined ? stickOverride : (nearBottom() || msg.from === 'operator');
    feedEl.append(node);
    if (stick) toBottom();
  }

  function update(msg) {
    const old = msg.msg_id ? byId.get(msg.msg_id) : null;
    if (!old) { append(msg); return; }
    const fresh = renderMsg(msg);
    old.replaceWith(fresh);
    byId.set(msg.msg_id, fresh);
  }

  function reset(list) {
    feedEl.textContent = '';
    byId.clear();
    emptyShown = null;
    if (!list.length) { showEmpty(); return; }
    for (const m of list) append(m, false);
    toBottom();
  }

  return { append, update, reset };
}

function renderChatMsg(msg, uid) {
  const isOp = msg.from === 'operator';
  let status = '';
  if (msg.failed) status = `✕ ${msg.error || 'send failed'} — tap to retry`;
  else if (msg.pending) status = '◷ sending';
  else if (msg.tier) status = ACK_COPY[msg.tier] || msg.tier;
  const cls = ['msg', isOp && 'msg--op', msg.pending && 'msg--pending', msg.failed && 'msg--failed']
    .filter(Boolean).join(' ');
  const node = el('article', { class: cls },
    el('div', { class: 'msg-meta' },
      el('span', { class: 'msg-from' }, msg.from || '?'),
      relSpan(msg.ts, 'msg-time'),
      status ? el('span', { class: 'msg-status' }, status) : null,
    ),
    el('div', { class: 'msg-text' }, msg.text || ''),
  );
  if (msg.failed) node.addEventListener('click', () => retrySend(msg, uid));
  return node;
}

// ---------------------------------------------------------------- view: NOW

function healthVerdict(h) {
  if (!h) return { tone: 'dim', title: 'measuring…', sub: 'waiting for api/health' };
  if (!h.auth_configured) {
    return { tone: 'err', title: 'hub locked', sub: 'FLEET_HUB_TOKEN not set on host' };
  }
  const broker = h.broker || {};
  if (!broker.connected) {
    return { tone: 'err', title: 'broker down', sub: `NATS unreachable · ${broker.error || 'no connection'}` };
  }
  if (h.monitor && h.monitor.ok === false) {
    return { tone: 'warn', title: 'monitor degraded', sub: h.monitor.error || 'NATS monitor endpoint unreachable' };
  }
  if (h.replay && h.replay.ok === false) {
    return { tone: 'warn', title: 'replay failed', sub: h.replay.error || 'history replay failed — presence may lag' };
  }
  const ps = h.presence_summary || {};
  const fresh = ps.fresh || 0;
  const total = (ps.fresh || 0) + (ps.recent || 0) + (ps.stale || 0) + (ps.never || 0) || 3;
  return { tone: 'ok', title: 'all quiet', sub: `${fresh}/${total} agents fresh · broker live` };
}

function statTile(label, num, sub) {
  return el('div', { class: 'tile' },
    el('div', { class: 'eyebrow' }, label),
    el('div', { class: 'tile-num' }, num),
    el('div', { class: 'tile-sub' }, sub),
  );
}

function notifControls() {
  const wrap = el('div', { class: 'notif-row' });
  if (!('Notification' in window)) {
    wrap.append(el('span', { class: 'notif-state' }, 'notifications unavailable in this browser'));
    return wrap;
  }
  const paint = () => {
    wrap.textContent = '';
    if (Notification.permission === 'granted') {
      wrap.append(el('span', { class: 'notif-state' }, 'notifications on · badge when backgrounded'));
    } else if (Notification.permission === 'denied') {
      wrap.append(el('span', { class: 'notif-state' }, 'notifications blocked — enable in Safari settings'));
    } else {
      wrap.append(el('button', {
        class: 'ghost-btn',
        onclick: async () => { try { await Notification.requestPermission(); } catch { /* dismissed */ } paint(); },
      }, 'enable notifications'));
    }
  };
  paint();
  return wrap;
}

function viewNow(root) {
  root.classList.add('view-pad');
  const offs = [];

  const strip = el('section', { class: 'strip', role: 'status' });
  const tiles = el('section', { class: 'tiles', 'aria-label': 'Heartbeat' });
  const ventures = el('section', { 'aria-label': 'Ventures' });
  const northStar = el('p', { class: 'north-star' });

  function paintHealth() {
    const h = store.state.health;
    const v = healthVerdict(h);
    strip.className = `strip strip--${v.tone}`;
    strip.textContent = '';
    strip.append(
      el('div', { class: 'strip-title' }, v.title),
      el('div', { class: 'strip-sub' }, v.sub),
    );

    tiles.textContent = '';
    const ps = (h && h.presence_summary) || {};
    const fresh = ps.fresh || 0;
    const total = (ps.fresh || 0) + (ps.recent || 0) + (ps.stale || 0) + (ps.never || 0);
    const broker = (h && h.broker) || {};
    const mon = (h && h.monitor) || {};
    tiles.append(
      statTile('agents', h ? `${fresh}/${total || '—'}` : '—', 'heard ≤5m'),
      statTile('broker', h ? fmtNum(broker.messages) : '—',
        broker.last_seq !== null && broker.last_seq !== undefined ? `seq ${fmtNum(broker.last_seq)}` : 'no stream info'),
      statTile('monitor', h && mon.ok ? fmtNum(mon.connections) : '—',
        h && mon.ok ? `nats ${mon.server_version || '?'}` : 'unreachable'),
    );
  }

  function paintVision() {
    const vis = store.state.vision;
    ventures.textContent = '';
    northStar.textContent = (vis && vis.north_star) || '';
    if (!vis) {
      ventures.append(el('div', { class: 'empty skel' }, 'loading vision…'));
      return;
    }
    const list = vis.ventures || [];
    if (!list.length) {
      ventures.append(el('div', { class: 'empty' }, 'No ventures declared yet — vision.json is empty.'));
      return;
    }
    for (const v of list) {
      const missions = v.missions || [];
      ventures.append(el('div', { class: 'venture' },
        el('div', { class: 'venture-name' }, v.name || v.id || '?'),
        el('div', { class: 'venture-line' }, v.line || ''),
        el('div', { class: 'venture-move' },
          missions.length ? `${missions.length} mission${missions.length === 1 ? '' : 's'}` : 'no receipted movement yet'),
      ));
    }
  }

  root.append(
    strip,
    sectionEyebrow('Heartbeat'),
    tiles,
    sectionEyebrow('Needs John'),
    el('section', { class: 'needs-john' },
      el('p', {}, 'No decision queue yet — foreman arrives next arc.'),
    ),
    sectionEyebrow('Ventures'),
    northStar,
    ventures,
    notifControls(),
    provenance(`${BASE}api/health`, `${BASE}api/vision`, 'stream DHARMA_A2A'),
  );

  paintHealth();
  paintVision();
  offs.push(store.on('health', paintHealth));
  offs.push(store.on('vision', paintVision));
  return () => offs.forEach((f) => f());
}

// ---------------------------------------------------------------- view: TALK

function dmStrip(currentUid) {
  const strip = el('div', { class: 'dm-strip', role: 'tablist', 'aria-label': 'Channels' });
  strip.append(el('a', {
    class: 'dm-chip' + (!currentUid ? ' on' : ''), href: '#/talk',
  }, el('span', { class: 'dot dot--fresh', 'aria-hidden': 'true', style: 'background:var(--gold)' }), 'group'));
  for (const r of activeAgents()) {
    strip.append(el('a', {
      class: 'dm-chip' + (currentUid === r.uid ? ' on' : ''),
      href: '#/agent/' + encodeURIComponent(r.uid),
    }, freshnessDot(r), r.display_name || r.callsign || r.uid));
  }
  return strip;
}

function viewTalk(root) {
  root.classList.add('view-pinned');
  const offs = [];

  const feed = el('div', { class: 'feed', 'aria-label': 'Fleet chat' });
  const ctl = feedController(feed, (m) => renderChatMsg(m, null), () =>
    el('div', { class: 'empty' },
      'No fleet chat in the replay window.',
      el('span', { class: 'mono' }, 'sends publish to dharma.fleet.chat — bridges may be quiet'),
    ));

  let strip = dmStrip(null);
  root.append(
    strip,
    feed,
    commandBar({ placeholder: 'message the fleet — /dm <callsign> to whisper', currentRow: null }),
    provenance(`${BASE}api/chat`, `sse ${BASE}events/stream`, 'subject dharma.fleet.chat'),
  );

  ctl.reset(store.state.chat);
  requestAnimationFrame(() => { feed.scrollTop = feed.scrollHeight; });

  offs.push(store.on('chat', (ev) => {
    if (ev.kind === 'append') ctl.append(ev.msg);
    else if (ev.kind === 'update') ctl.update(ev.msg);
    else ctl.reset(store.state.chat);
  }));
  offs.push(store.on('roster', () => {
    const fresh = dmStrip(null);
    strip.replaceWith(fresh);
    strip = fresh;
  }));
  return () => offs.forEach((f) => f());
}

// ---------------------------------------------------------------- view: AGENT (DM)

function viewAgent(root, params) {
  root.classList.add('view-pinned');
  const offs = [];
  const uid = params.uid;
  const row = store.state.roster.find((r) => r.uid === uid);

  if (!row) {
    root.classList.remove('view-pinned');
    root.classList.add('view-pad');
    root.append(
      el('div', { class: 'empty' },
        'Unknown agent.',
        el('span', { class: 'mono' }, uid),
      ),
      provenance(`${BASE}api/agent/${uid}`),
    );
    return () => {};
  }

  const head = el('div', { class: 'agent-head' },
    el('div', { class: 'agent-head-top' },
      el('a', { class: 'agent-head-back', href: '#/talk', 'aria-label': 'Back to Talk' }, '‹ talk'),
      freshnessDot(row),
      el('span', { class: 'agent-head-name' }, row.display_name || row.uid),
    ),
    el('div', { class: 'agent-head-meta mono' },
      twoSignalLine(row), ' · ', row.subject || 'no subject',
    ),
    el('div', { class: 'agent-head-meta mono' },
      [row.model, row.host, row.seat === 'archived' ? 'archived seat' : null]
        .filter(Boolean).join(' · '),
    ),
  );

  const feed = el('div', { class: 'feed', 'aria-label': 'Direct messages' });
  const ctl = feedController(feed, (m) => renderChatMsg(m, uid), () =>
    el('div', { class: 'empty' },
      'No direct messages yet.',
      el('span', { class: 'mono' }, `bridge may not be draining ${row.subject || 'this subject'}`),
    ));

  root.append(
    head,
    feed,
    commandBar({ placeholder: `dm ${row.callsign || row.uid}`, currentRow: row }),
    provenance(`${BASE}api/dm/${uid}`, `subject ${row.subject || '?'}`),
  );

  ctl.reset(dmList(uid));
  requestAnimationFrame(() => { feed.scrollTop = feed.scrollHeight; });

  // Backfill server-side DM history (survives restarts via replay).
  api(`api/dm/${encodeURIComponent(uid)}`)
    .then((d) => replaceDms(uid, d.messages || []))
    .catch(() => { /* honest empty state stands */ });

  offs.push(store.on('dm:' + uid, (ev) => {
    if (ev.kind === 'append') ctl.append(ev.msg);
    else if (ev.kind === 'update') ctl.update(ev.msg);
    else ctl.reset(dmList(uid));
  }));
  offs.push(store.on('roster', () => {
    const fresh = store.state.roster.find((r) => r.uid === uid);
    if (!fresh) return;
    const meta = head.querySelector('.agent-head-meta');
    meta.textContent = '';
    appendChildren(meta, [twoSignalLine(fresh), ' · ', fresh.subject || 'no subject']);
    const dot = head.querySelector('.dot');
    if (dot) dot.replaceWith(freshnessDot(fresh));
  }));
  return () => offs.forEach((f) => f());
}

// ---------------------------------------------------------------- view: FLEET

function viewFleet(root) {
  root.classList.add('view-pad');
  const offs = [];

  const agents = el('section', { 'aria-label': 'Agents' });
  const nodesCard = el('section', { class: 'card', 'aria-label': 'Nodes' });

  function paintAgents() {
    agents.textContent = '';
    const active = activeAgents();
    if (!active.length) {
      agents.append(el('div', { class: 'empty' },
        'No active seats.',
        el('span', { class: 'mono' }, 'roster empty or all seats archived'),
      ));
    }
    for (const r of active) {
      agents.append(el('a', {
        class: 'agent-row', href: '#/agent/' + encodeURIComponent(r.uid),
      },
        freshnessDot(r),
        el('span', { class: 'agent-row-main' },
          el('span', { class: 'agent-row-name' }, r.display_name || r.uid),
          el('br'),
          el('span', { class: 'agent-row-call' }, r.callsign || r.uid),
        ),
        el('span', { class: 'agent-row-sig' },
          'heard ', relSpan(r.last_heard),
          el('br'),
          'addressed ', relSpan(r.last_addressed),
        ),
      ));
    }
    agents.append(el('p', { class: 'legend' },
      el('span', { class: 'dot dot--fresh' }), ' heard ≤5m · ',
      el('span', { class: 'dot dot--recent' }), ' ≤2h · ',
      el('span', { class: 'dot dot--stale' }), ' >2h · ',
      el('span', { class: 'dot dot--never' }), ' never heard',
      el('br'),
      'heard = agent spoke · addressed = traffic to its subject',
    ));

    const archived = archivedAgents();
    if (archived.length) {
      const details = el('details', { class: 'archived' },
        el('summary', {}, `${archived.length} archived seat${archived.length === 1 ? '' : 's'} awaiting proof-of-life`),
      );
      for (const r of archived) {
        details.append(el('div', { class: 'archived-row' },
          freshnessDot(r),
          el('span', {}, r.display_name || r.uid),
          el('span', { class: 'mono' }, r.callsign || r.uid),
        ));
      }
      agents.append(details);
    }
  }

  function paintNodes() {
    nodesCard.textContent = '';
    const n = store.state.nodes;
    if (!n) {
      nodesCard.append(el('div', { class: 'skel' }, 'loading nodes…'));
      return;
    }
    const list = n.nodes || [];
    if (!list.length) {
      nodesCard.append(el('div', { class: 'skel' }, 'no nodes declared'));
      return;
    }
    for (const node of list) {
      nodesCard.append(el('div', { class: 'node-row' },
        el('span', { class: 'node-label' }, node.label || node.id || '?'),
        el('span', { class: 'node-role' }, node.role || ''),
        el('span', { class: 'node-ips' },
          (node.tailscale || '—'), el('br'), (node.public || '—'),
        ),
      ));
    }
  }

  root.append(
    sectionEyebrow('Agents — two-signal liveness'),
    agents,
    sectionEyebrow('Nodes'),
    nodesCard,
    provenance(`${BASE}api/presence`, `${BASE}api/nodes`, 'stream DHARMA_A2A'),
  );

  paintAgents();
  paintNodes();
  if (!store.state.nodes) {
    api('api/nodes')
      .then((n) => { store.state.nodes = n; store.emit('nodes'); })
      .catch(() => { /* skeleton copy stands */ });
  }
  offs.push(store.on('roster', paintAgents));
  offs.push(store.on('nodes', paintNodes));
  return () => offs.forEach((f) => f());
}

// ---------------------------------------------------------------- view: FLOW

function renderRawLine(frame) {
  const line = el('div', { class: 'raw-line' },
    el('span', { class: 'raw-n' }, `#${frame.n ?? '—'}`), ' ',
    el('span', { class: 'raw-subject' }, frame.subject || '?'), ' ',
    el('span', {}, frame.preview || ''), ' ',
    relSpan(frame.ts),
  );
  line.dataset.hay = `${frame.subject || ''} ${frame.preview || ''}`.toLowerCase();
  return line;
}

function viewFlow(root) {
  root.classList.add('view-pinned');
  const offs = [];
  let query = '';

  const feed = el('div', { class: 'flow-feed', 'aria-label': 'Raw frame feed' });
  let emptyNode = null;

  const showEmpty = () => {
    if (emptyNode) return;
    emptyNode = el('div', { class: 'empty', style: 'margin:10px 14px' },
      'No frames yet.',
      el('span', { class: 'mono' }, 'sse live — waiting for A2A traffic since page open'),
    );
    feed.append(emptyNode);
  };
  const hideEmpty = () => { if (emptyNode) { emptyNode.remove(); emptyNode = null; } };

  const applyFilter = () => {
    for (const line of feed.querySelectorAll('.raw-line')) {
      line.classList.toggle('filtered', Boolean(query) && !line.dataset.hay.includes(query));
    }
  };

  const filter = el('input', {
    class: 'flow-filter', type: 'search', placeholder: 'filter subjects + previews',
    autocapitalize: 'off', autocomplete: 'off', spellcheck: 'false',
    'aria-label': 'Filter frames',
    oninput: (e) => { query = e.target.value.trim().toLowerCase(); applyFilter(); },
  });

  const nearBottom = () => feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;

  function append(frame) {
    hideEmpty();
    const stick = nearBottom();
    const line = renderRawLine(frame);
    if (query && !line.dataset.hay.includes(query)) line.classList.add('filtered');
    feed.append(line);
    while (feed.querySelectorAll('.raw-line').length > 400) {
      const first = feed.querySelector('.raw-line');
      if (!first) break;
      first.remove();
    }
    if (stick) feed.scrollTop = feed.scrollHeight;
  }

  root.append(
    el('div', { class: 'flow-filter-wrap' }, filter),
    feed,
    provenance(`sse ${BASE}events/stream`, 'raw frames', 'cap 400'),
  );

  if (!store.state.raw.length) showEmpty();
  else {
    for (const f of store.state.raw) {
      const line = renderRawLine(f);
      feed.append(line);
    }
    feed.scrollTop = feed.scrollHeight;
  }

  offs.push(store.on('raw', append));
  return () => offs.forEach((f) => f());
}

// ---------------------------------------------------------------- router

const VIEWS = { now: viewNow, talk: viewTalk, fleet: viewFleet, flow: viewFlow, agent: viewAgent };
let currentCleanup = null;

function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  const [name, rest] = h.split('/');
  if (name === 'agent' && rest) return { name: 'agent', uid: decodeURIComponent(rest) };
  if (Object.prototype.hasOwnProperty.call(VIEWS, name) && name !== 'agent') return { name, uid: null };
  return { name: 'now', uid: null };
}

function mountView() {
  const route = parseHash();
  store.state.route = route;
  if (currentCleanup) { currentCleanup(); currentCleanup = null; }
  const view = document.getElementById('view');
  view.textContent = '';
  view.className = '';
  currentCleanup = VIEWS[route.name](view, route) || null;
  document.querySelectorAll('#tabs .tab').forEach((t) => {
    const v = t.dataset.view;
    t.classList.toggle('on', v === route.name || (route.name === 'agent' && v === 'talk'));
  });
  view.scrollTop = 0;
}

// ---------------------------------------------------------------- gate / auth

const gateEl = document.getElementById('gate');
const appEl = document.getElementById('app');
const gateBanner = document.getElementById('gate-banner');
const gateErr = document.getElementById('gate-err');
const gateForm = document.getElementById('gate-form');
const gateToken = document.getElementById('gate-token');
const gateBtn = document.getElementById('gate-btn');

function showGate(banner) {
  store.state.authenticated = false;
  sse.close();
  setConn('off');
  appEl.hidden = true;
  gateEl.hidden = false;
  if (banner) {
    gateBanner.textContent = banner;
    gateBanner.hidden = false;
  } else {
    gateBanner.hidden = true;
  }
}

function onUnauthorized() {
  if (!store.state.authenticated) return;
  showGate();
}

async function tryLogin(token) {
  const res = await fetch(BASE + 'login', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  });
  if (res.ok) return { ok: true };
  let detail = 'login failed';
  try {
    const j = await res.json();
    if (j && j.detail) detail = String(j.detail);
  } catch { /* non-JSON body */ }
  return { ok: false, detail };
}

gateForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const token = gateToken.value.trim();
  if (!token) return;
  gateErr.textContent = '';
  gateBtn.disabled = true;
  try {
    const r = await tryLogin(token);
    if (r.ok) {
      gateToken.value = '';
      await enterApp();
    } else {
      gateErr.textContent = r.detail;
    }
  } catch {
    gateErr.textContent = 'hub unreachable — check the network';
  } finally {
    gateBtn.disabled = false;
  }
});

// ---------------------------------------------------------------- boot

async function refetchCore() {
  const results = await Promise.allSettled([
    api('api/chat'),
    api('api/roster?include=archived'),
    api('api/health'),
  ]);
  if (results[0].status === 'fulfilled') replaceChat(results[0].value.messages || []);
  if (results[1].status === 'fulfilled') { store.state.roster = results[1].value.agents || []; store.emit('roster'); }
  if (results[2].status === 'fulfilled') { store.state.health = results[2].value; store.emit('health'); }
}

async function enterApp() {
  store.state.authenticated = true;
  gateEl.hidden = true;
  appEl.hidden = false;

  const results = await Promise.allSettled([
    api('api/roster?include=archived'),
    api('api/chat'),
    api('api/health'),
    api('api/vision'),
  ]);
  if (results[0].status === 'fulfilled') { store.state.roster = results[0].value.agents || []; store.emit('roster'); }
  if (results[1].status === 'fulfilled') replaceChat(results[1].value.messages || []);
  if (results[2].status === 'fulfilled') { store.state.health = results[2].value; store.emit('health'); }
  if (results[3].status === 'fulfilled') { store.state.vision = results[3].value; store.emit('vision'); }

  if (store.state.authenticated) {
    sse.open();
    mountView();
  }
}

async function boot() {
  startTicker();
  window.addEventListener('hashchange', () => { if (store.state.authenticated) mountView(); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    clearUnseen();
    if (store.state.authenticated && sse.closed()) {
      sse.open();
      refetchCore();
    }
  });

  let session;
  try {
    session = await api('api/session');
  } catch {
    showGate('hub unreachable — server not responding');
    return;
  }

  if (!session.auth_configured) {
    showGate('FLEET_HUB_TOKEN not set on host — hub locked');
    return;
  }

  if (session.authenticated) {
    await enterApp();
    return;
  }

  // Legacy v0.5 migration: raw token in localStorage → one login attempt, then gone forever.
  const legacy = localStorage.getItem('fleet_token');
  if (legacy) {
    localStorage.removeItem('fleet_token');
    try {
      const r = await tryLogin(legacy);
      if (r.ok) { await enterApp(); return; }
    } catch { /* fall through to gate */ }
  }
  showGate();
}

boot();
