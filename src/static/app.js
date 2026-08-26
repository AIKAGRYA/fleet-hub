/* Fleet Hub v1 — dependency-free, phone-first, evidence-aware operator shell. */
'use strict';

// Runtime requests follow the document directory. Install metadata is fixed at
// /fleet/; serving the same shell at / is a local-development convenience.
const BASE = location.pathname.replace(/[^/]*$/, '');
const TRACE_LIMIT = 200;
const REQUEST_TIMEOUT_MS = 9000;
const PAGE_LIMIT = 25;
const HASH_ID_LIMIT = 128;
const TITLES = {
  helm: 'Helm',
  chat: 'Chat',
  board: 'Board',
  trace: 'Trace',
  roster: 'Roster',
  agent: 'Chat',
};
const ACK_COPY = {
  PUBLISH_ACCEPTED: 'stored by broker; processing unproven',
  DELIVERED_TO_CONSUMER: 'delivered to a transport consumer',
  HANDLER_ACKED: 'handler acknowledged',
  DOMAIN_RECEIPTED: 'domain receipt recorded',
  NO_ACK: 'published without a persistence acknowledgement',
};
const BOARD_LANES = [
  { id: 'queue', label: 'Queue', states: ['pending'] },
  { id: 'assigned', label: 'Assigned', states: ['assigned'] },
  { id: 'running', label: 'Running', states: ['running'] },
  { id: 'review', label: 'Review', states: ['completed'] },
  { id: 'done', label: 'Done', states: [] },
  { id: 'failed', label: 'Failed', states: ['failed'] },
  { id: 'cancelled', label: 'Cancelled', states: ['cancelled'] },
  { id: 'quarantined', label: 'Quarantined', states: ['quarantined_fake_result'] },
  { id: 'unmapped', label: 'Unmapped', states: [] },
];

// ---------------------------------------------------------------- safe DOM

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'dataset') {
      for (const [dataKey, dataValue] of Object.entries(value)) node.dataset[dataKey] = dataValue;
    } else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2), value);
    } else if (key === 'disabled') {
      node.disabled = Boolean(value);
    } else {
      node.setAttribute(key, value === true ? '' : String(value));
    }
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === '') continue;
    if (Array.isArray(child)) append(node, child);
    else node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

function boundedText(value, limit = 1024) {
  const text = String(value ?? '');
  return text.length <= limit ? text : `${text.slice(0, limit)}…`;
}

// Defense in depth for trace previews. The backend owns authoritative redaction.
function redactPreview(value) {
  let text;
  if (typeof value === 'string') text = value;
  else {
    try { text = JSON.stringify(value); } catch { text = '[unrenderable payload]'; }
  }
  return boundedText(text, 320)
    .replace(/((?:authorization|cookie|password|secret|token|api[_-]?key)\s*[=:]\s*)[^\s,;}]+/gi, '$1[REDACTED]')
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer [REDACTED]');
}

// ---------------------------------------------------------------- time and identity

function normTs(value) {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'number' && Number.isFinite(value)) return new Date(value * 1000).toISOString();
  return String(value);
}

function relTime(value) {
  const ts = normTs(value);
  if (!ts) return 'never';
  const parsed = Date.parse(ts);
  if (!Number.isFinite(parsed)) return 'unknown';
  const seconds = Math.max(0, (Date.now() - parsed) / 1000);
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

function relSpan(value, cls = '') {
  const ts = normTs(value);
  return el('span', { class: cls, dataset: ts ? { ts } : null }, relTime(ts));
}

function startTicker() {
  setInterval(() => {
    document.querySelectorAll('[data-ts]').forEach((node) => {
      node.textContent = relTime(node.dataset.ts);
    });
  }, 30000);
}

function newId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

// ---------------------------------------------------------------- state

const state = {
  authenticated: false,
  csrfToken: null,
  authMode: null,
  sessionRevision: 0,
  snapshotRevision: 0,
  route: { name: 'helm', id: null },
  conn: 'offline',
  streamEpoch: null,
  lastStreamEventAt: null,
  health: null,
  vision: null,
  roster: [],
  nodes: null,
  chat: [],
  dms: new Map(),
  trace: [],
  drafts: new Map(),
  bootstrap: null,
  catalog: {
    available: false,
    discovery_complete: false,
    commands_available: false,
    commands: [],
    missions: [],
    configured_mission_ids: [],
    authority: null,
    total_configured_visible: null,
    next_cursor: null,
    source_version: null,
    error_code: 'not_loaded',
  },
  snapshots: new Map(),
  needs: {
    available: false,
    items: [],
    observed_at: null,
    process_local: true,
    mission_id: null,
    total: null,
    next_cursor: null,
    source_authority: null,
    source_version: null,
    commands_available: false,
    commands: [],
    error_code: 'not_loaded',
  },
};

const subs = new Map();
function on(topic, handler) {
  if (!subs.has(topic)) subs.set(topic, new Set());
  subs.get(topic).add(handler);
  return () => subs.get(topic).delete(handler);
}
function emit(topic, value) {
  for (const handler of [...(subs.get(topic) || [])]) {
    try { handler(value); } catch (error) { console.error(error); }
  }
}

const chatIndex = new Map();
const dmIndexes = new Map();
let optimisticOperatorMessages = new WeakSet();
function dmList(uid) {
  if (!state.dms.has(uid)) state.dms.set(uid, []);
  return state.dms.get(uid);
}
function dmIndex(uid) {
  if (!dmIndexes.has(uid)) dmIndexes.set(uid, new Map());
  return dmIndexes.get(uid);
}

function applyChat(message) {
  const known = message.msg_id ? chatIndex.get(message.msg_id) : null;
  if (known) {
    Object.assign(known, message, { pending: false, failed: false });
    emit('chat', { kind: 'update', message: known });
    return;
  }
  if (message.msg_id) chatIndex.set(message.msg_id, message);
  state.chat.push(message);
  if (state.chat.length > 500) state.chat.splice(0, state.chat.length - 500);
  emit('chat', { kind: 'append', message });
}

function applyDm(uid, message) {
  const index = dmIndex(uid);
  const known = message.msg_id ? index.get(message.msg_id) : null;
  if (known) {
    Object.assign(known, message, { pending: false, failed: false });
    emit(`dm:${uid}`, { kind: 'update', message: known });
    return;
  }
  if (message.msg_id) index.set(message.msg_id, message);
  dmList(uid).push(message);
  if (dmList(uid).length > 500) dmList(uid).splice(0, dmList(uid).length - 500);
  emit(`dm:${uid}`, { kind: 'append', message });
}

function replaceChat(messages) {
  const local = state.chat.filter((message) => (
    message.pending || message.failed || optimisticOperatorMessages.has(message)
  ));
  chatIndex.clear();
  state.chat = [];
  for (const message of Array.isArray(messages) ? messages : []) {
    if (message.msg_id) chatIndex.set(message.msg_id, message);
    state.chat.push(message);
  }
  for (const message of local) {
    if (message.msg_id && chatIndex.has(message.msg_id)) continue;
    if (message.msg_id) chatIndex.set(message.msg_id, message);
    state.chat.push(message);
  }
  emit('chat', { kind: 'reset' });
}

function replaceDms(uid, messages) {
  const local = dmList(uid).filter((message) => (
    message.pending || message.failed || optimisticOperatorMessages.has(message)
  ));
  const index = dmIndex(uid);
  index.clear();
  state.dms.set(uid, []);
  for (const message of Array.isArray(messages) ? messages : []) {
    if (message.msg_id) index.set(message.msg_id, message);
    dmList(uid).push(message);
  }
  for (const message of local) {
    if (message.msg_id && index.has(message.msg_id)) continue;
    if (message.msg_id) index.set(message.msg_id, message);
    dmList(uid).push(message);
  }
  emit(`dm:${uid}`, { kind: 'reset' });
}

function pushTrace(frame) {
  const safe = {
    n: frame && frame.n,
    ts: normTs(frame && (frame.ts || frame.observed_at)) || new Date().toISOString(),
    subject: boundedText(frame && (frame.subject || frame.type || frame.event) || 'unknown', 160),
    preview: redactPreview(frame && (frame.preview ?? frame.payload ?? frame.data ?? '')),
    tier: boundedText(frame && (frame.tier || frame.ack_tier) || '', 80),
  };
  state.trace.push(safe);
  if (state.trace.length > TRACE_LIMIT) state.trace.splice(0, state.trace.length - TRACE_LIMIT);
  emit('trace', safe);
}

function mergePresence(update) {
  if (!update || !update.uid) return;
  const row = state.roster.find((agent) => agent.uid === update.uid);
  if (!row) return;
  row.last_heard = normTs(update.last_heard) || row.last_heard;
  row.last_addressed = normTs(update.last_addressed) || row.last_addressed;
  if (update.freshness) row.freshness = update.freshness;
  if (update.contact) row.contact = update.contact;
  if (update.signals && typeof update.signals === 'object') {
    row.signals = { ...(row.signals || {}) };
    for (const name of ['last_heard', 'last_addressed']) {
      const signal = update.signals[name];
      if (!signal || typeof signal !== 'object') continue;
      row.signals[name] = { ...(row.signals[name] || {}), ...signal };
      if (signal.value) row[name] = normTs(signal.value) || row[name];
    }
  }
  emit('roster');
}

function activeAgents() {
  return state.roster.filter((row) => row.seat !== 'archived');
}
function findAgent(key) {
  const clean = String(key || '').toLowerCase();
  return state.roster.find((row) => [row.uid, row.callsign, row.display_name]
    .some((value) => String(value || '').toLowerCase() === clean)) || null;
}

// ---------------------------------------------------------------- API client

class ApiProblem extends Error {
  constructor(message, status = 0, code = 'request_failed') {
    super(message);
    this.name = 'ApiProblem';
    this.status = status;
    this.code = code;
  }
}

function userError(status, detail) {
  const safeDetail = boundedText(detail || '', 180);
  if (status === 401) return 'Session expired. Unlock Fleet Hub again.';
  if (status === 403) return 'Forbidden: session authority or request verification failed.';
  if (status === 409) return 'State changed at the owner. Refresh before trying again.';
  if (status === 422) return safeDetail || 'The owner did not accept this request.';
  if (status === 404 || status === 501 || status === 503) return 'Capability unavailable on this hub.';
  if (status >= 500) return 'Hub service unavailable. No effect is claimed.';
  return safeDetail || `Request failed (${status || 'network'}).`;
}

const activeRequests = new Set();

function abortActiveRequests() {
  for (const controller of [...activeRequests]) controller.abort();
  activeRequests.clear();
}

function sameOriginUrl(path) {
  const clean = String(path || '').replace(/^\/+/, '');
  if (!clean || clean.includes('://') || clean.startsWith('\\')) throw new ApiProblem('Invalid API path.');
  const url = new URL(BASE + clean, location.origin);
  if (url.origin !== location.origin) throw new ApiProblem('Cross-origin API requests are blocked.');
  return url;
}

async function api(path, options = {}) {
  const method = String(options.method || 'GET').toUpperCase();
  const mutating = !['GET', 'HEAD', 'OPTIONS'].includes(method);
  const headers = new Headers({ Accept: 'application/json' });
  if (options.body !== undefined) headers.set('Content-Type', 'application/json');
  if (mutating && !options.skipCsrf) {
    if (!state.csrfToken) throw new ApiProblem('Request verification unavailable.', 403, 'csrf_unavailable');
    headers.set('X-CSRF-Token', state.csrfToken);
  }
  if (options.idempotencyKey) headers.set('Idempotency-Key', options.idempotencyKey);
  if (options.version) headers.set('If-Match', options.version);

  const controller = new AbortController();
  let timedOut = false;
  const timeout = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, options.timeout || REQUEST_TIMEOUT_MS);
  const externalAbort = () => controller.abort();
  if (options.signal) {
    if (options.signal.aborted) controller.abort();
    else options.signal.addEventListener('abort', externalAbort, { once: true });
  }
  activeRequests.add(controller);
  try {
    const response = await fetch(sameOriginUrl(path), {
      method,
      credentials: 'same-origin',
      cache: 'no-store',
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
    });

    let payload = null;
    if (response.status !== 204) {
      try {
        // Keep the deadline armed while consuming the response body too.
        payload = await response.json();
      } catch (error) {
        if (error && error.name === 'AbortError') throw error;
        payload = null;
      }
    }
    if (!response.ok) {
      const errorBody = payload && payload.error && typeof payload.error === 'object'
        ? payload.error : null;
      const detail = errorBody && errorBody.message
        || payload && (payload.detail || payload.message)
        || '';
      const code = errorBody && errorBody.code
        || payload && (payload.error_code || payload.code)
        || `http_${response.status}`;
      if (response.status === 401 && state.authenticated) {
        showGate('Session expired. Unlock Fleet Hub again.');
      }
      throw new ApiProblem(userError(response.status, detail), response.status, code);
    }
    return payload || {};
  } catch (error) {
    if (error instanceof ApiProblem) throw error;
    if (error && error.name === 'AbortError') {
      if (timedOut) throw new ApiProblem('Hub request timed out.', 0, 'timeout');
      throw new ApiProblem('Request superseded by a newer projection.', 0, 'request_superseded');
    }
    throw new ApiProblem('Hub unreachable. No effect is claimed.', 0, 'network');
  } finally {
    // This finally intentionally follows response body consumption.
    clearTimeout(timeout);
    activeRequests.delete(controller);
    if (options.signal) options.signal.removeEventListener('abort', externalAbort);
  }
}

