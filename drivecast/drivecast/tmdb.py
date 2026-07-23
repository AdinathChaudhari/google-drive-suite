"""Optional TMDB enrichment: posters + metadata for parsed titles.

If no API key is configured this module is inert (enrich() returns None for
everything). Lookups — including negative results — are cached to
data/tmdb_cache.json, and posters are downloaded once to data/posters/.
"""
import asyncio
import difflib
import json
import os
import re
import threading

import httpx

from . import config

TMDB_BASE = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p/w342"
CACHE_PATH = os.path.join(config.DATA_DIR, "tmdb_cache.json")
# Hand-picked poster/metadata overrides from the "Fix poster" picker. Keyed by
# normalized title + media_type; consulted before any TMDB search so a manual
# correction survives rescans regardless of the parsed year.
OVERRIDES_PATH = os.path.join(config.DATA_DIR, "poster_overrides.json")

# Minimum title-similarity (0-1) a search result must clear to be accepted.
# Below this we return None (a placeholder) rather than a confidently-wrong
# poster — TMDB ranks by popularity, so results[0] is often the wrong film.
_MATCH_THRESHOLD = 0.6
_PUNCT_RE = re.compile(r"[^\w\s]")


def _norm_title(s):
    """Lowercase, strip punctuation, collapse whitespace — for fuzzy compare."""
    return " ".join(_PUNCT_RE.sub(" ", (s or "").lower()).split())


def _result_titles(r):
    """All name variants TMDB gives a result (localised + original, movie + tv)."""
    return [t for t in (r.get("title"), r.get("original_title"),
                        r.get("name"), r.get("original_name")) if t]


def _result_year(r):
    return (r.get("release_date") or r.get("first_air_date") or "")[:4]


