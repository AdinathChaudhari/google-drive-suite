// drivecast frontend v2 — cached library, tiles, seasons/episodes, hash routing.
"use strict";

const $ = (id) => document.getElementById(id);

// The app is split into tabs, each with its own nav entry, accent colour and
// vocabulary. A tab is user-defined data (config["tabs"] — see
// TABS_REFACTOR.md): its "behavior" (entertainment/courses/podcasts/a private
// plugin) supplies the classifier, mimes and vocab, but the tab itself —
// label, icon, accent — only exists once someone builds it in Settings.
// There are ZERO tabs by default, so there is no built-in fallback to fall
// back to: SECTION_META/SECTION_ORDER start empty and are only ever filled by
// /api/sections. A drive assigned to a tab that's since been deleted (or
// never assigned at all) belongs to no tab — rec.section can be null.
let SECTION_META = {};
let SECTION_ORDER = [];
// [{key,label}, ...] straight from /api/sections — feeds the create-tab
// "behaves like" picker (built-in behaviors + any loaded plugin behaviors).
let BEHAVIORS = [];

async function loadSections() {
  try {
    const data = await api("/api/sections");
    const list = data.sections || [];
    SECTION_META = {};
    SECTION_ORDER = [];
    for (const m of list) {
      SECTION_META[m.key] = m;
      SECTION_ORDER.push(m.key);
    }
    BEHAVIORS = data.behaviors || [];
  } catch (_) { /* fetch failed — keep whatever we last knew rather than
                   inventing phantom builtins that don't exist server-side */ }
}

// On a phone/tablet the page is served over the LAN/tailnet (not loopback), so
// playback stays in the browser / hands off to VLC/Infuse rather than launching
// mpv on the Mac. remoteToken is fetched lazily from /api/remote (needed for the
// external-player deep links; the same-origin <video> uses the cookie instead).
const REMOTE_LOCAL_HOSTS = ["127.0.0.1", "localhost", "::1", "[::1]"];

const state = {
  library: [],          // cached title records from /api/library
  byId: {},             // id -> record
  section: null,        // active tab key; null = no live tab (zero-tab onboarding)
  remote: !REMOTE_LOCAL_HOSTS.includes(location.hostname),
  remoteToken: "",      // secret token for external-player stream URLs
  remoteInfo: null,     // cached /api/remote payload (urls, port, token)
  filter: "all",        // all | movie | show | documentary | other
  sort: "title",        // title | year | added | watched
  group: "none",        // none | type | drive
  query: "",            // client-side search over the library
  watchedMap: {},       // file_id -> last_played epoch (for "Recently watched")
  progress: {},         // file_id -> {percent, watched} (lesson/episode progress)
  selectedDrives: [],
  driveSections: {},    // drive_id -> section
  autoplayNext: true,   // autoplay next episode when one finishes
  // browse (advanced) sub-state
  drives: [],
  driveName: {},
  driveId: null,
  folderId: null,
  crumbs: [],
  nextPageToken: null,
  browseView: "drives", // drives | browse | search
};

function sectionOf(rec) { return rec.section || null; }

// Tabs are the only source of what shows up in the nav bar — visible as soon
// as at least one exists, regardless of whether any drive is assigned to it
// yet (a brand new tab still deserves its own empty-state screen).
function sectionsActive() {
  return SECTION_ORDER.length > 0;
}

function setSection(sec) {
  // Coerce an unknown/stale/missing key to the first live tab, or to no tab
  // at all on a zero-tab install — there's no "entertainment" to fall back
  // to anymore.
  if (!sec || !SECTION_META[sec]) sec = SECTION_ORDER[0] || null;
  state.section = sec;
  document.body.dataset.section = sec || "";
  // Tab accents come from the metadata (so custom/plugin tabs theme
  // themselves); no active tab clears back to the stylesheet default.
  const m = (sec && SECTION_META[sec]) || {};
  if (m.accent) {
    document.body.style.setProperty("--accent", m.accent);
    document.body.style.setProperty("--accent-2", m.accent2 || m.accent);
  } else {
    document.body.style.removeProperty("--accent");
    document.body.style.removeProperty("--accent-2");
  }
  try {
    if (sec) localStorage.setItem("dc.section", sec);
    else localStorage.removeItem("dc.section");
  } catch (_) {}
}

function renderTabs() {
  const nav = $("sectionTabs");
  if (!sectionsActive()) { show(nav, false); return; }
  nav.innerHTML = "";
  for (const sec of SECTION_ORDER) {
    const m = SECTION_META[sec];
    const a = document.createElement("a");
    a.className = "section-tab" + (sec === state.section ? " active" : "");
    a.innerHTML = `<span class="tab-icon">${escapeHTML(m.icon)}</span> ${escapeHTML(m.label)}`;
    a.addEventListener("click", () => { location.hash = "#/s/" + sec; });
    nav.appendChild(a);
  }
  show(nav, true);
}

// ---------- utilities ----------
async function api(path, opts) {
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const msg = (data && (data.message || data.error)) || `HTTP ${res.status}`;
    const err = new Error(msg);
    err.payload = data;
    err.status = res.status;
    throw err;
  }
  return data;
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

function fmtTime(sec) {
  sec = Math.floor(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function escapeHTML(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function isFolder(f) { return f.mimeType === "application/vnd.google-apps.folder"; }

function show(el, on) { el.classList.toggle("hidden", !on); }

// Extensions Safari/Chrome play inline in a <video>/<audio> element. Everything
// else (MKV, AVI, …) is handed off to VLC iOS / Infuse on remote devices.
const BROWSER_EXTS = [".mp4", ".m4v", ".mov", ".webm", ".mp3", ".m4a", ".m4b", ".aac", ".wav"];
function canPlayInBrowser(name) {
  const n = String(name || "").toLowerCase();
  return BROWSER_EXTS.some((ext) => n.endsWith(ext));
}

// Load (once, or force-reload) the remote-access info: token + tappable URLs.
// Failure-silent — off/loopback just leaves state.remoteToken empty.
async function ensureRemoteInfo(force) {
  if (state.remoteInfo && !force) return state.remoteInfo;
  try {
    const data = await api("/api/remote");
    state.remoteToken = data.token || "";
    state.remoteInfo = data;
  } catch (_) { if (force) state.remoteInfo = null; }
  return state.remoteInfo;
}

function showView(name) {
  show($("libraryView"), name === "library");
  show($("detailView"), name === "detail");
  show($("settingsView"), name === "settings");
  show($("browseView"), name === "browse");
}

// ---------- poster / placeholder ----------
function posterMarkup(rec) {
  const ph = `<div class="ph-title">${escapeHTML(rec.title)}</div>` +
    (rec.year ? `<div class="ph-year">${rec.year}</div>` : "");
  if (rec.poster) {
    return { cls: "", html: `<img loading="lazy" src="/api/poster/${encodeURIComponent(rec.poster)}" alt=""
      onerror="this.parentElement.classList.add('placeholder');
               this.parentElement.insertAdjacentHTML('beforeend', this.dataset.ph||'');this.remove()" data-ph='${escapeHTML(ph)}'>` };
  }
  return { cls: " placeholder", html: ph };
}

// Quality pill (e.g. "4K", "1080p", "4K HDR") for a poster corner; "" if none.
function qualityPill(rec) {
  return rec.quality ? `<span class="pill">${escapeHTML(rec.quality)}</span>` : "";
}

// ---------- "Fix poster" picker ----------
// Only entertainment movies/shows draw posters from TMDB, so the affordance is
// scoped to that behavior; courses/podcasts use their own artwork.
function canFixPoster(rec) {
  const behavior = (SECTION_META[sectionOf(rec)] || {}).behavior;
  return behavior === "entertainment" && (rec.type === "movie" || rec.type === "show");
}

function fixPosterBtn(rec) {
  if (!canFixPoster(rec)) return "";
  return `<button class="fixposter" title="Fix poster" aria-label="Fix poster">🖼️</button>`;
}

function mediaTypeOf(rec) { return rec.type === "show" ? "tv" : "movie"; }

// Mirror of tmdb._norm_title so client-side record matching lines up with the
// server's override keying.
function normTitle(s) {
  return String(s == null ? "" : s).toLowerCase()
    .replace(/[^\w\s]/g, " ").split(/\s+/).filter(Boolean).join(" ");
}

let posterPickerState = null;  // { rec, posterEl } while the modal is open

function openPosterPicker(rec, posterEl) {
  posterPickerState = { rec, posterEl };
  $("posterModalTitle").textContent = `Fix poster — ${rec.title}`;
  const grid = $("posterCandidates");
  grid.innerHTML = `<div class="spinner">Searching…</div>`;
  show($("posterModal"), true);
  $("posterModalClose").focus();
  loadPosterCandidates(rec);
}

function closePosterPicker() {
  show($("posterModal"), false);
  posterPickerState = null;
}

async function loadPosterCandidates(rec) {
  const grid = $("posterCandidates");
  let data;
  try {
    data = await api(`/api/poster-candidates?title=${encodeURIComponent(rec.title)}` +
      `&type=${encodeURIComponent(mediaTypeOf(rec))}`);
  } catch (e) {
    grid.innerHTML = `<p class="muted">Couldn't load candidates: ${escapeHTML(e.message)}</p>`;
    return;
  }
  const cands = (data && data.candidates) || [];
  if (!cands.length) {
    grid.innerHTML = `<p class="muted">No matches found on TMDB.</p>`;
    return;
  }
  grid.innerHTML = "";
  cands.forEach((c) => grid.appendChild(candidateCard(rec, c)));
}

function candidateCard(rec, c) {
  const card = document.createElement("div");
  card.className = "candidate";
  const poster = c.poster_key
    ? `<img loading="lazy" src="/api/poster/${encodeURIComponent(c.poster_key)}" alt=""
         onerror="this.parentElement.classList.add('placeholder');this.remove()">`
    : "";
  const yr = c.year ? ` (${escapeHTML(c.year)})` : "";
  card.innerHTML = `
    <div class="candidate-poster poster${c.poster_key ? "" : " placeholder"}">${poster}</div>
    <div class="candidate-title">${escapeHTML(c.title)}${yr}</div>
    <div class="candidate-overview">${escapeHTML((c.overview || "").slice(0, 160))}</div>
    <button class="btn primary candidate-use" type="button">Use this</button>`;
  card.querySelector(".candidate-use").addEventListener(
    "click", () => applyPosterOverride(rec, c, card));
  return card;
}

async function applyPosterOverride(rec, cand, card) {
  const btn = card.querySelector(".candidate-use");
  btn.disabled = true;
  btn.textContent = "Saving…";
  let res;
  try {
    res = await api("/api/poster-override", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: rec.title, type: mediaTypeOf(rec),
                             tmdb_id: cand.tmdb_id }),
    });
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "Use this";
    toast(`Couldn't apply poster: ${e.message}`, "error");
    return;
  }
  applyPosterToRecords(rec, res.poster_key, res.title, res.year);
  updatePosterElement(posterPickerState && posterPickerState.posterEl, res.poster_key);
  closePosterPicker();
  toast("Poster updated.", "ok");
}

