"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => {
  if (n === null || n === undefined || n === "") return "–";
  n = Number(n);
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (n >= 1e4) return Math.round(n / 1e3) + "K";
  if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "K";
  return String(Math.round(n));
};

// ---- tabs ----
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + b.dataset.tab));
}));

// ---- funnel ----
function renderFunnel(el, f) {
  const steps = [["reviewed", "accounts reviewed"], ["in_market", "in the market"], ["in_band", "in the size band"],
                 ["judged", "judged by AI"], ["accepted", "accepted accounts"]];
  const top = Math.max(f.reviewed || 1, 1);
  el.innerHTML = steps.map(([k, label], i) => {
    const v = f[k] || 0, w = Math.max(3, Math.round(100 * v / top));
    return `<div class="step ${i === steps.length - 1 ? "last" : ""}"><div class="n">${fmt(v)}</div><div class="l">${label}</div><div class="bar" style="width:${w}%"></div></div>`;
  }).join("");
}

function renderYield(el, rows) {
  const body = rows.filter((r) => r.credits || r.accepted).map((r) => `<tr><td>${esc(r.source)}</td><td class="r">${r.credits}</td><td class="r">${r.accepted}</td><td class="r">${r.accepted_per_100_credits ?? "–"}</td></tr>`).join("");
  el.innerHTML = `<tr><th>Source</th><th class="r">Credits</th><th class="r">Accepted</th><th class="r">per 100 cr</th></tr>${body}`;
}

// ---- rows ----
let allRows = [];
function platformPills(r) {
  return (r.platforms || "").split(" + ").filter(Boolean).map((p) => `<span class="pill ${p === "TikTok" ? "tt" : "yt"}">${esc(p)}</span>`).join("");
}
function views(r) {
  const parts = [];
  if (r.avg_views_tiktok) parts.push(`TT ${fmt(r.avg_views_tiktok)}`);
  if (r.avg_views_youtube) parts.push(`YT ${fmt(r.avg_views_youtube)}`);
  if (r.avg_views_youtube_shorts) parts.push(`Shorts ${fmt(r.avg_views_youtube_shorts)}`);
  return parts.join(" · ") || "–";
}
function size(r) {
  const parts = [];
  if (r.followers_tiktok) parts.push(`TT ${fmt(r.followers_tiktok)}`);
  if (r.subscribers_youtube) parts.push(`YT ${fmt(r.subscribers_youtube)}`);
  return parts.join(" · ") || "–";
}
function renderRows(target, rows, countEl) {
  const q = ($("#q")?.value || "").toLowerCase();
  const list = $("#list-filter")?.value || "shortlist";
  const shown = rows.filter((r) => (list === "all" || r.list === list) &&
    (!q || JSON.stringify([r.creator, r.games, r.niche, r.reasons, r.platforms]).toLowerCase().includes(q)));
  if (countEl) countEl.textContent = `(${shown.length})`;
  const head = `<tr><th>Creator</th><th>Platforms</th><th>Country</th><th class="num">Followers / subs</th><th class="num">Avg views</th><th>Niche</th><th>Games</th><th class="num">Fit</th><th>Risks</th></tr>`;
  const body = shown.map((r, i) => `
    <tr class="item" data-i="${i}">
      <td><b>${esc(r.creator)}</b></td><td>${platformPills(r)}</td><td>${esc(r.country || r.market)}</td>
      <td class="num">${size(r)}</td><td class="num">${views(r)}</td><td>${esc(r.niche)}</td>
      <td>${esc(r.games)}</td><td class="num">${r.fit_score ?? "–"}</td>
      <td>${r.risks ? r.risks.split("; ").map((x) => `<span class="pill warn">${esc(x)}</span>`).join("") : ""}</td>
    </tr>`).join("");
  target.innerHTML = head + body;
  target.querySelectorAll("tr.item").forEach((tr) => tr.addEventListener("click", () => {
    const next = tr.nextElementSibling;
    if (next && next.classList.contains("detail")) { next.remove(); return; }
    const r = shown[Number(tr.dataset.i)];
    const d = document.createElement("tr");
    d.className = "detail";
    const links = [r.tiktok_url && `<a href="${esc(r.tiktok_url)}" target="_blank" rel="noopener">TikTok ↗</a>`,
                   r.youtube_url && `<a href="${esc(r.youtube_url)}" target="_blank" rel="noopener">YouTube ↗</a>`].filter(Boolean).join(" · ");
    d.innerHTML = `<td colspan="9"><div class="grid">
      <div><b>Why</b>${esc(r.reasons)}</div>
      <div><b>Evidence (verbatim)</b><div class="quote">${esc(r.evidence_quote)}</div></div>
      <div><b>Found via</b>${esc(r.found_via)}</div>
      <div><b>Market evidence</b>${esc(r.market_evidence)}</div>
      <div><b>Views window</b>${esc([r.views_window_tiktok && "TikTok: " + r.views_window_tiktok, r.views_window_youtube && "YouTube: " + r.views_window_youtube, r.views_window_youtube_shorts && "Shorts: " + r.views_window_youtube_shorts].filter(Boolean).join(" · "))}</div>
      <div><b>YouTube format</b>${esc(r.youtube_format || "–")}</div>
      <div><b>Trend</b>${esc(r.trend || "–")}</div>
      <div><b>Contact</b>${esc(r.contact)}${r.other_links ? "<br>" + esc(r.other_links) : ""}</div>
      <div><b>Profiles</b>${links}</div>
    </div></td>`;
    tr.after(d);
  }));
}
["#q", "#list-filter"].forEach((s) => $(s)?.addEventListener("input", () => renderRows($("#rows"), allRows, $("#count"))));

