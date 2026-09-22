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
  attention: el('attention-list'), attentionSection: el('attention-section'),
  capacity: el('capacity'), capacityList: el('capacity-list'), capacityCount: el('capacity-count'),
  workingCount: el('working-count'), attentionCount: el('attention-count'),
  workflowTitle: el('workflow-title'), workflowId: el('workflow-id'), crumbProject: el('crumb-project'),
  workflowDescription: el('workflow-description'), sidebarWorkflowTitle: el('sidebar-workflow-title'),
  crumbSection: el('crumb-section'), agentLibraryPage: el('agent-library-page'),
  librarySearch: el('library-search'), agentProfileGrid: el('agent-profile-grid'),
  libraryProfileCount: el('library-profile-count'),
  workflowCount: el('workflow-count'), agentCount: el('agent-count'), laneCount: el('lane-count'),
  toolbarWorking: el('toolbar-working'), toolbarAttention: el('toolbar-attention'),
  toolbarWorktrees: el('toolbar-worktrees'), providerFilter: el('provider-filter'),
  lanes: el('lane-list'), laneAlert: el('lane-alert'), laneAlertText: el('lane-alert-text'),
  releaseGate: el('release-gate'), gateProgress: el('gate-progress'),
  library: el('library-dialog'), libraryKicker: el('library-kicker'),
  libraryTitle: el('library-title'), libraryCopy: el('library-copy'), libraryList: el('library-list'),
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
let providerFilter = 'all';
let libraryQuery = '';

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
  const goal = state.goal || {};
  setText(ui.workflowTitle, goal.title || (state.project && state.project.name) || 'No active workflow');
  setText(ui.workflowId, goal.id || '');
  setText(ui.sidebarWorkflowTitle, goal.title || 'No active workflow');
  setText(ui.workflowCount, goal.id ? 1 : 0);
  const active = state.active_count || 0;
  setText(ui.workflowDescription, goal.description || (active
    ? `One workflow coordinator, ${active} active assignment${active === 1 ? '' : 's'}, and explicit review handoffs.`
    : 'Durable workflow state, assignments, and review handoffs.'));
  setText(ui.agentCount, (state.agent_library || []).length);
  setText(ui.crumbProject, state.project ? state.project.name : 'Project');
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
    supervisorBox.appendChild(node('div', 'role', 'WORKFLOW COORDINATOR'));
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

// Operators write roles by hand, so "BUILDER" and "build" are the same role.
const BUILDER_ROLES = new Set(['build', 'builder']);
const isBuilder = (agent) => BUILDER_ROLES.has(String(agent.role || '').toLowerCase());
// Statuses past review: the handoff footer states what the ledger says, never
// a guess about a reviewer that has not been dispatched.
const REVIEWED = new Set(['APPROVED', 'LANDING_READY', 'LANDING', 'VERIFIED', 'EVALUATED', 'DONE']);
const AWAITING_REVIEW = new Set(['WORK_COMPLETE', 'UNDER_REVIEW']);

function handoffFor(agent, reviewers) {
  if (!agent.id || !isBuilder(agent) || agent.review_policy === 'none') return '';
  const reviewer = reviewers.get(agent.id);
  if (reviewer) return `Reviewer ${(AGENT_MARKS[reviewer.status] || reviewer.status).toLowerCase()}`;
  if (REVIEWED.has(agent.job_status)) return 'Approved';
  if (agent.job_status === 'REJECTED') return 'Rejected';
  if (AWAITING_REVIEW.has(agent.job_status)) return 'Awaiting a reviewer';
  return 'Required before landing';
}