// Update every loaded record that shares this title + media type, so the fix
// sticks across re-renders (the server already did this for its own records).
function applyPosterToRecords(rec, posterKey, newTitle, newYear) {
  const want = normTitle(rec.title);
  const wantType = rec.type;
  for (const r of state.library) {
    if (r.type === wantType && normTitle(r.title) === want) {
      if (posterKey) r.poster = posterKey;
      if (newYear && !r.year) r.year = newYear;
    }
  }
}

// Swap the tile/detail poster image to the new artwork (cache-busted), or
// replace a placeholder with a fresh <img>.
function updatePosterElement(posterEl, posterKey) {
  if (!posterEl || !posterKey) return;
  const src = `/api/poster/${encodeURIComponent(posterKey)}?v=${Date.now()}`;
  const img = posterEl.querySelector("img");
  if (img) {
    img.src = src;
  } else {
    posterEl.classList.remove("placeholder");
    posterEl.querySelectorAll(".ph-title, .ph-year").forEach((n) => n.remove());
    const el = document.createElement("img");
    el.loading = "lazy";
    el.src = src;
    el.alt = "";
    posterEl.appendChild(el);
  }
}

// ---------- library tiles ----------
function countEpisodes(rec) {
  let n = 0;
  for (const s of rec.seasons || []) n += (s.episodes || []).length;
  return n;
}

// Real seasons only — extras pseudo-seasons (Featurettes/...) are listed in
// the season picker but never counted or shuffled as part of the show.
function realSeasons(rec) {
  return (rec.seasons || []).filter((s) => !s.extras);
}

// Fraction of a show/course's episodes marked watched (0..1).
function courseProgress(rec) {
  let total = 0, done = 0;
  for (const s of rec.seasons || [])
    for (const e of s.episodes || []) {
      if (!e.file_id) continue;
      total++;
      if ((state.progress[e.file_id] || {}).watched) done++;
    }
  return total ? done / total : 0;
}

// CSS conic-gradient progress ring for course tiles (0..1); "" when untouched.
function progressRing(p) {
  if (!p) return "";
  return `<span class="ring${p >= 1 ? " full" : ""}" style="--p:${Math.round(p * 100)}"
    title="${Math.round(p * 100)}% complete"></span>`;
}

function mediaPill(rec) {
  if (rec.media === "audio") return `<span class="pill">♪ audio</span>`;
  if (rec.media === "mixed") return `<span class="pill">♪ + ▶</span>`;
  return "";
}

function titleCard(rec) {
  const card = document.createElement("div");
  card.className = "card video";
  const p = posterMarkup(rec);
  const sec = sectionOf(rec);
  // Branch on the tab's BEHAVIOR, not its (arbitrary, user-chosen) key: a
  // custom tab keyed "my-courses" with behavior "courses" must render like one.
  const behavior = (SECTION_META[sec] || {}).behavior;
  const isShow = rec.type === "show";
  let badge = "", pill = "", sub = "";
  // Movie-shaped records (loose files on a sections drive, or v1-migrated
  // records pre-rescan) fall through to the plain movie subtitle.
  if (behavior === "courses" && isShow) {
    const prog = courseProgress(rec);
    pill = progressRing(prog);
    if (prog >= 1) badge = `<span class="badge done">✓ Done</span>`;
    const mods = (rec.seasons || []).length;
    sub = mods > 1 ? `${mods} modules · ${countEpisodes(rec)} lessons`
                   : `${countEpisodes(rec)} lessons`;
  } else if (behavior === "podcasts" && isShow) {
    pill = mediaPill(rec);
    sub = `${countEpisodes(rec)} episode${countEpisodes(rec) === 1 ? "" : "s"}`;
  } else if (behavior !== "entertainment" && isShow) {
    // Custom plugin behaviors: media pill + counts in the section's own nouns.
    pill = mediaPill(rec);
    const n = nomen(rec);
    const cnt = (rec.seasons || []).length;
    sub = `${cnt} ${n.season.toLowerCase()}${cnt === 1 ? "" : "s"} · ` +
          `${countEpisodes(rec)} ${n.episode.toLowerCase()}${countEpisodes(rec) === 1 ? "" : "s"}`;
  } else if (behavior !== "entertainment") {
    pill = mediaPill(rec) || qualityPill(rec);
    sub = rec.year || "";
  } else {
    badge = rec.type === "show" ? `<span class="badge tv">TV</span>` : "";
    pill = qualityPill(rec);
    sub = rec.type === "show"
      ? `${realSeasons(rec).length} season${realSeasons(rec).length === 1 ? "" : "s"}`
      : (rec.year || "");
  }
  card.innerHTML = `
    <div class="poster${p.cls}">${badge}${pill}${fixPosterBtn(rec)}${p.html}</div>
    <div class="label">${escapeHTML(rec.title)}</div>
    <div class="sub">${escapeHTML(String(sub))}</div>`;
  card.addEventListener("click", () => { location.hash = "#/title/" + encodeURIComponent(rec.id); });
  const fix = card.querySelector(".fixposter");
  if (fix) fix.addEventListener("click", (e) => {
    e.stopPropagation();
    openPosterPicker(rec, card.querySelector(".poster"));
  });
  return card;
}

function continueCard(item) {
  const card = document.createElement("div");
  card.className = "card video";
  const pct = Math.max(2, Math.min(98, item.percent || 0));
  const label = item.title || item.name;
  const progress = `<div class="progress"><span style="width:${pct}%"></span></div>`;
  const ph = `<div class="ph-title">${escapeHTML(label)}</div>` +
    `<div class="ph-year">${fmtTime(item.position)} watched</div>`;
  let cls = " placeholder", inner = ph;
  if (item.poster) {
    cls = "";
    inner = `<img loading="lazy" src="/api/poster/${encodeURIComponent(item.poster)}" alt=""
      onerror="this.parentElement.classList.add('placeholder');
               this.parentElement.insertAdjacentHTML('afterbegin', this.dataset.ph||'');this.remove()" data-ph='${escapeHTML(ph)}'>`;
  }
  const dismiss = `<button class="dismiss" title="Remove from Continue Watching" aria-label="Remove from Continue Watching">×</button>`;
  card.innerHTML = `
    <div class="poster${cls}">${inner}${progress}${dismiss}</div>
    <div class="label">${escapeHTML(label)}</div>
    <div class="sub">${Math.round(item.percent)}% · ${fmtTime(item.position)} watched</div>`;
  card.addEventListener("click", () =>
    playFile({ id: item.file_id, name: item.name, drive_id: item.drive_id, parent_id: item.parent_id },
             item.duration ? item.duration * 1000 : null, true));
  card.querySelector(".dismiss").addEventListener("click", async (e) => {
    e.stopPropagation();
    try {
      await api("/api/continue/" + encodeURIComponent(item.file_id), { method: "DELETE" });
    } catch (_) { /* removing the tile locally is the useful outcome regardless */ }
    card.remove();
    const row = $("continueRow");
    if (row && !row.children.length) show($("continueSection"), false);
  });
  return card;
}

// ---------- data loads ----------
async function loadLibrary() {
  try {
    const data = await api("/api/library");
    state.library = data.titles || [];
    state.selectedDrives = data.selected_drives || [];
    state.byId = {};
    for (const rec of state.library) state.byId[rec.id] = rec;
    if (data.scanning) startScanWatch();
  } catch (e) {
    toast("Could not load library: " + e.message, "error");
  }
}

// Effective category: TMDB-derived when known, else the structural type
// (movie/show), so libraries scanned without a TMDB key still filter sanely.
function categoryOf(rec) {
  return rec.category || (rec.type === "show" ? "show" : "movie");
}

function matchesFilter(rec) {
  if (sectionOf(rec) !== state.section) return false;
  if ((SECTION_META[state.section] || {}).behavior === "entertainment" &&
      state.filter !== "all" && categoryOf(rec) !== state.filter) return false;
  if (state.query) {
    const q = state.query.toLowerCase();
    if (!(rec.title || "").toLowerCase().includes(q)) return false;
  }
  return true;
}

