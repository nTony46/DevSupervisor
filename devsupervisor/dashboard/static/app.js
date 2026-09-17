/* DevSupervisor dashboard — read-only.
 *
 * Every string that comes from durable state is written with textContent. Job
 * titles, gate questions and blocker notes are operator data, not markup, and
 * this file never builds HTML out of them.
 */
'use strict';

const STATE_MS = 2000;
const ACTIVITY_MS = 4000;
const PAGE = 50;

const el = (id) => document.getElementById(id);
const ui = {
  project: el('project'), status: el('status'), git: el('git'), active: el('active'),
  leases: el('leases'), spend: el('spend'), pipeline: el('pipeline'), current: el('current'),
  graph: el('graph'), wires: el('wires'), supervisor: el('tier-supervisor'),
  workers: el('tier-workers'), activity: el('activity'), more: el('more'),
  filters: el('filters'), detail: el('detail'), detailTitle: el('detail-title'),
  detailBody: el('detail-body'), err: el('err'),
};

let project = new URLSearchParams(location.search).get('project') || '';
let filter = 'all';
let entries = [];
let activitySignature = '';
let lastState = { agents: [] };

/* --- tiny DOM helpers: every value lands as text, never as markup --- */
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

async function getJSON(path) {
  const response = await fetch(path, { headers: { Accept: 'application/json' } });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

function showError(message) {
  ui.err.textContent = message;
  ui.err.hidden = !message;
}

/* --- header --- */
function renderHeader(state) {
  const status = state.status || 'OFFLINE';
  ui.status.dataset.state = status;
  ui.status.querySelector('.label').textContent = status;
  const git = state.git || {};
  ui.git.textContent = git.branch ? `${git.branch} @ ${git.sha || '—'}${git.clean === false ? ' *' : ''}` : '';
  // The server counts active jobs; the agent list it sends may be capped.
  const live = state.active_count || 0;
  ui.active.textContent = `${live} active`;
  ui.leases.textContent = `${state.leases || 0} lease${state.leases === 1 ? '' : 's'}`;
  const spend = state.spend || {};
  ui.spend.textContent = typeof spend.project_usd === 'number'
    ? `$${spend.project_usd.toFixed(2)}` : '';
}

function renderProjects(state) {
  const names = state.projects || [];
  const current = state.project ? state.project.id : '';
  const signature = names.map((p) => p.id).join(',') + '|' + current;
  if (ui.project.dataset.signature === signature) return;
  ui.project.dataset.signature = signature;
  ui.project.replaceChildren();
  names.forEach((entry) => {
    const option = node('option', null, entry.name);
    option.value = entry.id;
    if (entry.id === current) option.selected = true;
    ui.project.appendChild(option);
  });
  ui.project.disabled = names.length < 2;
}

/* --- pipeline --- */
function renderPipeline(state) {
  ui.pipeline.replaceChildren();
  const stages = state.pipeline || [];
  if (!stages.length) {
    ui.pipeline.appendChild(node('span', 'empty', 'No planned work'));
  }
  stages.forEach((stage, index) => {
    const wrap = node('div', 'stage');
    wrap.dataset.state = stage.state;
    const mark = { complete: ' ✓', active: ' ●', blocked: ' ◆' }[stage.state] || ' ○';
    wrap.appendChild(node('span', 'name', stage.name + mark));
    if (index < stages.length - 1) wrap.appendChild(node('span', 'arrow', '→'));
    ui.pipeline.appendChild(wrap);
  });
  ui.current.textContent = state.current ? `Current: ${state.current}` : '';
}

/* --- graph --- */
function elapsed(seconds) {
  if (seconds === null || seconds === undefined) return '';
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return ` · ${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function supervisorNode(state) {
  const sup = state.supervisor || {};
  const gated = state.status === 'WAITING FOR HUMAN';
  const box = node('div', 'node supervisor' + (gated ? ' gate' : ''));
  box.dataset.status = gated ? 'WAITING' : sup.status === 'RUNNING' ? 'ACTIVE' : 'IDLE';
  box.appendChild(node('div', 'role', 'SUPERVISOR'));
  box.appendChild(node('div', 'state', gated ? '◆ WAITING FOR DECISION' : sup.status || ''));
  box.appendChild(node('div', 'line', sup.line || ''));
  if (gated && sup.detail) box.appendChild(node('div', 'gate-id', sup.detail));
  if (gated && sup.note) box.appendChild(node('div', 'note', sup.note));
  return box;
}

function workerNode(agent) {
  const box = node(agent.id ? 'button' : 'div', 'node');
  box.dataset.status = agent.status;
  if (agent.id) {
    box.dataset.job = agent.id;
    box.type = 'button';
    box.addEventListener('click', () => showDetail(agent.id));
  }
  box.appendChild(node('div', 'role', agent.role || 'agent'));
  const mark = { ACTIVE: '●', IDLE: '○', STALE: '⚠' }[agent.status] || '◆';
  box.appendChild(node('div', 'state', `${mark} ${agent.status}${elapsed(agent.elapsed_s)}`));
  box.appendChild(node('div', 'line', agent.line || ''));
  return box;
}

function renderGraph(state) {
  ui.supervisor.replaceChildren(supervisorNode(state));
  ui.workers.replaceChildren();
  const agents = state.agents || [];
  if (!agents.length) {
    ui.workers.appendChild(node('div', 'empty', 'No agents registered for this project'));
  }
  agents.forEach((agent) => ui.workers.appendChild(workerNode(agent)));
  if (state.hidden_agents) {
    ui.workers.appendChild(node('div', 'node more-agents',
      `+${state.hidden_agents} more not shown`));
  }
  requestAnimationFrame(() => drawWires(state));
}

function drawWires(state) {
  const box = ui.graph.getBoundingClientRect();
  ui.wires.setAttribute('viewBox', `0 0 ${box.width} ${box.height}`);
  ui.wires.replaceChildren();
  const root = ui.supervisor.querySelector('.node');
  if (!root) return;
  const from = root.getBoundingClientRect();
  const origin = { x: from.left - box.left + from.width / 2, y: from.bottom - box.top };
  const positions = new Map();
  ui.workers.querySelectorAll('.node').forEach((worker) => {
    const rect = worker.getBoundingClientRect();
    const point = { x: rect.left - box.left + rect.width / 2, y: rect.top - box.top };
    positions.set(worker.dataset.job || '', { point, rect, worker });
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', origin.x);
    line.setAttribute('y1', origin.y);
    line.setAttribute('x2', point.x);
    line.setAttribute('y2', point.y);
    const status = worker.dataset.status;
    if (status === 'ACTIVE') line.classList.add('live');
    if (status === 'WAITING') line.classList.add('gate');
    if (status === 'STALE') line.classList.add('stale');
    ui.wires.appendChild(line);
  });
  // Real delegation edges only: a reviewer to the work it reviews, a revision
  // to the attempt it replaces. Nothing decorative.
  (state.agents || []).forEach((agent) => {
    const target = agent.reviews || agent.revision_of;
    if (!agent.id || !target || !positions.has(agent.id) || !positions.has(target)) return;
    const a = positions.get(agent.id);
    const b = positions.get(target);
    // Dipped below the row: a straight line between two nodes would cut
    // through whichever unrelated nodes happen to sit between them.
    const ay = a.rect.bottom - box.top;
    const by = b.rect.bottom - box.top;
    const dip = Math.max(ay, by) + 26;
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', `M ${a.point.x} ${ay} C ${a.point.x} ${dip}, ${b.point.x} ${dip}, ${b.point.x} ${by}`);
    path.classList.add('rel');
    ui.wires.appendChild(path);
  });
}

/* --- activity --- */
const MARKS = { ok: '✓', bad: '✗', gate: '◇', run: '→', muted: '·', warn: '↻' };

function localMoment(iso) {
  const when = new Date(iso);
  if (isNaN(when)) return String(iso);
  return when.toLocaleString([], {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
  });
}

function detailValue(key, value) {
  if (key.endsWith('_at')) return localMoment(value);
  if (key === 'cost_usd') return `$${Number(value).toFixed(2)}`;
  return value;
}

function localTime(iso) {
  const when = new Date(iso);
  if (isNaN(when)) return (iso || '').slice(11, 16);
  return when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

function renderActivity() {
  ui.activity.replaceChildren();
  if (!entries.length) {
    ui.activity.appendChild(node('li', 'empty', 'Nothing recorded yet'));
    return;
  }
  entries.forEach((entry) => {
    const row = node('li');
    row.dataset.tone = entry.tone;
    const at = node('span', 'at', localTime(entry.at));
    at.title = entry.at || '';
    row.appendChild(at);
    row.appendChild(node('span', 'mark', MARKS[entry.tone] || '·'));
    const body = node('div');
    body.appendChild(node('div', 'title', entry.title || ''));
    if (entry.detail) body.appendChild(node('div', 'sub', entry.detail));
    if (entry.note) body.appendChild(node('div', 'note', entry.note));
    row.appendChild(body);
    ui.activity.appendChild(row);
  });
}

async function loadActivity(reset) {
  const params = new URLSearchParams({ limit: String(PAGE), kind: filter });
  if (project) params.set('project', project);
  if (!reset && entries.length) params.set('before', entries[entries.length - 1].at);
  const data = await getJSON('/api/activity?' + params.toString());
  if (reset) {
    const signature = `${filter}:${data.entries.length}:${data.entries[0] ? data.entries[0].at : ''}`;
    if (signature === activitySignature) return;
    activitySignature = signature;
    entries = data.entries;
  } else {
    entries = entries.concat(data.entries);
    activitySignature = '';
  }
  ui.more.hidden = !data.has_more;
  renderActivity();
}

/* --- detail panel --- */
async function showDetail(jobId) {
  try {
    const detail = await getJSON('/api/job?id=' + encodeURIComponent(jobId));
    ui.detailTitle.textContent = detail.id || jobId;
    ui.detailBody.replaceChildren();
    Object.entries(detail).forEach(([key, value]) => {
      if (key === 'id' || key === 'artifacts') return;
      ui.detailBody.appendChild(node('dt', null, key.replace(/_/g, ' ')));
      ui.detailBody.appendChild(node('dd', null, detailValue(key, value)));
    });
    (detail.artifacts || []).forEach((artifact) => {
      ui.detailBody.appendChild(node('dt', null, artifact.kind));
      ui.detailBody.appendChild(node('dd', null, artifact.summary));
    });
    ui.detail.hidden = false;
  } catch (error) {
    showError(error.message);
  }
}

function closeDetail() {
  ui.detail.hidden = true;
}

/* --- polling --- */
async function tick() {
  try {
    const state = await getJSON('/api/state' + (project ? '?project=' + encodeURIComponent(project) : ''));
    if (state.project) project = state.project.id;
    lastState = state;
    renderProjects(state);
    renderHeader(state);
    renderPipeline(state);
    renderGraph(state);
    showError('');
  } catch (error) {
    showError('dashboard: ' + error.message);
  }
}

async function tickActivity() {
  try {
    await loadActivity(true);
  } catch (error) {
    showError('activity: ' + error.message);
  }
}

ui.project.addEventListener('change', () => {
  project = ui.project.value;
  entries = [];
  activitySignature = '';
  closeDetail();
  const url = new URL(location.href);
  url.searchParams.set('project', project);
  history.replaceState(null, '', url);
  tick();
  tickActivity();
});

ui.filters.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-kind]');
  if (!button) return;
  filter = button.dataset.kind;
  ui.filters.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b === button));
  entries = [];
  activitySignature = '';
  tickActivity();
});

ui.more.addEventListener('click', () => loadActivity(false).catch((e) => showError(e.message)));
el('detail-close').addEventListener('click', closeDetail);
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeDetail(); });
window.addEventListener('resize', () => drawWires(lastState));

tick();
tickActivity();
setInterval(tick, STATE_MS);
setInterval(tickActivity, ACTIVITY_MS);
