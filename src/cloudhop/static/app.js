"use strict";

/* ------------------------------------------------------------------ basics */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const TOKEN_KEY = "cloudhop-token";

(function captureToken() {
  const params = new URLSearchParams(location.search);
  const t = params.get("token");
  if (t) {
    try { localStorage.setItem(TOKEN_KEY, t); } catch (e) { window.__token = t; }
    history.replaceState(null, "", location.pathname + location.hash);
  }
})();

function token() {
  try { return localStorage.getItem(TOKEN_KEY) || window.__token || ""; } catch (e) { return window.__token || ""; }
}

async function api(method, path, body) {
  const opts = { method, headers: { "X-Cloudhop-Token": token() } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch("/api/" + path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (res.status === 401) { showLocked(); throw new Error("locked"); }
  if (!res.ok) throw new Error((data && data.error) || "Request failed (" + res.status + ")");
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function B(n) {
  if (n == null) return "–";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"]; let i = 0;
  while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return (i ? n.toFixed(n < 10 ? 2 : 1) : n) + " " + u[i];
}
function D(s) {
  if (s == null) return "–";
  s = Math.round(s);
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : m ? `${m}m ${s % 60}s` : `${s}s`;
}
function ago(t) { return t ? D(Date.now() / 1000 - t) + " ago" : ""; }
function when(t) {
  if (!t) return "–";
  return new Date(t * 1000).toLocaleString([], { weekday: "short", day: "numeric", month: "short",
    hour: "2-digit", minute: "2-digit", hour12: false });
}
function pct(a, b) { return b ? Math.min(100, 100 * a / b) : 0; }
function view(html) { $("#view").innerHTML = html; }
function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast"; t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}
function showLocked() {
  clearInterval(timer);
  view(`<div class="card empty"><h2>cloudhop needs its key</h2>
    <p>Open the address that cloudhop printed in its window when it started.<br>
    It ends with <span class="mono">?token=…</span> and works as a bookmark.</p></div>`);
}

let STATE = null;
let timer = null;
function poll(fn, ms) {
  clearInterval(timer);
  timer = setInterval(() => fn().catch(() => {}), ms);
}
async function loadState() {
  STATE = await api("GET", "state");
  $("#ver").textContent = `v${STATE.version} · rclone ${STATE.rclone.version || "not installed"}`;
  return STATE;
}

const STATUS = {
  queued: "Queued", running: "Running", waiting_retry: "Waiting to retry", paused: "Needs your attention",
  succeeded: "Finished", failed: "Failed", cancelled: "Stopped", pending: "Waiting", done: "Done",
};
const PHASE = { transfer: "copying", verify: "verifying", archive: "archiving", delete: "deleting" };
function pill(status, label) {
  return `<span class="pill s-${esc(status)}">${esc(label || STATUS[status] || status)}</span>`;
}
function scheduleText(s) {
  if (!s || s.type === "manual") return "Only when started by hand";
  if (s.type === "interval") {
    const m = s.every_minutes;
    return "Every " + (m % 1440 === 0 ? `${m / 1440} day(s)` : m % 60 === 0 ? `${m / 60} hour(s)` : `${m} minutes`);
  }
  if (s.type === "daily") return `Every day at ${s.time}`;
  const names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  return `${s.days.map(d => names[d]).join(", ")} at ${s.time}`;
}

/* ------------------------------------------------------------------ router */

const ROUTES = [
  [/^#?\/?$/, "dashboard", () => viewDashboard()],
  [/^#\/jobs$/, "jobs", () => viewJobs()],
  [/^#\/jobs\/new$/, "jobs", () => viewJobEdit(null)],
  [/^#\/jobs\/(\w+)\/edit$/, "jobs", m => viewJobEdit(m[1])],
  [/^#\/jobs\/(\w+)$/, "jobs", m => viewJobHistory(m[1])],
  [/^#\/runs\/([\w-]+)$/, "dashboard", m => viewRun(m[1])],
  [/^#\/connections$/, "connections", () => viewConnections()],
  [/^#\/settings$/, "settings", () => viewSettings()],
];

async function route() {
  clearInterval(timer);
  closeModal();
  const hash = location.hash || "#/";
  for (const [re, nav, fn] of ROUTES) {
    const m = hash.match(re);
    if (!m) continue;
    $$("nav a").forEach(a => a.classList.toggle("on", a.dataset.nav === nav));
    try {
      await fn(m);
    } catch (e) {
      if (e.message !== "locked") view(`<div class="banner bad"><b>Something went wrong</b>${esc(e.message)}</div>`);
    }
    window.scrollTo(0, 0);
    return;
  }
  view("<h1>Page not found</h1>");
}

/* Delegated click actions: <button data-act="name" data-...> */
const ACT = {};
document.addEventListener("click", e => {
  const el = e.target.closest("[data-act]");
  if (!el || el.disabled) return;
  e.preventDefault();
  const fn = ACT[el.dataset.act];
  if (fn) Promise.resolve(fn(el, e)).catch(err => { if (err.message !== "locked") toast(err.message); });
});

/* ------------------------------------------------------------------ banners */

function banners() {
  let html = "";
  const r = STATE.rclone;
  if (!r.path) {
    html += `<div class="banner warn"><b>rclone is not installed yet</b>
      cloudhop uses the free, open-source <a href="https://rclone.org" target="_blank" rel="noopener">rclone</a> to move files.
      <div class="row" style="margin-top:8px"><button class="primary" data-act="install-rclone">Download rclone</button>
      <span class="small muted">${esc(r.install.message || "")}</span></div></div>`;
  }
  const found = STATE.detected_config;
  if (found && found.remotes.length) {
    html += `<div class="banner ok"><b>Existing connections found on this computer</b>
      You've used rclone before (${found.remotes.map(esc).join(", ")}). Use those connections in cloudhop?
      <div class="row" style="margin-top:8px"><button class="primary" data-act="use-config" data-path="${esc(found.path)}">Use them</button>
      <span class="small muted path">${esc(found.path)}</span></div></div>`;
  }
  return html;
}

ACT["install-rclone"] = async () => {
  await api("POST", "rclone/install");
  toast("Downloading rclone…");
  const check = setInterval(async () => {
    await loadState();
    if (STATE.rclone.install.state !== "running") {
      clearInterval(check);
      toast(STATE.rclone.install.message);
      route();
    }
  }, 1500);
};
ACT["use-config"] = async el => {
  await api("POST", "settings", { rclone_config: el.dataset.path });
  await loadState();
  toast("Now using your existing rclone connections");
  route();
};

/* ------------------------------------------------------------------ dashboard */

async function viewDashboard() {
  const render = async () => {
    const jobs = await api("GET", "jobs");
    let html = banners() + `<div class="row spread"><h1>Dashboard</h1>
      <a class="btn primary" href="#/jobs/new">+ New job</a></div>`;
    if (!jobs.length) {
      html += `<div class="card empty"><h2>Welcome to cloudhop 👋</h2>
        <p>Move or back up files between cloud accounts, in three steps:</p>
        <p><b>1.</b> <a href="#/connections">Add your connections</a> (for example OneDrive and Google Drive)<br>
        <b>2.</b> <a href="#/jobs/new">Create a job</a>: what to copy, where to, and when<br>
        <b>3.</b> Press <b>Run</b> and follow the progress here</p></div>`;
    } else {
      html += `<div class="jobs">${jobs.map(jobCard).join("")}</div>`;
    }
    view(html);
  };
  await render();
  poll(render, 3000);
}

function jobCard(j) {
  const a = j.active_run, last = j.last_run;
  let status, extra = "", actions = "";
  if (a) {
    status = pill(a.status);
    const progress = a.units_total > 1 ? `${a.units_done}/${a.units_total} folders · ` : "";
    extra = `<div class="small muted">${progress}${B(a.transferred)} transferred</div>`;
    if (a.status === "paused") {
      extra += `<div class="banner warn small" style="margin:8px 0 0"><b>Paused: needs your attention</b>${esc(a.error)}</div>`;
      actions += `<button class="primary small" data-act="resume" data-run="${a.id}">Resume</button>`;
    } else if (a.status === "waiting_retry") {
      extra += `<div class="small muted">Retrying ${when(a.retry_at)}${a.error ? " · last error: " + esc(a.error) : ""}</div>`;
      actions += `<button class="small" data-act="run" data-job="${j.id}">Retry now</button>`;
    }
    actions += `<a class="btn small" href="#/runs/${a.id}">Open</a>
      <button class="small danger" data-act="stop" data-run="${a.id}">Stop</button>`;
  } else {
    status = last ? pill(last.status) : pill("idle", "Never run");
    if (last) extra = `<div class="small muted">Last run ${ago(last.ended || last.started)}${last.error ? " · " + esc(last.error) : ""}</div>`;
    actions += `<button class="primary small" data-act="run" data-job="${j.id}">Run now</button>`;
    if (last) actions += `<a class="btn small" href="#/runs/${last.id}">Last run</a>`;
  }
  const next = j.enabled && j.next_run ? `Next run ${when(j.next_run)}` : (j.enabled ? scheduleText(j.schedule) : "Schedule paused");
  return `<div class="card job">
    <div><div class="row"><b>${esc(j.name)}</b> ${status}</div>
      <div class="path">${esc(j.source)}<span class="arrow">→</span>${esc(j.destination)}</div>
      ${extra}<div class="small muted">${esc(next)}</div></div>
    <div class="actions">${actions}<a class="btn small" href="#/jobs/${j.id}/edit">Edit</a></div></div>`;
}

ACT.run = async el => {
  const run = await api("POST", `jobs/${el.dataset.job}/run`);
  toast("Started");
  location.hash = `#/runs/${run.id}`;
};
ACT.stop = async el => {
  if (!confirm("Stop this run? Files already copied stay where they are; you can run the job again later.")) return;
  await api("POST", `runs/${el.dataset.run}/stop`);
  toast("Stopping…");
  route();
};
ACT.resume = async el => {
  await api("POST", `runs/${el.dataset.run}/resume`);
  toast("Resuming");
  location.hash = `#/runs/${el.dataset.run}`;
  route();
};

/* ------------------------------------------------------------------ run monitor */

const RING = 2 * Math.PI * 52;
function gaugeSvg(id, label, withTick) {
  return `<figure class="gauge" id="${id}"><div class="k">${label}</div>
    <svg viewBox="0 0 140 140" role="img"><title></title>
      <circle class="trk" cx="70" cy="70" r="52"/>
      <circle class="arc" cx="70" cy="70" r="52" transform="rotate(-90 70 70)" stroke-dasharray="0 400" style="opacity:0"/>
      ${withTick ? '<line class="tick" x1="70" y1="10" x2="70" y2="30" style="display:none"/>' : ""}
      <text class="big" x="70" y="72" text-anchor="middle">–</text>
      <text class="unit" x="70" y="92" text-anchor="middle"></text>
    </svg></figure>`;
}
function gauge(id, frac, tick, val, unit, tip) {
  const g = document.getElementById(id);
  if (!g) return;
  const f = Math.max(0, Math.min(1, frac || 0)), arc = $(".arc", g);
  arc.setAttribute("stroke-dasharray", `${f * RING} ${RING}`);
  arc.style.opacity = f > 0 ? 1 : 0;
  const t = $(".tick", g);
  if (t) { t.style.display = tick == null ? "none" : ""; t.setAttribute("transform", `rotate(${Math.min(1, tick || 0) * 360} 70 70)`); }
  $(".big", g).textContent = val;
  $(".unit", g).textContent = unit;
  $("title", g).textContent = tip;
  g.title = tip;
}

async function viewRun(id) {
  const first = await api("GET", `runs/${id}`);
  view(`<div id="run-head"></div><div id="run-banner"></div>
    <div class="card"><div class="top">
      ${gaugeSvg("g-transfer", "Transferred")}${gaugeSvg("g-verified", "Verified")}${gaugeSvg("g-speed", "Speed now", true)}
      <div class="stats" id="run-stats"></div></div></div>
    <div class="card" id="run-now"></div>
    <div class="card wrap" id="run-units"></div>
    <div class="card"><div class="k">Log</div><pre class="log" id="run-log"></pre></div>`);
  updateRun(first);
  const refresh = async () => updateRun(await api("GET", `runs/${id}`));
  poll(refresh, 2000);
}

function updateRun(r) {
  const job = r.job, live = r.live, active = ["queued", "running", "waiting_retry", "paused"].includes(r.status);
  let buttons = "";
  if (r.status === "paused" || r.status === "waiting_retry")
    buttons += `<button class="primary" data-act="resume" data-run="${r.id}">${r.status === "paused" ? "Resume" : "Retry now"}</button>`;
  if (active) buttons += `<button class="danger" data-act="stop" data-run="${r.id}">Stop</button>`;
  else buttons += `<button class="primary" data-act="run" data-job="${r.job_id}">Run again</button>`;
  $("#run-head").innerHTML = `<div class="row spread"><div><h1 style="margin-bottom:2px">${esc(job.name)} ${pill(r.status)}</h1>
    <div class="path muted">${esc(job.source)}<span class="arrow">→</span>${esc(job.destination)}</div>
    <div class="small muted">Started ${when(r.started || r.created)}${r.ended ? " · ended " + when(r.ended) : ""}${r.attempt > 1 ? " · attempt " + r.attempt : ""}</div></div>
    <div class="row">${buttons}<a class="btn" href="#/jobs/${r.job_id}">History</a></div></div><div style="height:14px"></div>`;

  let banner = "";
  if (r.status === "paused") banner = `<div class="banner warn"><b>Paused: needs your attention</b>${esc(r.error)}</div>`;
  else if (r.status === "waiting_retry") banner = `<div class="banner warn"><b>Will retry ${when(r.retry_at)}</b>${esc(r.error || "")}</div>`;
  else if (r.status === "failed") banner = `<div class="banner bad"><b>The run failed</b>${esc(r.error || "See the log below.")}</div>`;
  else if (r.status === "succeeded") banner = `<div class="banner ok"><b>All done</b>${B(r.transferred)} transferred this run.</div>`;
  $("#run-banner").innerHTML = banner;

  const total = r.total_bytes, tp = pct(r.transferred_bytes, total);
  gauge("g-transfer", total ? tp / 100 : 0, null, total ? tp.toFixed(1) + "%" : "–", B(r.transferred_bytes),
    `Transferred ${B(r.transferred_bytes)} of ${total ? B(total) : "unknown (still measuring)"}`);
  if (job.verify === "none") {
    gauge("g-verified", 0, null, "off", "no check", "Verification is turned off for this job");
  } else {
    const vp = pct(r.verified_bytes, total);
    gauge("g-verified", total ? vp / 100 : 0, null, total ? vp.toFixed(1) + "%" : "–", B(r.verified_bytes),
      `Verified ${B(r.verified_bytes)} of ${total ? B(total) : "?"}`);
  }
  const now = (live && live.speed) || 0, mx = Math.max(r.max_speed || 0, now, r.avg_speed || 0);
  const [sv, su] = B(now).split(" ");
  gauge("g-speed", mx ? now / mx : 0, mx && r.avg_speed ? r.avg_speed / mx : null, now ? sv : "0", now ? su + "/s" : "idle",
    `Speed now ${B(now)}/s · tick = average ${r.avg_speed ? B(r.avg_speed) + "/s" : "–"} · full ring = fastest seen ${B(mx)}/s`);

  const units = r.units || [];
  $("#run-stats").innerHTML = `
    <div title="Based on the average speed so far"><div class="k">Time remaining</div><div class="v">${active && r.eta_seconds != null ? D(r.eta_seconds) : "–"}</div>
      <div class="note">${active && r.eta_seconds != null ? "done ≈ " + when(Date.now() / 1000 + r.eta_seconds) : (r.sizes_complete || !active ? "" : "measuring size…")}</div></div>
    <div><div class="k">${units.length > 1 ? "Folders done" : "Progress"}</div><div class="v">${units.filter(u => u.status === "done").length} / ${units.length || "–"}</div>
      <div class="note">${total ? "of " + B(total) : ""}</div></div>
    <div><div class="k">Uploaded today</div><div class="v">${B(r.today_bytes)}</div>
      <div class="note">${job.drive_stop_on_upload_limit ? "of Drive's 750 GB daily cap" + (r.today_bytes > 7.5e9 ? " (" + pct(r.today_bytes, 750e9).toFixed(0) + " %)" : "") : ""}</div></div>
    <div><div class="k">Active time</div><div class="v">${D(r.active_seconds)}</div>
      <div class="note">${r.avg_speed ? "avg " + B(r.avg_speed) + "/s" : ""}</div></div>`;

  const cur = r.current;
  let nowHtml = `<div class="k">Now</div>`;
  if (cur && r.status === "running") {
    const phase = PHASE[cur.phase] || cur.phase || "preparing";
    const detail = cur.detail && cur.detail !== "main" ? ` (${esc(cur.detail)})` : "";
    nowHtml += `<div style="font-size:18px;margin:2px 0 6px"><b>${esc(cur.name)}</b> – ${esc(phase)}${detail}</div>`;
    if (live) {
      nowHtml += `<div class="bar"><i style="width:${pct(live.bytes, live.totalBytes)}%"></i></div>
        <div class="small muted">${B(live.bytes)} / ${B(live.totalBytes)} · files ${live.transfers || 0}/${live.totalTransfers || 0}
        · checked ${live.checks || 0}/${live.totalChecks || 0} · ETA ${D(live.eta)} · errors ${live.errors || 0}</div>`;
      nowHtml += (live.transferring || []).map(t => `<div class="file"><span>${esc(t.name)}</span><span>${t.percentage || 0}% of ${B(t.size)}</span></div>
        <div class="bar small"><i style="width:${t.percentage || 0}%"></i></div>`).join("");
    } else {
      nowHtml += `<div class="small muted">Listing files…</div>`;
    }
  } else {
    nowHtml += `<div class="muted">${r.status === "queued" ? "Waiting for another job to finish…" : "Nothing running right now."}</div>`;
  }
  $("#run-now").innerHTML = nowHtml;

  $("#run-units").innerHTML = `<table><thead><tr><th>${units.length > 1 ? "Folder" : "Part"}</th>
    <th class="num hide-sm">Transferred</th><th class="num">Size</th><th class="num hide-sm">Files</th><th>Status</th></tr></thead><tbody>
    ${units.map(u => {
      const label = u.status === "running" ? (PHASE[u.phase] || "working") : (STATUS[u.status] || u.status);
      const cls = u.status === "running" ? "running" : u.status;
      const sub = u.error ? `<div class="small" style="color:var(--bad)">${esc(u.error)}</div>` : (u.detail && u.status === "done" ? `<div class="small muted">${esc(u.detail)}</div>` : "");
      return `<tr><td>${esc(u.name)}</td><td class="num hide-sm">${u.transferred ? B(u.transferred) : "–"}</td>
        <td class="num">${B(u.size)}</td><td class="num hide-sm">${u.count ?? "–"}</td><td>${pill(cls, label)}${sub}</td></tr>`;
    }).join("") || `<tr><td colspan="5" class="muted">Preparing…</td></tr>`}</tbody></table>`;

  const log = $("#run-log"), atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
  log.textContent = (r.log || []).join("\n");
  if (atBottom) log.scrollTop = log.scrollHeight;
}

/* ------------------------------------------------------------------ jobs */

async function viewJobs() {
  const jobs = await api("GET", "jobs");
  view(`<div class="row spread"><h1>Jobs</h1><a class="btn primary" href="#/jobs/new">+ New job</a></div>
    ${jobs.length ? `<div class="card wrap"><table><thead><tr><th>Name</th><th>From → to</th><th class="hide-sm">When</th><th></th></tr></thead><tbody>
    ${jobs.map(j => `<tr><td><b>${esc(j.name)}</b>${j.enabled ? "" : ' <span class="pill s-pending">schedule off</span>'}</td>
      <td class="path">${esc(j.source)}<span class="arrow">→</span>${esc(j.destination)}</td>
      <td class="hide-sm small">${esc(scheduleText(j.schedule))}</td>
      <td class="row" style="justify-content:flex-end">
        <a class="btn small" href="#/jobs/${j.id}/edit">Edit</a><a class="btn small" href="#/jobs/${j.id}">History</a>
        <button class="small danger" data-act="delete-job" data-job="${j.id}" data-name="${esc(j.name)}">Delete</button></td></tr>`).join("")}
    </tbody></table></div>` : `<div class="card empty"><p>No jobs yet.</p><a class="btn primary" href="#/jobs/new">Create your first job</a></div>`}`);
}

ACT["delete-job"] = async el => {
  if (!confirm(`Delete the job "${el.dataset.name}"? Your files are not touched.`)) return;
  await api("DELETE", `jobs/${el.dataset.job}`);
  toast("Job deleted");
  route();
};

async function viewJobHistory(id) {
  const [jobs, runs] = await Promise.all([api("GET", "jobs"), api("GET", `runs?job=${id}&limit=100`)]);
  const job = jobs.find(j => j.id === id);
  if (!job) return view("<h1>Job not found</h1>");
  view(`<div class="row spread"><h1>${esc(job.name)}: history</h1>
      <div class="row"><button class="primary" data-act="run" data-job="${id}">Run now</button><a class="btn" href="#/jobs/${id}/edit">Edit</a></div></div>
    <div class="card wrap"><table><thead><tr><th>Started</th><th>Status</th><th class="num">Transferred</th><th class="hide-sm">Trigger</th><th></th></tr></thead><tbody>
    ${runs.map(r => `<tr><td>${when(r.started || r.created)}</td><td>${pill(r.status)}${r.error ? `<div class="small muted">${esc(r.error)}</div>` : ""}</td>
      <td class="num">${B(r.transferred)}</td><td class="hide-sm small">${esc(r.trigger)}</td>
      <td><a class="btn small" href="#/runs/${r.id}">Open</a></td></tr>`).join("") || '<tr><td colspan="5" class="muted">No runs yet.</td></tr>'}
    </tbody></table></div>`);
}

const NEW_JOB = {
  name: "", enabled: true, source: "", destination: "", mode: "copy", batch_by_folder: false, routes: [],
  verify: "content", after_verify: "keep", archive_to: "", only_new: false, max_age: "", min_age: "",
  include: "", exclude: "", bwlimit: "", transfers: 4, checkers: 8, tpslimit: 0,
  drive_stop_on_upload_limit: false, scan_sizes: true, retry_minutes: 60, retry_max: 0, extra_flags: "",
  schedule: { type: "manual", every_minutes: 1440, time: "02:00", days: [0, 1, 2, 3, 4, 5, 6] },
};

function radio(name, value, current, title, help) {
  return `<label class="choice"><input type="radio" name="${name}" value="${value}" ${value === current ? "checked" : ""}>
    <span><b>${title}</b><span class="help">${help}</span></span></label>`;
}
function check(name, on, title, help) {
  return `<label class="choice"><input type="checkbox" name="${name}" ${on ? "checked" : ""}>
    <span><b>${title}</b>${help ? `<span class="help">${help}</span>` : ""}</span></label>`;
}
function field(name, label, value, help, attrs = "") {
  return `<label class="field"><span>${label}</span><input name="${name}" value="${esc(value)}" ${attrs}>
    ${help ? `<div class="help">${help}</div>` : ""}</label>`;
}
function pathField(name, label, value, help) {
  return `<label class="field"><span>${label}</span><div class="row"><input class="grow" name="${name}" value="${esc(value)}"
    placeholder="connection:folder" style="flex:1"><button data-act="browse" data-target="${name}">Browse…</button></div>
    ${help ? `<div class="help">${help}</div>` : ""}</label>`;
}

async function viewJobEdit(id) {
  let job = { ...NEW_JOB, schedule: { ...NEW_JOB.schedule }, routes: [] };
  if (id) {
    const jobs = await api("GET", "jobs");
    const found = jobs.find(j => j.id === id);
    if (!found) return view("<h1>Job not found</h1>");
    job = JSON.parse(JSON.stringify(found));
  }
  const s = job.schedule, days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  view(`<h1>${id ? "Edit job" : "New job"}</h1><form id="jobform" autocomplete="off">
  <div class="card"><h2>1. What and where</h2>
    ${field("name", "Name", job.name, "Anything that helps you recognise the job, e.g. “Photos to Google Drive”.", 'placeholder="My job" required')}
    ${pathField("source", "Copy from", job.source, "The connection and folder to read from. Use <b>Browse…</b> to pick one.")}
    ${pathField("destination", "Copy to", job.destination, "Where the files should end up. Folders are created automatically.")}
  </div>
  <div class="card"><h2>2. How</h2>
    ${radio("mode", "copy", job.mode, "Copy (recommended)", "Copies new and changed files. Nothing is ever deleted at the destination.")}
    ${radio("mode", "sync", job.mode, "Mirror (sync)", "Makes the destination exactly like the source. <b>Files that are not in the source get deleted at the destination.</b>")}
    ${radio("mode", "move", job.mode, "Move", "Copies files and then deletes them from the source. No separate verification step.")}
  </div>
  <div class="card" id="sec-verify"><h2>3. Check the copies</h2>
    ${radio("verify", "content", job.verify, "Check the content (safest)", "Compares checksums, so even a single wrong byte is found. When the two services use different checksum types (like OneDrive and Google Drive), cloudhop records checksums while copying, so checking is quick and nothing is downloaded twice.")}
    ${radio("verify", "size", job.verify, "Check sizes only (fast)", "Confirms every file exists with the right size.")}
    ${radio("verify", "none", job.verify, "Don't check", "Trust the copy step.")}
    <div id="after-verify"><h3>After a file is checked</h3>
      ${radio("after_verify", "keep", job.after_verify, "Leave the original where it is", "")}
      ${radio("after_verify", "archive", job.after_verify, "Move the original to an “already copied” folder", "Shows clearly what's done. The folder must be on the same connection as the source.")}
      <div id="archive-box">${pathField("archive_to", "“Already copied” folder", job.archive_to, "For example <span class=mono>onedrive:_migrated</span>.")}</div>
      ${radio("after_verify", "delete", job.after_verify, "Delete the original", "Frees space at the source. <b>The checked copy becomes the only copy.</b> Some services keep deleted files in a recycle bin that still uses space.")}
      <div id="space-note"></div>
    </div>
  </div>
  <div class="card"><h2>4. Which files</h2>
    ${check("only_new", job.only_new, "Only new files", "Skip files that already exist at the destination, even if they changed.")}
    <div class="grid2">${field("max_age", "Only files changed within", job.max_age, "e.g. <span class=mono>7d</span>, <span class=mono>12h</span>. Empty = all files.", 'placeholder="all files"')}
    ${field("min_age", "Skip files changed within", job.min_age, "e.g. <span class=mono>1h</span> to leave files that are still being written.", 'placeholder="none"')}</div>
    <div class="grid2"><label class="field"><span>Only include</span><textarea name="include" placeholder="*.jpg&#10;Photos/**">${esc(job.include)}</textarea><div class="help">One pattern per line. Empty = everything.</div></label>
    <label class="field"><span>Leave out</span><textarea name="exclude" placeholder="*.tmp&#10;.DS_Store">${esc(job.exclude)}</textarea><div class="help">One pattern per line.</div></label></div>
    <h3>Send some file types somewhere else</h3>
    <div class="help">For example camera RAW files (cr2, nef, arw, dng) to their own folder. They keep the same sub-folders.</div>
    <div id="routes"></div><button data-act="add-route">+ Add file-type rule</button>
  </div>
  <div class="card"><h2>5. When</h2>
    ${radio("stype", "manual", s.type, "Only when I press Run", "")}
    ${radio("stype", "interval", s.type, "Repeat every…", "")}
    <div class="sched-interval row" style="margin-left:30px"><input type="number" name="every_value" min="1" style="width:110px"
      value="${s.every_minutes % 1440 === 0 ? s.every_minutes / 1440 : s.every_minutes % 60 === 0 ? s.every_minutes / 60 : s.every_minutes}">
      <select name="every_unit" style="width:140px">${["minutes", "hours", "days"].map(u => `<option ${u === (s.every_minutes % 1440 === 0 ? "days" : s.every_minutes % 60 === 0 ? "hours" : "minutes") ? "selected" : ""}>${u}</option>`).join("")}</select></div>
    ${radio("stype", "daily", s.type, "Every day at…", "")}
    ${radio("stype", "weekly", s.type, "On certain weekdays at…", "")}
    <div class="sched-time row" style="margin-left:30px"><input type="time" name="time" value="${esc(s.time)}" style="width:140px">
      <span class="sched-days row">${days.map((d, i) => `<label class="row small"><input type="checkbox" name="day" value="${i}" ${s.days.includes(i) ? "checked" : ""}>${d}</label>`).join("")}</span></div>
    <div class="help" id="sched-preview"></div>
    ${check("enabled", job.enabled, "Schedule is on", "Turn off to pause the schedule without deleting the job.")}
  </div>
  <div class="card"><h2>6. Speed and reliability</h2>
    <div class="grid2">${field("bwlimit", "Bandwidth limit", job.bwlimit, "e.g. <span class=mono>10M</span> (10 MB/s). Time-based: <span class=mono>08:00,2M 23:00,off</span> = slow during the day, full speed at night. Empty = no limit.", 'placeholder="no limit"')}
    ${field("retry_max", "Retry a failed run (times)", job.retry_max, "0 = don't retry automatically.", 'type="number" min="0"')}</div>
    <div class="grid2">${field("retry_minutes", "Wait before retrying (minutes)", job.retry_minutes, "", 'type="number" min="1"')}
    <div></div></div>
    ${check("batch_by_folder", job.batch_by_folder, "Work through one top-level folder at a time", "Recommended for big jobs: each folder is finished (and checked) before the next starts, so progress is saved along the way.")}
    ${check("scan_sizes", job.scan_sizes, "Measure the source first", "Needed for percentages and time remaining. Runs alongside the copying.")}
    ${check("drive_stop_on_upload_limit", job.drive_stop_on_upload_limit, "Stop cleanly at Google Drive's daily upload limit", "Google allows about 750 GB of uploads per day. Combine with automatic retries to continue the next day.")}
    <details><summary>Advanced</summary>
      <div class="grid3">${field("transfers", "Parallel transfers", job.transfers, "", 'type="number" min="1"')}
      ${field("checkers", "Parallel checks", job.checkers, "", 'type="number" min="1"')}
      ${field("tpslimit", "Max requests per second", job.tpslimit, "0 = unlimited. OneDrive likes ~10.", 'type="number" min="0"')}</div>
      ${field("extra_flags", "Extra rclone flags", job.extra_flags, 'Passed to every copy step, e.g. <span class=mono>--onedrive-hard-delete</span>. See <a href="https://rclone.org/flags/" target="_blank" rel="noopener">rclone flags</a>.', 'class="mono"')}
    </details>
  </div>
  <div id="form-error"></div>
  <div class="row"><button class="primary" data-act="save-job" data-id="${id || ""}">Save job</button>
    <a class="btn" href="${id ? "#/jobs/" + id : "#/jobs"}">Cancel</a></div>
  </form>`);
  ROUTES_DRAFT = job.routes.map(r => ({ ...r }));
  renderRoutes();
  const form = $("#jobform");
  form.addEventListener("change", () => syncJobForm());
  form.addEventListener("input", e => { if (["every_value", "time"].includes(e.target.name)) syncJobForm(); });
  form.addEventListener("submit", e => e.preventDefault());
  syncJobForm();
}

let ROUTES_DRAFT = [];
function renderRoutes() {
  $("#routes").innerHTML = ROUTES_DRAFT.map((r, i) => `<div class="route">
    <input data-route="${i}" data-key="extensions" value="${esc(r.extensions)}" placeholder="cr2, nef, arw">
    <div class="row"><input data-route="${i}" data-key="destination" value="${esc(r.destination)}" placeholder="connection:RAW" style="flex:1">
      <button data-act="browse" data-route-target="${i}">Browse…</button></div>
    <button class="danger" data-act="remove-route" data-index="${i}">Remove</button></div>`).join("");
  $$("#routes input").forEach(inp => inp.addEventListener("input", () => {
    ROUTES_DRAFT[+inp.dataset.route][inp.dataset.key] = inp.value;
  }));
}
ACT["add-route"] = () => { ROUTES_DRAFT.push({ extensions: "", destination: "" }); renderRoutes(); };
ACT["remove-route"] = el => { ROUTES_DRAFT.splice(+el.dataset.index, 1); renderRoutes(); };

function formValues() {
  const f = $("#jobform"), v = name => f.elements[name];
  const unit = { minutes: 1, hours: 60, days: 1440 }[v("every_unit").value];
  return {
    name: v("name").value, source: v("source").value, destination: v("destination").value,
    mode: f.querySelector("[name=mode]:checked").value,
    verify: f.querySelector("[name=verify]:checked").value,
    after_verify: f.querySelector("[name=after_verify]:checked").value,
    archive_to: v("archive_to").value, only_new: v("only_new").checked,
    max_age: v("max_age").value, min_age: v("min_age").value, include: v("include").value, exclude: v("exclude").value,
    routes: ROUTES_DRAFT, bwlimit: v("bwlimit").value, retry_max: +v("retry_max").value || 0,
    retry_minutes: +v("retry_minutes").value || 60, batch_by_folder: v("batch_by_folder").checked,
    scan_sizes: v("scan_sizes").checked, drive_stop_on_upload_limit: v("drive_stop_on_upload_limit").checked,
    transfers: +v("transfers").value || 4, checkers: +v("checkers").value || 8, tpslimit: +v("tpslimit").value || 0,
    extra_flags: v("extra_flags").value, enabled: v("enabled").checked,
    schedule: {
      type: f.querySelector("[name=stype]:checked").value,
      every_minutes: Math.max(5, (+v("every_value").value || 1) * unit),
      time: v("time").value || "02:00",
      days: $$("[name=day]:checked", f).map(c => +c.value),
    },
  };
}

let previewTimer = null;
function syncJobForm() {
  const val = formValues(), f = $("#jobform");
  const move = val.mode === "move";
  $$("[name=verify]", f).forEach(r => { r.disabled = move; });
  if (move) f.querySelector("[name=verify][value=none]").checked = true;
  $("#after-verify").style.display = move || f.querySelector("[name=verify]:checked").value === "none" || val.mode !== "copy" ? "none" : "";
  $("#archive-box").style.display = val.after_verify === "archive" ? "" : "none";
  spaceNote(val);
  const t = val.schedule.type;
  $(".sched-interval", f).style.display = t === "interval" ? "" : "none";
  $(".sched-time", f).style.display = t === "daily" || t === "weekly" ? "" : "none";
  $(".sched-days", f).style.display = t === "weekly" ? "" : "none";
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    try {
      const next = await api("POST", "jobs/preview-schedule", val.schedule);
      $("#sched-preview").textContent = next.length ? "Next runs: " + next.map(when).join(" · ") : "";
    } catch (e) { $("#sched-preview").textContent = e.message; }
  }, 300);
}

/* Moving/deleting at the source is normally free, but some accounts keep a copy of
   every moved or deleted file (e.g. Microsoft 365 retention policies): warn, and show
   how much room the source has. */
const ABOUT = {};
async function spaceNote(val) {
  const box = $("#space-note");
  if (!box) return;
  const shown = $("#after-verify").style.display !== "none";
  if (!shown || !["archive", "delete"].includes(val.after_verify)) { box.innerHTML = ""; return; }
  const remote = val.source.includes(":") ? val.source.split(":")[0] + ":" : "";
  const word = val.after_verify === "archive" ? "Moving" : "Deleting";
  const render = info => {
    let free = "";
    if (info && info.free != null) {
      free = `<div style="margin-top:6px"><b style="display:inline">${esc(remote)}</b> has <b style="display:inline">${B(info.free)}</b> free right now`
        + (info.total ? ` (of ${B(info.total)})` : "") + `.` + (info.free < 5e9 ? " That's very little: a retention policy would make this step stop almost immediately." : "") + `</div>`;
    } else if (info && info.error) {
      free = `<div class="muted" style="margin-top:6px">Couldn't read the free space of ${esc(remote)}: ${esc(info.error)}</div>`;
    }
    box.innerHTML = `<div class="banner warn small"><b>Check the free space on the source first</b>
      ${word} files is usually free, but some accounts keep a copy of every moved or deleted file, for example
      Microsoft 365 work or school accounts with a <i>retention policy</i>. Then this step needs free space of about
      <b style="display:inline">twice the size</b> of what's ${val.after_verify === "archive" ? "moved" : "deleted"}, and the job pauses when the account is full.
      Not sure? Ask your IT department, or choose “Leave the original where it is”.${free}</div>`;
  };
  if (!remote) return render(null);
  if (!(remote in ABOUT)) {
    render(null);
    ABOUT[remote] = api("GET", "about?path=" + encodeURIComponent(remote)).catch(e => ({ error: e.message }));
  }
  render(await ABOUT[remote]);
}

ACT["save-job"] = async el => {
  const body = formValues();
  if (el.dataset.id) body.id = el.dataset.id;
  try {
    const saved = await api("POST", "jobs", body);
    toast("Saved");
    location.hash = `#/jobs/${saved.id}`;
  } catch (e) {
    $("#form-error").innerHTML = `<div class="banner bad"><b>Can't save yet</b>${esc(e.message)}</div>`;
    $("#form-error").scrollIntoView({ behavior: "smooth", block: "center" });
  }
};

/* ------------------------------------------------------------------ folder picker */

let PICK = null;
ACT.browse = async el => {
  const input = el.dataset.target ? $(`#jobform [name=${el.dataset.target}]`)
    : $(`#routes input[data-route="${el.dataset.routeTarget}"][data-key=destination]`);
  const remotes = await api("GET", "remotes");
  if (!remotes.length) return toast("Add a connection first (Connections page)");
  const current = input.value.includes(":") ? input.value : remotes[0].name + ":";
  PICK = { input, remotes, path: current };
  renderPicker();
};
async function renderPicker() {
  const [remote, sub] = [PICK.path.split(":")[0], PICK.path.split(":").slice(1).join(":")];
  const parts = sub.split("/").filter(Boolean);
  openModal(`<h2>Choose a folder</h2>
    <label class="field"><span>Connection</span><select id="pick-remote">${PICK.remotes.map(r =>
      `<option ${r.name === remote ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select></label>
    <div class="crumbs"><button data-act="pick-go" data-path="${esc(remote)}:">${esc(remote)}:</button>
      ${parts.map((p, i) => `/<button data-act="pick-go" data-path="${esc(remote + ":" + parts.slice(0, i + 1).join("/"))}">${esc(p)}</button>`).join("")}</div>
    <div class="folders" id="pick-list"><div class="muted" style="padding:10px">Loading…</div></div>
    <div class="row" style="margin-top:12px"><button class="primary" data-act="pick-use">Use “${esc(PICK.path)}”</button>
      <button data-act="close-modal">Cancel</button></div>`);
  $("#pick-remote").addEventListener("change", e => { PICK.path = e.target.value + ":"; renderPicker(); });
  try {
    const dirs = await api("GET", "browse?path=" + encodeURIComponent(PICK.path));
    const base = PICK.path.endsWith(":") || PICK.path.endsWith("/") ? PICK.path : PICK.path + "/";
    $("#pick-list").innerHTML = dirs.map(d => `<button data-act="pick-go" data-path="${esc(base + d.name)}">📁 ${esc(d.name)}</button>`).join("")
      || `<div class="muted" style="padding:10px">No sub-folders here.</div>`;
  } catch (e) {
    $("#pick-list").innerHTML = `<div class="banner bad" style="margin:8px">${esc(e.message)}</div>`;
  }
}
ACT["pick-go"] = el => { PICK.path = el.dataset.path; renderPicker(); };
ACT["pick-use"] = () => {
  PICK.input.value = PICK.path;
  PICK.input.dispatchEvent(new Event("input", { bubbles: true }));
  PICK.input.dispatchEvent(new Event("change", { bubbles: true }));
  closeModal();
};

function openModal(html) {
  $("#modal").innerHTML = `<div class="modal-bg" data-act="modal-bg"><div class="modal" role="dialog">${html}</div></div>`;
}
function closeModal() { $("#modal").innerHTML = ""; clearInterval(setupTimer); }
ACT["close-modal"] = () => closeModal();
ACT["modal-bg"] = (el, e) => { if (e.target === el) closeModal(); };

/* ------------------------------------------------------------------ connections */

async function viewConnections() {
  const remotes = await api("GET", "remotes");
  view(banners() + `<div class="row spread"><h1>Connections</h1><button class="primary" data-act="add-remote">+ Add connection</button></div>
    <p class="muted">A connection is one storage account, like your OneDrive, a Google Drive, Dropbox, a server or a local disk.
    Settings are kept in <span class="path">${esc(STATE.rclone.config_file)}</span>.</p>
    ${remotes.length ? `<div class="card wrap"><table><thead><tr><th>Name</th><th>Type</th><th></th></tr></thead><tbody>
    ${remotes.map(r => `<tr><td><b>${esc(r.name)}</b><div id="test-${esc(r.name)}"></div></td><td>${esc(r.type)}</td>
      <td class="row" style="justify-content:flex-end"><button class="small" data-act="test-remote" data-name="${esc(r.name)}">Test</button>
      <button class="small danger" data-act="delete-remote" data-name="${esc(r.name)}">Delete</button></td></tr>`).join("")}
    </tbody></table></div>` : `<div class="card empty"><p>No connections yet.</p><button class="primary" data-act="add-remote">Add your first connection</button></div>`}`);
}

ACT["test-remote"] = async el => {
  const box = document.getElementById("test-" + el.dataset.name);
  box.innerHTML = `<div class="small muted">Testing…</div>`;
  const r = await api("POST", `remotes/${encodeURIComponent(el.dataset.name)}/test`);
  if (!r.ok) { box.innerHTML = `<div class="banner bad small" style="margin:6px 0 0"><b>Not working</b>${esc(r.error)}</div>`; return; }
  const about = r.about ? ` · ${B(r.about.used)} used${r.about.total ? " of " + B(r.about.total) : ""}${r.about.free != null ? " · " + B(r.about.free) + " free" : ""}` : "";
  const sample = r.entries.slice(0, 8).map(e => (e.dir ? "📁 " : "📄 ") + e.name).join("  ");
  box.innerHTML = `<div class="banner ok small" style="margin:6px 0 0"><b>Works ✓</b>${r.count} items in the top folder${esc(about)}
    <div class="muted" style="margin-top:4px">${esc(sample)}</div></div>`;
};
ACT["delete-remote"] = async el => {
  if (!confirm(`Remove the connection "${el.dataset.name}" from cloudhop? Files in the account are not touched.`)) return;
  await api("DELETE", `remotes/${encodeURIComponent(el.dataset.name)}`);
  toast("Connection removed");
  route();
};

const POPULAR = ["onedrive", "drive", "dropbox", "s3", "sftp", "webdav", "box", "pcloud", "mega", "protondrive", "local", "smb"];
let SETUP = null, setupTimer = null;

ACT["add-remote"] = async () => {
  openModal(`<h2>Add a connection</h2><p class="muted">Loading the list of supported services…</p>`);
  const providers = await api("GET", "providers");
  SETUP = { providers, type: null };
  renderTypePicker("");
};
function renderTypePicker(q) {
  const list = SETUP.providers.filter(p => !q || (p.name + " " + p.description).toLowerCase().includes(q.toLowerCase()))
    .sort((a, b) => (POPULAR.indexOf(a.name) + 1 || 99) - (POPULAR.indexOf(b.name) + 1 || 99));
  openModal(`<h2>Add a connection</h2><p class="muted">What kind of storage is it?</p>
    <input id="type-q" placeholder="Search, e.g. onedrive, google, dropbox, ftp…" value="${esc(q)}">
    <div class="types" style="margin-top:10px">${list.map(p => `<button class="type" data-act="pick-type" data-type="${esc(p.name)}">
      <b>${esc(p.description)}</b><span>${esc(p.name)}</span></button>`).join("") || '<p class="muted">Nothing matches.</p>'}</div>
    <div class="row" style="margin-top:12px"><button data-act="close-modal">Cancel</button></div>`);
  const input = $("#type-q");
  input.focus(); input.setSelectionRange(q.length, q.length);
  input.addEventListener("input", () => renderTypePicker(input.value));
}
ACT["pick-type"] = el => { SETUP.type = SETUP.providers.find(p => p.name === el.dataset.type); renderTypeForm(false); };

function optionInput(o, value, prefix = "opt-") {
  const id = prefix + o.name, val = value ?? o.default ?? "";
  if (o.type === "bool") {
    return `<select id="${id}" data-opt="${esc(o.name)}"><option value="">default (${esc(o.default || "false")})</option>
      <option value="true">yes</option><option value="false">no</option></select>`;
  }
  if (o.examples.length && o.exclusive) {
    return `<select id="${id}" data-opt="${esc(o.name)}">${o.required ? "" : '<option value="">(default)</option>'}
      ${o.examples.map(e => `<option value="${esc(e.value)}" ${e.value === val ? "selected" : ""}>${esc(e.help ? e.help.split("\n")[0] : e.value)}</option>`).join("")}</select>`;
  }
  const list = o.examples.length ? `<datalist id="dl-${id}">${o.examples.map(e => `<option value="${esc(e.value)}">${esc(e.help.split("\n")[0])}</option>`).join("")}</datalist>` : "";
  return `<input id="${id}" data-opt="${esc(o.name)}" type="${o.password ? "password" : "text"}" ${list ? `list="dl-${id}"` : ""}
    placeholder="${esc(o.default || "")}">${list}`;
}
function renderTypeForm(showAdvanced) {
  const t = SETUP.type;
  const opts = t.options.filter(o => (showAdvanced || !o.advanced) && !o.provider);
  opts.sort((a, b) => b.required - a.required);
  openModal(`<h2>${esc(t.description)}</h2>
    <label class="field"><span>Name for this connection</span><input id="remote-name" value="${esc(SETUP.name || t.name)}">
      <div class="help">Short and simple, e.g. <span class="mono">onedrive</span> or <span class="mono">work-drive</span>. You'll use it in jobs as <span class="mono">name:folder</span>.</div></label>
    <p class="muted small">Fill in what you know. Fields left empty use sensible defaults; for most personal accounts you don't need to change anything.</p>
    ${opts.map(o => `<label class="field"><span>${esc(o.name.replace(/_/g, " "))}${o.required ? " *" : ""}</span>${optionInput(o)}
      <div class="help" style="white-space:pre-line">${esc(o.help.length > 400 ? o.help.slice(0, 400) + "…" : o.help)}</div></label>`).join("")}
    <div class="row" style="margin:6px 0 10px"><button data-act="toggle-advanced" data-on="${showAdvanced ? "" : "1"}">${showAdvanced ? "Hide" : "Show"} advanced options</button></div>
    <div id="setup-error"></div>
    <div class="row"><button class="primary" data-act="create-remote">Continue</button><button data-act="add-remote">Back</button></div>`);
}
ACT["toggle-advanced"] = el => { SETUP.name = $("#remote-name").value; renderTypeForm(!!el.dataset.on); };
ACT["create-remote"] = async () => {
  const params = {};
  $$("[data-opt]", $("#modal")).forEach(i => { if (i.value) params[i.dataset.opt] = i.value; });
  const name = $("#remote-name").value.trim();
  try {
    const session = await api("POST", "remotes", { name, type: SETUP.type.name, params });
    renderSetup(session);
  } catch (e) {
    $("#setup-error").innerHTML = `<div class="banner bad"><b>Can't continue</b>${esc(e.message)}</div>`;
  }
};

function renderSetup(s) {
  clearInterval(setupTimer);
  SETUP.session = s;
  if (s.status === "done") {
    openModal(`<h2>Connection added ✓</h2><p>“${esc(s.name)}” is ready. Let's check that it works:</p>
      <div id="test-${esc(s.name)}"></div>
      <div class="row" style="margin-top:12px"><button class="primary" data-act="close-modal">Done</button></div>`);
    ACT["test-remote"]({ dataset: { name: s.name } }).catch(e => toast(e.message));
    if (location.hash === "#/connections") viewConnections().then(() => {});
    return;
  }
  if (s.status === "authorizing") {
    openModal(`<h2>Log in to finish</h2>
      <p>A browser window should have opened. Log in there and allow access; this page continues by itself.</p>
      ${s.auth_url ? `<p>No window? <a href="${esc(s.auth_url)}" target="_blank" rel="noopener">Open the login page</a>.</p>` : '<p class="muted">Preparing the login page…</p>'}
      <p class="small muted">Work or school accounts: if you see “Need admin approval”, your IT department has to allow the app first.</p>
      <div class="row"><button data-act="cancel-setup">Cancel</button></div>`);
    setupTimer = setInterval(async () => {
      const next = await api("GET", `remotes/setup/${s.id}`);
      if (next.status !== "authorizing" || next.auth_url !== s.auth_url) renderSetup(next);
    }, 1500);
    return;
  }
  if (s.status === "working") {
    openModal(`<h2>Setting up…</h2><p class="muted">One moment.</p>`);
    setupTimer = setInterval(async () => renderSetup(await api("GET", `remotes/setup/${s.id}`)), 1500);
    return;
  }
  if (s.status === "error") {
    openModal(`<h2>Setup didn't finish</h2><div class="banner bad">${esc(s.error)}</div>
      <div class="row"><button data-act="add-remote">Start again</button><button data-act="close-modal">Close</button></div>`);
    return;
  }
  const q = s.question;
  openModal(`<h2>One more question</h2>
    ${s.error ? `<div class="banner bad">${esc(s.error)}</div>` : ""}
    <label class="field"><span>${esc(q.name.replace(/_/g, " "))}</span>
      ${optionInput({ ...q, examples: q.examples, exclusive: q.exclusive || q.examples.length > 0 && q.type !== "string" }, q.default, "q-")}
      <div class="help" style="white-space:pre-line">${esc(q.help)}</div></label>
    <div class="row"><button class="primary" data-act="answer-setup">Continue</button><button data-act="cancel-setup">Cancel</button></div>`);
}
ACT["answer-setup"] = async () => {
  const input = $("[data-opt]", $("#modal"));
  const s = await api("POST", `remotes/setup/${SETUP.session.id}/answer`, { result: input ? input.value : "" });
  renderSetup(s);
};
ACT["cancel-setup"] = async () => {
  clearInterval(setupTimer);
  if (SETUP && SETUP.session) await api("POST", `remotes/setup/${SETUP.session.id}/cancel`).catch(() => {});
  closeModal();
};

/* ------------------------------------------------------------------ settings */

async function viewSettings() {
  await loadState();
  const r = STATE.rclone, st = STATE.settings;
  view(`<h1>Settings</h1>
  <div class="card"><h2>rclone</h2>
    <p class="muted">cloudhop uses <a href="https://rclone.org" target="_blank" rel="noopener">rclone</a> (free and open source, MIT licence) to talk to storage services.</p>
    <p>${r.path ? `Version <b>${esc(r.version || "?")}</b> · <span class="path">${esc(r.path)}</span>${r.managed ? " (downloaded by cloudhop)" : ""}` : "<b>Not installed.</b>"}</p>
    <div class="row"><button data-act="install-rclone">${r.managed ? "Update rclone" : "Download the latest rclone"}</button>
      <span class="small muted">${esc(r.install.message || "")}</span></div>
    ${field("rclone_path", "Use a specific rclone program (optional)", st.rclone_path, "Leave empty to use cloudhop's own copy, or the rclone already installed on this computer.", 'placeholder="automatic"')}
  </div>
  <div class="card"><h2>Connections file</h2>
    ${field("rclone_config", "rclone config file", st.rclone_config, `Leave empty for cloudhop's own file. Currently: <span class="path">${esc(r.config_file)}</span>`, 'placeholder="cloudhop default"')}
    ${STATE.detected_config ? `<p class="small">Found an existing file at <span class="path">${esc(STATE.detected_config.path)}</span>
      <button class="small" data-act="use-config" data-path="${esc(STATE.detected_config.path)}">Use it</button></p>` : ""}
  </div>
  <div class="card"><h2>Running jobs</h2>
    ${field("max_concurrent_runs", "Jobs allowed to run at the same time", st.max_concurrent_runs, "Others wait in line.", 'type="number" min="1" max="8"')}
    ${check("keep_awake", st.keep_awake, "Keep the computer awake while jobs run", "Stops the computer from going to sleep in the middle of a transfer.")}
  </div>
  <button class="primary" data-act="save-settings">Save settings</button>`);
}
ACT["save-settings"] = async () => {
  const v = name => $(`[name=${name}]`);
  await api("POST", "settings", {
    rclone_path: v("rclone_path").value, rclone_config: v("rclone_config").value,
    max_concurrent_runs: +v("max_concurrent_runs").value || 1, keep_awake: v("keep_awake").checked,
  });
  await loadState();
  toast("Settings saved");
  route();
};

/* ------------------------------------------------------------------ start */

window.addEventListener("hashchange", route);
loadState().then(route).catch(e => { if (e.message !== "locked") view(`<div class="banner bad">${esc(e.message)}</div>`); });