// Update filter-chip labels with live counts ("Documentaries · 7"); chips for
// empty categories are hidden (except All).
function updateFilterChips(items) {
  const counts = {};
  for (const rec of items) counts[categoryOf(rec)] = (counts[categoryOf(rec)] || 0) + 1;
  const labels = { all: "All", movie: "Movies", show: "TV Shows",
                   documentary: "Documentaries", other: "Other" };
  for (const chip of $("filters").children) {
    const f = chip.dataset.filter;
    const n = f === "all" ? items.length : (counts[f] || 0);
    chip.textContent = n && f !== "all" ? `${labels[f]} · ${n}` : labels[f];
    chip.classList.toggle("hidden", f !== "all" && !n && state.filter !== f);
  }
}

// ---------- sorting / grouping (client-side over cached library) ----------
function fileIdsOf(rec) {
  if (rec.type === "show") {
    const ids = [];
    for (const s of rec.seasons || [])
      for (const e of s.episodes || []) if (e.file_id) ids.push(e.file_id);
    return ids;
  }
  return rec.file_id ? [rec.file_id] : [];
}

function lastPlayedOf(rec) {
  let m = 0;
  for (const id of fileIdsOf(rec)) { const t = state.watchedMap[id] || 0; if (t > m) m = t; }
  return m;
}

function sortItems(items) {
  const arr = items.slice();
  const byTitle = (a, b) => (a.title || "").localeCompare(b.title || "");
  if (state.sort === "year") arr.sort((a, b) => ((b.year || 0) - (a.year || 0)) || byTitle(a, b));
  else if (state.sort === "added") arr.sort((a, b) => ((b.added_at || 0) - (a.added_at || 0)) || byTitle(a, b));
  else if (state.sort === "watched") arr.sort((a, b) => (lastPlayedOf(b) - lastPlayedOf(a)) || byTitle(a, b));
  else arr.sort(byTitle);
  return arr;
}

function driveLabel(id) { return state.driveName[id] || id || "Unknown drive"; }

function groupItems(items) {
  // Non-entertainment sections group into shelves by default ("Python",
  // "Audio Series", "Talks", ...) — the classifier sets rec.shelf.
  if ((SECTION_META[state.section] || {}).behavior !== "entertainment" && state.group === "none") {
    const map = {};
    for (const r of items) (map[r.shelf || ""] = map[r.shelf || ""] || []).push(r);
    const keys = Object.keys(map).sort((a, b) => a.localeCompare(b));
    if (keys.length === 1) return [{ key: null, title: null, items }];
    return keys.map((k) => ({ key: k || "_more", title: k || "More", items: map[k] }));
  }
  if (state.group === "type") {
    const movies = items.filter((r) => r.type === "movie");
    const shows = items.filter((r) => r.type === "show");
    const groups = [];
    if (movies.length) groups.push({ key: "movie", title: "Movies", items: movies });
    if (shows.length) groups.push({ key: "show", title: "TV Shows", items: shows });
    return groups;
  }
  if (state.group === "drive") {
    const map = {};
    for (const r of items) (map[r.drive_id || ""] = map[r.drive_id || ""] || []).push(r);
    return Object.keys(map)
      .sort((a, b) => driveLabel(a).localeCompare(driveLabel(b)))
      .map((k) => ({ key: k, title: driveLabel(k), items: map[k] }));
  }
  return [{ key: null, title: null, items }];
}

function renderLibrary() {
  // If the active tab no longer exists (deleted mid-session, or a stale
  // localStorage value from before this install had any tabs) snap to
  // another live tab, or to no tab at all — the hidden tab bar would
  // otherwise leave the library unreachable.
  if (state.section && !SECTION_META[state.section]) setSection(SECTION_ORDER[0] || null);
  renderTabs();

  // Zero-tab onboarding: nothing to show until a tab exists to show it in.
  if (!SECTION_ORDER.length) {
    show($("filters"), false);
    show($("libSection"), false);
    showCta(`<h2>Create your first tab</h2>
      <p>Tabs decide how your library is organized — Movies &amp; TV, Courses,
        Podcasts, or a custom behavior. Create one, then assign drives to it
        in Settings.</p>
      <button class="btn primary" id="ctaCreateTab">Create your first tab</button>`);
    const btn = $("ctaCreateTab");
    if (btn) btn.addEventListener("click", () => {
      openCreateFormOnSettingsLoad = true;
      location.hash = "#/settings";
    });
    return;
  }

  const meta = SECTION_META[state.section] || {};
  const isEntertainment = meta.behavior === "entertainment";
  $("continueHead").textContent = meta.continue || "Continue Watching";
  $("libHead").textContent = meta.lib || "Your Library";
  const grid = $("libGrid");
  grid.innerHTML = "";
  grid.className = "grid";
  const inSection = state.library.filter((r) => sectionOf(r) === state.section);
  show($("filters"), isEntertainment);
  if (isEntertainment) {
    updateFilterChips(inSection.filter((r) => !state.query ||
      (r.title || "").toLowerCase().includes(state.query.toLowerCase())));
  }
  let items = sortItems(state.library.filter(matchesFilter));

  // Empty states / call to action.
  if (!state.selectedDrives.length) {
    show($("libSection"), false);
    showCta(`<h2>Pick your drives</h2>
      <p>Choose which Shared Drives to include, then drivecast builds your library.</p>
      <a class="btn primary" href="#/settings">Open Settings</a>`);
    return;
  }
  if (!state.library.length) {
    show($("libSection"), false);
    showCta(`<h2>Library is empty</h2>
      <p>No titles cached yet. Run a refresh to scan your selected drives.</p>
      <button class="btn primary" onclick="triggerRefresh()">Refresh now</button>`);
    return;
  }
  if (!inSection.length) {
    show($("libSection"), false);
    showCta(`<h2>${meta.icon || ""} Nothing here yet</h2>
      <p>${escapeHTML(meta.empty || "Assign a drive to this tab in Settings.")}</p>
      <a class="btn primary" href="#/settings">Open Settings</a>`);
    return;
  }
  show($("cta"), false);
  show($("libSection"), true);
  if (!items.length) {
    grid.innerHTML = `<div class="empty">No titles match “${escapeHTML(state.query)}”.</div>`;
    return;
  }

  const groups = groupItems(items);
  if (groups.length === 1 && groups[0].key === null) {
    for (const rec of groups[0].items) grid.appendChild(titleCard(rec));
    return;
  }
  // Grouped: render a heading + its own grid per group.
  grid.classList.remove("grid");
  grid.classList.add("lib-grouped");
  for (const g of groups) {
    const sec = document.createElement("section");
    sec.className = "lib-group";
    const h = document.createElement("h3");
    h.className = "group-head";
    h.textContent = g.title;
    sec.appendChild(h);
    const gg = document.createElement("div");
    gg.className = "grid";
    for (const rec of g.items) gg.appendChild(titleCard(rec));
    sec.appendChild(gg);
    grid.appendChild(sec);
  }
}

// Lazy loaders for data the sort/group needs.
async function ensureWatchedMap() {
  try {
    const data = await api("/api/watched-map");
    state.watchedMap = data.map || {};
    state.progress = data.progress || {};
  } catch (_) { /* non-fatal */ }
}

async function ensureDriveNames() {
  if (Object.keys(state.driveName).length) return;
  try {
    const data = await api("/api/drives");
    for (const d of data.drives || []) state.driveName[d.id] = d.name;
  } catch (_) { /* non-fatal — falls back to raw ids */ }
}

function showCta(html) {
  $("cta").innerHTML = html;
  show($("cta"), true);
}

async function loadContinue() {
  try {
    const data = await api("/api/continue");
    // Scope the shelf to the active section. A continue item whose file no
    // longer resolves to a library title (its drive's tab was deleted, or the
    // drive was deselected) carries no section — it belongs to no tab, exactly
    // like an orphaned library record (see sectionOf), so it must NOT fall back
    // to entertainment and resurface there.
    const items = (data.items || []).filter(
      (it) => (it.section || null) === state.section);
    const sec = $("continueSection");
    const row = $("continueRow");
    row.innerHTML = "";
    if (!items.length) { show(sec, false); return; }
    for (const it of items) row.appendChild(continueCard(it));
    show(sec, true);
  } catch (_) { /* non-fatal */ }
}

// ---------- refresh ----------
let scanTimer = null;
async function triggerRefresh() {
  try {
    const res = await api("/api/refresh", { method: "POST" });
    if (res.running && !res.started) toast("A refresh is already running.");
    else toast("Refreshing library…");
    startScanWatch();
  } catch (e) {
    if (e.status === 400) toast(e.message, "error");
    else toast("Refresh failed: " + e.message, "error");
  }
}
window.triggerRefresh = triggerRefresh;

function startScanWatch() {
  $("refreshBtn").classList.add("spinning");
  if (scanTimer) return;
  scanTimer = setInterval(pollScan, 1200);
  pollScan();
}

async function pollScan() {
  let st;
  try { st = await api("/api/refresh/status"); } catch (_) { return; }
  const bar = $("scanBar");
  if (st.running) {
    show(bar, true);
    const names = st.scope_names || [];
    const label = names.length && names.length <= 3
      ? `Refreshing ${names.join(", ")}… ${st.scanned}/${st.total}`
      : `Scanning drives… ${st.scanned}/${st.total}`;
    bar.textContent = label + (st.added ? ` · +${st.added} new` : "");
  } else {
    clearInterval(scanTimer); scanTimer = null;
    $("refreshBtn").classList.remove("spinning");
    show(bar, false);
    if (st.error) toast("Scan finished with issues: " + st.error, "error");
    await loadLibrary();
    if (currentRoute() === "library") { renderLibrary(); loadContinue(); }
  }
}