function workerNode(agent, builderNumber, handoffLine) {
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
    const head = node('div', 'node-head');
    head.appendChild(node('div', 'role'));
    head.appendChild(node('span', 'instance'));
    box.appendChild(head);
    const state = node('div', 'state');
    state.appendChild(node('span', 'dot'));
    state.appendChild(node('span', 'label'));
    state.appendChild(node('span', 't'));
    box.appendChild(state);
    box.appendChild(node('div', 'line'));
    const runtime = node('div', 'runtime');
    const runtimeAgent = node('span', 'runtime-agent');
    runtimeAgent.appendChild(node('span', 'provider'));
    runtimeAgent.appendChild(node('span', 'model'));
    runtime.appendChild(runtimeAgent);
    runtime.appendChild(node('span', 'effort'));
    box.appendChild(runtime);
    box.appendChild(node('div', 'branch'));
    const handoff = node('div', 'handoff');
    handoff.appendChild(node('span', 'handoff-icon', '◇'));
    const handoffText = node('span');
    handoffText.appendChild(node('b', null, 'Independent review'));
    handoffText.appendChild(node('small'));
    handoff.appendChild(handoffText);
    box.appendChild(handoff);
    workerNodes.set(key, box);
  }
  setData(box, 'status', agent.status);
  const displayRole = isBuilder(agent) ? 'builder' : String(agent.role || 'agent').toLowerCase();
  setText(box.querySelector('.role'), displayRole);
  setText(box.querySelector('.instance'), builderNumber ? `B-${String(builderNumber).padStart(2, '0')}` : '');
  setText(box.querySelector('.state .label'), AGENT_MARKS[agent.status] || agent.status);
  const time = elapsed(agent.elapsed_s);
  setText(box.querySelector('.state .t'), time ? `· ${time}` : '');
  const line = box.querySelector('.line');
  setText(line, agent.title || agent.line || '');
  if (line.title !== (agent.line || '')) line.title = agent.line || '';
  const runtime = box.querySelector('.runtime');
  setText(runtime.querySelector('.provider'), agent.provider || '');
  setText(runtime.querySelector('.model'), agent.model || '');
  setText(runtime.querySelector('.effort'), agent.effort ? `${agent.effort} thinking` : '');
  runtime.hidden = !(agent.provider || agent.model || agent.effort);
  const branch = box.querySelector('.branch');
  setText(branch, agent.branch || '');
  branch.hidden = !agent.branch;
  const handoff = box.querySelector('.handoff');
  setText(handoff.querySelector('small'), handoffLine || '');
  handoff.hidden = !handoffLine;
  box.classList.toggle('selected', !!agent.id && agent.id === selectedJob);
  if (agent.id) box.setAttribute('aria-pressed', agent.id === selectedJob ? 'true' : 'false');
  return box;
}

let moreBox = null;
let emptyBox = null;

function renderGraph(state) {
  reconcile(ui.supervisor, [supervisorNode(state)]);
  const allAgents = state.agents || [];
  const agents = allAgents.filter((agent) => agent.status === 'IDLE' || providerFilter === 'all'
    || String(agent.provider || '').toLowerCase() === providerFilter);
  const active = agents.filter((agent) => !['IDLE', 'STALE', 'FAILED', 'BLOCKED', 'WAITING'].includes(agent.status));
  const attention = agents.filter((agent) => ['STALE', 'FAILED', 'BLOCKED', 'WAITING'].includes(agent.status));
  const idle = agents.filter((agent) => agent.status === 'IDLE');
  // Builders are numbered by job id so a card keeps its number while the
  // graph reorders around it.
  const builderNumbers = new Map();
  allAgents.filter((agent) => agent.id && isBuilder(agent))
    .map((agent) => agent.id).sort()
    .forEach((id, index) => builderNumbers.set(id, index + 1));
  const reviewers = new Map();
  allAgents.forEach((agent) => { if (agent.id && agent.reviews) reviewers.set(agent.reviews, agent); });
  const card = (agent) => workerNode(agent, builderNumbers.get(agent.id), handoffFor(agent, reviewers));
  const elements = active.map(card);
  const attentionElements = attention.map(card);
  const keys = new Set(active.concat(attention).map(agentKey));
  workerNodes.forEach((_, key) => { if (!keys.has(key)) workerNodes.delete(key); });
  if (!active.length && !attention.length) {
    if (!emptyBox) emptyBox = node('div', 'empty', 'No agents registered for this project');
    elements.push(emptyBox);
  }
  reconcile(ui.workers, elements);
  setData(ui.graph, 'layout', active.length ? 'workers' : 'solo');
  reconcile(ui.attention, attentionElements);
  ui.attentionSection.hidden = !attentionElements.length;
  setText(ui.workingCount, active.filter((agent) => agent.status === 'ACTIVE').length);
  setText(ui.attentionCount, attention.length);
  setText(ui.toolbarWorking, active.filter((agent) => agent.status === 'ACTIVE').length);
  setText(ui.toolbarAttention, attention.length);
  setText(ui.toolbarWorktrees, new Set(active.map((agent) => agent.branch).filter(Boolean)).size);
  setText(ui.laneCount, active.filter(isBuilder).length);

  const capacity = idle.map((agent) => node('span', 'capacity-role', agent.role));
  if (state.hidden_agents) {
    if (!moreBox) moreBox = node('span', 'capacity-role more-agents');
    setText(moreBox, `+${state.hidden_agents} more`);
    capacity.push(moreBox);
  }
  reconcile(ui.capacityList, capacity);
  ui.capacity.hidden = !capacity.length;
  setText(ui.capacityCount, capacity.length ? `${idle.length} idle${state.hidden_agents ? ` · ${state.hidden_agents} hidden` : ''}` : '');
  requestAnimationFrame(() => drawWires(state));
}

