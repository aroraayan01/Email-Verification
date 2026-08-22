const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

/* ---------------- theme ---------------- */
const savedTheme = localStorage.getItem("theme");
if (savedTheme) document.documentElement.dataset.theme = savedTheme;
else if (matchMedia("(prefers-color-scheme: dark)").matches)
  document.documentElement.dataset.theme = "dark";

$("#themeToggle").onclick = () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("theme", next);
};

/* ---------------- sidebar nav ---------------- */
const TITLES = { single: "Single check", bulk: "Bulk verification",
                 find: "Find email", history: "History" };
function showView(key) {
  if (!$("#view-" + key)) return false;
  $$(".nav-item[data-view]").forEach((t) =>
    t.classList.toggle("active", t.dataset.view === key));
  $$(".view").forEach((v) => v.classList.remove("active"));
  $("#view-" + key).classList.add("active");
  if ($("#pageTitle")) $("#pageTitle").textContent = TITLES[key] || "";
  if ($("#sidebar")) $("#sidebar").classList.remove("open");
  if (key === "history") loadHistory();
  return true;
}

$$(".nav-item[data-view]").forEach((item) => {
  item.addEventListener("click", (e) => {
    // On /app these switch panes in place; elsewhere the href navigates here.
    if (showView(item.dataset.view)) {
      e.preventDefault();
      history.replaceState(null, "", "#" + item.dataset.view);
    }
  });
});
// Honour /app#bulk style deep links.
if (location.hash) showView(location.hash.slice(1));
if ($("#sideToggle")) {
  $("#sideToggle").onclick = () => $("#sidebar").classList.toggle("open");
}

/* ---------------- donut chart ---------------- */
const DONUT_COLORS = {
  valid: "var(--good)", likely_valid: "var(--good)",
  invalid: "var(--bad)", catch_all: "var(--warn)",
  unknown: "var(--neutral)", duplicate: "var(--neutral)", disposable: "var(--neutral)",
};
function donut(counts, total) {
  const R = 68, C = 2 * Math.PI * R;
  const order = ["valid", "likely_valid", "invalid", "catch_all", "unknown",
                 "duplicate", "disposable"];
  const entries = order.filter((k) => counts[k]).map((k) => [k, counts[k]]);
  for (const k in counts) if (!order.includes(k) && counts[k]) entries.push([k, counts[k]]);
  let offset = 0;
  const segs = entries.map(([k, v]) => {
    const len = total ? (v / total) * C : 0;
    const s = `<circle cx="84" cy="84" r="${R}" fill="none" stroke="${DONUT_COLORS[k] || "var(--neutral)"}"
      stroke-width="26" stroke-dasharray="${len} ${C - len}" stroke-dashoffset="${-offset}"></circle>`;
    offset += len;
    return s;
  }).join("");
  const rows = entries.map(([k, v]) => `
    <div class="dl-row">
      <span class="swatch" style="background:${DONUT_COLORS[k] || "var(--neutral)"}"></span>
      <span class="name">${esc(String(k).replace(/_/g, " "))}</span>
      <span class="nums"><b>${v.toLocaleString()}</b><span>${total ? ((v / total) * 100).toFixed(1) : 0}%</span></span>
    </div>`).join("");
  return `<div class="donut">
      <svg viewBox="0 0 168 168">
        <circle cx="84" cy="84" r="${R}" fill="none" stroke="var(--surface-3)" stroke-width="26"></circle>
        ${segs}
      </svg>
      <div class="center"><b>${total.toLocaleString()}</b><span>emails</span></div>
    </div>
    <div class="donut-legend">${rows}</div>`;
}

