# drivecast Tabs Refactor — Behaviors vs. Tabs

Companion to the original entertainment/courses/podcasts/plugin "sections"
design. This note documents the follow-on refactor that splits what that
design called a "section" into two independent things, and the
migration that gets an existing install there without re-stamping a single
record. All paths relative to the repo root; the affected module is
`drivecast/sections.py` + `drivecast/config.py`.

## 1. Why split at all

The original design made "section" do two jobs at once: it was both *the
kind of classifier* (courses vs. podcasts vs. entertainment) *and* *the tab
the user sees*. That's fine as long as the two are always 1:1 — but a user
who wants two separate course tabs (say, "Work Courses" and "Personal
Courses", each fed by different drives) or who wants to rename/re-icon/
re-color a tab has nowhere to do it: the tab *is* the classifier.

So the refactor introduces two cleanly separated namespaces:

- **Behaviors** = code. A behavior is "a way of turning a walked drive into
  library records": a classifier function, the mime families it scans, and
  the season/episode vocabulary it renders with. Behaviors are a fixed
  catalog — the three built-ins (`entertainment`, `courses`, `podcasts`) plus
  whatever private plugins are loaded from
  `~/Library/Application Support/drivecast/sections/*.py`. A behavior never
  appears in the UI by itself, is never stored in `drive_sections`, and is
  never deleted.
- **Tabs** = data. A tab is a row in the `config["tabs"]` list:
  `{"key","label","icon","behavior","accent"?,"accent2"?}`. Tab order *is*
  list order. Tabs are the **only** source of what shows up in the nav bar,
  and the **only** thing `drive_sections` values / `rec["section"]` ever
  point at. There are **zero tabs by default** — a brand new install shows
  nothing until the user builds a tab in Settings (pick a behavior, pick a
  label/icon/color) or an upgrade migration seeds some for them.

## 2. Zero-default, by design

Before this refactor, "no assignment" meant "assume Entertainment" — a
hardcoded fallback. That doesn't work once tabs are user-defined: there's no
guarantee an "entertainment" tab even exists anymore. So:

- `config.DEFAULTS["tabs"] = []`.
- `sections.section_for_drive()` returns the assigned tab key if it's a
  *live* tab, else **`None`** — no fallback. A drive that's unassigned (or
  assigned to a tab that's since been deleted) simply belongs to no tab.
- `sections.all_sections()` returns tab keys in tab order (empty tuple on a
  fresh install).

## 3. The migration seam (`config.migrate_config`)

An existing install already has drives stamped `"courses"` / `"podcasts"` /
a plugin key inside `drive_sections`, and — critically — `library.json`
records, `history.json` entries, and browser `localStorage` all reference
those same strings. The migration's one hard constraint: **seed tabs whose
key is byte-identical to what's already on disk**, so nothing needs
re-stamping. This is why the three built-in behavior keys and the three
historical tab keys are the same strings — that equality is the whole
migration trick, not a coincidence to "clean up" later.

**Idempotency sentinel:** presence of the `"tabs"` key in the *raw*
`config.json` file, read **before** it's merged over `DEFAULTS`. Since
`DEFAULTS` now supplies `"tabs": []`, the merged dict always looks like it
has a `"tabs"` key — checking the merged dict would make migration think
every install (even a truly fresh one) needs seeding, forever. `load_config()`
reads the file once, checks `"tabs" in raw`, and passes that single bit into
`migrate_config(merged, had_tabs_key)` — a pure function, easy to unit test
in isolation from any file I/O.

**Seeding rules** (only run when `had_tabs_key` is False AND
`selected_drives` is non-empty — i.e. an actual pre-tabs install, not a
fresh one):

1. Always seed `{"key":"entertainment","label":"Entertainment","icon":"🍿","behavior":"entertainment"}`
   (no accent — today's UI default).
2. For each distinct value already present in `drive_sections`:
   - `"courses"` → `{"key":"courses","label":"Courses","icon":"🎓","behavior":"courses","accent":"#4ade80","accent2":"#86efac"}`
   - `"podcasts"` → `{"key":"podcasts","label":"Podcasts","icon":"🎙","behavior":"podcasts","accent":"#c084fc","accent2":"#e0b3ff"}`
   - a value matching a loaded plugin's behavior key (e.g. `"myaudio"`) → a tab
     seeded from that plugin's own `label`/`icon`/`accent`/`accent2`.
   - a value matching nothing resolvable → dropped (falls through to rule 3).
3. Every currently-selected drive ends up with an explicit `drive_sections`
   entry: one it already had (if it resolved to a seeded tab) is kept;
   everything else — no entry, or an entry that didn't resolve — defaults to
   `"entertainment"`.

A truly fresh install (`had_tabs_key` False, `selected_drives` empty) seeds
nothing: `tabs` stays `[]`, matching `DEFAULTS`, and nothing is written to
disk until the user actually does something.

## 4. Frozen contracts (what other chunks build against)

`sections.py` exports (signatures frozen for the rest of this refactor):

```
behaviors()        -> {behavior_key: {"label","mimes":tuple,"meta":dict}}
behaviors_meta()   -> [{"key","label"}, ...]        # create-tab picker
behavior_for(tab_key) -> behavior_key | None
tabs()             -> validated list (lazy cache, mirrors plugins())
set_tabs(raw)      -> validate + replace the cache
validate_tabs(raw) -> list                          # PURE, unit-testable
all_sections()     -> tuple of TAB keys
mimes_for(tab_key) -> behaviors()[behavior_for(tab_key)]["mimes"] or ("video",)
classify_for(behavior_key) -> plugin classifier | None   # keyed by BEHAVIOR now
meta_list()        -> per-tab dict, fully resolved server-side
section_for_drive(drive_sections, drive_id) -> tab key | None
```

`/api/sections` response shape (frozen — both the web client and the
Android client depend on it):

```json
{"sections": [{"key","label","icon","behavior","accent"?,"accent2"?,
                "continue","lib","empty","season"?,"episode"?}, ...],
 "behaviors": [{"key","label"}, ...]}
```

`validate_tabs()` rules, for reference:

- `label`: 1–40 chars after stripping; missing/blank → the entry is dropped.
- `key`: slugified from the label if absent (unicode/empty labels fall back
  to a stable placeholder rather than crashing); uniqueness **among tabs
  only** is the sole key-collision rule now — a tab no longer has to dodge a
  behavior's name, since behaviors don't render.
- `icon`: defaults to `"📁"`, capped at ~8 chars.
- `behavior`: must resolve in `behaviors()`, else the entry is dropped
  (nothing to render it with).
- `accent`/`accent2`: must both match `^#[0-9a-f]{6}$`; if either is
  missing/invalid, **both** are auto-assigned together from a fixed palette
  disjoint from the builtin courses/podcasts accents, deterministically by
  position so the same input always yields the same colors (no jitter across
  saves) and two auto-assigned tabs never collide with each other or with an
  explicit accent already in the list.
- Invalid entries are **dropped** on a round-trip (lenient — old garbage in
  config.json shouldn't crash Settings), but the server-side create-tab API
  (a later chunk) returns 400 for an explicitly rejected create — the
  leniency is for *existing* config, not for a request the user just made.