// ---------- detail view ----------
async function openDetail(id) {
  showView("detail");
  const body = $("detailBody");
  let rec = state.byId[id];
  if (!rec) {
    try { rec = await api("/api/title/" + encodeURIComponent(id)); }
    catch (_) { body.innerHTML = `<div class="empty">Title not found.</div>`; return; }
  }
  // Fresh progress so checkmarks / Resume-course / default module reflect
  // what you just finished watching (not the page-load snapshot).
  await ensureWatchedMap();
  if (rec.type === "show") renderShowDetail(rec);
  else renderMovieDetail(rec);
  const fix = body.querySelector(".detail-poster .fixposter");
  if (fix) fix.addEventListener("click", (e) => {
    e.stopPropagation();
    openPosterPicker(rec, body.querySelector(".detail-poster"));
  });
}

// Season/episode nouns for a record's section ("Module"/"Lesson", ...).
function nomen(rec) {
  const m = SECTION_META[sectionOf(rec)] || {};
  return { season: m.season || "Season", episode: m.episode || "Episode" };
}

function detailHeader(rec) {
  const p = posterMarkup(rec);
  const sec = sectionOf(rec);
  const n = nomen(rec);
  const count = realSeasons(rec).length;
  let subBits = [];
  if (rec.year) subBits.push(String(rec.year));
  if (rec.type === "show" && count)
    subBits.push(`${count} ${n.season.toLowerCase()}${count === 1 ? "" : "s"}`);
  if (sec === "courses") {
    const prog = courseProgress(rec);
    if (prog > 0) subBits.push(`${Math.round(prog * 100)}% complete`);
  }
  const pill = (SECTION_META[state.section] || {}).behavior === "entertainment"
    ? qualityPill(rec) : mediaPill(rec);
  return `
    <div class="detail-hero">
      <div class="detail-poster poster${p.cls}">${pill}${fixPosterBtn(rec)}${p.html}</div>
      <div class="detail-meta">
        <h1>${escapeHTML(rec.title)}</h1>
        <div class="detail-sub">${escapeHTML(subBits.join(" · "))}</div>
        <p class="detail-overview">${escapeHTML(rec.overview || "")}</p>
        <div id="detailActions" class="detail-actions"></div>
      </div>
    </div>`;
}

function renderMovieDetail(rec) {
  $("detailBody").innerHTML = detailHeader(rec) + `<div id="movieExtras"></div>`;
  const actions = $("detailActions");
  const play = document.createElement("button");
  play.className = "btn primary";
  play.textContent = "Play";
  play.addEventListener("click", () => playFile(
    { id: rec.file_id, name: rec.title, drive_id: rec.drive_id, parent_id: rec.folder_id },
    rec.duration_ms || null, false, [], rec.media || null));
  actions.appendChild(play);
  renderMovieExtras(rec);
}

// Movie featurettes/extras: labelled groups of bonus clips shown below the
// Play button. Each clip plays as a SINGLE item (no autoplay queue), so a
// featurette never chains into another clip or the feature.
function renderMovieExtras(rec) {
  const box = $("movieExtras");
  if (!box) return;
  box.innerHTML = "";
  const groups = (rec.extras || []).filter((g) => (g.episodes || []).length);
  if (!groups.length) return;
  groups.forEach((g) => {
    const head = document.createElement("h3");
    head.className = "materials-head";
    head.textContent = g.name || "Extras";
    box.appendChild(head);
    const list = document.createElement("div");
    list.className = "episodes";
    (g.episodes || []).forEach((ep) => {
      if (!ep.file_id) return;
      const row = document.createElement("div");
      row.className = "episode";
      const prog = state.progress[ep.file_id] || {};
      if (prog.watched) row.classList.add("watched");
      const mark = prog.watched ? `<span class="ep-done">✓</span>`
        : (ep.media === "audio" ? `<span class="ep-audio">♪</span>` : "");
      const pct = !prog.watched && prog.percent > 2
        ? `<div class="progress"><span style="width:${Math.min(98, prog.percent)}%"></span></div>` : "";
      row.innerHTML = `
        <span class="ep-num"></span>
        <span class="ep-title">${escapeHTML(ep.title || ep.name)}</span>
        <span class="ep-dur">${ep.duration_ms ? fmtTime(ep.duration_ms / 1000) : ""}</span>
        <span class="ep-play">${mark || "▶"}</span>${pct}`;
      row.addEventListener("click", () => playFile(
        { id: ep.file_id, name: ep.name, drive_id: rec.drive_id, parent_id: ep.parent_id },
        ep.duration_ms || null, false, [], ep.media || null));
      list.appendChild(row);
    });
    box.appendChild(list);
  });
}

function renderShowDetail(rec) {
  const n = nomen(rec);
  $("detailBody").innerHTML = detailHeader(rec) + `
    <div class="season-select">
      <label>${escapeHTML(n.season)}</label>
      <select id="seasonSel"></select>
    </div>
    <div id="episodeList" class="episodes"></div>
    <div id="materialsBox"></div>`;

  const actions = $("detailActions");
  const sec = sectionOf(rec);
  const eps = allEpisodes(rec);

  if (sec === "courses") {
    // Resume course: first unwatched lesson, queueing everything after it
    // (across modules) so autoplay carries the whole course.
    const next = eps.find((e) => !(state.progress[e.file_id] || {}).watched);
    const resumeBtn = document.createElement("button");
    resumeBtn.className = "btn primary";
    resumeBtn.textContent = !next ? "Replay course"
      : (next === eps[0] ? "Start course" : "Resume course");
    resumeBtn.addEventListener("click", () => {
      const start = next || eps[0];
      if (!start) { toast("No lessons found."); return; }
      const queue = eps.slice(eps.indexOf(start) + 1).map(queueItem);
      playFile(
        { id: start.file_id, name: start.name, drive_id: rec.drive_id, parent_id: start.parent_id },
        start.duration_ms || null, false, queue, start.media || rec.media);
    });
    actions.appendChild(resumeBtn);
  } else {
    // Shuffle: play every episode in a random order (autoplay carries
    // through the shuffled queue).
    const shuffleBtn = document.createElement("button");
    shuffleBtn.className = "btn";
    shuffleBtn.innerHTML = "⤨ Shuffle";
    shuffleBtn.addEventListener("click", () => {
      const order = shuffle(eps);
      if (!order.length) { toast("No episodes to shuffle."); return; }
      toast(`Shuffling ${order.length} episode${order.length === 1 ? "" : "s"}`);
      const first = order[0];
      const queue = order.slice(1).map(queueItem);
      playFile(
        { id: first.file_id, name: first.name, drive_id: rec.drive_id, parent_id: first.parent_id },
        first.duration_ms || null, true, queue, first.media || rec.media);
    });
    actions.appendChild(shuffleBtn);
  }
  if (state.autoplayNext) {
    const hint = document.createElement("span");
    hint.className = "autoplay-hint";
    hint.textContent = "Autoplay on";
    actions.appendChild(hint);
  }

  const seasons = rec.seasons || [];
  const sel = $("seasonSel");
  seasons.forEach((s, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    opt.textContent = s.name
      ? s.name
      : (s.season === 0 ? "Specials" : `${n.season} ${s.season}`);
    sel.appendChild(opt);
  });
  // Open on the season/module of the first unwatched episode (falls back to
  // the first season).
  let startIdx = 0;
  const firstUnwatched = eps.find((e) => !(state.progress[e.file_id] || {}).watched);
  if (firstUnwatched) {
    const idx = seasons.findIndex((s) =>
      (s.episodes || []).some((e) => e.file_id === firstUnwatched.file_id));
    if (idx > 0) startIdx = idx;
  }
  sel.value = String(startIdx);
  sel.addEventListener("change", () => renderEpisodes(rec, seasons[+sel.value]));
  renderEpisodes(rec, seasons[startIdx]);
  renderMaterials(rec);
}

