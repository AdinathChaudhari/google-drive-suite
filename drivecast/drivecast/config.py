"""Configuration: merge config.json over defaults, auto-create on first run.

Config, data and secrets live in a STABLE per-user directory
(~/Library/Application Support/drivecast) rather than inside the repo/app
bundle. This means the packaged .app can read the user's TMDB key and selected
drives, and rebuilding/reinstalling the bundle never wipes them. Only the
first-run example config is read from the repo.
"""
import json
import os
import shutil
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Stable user directory that survives app rebuilds and is writable by the bundle.
USER_DIR = os.path.expanduser("~/Library/Application Support/drivecast")
CONFIG_PATH = os.path.join(USER_DIR, "config.json")
# First-run template still ships in the repo.
EXAMPLE_PATH = os.path.join(REPO_ROOT, "config.example.json")
DATA_DIR = os.path.join(USER_DIR, "data")
POSTERS_DIR = os.path.join(DATA_DIR, "posters")
SUBS_DIR = os.path.join(DATA_DIR, "subs")
SECRETS_PATH = os.path.join(USER_DIR, "secrets", "secrets.json")

# Secret settings: resolved from env vars / secrets/secrets.json, NEVER written
# back to config.json and NEVER committed (secrets/ is gitignored). This keeps
# API keys out of the (public) repo entirely.
SECRET_KEYS = {
    # config key : environment-variable override
    "tmdb_api_key": "DRIVECAST_TMDB_API_KEY",
    "opensubtitles_api_key": "DRIVECAST_OPENSUBTITLES_API_KEY",
}

DEFAULTS = {
    "remote": "gdrive1",
    "tmdb_api_key": "",
    "opensubtitles_api_key": "",
    "player": "auto",
    # Load English subtitles when available (sibling .srt on the drive, or
    # OpenSubtitles when an API key is configured in secrets).
    "subtitles": True,
    "port": 8737,
    "https_port": 8738,               # trusted-LAN HTTPS listener (remote access)
    "page_size": 200,
    # Library upgrade:
    "selected_drives": [],            # Shared Drive ids to include (empty = none yet)
    "auto_refresh_on_startup": False,  # rescan the library each launch
    "scan_throttle": 0.15,            # seconds to pause between scan API calls
    "autoplay_next": True,            # auto-play the next episode when one finishes
    # Hold a macOS power assertion (caffeinate) while any stream is active, so a
    # lid-closed/clamshell Mac doesn't sleep and kill remote playback.
    "keep_awake": True,
    # Remote access (opt-in): when enabled the server binds to the LAN/tailnet
    # instead of loopback and every non-local request must carry the secret
    # token. Both keys are non-secret and auto-saved (the token is generated on
    # first enable, so it lives in config.json — treat the link like a password).
    "remote_access": False,
    "remote_token": "",
    # Which TAB each drive belongs to: drive_id -> a key from "tabs" below
    # (see sections.py). Unassigned/stale values mean the drive shows in NO
    # tab (there is no more hardcoded "entertainment" fallback — with zero
    # tabs by default, that fallback tab might not even exist). Set from
    # Settings. NOTE: these are TAB keys, not behavior keys, even though the
    # three original tab keys ("entertainment"/"courses"/"podcasts") happen
    # to equal their behavior's key — that equality is what let existing
    # library.json / history / localStorage state migrate without
    # re-stamping (see sections.py module docstring). drive_sections and
    # rec["section"] are kept spelled this way purely for that continuity;
    # "tabs" below is the actual source of truth for what a key means.
    "drive_sections": {},
    # Per-drive classifier hints (hand-edited; no UI). Shapes:
    #   {"<drive_id>": {"category": "documentary"}}   TMDB-miss category fallback
    #   {"<drive_id>": {"single_course": true}}       whole drive is ONE course
    "drive_hints": {},
    # The user's navigation bar: an ORDERED list of tab records
    # {"key","label","icon","behavior","accent"?,"accent2"?} — see
    # sections.py's module docstring for the behaviors-vs-tabs split. Zero by
    # default (a fresh install shows nothing until the user creates a tab, or
    # until migrate_config() below seeds tabs for an upgraded install).
    "tabs": [],
}

# Keys we persist back to config.json. Secret keys are deliberately excluded so
# they never get written into the repo — they live only in secrets/ or env.
SAVED_KEYS = [k for k in DEFAULTS.keys() if k not in SECRET_KEYS]


