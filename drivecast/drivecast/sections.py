"""Behaviors and tabs: the classifier catalog vs. the user's navigation bar.

This module used to conflate two things that turned out to need very
different lifetimes, so it now keeps them in two namespaces:

* **Behaviors** are *code* — a classifier plus the mime families it scans and
  the season/episode vocabulary it renders with. They are a fixed catalog:
  three built in ("entertainment", "courses", "podcasts") plus whatever
  private plugins the user has dropped in
  ``~/Library/Application Support/drivecast/sections/``. Behaviors never
  appear in the UI by themselves, are never stored in ``drive_sections``, and
  are never deleted — they are just "the kinds of libraries drivecast knows
  how to build".
* **Tabs** are *data* — the user's actual navigation bar, entirely described
  by the ``"tabs"`` config list (``config.py`` DEFAULTS). Each tab picks a
  behavior to power it (``{"key","label","icon","behavior","accent","accent2"}``)
  and tab **order = list order**. There are zero tabs by default: a fresh
  install shows nothing until the user creates a tab in Settings (or an
  upgrade migration seeds some — see ``config.migrate_config``). Every
  ``drive_sections`` value and every ``rec["section"]`` is a **tab key**, not
  a behavior key — a drive is assigned to a tab, and that tab happens to be
  powered by some behavior.

Why tab keys equal today's behavior keys for the three built-ins
(``"entertainment"``/``"courses"``/``"podcasts"``): this refactor ships on
top of libraries that already have those exact strings stamped into
``library.json`` records, ``history.json``-adjacent state, and browser
``localStorage``. Migration seeds tabs whose ``key`` is byte-identical to the
stamp that's already on disk, so nothing needs re-stamping. Don't "clean up"
those seeded keys — the identity is load-bearing.

A plugin module in the private plugin directory still defines a single
``SECTION`` dict, but it now registers a **behavior** (its classifier + mimes
+ vocabulary), while its ``label``/``icon``/``accent`` describe the tab a
migration or the user would create for it:

    SECTION = {
        "key": "mysection",          # unique BEHAVIOR id (must avoid the 3 builtins)
        "label": "My Section",       # suggested tab label
        "icon": "🎧",                # suggested tab emoji
        "accent": "#e08b3c",         # suggested UI accent colour (+ optional "accent2")
        "continue": "Continue Listening",
        "lib": "My Library",         # library heading
        "empty": "No drive yet — assign one in Settings.",
        "season": "Volume",          # season/episode vocabulary
        "episode": "Track",
        "mimes": ("video", "audio", "image"),   # file families to scan
        "classify": my_classifier,   # fn(drive_id, drive_name, nodes, loose)
    }

The classifier is pure and receives the walked node trees exactly like the
built-in classifiers (see courses.py / playlists.py for the record shapes).

A *category* further splits entertainment titles (movie / show / documentary
/ other) using the TMDB genre ids we already fetch for posters — unrelated to
the behaviors/tabs split above and unchanged by it.
"""
import importlib.util
import logging
import os
import re

from . import config

log = logging.getLogger("drivecast.sections")

BUILTIN_BEHAVIORS = ("entertainment", "courses", "podcasts")

# The behavior catalog for the three built-ins: label + mime families the
# scanner walks + vocabulary/meta served to the frontend. Entertainment stays
# video-only; courses carry workbook PDFs and cover images; podcasts are
# YouTube downloads (either medium).
#
# Deliberately NO "icon"/"accent" here: those are tab properties now (two
# tabs built on the same behavior — e.g. two different course drives — can
# look completely different), so a behavior only describes vocabulary and
# scanning, never appearance. The *historical* icon/accent pairs that
# courses/podcasts tabs have always shipped with are NOT duplicated here —
# `config.migrate_config` hardcodes those literal values itself when seeding
# an upgraded install's tabs, so there is exactly one place that remembers
# "courses used to be green".
BEHAVIORS = {
    "entertainment": {
        "label": "Entertainment",
        "mimes": ("video",),
        "meta": {
            "continue": "Continue Watching", "lib": "Your Library",
            "empty": "No entertainment titles yet — assign drives in Settings and refresh.",
        },
    },
    "courses": {
        "label": "Courses",
        "mimes": ("video", "pdf", "image"),
        "meta": {
            "continue": "Continue Learning", "lib": "Your Courses",
            "empty": "No course drive yet — assign one to Courses in Settings and it appears here.",
            "season": "Module", "episode": "Lesson",
        },
    },
    "podcasts": {
        "label": "Podcasts",
        "mimes": ("video", "audio"),
        "meta": {
            "continue": "Continue Watching", "lib": "Your Podcasts",
            "empty": "No podcast drive yet — assign one in Settings when you've added it.",
        },
    },
}

PLUGIN_DIR = os.path.join(config.USER_DIR, "sections")

TMDB_DOCUMENTARY_GENRE = 99