// Course-level workbooks / PDFs, served through the streaming proxy.
function renderMaterials(rec) {
  const box = $("materialsBox");
  if (!box) return;
  const mats = rec.materials || [];
  if (!mats.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<h3 class="materials-head">Materials</h3>` + mats.map((m) =>
    `<a class="material" target="_blank" href="/stream/${encodeURIComponent(m.file_id)}">📄 ${escapeHTML(m.name)}</a>`
  ).join("");
}

// Minimal queue item {file_id, name, duration_ms, media} for the autoplay queue.
function queueItem(ep) {
  return { file_id: ep.file_id, name: ep.name, duration_ms: ep.duration_ms || null,
           media: ep.media || null };
}

// Every playable episode across all REAL seasons, in order. Extras
// pseudo-seasons are excluded so shuffle/resume never wander into featurettes
// (their episodes still play + queue from the episode list itself).
function allEpisodes(rec) {
  const eps = [];
  for (const s of realSeasons(rec))
    for (const e of s.episodes || []) if (e.file_id) eps.push(e);
  return eps;
}

// Fisher–Yates shuffle (returns a new array).
function shuffle(arr) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

function renderEpisodes(rec, season) {
  const list = $("episodeList");
  list.innerHTML = "";
  if (!season) return;

  // A volume/season ripped as a single audiobook file gets a one-tap play.
  if (season.audiobook && season.audiobook.file_id) {
    const ab = document.createElement("button");
    ab.className = "btn audiobook-btn";
    ab.textContent = `♪ Play ${season.name || "this volume"} as one audiobook`;
    ab.addEventListener("click", () =>
      playFile({ id: season.audiobook.file_id, name: season.audiobook.name,
                 drive_id: rec.drive_id },
               null, false, [], "audio"));
    list.appendChild(ab);
  }

  const eps = season.episodes;
  eps.forEach((ep, idx) => {
    const row = document.createElement("div");
    row.className = "episode";
    const prog = state.progress[ep.file_id] || {};
    if (prog.watched) row.classList.add("watched");
    const num = ep.episode != null
      ? ((SECTION_META[state.section] || {}).behavior === "entertainment"
          ? `E${String(ep.episode).padStart(2, "0")}` : String(ep.episode))
      : "";
    const mark = prog.watched ? `<span class="ep-done">✓</span>`
      : (ep.media === "audio" ? `<span class="ep-audio">♪</span>` : "");
    const pct = !prog.watched && prog.percent > 2
      ? `<div class="progress"><span style="width:${Math.min(98, prog.percent)}%"></span></div>` : "";
    row.innerHTML = `
      <span class="ep-num">${num}</span>
      <span class="ep-title">${escapeHTML(ep.title || ep.name)}</span>
      <span class="ep-dur">${ep.duration_ms ? fmtTime(ep.duration_ms / 1000) : ""}</span>
      <span class="ep-play">${mark || "▶"}</span>${pct}`;
    row.addEventListener("click", () => {
      // Autoplay: queue the rest of this season after the clicked episode.
      const queue = eps.slice(idx + 1).filter((e) => e.file_id).map(queueItem);
      playFile(
        { id: ep.file_id, name: ep.name, drive_id: rec.drive_id, parent_id: ep.parent_id },
        ep.duration_ms || null, false, queue, ep.media || null);
    });
    list.appendChild(row);
  });
}

// ---------- settings view ----------

// ---- tabs: create / delete ----
// Set by the zero-tab onboarding CTA (renderLibrary) before it routes to
// Settings, so openSettings pops the create-tab form open as soon as the
// drive list has rendered — the CTA's button can't open the form directly
// because it lives inside the Settings view, which hasn't been built yet.
let openCreateFormOnSettingsLoad = false;
function maybeOpenCreateFormNow() {
  if (!openCreateFormOnSettingsLoad) return;
  openCreateFormOnSettingsLoad = false;
  openCreateTabForm();
}

// The <select class="drive-section"> that triggered "＋ New tab…", so the
// newly created tab can be pre-selected there once it exists; null when the
// form was opened from the zero-tab CTA or the "Your tabs" list instead.
let createTabTriggerSelect = null;

// Build/rebuild one drive's tab <select>: a disabled placeholder (no
// "entertainment" default to fall back on), every live tab, then "＋ New
// tab…". `currentValue` is kept if it's still a live tab, else the
// placeholder — this is also how a deleted tab's former assignees fall back
// to "unchosen" without any special-casing at delete time.
function populateDriveSectionSelect(sel, currentValue) {
  sel.innerHTML = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "— choose a tab —";
  placeholder.disabled = true;
  sel.appendChild(placeholder);
  for (const key of SECTION_ORDER) {
    const m = SECTION_META[key] || {};
    const opt = document.createElement("option");
    opt.value = key;
    opt.textContent = `${m.icon || ""} ${m.label || key}`.trim();
    sel.appendChild(opt);
  }
  const newOpt = document.createElement("option");
  newOpt.value = "__new__";
  newOpt.textContent = "＋ New tab…";
  sel.appendChild(newOpt);
  sel.value = SECTION_META[currentValue] ? currentValue : "";
  sel.dataset.prev = sel.value;
}

// The raw config["tabs"] shape (see TABS_REFACTOR.md) reconstructed from the
// currently-loaded SECTION_META/SECTION_ORDER — meta_list() already carries
// each tab's own "key", so round-tripping this never re-slugifies a label
// into a different key. Used as the base list for both create (append) and
// delete (filter out).
function currentTabsPayload() {
  return SECTION_ORDER.map((key) => {
    const m = SECTION_META[key] || {};
    const t = { key: m.key || key, label: m.label, icon: m.icon, behavior: m.behavior };
    if (m.accent) t.accent = m.accent;
    if (m.accent2) t.accent2 = m.accent2;
    return t;
  });
}

function populateBehaviorSelect() {
  const sel = $("newSectionBehavior");
  if (!sel) return;
  sel.innerHTML = "";
  for (const b of BEHAVIORS) {
    const opt = document.createElement("option");
    opt.value = b.key;
    opt.textContent = b.label;
    sel.appendChild(opt);
  }
}

function openCreateTabForm() {
  const form = $("newSectionForm");
  if (!form) return;
  $("newSectionName").value = "";
  $("newSectionIcon").value = "";
  $("newSectionMsg").textContent = "";
  populateBehaviorSelect();
  show(form, true);
  $("newSectionName").focus();
}

function closeCreateTabForm() {
  const form = $("newSectionForm");
  if (form) show(form, false);
  createTabTriggerSelect = null;
}

async function submitCreateTab() {
  const name = ($("newSectionName").value || "").trim();
  const icon = ($("newSectionIcon").value || "").trim();
  const behavior = $("newSectionBehavior").value;
  const msg = $("newSectionMsg");
  if (!name) { msg.textContent = "Name is required."; return; }
  if (!behavior) { msg.textContent = "Choose what this tab behaves like."; return; }
  msg.textContent = "Creating…";
  const entry = { label: name, behavior };
  if (icon) entry.icon = icon;   // empty -> server picks the default icon
  const priorKeys = new Set(SECTION_ORDER);
  const triggerSel = createTabTriggerSelect;
  try {
    await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tabs: currentTabsPayload().concat([entry]) }),
    });
    await loadSections();
    const newKey = SECTION_ORDER.find((k) => !priorKeys.has(k));
    document.querySelectorAll("select.drive-section").forEach((sel) => {
      populateDriveSectionSelect(sel, sel === triggerSel ? newKey : sel.value);
    });
    closeCreateTabForm();
    renderTabs();
    renderTabsManageList();
    toast(`Created “${(SECTION_META[newKey] || {}).label || name}”.`, "success");
  } catch (e) {
    msg.textContent = e.message || "Could not create tab.";
  }
}

function renderTabsManageList() {
  const box = $("tabsManageList");
  if (!box) return;
  box.innerHTML = "";
  if (!SECTION_ORDER.length) {
    box.innerHTML = `<p class="muted">No tabs yet — create one below.</p>`;
    return;
  }
  for (const key of SECTION_ORDER) {
    const m = SECTION_META[key] || {};
    const row = document.createElement("div");
    row.className = "tab-manage-row";
    row.innerHTML = `<span class="tab-manage-icon">${escapeHTML(m.icon || "")}</span>
      <span class="tab-manage-label">${escapeHTML(m.label || key)}</span>
      <button class="tab-manage-del" title="Delete this tab" aria-label="Delete this tab">✕</button>`;
    row.querySelector(".tab-manage-del").addEventListener("click", () => deleteTab(key));
    box.appendChild(row);
  }
}

async function deleteTab(key) {
  const m = SECTION_META[key] || {};
  const affected = Object.values(state.driveSections || {}).filter((v) => v === key).length;
  const warn = affected
    ? ` ${affected} drive${affected === 1 ? "" : "s"} assigned to it will need a new tab chosen in Settings.`
    : "";
  if (!confirm(`Delete “${m.label || key}”?${warn}`)) return;
  try {
    await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tabs: currentTabsPayload().filter((t) => t.key !== key) }),
    });
    await loadSections();
    renderTabs();
    renderTabsManageList();
    document.querySelectorAll("select.drive-section")
      .forEach((sel) => populateDriveSectionSelect(sel, sel.value));
  } catch (e) {
    toast("Could not delete tab: " + e.message, "error");
  }
}

async function openSettings() {
  showView("settings");
  $("settingsMsg").textContent = "";
  closeCreateTabForm();
  renderTabsManageList();
  const list = $("driveList");
  list.innerHTML = `<div class="spinner">Loading drives…</div>`;
  let drives = [], settings = {};
  try { settings = await api("/api/settings"); } catch (_) {}
  try { drives = (await api("/api/drives")).drives || []; }
  catch (e) {
    list.innerHTML = `<div class="empty">Could not list drives: ${escapeHTML(e.message)}</div>`;
    maybeOpenCreateFormNow();
    return;
  }
  const selected = new Set(settings.selected_drives || []);
  const assigned = settings.drive_sections || {};
  state.driveSections = assigned;
  $("autoRefresh").checked = !!settings.auto_refresh_on_startup;
  state.autoplayNext = settings.autoplay_next !== false;
  if ($("autoplayNext")) $("autoplayNext").checked = state.autoplayNext;
  if ($("subtitlesOn")) $("subtitlesOn").checked = settings.subtitles !== false;
  if ($("keepAwake")) $("keepAwake").checked = settings.keep_awake !== false;
  if ($("remoteAccess")) {
    $("remoteAccess").checked = !!settings.remote_access;
    show($("remoteRestartNote"), false);
    renderRemoteBlock();
  }
  const sel = $("playerSelect");
  if (sel) {
    sel.value = settings.player || "auto";
    const avail = settings.available_players || [];
    for (const opt of sel.options) {
      if (opt.value !== "auto" && avail.length && !avail.includes(opt.value)) {
        opt.textContent = opt.textContent.replace(/ \(not installed\)$/, "") + " (not installed)";
      }
    }
    const hint = $("playerHint");
    if (hint && avail.length) hint.textContent =
      "Installed: " + avail.join(", ") + ". mpv, IINA and VLC track your position for Continue Watching; Infuse is launch-only (no tracking or autoplay). mpv stays the recommended default.";
  }
  list.innerHTML = "";
  if (!drives.length) {
    list.innerHTML = `<div class="empty">No Shared Drives found.</div>`;
    maybeOpenCreateFormNow();
    return;
  }
  for (const d of drives) {
    const label = document.createElement("label");
    label.className = "drive-row";
    label.innerHTML = `<input type="checkbox" value="${escapeHTML(d.id)}" ${selected.has(d.id) ? "checked" : ""}>
      <span class="drive-name">${escapeHTML(d.name || d.id)}</span>`;
    if (selected.has(d.id)) {
      // Tab assignment for included drives — no default, the drive stays on
      // the placeholder until the user actively picks (or creates) a tab.
      const sel = document.createElement("select");
      sel.className = "drive-section";
      sel.dataset.driveId = d.id;
      populateDriveSectionSelect(sel, assigned[d.id]);
      sel.addEventListener("change", () => {
        if (sel.value === "__new__") {
          createTabTriggerSelect = sel;
          sel.value = sel.dataset.prev;   // revert now — cancel needs no cleanup
          openCreateTabForm();
        } else {
          sel.dataset.prev = sel.value;
        }
      });
      label.appendChild(sel);

      const btn = document.createElement("button");
      btn.className = "drive-refresh";
      btn.title = `Refresh only “${d.name || d.id}”`;
      btn.textContent = "⟳";
      btn.addEventListener("click", (e) => {
        e.preventDefault();          // inside a <label>: don't toggle the checkbox
        e.stopPropagation();
        refreshDrives([d.id], d.name || d.id);
      });
      label.appendChild(btn);
    }
    list.appendChild(label);
  }
  maybeOpenCreateFormNow();
}

// "Watch on iPhone / iPad" card: show tappable URLs + a QR of the best one.
// URLs come from /api/remote; the QR is rendered server-side at /api/remote/qr.
async function renderRemoteBlock() {
  const on = $("remoteAccess") && $("remoteAccess").checked;
  show($("remoteDetails"), !!on);
  if (!on) return;
  const info = await ensureRemoteInfo(true);
  const urls = (info && info.urls) || [];
  const list = $("remoteUrls");
  list.innerHTML = "";
  if (!urls.length) {
    list.innerHTML = `<p class="muted">Save and restart drivecast — your address appears here once remote access is live.</p>`;
  } else {
    urls.forEach((u, i) => {
      const a = document.createElement("a");
      a.className = "remote-url" + (i === 0 ? " active" : "");
      a.href = u.url;
      a.innerHTML = `<span class="remote-url-label">${escapeHTML(u.label)}</span>${escapeHTML(u.url)}`;
      // Tap a row to show ITS QR (e.g. Wi-Fi for a guest without Tailscale).
      a.addEventListener("click", (e) => {
        e.preventDefault();
        $("remoteQr").src = "/api/remote/qr?label=" + encodeURIComponent(u.label) + "&_=" + Date.now();
        for (const row of list.children) row.classList.toggle("active", row === a);
      });
      list.appendChild(a);
    });
  }
  const qr = $("remoteQr");
  if (urls.length) {
    qr.onerror = () => show(qr, false);
    qr.src = "/api/remote/qr?_=" + Date.now();
    show(qr, true);
  } else {
    show(qr, false);
  }
  // Trusted-LAN HTTPS: when a CA download is available, offer the one-time
  // "Trust this Mac" flow. Tapping it swaps the QR to the CA-download URL.
  const trust = $("trustBlock");
  if (trust) {
    show(trust, !!(info && info.ca_url));
    const fp = $("trustFingerprint");
    if (fp) fp.textContent = (info && info.ca_fingerprint) || "";
    const link = $("trustLink");
    if (link && info && info.ca_url) {
      link.onclick = (e) => {
        e.preventDefault();
        $("remoteQr").src = "/api/remote/qr?label=trust&_=" + Date.now();
        show($("remoteQr"), true);
        for (const row of list.children) row.classList.remove("active");
      };
    }
  }
  // The HTTPS listener's cert no longer matches this Mac's Wi-Fi address
  // (roamed networks, or Wi-Fi came up after launch) — prompt a restart.
  const stale = $("remoteHttpsStale");
  if (stale) show(stale, !!(info && info.https_stale));
}

// Kick a refresh scoped to specific drives (per-drive refresh).
async function refreshDrives(ids, displayName) {
  try {
    const res = await api("/api/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ drives: ids }),
    });
    if (res.running && !res.started) toast("A refresh is already running.");
    else toast(`Refreshing ${displayName}…`);
    startScanWatch();
  } catch (e) {
    toast("Refresh failed: " + e.message, "error");
  }
}

async function saveSettings() {
  const selected = [...$("driveList").querySelectorAll("input:checked")].map((c) => c.value);
  const auto = $("autoRefresh").checked;
  const autoplay = $("autoplayNext") ? $("autoplayNext").checked : true;
  // Collect tab assignments. There's no "entertainment" default anymore, so
  // every included drive needs an explicit, live tab — Save is blocked if any
  // still shows the placeholder (or, transiently, "＋ New tab…").
  // Skip drives the user just unchecked — their row still holds a select.
  const driveSections = {};
  for (const sel of $("driveList").querySelectorAll("select.drive-section")) {
    const cb = sel.closest("label").querySelector("input[type=checkbox]");
    if (cb && !cb.checked) continue;
    if (!sel.value || sel.value === "__new__") {
      $("settingsMsg").textContent = "Choose a tab for every included drive before saving.";
      return;
    }
    driveSections[sel.dataset.driveId] = sel.value;
  }
  $("settingsMsg").textContent = "Saving…";
  try {
    const res = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selected_drives: selected, drive_sections: driveSections,
        auto_refresh_on_startup: auto, autoplay_next: autoplay,
        subtitles: $("subtitlesOn") ? $("subtitlesOn").checked : true,
        keep_awake: $("keepAwake") ? $("keepAwake").checked : true,
        remote_access: $("remoteAccess") ? $("remoteAccess").checked : false,
        player: ($("playerSelect") || {}).value || "auto" }),
    });
    $("settingsMsg").textContent = "Saved.";
    state.selectedDrives = res.selected_drives || [];
    state.driveSections = res.drive_sections || {};
    state.autoplayNext = res.autoplay_next !== false;
    renderTabs();
    // A remote_access flip only takes effect on the next launch.
    if ($("remoteRestartNote")) show($("remoteRestartNote"), !!res.restart_required);
    if (res.restart_required) toast("Restart drivecast to apply remote access.");
    await renderRemoteBlock();
    if (res.refresh_started) { toast("Drives changed — refreshing library…"); startScanWatch(); }
  } catch (e) {
    $("settingsMsg").textContent = "Save failed: " + e.message;
  }
}

// ---------- play ----------
let pendingPlay = null;

async function playFile(f, durationMs, skipResumeCheck, queue, media) {
  markPlaybackStarted();   // this client is now watching -> arm keep-awake polling
  const fileId = f.id;
  const name = f.name;
  const durMs = durationMs || null;
  const driveId = f.drive_id || state.driveId;
  const parentId = f.parent_id || state.folderId;
  const q = queue || [];
  const med = media || null;

  let resumeAt = 0;
  if (!skipResumeCheck) {
    try {
      const cont = await api("/api/continue");
      const hit = (cont.items || []).find((x) => x.file_id === fileId);
      if (hit) resumeAt = hit.position || 0;
    } catch (_) {}
  }
  if (resumeAt > 5 && !skipResumeCheck) {
    pendingPlay = { fileId, name, durMs, driveId, parentId, queue: q, media: med, resumeAt };
    $("modalBody").textContent =
      `You were at ${fmtTime(resumeAt)}. Resume or start from the beginning?`;
    show($("modal"), true);
    return;
  }
  await launch(fileId, name, durMs, driveId, parentId, false, q, med, resumeAt);
}

async function launch(fileId, name, durMs, driveId, parentId, startOver, queue, media, resumeAt) {
  // Remote devices never launch mpv on the Mac: play inline when the format is
  // browser-friendly, otherwise offer the VLC/Infuse hand-off.
  if (state.remote) {
    let at = startOver ? 0 : (resumeAt || 0);
    // Continue Watching / course-resume skip the modal (skipResumeCheck) so `at`
    // is unset — locally the server resumes from history, so mirror that here.
    if (!startOver && !at) {
      try {
        const cont = await api("/api/continue");
        const hit = (cont.items || []).find((x) => x.file_id === fileId);
        if (hit) at = hit.position || 0;
      } catch (_) {}
    }
    if (canPlayInBrowser(name)) {
      openWebPlayer({ fileId, name, resumeAt: at, queue: queue || [], media: media || null });
    } else {
      openChooser({ fileId, name, resumeAt: at, queue: queue || [], media: media || null });
    }
    return;
  }
  try {
    const res = await api("/api/play", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        file_id: fileId, name, duration_ms: durMs,
        drive_id: driveId, parent_id: parentId, start_over: !!startOver,
        queue: queue || [], media: media || null,
      }),
    });
    const from = res.resumed_from > 1 ? ` (resumed at ${fmtTime(res.resumed_from)})` : "";
    const subs = res.subtitles ? " · EN subs" : "";
    toast(`Playing in ${res.player}${from}${subs}`, "success");
    if (res.player === "vlc") showVlcBanner();
    setTimeout(loadContinue, 1500);
  } catch (e) {
    if (e.status === 501) toast(e.message, "error");
    else toast("Play failed: " + e.message, "error");
  }
}

// Informational only — show briefly and get out of the way (the scan bar
// stays sticky because it reports crucial in-flight work; this doesn't).
function showVlcBanner() {
  const b = $("banner");
  b.innerHTML = "Playing in VLC — resume &amp; Continue Watching are tracked via VLC's " +
    "HTTP interface. If resume doesn't stick, update VLC or install mpv: <code>brew install mpv</code>";
  show(b, true);
  clearTimeout(showVlcBanner._timer);
  showVlcBanner._timer = setTimeout(() => show(b, false), 6000);
}

$("btnResume").addEventListener("click", async () => {
  show($("modal"), false);
  if (pendingPlay) await launch(pendingPlay.fileId, pendingPlay.name, pendingPlay.durMs, pendingPlay.driveId, pendingPlay.parentId, false, pendingPlay.queue, pendingPlay.media, pendingPlay.resumeAt);
});
$("btnStartOver").addEventListener("click", async () => {
  show($("modal"), false);
  if (pendingPlay) await launch(pendingPlay.fileId, pendingPlay.name, pendingPlay.durMs, pendingPlay.driveId, pendingPlay.parentId, true, pendingPlay.queue, pendingPlay.media, pendingPlay.resumeAt);
});
$("btnCancel").addEventListener("click", () => { show($("modal"), false); pendingPlay = null; });

// Fix-poster picker: close via the ✕, a backdrop click, or Esc.
if ($("posterModalClose")) $("posterModalClose").addEventListener("click", closePosterPicker);
if ($("posterModal")) $("posterModal").addEventListener("click", (e) => {
  if (e.target === $("posterModal")) closePosterPicker();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && posterPickerState) closePosterPicker();
});

// ==================== Web player (iPhone / iPad) ====================
// Fullscreen inline <video> for remote devices. Reports position back to
// /api/progress so Continue Watching / resume sync across devices, and carries
// the autoplay queue client-side (the Mac never sees these files play).
let wp = null; // active web-player session

// Absolute stream URL WITH the token — for external players (VLC/Infuse) that
// don't carry the browser's auth cookie.
function absStreamUrl(fileId) {
  return location.origin + "/stream/" + encodeURIComponent(fileId) +
    "?token=" + encodeURIComponent(state.remoteToken || "");
}

function playInWebPlayer(fileId, name, resumeAt) {
  const video = $("wpVideo");
  $("wpTitle").textContent = name || "";
  video.src = "/stream/" + encodeURIComponent(fileId); // same-origin: cookie auth
  video.currentTime = 0;
  video.load();
  video.play().catch(() => {}); // autoplay may need a tap on iOS — controls cover it
}

function openWebPlayer({ fileId, name, resumeAt, queue, media }) {
  markPlaybackStarted();   // web player counts as watching too
  stopProgressTimer();   // a rapid double-tap must not leak the old interval
  wp = { fileId, name, media: media || null, queue: queue || [],
         resumeAt: resumeAt || 0, duration: null, timer: null };
  show($("webPlayer"), true);
  playInWebPlayer(fileId, name, resumeAt || 0);
}

function stopProgressTimer() {
  if (wp && wp.timer) { clearInterval(wp.timer); wp.timer = null; }
}
function startProgressTimer() {
  stopProgressTimer();
  if (wp) wp.timer = setInterval(() => reportProgress(false), 10000);
}

function reportProgress(ended) {
  if (!wp) return;
  const video = $("wpVideo");
  const dur = (video.duration && isFinite(video.duration)) ? video.duration : (wp.duration || null);
  const body = { file_id: wp.fileId, name: wp.name, position: video.currentTime || 0 };
  if (dur) body.duration = dur;
  if (ended) body.ended = true;
  api("/api/progress", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).catch(() => {});
}

function closeWebPlayer() {
  stopProgressTimer();
  if (wp) reportProgress(false); // save final position
  const video = $("wpVideo");
  video.pause();
  video.removeAttribute("src");
  video.load();
  show($("webPlayer"), false);
  wp = null;
  setTimeout(loadContinue, 600);
}

function onWebPlayerEnded() {
  stopProgressTimer();
  reportProgress(true);
  const next = (wp.queue || []).shift();
  if (state.autoplayNext && next && next.file_id && canPlayInBrowser(next.name)) {
    wp.fileId = next.file_id;
    wp.name = next.name;
    wp.media = next.media || null;
    wp.resumeAt = 0;
    wp.duration = null;
    playInWebPlayer(next.file_id, next.name, 0);
  } else {
    closeWebPlayer();
  }
}

(function initWebPlayer() {
  const video = $("wpVideo");
  video.addEventListener("loadedmetadata", () => {
    if (video.duration && isFinite(video.duration)) wp && (wp.duration = video.duration);
    if (wp && wp.resumeAt > 0 && wp.resumeAt < (video.duration || Infinity)) {
      video.currentTime = wp.resumeAt;
    }
  });
  video.addEventListener("play", startProgressTimer);
  video.addEventListener("pause", () => { stopProgressTimer(); reportProgress(false); });
  video.addEventListener("ended", onWebPlayerEnded);
  $("wpClose").addEventListener("click", closeWebPlayer);
})();

// ---------- external-player chooser (MKV etc. on remote devices) ----------
let chooserFile = null;

function openChooser({ fileId, name, resumeAt, queue, media }) {
  chooserFile = { fileId, name, resumeAt: resumeAt || 0,
                  queue: queue || [], media: media || null };
  const enc = encodeURIComponent(absStreamUrl(fileId));
  $("chooserTitle").textContent = name || "";
  $("chooserVlc").href = "vlc-x-callback://x-callback-url/stream?url=" + enc;
  $("chooserInfuse").href = "infuse://x-callback-url/play?url=" + enc;
  show($("chooser"), true);
}

$("chooserBrowser").addEventListener("click", () => {
  show($("chooser"), false);
  if (chooserFile) openWebPlayer({ fileId: chooserFile.fileId, name: chooserFile.name,
    resumeAt: chooserFile.resumeAt, queue: chooserFile.queue, media: chooserFile.media });
});
$("chooserCancel").addEventListener("click", () => { show($("chooser"), false); chooserFile = null; });

// ==================== Browse (advanced) ====================
async function loadDrives() {
  try {
    const data = await api("/api/drives");
    state.drives = data.drives || [];
    state.driveName = {};
    const row = $("drivesRow");
    row.innerHTML = "";
    for (const d of state.drives) {
      state.driveName[d.id] = d.name;
      const c = document.createElement("div");
      c.className = "card folder";
      c.innerHTML = `<div class="poster"><span class="folder-icon">🎞️</span></div>
                     <div class="label">${escapeHTML(d.name)}</div>`;
      c.addEventListener("click", () => {
        state.crumbs = [{ name: d.name, driveId: d.id, folderId: null }];
        location.hash = `#/browse/drive/${d.id}`;
      });
      row.appendChild(c);
    }
  } catch (e) {
    toast("Could not load drives: " + e.message, "error");
  }
}