async function optionalApi(path, options = {}) {
  try { return await api(path, options); }
  catch (error) {
    if (error instanceof ApiProblem && [404, 501, 503].includes(error.status)) return null;
    throw error;
  }
}

// ---------------------------------------------------------------- v1 projections

function normalizeCatalog(value) {
  const nested = value && value.missions && !Array.isArray(value.missions)
    ? value.missions : null;
  const raw = value && (
    value.missions_catalog || value.mission_catalog || value.missions_projection || nested || value
  );
  if (!raw || typeof raw !== 'object') return state.catalog;
  const listed = Array.isArray(raw.missions) ? raw.missions : [];
  const capabilities = raw.capabilities && typeof raw.capabilities === 'object'
    ? raw.capabilities : {};
  const total = Number.isInteger(raw.total_configured_visible)
    ? Math.max(0, raw.total_configured_visible) : listed.length;
  return {
    available: raw.available === true,
    discovery_complete: raw.discovery_complete === true,
    commands_available: raw.commands_available === true || capabilities.commands_available === true,
    commands: Array.isArray(raw.commands)
      ? raw.commands.map(String)
      : (Array.isArray(capabilities.commands) ? capabilities.commands.map(String) : []),
    missions: listed,
    configured_mission_ids: Array.isArray(raw.configured_mission_ids) ? raw.configured_mission_ids : [],
    authority: raw.authority || raw.source || null,
    observed_at: raw.observed_at || null,
    source_version: raw.source_version || null,
    total_configured_visible: total,
    next_cursor: typeof raw.next_cursor === 'string' && raw.next_cursor.length <= 512
      ? raw.next_cursor : null,
    error_code: raw.error_code || (raw.available === true ? null : 'provider_unavailable'),
  };
}

function appendCatalogPage(current, value) {
  const page = normalizeCatalog(value);
  if (!page.available) return page;
  const byId = new Map();
  for (const mission of [...current.missions, ...page.missions]) {
    const id = String(mission && mission.mission_id || '');
    if (id) byId.set(id, mission);
  }
  return {
    ...current,
    ...page,
    missions: [...byId.values()],
    configured_mission_ids: page.configured_mission_ids.length
      ? page.configured_mission_ids : current.configured_mission_ids,
  };
}

function normalizeNeeds(value) {
  const raw = value && (value.needs_john || value.needs || value);
  if (!raw || typeof raw !== 'object') return state.needs;
  const capabilities = raw.capabilities && typeof raw.capabilities === 'object'
    ? raw.capabilities : {};
  const listed = Array.isArray(raw.items) ? raw.items : [];
  const total = Number.isInteger(raw.total)
    ? Math.max(0, raw.total)
    : (Number.isInteger(raw.count) ? Math.max(0, raw.count) : listed.length);
  return {
    available: raw.available === true,
    items: listed,
    observed_at: raw.observed_at || null,
    process_local: raw.process_local !== false,
    mission_id: raw.mission_id || null,
    total,
    next_cursor: typeof raw.next_cursor === 'string' && raw.next_cursor.length <= 512
      ? raw.next_cursor : null,
    commands_available: raw.commands_available === true || capabilities.commands_available === true,
    commands: Array.isArray(raw.commands)
      ? raw.commands.map(String)
      : (Array.isArray(capabilities.commands) ? capabilities.commands.map(String) : []),
    source_authority: raw.source_authority || raw.source || null,
    source_version: raw.source_version || null,
    rule_version: raw.rule_version || null,
    error_code: raw.error_code || (raw.available === true ? null : 'provider_unavailable'),
  };
}

function appendNeedsPage(current, value) {
  const page = normalizeNeeds(value);
  if (!page.available) return page;
  const byId = new Map();
  for (const item of [...current.items, ...page.items]) {
    const id = String(item && (item.item_id || item.id) || '');
    if (id) byId.set(id, item);
  }
  return { ...current, ...page, items: [...byId.values()] };
}

function applyBootstrap(payload) {
  if (!payload || typeof payload !== 'object') return;
  state.bootstrap = payload;
  paintEvidenceMode(payload);
  if (payload.missions || payload.mission_catalog || payload.missions_catalog || payload.missions_projection) {
    state.catalog = normalizeCatalog(payload);
  }
  if (payload.needs_john || payload.needs) state.needs = normalizeNeeds(payload);
  emit('catalog');
  emit('needs');
  paintNeedsBadge();
}

function paintEvidenceMode(payload) {
  const banner = document.getElementById('mode-banner');
  const copy = document.getElementById('mode-banner-copy');
  if (!banner || !copy) return;
  const fixture = payload && (
    payload.generated_by_fixture === true || payload.evidence_mode === 'fixture'
  );
  banner.hidden = !fixture;
  if (fixture) {
    const instance = boundedText(payload.source_instance || 'local fixture', 80);
    copy.textContent = `${instance} · no production effect`;
  } else {
    copy.textContent = '';
  }
}

async function refreshV1(
  sessionRevision = state.sessionRevision,
  snapshotRevision = state.snapshotRevision,
) {
  const results = await Promise.allSettled([
    optionalApi('api/v1/bootstrap'),
    optionalApi(`api/v1/missions?limit=${PAGE_LIMIT}`),
    optionalApi(`api/v1/needs-john?limit=${PAGE_LIMIT}`),
  ]);
  if (
    !state.authenticated
    || sessionRevision !== state.sessionRevision
    || snapshotRevision !== state.snapshotRevision
  ) return;
  let catalogApplied = false;
  let needsApplied = false;
  if (results[0].status === 'fulfilled' && results[0].value) {
    applyBootstrap(results[0].value);
    catalogApplied = Boolean(results[0].value.missions);
    needsApplied = Boolean(results[0].value.needs_john || results[0].value.needs);
  }
  if (results[1].status === 'fulfilled' && results[1].value) {
    state.catalog = normalizeCatalog(results[1].value);
    catalogApplied = true;
    emit('catalog');
  }
  if (results[2].status === 'fulfilled' && results[2].value) {
    state.needs = normalizeNeeds(results[2].value);
    needsApplied = true;
    emit('needs');
  }
  if (!catalogApplied) {
    state.catalog = {
      ...state.catalog,
      available: false,
      missions: [],
      total_configured_visible: null,
      next_cursor: null,
      error_code: 'refresh_unavailable',
    };
    emit('catalog');
  }
  if (!needsApplied) {
    state.needs = {
      ...state.needs,
      available: false,
      items: [],
      total: null,
      next_cursor: null,
      error_code: 'refresh_unavailable',
    };
    emit('needs');
  }
  paintNeedsBadge();
}

async function loadMoreMissions() {
  const cursor = state.catalog.next_cursor;
  if (!cursor) return;
  const sessionRevision = state.sessionRevision;
  const snapshotRevision = state.snapshotRevision;
  const page = await api(`api/v1/missions?limit=${PAGE_LIMIT}&cursor=${encodeURIComponent(cursor)}`);
  if (
    !state.authenticated
    || sessionRevision !== state.sessionRevision
    || snapshotRevision !== state.snapshotRevision
  ) return;
  state.catalog = appendCatalogPage(state.catalog, page);
  emit('catalog');
}

