/* HN Watch frontend: feed + monitor CRUD + SSE + dig-deeper live view. */
"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  monitors: [],
  feedOldestId: null,
  runId: null,        // currently open swarm run
  agentPanes: {},     // agent_id -> pane element
};

// ---------- API ----------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.status === 204 ? null : res.json();
}

// ---------- monitors ----------
function renderMonitors() {
  const ul = $("#monitor-list");
  ul.innerHTML = "";
  if (!state.monitors.length) {
    ul.innerHTML = '<li class="monitor" style="color:var(--muted)">No monitors yet.</li>';
    return;
  }
  for (const m of state.monitors) {
    const li = document.createElement("li");
    li.className = "monitor" + (m.enabled ? "" : " disabled");
    const status = m._live === "running" ? "running" : m.last_status === "error" ? "error" : m.last_status === "ok" ? "ok" : "";
    li.innerHTML = `
      <div class="m-row">
        <span class="dot ${status}"></span>
        <span class="m-name" title="${esc(m.prompt)}">${esc(m.name)}</span>
        <button class="iconbtn" data-act="run" title="Run now">&#9654;</button>
        <button class="iconbtn" data-act="toggle" title="${m.enabled ? "Pause" : "Resume"}">${m.enabled ? "&#10074;&#10074;" : "&#9655;"}</button>
        <button class="iconbtn" data-act="del" title="Delete">&#10005;</button>
      </div>
      <div class="m-meta">
        <span>every ${m.interval_minutes}m</span>
        <span>$${(m.total_cost_usd || 0).toFixed(2)}</span>
        ${m.last_run_at ? `<span>ran ${timeAgo(m.last_run_at)}</span>` : "<span>never ran</span>"}
      </div>
      ${m.last_status === "error" && m.last_error ? `<div class="m-error">${esc(m.last_error)}</div>` : ""}`;
    li.addEventListener("click", async (e) => {
      const act = e.target.dataset && e.target.dataset.act;
      if (!act) return;
      if (act === "run") {
        e.target.disabled = true;
        try { await api(`/api/monitors/${m.id}/run`, { method: "POST" }); }
        catch (err) { console.error(err); }
        e.target.disabled = false;
      } else if (act === "toggle") {
        await api(`/api/monitors/${m.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !m.enabled }) });
        await loadMonitors();
      } else if (act === "del") {
        if (confirm(`Delete monitor "${m.name}" and its feed items?`)) {
          await api(`/api/monitors/${m.id}`, { method: "DELETE" });
          await loadMonitors();
          await loadFeed(true);
        }
      }
    });
    ul.appendChild(li);
  }
}

async function loadMonitors() {
  const live = Object.fromEntries(state.monitors.map((m) => [m.id, m._live]));
  state.monitors = await api("/api/monitors");
  for (const m of state.monitors) m._live = live[m.id];
  renderMonitors();
}

// ---------- feed ----------
function feedCard(item) {
  const div = document.createElement("div");
  div.className = "card";
  div.dataset.itemId = item.id;
  const link = item.url || item.hn_url;
  const host = item.url ? new URL(item.url).hostname.replace(/^www\./, "") : "news.ycombinator.com";
  div.innerHTML = `
    <div class="c-top">
      <span class="chip">${esc(item.monitor_name || "")}</span>
      <span>${timeAgo(item.matched_at)}</span>
    </div>
    <h3><a href="${esc(link)}" target="_blank" rel="noopener">${esc(item.title)}</a>
        <span style="color:var(--muted);font-weight:400;font-size:12px"> ${esc(host)}</span></h3>
    <div class="c-summary">${esc(item.summary)}</div>
    <div class="c-bottom">
      <span>&#9650; ${item.points ?? 0}</span>
      <a href="${esc(item.hn_url)}" target="_blank" rel="noopener">${item.num_comments ?? 0} comments</a>
      <span>by ${esc(item.author || "?")}</span>
      <button class="btn small dig">${item.latest_run_status === "done" ? "View brief" : "&#128269; Dig deeper"}</button>
    </div>`;
  div.querySelector(".dig").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      if (item.latest_run_status === "done" || item.latest_run_status === "running" || item.latest_run_status === "synthesizing") {
        openRun(item.latest_run_id);
      } else {
        const { run_id } = await api(`/api/feed/${item.id}/dig`, { method: "POST" });
        openRun(run_id);
      }
    } catch (err) { console.error(err); }
    e.target.disabled = false;
  });
  return div;
}

async function loadFeed(reset = false) {
  const feedEl = $("#feed");
  if (reset) { feedEl.innerHTML = ""; state.feedOldestId = null; }
  const q = state.feedOldestId ? `&before_id=${state.feedOldestId}` : "";
  const items = await api(`/api/feed?limit=30${q}`);
  for (const item of items) {
    feedEl.appendChild(feedCard(item));
    state.feedOldestId = item.id;
  }
  $("#feed-empty").classList.toggle("hidden", feedEl.children.length > 0);
  $("#btn-more").classList.toggle("hidden", items.length < 30);
}

function prependFeedItems(items) {
  const feedEl = $("#feed");
  for (const item of [...items].reverse()) {
    if (feedEl.querySelector(`[data-item-id="${item.id}"]`)) continue;
    feedEl.prepend(feedCard(item));
  }
  $("#feed-empty").classList.add("hidden");
}

// ---------- swarm run view ----------
async function openRun(runId) {
  location.hash = `#run/${runId}`;
}

async function showRun(runId) {
  state.runId = runId;
  state.agentPanes = {};
  $("#view-feed").classList.add("hidden");
  $("#view-run").classList.remove("hidden");
  $("#agent-grid").innerHTML = "";
  $("#brief").classList.add("hidden");
  try {
    const run = await api(`/api/runs/${runId}`);
    renderRun(run);
    // replay any persisted agent output for finished/old runs
    for (const agent of run.agents) {
      if (agent.output_md) appendAgentEvent(agent.id, { kind: "text", text: agent.output_md });
      if (agent.status === "error") appendAgentEvent(agent.id, { kind: "error", text: "agent failed" });
    }
  } catch (err) {
    $("#run-title").textContent = "Run not found";
  }
}

function renderRun(run) {
  $("#run-title").textContent = run.title;
  const labels = { running: "researching…", synthesizing: "compiling brief…", done: "done", error: "failed" };
  $("#run-status").textContent =
    `${labels[run.status] || run.status} · $${(run.cost_usd || 0).toFixed(2)}` +
    (run.error ? ` · ${run.error}` : "");
  for (const agent of run.agents) getPane(agent.id, agent.angle, agent.status);
  if (run.status === "done" && run.brief_md) {
    $("#brief").classList.remove("hidden");
    $("#brief-md").innerHTML = marked.parse(run.brief_md);
  }
}

function getPane(agentId, angle, status) {
  if (state.agentPanes[agentId]) {
    if (status !== undefined) {
      const dot = state.agentPanes[agentId].querySelector(".dot");
      dot.className = "dot " + (status === "done" ? "ok" : status === "error" ? "error" : "running");
    }
    return state.agentPanes[agentId];
  }
  const pane = document.createElement("div");
  pane.className = "agent-pane";
  pane.innerHTML = `
    <div class="a-head"><span class="dot ${status === "done" ? "ok" : status === "error" ? "error" : "running"}"></span>${esc(angle)}</div>
    <div class="a-log"></div>`;
  $("#agent-grid").appendChild(pane);
  state.agentPanes[agentId] = pane;
  return pane;
}

function appendAgentEvent(agentId, ev, angle) {
  const pane = getPane(agentId, angle || "…");
  const log = pane.querySelector(".a-log");
  const el = document.createElement("div");
  if (ev.kind === "tool") {
    el.className = "ev-tool";
    el.textContent = `⚙ ${ev.tool} ${ev.input_summary || ""}`;
  } else if (ev.kind === "error") {
    el.className = "ev-error";
    el.textContent = ev.text || "agent failed";
    pane.querySelector(".dot").className = "dot error";
  } else if (ev.kind === "done") {
    pane.querySelector(".dot").className = "dot ok";
    return;
  } else {
    el.className = "ev-text";
    el.textContent = ev.text || "";
  }
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

// ---------- SSE ----------
function connectSSE() {
  const es = new EventSource("/api/events");
  es.onopen = () => setConn(true);
  es.onerror = () => setConn(false);
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "feed.new") {
      prependFeedItems(ev.items);
      loadMonitors();
    } else if (ev.type === "monitor.status") {
      const m = state.monitors.find((x) => x.id === ev.monitor.id);
      if (m) Object.assign(m, ev.monitor, { _live: ev.status === "running" ? "running" : null });
      renderMonitors();
    } else if (ev.type === "swarm.agent" && ev.run_id === state.runId) {
      appendAgentEvent(ev.agent_id, ev, ev.angle);
    } else if (ev.type === "swarm.status" && ev.run.id === state.runId) {
      renderRun(ev.run);
    }
  };
}

function setConn(ok) {
  $("#conn-dot").className = "dot " + (ok ? "ok" : "error");
  $("#conn-label").textContent = ok ? "live" : "reconnecting…";
}

// ---------- routing ----------
function route() {
  const match = location.hash.match(/^#run\/(\d+)$/);
  if (match) {
    showRun(Number(match[1]));
  } else {
    state.runId = null;
    $("#view-run").classList.add("hidden");
    $("#view-feed").classList.remove("hidden");
    loadFeed(true);
  }
}

// ---------- helpers ----------
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function timeAgo(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// ---------- init ----------
$("#btn-new-monitor").addEventListener("click", () => $("#monitor-form").classList.toggle("hidden"));
$("#mf-cancel").addEventListener("click", () => $("#monitor-form").classList.add("hidden"));
$("#monitor-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  await api("/api/monitors", {
    method: "POST",
    body: JSON.stringify({
      name: $("#mf-name").value.trim(),
      prompt: $("#mf-prompt").value.trim(),
      interval_minutes: Number($("#mf-interval").value) || 30,
    }),
  });
  e.target.reset();
  $("#monitor-form").classList.add("hidden");
  await loadMonitors();
});
$("#btn-more").addEventListener("click", () => loadFeed(false));
$("#btn-back").addEventListener("click", () => (location.hash = ""));
window.addEventListener("hashchange", route);

loadMonitors();
route();
connectSSE();
setInterval(renderMonitors, 60_000); // refresh "x min ago" labels
