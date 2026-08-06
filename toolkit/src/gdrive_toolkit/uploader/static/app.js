"use strict";

const $ = (sel) => document.querySelector(sel);

let drives = [];
let sources = [];              // [{path, is_dir, staged, name, size}]
let selectedDriveId = "";
let selectedDestPath = null;   // null until a drive is chosen, then "" (root) by default
let evtSource = null;

function fmtSize(b) {
  if (b == null || b < 0) return "?";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (i === 0 ? b : b.toFixed(1)) + " " + u[i];
}

// ---- api helper -------------------------------------------------------
async function api(url, opts) {
  const o = Object.assign({ headers: { "Content-Type": "application/json" } }, opts);
  const r = await fetch(url, o);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

// ---- sources (left pane) ----------------------------------------------
function renderSources() {
  const list = $("#src-list");
  list.innerHTML = "";
  $("#src-count").textContent = sources.length;
  $("#src-hint").hidden = sources.length > 0;
  let total = 0;
  for (const s of sources) {
    if (s.size >= 0) total += s.size;
    const li = document.createElement("li");
    li.innerHTML = `<span class="ico">${s.is_dir ? "📁" : "📄"}</span>` +
      `<span class="nm" title="${s.path}">${s.name}</span>` +
      `<span class="sz">${fmtSize(s.size)}</span>` +
      `<span class="x">✕</span>`;
    li.querySelector(".x").addEventListener("click", () => {
      sources = sources.filter((x) => x.path !== s.path);
      renderSources();
      updateUploadEnabled();
    });
    list.appendChild(li);
  }
  $("#src-total").textContent = fmtSize(total);
  updateUploadEnabled();
}

async function addPicked(paths, isDir) {
  const fresh = paths.filter((p) => !sources.some((s) => s.path === p));
  if (!fresh.length) return;
  let stats;
  try {
    stats = await api("/api/stat", { method: "POST", body: JSON.stringify({ paths: fresh }) });
  } catch (e) {
    stats = fresh.map((p) => ({ path: p, name: p.split("/").pop(), is_dir: isDir, size: -1 }));
  }
  for (const st of stats) {
    if (st.exists === false) continue;
    sources.push({ path: st.path, is_dir: st.is_dir, staged: false, name: st.name, size: st.size });
  }
  renderSources();
}

async function pick(kind) {
  try {
    const res = await api("/api/pick", { method: "POST", body: JSON.stringify({ kind }) });
    await addPicked(res.paths, kind === "folders");
  } catch (e) {
    alert("Pick failed: " + e.message);
  }
}

// ---- destination (right pane) -----------------------------------------
function fillDrives(sel) {
  sel.innerHTML = '<option value="">— choose —</option>' +
    drives.map((d) => `<option value="${d.id}">${d.name}</option>`).join("");
}

function driveName(id) {
  return (drives.find((d) => d.id === id) || {}).name || id;
}

function renderBreadcrumb() {
  const bc = $("#breadcrumb");
  if (!selectedDriveId || selectedDestPath === null) { bc.textContent = ""; return; }
  const segs = selectedDestPath === "" ? [] : selectedDestPath.split("/");
  bc.textContent = [driveName(selectedDriveId), ...segs].join(" / ");
}

function refreshDestRadios() {
  document.querySelectorAll("#tree .row[data-path]").forEach((row) => {
    const radio = row.querySelector("input[type=radio]");
    if (radio) radio.checked = row.dataset.path === selectedDestPath;
  });
}

function selectDest(path) {
  selectedDestPath = path;
  refreshDestRadios();
  renderBreadcrumb();
  $("#new-folder").disabled = false;
  updateUploadEnabled();
}

function makeDestRow(node) {
  const row = document.createElement("div");
  row.className = "row";
  row.dataset.drive = node.drive_id;
  row.dataset.path = node.path;

  const tw = document.createElement("span");
  tw.className = "tw";
  tw.textContent = "▸";
  row.appendChild(tw);

  const radio = document.createElement("input");
  radio.type = "radio";
  radio.name = "dest-radio";
  radio.addEventListener("change", () => selectDest(node.path));
  row.appendChild(radio);

  const name = document.createElement("span");
  name.className = "name dir";
  name.textContent = node.name;
  name.addEventListener("click", () => selectDest(node.path));
  row.appendChild(name);

  const expand = () => toggleExpand(node, row, tw);
  tw.addEventListener("click", expand);

  return row;
}

async function toggleExpand(node, row, tw) {
  const next = row.nextElementSibling;
  if (next && next.classList.contains("children") && next.dataset.for === node.path) {
    next.hidden = !next.hidden;
    tw.textContent = next.hidden ? "▸" : "▾";
    return;
  }
  tw.textContent = "▾";
  const box = document.createElement("div");
  box.className = "children";
  box.dataset.for = node.path;
  box.innerHTML = '<div class="loading">Loading…</div>';
  row.after(box);
  await fillChildrenBox(node, box);
}

async function fillChildrenBox(node, box) {
  try {
    const items = await api(`/api/browse?drive_id=${encodeURIComponent(node.drive_id)}&path=${encodeURIComponent(node.path)}`);
    box.innerHTML = items.length ? "" : '<div class="loading">(no subfolders)</div>';
    for (const it of items) {
      box.appendChild(makeDestRow({ drive_id: node.drive_id, path: it.path, name: it.name }));
    }
    refreshDestRadios();
  } catch (e) {
    box.innerHTML = `<div class="loading">error: ${e.message}</div>`;
  }
}

async function loadTree(drive_id) {
  const tree = $("#tree");
  selectedDriveId = drive_id;
  if (!drive_id) {
    selectedDestPath = null;
    tree.innerHTML = '<p class="hint">Pick a shared drive to browse.</p>';
    $("#new-folder").disabled = true;
    renderBreadcrumb();
    updateUploadEnabled();
    return;
  }
  tree.innerHTML = '<div class="loading">Loading…</div>';
  try {
    const items = await api(`/api/browse?drive_id=${encodeURIComponent(drive_id)}&path=`);
    tree.innerHTML = "";
    const dname = driveName(drive_id);
    tree.appendChild(makeDestRow({ drive_id, path: "", name: `${dname} (root)` }));
    for (const it of items) {
      tree.appendChild(makeDestRow({ drive_id, path: it.path, name: it.name }));
    }
    selectDest("");   // whole-drive root is the default destination
  } catch (e) {
    tree.innerHTML = `<p class="hint">error: ${e.message}</p>`;
  }
}

async function newFolder() {
  if (!selectedDriveId || selectedDestPath === null) return;
  const name = window.prompt("New folder name:");
  if (!name) return;
  try {
    await api("/api/mkdir", {
      method: "POST",
      body: JSON.stringify({ drive_id: selectedDriveId, path: selectedDestPath, name }),
    });
    if (selectedDestPath === "") {
      await loadTree(selectedDriveId);
    } else {
      const row = document.querySelector(`#tree .row[data-path="${CSS.escape(selectedDestPath)}"]`);
      const box = row && row.nextElementSibling;
      if (row && box && box.classList.contains("children") && !box.hidden) {
        await fillChildrenBox({ drive_id: selectedDriveId, path: selectedDestPath }, box);
      }
    }
  } catch (e) {
    alert("Couldn't create folder: " + e.message);
  }
}

// ---- upload + progress --------------------------------------------------
function updateUploadEnabled() {
  $("#upload").disabled = !(sources.length > 0 && selectedDriveId && selectedDestPath !== null);
}

async function startUpload() {
  const body = {
    sources: sources.map((s) => ({ path: s.path, is_dir: s.is_dir, staged: !!s.staged })),
    drive_id: selectedDriveId,
    dest_path: selectedDestPath,
  };
  let res;
  try {
    res = await api("/api/upload", { method: "POST", body: JSON.stringify(body) });
  } catch (e) {
    alert("Upload failed to start: " + e.message);
    return;
  }
  $("#upload").disabled = true;
  $("#cancel").hidden = false;
  $("#cancel").dataset.group = res.group_id;
  const prog = $("#progress");
  prog.hidden = false;
  if (evtSource) evtSource.close();
  evtSource = new EventSource(`/api/progress/${res.group_id}`);
  evtSource.onmessage = (ev) => renderProgress(JSON.parse(ev.data));
  evtSource.onerror = () => { evtSource.close(); };
}

function bar(pct, cls) { return `<div class="bar-wrap"><div class="bar ${cls}" style="width:${pct}%"></div></div>`; }

function renderProgress(p) {
  const prog = $("#progress");
  let html = `<div class="overall"><div class="pline"><span class="nm">Overall</span><span>${p.overall_pct}%</span></div>${bar(p.overall_pct, p.done ? "done" : "")}</div>`;
  for (const j of p.jobs) {
    const cls = j.state === "error" ? "error" : j.state === "done" ? "done" : "";
    const meta = j.state === "error" ? (j.error || "error")
      : j.state === "running" ? `${j.speed} · ETA ${j.eta}` : j.state;
    html += `<div><div class="pline"><span class="nm">${j.label}</span><span>${meta}</span></div>${bar(j.pct, cls)}`;
    if (j.files && j.files.length) {
      html += `<div class="files">` + j.files.map((f) => `<div>${f.pct}% ${f.name}</div>`).join("") + `</div>`;
    }
    html += `</div>`;
  }
  prog.innerHTML = html;
  if (p.done) {
    $("#cancel").hidden = true;
    updateUploadEnabled();
    if (evtSource) evtSource.close();
  }
}

async function cancelUpload() {
  const g = $("#cancel").dataset.group;
  if (g) await api(`/api/cancel/${g}`, { method: "POST", body: "{}" });
}

// ---- theme (light / dark) -------------------------------------------------
// <html data-theme> is already stamped before first paint by the inline script
// in index.html; this only keeps the button's icon in sync and persists a
// change. Default is dark. Mirrors drivecast's toggle so the suite's web apps
// behave identically.
//
// Wired at top level rather than inside boot(): boot() awaits /api/drives, and
// the theme must still be togglable when rclone is slow or the drive list
// fails outright.
function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}
function applyTheme(theme) {
  const light = theme === "light";
  document.documentElement.setAttribute("data-theme", light ? "light" : "dark");
  try { localStorage.setItem("theme", light ? "light" : "dark"); } catch (e) { /* private mode */ }
  const btn = $("#theme-toggle");
  const icon = btn && btn.querySelector(".theme-icon");
  if (icon) icon.textContent = light ? "☀️" : "🌙";
}
applyTheme(currentTheme());   // sync the icon with the pre-paint choice
$("#theme-toggle").addEventListener("click", () =>
  applyTheme(currentTheme() === "light" ? "dark" : "light"));

// ---- boot -----------------------------------------------------------------
async function boot() {
  const sel = $("#drive-select");
  try {
    drives = await api("/api/drives");
    fillDrives(sel);
  } catch (e) {
    sel.innerHTML = `<option value="">error: ${e.message}</option>`;
  }
  sel.addEventListener("change", () => loadTree(sel.value));
  $("#refresh").addEventListener("click", async () => { drives = await api("/api/drives?force=1"); fillDrives(sel); });
  $("#add-files").addEventListener("click", () => pick("files"));
  $("#add-folders").addEventListener("click", () => pick("folders"));
  $("#new-folder").addEventListener("click", newFolder);
  $("#upload").addEventListener("click", startUpload);
  $("#cancel").addEventListener("click", cancelUpload);
  renderSources();
}
boot();
