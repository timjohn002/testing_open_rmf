'use strict';

const POLL_MS = 2000;
const FLEET_COLORS = ['#2563eb', '#d97706', '#7c3aed', '#0d9488', '#db2777', '#65a30d'];
const ACTIVE_TASK = new Set(['queued', 'standby', 'underway', 'blocked', 'pending']);

const state = { fleets: [], site: { places: [], lanes: [] }, route: [] };
const robotRows = new Map(); // "fleet/robot" -> row elements

const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c);
  return node;
}

function toast(msg, isError = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = `toast show${isError ? ' err' : ''}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.className = 'toast'), 3500);
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new Error(errorText(data) || `${res.status} ${res.statusText}`);
  return data;
}

function errorText(data) {
  if (!data) return '';
  const d = data.detail ?? data;
  if (typeof d === 'string') return d;
  const errs = d.errors || d.root?.errors || d.commission?.errors;
  if (Array.isArray(errs) && errs.length) return errs.map((e) => e.detail || e.category).join('; ');
  return JSON.stringify(d).slice(0, 200);
}

// A robot task response nests success under .root (and sometimes .root.root).
function taskResult(resp) {
  let r = resp;
  while (r && r.root) r = r.root;
  if (r && r.success === false) throw new Error(errorText(r) || 'Rejected by RMF');
  return r?.state?.booking?.id;
}

// --- Summary -----------------------------------------------------------------
function renderSummary() {
  const robots = state.fleets.flatMap((f) => f.robots);
  const count = (s) => robots.filter((r) => r.status === s).length;
  const offline = robots.filter((r) => ['offline', 'shutdown', 'uninitialized'].includes(r.status)).length;
  const stats = [
    ['Fleets', state.fleets.length],
    ['Robots', robots.length],
    ['Working', count('working')],
    ['Idle', count('idle')],
    ['Charging', count('charging')],
    ['Error / offline', count('error') + offline],
  ];
  $('#summary').replaceChildren(...stats.map(([label, value]) =>
    el('div', { class: 'stat' }, el('div', { class: 'label' }, label), el('div', { class: 'value' }, String(value)))));
}

// --- Robot list ----------------------------------------------------------------
function placeOptions(select, keepValue = true) {
  const prev = select.value;
  select.replaceChildren(...state.site.places.map((p) => el('option', { value: p.name }, p.name)));
  if (keepValue && prev) select.value = prev;
}

function makeRobotRow(fleet, robot) {
  const key = `${fleet}/${robot.name}`;
  const status = el('span', { class: 'pill' });
  const fill = el('div', { class: 'fill' });
  const pct = el('span');
  const where = el('div', { class: 'sub' });
  const task = el('div', { class: 'sub' });
  const place = el('select', { 'aria-label': `Destination for ${robot.name}` });
  placeOptions(place, false);

  const go = el('button', {
    class: 'secondary', type: 'button',
    onclick: async () => {
      if (!place.value) return;
      go.disabled = true;
      try {
        const id = taskResult(await api('POST', `/api/robots/${enc(fleet)}/${enc(robot.name)}/go_to`, { place: place.value }));
        toast(`${robot.name} → ${place.value}${id ? ` (${id})` : ''}`);
        refreshTasks();
      } catch (e) { toast(`Go to failed: ${e.message}`, true); }
      go.disabled = false;
    },
  }, 'Go');

  const cancel = el('button', {
    class: 'danger', type: 'button',
    onclick: async () => {
      const id = rows.current.task_id;
      if (!id || !confirm(`Cancel task ${id} on ${robot.name}?`)) return;
      try { await api('POST', `/api/tasks/${enc(id)}/cancel`); toast(`Cancel requested for ${id}`); }
      catch (e) { toast(`Cancel failed: ${e.message}`, true); }
    },
  }, 'Cancel task');

  const commission = el('button', {
    class: 'ghost', type: 'button',
    onclick: async () => {
      const on = rows.current.commissioned;
      const verb = on ? 'decommission' : 'recommission';
      if (on && !confirm(`Take ${robot.name} out of service? Queued tasks will be reassigned.`)) return;
      try {
        await api('POST', `/api/robots/${enc(fleet)}/${enc(robot.name)}/${verb}`, on ? { reassign_tasks: true } : undefined);
        toast(`${robot.name}: ${verb} requested`);
      } catch (e) { toast(`${verb} failed: ${e.message}`, true); }
    },
  }, '');

  const row = el('div', { class: 'robot' },
    el('div', {}, el('div', { class: 'name' }, robot.name), where),
    el('div', {}, status),
    el('div', {}, el('div', { class: 'battery' }, el('div', { class: 'bar' }, fill), pct), task),
    el('div', { class: 'actions' }, place, go, cancel, commission));

  const rows = { row, status, fill, pct, where, task, place, cancel, commission, current: robot };
  robotRows.set(key, rows);
  return rows;
}

function updateRobotRow(rows, robot) {
  rows.current = robot;
  const s = robot.status || 'uninitialized';
  rows.status.textContent = s;
  rows.status.className = `pill s-${s}`;
  const b = typeof robot.battery === 'number' ? Math.round(robot.battery * 100) : null;
  rows.pct.textContent = b === null ? '–' : `${b}%`;
  rows.fill.style.width = `${b ?? 0}%`;
  rows.fill.className = `fill${b !== null && b < 15 ? ' crit' : b !== null && b < 30 ? ' low' : ''}`;
  const loc = robot.location;
  rows.where.textContent = loc ? `${loc.map} · ${loc.x.toFixed(1)}, ${loc.y.toFixed(1)}` : 'no location';
  const issues = robot.issues.length ? ` · ⚠ ${robot.issues.length} issue(s)` : '';
  rows.task.textContent = (robot.task_id ? `task ${robot.task_id}` : 'no task') + issues;
  rows.task.title = robot.issues.map((i) => `${i.category}: ${JSON.stringify(i.detail)}`).join('\n');
  rows.cancel.disabled = !robot.task_id;
  rows.commission.textContent = robot.commissioned ? 'Take offline' : 'Put in service';
}

let fleetLayout = '';

function renderFleets() {
  const container = $('#fleets');
  if (!state.fleets.length) {
    container.replaceChildren(el('div', { class: 'empty' },
      'No fleets reported yet. Start the fleet adapters and check they are connected to the api-server.'));
    robotRows.clear();
    fleetLayout = '';
    return;
  }
  const layout = state.fleets.map((f) => `${f.name}:${f.robots.map((r) => r.name).join(',')}`).join('|');
  for (const fleet of state.fleets) {
    for (const robot of fleet.robots) {
      const rows = robotRows.get(`${fleet.name}/${robot.name}`) || makeRobotRow(fleet.name, robot);
      updateRobotRow(rows, robot);
    }
  }
  // Only rebuild the DOM when robots appear/disappear, so an open dropdown
  // is not closed by the 2 s refresh.
  if (layout === fleetLayout) return;
  fleetLayout = layout;
  const seen = new Set();
  container.replaceChildren(...state.fleets.map((fleet) => {
    const section = el('div', { class: 'fleet' }, el('div', { class: 'fleet-name' }, fleet.name));
    for (const robot of fleet.robots) {
      const key = `${fleet.name}/${robot.name}`;
      seen.add(key);
      section.append(robotRows.get(key).row);
    }
    return section;
  }));
  for (const key of [...robotRows.keys()]) if (!seen.has(key)) robotRows.delete(key);
}

// --- Site plan (plain 2D plot, not a simulation) -------------------------------
function renderPlan() {
  const svg = $('#plan');
  const { places, lanes } = state.site;
  const robots = state.fleets.flatMap((f, i) =>
    f.robots.filter((r) => r.location).map((r) => ({ ...r, fleet: f.name, color: FLEET_COLORS[i % FLEET_COLORS.length] })));
  const xs = [...places.map((p) => p.x), ...lanes.flatMap((l) => [l.x1, l.x2]), ...robots.map((r) => r.location.x)];
  const ys = [...places.map((p) => p.y), ...lanes.flatMap((l) => [l.y1, l.y2]), ...robots.map((r) => r.location.y)];
  if (!xs.length) { svg.replaceChildren(); return; }
  const pad = 2.5;
  const minX = Math.min(...xs) - pad, maxX = Math.max(...xs) + pad;
  const minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + pad;
  // RMF uses y-up; SVG is y-down, so flip y.
  svg.setAttribute('viewBox', `${minX} ${-maxY} ${maxX - minX} ${maxY - minY}`);
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  // metres per screen pixel, so marks and labels keep a constant on-screen size
  const box = svg.getBoundingClientRect();
  const scale = Math.max((maxX - minX) / (box.width || 600), (maxY - minY) / (box.height || 320));
  const ns = 'http://www.w3.org/2000/svg';
  const mk = (tag, attrs, text) => {
    const n = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text) n.textContent = text;
    return n;
  };
  const nodes = [];
  for (const l of lanes) nodes.push(mk('line', { class: 'lane', x1: l.x1, y1: -l.y1, x2: l.x2, y2: -l.y2, 'stroke-width': 3 * scale }));
  for (const p of places) {
    nodes.push(mk('circle', { class: `place${p.is_charger ? ' charger' : ''}`, cx: p.x, cy: -p.y, r: 4 * scale }));
    nodes.push(mk('text', { class: 'place-label', x: p.x + 6 * scale, y: -p.y + 14 * scale, 'font-size': 11 * scale }, p.name));
  }
  for (const r of robots) {
    const { x, y, yaw = 0 } = r.location;
    const g = mk('g', {});
    g.append(mk('title', {}, `${r.fleet}/${r.name} · ${r.status}`));
    g.append(mk('circle', { class: 'robot-dot', cx: x, cy: -y, r: 8 * scale, fill: r.color, 'stroke-width': 2 * scale }));
    g.append(mk('line', { x1: x, y1: -y, x2: x + Math.cos(yaw) * 12 * scale, y2: -y - Math.sin(yaw) * 12 * scale, stroke: r.color, 'stroke-width': 2.5 * scale }));
    g.append(mk('text', { class: 'robot-label', x: x + 10 * scale, y: -y - 10 * scale, 'font-size': 11 * scale }, r.name));
    nodes.push(g);
  }
  svg.replaceChildren(...nodes);
  $('#legend').replaceChildren(...state.fleets.map((f, i) =>
    el('span', {}, el('i', { style: `background:${FLEET_COLORS[i % FLEET_COLORS.length]}` }), f.name)));
}

// --- Patrol dispatch -----------------------------------------------------------
function renderRoute() {
  const route = $('#route');
  if (!state.route.length) { route.replaceChildren(el('span', { class: 'muted' }, 'Add places below')); return; }
  route.replaceChildren(...state.route.flatMap((p, i) =>
    i ? [el('span', { class: 'muted' }, '→'), el('span', { class: 'chip' }, p)] : [el('span', { class: 'chip' }, p)]));
}

let targetLayout = null;

function renderTargets() {
  const select = $('#patrol-target');
  if (fleetLayout === targetLayout) return;
  targetLayout = fleetLayout;
  const prev = select.value;
  const opts = [el('option', { value: '' }, 'Best available robot (any fleet)')];
  for (const f of state.fleets) {
    opts.push(el('option', { value: `${f.name}/` }, `Any robot in ${f.name}`));
    for (const r of f.robots) opts.push(el('option', { value: `${f.name}/${r.name}` }, `  ${f.name} / ${r.name}`));
  }
  select.replaceChildren(...opts);
  select.value = prev;
  if (select.value !== prev) select.value = '';
}

$('#add-place').addEventListener('click', () => {
  const v = $('#patrol-place').value;
  if (v) { state.route.push(v); renderRoute(); }
});
$('#clear-route').addEventListener('click', () => { state.route = []; renderRoute(); });
$('#patrol-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  if (!state.route.length) { toast('Add at least one place to the route', true); return; }
  const [fleet, robot] = ($('#patrol-target').value || '/').split('/');
  const body = { places: state.route, rounds: Number($('#patrol-rounds').value) || 1, fleet: fleet || null, robot: robot || null };
  try {
    const resp = await api('POST', '/api/tasks/patrol', body);
    const id = taskResult(resp);
    toast(`Patrol dispatched${id ? `: ${id}` : ''}`);
    state.route = []; renderRoute(); refreshTasks();
  } catch (e) { toast(`Dispatch failed: ${e.message}`, true); }
});

// --- Tasks ---------------------------------------------------------------------
async function refreshTasks() {
  let tasks;
  try { tasks = await api('GET', '/api/tasks?limit=25'); } catch { return; }
  const body = $('#tasks');
  if (!tasks.length) { body.replaceChildren(el('tr', {}, el('td', { colspan: 5, class: 'empty' }, 'No tasks yet'))); return; }
  body.replaceChildren(...tasks.map((t) => {
    const id = t.booking?.id || '';
    const status = t.status || 'unknown';
    const when = t.booking?.unix_millis_request_time ? new Date(t.booking.unix_millis_request_time).toLocaleTimeString() : '–';
    const robot = t.assigned_to ? `${t.assigned_to.group}/${t.assigned_to.name}` : '–';
    const cancel = el('button', {
      class: 'ghost', type: 'button',
      onclick: async () => {
        try { await api('POST', `/api/tasks/${enc(id)}/cancel`); toast(`Cancel requested for ${id}`); refreshTasks(); }
        catch (e) { toast(`Cancel failed: ${e.message}`, true); }
      },
    }, 'Cancel');
    if (!ACTIVE_TASK.has(status)) cancel.disabled = true;
    return el('tr', {},
      el('td', { class: 'id' }, el('span', { class: 'cat' }, t.category || ''), id),
      el('td', {}, robot), el('td', {}, status), el('td', {}, when), el('td', {}, cancel));
  }));
}

// --- Polling -------------------------------------------------------------------
const enc = encodeURIComponent;

function setConn(ok, text) {
  $('#conn').className = `conn ${ok ? 'ok' : 'bad'}`;
  $('#conn-text').textContent = text;
}

async function refreshFleets() {
  try {
    state.fleets = await api('GET', '/api/fleets');
    setConn(true, 'Connected to Open-RMF');
    $('#updated').textContent = `updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    setConn(false, `RMF unavailable: ${e.message}`);
  }
  renderSummary();
  renderFleets();
  renderPlan();
  renderTargets();
}

async function init() {
  try { state.site = await api('GET', '/api/site'); } catch (e) { toast(`Could not load site: ${e.message}`, true); }
  placeOptions($('#patrol-place'), false);
  renderRoute();
  await refreshFleets();
  refreshTasks();
  setInterval(refreshFleets, POLL_MS);
  setInterval(refreshTasks, POLL_MS * 2);
}

init();