function browseVideoCard(f) {
  const card = document.createElement("div");
  card.className = "card video";
  const ph = `<div class="ph-title">${escapeHTML(f.name)}</div>`;
  let inner = "";
  let cls = " placeholder";
  if (f.thumbnailLink) {
    cls = "";
    const u = `/api/poster/_?thumb=${encodeURIComponent(f.thumbnailLink)}`;
    inner = `<img loading="lazy" src="${u}" alt="" data-ph='${escapeHTML(ph)}'
      onerror="this.parentElement.classList.add('placeholder');
               this.parentElement.insertAdjacentHTML('beforeend', this.dataset.ph||'');this.remove()">`;
  } else {
    inner = ph;
  }
  card.innerHTML = `
    <div class="poster${cls}">${inner}</div>
    <div class="label">${escapeHTML(f.name)}</div>`;
  card.addEventListener("click", () => playFile(f,
    (f.videoMediaMetadata && f.videoMediaMetadata.durationMillis) || null));
  return card;
}

function folderCard(f) {
  const card = document.createElement("div");
  card.className = "card folder";
  card.innerHTML = `
    <div class="poster"><span class="folder-icon">📁</span></div>
    <div class="label">${escapeHTML(f.name)}</div>`;
  card.addEventListener("click", () => {
    state.crumbs.push({ name: f.name, driveId: state.driveId, folderId: f.id });
    location.hash = `#/browse/drive/${state.driveId}/folder/${f.id}`;
  });
  return card;
}