async function loadMoreNeeds() {
  const cursor = state.needs.next_cursor;
  if (!cursor) return;
  const sessionRevision = state.sessionRevision;
  const snapshotRevision = state.snapshotRevision;
  const params = new URLSearchParams({ limit: String(PAGE_LIMIT), cursor });
  if (state.needs.mission_id) params.set('mission_id', state.needs.mission_id);
  const page = await api(`api/v1/needs-john?${params.toString()}`);
  if (
    !state.authenticated
    || sessionRevision !== state.sessionRevision
    || snapshotRevision !== state.snapshotRevision
  ) return;
  state.needs = appendNeedsPage(state.needs, page);
  emit('needs');
  paintNeedsBadge();
}

const snapshotRequests = new Map();

function invalidateSnapshots() {
  state.snapshotRevision += 1;
  state.snapshots.clear();
  for (const pending of snapshotRequests.values()) pending.controller.abort();
  snapshotRequests.clear();
  emit('snapshots-reset', state.snapshotRevision);
}

async function loadSnapshot(missionId, force = false) {
  if (!force && state.snapshots.has(missionId)) return state.snapshots.get(missionId);
  const existing = snapshotRequests.get(missionId);
  if (existing && !force) return existing.promise;
  if (existing) existing.controller.abort();

  const controller = new AbortController();
  const snapshotRevision = state.snapshotRevision;
  const sessionRevision = state.sessionRevision;
  const pending = { controller, snapshotRevision, sessionRevision, promise: null };
  pending.promise = (async () => {
    const encoded = encodeURIComponent(missionId);
    let projection = await optionalApi(`api/v1/missions/${encoded}/snapshot`, {
      signal: controller.signal,
    });
    if (!projection) {
      projection = { mission_id: missionId, available: false, error_code: 'provider_unavailable' };
    }
    if (
      !state.authenticated
      || snapshotRevision !== state.snapshotRevision
      || sessionRevision !== state.sessionRevision
    ) {
      throw new ApiProblem('Snapshot was superseded by a newer projection.', 0, 'request_superseded');
    }
    state.snapshots.set(missionId, projection);
    return projection;
  })();
  snapshotRequests.set(missionId, pending);
  try {
    return await pending.promise;
  } finally {
    if (snapshotRequests.get(missionId) === pending) snapshotRequests.delete(missionId);
  }
}

// ---------------------------------------------------------------- one multiplexed event stream

let recoveryPromise = null;
let recoverySequence = 0;
let queuedRecovery = null;
let lastRecoveryKey = null;
let lastRecoveryAt = 0;