function renderProviderFilter(state) {
  const providers = Array.from(new Set((state.agents || [])
    .map((agent) => agent.provider).filter(Boolean).map((value) => String(value).toLowerCase()))).sort();
  const options = [node('option', null, 'All providers')];
  options[0].value = 'all';
  providers.forEach((provider) => {
    const option = node('option', null, provider);
    option.value = provider;
    options.push(option);
  });
  if (providerFilter !== 'all' && !providers.includes(providerFilter)) providerFilter = 'all';
  ui.providerFilter.replaceChildren(...options);
  ui.providerFilter.value = providerFilter;
  ui.providerFilter.closest('.provider-filter').hidden = providers.length < 2;
}

function renderLanes(state) {
  const visible = (state.agents || []).filter((agent) => agent.id && agent.status !== 'IDLE'
    && (providerFilter === 'all' || String(agent.provider || '').toLowerCase() === providerFilter));
  const agents = visible.filter(isBuilder);
  const attention = visible.filter((agent) => ['STALE', 'FAILED', 'BLOCKED', 'WAITING'].includes(agent.status));
  ui.laneAlert.hidden = !attention.length;
  if (attention.length) {
    const first = attention[0];
    setText(ui.laneAlertText, attention.length === 1
      ? `${first.title || first.line} · ${AGENT_MARKS[first.status] || first.status}`
      : `${attention.length} assignments need attention`);
  }
  if (!agents.length) {
    ui.lanes.replaceChildren(node('p', 'empty', 'No assignment lanes match this view'));
    return;
  }
  ui.lanes.replaceChildren(...agents.map((agent) => {
    const row = node('button', 'lane-row');
    row.type = 'button';
    row.addEventListener('click', () => showDetail(agent.id));
    const identity = node('span', 'lane-identity');
    identity.appendChild(node('strong', null, agent.title || agent.line));
    identity.appendChild(node('small', null, `${String(agent.role || 'agent').toLowerCase()} instance`));
    row.appendChild(identity);
    row.appendChild(node('span', 'lane-branch', agent.branch || 'No branch'));
    const runtime = node('span', 'lane-runtime');
    runtime.appendChild(node('strong', null, agent.provider || 'Unassigned provider'));
    runtime.appendChild(node('small', null, [agent.model, agent.effort].filter(Boolean).join(' · ')));
    row.appendChild(runtime);
    const status = node('span', 'lane-status', AGENT_MARKS[agent.status] || agent.status);
    status.dataset.status = agent.status;
    row.appendChild(status);
    row.appendChild(node('span', 'lane-arrow', '›'));
    return row;
  }));
}

function renderReleaseGate(state) {
  const builders = (state.agents || []).filter((agent) => agent.id && isBuilder(agent));
  ui.releaseGate.hidden = !builders.length;
  if (!builders.length) return;
  const reviewed = builders.filter((agent) => REVIEWED.has(agent.job_status)
    || agent.review_policy === 'none').length;
  setText(ui.gateProgress, `${reviewed} / ${builders.length} candidates cleared`);
}

const ROLE_COPY = {
  supervisor: 'Coordinates the workflow and dispatches bounded work.',
  planner: 'Turns goals into a dependency-aware execution plan.',
  architect: 'Defines boundaries and technical direction before implementation.',
  build: 'Implements a scoped change in an isolated worktree.',
  reviewer: 'Checks a candidate independently and returns a verdict.',
  specialist: 'Reviews a focused technical or domain concern.',
  security: 'Audits security-sensitive behavior and evidence.',
  benchmark: 'Measures behavior against a controlled baseline.',
  evaluator: 'Evaluates outcomes after verification.',
  researcher: 'Collects external or repository evidence for a decision.',
  investigator: 'Traces current behavior without changing the tree.',
  qa: 'Verifies acceptance behavior and regressions.',
  landing: 'Integrates an approved candidate into the target branch.',
  freeze: 'Records an approved artifact as authoritative.',
  operator: 'Performs bounded operational verification.',
};
const ROLE_NAMES = {
  supervisor: 'Workflow coordinator', build: 'Builder', qa: 'QA engineer',
  security: 'Security reviewer', landing: 'Landing agent', freeze: 'Freeze agent',
};