function renderBrowseFiles(files, append) {
  const grid = $("grid");
  if (!append) grid.innerHTML = "";
  for (const f of files.filter(isFolder)) grid.appendChild(folderCard(f));
  for (const f of files.filter((x) => !isFolder(x))) grid.appendChild(browseVideoCard(f));
}

async function doBrowse(driveId, folderId, append) {
  state.browseView = "browse";
  state.driveId = driveId;
  state.folderId = folderId;
  show($("drivesSection"), false);
  show($("gridSection"), true);
  show($("empty"), false);
  renderBreadcrumb();
  const params = new URLSearchParams({ drive_id: driveId });
  if (folderId) params.set("folder_id", folderId);
  if (append && state.nextPageToken) params.set("page_token", state.nextPageToken);
  if (!append) $("grid").innerHTML = '<div class="spinner">Loading…</div>';
  try {
    const data = await api("/api/browse?" + params.toString());
    if (!append) $("grid").innerHTML = "";
    state.nextPageToken = data.nextPageToken || null;
    $("gridTitle").textContent = state.crumbs.length
      ? state.crumbs[state.crumbs.length - 1].name : (state.driveName[driveId] || "Browse");
    renderBrowseFiles(data.files || [], append);
    show($("loadMore"), !!state.nextPageToken);
    if (!append && !(data.files || []).length) {
      $("empty").textContent = "This folder is empty."; show($("empty"), true);
    }
  } catch (e) {
    $("grid").innerHTML = "";
    toast("Browse failed: " + e.message, "error");
  }
}