/* ---------------- helpers ---------------- */
const LABEL = {
  valid: "Confirmed real — safe to send",
  invalid: "Undeliverable — do not send",
  catch_all: "Server accepts everything, so nobody can confirm this one",
  unknown: "Not provable here — send to your paid verifier",
  likely_valid: "Best guess: real. Based on the address pattern, not a probe.",
  likely_invalid: "Best guess: not real. Based on the address pattern.",
  duplicate: "Duplicate of an earlier row",
  disposable: "Throwaway address provider",
};
const TIER = {
  cache: "saved result",
  local: "syntax / DNS",
  microsoft: "Microsoft directory",
  smtp: "mail server",
  vendor: "needs paid check",
  "": "needs paid check",
};
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function confBar(conf) {
  if (conf === "" || conf === null || conf === undefined) return "";
  const n = parseInt(conf, 10);
  if (Number.isNaN(n)) return "";
  const color = n >= 70 ? "var(--good)" : n <= 35 ? "var(--bad)" : "var(--warn)";
  return `<div class="conf">
    <div class="conf-track"><div class="conf-fill" style="width:${n}%;background:${color}"></div></div>
    <span class="conf-num">${n}%</span>
  </div>`;
}

/* ---------------- single check ---------------- */
$("#singleForm").onsubmit = async (e) => {
  e.preventDefault();
  const email = $("#singleInput").value.trim();
  if (!email) return;
  const box = $("#singleResult");
  const btn = $("#singleBtn");
  btn.disabled = true;
  btn.textContent = "Checking…";
  box.classList.remove("hidden");
  box.innerHTML = '<div class="muted">Checking…</div>';

  try {
    const res = await fetch("/api/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || "request failed");

    box.innerHTML = `
      <div class="result-top">
        <span class="result-email">${esc(d.email)}</span>
        <span class="pill ${esc(d.status)}">${esc(d.status.replace("_", " "))}</span>
      </div>
      <p style="margin:0 0 14px">${esc(LABEL[d.status] || "")}</p>
      <dl class="kv">
        <dt>Checked by</dt><dd>${esc(TIER[d.tier] || d.tier)}</dd>
        <dt>Mail host</dt><dd>${esc(d.route || "—")}</dd>
        ${d.confidence != null ? `<dt>Confidence</dt><dd>${confBar(d.confidence)}</dd>` : ""}
        <dt>Details</dt><dd>${esc(d.reason || "—")}</dd>
        ${d.suggestion ? `<dt>Did you mean</dt><dd><strong>${esc(d.suggestion)}</strong></dd>` : ""}
      </dl>`;
  } catch (err) {
    box.innerHTML = `<div class="err">${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Verify";
  }
};

/* ---------------- find email ---------------- */
const FIND_LABEL = {
  found: "Verified — this address is real. Safe to send.",
  guess: "Best-guess pattern. The domain can't be verified (catch-all), so confirm with Clearout/Hunter before sending.",
  not_found: "No pattern was accepted — the name may be spelled differently, or not at this domain.",
  unknown: "Couldn't check this one.",
};
const FIND_PILL = { found: "valid", guess: "catch_all", not_found: "invalid", unknown: "unknown" };

$("#findForm").onsubmit = async (e) => {
  e.preventDefault();
  const name = $("#findName").value.trim();
  const domain = $("#findDomain").value.trim();
  if (!name || !domain) return;
  const box = $("#findResult");
  const btn = $("#findBtn");
  btn.disabled = true; btn.textContent = "Finding…";
  box.classList.remove("hidden");
  box.innerHTML = '<div class="muted">Trying patterns…</div>';
  try {
    const res = await fetch("/api/find", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, domain }),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || "request failed");
    const pill = FIND_PILL[d.status] || "unknown";
    const emailRow = d.email
      ? `<div class="found-email">${esc(d.email)}
           <button class="copy-btn" onclick="navigator.clipboard.writeText('${esc(d.email)}')">Copy</button>
         </div>` : "";
    box.innerHTML = `
      <div class="result-top">
        <span class="result-email">${esc(name)} @ ${esc(domain)}</span>
        <span class="pill ${pill}">${esc(d.status.replace("_", " "))}</span>
      </div>
      ${emailRow}
      <p style="margin:0 0 12px">${esc(FIND_LABEL[d.status] || "")}</p>
      <dl class="kv">
        <dt>How</dt><dd>${esc(d.method || "—")}</dd>
        ${d.confidence ? `<dt>Confidence</dt><dd>${confBar(d.confidence)}</dd>` : ""}
        ${d.tried ? `<dt>Found on try</dt><dd>#${d.tried} of ${d.candidates.length}</dd>` : ""}
        <dt>Patterns tried</dt><dd class="muted" style="font-family:ui-monospace,monospace;font-size:12.5px">${d.candidates.map(esc).join("<br>")}</dd>
      </dl>`;
  } catch (err) {
    box.innerHTML = `<div class="err">${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = "Find";
  }
};

/* ---------------- bulk find ---------------- */
const dzFind = $("#dzFind");
const fileFind = $("#fileFind");
let findJob = null, findPoller = null;

if (dzFind) {
  dzFind.onclick = () => fileFind.click();
  dzFind.ondragover = (e) => { e.preventDefault(); dzFind.classList.add("over"); };
  dzFind.ondragleave = () => dzFind.classList.remove("over");
  dzFind.ondrop = (e) => { e.preventDefault(); dzFind.classList.remove("over");
    if (e.dataTransfer.files.length) uploadFind(e.dataTransfer.files[0]); };
  fileFind.onchange = () => { if (fileFind.files.length) uploadFind(fileFind.files[0]); };
}

async function uploadFind(file) {
  const body = new FormData();
  body.append("file", file);
  $("#findJobPanel").classList.remove("hidden");
  $("#findJobActions").classList.add("hidden");
  $("#findJobName").textContent = file.name;
  $("#findJobStatus").textContent = "uploading";
  $("#findJobStatus").className = "pill queued";
  $("#findJobBar").style.width = "0%";
  $("#findJobStage").textContent = "Uploading…";
  try {
    const res = await fetch("/api/bulk-find", { method: "POST", body });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || "upload failed");
    findJob = d.job_id;
    $("#findJobStage").textContent = `Found ${d.found} names. Looking them up…`;
    clearInterval(findPoller);
    findPoller = setInterval(pollFind, 1000);
    pollFind();
  } catch (err) {
    $("#findJobStage").innerHTML = `<span class="err">${esc(err.message)}</span>`;
    $("#findJobStatus").textContent = "failed";
    $("#findJobStatus").className = "pill failed";
  }
  fileFind.value = "";
}

async function pollFind() {
  if (!findJob) return;
  const res = await fetch(`/api/jobs/${findJob}`);
  if (!res.ok) return;
  const j = await res.json();
  $("#findJobStatus").textContent = j.status;
  $("#findJobStatus").className = "pill " + j.status;
  const pct = j.total ? Math.round((j.done / j.total) * 100) : (j.status === "done" ? 100 : 8);
  $("#findJobBar").style.width = pct + "%";
  $("#findJobStage").textContent =
    j.status === "done" ? `Done — found ${j.resolved} of ${j.unique_in}.`
    : j.status === "failed" ? (j.error || "Failed.")
    : `${j.stage || "Working"}${j.total ? ` — ${j.done}/${j.total}` : "…"}`;
  if (j.status === "done") {
    clearInterval(findPoller);
    $("#dlFind").href = `/api/jobs/${findJob}/download?which=all`;
    $("#findJobActions").classList.remove("hidden");
    loadStats();
  }
  if (j.status === "failed") clearInterval(findPoller);
}

/* ---------------- bulk upload ---------------- */
const dz = $("#dropzone");
const fileInput = $("#fileInput");

const threshold = $("#threshold");
threshold.oninput = () => {
  const v = parseInt(threshold.value, 10);
  $("#threshValue").textContent = v === 0 ? "off" : v + "%";
};

dz.onclick = () => fileInput.click();
dz.ondragover = (e) => { e.preventDefault(); dz.classList.add("over"); };
dz.ondragleave = () => dz.classList.remove("over");
dz.ondrop = (e) => {
  e.preventDefault();
  dz.classList.remove("over");
  if (e.dataTransfer.files.length) upload(e.dataTransfer.files[0]);
};
fileInput.onchange = () => { if (fileInput.files.length) upload(fileInput.files[0]); };

let currentJob = null;
let poller = null;

async function upload(file) {
  const body = new FormData();
  body.append("file", file);
  const thresh = parseInt(threshold.value, 10) || 0;
  $("#jobPanel").classList.remove("hidden");
  $("#resultsPanel").classList.add("hidden");
  $("#jobSummary").classList.add("hidden");
  $("#jobActions").classList.add("hidden");
  $("#jobName").textContent = file.name;
  $("#jobStatus").textContent = "uploading";
  $("#jobStatus").className = "pill queued";
  $("#jobBar").style.width = "0%";
  $("#jobStage").textContent = "Uploading…";

  try {
    const res = await fetch(`/api/bulk?threshold=${thresh}`, { method: "POST", body });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || "upload failed");
    currentJob = d.job_id;
    $("#jobStage").textContent = `Found ${d.found} addresses. Starting…`;
    clearInterval(poller);
    poller = setInterval(pollJob, 900);
    pollJob();
  } catch (err) {
    $("#jobStage").innerHTML = `<span class="err">${esc(err.message)}</span>`;
    $("#jobStatus").textContent = "failed";
    $("#jobStatus").className = "pill failed";
  }
  fileInput.value = "";
}

