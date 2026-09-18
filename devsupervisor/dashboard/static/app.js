/* DevSupervisor dashboard — read-only.
 *
 * Every string that comes from durable state is written with textContent. Job
 * titles, gate questions and blocker notes are operator data, not markup, and
 * this file never builds HTML out of them.
 *
 * Rendering reconciles rather than rebuilds: a node keeps its DOM element
 * across polls, so a pulse or a glow is not restarted every two seconds and a
 * state change animates from where it was.
 */
'use strict';

const STATE_MS = 2000;
const ACTIVITY_MS = 4000;
const PAGE = 50;

const el = (id) => document.getElementById(id);
const ui = {
  project: el('project'), projectList: el('project-list'), status: el('status'), git: el('git'), active: el('active'),
  gates: el('gates'), leases: el('leases'), spend: el('spend'), pipeline: el('pipeline'),
  current: el('current'), graph: el('graph'), wires: el('wires'),
  supervisor: el('tier-supervisor'), workers: el('tier-workers'), activity: el('activity'),
  more: el('more'), filters: el('filters'), detail: el('detail'),
  detailRole: el('detail-role'), detailTitle: el('detail-title'),
  detailState: el('detail-state'), detailBody: el('detail-body'), err: el('err'),
};

let project = new URLSearchParams(location.search).get('project') || '';
let filter = 'all';
let entries = [];
let activitySignature = '';
let lastState = { agents: [] };
let selectedJob = null;
let hotKey = null;

/* --- tiny DOM helpers: every value lands as text, never as markup --- */
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function setText(element, text) {
  const value = text === undefined || text === null ? '' : String(text);
  if (element.textContent !== value) element.textContent = value;
}

function setData(element, key, value) {
  if (element.dataset[key] !== value) element.dataset[key] = value;
}

/* Put `elements` under `parent` in that order, moving only what is out of
 * place. Re-inserting an element restarts its CSS animations, so an element
 * that is already where it belongs is not touched. */
