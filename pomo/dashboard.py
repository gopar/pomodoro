#!/usr/bin/env python3
"""Web dashboard for pomo sessions. Stdlib only (http.server).

Serves a single-page app (HTML/CSS/JS) that calls the pomo-server JSON API
directly from the browser. The dashboard server itself is a thin static-file
server — it does not touch the database.

Config:
    agent.toml:  dashboard_port = 9090
    env:         POMO_DASHBOARD_PORT (overrides config)
"""

from __future__ import annotations

import contextlib
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if sys.version_info < (3, 11):
    sys.exit(f"Error: Python 3.11+ required (current: {sys.version.split()[0]})")

from pomo import common

_STYLE = r"""
:root {
  --bg: #fff;
  --fg: #222;
  --muted: #888;
  --border: #ddd;
  --accent: #d94f4f;
  --accent-soft: #fde8e8;
  --green: #2e8b57;
  --green-soft: #e8f5e9;
  --amber: #d97706;
  --amber-soft: #fef3c7;
  --card-bg: #f8f8f8;
  --input-bg: #fff;
  --hover: #f0f0f0;
  --danger: #c00;
  --radius: 6px;
}
[data-theme="dark"] {
  --bg: #1a1a2e;
  --fg: #e0e0e0;
  --muted: #999;
  --border: #333;
  --accent: #e57373;
  --accent-soft: #3a1a1a;
  --green: #66bb6a;
  --green-soft: #1a3a1a;
  --amber: #ffb74d;
  --amber-soft: #3a2a1a;
  --card-bg: #22223a;
  --input-bg: #2a2a3e;
  --hover: #2a2a3e;
  --danger: #f44;
}

* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg);
  color: var(--fg);
  max-width: 900px;
  margin: 0 auto;
  padding: 24px 16px;
  transition: background 0.2s, color 0.2s;
}

/* Nav */
nav {
  display: flex; align-items: center; gap: 8px; margin-bottom: 24px;
  border-bottom: 1px solid var(--border); padding-bottom: 12px;
}
nav h1 { font-size: 18px; font-weight: 600; margin-right: auto; }
nav button, nav a {
  background: none; border: 1px solid var(--border); color: var(--fg);
  padding: 6px 12px; border-radius: var(--radius); cursor: pointer;
  font-size: 13px; text-decoration: none;
}
nav button.active { background: var(--accent); color: #fff; border-color: var(--accent); }
nav button:hover:not(.active) { background: var(--hover); }

/* Layout */
.tab { display: none; }
.tab.active { display: block; }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 16px; }
.card {
  background: var(--card-bg); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 16px; margin-bottom: 16px;
}

/* Inputs */
input, select, button.btn {
  padding: 8px 12px; border: 1px solid var(--border); border-radius: var(--radius);
  background: var(--input-bg); color: var(--fg); font-size: 13px;
}
button.btn {
  cursor: pointer; font-weight: 500;
}
button.btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
button.btn.danger { background: var(--danger); color: #fff; border-color: var(--danger); }
button.btn.small { padding: 4px 8px; font-size: 12px; }

/* Live */
.pomodoro-badge { color: var(--accent); }
.break-badge { color: var(--green); }
.overtime-badge { color: var(--amber); }
.countdown { font-size: 72px; font-weight: 700; text-align: center; margin: 24px 0; font-variant-numeric: tabular-nums; }
.meta { text-align: center; color: var(--muted); margin-bottom: 16px; }
.controls { display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; }
.controls input { width: 80px; }

/* Tables */
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border); }
th { font-weight: 600; color: var(--muted); }
tr:hover { background: var(--hover); }
td.actions { white-space: nowrap; gap: 4px; display: flex; }
td.editable { cursor: pointer; }
td.editable:hover { background: var(--hover); border-radius: var(--radius); }

/* Stats bars */
.stat-number { font-size: 36px; font-weight: 700; }
.stat-label { color: var(--muted); font-size: 13px; }
.bar-wrap { background: var(--border); border-radius: 3px; height: 20px; overflow: hidden; }
.bar-fill { height: 100%; background: var(--accent); border-radius: 3px; transition: width 0.3s; }

/* Theme toggle */
#theme-toggle { cursor: pointer; font-size: 16px; }
.error { color: var(--danger); text-align: center; padding: 16px; }
.loading { text-align: center; color: var(--muted); padding: 32px; }
.flash { animation: flash 1s ease-in-out infinite alternate; }
@keyframes flash { from { opacity: 1; } to { opacity: 0.4; } }
"""