// Accepted accounts vs creators: a creator accepted on both TikTok and YouTube is one row.
function creatorsLabel(run) {
  const accounts = run.funnel.accepted || 0, creators = run.creators ?? accounts, merged = accounts - creators;
  return merged > 0
    ? `${creators} creators (${accounts} accounts; ${merged} ${merged === 1 ? "has" : "have"} both TikTok and YouTube, merged)`
    : `${creators} creators`;
}

// ---- demo ----
async function loadDemo() {
  const runs = await (await fetch("/api/demo/runs")).json();
  const cards = $("#run-cards");
  if (!runs.length) { cards.innerHTML = '<div class="muted">No saved runs in this deployment.</div>'; return; }
  cards.innerHTML = runs.map((r) => `
    <div class="card" data-id="${r.id}">
      <div class="muted small">${esc(r.market_name)} · ${(r.params.platforms || ["tiktok"]).join(" + ")}</div>
      <h3>${esc(r.brief || "Run " + r.id)}</h3>
      <div class="big">${r.creators ?? r.funnel.accepted} creators</div>
      <div class="stats"><span>${fmt(r.funnel.reviewed)} reviewed</span><span>${r.funnel.accepted} accounts</span><span>${r.credits} credits</span>
      ${r.recall ? `<span>partners found: ${r.recall.found}/${r.recall.total}</span>` : ""}</div>
    </div>`).join("");
  cards.querySelectorAll(".card").forEach((c) => c.addEventListener("click", () => showRun("demo", runs.find((r) => String(r.id) === c.dataset.id))));
  showRun("demo", runs[0]);
}

async function showRun(source, run) {
  document.querySelectorAll(".card").forEach((c) => c.classList.toggle("sel", c.dataset.id === String(run.id)));
  $("#run-view").hidden = false;
  $("#run-title").textContent = `${run.market_name}: ${creatorsLabel(run)}`;
  const p = run.params || {};
  $("#run-sub").textContent = `${(p.platforms || ["tiktok"]).join(" + ")} · TikTok ${fmt(p.band_min)}–${fmt(p.band_max)} · YouTube ${fmt(p.yt_band_min)}–${fmt(p.yt_band_max)} · ${run.credits} credits · ${run.llm_calls || 0} LLM calls`;
  $("#dl-full").href = `/api/${source}/runs/${run.id}/export?layout=full`;
  $("#dl-prenew").href = `/api/${source}/runs/${run.id}/export?layout=prenew`;
  renderFunnel($("#funnel"), run.funnel);
  renderYield($("#yield"), run.yield || []);
  $("#recall").innerHTML = run.recall
    ? `<div class="recall-big">${run.recall.found} of ${run.recall.total}</div><p class="muted">of the client's existing partners in this market were found by the tool on its own, without using the list as input.${run.recall.note ? " " + esc(run.recall.note) : ""}</p>`
    : '<p class="muted">No hold-out check for this run.</p>';
  allRows = await (await fetch(`/api/${source}/runs/${run.id}/rows`)).json();
  renderRows($("#rows"), allRows, $("#count"));
}