async function pollJob() {
  if (!currentJob) return;
  const res = await fetch(`/api/jobs/${currentJob}`);
  if (!res.ok) return;
  const j = await res.json();

  $("#jobStatus").textContent = j.status;
  $("#jobStatus").className = "pill " + j.status;
  const pct = j.total ? Math.round((j.done / j.total) * 100) : (j.status === "done" ? 100 : 8);
  $("#jobBar").style.width = pct + "%";
  $("#jobStage").textContent =
    j.status === "done" ? "Finished."
    : j.status === "failed" ? j.error || "Failed."
    : `${j.stage || "Working"}${j.total ? ` — ${j.done}/${j.total}` : "…"}`;

  if (j.status === "done") {
    clearInterval(poller);
    const counts = j.counts || {};
    const total = Object.values(counts).reduce((a, b) => a + b, 0) || j.unique_in || 0;
    const dw = $("#jobDonut");
    if (dw) { dw.innerHTML = donut(counts, total); dw.classList.remove("hidden"); }
    $("#dlClearout").href = `/api/jobs/${currentJob}/download?which=clearout`;
    $("#dlResolved").href = `/api/jobs/${currentJob}/download?which=resolved`;
    $("#dlAll").href = `/api/jobs/${currentJob}/download?which=all`;
    $("#jobActions").classList.remove("hidden");
    loadResults("");
    loadStats();
  }
  if (j.status === "failed") clearInterval(poller);
}