_SCRIPT = r"""
const API = localStorage.getItem('pomo_api_url') || 'http://127.0.0.1:8787';

// Theme
const themeToggle = document.getElementById('theme-toggle');
const savedTheme = localStorage.getItem('pomo_theme') || 'light';
document.documentElement.setAttribute('data-theme', savedTheme);
themeToggle.textContent = savedTheme === 'dark' ? '\u263e' : '\u2600';
themeToggle.onclick = () => {
  const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('pomo_theme', next);
  themeToggle.textContent = next === 'dark' ? '\u263e' : '\u2600';
};

// Navigation
const tabs = document.querySelectorAll('.nav-tab');
const panels = document.querySelectorAll('.tab');
tabs.forEach(t => t.onclick = () => {
  tabs.forEach(x => x.classList.remove('active'));
  panels.forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  document.getElementById(t.dataset.tab).classList.add('active');
  if (t.dataset.tab === 'live') mountLive();
  if (t.dataset.tab === 'history') mountHistory();
  if (t.dataset.tab === 'stats') mountStats();
});

async function api(method, path, body) {
  const headers = { 'Accept': 'application/json' };
  const tok = localStorage.getItem('pomo_token');
  if (tok) headers['Authorization'] = 'Bearer ' + tok;
  if (body) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(body); }
  const res = await fetch(API + path, { method, headers, body });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

function fmtTime(secs) {
  const s = Math.abs(secs);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  const parts = h ? [h, m, ss] : [m, ss];
  return (secs < 0 ? '+' : '') + parts.map(x => String(x).padStart(2, '0')).join(':');
}

function dateStr(epoch) { return new Date(epoch * 1000).toISOString().slice(0, 10); }
function timeStr(epoch) { return new Date(epoch * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }

function stateClass(state) {
  if (state === 'pomodoro' || state === 'overtime') return 'pomodoro-badge';
  if (state === 'break' || state === 'break-overtime') return 'break-badge';
  return '';
}
function stateLabel(s) {
  if (s === 'break-overtime') return 'break overtime';
  return s || '';
}

// -------- Live ----------
let liveTimer;
async function mountLive() {
  clearInterval(liveTimer);
  const render = async () => {
    try {
      const cur = await api('GET', '/current');
      const el = document.getElementById('live-panel');
      if (cur.state === 'idle') {
        el.innerHTML = '<div class="countdown" style="font-size:24px;color:var(--muted)">No active session</div><div class="controls" id="live-ctrls"></div>';
        renderControls();
      } else {
        const now = Math.floor(Date.now() / 1000);
        const remaining = cur.duration - (now - cur.start_epoch);
        const cls = remaining < 0 ? 'overtime-badge flash' : cur.kind === 'break' ? 'break-badge' : 'pomodoro-badge';
        const parts = [cur.project ? `[${cur.project}]` : '', cur.name ? `[${cur.name}]` : ''].filter(Boolean).join(' ');
        el.innerHTML = `<div class="countdown ${cls}">${fmtTime(remaining)}</div>
          <div class="meta">${stateLabel(cur.state)} ${parts}</div>
          <div class="controls" id="live-ctrls"></div>`;
        renderControls();
      }
    } catch (e) {
      document.getElementById('live-panel').innerHTML = `<div class="error">Server unreachable</div>`;
    }
  };
  await render();
  liveTimer = setInterval(render, 2000);
}

function renderControls() {
  const ctrls = document.getElementById('live-ctrls');
  if (!ctrls) return;
  ctrls.innerHTML = `
    <input id="live-mins" type="number" value="25" min="1" max="120" title="Duration (minutes)">
    <input id="live-project" placeholder="project" maxlength="64">
    <input id="live-name" placeholder="name" maxlength="128">
    <button class="btn primary" onclick="startSession('pomodoro')">Pomodoro</button>
    <button class="btn" style="color:var(--green);border-color:var(--green)" onclick="startSession('break')">Break</button>
    <button class="btn danger" onclick="stopSession()">Stop</button>
  `;
}

async function startSession(kind) {
  const mins = parseInt(document.getElementById('live-mins').value) || 25;
  const project = document.getElementById('live-project').value || null;
  const name = document.getElementById('live-name').value || null;
  const now = Math.floor(Date.now() / 1000);
  const session = {
    id: crypto.randomUUID(),
    state: kind === 'break' ? 'break' : 'pomodoro',
    start_epoch: now,
    duration: mins * 60,
    origin_machine: 'dashboard',
    updated_at: now,
    ended_at: null,
    name, project,
    kind: kind === 'break' ? 'break' : 'pomodoro',
  };
  try {
    await api('POST', '/sessions', session);
    mountLive();
  } catch (e) {
    alert('Failed to start: ' + e.message);
  }
}

async function stopSession() {
  try {
    const cur = await api('GET', '/current');
    if (cur.state === 'idle') return;
    const now = Math.floor(Date.now() / 1000);
    await api('POST', '/sessions/end', { ...cur, state: 'ended', updated_at: now, ended_at: now });
    mountLive();
  } catch (e) {
    alert('Failed to stop: ' + e.message);
  }
}

// -------- History ----------
let historyTimer;
async function mountHistory() {
  clearInterval(historyTimer);
  const today = dateStr(Math.floor(Date.now() / 1000));
  document.getElementById('hist-from').value = today;
  document.getElementById('hist-to').value = today;
  loadProjects();
  await loadHistory();
  historyTimer = setInterval(loadHistory, 10000);
}

async function loadProjects() {
  try {
    const projects = await api('GET', '/projects');
    const sel = document.getElementById('hist-project');
    sel.innerHTML = '<option value="">All projects</option>' +
      projects.map(p => `<option value="${p.project}">${p.project}</option>`).join('');
  } catch (e) { /* ignore */ }
}

async function loadHistory() {
  const from = document.getElementById('hist-from').value;
  const to = document.getElementById('hist-to').value;
  const project = document.getElementById('hist-project').value;
  const state = document.getElementById('hist-state').value;
  const archived = document.getElementById('hist-archived').checked;
  const params = new URLSearchParams();
  if (from) params.set('from', from);
  if (to) params.set('to', to);
  if (project) params.set('project', project);
  if (state) params.set('state', state);
  if (archived) params.set('include_archived', '1');

  const tbody = document.getElementById('hist-tbody');
  try {
    const sessions = await api('GET', '/sessions?' + params.toString());
    if (!sessions.length) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:24px">No sessions found</td></tr>';
      return;
    }
    tbody.innerHTML = sessions.map(s => {
      const dur = (s.ended_at || Math.floor(Date.now() / 1000)) - s.start_epoch;
      return `<tr>
        <td>${dateStr(s.start_epoch)} ${timeStr(s.start_epoch)}</td>
        <td>${escapeHtml(s.project || '')}</td>
        <td class="editable" ondblclick="editCell(this,'${s.id}','name')">${escapeHtml(s.name || '')}</td>
        <td class="${stateClass(s.state)}">${stateLabel(s.state)}</td>
        <td>${fmtTime(Math.max(0, dur))}</td>
        <td class="actions">
          <button class="btn small" onclick="editProjectPrompt('${s.id}','${escapeHtml(s.project || '')}')" title="Edit project">&hellip;</button>
          <button class="btn small danger" onclick="archiveSession('${s.id}')" title="Archive">X</button>
        </td>
      </tr>`;
    }).join('');
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="6" class="error">Server unreachable</td></tr>';
  }
}

function escapeHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function editCell(td, id, field) {
  const old = td.textContent;
  td.innerHTML = `<input value="${escapeHtml(old)}" onblur="saveCell(this,'${id}','${field}')" onkeydown="if(event.key==='Enter')this.blur()" autofocus style="width:100%">`;
  td.querySelector('input').focus();
}

async function saveCell(input, id, field) {
  const value = input.value.trim() || '';
  const now = Math.floor(Date.now() / 1000);
  try {
    await api('PATCH', '/sessions/' + id, { [field]: value || null, updated_at: now });
  } catch (e) {
    alert('Failed to save: ' + e.message);
  }
  loadHistory();
}

async function editProjectPrompt(id, current) {
  const value = prompt('Project:', current);
  if (value === null) return;
  const now = Math.floor(Date.now() / 1000);
  try {
    await api('PATCH', '/sessions/' + id, { project: value || null, updated_at: now });
  } catch (e) {
    alert('Failed to save: ' + e.message);
  }
  loadHistory();
}

async function archiveSession(id) {
  if (!confirm('Archive this session?')) return;
  const now = Math.floor(Date.now() / 1000);
  try {
    await api('POST', '/sessions/' + id + '/archive', { updated_at: now });
    loadHistory();
  } catch (e) {
    alert('Failed to archive: ' + e.message);
  }
}

// -------- Stats ----------
async function mountStats() {
  const today = dateStr(Math.floor(Date.now() / 1000));
  document.getElementById('stats-from').value = today;
  document.getElementById('stats-to').value = today;
  await loadStats();
}

async function loadStats() {
  const from = document.getElementById('stats-from').value;
  const to = document.getElementById('stats-to').value;
  const project = document.getElementById('stats-project').value;
  const params = new URLSearchParams();
  if (from) params.set('from', from);
  if (to) params.set('to', to);
  if (project) params.set('project', project);

  try {
    const stats = await api('GET', '/stats?' + params.toString());
    document.getElementById('stats-total').textContent = fmtTime(stats.total_seconds);
    document.getElementById('stats-count').textContent = stats.session_count;
    const max = Math.max(...Object.values(stats.projects).map(p => p.seconds), 1);
    const list = document.getElementById('stats-projects');
    const entries = Object.entries(stats.projects);
    if (!entries.length) {
      list.innerHTML = '<div style="color:var(--muted);padding:16px;text-align:center">No data for this range</div>';
    } else {
      list.innerHTML = entries.map(([proj, p]) => {
        const pct = Math.round((p.seconds / max) * 100);
        return `<div style="margin-bottom:12px">
          <div style="display:flex;justify-content:space-between;margin-bottom:4px">
            <span><strong>${escapeHtml(proj)}</strong></span>
            <span>${fmtTime(p.seconds)} (${p.count} session${p.count !== 1 ? 's' : ''})</span>
          </div>
          <div class="bar-wrap"><div class="bar-fill" style="width:${pct}%"></div></div>
        </div>`;
      }).join('');
    }
  } catch (e) {
    document.getElementById('stats-projects').innerHTML = '<div class="error">Server unreachable</div>';
  }
}

// Init
mountLive();
"""