def _load_secrets():
    """Read secrets/secrets.json if present. Returns {} on any error/absence."""
    try:
        with open(SECRETS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _resolve_secrets(merged):
    """Fill secret keys from (1) env var, (2) secrets/secrets.json, (3) whatever
    was already in config.json (legacy). Precedence: env > secrets file > config."""
    secrets = _load_secrets()
    for key, env_var in SECRET_KEYS.items():
        value = os.environ.get(env_var) or secrets.get(key) or merged.get(key, "")
        merged[key] = (value or "").strip() if isinstance(value, str) else value
    return merged


def _ensure_config_file():
    """Create config.json from config.example.json on first run."""
    if os.path.exists(CONFIG_PATH):
        return
    try:
        os.makedirs(USER_DIR, exist_ok=True)
        if os.path.exists(EXAMPLE_PATH):
            shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
        else:
            with open(CONFIG_PATH, "w") as f:
                json.dump(DEFAULTS, f, indent=2)
    except OSError:
        pass


def migrate_config(cfg, had_tabs_key):
    """One-time upgrade seam: give an existing (pre-tabs) install a starter
    "tabs" list built from its current drive_sections, so the upgrade is
    invisible — the drives the user already sorted into Courses/Podcasts/a
    plugin section keep showing up in a tab with that exact name.

    PURE (no config I/O) so it's trivially unit-testable; `load_config()` is
    the only caller, and it does the actual save when `changed` comes back
    True. Returns `(cfg, changed)`.

    Idempotency sentinel: `had_tabs_key` must be "did the raw on-disk
    config.json (BEFORE merging over DEFAULTS) contain a 'tabs' key" — NOT
    whether `cfg["tabs"]` is falsy/empty. Since DEFAULTS now supplies
    `"tabs": []`, the merged dict always has *a* "tabs" key; checking the
    merged dict would make this run forever (every already-migrated load
    looks "absent" if you check the wrong dict). `load_config()` reads the
    raw file dict once, before the merge, purely to compute this flag.

    Seeding only happens when migrating an existing install (tabs key
    absent AND selected_drives non-empty):
      1. Always seed an "entertainment" tab (no accent = today's UI default).
      2. For each distinct value already used in drive_sections: "courses"
         and "podcasts" seed their historical tab (with their historical
         accent pair, unchanged); a value matching a loaded plugin's
         behavior key seeds a tab from that plugin's own label/icon/accent;
         a value matching nothing we can resolve is dropped (see step 3).
      3. Every currently-selected drive ends up with an explicit
         drive_sections entry: drives that already had a valid one keep it,
         drives with an unresolvable value or no entry at all default to
         "entertainment".
    A fresh install (tabs key absent, selected_drives still empty) seeds
    nothing — tabs stays [] (true zero-start).

    Seeded tab keys are byte-identical to today's drive_sections stamps
    ("entertainment"/"courses"/"podcasts"/the plugin key) ON PURPOSE: it's
    what lets library.json records, history, and localStorage keep working
    with zero re-stamping. Do not "clean up" these keys.
    """
    if had_tabs_key:
        return cfg, False

    selected = cfg.get("selected_drives") or []
    if not selected:
        return cfg, False  # fresh install: true zero-start, tabs stays []

    # Deferred import: sections.py imports config at module scope, so
    # importing it back at config.py's module scope would be circular. By
    # the time migrate_config() actually runs (always via load_config(),
    # never at import time) config is already fully initialized, so a
    # function-local import here is safe.
    from . import sections

    drive_sections = dict(cfg.get("drive_sections") or {})
    plugin_behaviors = sections.plugins()

    seeded = {}  # tab key -> tab record; dict preserves first-seen order

    def _seed(key, label, icon, behavior, accent=None, accent2=None):
        if key in seeded:
            return
        rec = {"key": key, "label": label, "icon": icon, "behavior": behavior}
        if accent:
            rec["accent"], rec["accent2"] = accent, accent2
        seeded[key] = rec

    _seed("entertainment", "Entertainment", "\U0001F37F", "entertainment")

    distinct_values = {v for v in drive_sections.values() if v}
    if "courses" in distinct_values:
        _seed("courses", "Courses", "\U0001F393", "courses", "#4ade80", "#86efac")
    if "podcasts" in distinct_values:
        _seed("podcasts", "Podcasts", "\U0001F399", "podcasts", "#c084fc", "#e0b3ff")
    for value in sorted(distinct_values - {"entertainment", "courses", "podcasts"}):
        plugin = plugin_behaviors.get(value)
        if plugin:
            _seed(value, plugin.get("label", value), plugin.get("icon", "\U0001F4C1"),
                  value, plugin.get("accent"), plugin.get("accent2"))
        # else: value matches nothing we can render -> falls through to the
        # drive_sections cleanup below, which drops the assignment.

    valid_keys = set(seeded)
    for drive_id, value in list(drive_sections.items()):
        if value not in valid_keys:
            del drive_sections[drive_id]
    for drive_id in selected:
        drive_sections.setdefault(drive_id, "entertainment")

    cfg["tabs"] = list(seeded.values())
    cfg["drive_sections"] = drive_sections
    return cfg, True


def load_config():
    """Return merged config (config.json over DEFAULTS). Creates dirs as needed."""
    _ensure_config_file()
    merged = dict(DEFAULTS)
    had_tabs_key = False
    try:
        with open(CONFIG_PATH) as f:
            raw = json.load(f)
        # Compute the migration sentinel from the RAW file dict, before it's
        # merged over DEFAULTS (which itself now carries "tabs": []) — see
        # migrate_config()'s docstring for why this ordering matters.
        had_tabs_key = isinstance(raw, dict) and "tabs" in raw
        merged.update(raw)
    except (OSError, ValueError):
        pass
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(POSTERS_DIR, exist_ok=True)
    merged, changed = migrate_config(merged, had_tabs_key)
    if changed:
        save_config(merged)
    _resolve_secrets(merged)
    return merged


def save_config(cfg):
    """Persist the known config keys to config.json (atomic write)."""
    out = {}
    for k in SAVED_KEYS:
        if k in cfg:
            out[k] = cfg[k]
    try:
        os.makedirs(USER_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=USER_DIR, prefix=".config-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(out, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        pass