function controlData(event) {
  try {
    const parsed = JSON.parse(event.data);
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function scheduleProjectionRecovery(key, reason) {
  if (!state.authenticated) return Promise.resolve();
  const now = Date.now();
  if (key === lastRecoveryKey && now - lastRecoveryAt < 2000) {
    return recoveryPromise || Promise.resolve();
  }
  if (recoveryPromise) {
    queuedRecovery = { key, reason };
    return recoveryPromise;
  }
  lastRecoveryKey = key;
  lastRecoveryAt = now;
  const recoveryId = ++recoverySequence;
  const sessionRevision = state.sessionRevision;
  invalidateSnapshots();
  const currentRecovery = (async () => {
    await refreshAll(reason, { invalidate: false });
    if (!state.authenticated || sessionRevision !== state.sessionRevision) return;
    mountView(false);
    if (!needsRail.hidden) paintNeedsRail();
  })().finally(() => {
    if (recoverySequence !== recoveryId) return;
    recoveryPromise = null;
    const queued = queuedRecovery;
    queuedRecovery = null;
    if (queued && state.authenticated) {
      scheduleProjectionRecovery(queued.key, queued.reason);
    }
  });
  recoveryPromise = currentRecovery;
  return recoveryPromise;
}

const stream = {
  source: null,
  generation: 0,
  open() {
    if (this.source && this.source.readyState !== EventSource.CLOSED) return;
    const source = new EventSource(sameOriginUrl('events/stream'));
    const generation = ++this.generation;
    this.source = source;
    const current = () => (
      state.authenticated && this.source === source && this.generation === generation
    );
    source.onopen = () => { if (current()) setConnection('live'); };
    source.onerror = () => { if (current()) setConnection('reconnecting'); };

    source.addEventListener('hello', (event) => {
      if (!current()) return;
      setConnection('live');
      state.lastStreamEventAt = Date.now();
      try {
        const data = JSON.parse(event.data);
        const changed = state.streamEpoch && data.epoch && data.epoch !== state.streamEpoch;
        state.streamEpoch = data.epoch || state.streamEpoch;
        if (changed) {
          pushTrace({ subject: 'fleet.stream.reset', preview: 'server process epoch changed; projections refetched' });
          scheduleProjectionRecovery(
            `epoch:${boundedText(data.epoch, 80)}`,
            'Server process changed. Refetched bounded projections.',
          );
        }
      } catch { /* malformed control frame is ignored */ }
    });

    const handleReset = (event) => {
      if (!current()) return;
      state.lastStreamEventAt = Date.now();
      const data = controlData(event);
      const reason = boundedText(data.reason || 'cursor_reset', 100);
      pushTrace({ subject: 'fleet.stream.reset', preview: 'replay cursor reset; projections refetched' });
      scheduleProjectionRecovery(
        `reset:${reason}:${event.lastEventId || ''}`,
        'Stream cursor reset. Refetched bounded projections.',
      );
    };
    // reset_required is canonical. reset is a one-release compatibility event;
    // the recovery key prevents the pair from causing two refetches.
    source.addEventListener('reset_required', handleReset);
    source.addEventListener('reset', handleReset);
    source.addEventListener('chat', (event) => {
      if (!current()) return;
      state.lastStreamEventAt = Date.now();
      try { applyChat(JSON.parse(event.data)); } catch { /* malformed event */ }
    });
    source.addEventListener('dm', (event) => {
      if (!current()) return;
      state.lastStreamEventAt = Date.now();
      try {
        const message = JSON.parse(event.data);
        if (message.uid) applyDm(message.uid, message);
      } catch { /* malformed event */ }
    });
    source.addEventListener('raw', (event) => {
      if (!current()) return;
      state.lastStreamEventAt = Date.now();
      try { pushTrace(JSON.parse(event.data)); } catch { /* malformed event */ }
    });
    source.addEventListener('presence', (event) => {
      if (!current()) return;
      state.lastStreamEventAt = Date.now();
      try { mergePresence(JSON.parse(event.data)); } catch { /* malformed event */ }
    });
    source.addEventListener('health', (event) => {
      if (!current()) return;
      state.lastStreamEventAt = Date.now();
      try {
        const update = JSON.parse(event.data);
        if (state.health && state.health.broker) state.health.broker.connected = update.connected;
        emit('health');
      } catch { /* malformed event */ }
    });
    source.addEventListener('mission', (event) => {
      if (!current()) return;
      scheduleProjectionRecovery(
        `mission:${event.lastEventId || ''}`,
        'Mission projection changed. Refetched authoritative reads.',
      );
    });
    source.addEventListener('needs-john', (event) => {
      if (!current()) return;
      scheduleProjectionRecovery(
        `needs-john:${event.lastEventId || ''}`,
        'Decision projection changed. Refetched authoritative reads.',
      );
    });
  },
  close() {
    this.generation += 1;
    if (this.source) this.source.close();
    this.source = null;
  },
};

function setConnection(value) {
  state.conn = value;
  const dot = document.getElementById('conn-dot');
  const label = document.getElementById('conn-label');
  if (!dot || !label) return;
  dot.className = `conn-dot conn-dot--${value}`;
  const labels = {
    live: 'browser live',
    reconnecting: 'reconnecting',
    stale: 'stream stale',
    offline: 'offline',
  };
  label.textContent = labels[value] || value;
  emit('connection', value);
}

// ---------------------------------------------------------------- chat send and drafts

function draftKey(row) { return row ? `dm:${row.uid}` : 'group'; }

async function postChat(message, uid) {
  const body = { text: message.text, to: message.to, msg_id: message.msg_id };
  try {
    const result = await api('api/v1/intents/chat', {
      method: 'POST', body, idempotencyKey: message.msg_id,
    });
    if (result.ok === false || result.accepted === false) {
      throw new ApiProblem(userError(422, result.error || result.detail), 422, result.error_code);
    }
    message.pending = false;
    message.failed = false;
    message.tier = result.ack_tier || result.tier || null;
    message.seq = result.seq ?? null;
  } catch (error) {
    message.pending = false;
    message.failed = true;
    message.error = error instanceof ApiProblem ? error.message : 'Send failed. No effect is claimed.';
  }
  emit(uid ? `dm:${uid}` : 'chat', { kind: 'update', message });
}

function sendMessage(text, row) {
  const clean = text.trim();
  if (!clean) return false;
  const message = {
    msg_id: newId(),
    text: clean,
    ts: new Date().toISOString(),
    to: row ? (row.callsign || row.uid) : null,
    pending: true,
    failed: false,
    tier: null,
  };
  // This identity hint never leaves the browser and never substitutes for the
  // server-derived sender_claim received on the authoritative stream.
  optimisticOperatorMessages.add(message);
  if (row) {
    dmIndex(row.uid).set(message.msg_id, message);
    dmList(row.uid).push(message);
    emit(`dm:${row.uid}`, { kind: 'append', message });
    postChat(message, row.uid);
  } else {
    chatIndex.set(message.msg_id, message);
    state.chat.push(message);
    emit('chat', { kind: 'append', message });
    postChat(message, null);
  }
  return true;
}

function retryMessage(message, uid) {
  if (!message.failed || message.pending) return;
  message.failed = false;
  message.pending = true;
  message.error = '';
  emit(uid ? `dm:${uid}` : 'chat', { kind: 'update', message });
  postChat(message, uid);
}

function handleChatCommand(raw, row, report) {
  const text = raw.trim();
  if (!text) return false;
  if (text.startsWith('/dm')) {
    const match = text.match(/^\/dm\s+(\S+)\s+([\s\S]+)$/);
    if (!match) { report('Use /dm <callsign> message'); return false; }
    const target = findAgent(match[1]);
    if (!target) { report(`Unknown active callsign: ${match[1]}`); return false; }
    sendMessage(match[2], target);
    if (!row || row.uid !== target.uid) location.hash = `#/chat/${encodeURIComponent(target.uid)}`;
    return true;
  }
  if (text.startsWith('/')) { report('Only /dm <callsign> message is supported.'); return false; }
  return sendMessage(text, row);
}

// ---------------------------------------------------------------- shared view components

function heading(title, subtitle) {
  return el('header', { class: 'view-head' },
    el('h1', {}, title),
    subtitle ? el('p', {}, subtitle) : null,
  );
}

function provenance(...parts) {
  return el('footer', { class: 'provenance' }, `source: ${parts.join(' · ')}`);
}

function unavailable(title, body, code) {
  return el('section', { class: 'state-card state-card--unavailable', role: 'status' },
    el('h2', {}, title),
    el('p', {}, body),
    code ? el('p', { class: 'mono state-code' }, code) : null,
  );
}

function freshnessDot(row) {
  const reported = ['fresh', 'recent', 'stale', 'never'].includes(row.freshness)
    ? row.freshness : 'never';
  const freshness = row.seat === 'archived' ? 'archived' : reported;
  return el('span', { class: `dot dot--${freshness}`, 'aria-hidden': 'true' });
}

function durationLabel(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return 'not reported';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

function signalDefinition(label, signal, compatibilityValue) {
  const projected = signal && typeof signal === 'object' ? signal : {};
  const value = normTs(projected.value || compatibilityValue);
  const ttl = Number(projected.ttl_s);
  const observed = normTs(projected.observed_at || value);
  let expires = normTs(projected.expires_at);
  if (!expires && observed && Number.isFinite(ttl) && ttl >= 0) {
    const observedMs = Date.parse(observed);
    if (Number.isFinite(observedMs)) expires = new Date(observedMs + ttl * 1000).toISOString();
  }
  const expiryMs = expires ? Date.parse(expires) : NaN;
  const expiryCopy = Number.isFinite(expiryMs)
    ? `expires ${boundedText(expires, 48)}`
    : 'expiry not reported';
  return el('div', {},
    el('dt', {}, label),
    el('dd', {},
      relSpan(value),
      el('small', {},
        `source ${boundedText(projected.source || 'not reported', 120)} · `,
        `${boundedText(projected.verification || 'unknown', 80)} · `,
        `TTL ${durationLabel(ttl)} · ${expiryCopy}`,
      ),
    ),
  );
}

function channelStrip(currentUid) {
  const strip = el('nav', { class: 'channel-strip', 'aria-label': 'Chat rooms' });
  strip.append(el('a', {
    class: `channel-chip${currentUid ? '' : ' on'}`,
    href: '#/chat',
    'aria-current': currentUid ? null : 'page',
  }, 'Fleet group'));
  for (const row of activeAgents()) {
    strip.append(el('a', {
      class: `channel-chip${row.uid === currentUid ? ' on' : ''}`,
      href: `#/chat/${encodeURIComponent(row.uid)}`,
      'aria-current': row.uid === currentUid ? 'page' : null,
    }, freshnessDot(row), row.display_name || row.callsign || row.uid));
  }
  return strip;
}

function composer(row) {
  const key = draftKey(row);
  const error = el('p', { class: 'composer-error', role: 'alert', hidden: true });
  const input = el('textarea', {
    class: 'composer-input', rows: '1', enterkeyhint: 'send', maxlength: '8192',
    autocomplete: 'off', placeholder: row ? `Message ${row.callsign || row.uid}` : 'Message the fleet',
    'aria-label': row ? `Message ${row.display_name || row.uid}` : 'Message the fleet',
    oninput: (event) => {
      state.drafts.set(key, event.target.value);
      error.hidden = true;
    },
    onkeydown: (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        event.currentTarget.form.requestSubmit();
      }
    },
  });
  input.value = state.drafts.get(key) || '';
  const form = el('form', {
    class: 'composer',
    onsubmit: (event) => {
      event.preventDefault();
      const ok = handleChatCommand(input.value, row, (message) => {
        error.textContent = message;
        error.hidden = false;
      });
      if (ok) {
        input.value = '';
        state.drafts.delete(key);
      }
      input.focus();
    },
  },
    input,
    el('button', { class: 'primary-button composer-send', type: 'submit' }, 'Send'),
  );
  return el('div', { class: 'composer-wrap' }, form, error);
}

function renderMessage(message, uid) {
  const claim = message.sender_claim && typeof message.sender_claim === 'object'
    ? message.sender_claim : null;
  const verifiedOperator = Boolean(
    claim
    && claim.value === 'operator'
    && claim.status === 'authenticated_server_derived'
    && claim.source === 'fleet_hub_session'
  );
  let sender = boundedText(claim && claim.value || message.from || 'unknown', 80);
  let senderStatus = 'reported sender · unverified';
  if (verifiedOperator) {
    sender = 'Operator';
    senderStatus = 'authenticated by Fleet Hub session';
  } else if (!claim && optimisticOperatorMessages.has(message)) {
    sender = 'Operator intent';
    senderStatus = 'local optimistic label · awaiting server identity';
  }
  const article = el('article', {
    class: ['message', verifiedOperator && 'message--operator', message.pending && 'message--pending', message.failed && 'message--failed']
      .filter(Boolean).join(' '),
  },
    el('header', { class: 'message-meta' },
      el('strong', {}, sender),
      el('span', { class: 'sender-verification' }, senderStatus),
      relSpan(message.ts),
    ),
    el('p', { class: 'message-text' }, boundedText(message.text || '', 8192)),
  );
  if (message.pending) article.append(el('p', { class: 'message-state' }, 'Sending… effect not yet known.'));
  else if (message.failed) {
    article.append(
      el('p', { class: 'message-state message-state--error', role: 'alert' }, message.error || 'Send failed.'),
      el('button', {
        class: 'retry-button', type: 'button',
        onclick: () => retryMessage(message, uid),
      }, 'Retry this message'),
    );
  } else if (message.tier) {
    article.append(el('p', { class: 'message-state' }, ACK_COPY[message.tier] || boundedText(message.tier, 80)));
  }
  return article;
}

function feedController(feed, renderer, emptyCopy) {
  const nodes = new Map();
  const nearBottom = () => feed.scrollHeight - feed.scrollTop - feed.clientHeight < 72;
  const scrollBottom = () => { feed.scrollTop = feed.scrollHeight; };
  function reset(messages) {
    feed.textContent = '';
    nodes.clear();
    if (!messages.length) {
      feed.append(el('div', { class: 'empty' }, emptyCopy));
      return;
    }
    for (const message of messages) {
      const node = renderer(message);
      if (message.msg_id) nodes.set(message.msg_id, node);
      feed.append(node);
    }
    scrollBottom();
  }
  function add(message) {
    const stick = nearBottom() || optimisticOperatorMessages.has(message)
      || message.sender_claim && message.sender_claim.status === 'authenticated_server_derived';
    const empty = feed.querySelector('.empty');
    if (empty) empty.remove();
    const node = renderer(message);
    if (message.msg_id) nodes.set(message.msg_id, node);
    feed.append(node);
    if (stick) scrollBottom();
  }
  function update(message) {
    const old = message.msg_id ? nodes.get(message.msg_id) : null;
    if (!old) { add(message); return; }
    const node = renderer(message);
    old.replaceWith(node);
    nodes.set(message.msg_id, node);
  }
  return { reset, add, update };
}

// ---------------------------------------------------------------- Helm

function healthVerdict() {
  const health = state.health;
  if (!health) return ['unknown', 'Health unavailable', 'No current health projection.'];
  if (!health.auth_configured) return ['error', 'Hub locked', 'Server authentication is not configured.'];
  if (!health.broker || !health.broker.connected) return ['error', 'Broker unavailable', 'NATS transport is not connected.'];
  const replay = health.startup_backfill || health.replay;
  if (replay && (replay.ok === false || replay.complete === false)) return ['warning', 'Replay degraded', 'Recent history may be incomplete.'];
  return ['ok', 'Transport connected', 'This proves broker contact, not task execution.'];
}

function truthDimension(label, value, tone, detail) {
  return el('article', { class: `truth-dimension truth-dimension--${tone}` },
    el('div', { class: 'truth-dimension__head' },
      el('span', { class: 'truth-dimension__mark', 'aria-hidden': 'true' }),
      el('span', { class: 'eyebrow' }, label),
    ),
    el('strong', {}, value),
    el('small', {}, detail),
  );
}

function viewHelm(root) {
  root.classList.add('view-scroll');
  const off = [];
  const hero = el('section', { class: 'helm-hero' });
  const dimensions = el('section', {
    class: 'connection-grid',
    'aria-label': 'Independent connection dimensions',
  });
  const healthCard = el('section', { class: 'verdict' });
  const summary = el('section', { class: 'summary-grid', 'aria-label': 'Fleet summary' });
  const needsPreview = el('section', { class: 'card' });

  function paint() {
    const [tone, title, copy] = healthVerdict();
    healthCard.className = `verdict verdict--${tone}`;
    healthCard.textContent = '';
    healthCard.append(
      el('span', { class: 'eyebrow' }, 'Transport evidence'),
      el('h2', {}, title),
      el('p', {}, copy),
    );

    const missions = state.catalog.missions || [];
    const selected = state.bootstrap && state.bootstrap.selected_mission
      || missions[0]
      || null;
    const firstNeed = state.needs.items && state.needs.items[0] || null;
    const missionTotal = Number.isInteger(state.catalog.total_configured_visible)
      ? state.catalog.total_configured_visible : null;
    const needsTotal = Number.isInteger(state.needs.total) ? state.needs.total : null;

    hero.textContent = '';
    if (firstNeed && firstNeed.severity === 'critical') {
      hero.className = 'helm-hero helm-hero--critical';
      hero.append(
        el('div', { class: 'helm-hero__overline' },
          el('span', { class: 'eyebrow' }, 'Decision required'),
          el('span', { class: 'status-pill status-pill--critical' }, 'Critical'),
        ),
        el('h2', {}, boundedText(firstNeed.title || firstNeed.kind || 'Needs John', 220)),
        el('p', { class: 'helm-hero__lead' }, boundedText(firstNeed.reason || 'Owner state requires operator review.', 420)),
        el('p', { class: 'helm-hero__meta mono' }, `source ${boundedText(firstNeed.source_authority || state.needs.source_authority || 'unknown', 100)} · observed ${relTime(firstNeed.observed_at || state.needs.observed_at)}`),
        el('button', { class: 'primary-button', type: 'button', onclick: openNeedsRail }, 'Review decision'),
      );
    } else if (selected) {
      hero.className = 'helm-hero helm-hero--mission';
      hero.append(
        el('div', { class: 'helm-hero__overline' },
          el('span', { class: 'eyebrow' }, 'Selected mission'),
          el('span', { class: 'status-pill' }, boundedText(selected.status || 'unknown', 60)),
        ),
        el('h2', {}, boundedText(selected.title || selected.mission_id, 220)),
        el('p', { class: 'helm-hero__lead' }, boundedText(selected.goal || 'No goal projected by the owner.', 520)),
        el('div', { class: 'helm-hero__footer' },
          el('p', { class: 'helm-hero__meta mono' }, `reconciliation ${boundedText(selected.reconciliation || 'unknown', 80)} · observed ${relTime(selected.observed_at)}`),
          el('a', { class: 'primary-button', href: `#/board/${encodeURIComponent(selected.mission_id)}` }, 'Open mission'),
        ),
      );
    } else if (firstNeed) {
      hero.className = 'helm-hero helm-hero--decision';
      hero.append(
        el('div', { class: 'helm-hero__overline' }, el('span', { class: 'eyebrow' }, 'Needs John')),
        el('h2', {}, boundedText(firstNeed.title || firstNeed.kind || 'Operator review', 220)),
        el('p', { class: 'helm-hero__lead' }, boundedText(firstNeed.reason || 'Review the owner-backed decision evidence.', 420)),
        el('button', { class: 'primary-button', type: 'button', onclick: openNeedsRail }, 'Open decision rail'),
      );
    } else {
      hero.className = 'helm-hero helm-hero--empty';
      const ownerCopy = state.catalog.available
        ? 'No owner-backed mission or decision is selected.'
        : 'Mission Control projection is unavailable. No empty-fleet claim is made.';
      hero.append(
        el('span', { class: 'eyebrow' }, 'Honest state'),
        el('h2', {}, state.catalog.available ? 'Nothing selected' : 'Owner unavailable'),
        el('p', { class: 'helm-hero__lead' }, ownerCopy),
        state.catalog.available
          ? el('a', { class: 'secondary-button', href: '#/board' }, 'Choose a mission')
          : null,
      );
    }

    const hubHealthy = Boolean(state.health && state.health.auth_configured);
    const natsConnected = Boolean(state.health && state.health.broker && state.health.broker.connected);
    const selectedReconciliation = selected && selected.reconciliation;
    dimensions.textContent = '';
    dimensions.append(
      truthDimension(
        'Browser',
        state.conn === 'live' ? 'Live' : state.conn === 'reconnecting' ? 'Reconnecting' : state.conn === 'stale' ? 'Stale' : 'Offline',
        state.conn === 'live' ? 'ok' : state.conn === 'offline' ? 'off' : 'warn',
        'browser ↔ hub stream',
      ),
      truthDimension(
        'Hub',
        state.health ? (hubHealthy ? 'Healthy' : 'Locked') : 'Unknown',
        state.health ? (hubHealthy ? 'ok' : 'off') : 'unknown',
        'process and authentication',
      ),
      truthDimension(
        'NATS',
        state.health ? (natsConnected ? 'Connected' : 'Offline') : 'Unknown',
        state.health ? (natsConnected ? 'ok' : 'off') : 'unknown',
        'transport only',
      ),
      truthDimension(
        'Owner',
        state.catalog.available ? 'Available' : 'Unavailable',
        state.catalog.available ? 'ok' : 'off',
        state.catalog.authority || 'Mission Control projection',
      ),
      truthDimension(
        'Mission',
        selected ? boundedText(selectedReconciliation || 'Unknown', 40) : 'None selected',
        selectedReconciliation === 'coherent' ? 'ok' : selected ? 'warn' : 'unknown',
        selected ? 'owner reconciliation' : 'no inferred state',
      ),
    );

    summary.textContent = '';
    summary.append(
      el('div', { class: 'metric' }, el('span', {}, 'Missions'), el('strong', {}, state.catalog.available ? (missionTotal ?? missions.length) : '—'), el('small', {}, state.catalog.available ? `${missions.length} loaded · configured scope` : 'projection unavailable')),
      el('div', { class: 'metric' }, el('span', {}, 'Needs John'), el('strong', {}, state.needs.available ? (needsTotal ?? state.needs.items.length) : '—'), el('small', {}, state.needs.available ? `${state.needs.items.length} loaded · derived projection` : 'projection unavailable')),
    );

    needsPreview.textContent = '';
    needsPreview.append(el('div', { class: 'card-head' }, el('h2', {}, 'Needs John'),
      el('button', { class: 'text-button', type: 'button', onclick: openNeedsRail }, 'Open rail')));
    if (!state.needs.available) needsPreview.append(el('p', { class: 'muted' }, 'Owner decision projection unavailable. No empty-queue claim is made.'));
    else if (!state.needs.items.length) needsPreview.append(el('p', { class: 'muted' }, 'Owner projection reports no current decision items.'));
    else {
      for (const item of state.needs.items.slice(0, 3)) {
        needsPreview.append(el('article', { class: 'preview-item' },
          el('span', { class: 'preview-item__kind eyebrow' }, boundedText(item.severity || item.kind || 'Review', 48)),
          el('strong', {}, boundedText(item.title || item.requested_action || item.kind || item.id, 180)),
          item.recommended_default
            ? el('small', {}, boundedText(item.recommended_default, 180))
            : null,
        ));
      }
    }
  }

  const observed = state.bootstrap && (state.bootstrap.observed_at || state.bootstrap.generated_at);
  root.append(
    heading('Helm', 'What is true, what needs your call, and what remains unproven.'),
    el('div', { class: 'scope-note' },
      el('strong', {}, 'Evidence boundary: '),
      'stream state is process-local and bounded. ',
      observed ? el('span', {}, 'Projection observed ', relSpan(observed), ' ago.') : 'No v1 observation timestamp is available.',
    ),
    hero,
    el('h2', { class: 'section-title section-title--quiet' }, 'Connection dimensions'),
    dimensions,
    summary,
    needsPreview,
    healthCard,
    el('section', { class: 'card authority-card' },
      el('h2', {}, 'Authority boundary'),
      el('p', {}, 'Board state is projected from TaskBoard + RuntimeStateStore through Mission Control. Fleet Hub is not an owner database and a receipt does not prove executor liveness.'),
    ),
    el('button', {
      class: 'secondary-button', type: 'button',
      onclick: async (event) => {
        event.currentTarget.disabled = true;
        try { await api('api/v1/session/logout', { method: 'POST', body: {} }); } catch { /* local lock still proceeds */ }
        showGate('Signed out on this device.');
        event.currentTarget.disabled = false;
      },
    }, 'Sign out'),
    provenance('api/health', 'api/v1/bootstrap', 'Mission Control projection'),
  );
  paint();
  off.push(
    on('health', paint),
    on('catalog', paint),
    on('needs', paint),
    on('roster', paint),
    on('connection', paint),
  );
  return () => off.forEach((stop) => stop());
}

// ---------------------------------------------------------------- Chat

function viewChat(root, route) {
  root.classList.add('view-pinned');
  const off = [];
  const uid = route.id;
  const row = uid ? state.roster.find((agent) => agent.uid === uid) : null;
  if (uid && !row) {
    root.classList.remove('view-pinned');
    root.classList.add('view-scroll');
    root.append(heading('Chat', 'Unknown direct-message target.'), unavailable('Agent unavailable', 'This UID is not in the configured roster.', uid));
    return () => {};
  }

  let strip = channelStrip(uid);
  const title = row ? `Direct · ${row.display_name || row.uid}` : 'Fleet group';
  const capabilities = state.bootstrap
    && state.bootstrap.capabilities
    && state.bootstrap.capabilities.chat
    && typeof state.bootstrap.capabilities.chat === 'object'
    ? state.bootstrap.capabilities.chat : {};
  const routeCopy = row
    ? (capabilities.direct_message === true
      ? `Direct intent route to ${row.callsign || row.uid}; a semantic reply is not promised.`
      : 'Direct messaging is not advertised by this hub and will fail closed.')
    : (capabilities.group_transcript === true
      ? `One group transcript subject; per-agent fan-out is ${capabilities.group_fanout === true ? 'advertised' : 'not promised'}.`
      : 'Group transcript capability is not advertised by this hub.');
  const feed = el('section', { class: 'chat-feed', role: 'log', 'aria-label': `${title} messages`, 'aria-live': 'polite', 'aria-relevant': 'additions text' });
  const list = row ? dmList(row.uid) : state.chat;
  const controller = feedController(feed, (message) => renderMessage(message, row && row.uid), 'No messages in the bounded replay window.');
  root.append(
    heading('Chat', routeCopy),
    strip,
    feed,
    composer(row),
    provenance(row ? `api/dm/${row.uid}` : 'api/chat', 'api/v1/bootstrap chat capability', 'one multiplexed events/stream'),
  );
  controller.reset(list);
  off.push(on(row ? `dm:${row.uid}` : 'chat', (event) => {
    if (event.kind === 'append') controller.add(event.message);
    else if (event.kind === 'update') controller.update(event.message);
    else controller.reset(row ? dmList(row.uid) : state.chat);
  }));
  off.push(on('roster', () => {
    const next = channelStrip(uid);
    strip.replaceWith(next);
    strip = next;
  }));
  if (row) {
    api(`api/dm/${encodeURIComponent(row.uid)}`)
      .then((payload) => replaceDms(row.uid, payload.messages || []))
      .catch(() => { /* visible bounded empty state remains honest */ });
  }
  return () => off.forEach((stop) => stop());
}

// ---------------------------------------------------------------- Board

function missionLink(summary) {
  const id = String(summary.mission_id || '');
  return el('a', { class: 'mission-card', href: `#/board/${encodeURIComponent(id)}` },
    el('span', { class: 'mission-title' }, boundedText(summary.title || id, 180)),
    el('span', { class: 'status-pill' }, boundedText(summary.status || 'unknown', 80)),
    el('span', { class: 'mission-goal' }, boundedText(summary.goal || 'No goal projected.', 300)),
    el('span', { class: 'mission-meta mono' }, `reconciliation: ${summary.reconciliation || 'unknown'} · observed ${relTime(summary.observed_at)}`),
  );
}

function commandPanel(projection, missionId) {
  const capabilities = projection.capabilities && typeof projection.capabilities === 'object'
    ? projection.capabilities : {};
  const commands = Array.isArray(projection.commands)
    ? projection.commands.map(String)
    : (Array.isArray(capabilities.commands) ? capabilities.commands.map(String) : []);
  const available = projection.commands_available === true || capabilities.commands_available === true;
  if (!available || !commands.length) {
    return unavailable('Commands unavailable', 'This owner projection does not advertise an authorized mutation capability. Board state remains read-only.', projection.error_code);
  }
  const status = el('p', { class: 'form-status', role: 'status', 'aria-live': 'polite' });
  const select = el('select', { 'aria-label': 'Authorized command' });
  for (const command of commands) select.append(el('option', { value: command }, boundedText(command, 100)));
  const form = el('form', {
    class: 'command-form',
    onsubmit: async (event) => {
      event.preventDefault();
      const button = form.querySelector('button');
      button.disabled = true;
      status.textContent = 'Submitting authorized command…';
      try {
        await api(`api/v1/missions/${encodeURIComponent(missionId)}/commands`, {
          method: 'POST',
          body: { command: select.value },
          idempotencyKey: newId(),
          version: projection.source_version,
        });
        status.textContent = 'Command accepted by owner; refetching authoritative state.';
        await scheduleProjectionRecovery(
          `mission-command:${missionId}:${projection.source_version || ''}`,
          'Owner accepted a mission command. Refetched authoritative reads.',
        );
      } catch (error) {
        status.textContent = error instanceof ApiProblem ? error.message : 'Command failed. No effect is claimed.';
      } finally { button.disabled = false; }
    },
  }, select, el('button', { class: 'primary-button', type: 'submit' }, 'Submit command'));
  return el('section', { class: 'card' }, el('h2', {}, 'Owner commands'), form, status);
}

function laneForTask(task) {
  const status = String(task && task.status || '').toLowerCase();
  return BOARD_LANES.find((lane) => lane.states.includes(status))
    || BOARD_LANES.find((lane) => lane.id === 'unmapped');
}

function taskCard(task) {
  return el('article', { class: 'task-card' },
    el('div', { class: 'card-head' },
      el('h3', {}, boundedText(task.title || task.task_id, 240)),
      el('span', { class: 'status-pill' }, boundedText(task.status || 'unknown', 80)),
    ),
    task.description ? el('p', {}, boundedText(task.description, 600)) : null,
    el('dl', { class: 'task-facts' },
      el('div', {}, el('dt', {}, 'Task'), el('dd', { class: 'mono' }, boundedText(task.task_id, 160))),
      el('div', {}, el('dt', {}, 'Delegate'), el('dd', {}, boundedText(task.assigned_to || 'Unassigned', 100))),
    ),
    el('p', { class: 'task-proof' }, 'Owner state only · independent verification not projected'),
  );
}

function renderSnapshot(root, projection, missionId) {
  if (!projection || projection.available !== true || !projection.snapshot) {
    root.append(unavailable('Mission projection unavailable', 'No owner-produced snapshot is available. Fleet Hub will not synthesize board state.', projection && projection.error_code));
    return;
  }
  const snapshot = projection.snapshot;
  const mission = snapshot.mission || {};
  root.append(
    el('section', { class: 'card mission-detail' },
      el('div', { class: 'card-head' }, el('h2', {}, boundedText(mission.title || missionId, 240)), el('span', { class: 'status-pill' }, boundedText(mission.status || 'unknown', 80))),
      el('p', {}, boundedText(mission.goal || 'No goal projected.', 1200)),
      el('dl', { class: 'truth-grid' },
        el('div', {}, el('dt', {}, 'Authority'), el('dd', {}, snapshot.authority || projection.authority || 'unknown')),
        el('div', {}, el('dt', {}, 'Reconciliation'), el('dd', {}, snapshot.reconciliation || 'unknown')),
        el('div', {}, el('dt', {}, 'Observed'), el('dd', {}, snapshot.observed_at ? relTime(snapshot.observed_at) : 'unknown')),
        el('div', {}, el('dt', {}, 'Executor live?'), el('dd', {}, snapshot.proves_executor_liveness === false ? 'Not proven' : 'invalid projection')),
      ),
    ),
  );
  const tasks = Array.isArray(snapshot.tasks) ? snapshot.tasks : [];
  const laneBoard = el('section', {
    class: 'lane-board',
    'aria-label': 'Read-only owner task lanes',
    tabindex: '0',
  });
  for (const lane of BOARD_LANES) {
    const laneTasks = tasks.filter((task) => laneForTask(task).id === lane.id);
    const column = el('section', { class: `lane lane--${lane.id}` },
      el('header', { class: 'lane-head' },
        el('h3', {}, lane.label),
        el('span', { class: 'lane-count mono', 'aria-label': `${laneTasks.length} tasks` }, String(laneTasks.length)),
      ),
    );
    if (!laneTasks.length) {
      column.append(el('p', { class: 'lane-empty' }, 'No owner tasks'));
    } else {
      for (const task of laneTasks) column.append(taskCard(task));
    }
    laneBoard.append(column);
  }
  root.append(
    el('div', { class: 'board-section-head' },
      el('div', {},
        el('h2', { class: 'section-title' }, `Owner task lanes (${tasks.length})`),
        el('p', { class: 'board-mapping mono' }, 'mapping fleet.board.lanes.taskboard.v1 · swipe horizontally'),
      ),
      el('span', { class: 'status-pill' }, 'Read only'),
    ),
    laneBoard,
    commandPanel(projection, missionId),
  );
}

function viewBoard(root, route) {
  root.classList.add('view-scroll');
  const missionId = route.id;
  root.append(heading('Board', 'One read projection from TaskBoard + RuntimeStateStore; Fleet Hub owns no work state.'));
  if (!state.catalog.available) {
    root.append(
      unavailable('Board unavailable', 'Mission Control owner access is not configured or not reachable. No local or Hermes board is substituted.', state.catalog.error_code),
      provenance('api/v1/missions', 'authority TaskBoard+RuntimeStateStore'),
    );
    return () => {};
  }
  if (!missionId) {
    const scope = el('p', { class: 'scope-note' });
    const list = el('section', { class: 'mission-list', 'aria-label': 'Configured missions' });
    const pageState = el('p', { class: 'form-status', role: 'status', 'aria-live': 'polite' });
    const loadMore = el('button', {
      class: 'secondary-button', type: 'button',
      onclick: async (event) => {
        event.currentTarget.disabled = true;
        pageState.textContent = 'Loading the next bounded owner page…';
        try {
          await loadMoreMissions();
          pageState.textContent = 'Loaded the next bounded page.';
        } catch (error) {
          pageState.textContent = error instanceof ApiProblem ? error.message : 'Next page unavailable.';
        } finally { event.currentTarget.disabled = false; }
      },
    }, 'Load more missions');
    const commandHost = el('div');
    function paintCatalog() {
      const loaded = state.catalog.missions.length;
      const total = Number.isInteger(state.catalog.total_configured_visible)
        ? state.catalog.total_configured_visible : null;
      scope.textContent = state.catalog.discovery_complete
        ? `Configured provider discovery is complete; showing ${loaded}${total === null ? '' : ` of ${total}`}.`
        : `Configured mission scope only; showing ${loaded}${total === null ? '' : ` of ${total}`} visible snapshots. This does not claim fleet-wide discovery.`;
      list.textContent = '';
      if (!loaded) list.append(el('div', { class: 'empty' }, 'Owner projection reports no configured mission snapshots.'));
      for (const mission of state.catalog.missions) list.append(missionLink(mission));
      loadMore.hidden = !state.catalog.next_cursor;
      commandHost.textContent = '';
      commandHost.append(commandPanel(state.catalog, ''));
    }
    root.append(
      scope,
      list,
      loadMore,
      pageState,
      commandHost,
      provenance('api/v1/missions', 'bounded Mission Control read projection'),
    );
    paintCatalog();
    const stop = on('catalog', paintCatalog);
    return () => stop();
  }
  root.append(el('a', { class: 'back-link', href: '#/board' }, '← All missions'));
  const loading = el('div', { class: 'loading', role: 'status' }, 'Loading owner snapshot…');
  root.append(loading);
  const mountedRevision = viewRevision;
  const requestedSnapshotRevision = state.snapshotRevision;
  loadSnapshot(missionId)
    .then((projection) => {
      if (
        mountedRevision !== viewRevision
        || requestedSnapshotRevision !== state.snapshotRevision
        || state.route.name !== 'board'
        || state.route.id !== missionId
      ) return;
      loading.remove();
      renderSnapshot(root, projection, missionId);
      root.append(provenance(`api/v1/missions/${missionId}/snapshot`, 'bounded and redacted owner wire'));
    })
    .catch((error) => {
      if (
        !loading.isConnected
        || mountedRevision !== viewRevision
        || requestedSnapshotRevision !== state.snapshotRevision
        || error && error.code === 'request_superseded'
      ) return;
      loading.replaceWith(unavailable('Snapshot request failed', error.message, error.code));
    });
  return () => {};
}

// ---------------------------------------------------------------- Trace

function receiptRows() {
  const rows = [];
  for (const projection of state.snapshots.values()) {
    const snapshot = projection && projection.snapshot;
    for (const receipt of snapshot && Array.isArray(snapshot.receipts) ? snapshot.receipts : []) {
      rows.push({
        ts: receipt.created_at,
        subject: `receipt.${receipt.receipt_type || 'unknown'}`,
        preview: `${receipt.receipt_id || '?'} · task ${receipt.task_id || '?'} · status ${receipt.status || 'unknown'}`,
        tier: 'DOMAIN_RECEIPTED',
      });
    }
  }
  return rows.slice(-TRACE_LIMIT);
}

function traceRow(frame) {
  return el('article', { class: 'trace-row', dataset: { hay: `${frame.subject} ${frame.preview} ${frame.tier}`.toLowerCase() } },
    el('div', { class: 'trace-meta mono' },
      el('span', {}, boundedText(frame.subject, 160)),
      relSpan(frame.ts),
    ),
    el('p', {}, redactPreview(frame.preview)),
    frame.tier ? el('p', { class: 'trace-tier' }, ACK_COPY[frame.tier] || boundedText(frame.tier, 80)) : null,
  );
}

function viewTrace(root) {
  root.classList.add('view-pinned');
  let query = '';
  const count = el('span', { class: 'trace-count mono' });
  const feed = el('section', { class: 'trace-feed', 'aria-label': 'Bounded trace records' });
  const filter = el('input', {
    type: 'search', class: 'trace-filter', placeholder: 'Filter subject, preview, or tier',
    'aria-label': 'Filter trace records', autocomplete: 'off', spellcheck: 'false',
    oninput: (event) => { query = event.target.value.trim().toLowerCase(); applyFilter(); },
  });
  const allFrames = () => [...receiptRows(), ...state.trace].slice(-TRACE_LIMIT);
  function applyFilter() {
    let visible = 0;
    for (const row of feed.querySelectorAll('.trace-row')) {
      const show = !query || row.dataset.hay.includes(query);
      row.hidden = !show;
      if (show) visible += 1;
    }
    count.textContent = `${visible}/${feed.querySelectorAll('.trace-row').length} shown · cap ${TRACE_LIMIT}`;
  }
  function paint() {
    feed.textContent = '';
    const frames = allFrames();
    if (!frames.length) feed.append(el('div', { class: 'empty' }, 'No process-local frames or loaded owner receipts. This is not proof that the fleet was idle.'));
    for (const frame of frames) feed.append(traceRow(frame));
    applyFilter();
  }
  root.append(
    heading('Trace', 'Bounded and redacted evidence inspection; not an accessibility live log.'),
    el('div', { class: 'trace-tools' }, filter, count),
    el('p', { class: 'scope-note' }, 'Raw stream frames exist only in this browser process and reset with the page. Owner receipts appear only for mission snapshots opened in Board.'),
    feed,
    provenance('one multiplexed events/stream', 'loaded Mission Control receipts', `cap ${TRACE_LIMIT}`),
  );
  paint();
  const stop = on('trace', () => paint());
  return () => stop();
}

// ---------------------------------------------------------------- Roster

function rosterRow(row) {
  const signals = row.signals && typeof row.signals === 'object' ? row.signals : {};
  return el('article', { class: 'roster-card' },
    el('div', { class: 'roster-title' }, freshnessDot(row), el('h2', {}, row.display_name || row.uid), el('span', { class: 'mono' }, row.callsign || row.uid)),
    el('dl', { class: 'signal-grid' },
      signalDefinition('Heard', signals.last_heard, row.last_heard),
      signalDefinition('Addressed', signals.last_addressed, row.last_addressed),
    ),
    el('p', { class: 'signal-source mono' }, `subject: ${boundedText(row.subject || 'not declared', 180)}`),
    el('p', { class: 'muted' }, 'Heard is traffic whose payload reported this roster identity, subject to the displayed verification. Addressed is traffic observed on its configured subject. Neither proves a running process.'),
    row.seat === 'archived' ? el('span', { class: 'status-pill' }, 'archived seat') : el('a', { class: 'text-button', href: `#/chat/${encodeURIComponent(row.uid)}` }, 'Open chat'),
  );
}

function viewRoster(root) {
  root.classList.add('view-scroll');
  const list = el('section', { class: 'roster-list', 'aria-label': 'Configured agent roster' });
  const nodes = el('section', { class: 'card' });
  function paint() {
    list.textContent = '';
    if (!state.roster.length) list.append(el('div', { class: 'empty' }, 'No configured roster projection is available.'));
    for (const row of state.roster) list.append(rosterRow(row));
    nodes.textContent = '';
    nodes.append(el('h2', {}, 'Nodes'));
    if (!state.nodes || !Array.isArray(state.nodes.nodes)) nodes.append(el('p', { class: 'muted' }, 'Node catalog unavailable.'));
    else for (const node of state.nodes.nodes) {
      nodes.append(el('div', { class: 'node-row' },
        el('strong', {}, boundedText(node.label || node.id || 'unknown', 100)),
        el('span', {}, boundedText(node.role || '', 120)),
        el('span', { class: 'mono' }, boundedText(node.tailscale || node.public || 'no address projected', 160)),
      ));
    }
  }
  root.append(
    heading('Roster', 'Configured identities with two distinct, sourced TTL signals.'),
    el('p', { class: 'scope-note' }, 'Each signal retains the server-projected source, verification status, TTL, and expiry. Identity remains reported unless its issuer contract authenticates it.'),
    list,
    nodes,
    provenance('api/v1/roster?include=archived', 'api/v1/topology', 'per-signal source labels'),
  );
  paint();
  const stopRoster = on('roster', paint);
  const stopNodes = on('nodes', paint);
  if (!state.nodes) api('api/v1/topology').then((payload) => { state.nodes = payload; emit('nodes'); }).catch(() => emit('nodes'));
  return () => { stopRoster(); stopNodes(); };
}

// ---------------------------------------------------------------- Needs John rail

const needsToggle = document.getElementById('needs-toggle');
const needsBadge = document.getElementById('needs-badge');
const needsRail = document.getElementById('needs-rail');
const needsClose = document.getElementById('needs-close');
const railScrim = document.getElementById('rail-scrim');
const needsState = document.getElementById('needs-state');
const needsList = document.getElementById('needs-list');
let railReturnFocus = null;

function paintNeedsBadge() {
  if (!state.needs.available) {
    needsBadge.textContent = '—';
    needsBadge.setAttribute('aria-label', 'Needs John unavailable');
    needsToggle.classList.remove('has-items');
    return;
  }
  const count = Number.isInteger(state.needs.total) ? state.needs.total : state.needs.items.length;
  needsBadge.textContent = String(count);
  needsBadge.setAttribute(
    'aria-label',
    `${count} Needs John item${count === 1 ? '' : 's'}; ${state.needs.items.length} loaded`,
  );
  needsToggle.classList.toggle('has-items', count > 0);
}

async function submitNeedsAction(item, action, button, result) {
  if (state.needs.commands_available !== true) return;
  button.disabled = true;
  result.textContent = 'Submitting to owner…';
  try {
    await api(`api/v1/needs-john/${encodeURIComponent(item.item_id || item.id)}/commands`, {
      method: 'POST', body: { command: action }, idempotencyKey: newId(), version: item.source_version,
    });
    result.textContent = 'Owner accepted the command; refetching projection.';
    await scheduleProjectionRecovery(
      `needs-command:${item.item_id || item.id}:${item.source_version || ''}`,
      'Owner accepted a decision command. Refetched authoritative reads.',
    );
  } catch (error) {
    result.textContent = error instanceof ApiProblem ? error.message : 'Action failed. No effect is claimed.';
  } finally { button.disabled = false; }
}

function paintNeedsRail() {
  needsState.textContent = '';
  needsList.textContent = '';
  if (!state.needs.available) {
    needsState.append(el('p', {}, 'Owner decision projection unavailable. An empty queue is not claimed.'), el('p', { class: 'mono' }, state.needs.error_code || 'provider_unavailable'));
    return;
  }
  const total = Number.isInteger(state.needs.total) ? state.needs.total : null;
  needsState.append(el('p', {}, `${state.needs.items.length}${total === null ? '' : ` of ${total}`} derived decision item${total === 1 ? '' : 's'} loaded.`),
    state.needs.mission_id ? el('p', { class: 'mono' }, `mission ${boundedText(state.needs.mission_id, 128)}`) : null,
    state.needs.observed_at ? el('p', { class: 'mono' }, `observed ${relTime(state.needs.observed_at)}`) : null);
  if (!state.needs.items.length) needsList.append(el('div', { class: 'empty' }, 'Owner projection reports no current decision items.'));
  for (const item of state.needs.items) {
    const result = el('p', { class: 'form-status', role: 'status', 'aria-live': 'polite' });
    const advertised = new Set(Array.isArray(state.needs.commands) ? state.needs.commands : []);
    const actions = state.needs.commands_available === true && Array.isArray(item.allowed_commands)
      ? item.allowed_commands.filter((command) => advertised.has(command)) : [];
    const evidenceRefs = Array.isArray(item.evidence_refs) ? item.evidence_refs : [];
    const card = el('article', { class: 'need-card' },
      el('p', { class: 'eyebrow' }, boundedText(item.kind || 'decision', 80)),
      el('h3', {}, boundedText(item.requested_action || item.item_id || item.id, 240)),
      el('p', {}, boundedText(item.reason || 'No reason projected.', 1200)),
      el('p', { class: 'muted' }, `Recommended default: ${boundedText(item.recommended_default || 'not projected', 500)}`),
      item.consequence ? el('p', { class: 'muted' }, `Consequence: ${boundedText(item.consequence, 500)}`) : null,
      evidenceRefs.length
        ? el('ul', { class: 'evidence-list', 'aria-label': 'Evidence references' },
          evidenceRefs.map((reference) => el('li', { class: 'mono' }, boundedText(reference, 220))))
        : el('p', { class: 'mono muted' }, 'No evidence references projected.'),
      el('p', { class: 'mono need-source' },
        `source: ${boundedText(item.source_authority || state.needs.source_authority || 'unknown', 160)} · `,
        `version ${boundedText(item.source_version || state.needs.source_version || 'unknown', 220)}`,
      ),
    );
    if (actions.length) {
      const actionRow = el('div', { class: 'need-actions' });
      for (const action of actions) {
        const button = el('button', { class: 'secondary-button', type: 'button' }, boundedText(action, 100));
        button.addEventListener('click', () => submitNeedsAction(item, action, button, result));
        actionRow.append(button);
      }
      card.append(actionRow, result);
    } else card.append(el('p', { class: 'unavailable-label' }, 'Actions unavailable: owner authority was not advertised.'));
    needsList.append(card);
  }
  if (state.needs.next_cursor) {
    const pageResult = el('p', { class: 'form-status', role: 'status', 'aria-live': 'polite' });
    const more = el('button', {
      class: 'secondary-button', type: 'button',
      onclick: async () => {
        more.disabled = true;
        pageResult.textContent = 'Loading the next bounded decision page…';
        try {
          await loadMoreNeeds();
          paintNeedsRail();
        } catch (error) {
          more.disabled = false;
          pageResult.textContent = error instanceof ApiProblem ? error.message : 'Next page unavailable.';
        }
      },
    }, 'Load more decisions');
    needsList.append(more, pageResult);
  }
}

function openNeedsRail() {
  if (!needsRail.hidden) return;
  railReturnFocus = document.activeElement;
  app.setAttribute('inert', '');
  needsRail.hidden = false;
  railScrim.hidden = false;
  needsRail.setAttribute('aria-hidden', 'false');
  needsToggle.setAttribute('aria-expanded', 'true');
  paintNeedsRail();
  needsClose.focus();
}

function closeNeedsRail(restoreFocus = true) {
  if (needsRail.hidden) return;
  needsRail.hidden = true;
  railScrim.hidden = true;
  needsRail.setAttribute('aria-hidden', 'true');
  needsToggle.setAttribute('aria-expanded', 'false');
  app.removeAttribute('inert');
  if (restoreFocus && railReturnFocus && railReturnFocus.focus) railReturnFocus.focus();
  railReturnFocus = null;
}

function trapNeedsFocus(event) {
  if (event.key !== 'Tab' || needsRail.hidden) return;
  const focusable = [...needsRail.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )].filter((node) => !node.hidden && node.getAttribute('aria-hidden') !== 'true');
  if (!focusable.length) {
    event.preventDefault();
    needsRail.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (!needsRail.contains(document.activeElement)) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
  } else if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

needsToggle.addEventListener('click', openNeedsRail);
needsClose.addEventListener('click', closeNeedsRail);
railScrim.addEventListener('click', closeNeedsRail);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !needsRail.hidden) {
    event.preventDefault();
    closeNeedsRail();
    return;
  }
  trapNeedsFocus(event);
});