// ---- live ----
async function loadConfig() {
  const cfg = await (await fetch("/api/config")).json();
  $("#market").innerHTML = cfg.markets.map((m) => `<option value="${m.code.toLowerCase()}">${esc(m.name)}</option>`).join("");
  $("#budget").max = cfg.max_budget;
  $("#live-note").textContent = cfg.live_enabled
    ? `Live runs are capped at ${cfg.max_budget} credits and about ${Math.round(cfg.live_seconds / 60)} minutes.`
    : "Live runs are not enabled on this deployment. Saved runs work fully.";
  if (!cfg.live_enabled) $("#go").disabled = true;
}

$("#live-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const body = { market: fd.get("market"), preset: fd.get("preset"), target: Number(fd.get("target")),
                 budget: Number(fd.get("budget")), access_code: fd.get("access_code"),
                 platforms: fd.getAll("platforms") };
  const log = $("#log"); log.innerHTML = ""; $("#live-results").innerHTML = "";
  const add = (st, msg) => { const li = document.createElement("li"); li.innerHTML = `<span class="st">${esc(st)}</span>${esc(msg)}`; log.prepend(li); };
  $("#go").disabled = true; $("#go").textContent = "Running…";
  try {
    const res = await fetch("/api/live", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!res.ok) { add("error", (await res.json()).detail || res.statusText); return; }
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!chunk.startsWith("data: ")) continue;
        const e = JSON.parse(chunk.slice(6));
        if (e.stage === "started") { add("start", `run ${e.run_id} in ${e.market}, budget ${e.budget} credits`); continue; }
        if (e.stage === "failed") { add("error", "run failed: " + e.message); continue; }
        if (e.stage === "finished") { await showLiveResult(e.run, e.rows, e.files); continue; }
        if (e.message) add(e.stage, e.message);
        if (e.funnel) {
          const f = e.funnel;
          renderFunnel($("#live-funnel"), { reviewed: f.seen, in_market: (f.bucket_sure || 0) + (f.bucket_unsure || 0),
            in_band: (f.bucket_sure || 0) + (f.bucket_unsure || 0) - (f.filtered_band || 0), judged: f.judged, accepted: f.accepted });
        }
        if (e.budget) { $("#credit-bar").style.width = Math.min(100, 100 * e.credits_used / e.budget) + "%"; $("#credit-text").textContent = `Credits used: ${e.credits_used} / ${e.budget}`; }
      }
    }
  } catch (err) { add("error", String(err)); }
  finally { $("#go").disabled = false; $("#go").textContent = "Run (about 4 minutes)"; }
});

// Workbooks arrive inside the finished event, so downloading never depends on
// which server instance ran the job.
function downloadAttrs(file, fallback) {
  if (!file) return `href="${esc(fallback)}"`;
  const bytes = Uint8Array.from(atob(file.data), (c) => c.charCodeAt(0));
  const url = URL.createObjectURL(new Blob([bytes], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }));
  return `href="${url}" download="${esc(file.name)}"`;
}

async function showLiveResult(run, rows, files) {
  rows = rows || await (await fetch(`/api/live/runs/${run.id}/rows`)).json();
  files = files || {};
  const box = $("#live-results");
  box.innerHTML = `<div class="box"><div class="tablebar"><h3>${esc(run.market_name)}: ${esc(creatorsLabel(run))} <span class="muted">(${run.credits} credits)</span></h3>
    <div class="actions"><a class="btn" ${downloadAttrs(files.full, `/api/live/runs/${run.id}/export?layout=full`)}>Download XLSX</a>
    <a class="btn primary" ${downloadAttrs(files.prenew, `/api/live/runs/${run.id}/export?layout=prenew`)}>Download in Prenew format</a></div></div>
    <div class="tablewrap"><table class="rows" id="live-rows"></table></div></div>`;
  renderFunnel($("#live-funnel"), run.funnel);
  const listSel = $("#list-filter"); const prev = listSel.value; listSel.value = "all";
  renderRows($("#live-rows"), rows, null);
  listSel.value = prev;
}

loadConfig();
loadDemo();