_PAGE = (
    r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pomo dashboard</title>
<style>
"""
    + _STYLE
    + """</style>
</head>
<body>
<nav>
  <h1>pomo dashboard</h1>
  <span id="theme-toggle" title="Toggle theme"></span>
  <button class="nav-tab active" data-tab="live">Live</button>
  <button class="nav-tab" data-tab="history">History</button>
  <button class="nav-tab" data-tab="stats">Stats</button>
</nav>

<section id="live" class="tab active">
  <div id="live-panel">
    <div class="loading">Loading&hellip;</div>
  </div>
</section>

<section id="history" class="tab">
  <div class="row">
    <input id="hist-from" type="date" onchange="loadHistory()">
    <span style="color:var(--muted)">to</span>
    <input id="hist-to" type="date" onchange="loadHistory()">
    <select id="hist-project" onchange="loadHistory()">
      <option value="">All projects</option>
    </select>
    <select id="hist-state" onchange="loadHistory()">
      <option value="">All states</option>
      <option value="pomodoro">pomodoro</option>
      <option value="overtime">overtime</option>
      <option value="break">break</option>
      <option value="break-overtime">break-overtime</option>
      <option value="ended">ended</option>
      <option value="archived">archived</option>
    </select>
    <label style="font-size:13px;display:flex;align-items:center;gap:4px">
      <input id="hist-archived" type="checkbox" onchange="loadHistory()">include archived
    </label>
    <button class="btn" onclick="loadHistory()">Refresh</button>
  </div>
  <table>
    <thead>
      <tr><th>Date</th><th>Project</th><th>Name</th><th>State</th><th>Duration</th><th></th></tr>
    </thead>
    <tbody id="hist-tbody"></tbody>
  </table>
</section>

<section id="stats" class="tab">
  <div class="row">
    <input id="stats-from" type="date" onchange="loadStats()">
    <span style="color:var(--muted)">to</span>
    <input id="stats-to" type="date" onchange="loadStats()">
    <input id="stats-project" placeholder="project" onchange="loadStats()">
    <button class="btn" onclick="loadStats()">Refresh</button>
  </div>
  <div class="row" style="gap:24px;justify-content:center;margin:24px 0">
    <div style="text-align:center">
      <div class="stat-number" id="stats-total">00:00</div>
      <div class="stat-label">total focus time</div>
    </div>
    <div style="text-align:center">
      <div class="stat-number" id="stats-count">0</div>
      <div class="stat-label">sessions</div>
    </div>
  </div>
  <div id="stats-projects"></div>
</section>

<script>
"""
    + _SCRIPT
    + """</script>
</body>
</html>
"""
)


class Handler(BaseHTTPRequestHandler):
    server_version = "pomo-dashboard/1.0"

    def _serve(self, content: str, content_type: str) -> None:
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        sys.stderr.write(f"{self.address_string()} - {format % args}\n")

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            return self._serve(_PAGE, "text/html; charset=utf-8")
        if self.path == "/app.js":
            return self._serve(_SCRIPT, "application/javascript; charset=utf-8")
        if self.path == "/style.css":
            return self._serve(_STYLE, "text/css; charset=utf-8")
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main() -> None:
    cfg = common.load_config()
    port = cfg.get("dashboard_port", 9090)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    sys.stderr.write(f"pomo-dashboard v{common.version()} listening on http://127.0.0.1:{port}\n")
    with contextlib.suppress(KeyboardInterrupt):
        httpd.serve_forever()


if __name__ == "__main__":
    main()