function roleName(role) {
  if (ROLE_NAMES[role]) return ROLE_NAMES[role];
  return String(role || 'Agent').replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase());
}

function libraryCard(profile) {
  const card = node('article', 'library-card');
  const head = node('div', 'library-card-head');
  head.appendChild(node('span', 'agent-glyph', profile.provider === 'codex' ? '›_' : '✳'));
  const title = node('span');
  title.appendChild(node('strong', null, profile.role));
  title.appendChild(node('small', null, ROLE_COPY[profile.role] || 'Reusable workflow role.'));
  head.appendChild(title);
  const count = node('span', 'instance-count', profile.instances
    ? `${profile.instances} live` : 'Available');
  head.appendChild(count);
  card.appendChild(head);
  const facts = node('div', 'library-facts');
  facts.appendChild(node('span', null, profile.provider || 'Policy provider'));
  facts.appendChild(node('span', null, profile.model || 'Policy model'));
  facts.appendChild(node('span', null, `${profile.effort || 'default'} thinking`));
  facts.appendChild(node('span', null, profile.access));
  if (profile.source) facts.appendChild(node('span', null, profile.source));
  card.appendChild(facts);
  return card;
}

function profileCard(profile) {
  const card = node('button', 'agent-profile-card');
  card.type = 'button';
  card.dataset.provider = profile.provider || 'policy';
  card.setAttribute('aria-label', `Inspect ${roleName(profile.role)} configuration`);
  card.addEventListener('click', () => showProfile(profile));
  const top = node('span', 'profile-top');
  top.appendChild(node('span', 'agent-glyph', profile.provider === 'codex' ? '›_' : '✳'));
  top.appendChild(node('span', 'profile-badge', profile.instances ? `${profile.instances} live` : 'Preset'));
  card.appendChild(top);
  card.appendChild(node('strong', 'profile-name', roleName(profile.role)));
  card.appendChild(node('span', 'profile-copy', ROLE_COPY[profile.role] || 'Reusable workflow role.'));
  const footer = node('span', 'profile-footer');
  const settings = [profile.provider || 'Policy provider', profile.model,
    profile.effort ? `${profile.effort} thinking` : ''].filter(Boolean).join(' · ');
  footer.appendChild(node('span', null, settings));
  footer.appendChild(node('span', 'profile-action', 'Inspect configuration ↗'));
  card.appendChild(footer);
  return card;
}

function renderAgentLibrary(state) {
  const profiles = state.agent_library || [];
  const query = libraryQuery.trim().toLowerCase();
  const shown = profiles.filter((profile) => [profile.role, profile.provider, profile.model,
    profile.effort, profile.access, ROLE_COPY[profile.role]].filter(Boolean).join(' ').toLowerCase().includes(query));
  setText(ui.libraryProfileCount, profiles.length);
  if (!shown.length) {
    ui.agentProfileGrid.replaceChildren(node('p', 'empty', 'No agent profiles match this search'));
    return;
  }
  ui.agentProfileGrid.replaceChildren(...shown.map(profileCard));
}

function showProfile(profile) {
  setText(ui.libraryKicker, 'Reusable profile');
  setText(ui.libraryTitle, `${roleName(profile.role)} configuration`);
  setText(ui.libraryCopy, ROLE_COPY[profile.role] || 'Reusable workflow role.');
  const details = node('dl', 'profile-details');
  const fields = [
    ['Provider', profile.provider || 'Resolved by routing policy'],
    ['Model', profile.model || 'Resolved by routing policy'],
    ['Thinking', profile.effort || 'Default'],
    ['Access', profile.access],
    ['Live instances', profile.instances],
    ['Source', profile.source],
  ];
  fields.forEach(([label, value]) => {
    details.appendChild(node('dt', null, label));
    details.appendChild(node('dd', null, value));
  });
  ui.libraryList.replaceChildren(details);
  if (typeof ui.library.showModal === 'function') ui.library.showModal();
  else ui.library.setAttribute('open', '');
}