class TMDB:
    def __init__(self, api_key):
        self.api_key = (api_key or "").strip()
        self.enabled = bool(self.api_key)
        self._cache = self._load_cache()
        self._cache_lock = threading.Lock()
        self._overrides = self._load_overrides()
        self._overrides_lock = threading.Lock()
        self._sem = asyncio.Semaphore(4)
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0)) if self.enabled else None

    def _load_cache(self):
        try:
            with open(CACHE_PATH) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return self._heal_cache(data)
        except (OSError, ValueError):
            pass
        return {}

    @staticmethod
    def _heal_cache(data):
        """Drop positive entries cached before genre_ids existed so they get
        refetched (once) with genres. None negative markers stay valid."""
        return {k: v for k, v in data.items()
                if v is None or (isinstance(v, dict) and "genre_ids" in v)}

    def _save_cache(self):
        with self._cache_lock:
            try:
                os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
                tmp = CACHE_PATH + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(self._cache, f, indent=2)
                os.replace(tmp, CACHE_PATH)
            except OSError:
                pass

    # ---- poster overrides (manual "Fix poster" picks) ----

    def _load_overrides(self):
        """Read poster_overrides.json if present. Inert ({}) on absence/error,
        so a missing file or disabled TMDB never breaks enrichment."""
        try:
            with open(OVERRIDES_PATH) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _save_overrides(self):
        with self._overrides_lock:
            try:
                os.makedirs(os.path.dirname(OVERRIDES_PATH), exist_ok=True)
                tmp = OVERRIDES_PATH + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(self._overrides, f, indent=2)
                os.replace(tmp, OVERRIDES_PATH)
            except OSError:
                pass

    @staticmethod
    def _override_key(title, media_type):
        return "%s|%s" % (media_type, _norm_title(title))

    def _get_override(self, title, media_type):
        """The stored override for a title (a copy, so callers can't mutate the
        store), or None."""
        with self._overrides_lock:
            ov = self._overrides.get(self._override_key(title, media_type))
        return json.loads(json.dumps(ov)) if isinstance(ov, dict) else None

    def set_override(self, title, media_type, meta):
        """Persist a hand-picked enrich-shaped match for a title. enrich()
        consults this first, so the correction applies across future rescans."""
        with self._overrides_lock:
            self._overrides[self._override_key(title, media_type)] = meta
        self._save_overrides()

    async def aclose(self):
        if self._client:
            await self._client.aclose()

    def _cache_key(self, title, year, media_type):
        return "%s|%s|%s" % (media_type, (title or "").lower(), year or "")

    async def enrich(self, title, year=None, media_type="movie"):
        """Return {"tmdb_id","title","year","poster_key","overview"} or None.

        Negative results are cached as an explicit None marker.
        """
        if not self.enabled:
            return None
        # A manual "Fix poster" pick wins over any TMDB search/cache — return it
        # (re-materialising its poster) before doing any lookup.
        override = self._get_override(title, media_type)
        if override is not None:
            await self._ensure_poster(override)
            return override
        key = self._cache_key(title, year, media_type)
        with self._cache_lock:
            hit = key in self._cache
            cached = self._cache.get(key)
        if hit:
            await self._ensure_poster(cached)
            return cached

        async with self._sem:
            # Double-check cache inside the semaphore.
            with self._cache_lock:
                hit = key in self._cache
                cached = self._cache.get(key)
            if hit:
                await self._ensure_poster(cached)
                return cached
            result = await self._lookup(title, year, media_type)

        with self._cache_lock:
            self._cache[key] = result
        self._save_cache()
        return result

    async def _search(self, endpoint, title, year, media_type):
        """Raw TMDB search. Returns the results list (possibly empty), or None
        on a transport/HTTP error so the caller can distinguish 'no match' from
        'could not ask'."""
        params = {"api_key": self.api_key, "query": title, "include_adult": "false"}
        if year:
            # primary_release_year is stricter than `year` (which also matches
            # re-release/DVD dates), so it disambiguates same-named films better.
            params["primary_release_year" if media_type == "movie"
                   else "first_air_date_year"] = str(year)
        try:
            resp = await self._client.get(TMDB_BASE + endpoint, params=params)
            if resp.status_code != 200:
                return None
            return resp.json().get("results") or []
        except (httpx.HTTPError, ValueError):
            return None

    @staticmethod
    def _choose(results, want_title, want_year):
        """Pick the result best matching want_title (year breaks ties), instead
        of trusting TMDB's popularity-ranked results[0]. Returns None if nothing
        clears the similarity bar — a weak title match can't be rescued by the
        year bonus, so a wrong film with a coincidentally right year won't win."""
        want = _norm_title(want_title)
        best, best_score, best_ratio = None, -1.0, 0.0
        for r in results or []:
            ratio = max((difflib.SequenceMatcher(None, want, _norm_title(t)).ratio()
                         for t in _result_titles(r)), default=0.0)
            score = ratio + (0.3 if want_year and _result_year(r) == str(want_year) else 0.0)
            if score > best_score:
                best, best_score, best_ratio = r, score, ratio
        return best if best is not None and best_ratio >= _MATCH_THRESHOLD else None

    @staticmethod
    def _strong_matches(results, want_title):
        """Results whose best title variant clears _MATCH_THRESHOLD — the
        plausible matches, before year is considered."""
        want = _norm_title(want_title)
        strong = []
        for r in results or []:
            ratio = max((difflib.SequenceMatcher(None, want, _norm_title(t)).ratio()
                         for t in _result_titles(r)), default=0.0)
            if ratio >= _MATCH_THRESHOLD:
                strong.append(r)
        return strong

    @classmethod
    def _is_ambiguous(cls, results, want_title, want_year):
        """True when we should NOT guess: 2+ plausible title matches whose
        release years DIFFER and the wanted year doesn't single one out.

        TMDB ranks by popularity, so silently taking the top result here often
        picks the wrong same-named film — better to return None (a placeholder)
        and let the "Fix poster" picker prompt the user. Only meaningful on an
        UNCONSTRAINED search (year is None, or a year that matched nothing);
        a year-constrained search already returns a single year, so no conflict.
        """
        strong = cls._strong_matches(results, want_title)
        if len(strong) < 2:
            return False
        years = {_result_year(r) for r in strong if _result_year(r)}
        if len(years) < 2:
            return False  # they agree on year (or years unknown) — no conflict
        if want_year and sum(1 for r in strong
                             if _result_year(r) == str(want_year)) == 1:
            return False  # the wanted year uniquely selects one
        return True

    async def _lookup(self, title, year, media_type):
        endpoint = "/search/tv" if media_type == "tv" else "/search/movie"
        # Query variants: the full title, then progressively drop trailing words.
        # The parser already strips brackets + quality/edition tokens, but junk
        # OUTSIDE brackets survives — a distributor ("Golden Hour Criterion"),
        # a release group, a colloquial ordinal ("Racers 1"). Trimming trailing
        # words sheds all of it with NO hardcoded name list. It is safe because
        # every candidate is scored (by _choose / _is_ambiguous) against the
        # ORIGINAL `title`, not the trimmed query: a broader query can only ADD
        # the right film to the pool — it can never drift the accepted match to
        # something that doesn't resemble the real title (threshold + year guard).
        words = title.split()
        variants = [" ".join(words[:i]) for i in range(len(words), 0, -1)]
        chosen = None
        for q in variants:
            # a) constrained by year when known.
            results = await self._search(endpoint, q, year, media_type)
            # Unconstrained search (no year): refuse to guess between 2+ same-
            # titled films of different years — return None so the picker can
            # prompt. Scored against the original title, so trimming a specific
            # title into a generic one is caught here too.
            if not year and self._is_ambiguous(results, title, year):
                return None
            chosen = self._choose(results, title, year)
            # b) a wrong/edition year in the filename suppresses the real match —
            #    retry unconstrained (year still breaks ties inside _choose).
            if chosen is None and year:
                results = await self._search(endpoint, q, None, media_type)
                if self._is_ambiguous(results, title, year):
                    return None
                chosen = self._choose(results, title, year)
            if chosen is not None:
                break
        if chosen is None:
            return None
        top = chosen
        poster_path = top.get("poster_path")
        poster_key = None
        if poster_path:
            poster_key = poster_path.lstrip("/")
            await self._download_poster(poster_path, poster_key)
        return {
            "tmdb_id": top.get("id"),
            "title": top.get("title") or top.get("name") or title,
            "year": (top.get("release_date") or top.get("first_air_date") or "")[:4] or year,
            "poster_key": poster_key,
            "overview": top.get("overview") or None,
            "genre_ids": top.get("genre_ids") or [],
        }

    async def search_candidates(self, title, media_type="movie", limit=6):
        """Top plausible TMDB matches for a title — for the "Fix poster" picker.

        Each candidate's poster is downloaded so the UI can show a thumbnail via
        the existing /api/poster/{poster_key} route. Returns [] when disabled.
        """
        if not self.enabled:
            return []
        endpoint = "/search/tv" if media_type == "tv" else "/search/movie"
        async with self._sem:
            results = await self._search(endpoint, title, None, media_type)
        # Prefer results that clear the similarity bar, but fall back to the raw
        # (popularity-ordered) list so the manual picker always offers choices.
        picks = self._strong_matches(results, title) or (results or [])
        out = []
        for r in picks[:limit]:
            poster_path = r.get("poster_path")
            poster_key = None
            if poster_path:
                poster_key = poster_path.lstrip("/")
                await self._download_poster(poster_path, poster_key)
            out.append({
                "tmdb_id": r.get("id"),
                "title": r.get("title") or r.get("name") or title,
                "year": (r.get("release_date") or r.get("first_air_date") or "")[:4],
                "poster_key": poster_key,
                "overview": r.get("overview") or None,
            })
        return out

    async def by_id(self, tmdb_id, media_type="movie"):
        """Fetch a specific TMDB id's details as an enrich-shaped dict (poster
        downloaded), or None if the id is unknown/unreachable. Used by the
        poster-override endpoint once the user picks a candidate."""
        if not self.enabled:
            return None
        endpoint = ("/tv/%s" if media_type == "tv" else "/movie/%s") % tmdb_id
        try:
            resp = await self._client.get(TMDB_BASE + endpoint,
                                          params={"api_key": self.api_key})
            if resp.status_code != 200:
                return None
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(data, dict) or not data.get("id"):
            return None
        poster_path = data.get("poster_path")
        poster_key = None
        if poster_path:
            poster_key = poster_path.lstrip("/")
            await self._download_poster(poster_path, poster_key)
        # The details endpoint returns full genre objects; reduce to the ids the
        # rest of the app (and the cache) expect under genre_ids.
        genre_ids = [g.get("id") for g in (data.get("genres") or []) if g.get("id")]
        return {
            "tmdb_id": data.get("id"),
            "title": data.get("title") or data.get("name"),
            "year": (data.get("release_date") or data.get("first_air_date") or "")[:4] or None,
            "poster_key": poster_key,
            "overview": data.get("overview") or None,
            "genre_ids": genre_ids,
        }

    async def _ensure_poster(self, result):
        """Guarantee a cached hit's poster image is actually on disk.

        enrich() returns cached lookups without re-querying TMDB, so it must
        also re-materialise the poster: a file can go missing after the lookup
        was cached (the title was reclassified and its old poster pruned, or the
        original download failed after the positive result was cached). Without
        this, such a title shows a permanent placeholder because nothing ever
        re-downloads its poster. _download_poster is a no-op when the file
        already exists, so this costs one stat on the common path.
        """
        pk = result.get("poster_key") if isinstance(result, dict) else None
        if pk:
            await self._download_poster("/" + pk, pk)

    async def _download_poster(self, poster_path, poster_key):
        dest = os.path.join(config.POSTERS_DIR, poster_key)
        if os.path.exists(dest):
            return
        try:
            resp = await self._client.get(IMG_BASE + poster_path)
            if resp.status_code == 200:
                os.makedirs(config.POSTERS_DIR, exist_ok=True)
                tmp = dest + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(resp.content)
                os.replace(tmp, dest)
        except (httpx.HTTPError, OSError):
            pass

    def poster_path(self, poster_key):
        """Local filesystem path for a cached poster, or None if absent."""
        if not poster_key:
            return None
        p = os.path.join(config.POSTERS_DIR, poster_key)
        return p if os.path.exists(p) else None