// ---------------------------------------------------------------- router

const viewFactories = { helm: viewHelm, chat: viewChat, board: viewBoard, trace: viewTrace, roster: viewRoster };
let cleanupView = null;
let viewRevision = 0;

function safeHashId(raw) {
  if (!raw || raw.length > HASH_ID_LIMIT * 3) return null;
  let decoded;
  try { decoded = decodeURIComponent(raw); } catch { return null; }
  if (decoded.length > HASH_ID_LIMIT) return null;
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(decoded)) return null;
  return decoded;
}

function parseHash() {
  if (location.hash.length > 512) return { name: 'helm', id: null };
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  if (parts.length > 2 || (parts[0] && !/^[a-z]{1,16}$/.test(parts[0]))) {
    return { name: 'helm', id: null };
  }
  const aliases = { now: 'helm', talk: 'chat', fleet: 'roster', flow: 'trace', agent: 'chat' };
  const name = aliases[parts[0]] || parts[0] || 'helm';
  if (!Object.prototype.hasOwnProperty.call(viewFactories, name)) return { name: 'helm', id: null };
  const id = parts[1] ? safeHashId(parts[1]) : null;
  if (parts[1] && !id) return { name: 'helm', id: null };
  return { name, id };
}

function mountView(focus = true) {
  const route = parseHash();
  state.route = route;
  viewRevision += 1;
  if (cleanupView) cleanupView();
  const root = document.getElementById('view');
  root.textContent = '';
  root.className = '';
  cleanupView = viewFactories[route.name](root, route) || null;
  document.querySelectorAll('#tabs .tab').forEach((tab) => {
    const current = tab.dataset.view === route.name;
    tab.classList.toggle('on', current);
    if (current) tab.setAttribute('aria-current', 'page');
    else tab.removeAttribute('aria-current');
  });
  document.title = `${TITLES[route.name] || 'Fleet Hub'} · Fleet Hub`;
  root.scrollTop = 0;
  if (focus) requestAnimationFrame(() => root.focus({ preventScroll: true }));
}