function showLibrary(kind) {
  const assignments = kind === 'assignments';
  setText(ui.libraryKicker, assignments ? 'Current workflow' : 'Reusable profiles');
  setText(ui.libraryTitle, assignments ? 'Agent assignments' : 'Agent library');
  setText(ui.libraryCopy, assignments
    ? 'Each row is a separate agent instance with its own task and runtime settings.'
    : 'Roles define how agents work. Live assignments are separate instances of these reusable profiles.');
  if (assignments) {
    const profiles = (lastState.agents || []).filter((agent) => agent.id).map((agent) => ({
      role: agent.role, provider: agent.provider, model: agent.model, effort: agent.effort,
      access: agent.branch || 'No branch', instances: 1,
    }));
    ui.libraryList.replaceChildren(...profiles.map(libraryCard));
  } else {
    ui.libraryList.replaceChildren(...(lastState.agent_library || []).map(libraryCard));
  }
  if (typeof ui.library.showModal === 'function') ui.library.showModal();
  else ui.library.setAttribute('open', '');
}

function closeLibrary() {
  if (ui.library.open && typeof ui.library.close === 'function') ui.library.close();
  else ui.library.removeAttribute('open');
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
// The colour each end of an edge takes from the node it touches.
const TONE = { ACTIVE: 'signal', WAITING: 'hold', STALE: 'hold', COMPLETE: 'landed',
  FAILED: 'fault', BLOCKED: 'fault', IDLE: 'dormant' };
// Where along the edge the supervisor's colour gives way to the worker's.
const GRADIENT_STOPS = [[0, 'from'], [0.15, 'from'], [0.4, 'to'], [1, 'to']];

let wireDefs = null;

function gradientFor(key) {
  const gradient = document.createElementNS(SVG, 'linearGradient');
  gradient.id = 'wire-' + key.replace(/[^A-Za-z0-9_-]/g, '_');
  gradient.setAttribute('gradientUnits', 'userSpaceOnUse');
  GRADIENT_STOPS.forEach(([offset, end]) => {
    const stop = document.createElementNS(SVG, 'stop');
    stop.setAttribute('offset', String(offset));
    stop.dataset.end = end;
    gradient.appendChild(stop);
  });
  return gradient;
}

function paintGradient(gradient, coords, fromTone, toTone) {
  setAttrs(gradient, coords);
  gradient.querySelectorAll('stop').forEach((stop) => {
    const tone = stop.dataset.end === 'from' ? fromTone : toTone;
    const colour = `var(--${tone})`;
    if (stop.style.stopColor !== colour) stop.style.stopColor = colour;
  });
}

function drawWires(state) {
  const box = ui.graph.getBoundingClientRect();
  ui.wires.setAttribute('viewBox', `0 0 ${box.width} ${box.height}`);
  const root = ui.supervisor.querySelector('.node');
  if (!root) { ui.wires.replaceChildren(); wireNodes.clear(); return; }
  if (!wireDefs) wireDefs = document.createElementNS(SVG, 'defs');
  const from = root.getBoundingClientRect();
  const origin = { x: from.left - box.left + from.width / 2, y: from.bottom - box.top };
  const supervisorTone = TONE[root.dataset.status] || 'dormant';
  const positions = new Map();
  const elements = [wireDefs];
  const gradients = [];
  const seen = new Set();
  const workers = Array.from(ui.workers.querySelectorAll('.node'));
  // Every wire bends in the gap under the coordinator and arrives vertically,
  // so one bound for a node in a later row drops behind the row above it
  // instead of slicing diagonally across its neighbours.
  const firstRowTop = Math.min(...workers.map((worker) => worker.getBoundingClientRect().top - box.top));
  const bend = (origin.y + firstRowTop) / 2;
  workers.forEach((worker) => {
    const rect = worker.getBoundingClientRect();
    const point = { x: rect.left - box.left + rect.width / 2, y: rect.top - box.top };
    const key = worker.dataset.key || 'more';
    positions.set(worker.dataset.job || '', { point, rect, worker });
    seen.add(key);
    let wire = wireNodes.get(key);
    if (!wire) {
      wire = { line: document.createElementNS(SVG, 'path'), gradient: gradientFor(key) };
      wire.line.style.stroke = `url(#${wire.gradient.id})`;
      wireNodes.set(key, wire);
    }
    const coords = { x1: origin.x, y1: origin.y, x2: point.x, y2: point.y };
    // The curve lands on the first row; a node in a later row gets a straight
    // drop from there, which only shows in the gap between rows.
    const d = `M ${origin.x} ${origin.y} C ${origin.x} ${bend}, ${point.x} ${bend}, ${point.x} ${firstRowTop}`
      + (point.y - firstRowTop > 1 ? ` V ${point.y}` : '');
    if (wire.line.getAttribute('d') !== d) wire.line.setAttribute('d', d);
    const status = worker.dataset.status;
    const classes = [WIRE_CLASS[status] || '', key === hotKey ? 'hot' : ''].filter(Boolean).join(' ');
    wire.line.setAttribute('class', classes);
    wire.gradient.setAttribute('class', classes);
    paintGradient(wire.gradient, coords, supervisorTone, TONE[status] || 'dormant');
    gradients.push(wire.gradient);
    elements.push(wire.line);
  });
  reconcile(wireDefs, gradients);
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
    let d;
    if (Math.abs(ay - by) > 12) {
      // Different rows: the arc above the upper row would cut through it, so
      // run through the gap between the rows instead.
      const [lower, upper] = ay > by ? [a, b] : [b, a];
      const y1 = lower.rect.top - box.top;
      const y2 = upper.rect.bottom - box.top;
      const mid = (y1 + y2) / 2;
      d = `M ${lower.point.x} ${y1} C ${lower.point.x} ${mid}, ${upper.point.x} ${mid}, ${upper.point.x} ${y2}`;
    } else {
      d = `M ${a.point.x} ${ay} C ${a.point.x} ${rise}, ${b.point.x} ${rise}, ${b.point.x} ${by}`;
    }
    const relKey = `rel:${agent.id}`;
    seen.add(relKey);
    let wire = wireNodes.get(relKey);
    if (!wire) {
      wire = { path: document.createElementNS(SVG, 'path') };
      wire.path.setAttribute('class', 'rel');
      wireNodes.set(relKey, wire);
    }
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
    wire.gradient.classList.toggle('hot', wireKey === key);
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
    renderAgentLibrary(state);
    renderPipeline(state);
    renderProviderFilter(state);
    renderGraph(state);
    renderLanes(state);
    renderReleaseGate(state);
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
  providerFilter = 'all';
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
ui.providerFilter.addEventListener('change', () => {
  providerFilter = ui.providerFilter.value;
  renderGraph(lastState);
  renderLanes(lastState);
});
el('assignments-open').addEventListener('click', () => showLibrary('assignments'));
el('library-close').addEventListener('click', closeLibrary);
el('library-done').addEventListener('click', closeLibrary);
el('current-workflow-link').addEventListener('click', () => selectView('graph'));
ui.librarySearch.addEventListener('input', () => {
  libraryQuery = ui.librarySearch.value;
  renderAgentLibrary(lastState);
});
el('detail-close').addEventListener('click', closeDetail);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && ui.projectList.hidden) {
    closeDetail();
    closeLibrary();
  }
});
window.addEventListener('resize', () => drawWires(lastState));