/* ---------------- results table ---------------- */
$$(".chip").forEach((chip) => {
  chip.onclick = () => {
    $$(".chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    loadResults(chip.dataset.filter);
  };
});

async function loadResults(status) {
  if (!currentJob) return;
  const res = await fetch(
    `/api/jobs/${currentJob}/results?which=all&status=${encodeURIComponent(status)}&limit=300`);
  const d = await res.json();
  const tbody = $("#resultsTable tbody");
  tbody.innerHTML = d.rows
    .map(
      (r) => `<tr>
        <td class="email">${esc(r.email)}</td>
        <td><span class="pill ${esc(r.status)}">${esc(String(r.status).replace(/_/g, " "))}</span></td>
        <td>${confBar(r.confidence)}</td>
        <td>${esc(TIER[r.tier] ?? r.tier)}</td>
        <td class="why">${esc(r.reason)}</td>
      </tr>`)
    .join("");
  $("#resultsMore").textContent =
    d.total > d.rows.length
      ? `Showing ${d.rows.length} of ${d.total}. Download the CSV for the full list.`
      : `${d.total} row${d.total === 1 ? "" : "s"}.`;
  $("#resultsPanel").classList.remove("hidden");
}

/* ---------------- history ---------------- */
async function loadHistory() {
  const list = $("#historyList");
  const [jobsRes, histRes] = await Promise.all([
    fetch("/api/jobs"),
    fetch("/api/history"),
  ]);
  const { jobs } = await jobsRes.json();
  const { checks } = await histRes.json();

  if (!jobs.length && !checks.length) {
    list.innerHTML = '<p class="muted">Nothing yet. Run a check or upload a list to get started.</p>';
    return;
  }

  // Single verify + find checks, newest first, as a compact table.
  let single = "";
  if (checks.length) {
    single = `<h3 class="hist-sub">Single checks</h3>
      <div class="table-wrap"><table><thead><tr>
      <th>When</th><th>Type</th><th>Query</th><th>Result</th><th>Via</th>
      </tr></thead><tbody>` +
      checks.map((c) => `<tr>
        <td class="muted">${esc((c.at || "").replace("T", " ").slice(0, 19))}</td>
        <td>${esc(c.kind)}</td>
        <td class="email">${esc(c.query)}</td>
        <td><span class="pill ${esc((c.result || "").split(" ")[0])}">${esc(c.result || "—")}</span></td>
        <td class="muted">${esc(c.via)}</td>
      </tr>`).join("") +
      `</tbody></table></div>`;
  }

  const jobsHtml = !jobs.length ? "" :
    `<h3 class="hist-sub">Bulk uploads</h3><div class="hist-grid">` + jobs
    .map((j) => {
      const counts = j.counts || {};
      const total = Object.values(counts).reduce((a, b) => a + b, 0) || j.unique_in || 0;
      const chart = j.status === "done" && total
        ? `<div class="donut-wrap">${donut(counts, total)}</div>` : "";
      return `<div class="hist-card">
        <div class="hist-head">
          <span class="pill ${esc(j.status)}">${esc(j.status)}</span>
          <span class="fname" title="${esc(j.filename)}">${esc(j.filename)}</span>
        </div>
        <div class="hist-meta">${esc((j.created_at || "").replace("T", " ").replace("+00:00", " UTC"))}</div>
        ${chart}
        <div class="hist-actions">
          ${j.status === "done"
            ? `<a href="/api/jobs/${j.id}/download?which=all">Download</a>
               <a href="/api/jobs/${j.id}/download?which=clearout">Review list</a>` : ""}
          <span class="spacer"></span>
          <button class="del" data-id="${j.id}" title="Delete">×</button>
        </div>
      </div>`;
    })
    .join("") + `</div>`;
  list.innerHTML = single + jobsHtml;
  $$(".del").forEach((btn) => {
    btn.onclick = async () => {
      await fetch(`/api/jobs/${btn.dataset.id}`, { method: "DELETE" });
      loadHistory();
    };
  });
}

/* ---------------- sidebar quota ---------------- */
async function loadStats() {
  try {
    const d = await (await fetch("/api/me")).json();
    if (d.email && $("#acctEmail")) $("#acctEmail").textContent = d.email;
    if ($("#qUsed")) {
      $("#qUsed").textContent = (d.used_today ?? 0).toLocaleString();
      $("#qTotal").textContent = "/ " + (d.daily_quota ?? 0).toLocaleString();
      const pct = d.daily_quota ? Math.min(100, (d.used_today / d.daily_quota) * 100) : 0;
      $("#qBar").style.width = pct + "%";
    }
  } catch { /* non-critical */ }
}
loadStats();