// ---------------------------------------------------------------- gate and boot

const gate = document.getElementById('gate');
const app = document.getElementById('app');
const gateBanner = document.getElementById('gate-banner');
const gateError = document.getElementById('gate-err');
const gateForm = document.getElementById('gate-form');
const gateToken = document.getElementById('gate-token');
const gateButton = document.getElementById('gate-btn');
const appStatus = document.getElementById('app-status');

function announce(message) { appStatus.textContent = message; }

function resetPrivateState() {
  state.sessionRevision += 1;
  abortActiveRequests();
  invalidateSnapshots();
  if (cleanupView) cleanupView();
  cleanupView = null;
  viewRevision += 1;
  subs.clear();
  chatIndex.clear();
  dmIndexes.clear();
  optimisticOperatorMessages = new WeakSet();
  state.authenticated = false;
  state.csrfToken = null;
  state.authMode = null;
  state.route = { name: 'helm', id: null };
  state.streamEpoch = null;
  state.lastStreamEventAt = null;
  state.health = null;
  state.vision = null;
  state.roster = [];
  state.nodes = null;
  state.chat = [];
  state.dms.clear();
  state.trace = [];
  state.drafts.clear();
  state.bootstrap = null;
  paintEvidenceMode(null);
  state.catalog = {
    available: false,
    discovery_complete: false,
    commands_available: false,
    commands: [],
    missions: [],
    configured_mission_ids: [],
    total_configured_visible: null,
    next_cursor: null,
    source_version: null,
    error_code: 'not_loaded',
  };
  state.needs = {
    available: false,
    items: [],
    observed_at: null,
    process_local: true,
    mission_id: null,
    total: null,
    next_cursor: null,
    source_authority: null,
    source_version: null,
    commands_available: false,
    commands: [],
    error_code: 'not_loaded',
  };
  recoverySequence += 1;
  recoveryPromise = null;
  queuedRecovery = null;
  lastRecoveryKey = null;
  lastRecoveryAt = 0;
  const view = document.getElementById('view');
  view.textContent = '';
  view.className = '';
  needsState.textContent = '';
  needsList.textContent = '';
  paintNeedsBadge();
  appStatus.textContent = '';
  if (location.hash !== '#/helm') {
    history.replaceState(null, '', `${location.pathname}${location.search}#/helm`);
  }
}