function selectView(view) {
  const main = document.querySelector('.app-main');
  main.dataset.view = view;
  setText(ui.crumbSection, view === 'library' ? 'Agent library' : view === 'activity' ? 'Run history' : 'Workflow');
  document.querySelectorAll('.view-tab').forEach((button) => {
    const selected = button.dataset.view === view;
    button.classList.toggle('on', selected);
    button.setAttribute('aria-selected', selected ? 'true' : 'false');
  });
  document.querySelectorAll('.nav-item').forEach((button) => {
    const selected = button.dataset.section === (view === 'library' ? 'agents'
      : view === 'activity' ? 'activity' : 'workflow');
    button.classList.toggle('on', selected);
    if (selected) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  if (view === 'graph') requestAnimationFrame(() => drawWires(lastState));
}

document.querySelectorAll('.view-tab').forEach((button) => {
  button.addEventListener('click', () => selectView(button.dataset.view));
});
document.querySelectorAll('.nav-item').forEach((button) => {
  button.addEventListener('click', () => selectView(button.dataset.section === 'agents'
    ? 'library' : button.dataset.section === 'activity' ? 'activity' : 'graph'));
});

tick();
tickActivity();
setInterval(tick, STATE_MS);
setInterval(tickActivity, ACTIVITY_MS);