function renderBreadcrumb() {
  const bc = $("breadcrumb");
  if (!state.crumbs.length) { show(bc, false); return; }
  bc.innerHTML = "";
  const home = document.createElement("a");
  home.textContent = "Drives";
  home.addEventListener("click", () => { location.hash = "#/browse"; });
  bc.appendChild(home);
  state.crumbs.forEach((c, i) => {
    const sep = document.createElement("span");
    sep.className = "sep"; sep.textContent = "›";
    bc.appendChild(sep);
    const a = document.createElement("a");
    a.textContent = c.name;
    a.addEventListener("click", () => {
      state.crumbs = state.crumbs.slice(0, i + 1);
      location.hash = c.folderId
        ? `#/browse/drive/${c.driveId}/folder/${c.folderId}` : `#/browse/drive/${c.driveId}`;
    });
    bc.appendChild(a);
  });
  show(bc, true);
}

function openBrowseHome() {
  state.browseView = "drives";
  state.crumbs = [];
  state.nextPageToken = null;
  show($("drivesSection"), true);
  show($("gridSection"), false);
  show($("breadcrumb"), false);
  show($("empty"), false);
  loadDrives();
}

$("loadMore").addEventListener("click", () => {
  if (state.browseView === "browse") doBrowse(state.driveId, state.folderId, true);
});

// ---------- routing ----------
function currentRoute() {
  const h = location.hash || "#/";
  if (h.startsWith("#/title/")) return "detail";
  if (h.startsWith("#/settings")) return "settings";
  if (h.startsWith("#/browse")) return "browse";
  return "library";
}

function router() {
  const h = location.hash || "#/";
  const mTitle = h.match(/^#\/title\/(.+)$/);
  const mBrowseFolder = h.match(/^#\/browse\/drive\/([^/]+)\/folder\/([^/]+)$/);
  const mBrowseDrive = h.match(/^#\/browse\/drive\/([^/]+)$/);
  // Tab keys are slugs (lowercase alnum + dashes, e.g. "my-courses"), so the
  // route must allow dashes — \w alone would silently reject multi-word tabs.
  const mSection = h.match(/^#\/s\/([\w-]+)$/);

  if (mSection) {
    setSection(mSection[1]);
    showView("library");
    renderLibrary();
    loadContinue();
    return;
  }
  if (mTitle) {
    openDetail(decodeURIComponent(mTitle[1]));
  } else if (h.startsWith("#/settings")) {
    openSettings();
  } else if (mBrowseFolder) {
    showView("browse");
    doBrowse(decodeURIComponent(mBrowseFolder[1]), decodeURIComponent(mBrowseFolder[2]), false);
  } else if (mBrowseDrive) {
    showView("browse");
    const id = decodeURIComponent(mBrowseDrive[1]);
    if (!state.crumbs.length) state.crumbs = [{ name: state.driveName[id] || "Drive", driveId: id, folderId: null }];
    doBrowse(id, null, false);
  } else if (h.startsWith("#/browse")) {
    showView("browse");
    openBrowseHome();
  } else {
    showView("library");
    renderLibrary();
    loadContinue();
  }
}

// ---------- events ----------
$("homeBtn").addEventListener("click", () => { location.hash = "#/"; });
$("detailBack").addEventListener("click", () => history.back());
$("settingsBack").addEventListener("click", () => { location.hash = "#/"; });
$("browseBack").addEventListener("click", () => { location.hash = "#/"; });
$("refreshBtn").addEventListener("click", triggerRefresh);
$("saveSettings").addEventListener("click", saveSettings);
if ($("remoteAccess")) $("remoteAccess").addEventListener("change", renderRemoteBlock);
if ($("newSectionCreate")) $("newSectionCreate").addEventListener("click", submitCreateTab);
if ($("newSectionCancel")) $("newSectionCancel").addEventListener("click", closeCreateTabForm);

// ---------- theme (light / dark) ----------
// The saved theme is applied before first paint by an inline script in
// index.html's <head>; here we just keep the toggle button's icon in sync and
// persist changes. Default is dark (no data-theme attribute).
function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}
function applyTheme(theme) {
  const light = theme === "light";
  document.documentElement.setAttribute("data-theme", light ? "light" : "dark");
  try { localStorage.setItem("theme", light ? "light" : "dark"); } catch (_) {}
  const icon = $("themeToggle") && $("themeToggle").querySelector(".theme-icon");
  if (icon) icon.textContent = light ? "☀️" : "🌙";
  // Keep the mobile status-bar tint matching the page background.
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", light ? "#f4f5f8" : "#0f0f13");
}
if ($("themeToggle")) {
  applyTheme(currentTheme());   // sync icon + meta with the pre-paint choice
  $("themeToggle").addEventListener("click", () =>
    applyTheme(currentTheme() === "light" ? "dark" : "light"));
}

$("filters").addEventListener("click", (e) => {
  const btn = e.target.closest(".chip");
  if (!btn) return;
  state.filter = btn.dataset.filter;
  [...$("filters").children].forEach((c) => c.classList.toggle("active", c === btn));
  renderLibrary();
});

$("sortSel").addEventListener("change", async (e) => {
  state.sort = e.target.value;
  try { localStorage.setItem("dc.sort", state.sort); } catch (_) {}
  if (state.sort === "watched") await ensureWatchedMap();
  renderLibrary();
});

$("groupSel").addEventListener("change", async (e) => {
  state.group = e.target.value;
  try { localStorage.setItem("dc.group", state.group); } catch (_) {}
  if (state.group === "drive") await ensureDriveNames();
  renderLibrary();
});

$("search").addEventListener("input", (e) => {
  state.query = e.target.value.trim();
  if (currentRoute() === "library") renderLibrary();
  else location.hash = "#/";
});

window.addEventListener("hashchange", router);

// ---------- init ----------
function restoreControls() {
  try {
    const s = localStorage.getItem("dc.sort");
    const g = localStorage.getItem("dc.group");
    const sec = localStorage.getItem("dc.section");
    if (s) state.sort = s;
    if (g) state.group = g;
    if (sec && SECTION_META[sec]) setSection(sec);
    else setSection(state.section);
  } catch (_) { setSection(state.section); }
  const ss = $("sortSel"), gs = $("groupSel");
  if (ss) ss.value = state.sort;
  if (gs) gs.value = state.group;
}

async function loadPlaybackSettings() {
  try {
    const s = await api("/api/settings");
    state.autoplayNext = s.autoplay_next !== false;
    state.driveSections = s.drive_sections || {};
  } catch (_) { /* non-fatal — defaults to on */ }
}

// ---------- keep-awake: "Are you still watching?" ----------
// drivecast holds the Mac awake while streaming (see awake.py). When streams
// stop it enters a 120s grace, then a 30s prompt window before releasing. If
// this client started playback this session, poll the server's phase and pop a
// countdown modal during the prompt so the viewer can keep the Mac awake.
const awakeWatch = { started: false, showing: false, remaining: 0,
                     pollTimer: null, countdownTimer: null };

function markPlaybackStarted() {
  awakeWatch.started = true;
  ensureAwakePolling();
}

function ensureAwakePolling() {
  if (!awakeWatch.started || document.hidden) return;
  if (awakeWatch.pollTimer) return;   // already scheduled/running
  pollAwake();                        // poll immediately, then reschedule itself
}

function stopAwakePolling() {
  if (awakeWatch.pollTimer) { clearTimeout(awakeWatch.pollTimer); awakeWatch.pollTimer = null; }
}

async function pollAwake() {
  awakeWatch.pollTimer = null;
  if (!awakeWatch.started || document.hidden) return;
  let st = null;
  try { st = await api("/api/awake/status"); } catch (_) {}
  let delay = 15000;   // default cadence
  if (st) {
    if (st.phase === "prompt") {
      showAwakeModal(st);
      delay = 5000;                    // tighten while the prompt is up
    } else {
      hideAwakeModal();                // phase left prompt -> auto-dismiss
      // Tighten as the grace window runs low so we don't miss the prompt.
      if (st.phase === "grace" && st.seconds_left != null && st.seconds_left < 40) delay = 5000;
    }
  }
  awakeWatch.pollTimer = setTimeout(pollAwake, delay);
}

function showAwakeModal(st) {
  const el = $("awakeModal");
  if (!el) return;
  awakeWatch.remaining = Math.max(0, Math.round(st.seconds_left || 0));
  $("awakeCountdown").textContent = awakeWatch.remaining;
  if (awakeWatch.showing) return;
  awakeWatch.showing = true;
  show(el, true);
  // Local 1s countdown between polls; the 5s poll re-syncs the value.
  if (awakeWatch.countdownTimer) clearInterval(awakeWatch.countdownTimer);
  awakeWatch.countdownTimer = setInterval(() => {
    awakeWatch.remaining -= 1;
    if (awakeWatch.remaining <= 0) { hideAwakeModal(); return; }
    $("awakeCountdown").textContent = awakeWatch.remaining;
  }, 1000);
}

function hideAwakeModal() {
  if (!awakeWatch.showing) return;
  awakeWatch.showing = false;
  show($("awakeModal"), false);
  if (awakeWatch.countdownTimer) { clearInterval(awakeWatch.countdownTimer); awakeWatch.countdownTimer = null; }
}

if ($("awakeYes")) $("awakeYes").addEventListener("click", async () => {
  hideAwakeModal();
  try { await api("/api/awake/extend", { method: "POST" }); } catch (_) {}
  ensureAwakePolling();
});
if ($("awakeNo")) $("awakeNo").addEventListener("click", async () => {
  hideAwakeModal();
  try { await api("/api/awake/release", { method: "POST" }); } catch (_) {}
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) { stopAwakePolling(); hideAwakeModal(); }
  else ensureAwakePolling();
});

(async function init() {
  await loadSections();
  restoreControls();
  // On a remote device, load the token up front so the VLC/Infuse deep links
  // (built synchronously when the chooser opens) already have it.
  if (state.remote) await ensureRemoteInfo();
  await loadLibrary();
  await ensureWatchedMap();
  await loadPlaybackSettings();
  if (state.group === "drive") await ensureDriveNames();
  router();
})();