function showGate(message = '') {
  stream.close();
  setConnection('offline');
  closeNeedsRail(false);
  app.removeAttribute('inert');
  resetPrivateState();
  app.hidden = true;
  gate.hidden = false;
  gateBanner.hidden = !message;
  gateBanner.textContent = message;
  gateToken.value = '';
  document.title = 'Unlock · Fleet Hub';
  requestAnimationFrame(() => gateToken.focus());
}

async function readSession() {
  const session = await api('api/session');
  state.csrfToken = typeof session.csrf_token === 'string' ? session.csrf_token : null;
  state.authMode = session.auth_mode || null;
  return session;
}

async function login(token) {
  const result = await api('login', { method: 'POST', body: { token }, skipCsrf: true });
  if (result.ok === false) throw new ApiProblem('Login rejected.', 401, 'login_rejected');
  return readSession();
}

gateForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const token = gateToken.value.trim();
  if (!token) return;
  gateButton.disabled = true;
  gateError.textContent = '';
  try {
    const session = await login(token);
    gateToken.value = '';
    if (!session.authenticated) throw new ApiProblem('Login did not create an authenticated session.', 401);
    await enterApp();
  } catch (error) {
    gateError.textContent = error instanceof ApiProblem ? error.message : 'Hub unreachable.';
  } finally { gateButton.disabled = false; }
});