# A palette of accent pairs assigned to auto-generated tabs (create-tab flow
# omits/mangles a colour), kept visually disjoint from the two builtin accents
# above (green/purple) so a fresh tab never looks like it "belongs" to
# Courses or Podcasts by accident.
_AUTO_PALETTE = (
    ("#60a5fa", "#93c5fd"),  # blue
    ("#f472b6", "#f9a8d4"),  # pink
    ("#fb923c", "#fdba74"),  # orange
    ("#38bdf8", "#7dd3fc"),  # sky
    ("#facc15", "#fde047"),  # yellow
    ("#f87171", "#fca5a5"),  # red
    ("#2dd4bf", "#5eead4"),  # teal
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_ACCENT_RE = re.compile(r"^#[0-9a-f]{6}$")

_plugins = None  # lazy cache: key -> SECTION dict (behaviors loaded from disk)
_tabs = None     # lazy cache: validated list from config["tabs"]


def _load_plugins():
    """Load custom BEHAVIOR plugins from PLUGIN_DIR. Errors never crash the app."""
    found = {}
    if not os.path.isdir(PLUGIN_DIR):
        return found
    for fn in sorted(os.listdir(PLUGIN_DIR)):
        if not fn.endswith(".py") or fn.startswith(("_", "test")):
            continue
        path = os.path.join(PLUGIN_DIR, fn)
        try:
            spec = importlib.util.spec_from_file_location(
                "drivecast_section_%s" % fn[:-3], path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            meta = getattr(mod, "SECTION", None)
            key = (meta or {}).get("key")
            # A plugin's behavior key only has to avoid the 3 builtin
            # behavior keys — it no longer has to avoid every existing tab,
            # since tabs are just data pointing at a behavior.
            if (isinstance(meta, dict) and key and key not in BUILTIN_BEHAVIORS
                    and callable(meta.get("classify"))):
                found[key] = meta
                log.info("Loaded custom section behavior %r from %s", key, fn)
            else:
                log.warning("Ignoring %s: no valid SECTION dict", fn)
        except Exception:
            log.exception("Failed to load custom section %s", fn)
    return found


def plugins():
    global _plugins
    if _plugins is None:
        _plugins = _load_plugins()
    return _plugins


def behaviors():
    """Every behavior: {behavior_key: {"label","mimes":tuple,"meta":dict}}.

    Builtins first, then loaded plugins (a plugin's own dict is reshaped into
    the same {label,mimes,meta} shape so callers never branch on origin).
    """
    out = {k: v for k, v in BEHAVIORS.items()}
    for key, p in plugins().items():
        out[key] = {
            "label": p.get("label") or key,
            "mimes": tuple(p.get("mimes") or ("video",)),
            "meta": {k: v for k, v in p.items()
                     if k not in ("key", "label", "mimes", "classify") and not callable(v)},
        }
    return out


def behaviors_meta():
    """[{"key","label"}, ...] for the create-tab "behaves like" picker."""
    return [{"key": key, "label": b["label"]} for key, b in behaviors().items()]


def behavior_for(tab_key):
    """The behavior key powering a tab, or None if the tab doesn't exist or
    its behavior has since disappeared (e.g. a plugin was removed)."""
    for t in tabs():
        if t["key"] == tab_key:
            return t["behavior"] if t["behavior"] in behaviors() else None
    return None


def _slugify(label, fallback):
    """Turn a label into a lowercase-alnum-dash key; empty/all-unicode labels
    fall back to a stable placeholder so a tab always gets *some* key."""
    slug = _SLUG_RE.sub("-", (label or "").strip().lower()).strip("-")
    return slug or fallback


def _assigned_accents(entries):
    pairs = set()
    for e in entries:
        if e.get("accent"):
            pairs.add((e["accent"], e.get("accent2") or ""))
    return pairs


def validate_tabs(raw):
    """Validate+normalize a raw ``config["tabs"]`` list into the canonical
    tab-record shape. PURE — no config I/O, unit-testable in isolation.

    Rules (drop the entry entirely if any of these can't be repaired):
      * ``label``: 1-40 chars after stripping; missing/blank -> dropped.
      * ``key``: slugified from label if absent; must be unique among tabs
        already accepted in this call — a later duplicate is dropped (the
        first entry with a given key wins). This is the ONLY key-collision
        rule now: tabs no longer have to dodge behavior names.
      * ``icon``: defaults to "📁"; capped at ~8 chars (emoji + modifiers).
      * ``behavior``: must name a live entry in ``behaviors()`` — an entry
        whose behavior doesn't resolve is dropped (nothing to render).
      * ``accent``/``accent2``: must match ``^#[0-9a-f]{6}$``; if either is
        missing/invalid, BOTH are auto-assigned from ``_AUTO_PALETTE``,
        deterministically by position among already-valid accents so the
        same input list always yields the same assignment (stability across
        calls — no colour "jitter" on every settings save).
    """
    valid_behaviors = behaviors()
    out = []
    seen_keys = set()
    pending_auto = []  # indices (into `out`) still needing a palette colour

    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip()
        if not label or len(label) > 40:
            continue
        behavior = entry.get("behavior")
        if behavior not in valid_behaviors:
            continue
        key = str(entry.get("key") or "").strip().lower() or _slugify(label, "tab")
        key = _slugify(key, "tab")
        if key in seen_keys:
            continue  # duplicate key among tabs -> drop this later entry
        seen_keys.add(key)

        icon = str(entry.get("icon") or "📁").strip()[:8] or "📁"

        rec = {"key": key, "label": label, "icon": icon, "behavior": behavior}

        accent = entry.get("accent")
        accent2 = entry.get("accent2")
        if isinstance(accent, str) and _ACCENT_RE.match(accent) and \
                isinstance(accent2, str) and _ACCENT_RE.match(accent2):
            rec["accent"] = accent
            rec["accent2"] = accent2
        else:
            pending_auto.append(len(out))

        out.append(rec)

    # Auto-assign palette colours to whatever's left, in a fixed order
    # (position among tabs needing one) so re-validating the same raw list
    # always produces the same colours.
    taken = _assigned_accents(out)
    palette_idx = 0
    for i in pending_auto:
        # Skip palette entries already claimed by an explicit accent so two
        # auto-assigned tabs never collide with a hand-picked one either.
        while palette_idx < len(_AUTO_PALETTE) and _AUTO_PALETTE[palette_idx] in taken:
            palette_idx += 1
        accent, accent2 = _AUTO_PALETTE[palette_idx % len(_AUTO_PALETTE)]
        out[i]["accent"] = accent
        out[i]["accent2"] = accent2
        taken.add((accent, accent2))
        palette_idx += 1

    return out


def tabs():
    """Validated tab list (lazy cache, mirrors the `plugins()` pattern).
    Self-loads from ``config.load_config()["tabs"]`` on first use."""
    global _tabs
    if _tabs is None:
        _tabs = validate_tabs(config.load_config().get("tabs"))
    return _tabs


def set_tabs(raw):
    """Validate `raw` and replace the tabs cache. Called by the server on
    every settings write so in-process state matches what was just saved."""
    global _tabs
    _tabs = validate_tabs(raw)
    return _tabs


def all_sections():
    """Every live TAB key, in tab order. (Despite the name, these are tabs,
    not behaviors — kept for the many callers that just want "the drive
    assignment vocabulary".)"""
    return tuple(t["key"] for t in tabs())


def section_for_drive(drive_sections, drive_id):
    """The TAB a drive is assigned to, or None if unassigned/stale/unknown.

    There is no "entertainment" fallback anymore: with zero tabs by default,
    falling back to a fixed name would render a drive into a tab that may not
    even exist. An unassigned/invalid drive simply belongs to no tab.
    """
    sec = (drive_sections or {}).get(drive_id)
    return sec if sec in all_sections() else None


def mimes_for(tab_key):
    """File families the scanner walks for a tab (inherited from its
    behavior); ("video",) if the tab/behavior can't be resolved."""
    behavior = behavior_for(tab_key)
    if behavior is None:
        return ("video",)
    return behaviors()[behavior]["mimes"]


def classify_for(behavior_key):
    """A plugin behavior's classifier, or None for built-ins. Keyed by
    BEHAVIOR key (not tab key) — multiple tabs can share one behavior."""
    p = plugins().get(behavior_key)
    return p.get("classify") if p else None


def meta_list():
    """Per-tab metadata for the frontend (/api/sections): each tab's
    behavior meta (vocabulary — "continue"/"season"/"episode" — inherited
    from whatever behavior it's built on) overlaid with the tab's own
    label/icon/accent/accent2, plus "key"/"behavior" (the field clients
    branch on) and "lib"/"empty" derived from the tab's own label — since a
    user-created tab has no hardcoded copy of its own, fully resolved
    server-side so the client never has to cross-reference behaviors."""
    out = []
    all_behaviors = behaviors()
    for t in tabs():
        b = all_behaviors.get(t["behavior"], {})
        m = dict(b.get("meta") or {})
        m["empty"] = "No %s drive yet — assign one in Settings and it appears here." % t["label"]
        m.update({
            "key": t["key"],
            "behavior": t["behavior"],
            "label": t["label"],
            "icon": t["icon"],
            "lib": t["label"],
        })
        if "accent" in t:
            m["accent"] = t["accent"]
            m["accent2"] = t.get("accent2")
        out.append(m)
    return out


def category_for(meta, structural_type, hint_category=None):
    """Entertainment category from a TMDB enrich() result.

    meta is the cached TMDB dict (or None for a negative lookup):
      * None       -> the hint category if configured, else the structural type
                      ("show" for shows) so a structurally-detected show never
                      falls back to "other"; non-shows fall back to "other"
      * genre 99   -> "documentary" (movies and TV alike)
      * otherwise  -> the structural type ("show" for shows, "movie" else)
    Callers skip this entirely when TMDB is disabled (category stays None and
    the UI falls back to the structural type).
    """
    if meta is None:
        return hint_category or ("show" if structural_type == "show" else "other")
    if TMDB_DOCUMENTARY_GENRE in (meta.get("genre_ids") or []):
        return "documentary"
    return "show" if structural_type == "show" else "movie"