function reconcile(parent, elements) {
  const keep = new Set(elements);
  Array.from(parent.children).forEach((child) => { if (!keep.has(child)) child.remove(); });
  elements.forEach((element, index) => {
    if (parent.children[index] !== element) parent.insertBefore(element, parent.children[index] || null);
  });
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
// The one place the client rewords the reader: the operator-facing headline
// for an open gate. The supervisor node says the same thing; the activity log
// keeps the state's own name.
const HUMAN_HEADLINE = 'HUMAN DECISION REQUIRED';
const STATUS_LABELS = { 'WAITING FOR HUMAN': HUMAN_HEADLINE };

function fact(element, value, label, tone) {
  element.replaceChildren();
  if (value === '' || value === null || value === undefined) { delete element.dataset.tone; return; }
  element.appendChild(node('b', null, value));
  if (label) element.appendChild(document.createTextNode(' ' + label));
  if (tone) element.dataset.tone = tone; else delete element.dataset.tone;
}

function renderHeader(state) {
  const status = state.status || 'OFFLINE';
  setData(ui.status, 'state', status);
  setText(ui.status.querySelector('.label'), STATUS_LABELS[status] || status);
  const git = state.git || {};
  ui.git.textContent = git.branch ? `${git.branch} @ ${git.sha || '—'}${git.clean === false ? ' *' : ''}` : '';
  // The server counts active jobs; the agent list it sends may be capped.
  const live = state.active_count || 0;
  fact(ui.active, live, 'active', live ? 'signal' : null);
  const gates = (state.gates || []).length;
  fact(ui.gates, gates || '', gates === 1 ? 'gate' : 'gates', gates ? 'hold' : null);
  fact(ui.leases, state.leases || 0, state.leases === 1 ? 'lease' : 'leases');
  const spend = state.spend || {};
  fact(ui.spend, typeof spend.project_usd === 'number' ? `$${spend.project_usd.toFixed(2)}` : '');
}

/* The project selector is a button and a listbox rather than a <select>: the
 * native popup is drawn by the OS and cannot sit in the console's theme. It
 * owes the full behaviour in return — arrows, Home/End, Enter, Escape,
 * click-outside, and focus returned to the button. */
function renderProjects(state) {
  const names = state.projects || [];
  const current = state.project ? state.project.id : '';
  const signature = names.map((p) => p.id + ':' + p.name).join(',') + '|' + current;
  if (ui.project.dataset.signature === signature) return;
  ui.project.dataset.signature = signature;
  const selected = names.find((p) => p.id === current);
  setText(ui.project, selected ? selected.name : current || '—');
  ui.projectList.replaceChildren();
  names.forEach((entry) => {
    const option = node('li', null, entry.name);
    option.setAttribute('role', 'option');
    option.setAttribute('aria-selected', entry.id === current ? 'true' : 'false');
    option.tabIndex = -1;
    option.dataset.id = entry.id;
    option.addEventListener('click', () => chooseProject(entry.id));
    ui.projectList.appendChild(option);
  });
  ui.project.disabled = names.length < 2;
  if (ui.project.disabled) closeMenu();
}

function openMenu() {
  if (ui.project.disabled) return;
  ui.projectList.hidden = false;
  ui.project.setAttribute('aria-expanded', 'true');
  const active = ui.projectList.querySelector('[aria-selected="true"]') || ui.projectList.firstElementChild;
  if (active) active.focus();
}

function closeMenu(refocus) {
  if (ui.projectList.hidden) return;
  ui.projectList.hidden = true;
  ui.project.setAttribute('aria-expanded', 'false');
  if (refocus) ui.project.focus();
}

function chooseProject(id) {
  closeMenu(true);
  if (id === project) return;
  project = id;
  resetForProject();
  const url = new URL(location.href);
  url.searchParams.set('project', project);
  history.replaceState(null, '', url);
  tick();
  tickActivity();
}

/* --- pipeline --- */
const STAGE_MARKS = { complete: '✓', active: '●', blocked: '◆' };
const stageNodes = new Map();

function renderPipeline(state) {
  const stages = state.pipeline || [];
  if (!stages.length) {
    stageNodes.clear();
    ui.pipeline.replaceChildren(node('span', 'empty', 'No planned work'));
    ui.current.replaceChildren();
    return;
  }
  const elements = stages.map((stage, index) => {
    let wrap = stageNodes.get(stage.name);
    if (!wrap) {
      wrap = node('div', 'stage');
      const name = node('span', 'name');
      name.appendChild(node('span', 'label', stage.name));
      name.appendChild(node('span', 'mark'));
      wrap.appendChild(name);
      wrap.appendChild(node('span', 'arrow'));
      stageNodes.set(stage.name, wrap);
    }
    setData(wrap, 'state', stage.state);
    setText(wrap.querySelector('.mark'), STAGE_MARKS[stage.state] || '○');
    wrap.querySelector('.arrow').hidden = index === stages.length - 1;
    return wrap;
  });
  stageNodes.forEach((element, name) => { if (!stages.some((s) => s.name === name)) stageNodes.delete(name); });
  reconcile(ui.pipeline, elements);
  ui.current.replaceChildren();
  if (state.current) {
    ui.current.appendChild(node('span', 'k', 'Current'));
    ui.current.appendChild(document.createTextNode(state.current));
  }
}

/* --- graph --- */
function elapsed(seconds) {
  if (seconds === null || seconds === undefined) return '';
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

const AGENT_MARKS = { ACTIVE: 'ACTIVE', IDLE: 'IDLE', STALE: 'STALLED', WAITING: 'WAITING',
  BLOCKED: 'BLOCKED', FAILED: 'FAILED', COMPLETE: 'COMPLETE' };

let supervisorBox = null;

function supervisorNode(state) {
  const sup = state.supervisor || {};
  const gated = state.status === 'WAITING FOR HUMAN';
  if (!supervisorBox) {
    supervisorBox = node('div', 'node supervisor');
    supervisorBox.appendChild(node('div', 'role', 'SUPERVISOR'));
    const line = node('div', 'state');
    line.appendChild(node('span', 'dot'));
    line.appendChild(node('span', 'label'));
    supervisorBox.appendChild(line);
    supervisorBox.appendChild(node('div', 'line'));
    supervisorBox.appendChild(node('div', 'gate-id'));
    supervisorBox.appendChild(node('div', 'note'));
  }
  const box = supervisorBox;
  box.classList.toggle('gate', gated);
  setData(box, 'status', gated ? 'WAITING' : sup.status === 'RUNNING' ? 'ACTIVE' : 'IDLE');
  setText(box.querySelector('.state .label'), gated ? HUMAN_HEADLINE : sup.status || '');
  setText(box.querySelector('.line'), sup.line || '');
  const gateId = box.querySelector('.gate-id');
  gateId.hidden = !(gated && sup.detail);
  setText(gateId, gated ? sup.detail || '' : '');
  const note = box.querySelector('.note');
  note.hidden = !sup.note && !(!gated && sup.detail);
  note.replaceChildren();
  if (gated && sup.note) {
    // "8 decisions pending": the count is the part worth a glance.
    const match = /^(\d+)\s+(.*)$/.exec(sup.note);
    if (match) {
      note.appendChild(node('b', null, match[1]));
      note.appendChild(document.createTextNode(' ' + match[2]));
    } else {
      note.textContent = sup.note;
    }
  } else if (!gated && sup.detail) {
    note.textContent = sup.detail;
  }
  return box;
}

const workerNodes = new Map();
const agentKey = (agent) => agent.id || `role:${agent.role}`;

function workerNode(agent) {
  const key = agentKey(agent);
  let box = workerNodes.get(key);
  if (!box) {
    box = node(agent.id ? 'button' : 'div', 'node');
    if (agent.id) {
      box.dataset.job = agent.id;
      box.type = 'button';
      box.addEventListener('click', () => showDetail(agent.id));
    }
    box.dataset.key = key;
    box.addEventListener('mouseenter', () => setHot(key));
    box.addEventListener('mouseleave', () => setHot(null));
    box.addEventListener('focus', () => setHot(key));
    box.addEventListener('blur', () => setHot(null));
    box.appendChild(node('div', 'role'));
    const state = node('div', 'state');
    state.appendChild(node('span', 'dot'));
    state.appendChild(node('span', 'label'));
    state.appendChild(node('span', 't'));
    box.appendChild(state);
    box.appendChild(node('div', 'line'));
    workerNodes.set(key, box);
  }
  setData(box, 'status', agent.status);
  setText(box.querySelector('.role'), agent.role || 'agent');
  setText(box.querySelector('.state .label'), AGENT_MARKS[agent.status] || agent.status);
  const time = elapsed(agent.elapsed_s);
  setText(box.querySelector('.state .t'), time ? `· ${time}` : '');
  const line = box.querySelector('.line');
  setText(line, agent.line || '');
  if (line.title !== (agent.line || '')) line.title = agent.line || '';
  box.classList.toggle('selected', !!agent.id && agent.id === selectedJob);
  if (agent.id) box.setAttribute('aria-pressed', agent.id === selectedJob ? 'true' : 'false');
  return box;
}

let moreBox = null;
let emptyBox = null;

function renderGraph(state) {
  reconcile(ui.supervisor, [supervisorNode(state)]);
  const agents = state.agents || [];
  const elements = agents.map(workerNode);
  const keys = new Set(agents.map(agentKey));
  workerNodes.forEach((_, key) => { if (!keys.has(key)) workerNodes.delete(key); });
  if (!agents.length) {
    if (!emptyBox) emptyBox = node('div', 'empty', 'No agents registered for this project');
    elements.push(emptyBox);
  }
  if (state.hidden_agents) {
    if (!moreBox) moreBox = node('div', 'node more-agents');
    setText(moreBox, `+${state.hidden_agents} more not shown`);
    elements.push(moreBox);
  }
  reconcile(ui.workers, elements);
  requestAnimationFrame(() => drawWires(state));
}

const wireNodes = new Map();
const SVG = 'http://www.w3.org/2000/svg';

function setAttrs(element, attrs) {
  Object.entries(attrs).forEach(([name, value]) => {
    const text = String(Math.round(value * 10) / 10);
    if (element.getAttribute(name) !== text) element.setAttribute(name, text);
  });
}

const WIRE_CLASS = { ACTIVE: 'live', WAITING: 'gate', STALE: 'stale', COMPLETE: 'done',
  FAILED: 'fault', BLOCKED: 'fault' };

function drawWires(state) {
  const box = ui.graph.getBoundingClientRect();
  ui.wires.setAttribute('viewBox', `0 0 ${box.width} ${box.height}`);
  const root = ui.supervisor.querySelector('.node');
  if (!root) { ui.wires.replaceChildren(); wireNodes.clear(); return; }
  const from = root.getBoundingClientRect();
  const origin = { x: from.left - box.left + from.width / 2, y: from.bottom - box.top };
  const positions = new Map();
  const elements = [];
  const seen = new Set();
  ui.workers.querySelectorAll('.node').forEach((worker) => {
    const rect = worker.getBoundingClientRect();
    const point = { x: rect.left - box.left + rect.width / 2, y: rect.top - box.top };
    const key = worker.dataset.key || 'more';
    positions.set(worker.dataset.job || '', { point, rect, worker });
    seen.add(key);
    let wire = wireNodes.get(key);
    if (!wire) {
      wire = { line: document.createElementNS(SVG, 'line'), pulse: null };
      wireNodes.set(key, wire);
    }
    const coords = { x1: origin.x, y1: origin.y, x2: point.x, y2: point.y };
    setAttrs(wire.line, coords);
    const status = worker.dataset.status;
    wire.line.setAttribute('class', [WIRE_CLASS[status] || '', key === hotKey ? 'hot' : '']
      .filter(Boolean).join(' '));
    elements.push(wire.line);
    // A signal travelling supervisor → worker, only while the worker is live.
    if (status === 'ACTIVE') {
      if (!wire.pulse) {
        wire.pulse = document.createElementNS(SVG, 'line');
        wire.pulse.setAttribute('class', 'pulse');
      }
      setAttrs(wire.pulse, coords);
      elements.push(wire.pulse);
    } else {
      wire.pulse = null;
    }
  });
  // Real delegation edges only: a reviewer to the work it reviews, a revision
  // to the attempt it replaces. Nothing decorative.
  (state.agents || []).forEach((agent) => {
    const target = agent.reviews || agent.revision_of;
    if (!agent.id || !target || !positions.has(agent.id) || !positions.has(target)) return;
    const a = positions.get(agent.id);
    const b = positions.get(target);
    // Arced above the row, into the gap under the supervisor: a straight line
    // between two nodes would cut through whichever unrelated nodes sit
    // between them, and a dip below the row would cross a second row.
    const ay = a.rect.top - box.top;
    const by = b.rect.top - box.top;
    const rise = Math.min(ay, by) - 30;
    const relKey = `rel:${agent.id}`;
    seen.add(relKey);
    let wire = wireNodes.get(relKey);
    if (!wire) {
      wire = { path: document.createElementNS(SVG, 'path') };
      wire.path.setAttribute('class', 'rel');
      wireNodes.set(relKey, wire);
    }
    const d = `M ${a.point.x} ${ay} C ${a.point.x} ${rise}, ${b.point.x} ${rise}, ${b.point.x} ${by}`;
    if (wire.path.getAttribute('d') !== d) wire.path.setAttribute('d', d);
    elements.push(wire.path);
  });
  wireNodes.forEach((_, key) => { if (!seen.has(key)) wireNodes.delete(key); });
  reconcile(ui.wires, elements);
}

function setHot(key) {
  hotKey = key;
  wireNodes.forEach((wire, wireKey) => {
    if (!wire.line) return;
    wire.line.classList.toggle('hot', wireKey === key);
  });
}

/* --- activity --- */
const MARKS = { ok: '✓', bad: '✕', gate: '◆', run: '→', muted: '·', warn: '↻' };

function localMoment(iso) {
  const when = new Date(iso);
  if (isNaN(when)) return String(iso);
  return when.toLocaleString([], {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
  });
}

function localTime(iso) {
  const when = new Date(iso);
  if (isNaN(when)) return (iso || '').slice(11, 16);
  return when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

const entryKey = (entry) => `${entry.at}|${entry.title}|${entry.note || ''}`;
let knownEntries = new Set();

function renderActivity() {
  ui.activity.replaceChildren();
  if (!entries.length) {
    ui.activity.appendChild(node('li', 'empty', 'Nothing recorded yet'));
    knownEntries = new Set();
    return;
  }
  // Only rows that were not on screen a moment ago slide in; the first page
  // and a filter change land at once.
  const animate = knownEntries.size > 0;
  const next = new Set();
  entries.forEach((entry) => {
    const key = entryKey(entry);
    next.add(key);
    const row = node('li', animate && !knownEntries.has(key) ? 'enter' : null);
    row.dataset.tone = entry.tone;
    const at = node('span', 'at', localTime(entry.at));
    at.title = entry.at || '';
    row.appendChild(at);
    row.appendChild(node('span', 'mark', MARKS[entry.tone] || '·'));
    const body = node('div');
    const title = node('div', 'title');
    const text = entry.title || '';
    if (entry.job_id && text.startsWith(entry.job_id + ' ')) {
      title.appendChild(node('span', 'id', entry.job_id));
      title.appendChild(node('span', 'verb', text.slice(entry.job_id.length + 1)));
    } else {
      title.textContent = text;
    }
    body.appendChild(title);
    if (entry.detail) body.appendChild(node('div', 'sub', entry.detail));
    if (entry.note) body.appendChild(node('div', 'note', entry.note));
    row.appendChild(body);
    ui.activity.appendChild(row);
  });
  knownEntries = next;
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
const DETAIL_SECTIONS = [
  ['', ['summary', 'action', 'job_type', 'risk']],
  ['work', ['branch', 'base_sha', 'result_sha', 'worktree']],
  ['run', ['model', 'effort', 'attempt', 'revision', 'cost_usd', 'started_at']],
  ['relations', ['reviews', 'revision_of']],
];
const DETAIL_LABELS = { cost_usd: 'cost', started_at: 'started', base_sha: 'base sha',
  result_sha: 'result sha', job_type: 'type', revision_of: 'revision of' };
const STATE_TONES = { RUNNING: 'signal', UNDER_REVIEW: 'signal', LANDING: 'signal',
  DISPATCHED: 'signal', REVISION_READY: 'signal', WAITING_HUMAN: 'hold', BLOCKED: 'fault',
  FAILED: 'fault', REJECTED: 'fault', DONE: 'landed', VERIFIED: 'landed', APPROVED: 'landed',
  EVALUATED: 'landed', WORK_COMPLETE: 'landed' };

function detailValue(key, value) {
  if (key.endsWith('_at')) return localMoment(value);
  // A cost is shown only when it is non-zero, so it must never round to $0.00.
  if (key === 'cost_usd') {
    const cost = Number(value);
    return `$${cost.toFixed(cost < 0.01 ? 4 : 2)}`;
  }
  return value;
}

async function showDetail(jobId) {
  try {
    const detail = await getJSON('/api/job?id=' + encodeURIComponent(jobId));
    selectedJob = detail.id || jobId;
    ui.detailRole.textContent = detail.role || '';
    ui.detailTitle.textContent = detail.id || jobId;
    ui.detailState.textContent = detail.state || '';
    ui.detailState.dataset.tone = STATE_TONES[detail.state] || '';
    ui.detailBody.replaceChildren();
    const shown = new Set();
    DETAIL_SECTIONS.forEach(([section, keys]) => {
      const present = keys.filter((key) => detail[key] !== null && detail[key] !== undefined && detail[key] !== '');
      if (!present.length) return;
      if (section) ui.detailBody.appendChild(node('dt', 'section', section));
      present.forEach((key) => {
        shown.add(key);
        ui.detailBody.appendChild(node('dt', null, DETAIL_LABELS[key] || key.replace(/_/g, ' ')));
        const dd = node('dd', key.endsWith('sha') ? 'sha' : null, detailValue(key, detail[key]));
        ui.detailBody.appendChild(dd);
      });
    });
    Object.entries(detail).forEach(([key, value]) => {
      if (['id', 'role', 'state', 'artifacts'].includes(key) || shown.has(key)) return;
      if (value === null || value === undefined || value === '') return;
      ui.detailBody.appendChild(node('dt', null, key.replace(/_/g, ' ')));
      ui.detailBody.appendChild(node('dd', null, detailValue(key, value)));
    });
    if ((detail.artifacts || []).length) ui.detailBody.appendChild(node('dt', 'section', 'artifacts'));
    (detail.artifacts || []).forEach((artifact) => {
      ui.detailBody.appendChild(node('dt', null, artifact.kind));
      ui.detailBody.appendChild(node('dd', null, artifact.summary));
    });
    ui.detail.hidden = false;
    renderGraph(lastState);
  } catch (error) {
    showError(error.message);
  }
}

function closeDetail() {
  if (ui.detail.hidden) return;
  ui.detail.hidden = true;
  selectedJob = null;
  renderGraph(lastState);
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

function resetForProject() {
  closeDetail();
  entries = [];
  activitySignature = '';
  knownEntries = new Set();
  workerNodes.clear();
  wireNodes.clear();
  stageNodes.clear();
  hotKey = null;
  lastState = { agents: [] };
}

ui.project.addEventListener('click', () => (ui.projectList.hidden ? openMenu() : closeMenu()));
ui.project.addEventListener('keydown', (event) => {
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); openMenu(); }
});
ui.projectList.addEventListener('keydown', (event) => {
  const options = Array.from(ui.projectList.children);
  const index = options.indexOf(document.activeElement);
  const focus = (i) => options[Math.max(0, Math.min(options.length - 1, i))].focus();
  switch (event.key) {
    case 'ArrowDown': event.preventDefault(); focus(index + 1); break;
    case 'ArrowUp': event.preventDefault(); focus(index - 1); break;
    case 'Home': event.preventDefault(); focus(0); break;
    case 'End': event.preventDefault(); focus(options.length - 1); break;
    case 'Enter': case ' ':
      event.preventDefault();
      if (index >= 0) chooseProject(options[index].dataset.id);
      break;
    case 'Escape': event.preventDefault(); closeMenu(true); break;
    case 'Tab': closeMenu(); break;
    default: break;
  }
});
document.addEventListener('pointerdown', (event) => {
  if (!ui.project.contains(event.target) && !ui.projectList.contains(event.target)) closeMenu();
});

ui.filters.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-kind]');
  if (!button) return;
  filter = button.dataset.kind;
  ui.filters.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b === button));
  entries = [];
  activitySignature = '';
  knownEntries = new Set();
  tickActivity();
});

ui.more.addEventListener('click', () => loadActivity(false).catch((e) => showError(e.message)));
el('detail-close').addEventListener('click', closeDetail);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && ui.projectList.hidden) closeDetail();
});
window.addEventListener('resize', () => drawWires(lastState));

tick();
tickActivity();
setInterval(tick, STATE_MS);
setInterval(tickActivity, ACTIVITY_MS);