async function refreshLegacy(sessionRevision = state.sessionRevision) {
  const results = await Promise.allSettled([
    api('api/v1/roster?include=archived'),
    api('api/chat'),
    api('api/health'),
    optionalApi('api/vision'),
    optionalApi('api/v1/topology'),
  ]);
  if (!state.authenticated || sessionRevision !== state.sessionRevision) return;
  if (results[0].status === 'fulfilled') { state.roster = results[0].value.agents || []; emit('roster'); }
  if (results[1].status === 'fulfilled') replaceChat(results[1].value.messages || []);
  if (results[2].status === 'fulfilled') { state.health = results[2].value; emit('health'); }
  if (results[3].status === 'fulfilled' && results[3].value) state.vision = results[3].value;
  if (results[4].status === 'fulfilled' && results[4].value) { state.nodes = results[4].value; emit('nodes'); }
}

async function refreshAll(reason = '', options = {}) {
  if (options.invalidate !== false) invalidateSnapshots();
  const sessionRevision = state.sessionRevision;
  await Promise.allSettled([
    refreshLegacy(sessionRevision),
    refreshV1(sessionRevision),
  ]);
  if (!state.authenticated || sessionRevision !== state.sessionRevision) return;
  if (reason) announce(reason);
}

async function enterApp() {
  state.authenticated = true;
  gate.hidden = true;
  app.hidden = false;
  await refreshAll();
  if (!state.authenticated) return;
  stream.open();
  mountView(false);
}

async function registerWorker() {
  if (!('serviceWorker' in navigator)) return;
  try {
    await navigator.serviceWorker.register(BASE + 'sw.js', { scope: BASE });
  } catch {
    // The app remains network-only; no false offline-ready state is shown.
  }
}

async function boot() {
  startTicker();
  paintNeedsBadge();
  window.addEventListener('hashchange', () => { if (state.authenticated) mountView(true); });
  window.addEventListener('online', () => {
    if (state.authenticated) {
      scheduleProjectionRecovery('network-restored', 'Network restored; projections refetched.');
    }
  });
  window.addEventListener('offline', () => setConnection('offline'));
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && state.authenticated) {
      stream.open();
      scheduleProjectionRecovery(
        `visibility:${Date.now()}`,
        'Returned to Fleet Hub; projections refetched.',
      );
    }
  });
  setInterval(() => {
    if (state.authenticated && state.lastStreamEventAt && Date.now() - state.lastStreamEventAt > 45000) {
      setConnection('stale');
    }
  }, 15000);
  registerWorker();

  let session;
  try { session = await readSession(); }
  catch (error) {
    showGate(error instanceof ApiProblem ? error.message : 'Hub unreachable.');
    return;
  }
  if (!session.auth_configured) {
    showGate('Hub locked: server authentication is not configured.');
    return;
  }
  if (session.authenticated) await enterApp();
  else showGate();
}

boot();
