#!/usr/bin/env python3
"""Headless tests for yt-show's PURE episode-assignment function
(assign_new_episodes). No yt_dlp, no filesystem, no network — yt-show imports
yt_dlp lazily (inside the functions that need it), so loading the module here
does not require yt_dlp to be installed.

    .venv/bin/python3 test_yt_show.py     (or: python3 test_yt_show.py)

show_rec is the per-show record persisted in yt_shows.json:
    {"episodes": {video_id: {"season": S, "episode": E}, ...},
     "playlists": {plid: {"season": S}, ...}}
assign_new_episodes(show_rec, plid, new_items, mode) takes the ordered new
video ids for one playlist pass and returns the (video_id, season, episode)
assignments for them, in order, WITHOUT mutating show_rec. `mode` ('same' or
'new') only matters the first time a playlist is seen; a plid already present
in show_rec['playlists'] keeps its recorded season regardless of mode.
"""
import copy
import importlib.machinery
import importlib.util
import os
import tempfile
import unittest
from unittest import mock

_P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yt-show")
_spec = importlib.util.spec_from_loader(
    "yt_show", importlib.machinery.SourceFileLoader("yt_show", _P))
yt_show = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(yt_show)


def _episodes_at(show_rec, plid, season, count, start=1):
    """Seed show_rec['episodes']/['playlists'] as if `count` prior videos
    (v<plid>-1 .. v<plid>-count) had already been committed to `season`,
    starting at episode `start`."""
    show_rec.setdefault("playlists", {})[plid] = {"season": season}
    eps = show_rec.setdefault("episodes", {})
    for i in range(count):
        eps["v%s-%d" % (plid, i + 1)] = {"season": season,
                                          "episode": start + i}


class TestAssignNewEpisodes(unittest.TestCase):
    def test_brand_new_show_starts_season_1(self):
        show_rec = {"episodes": {}, "playlists": {}}
        result = yt_show.assign_new_episodes(
            show_rec, "PL1", ["v1", "v2", "v3"], "same")
        self.assertEqual(result, [("v1", 1, 1), ("v2", 1, 2), ("v3", 1, 3)])

    def test_incremental_continues_from_next_episode(self):
        show_rec = {"episodes": {}, "playlists": {}}
        _episodes_at(show_rec, "PL1", season=1, count=3)   # v PL1-1..3 = ep 1-3
        result = yt_show.assign_new_episodes(
            show_rec, "PL1", ["v4", "v5"], "same")
        self.assertEqual(result, [("v4", 1, 4), ("v5", 1, 5)])

    def test_same_season_mode_new_playlist_continues_max_season(self):
        show_rec = {"episodes": {}, "playlists": {}}
        _episodes_at(show_rec, "PL1", season=1, count=5)   # max season is 1
        result = yt_show.assign_new_episodes(
            show_rec, "PL2", ["w1", "w2"], "same")
        self.assertEqual(result, [("w1", 1, 6), ("w2", 1, 7)])

    def test_new_season_mode_new_playlist_bumps_season(self):
        show_rec = {"episodes": {}, "playlists": {}}
        _episodes_at(show_rec, "PL1", season=1, count=5)   # max season is 1
        result = yt_show.assign_new_episodes(
            show_rec, "PL2", ["w1"], "new")
        self.assertEqual(result, [("w1", 2, 1)])

    def test_roll_at_100_starts_next_season(self):
        show_rec = {"episodes": {}, "playlists": {}}
        items = ["v%d" % i for i in range(1, 102)]   # 101 items
        result = yt_show.assign_new_episodes(show_rec, "PL1", items, "same")
        self.assertEqual(len(result), 101)
        self.assertEqual(result[0], ("v1", 1, 1))
        self.assertEqual(result[99], ("v100", 1, 100))
        self.assertEqual(result[100], ("v101", 2, 1))

    def test_known_playlist_keeps_recorded_season_regardless_of_mode(self):
        show_rec = {"episodes": {}, "playlists": {}}
        _episodes_at(show_rec, "PL2", season=2, count=2)   # PL2 already at S2
        # mode='new' would normally bump the season for an UNSEEN playlist,
        # but PL2 is already known -> stays at its recorded season 2.
        result = yt_show.assign_new_episodes(show_rec, "PL2", ["x1"], "new")
        self.assertEqual(result, [("x1", 2, 3)])

    def test_rollover_skips_season_occupied_by_another_playlist(self):
        # PL1 fills season 1 (E1-E100); PL2 already holds S2E1-E3. Adding new
        # PL1 videos must roll PAST the committed episodes in season 2, never
        # reusing a (season, episode) another playlist already owns.
        show_rec = {"episodes": {}, "playlists": {}}
        _episodes_at(show_rec, "PL1", season=1, count=100)   # S01E01-E100
        _episodes_at(show_rec, "PL2", season=2, count=3)     # S02E01-E03
        result = yt_show.assign_new_episodes(
            show_rec, "PL1", ["a_new1", "a_new2"], "same")
        self.assertEqual(result, [("a_new1", 2, 4), ("a_new2", 2, 5)])
        committed = {(e["season"], e["episode"])
                     for e in show_rec["episodes"].values()}
        for (_vid, s, e) in result:
            self.assertNotIn((s, e), committed)

    def test_does_not_mutate_show_rec(self):
        show_rec = {"episodes": {}, "playlists": {}}
        _episodes_at(show_rec, "PL1", season=1, count=3)
        before = copy.deepcopy(show_rec["episodes"])
        yt_show.assign_new_episodes(show_rec, "PL1", ["v4", "v5"], "same")
        self.assertEqual(show_rec["episodes"], before)


class TestDriveUsage(unittest.TestCase):
    def test_parse_counts_trash_and_computes_free(self):
        data = {"cap_bytes": 100 * 1000 ** 3, "drives": [
            {"name": "TV", "bytes": 40 * 1000 ** 3, "trash_bytes": 10 * 1000 ** 3},
            {"name": "Courses", "bytes": 0, "trash_bytes": 0},
        ]}
        u = yt_show.parse_drive_usage(data)
        self.assertEqual(u["TV"]["used"], 50 * 1000 ** 3)   # content + trash
        self.assertEqual(u["TV"]["free"], 50 * 1000 ** 3)   # cap - used
        self.assertEqual(u["TV"]["pct"], 50.0)
        self.assertEqual(u["Courses"]["free"], 100 * 1000 ** 3)

    def test_parse_no_cap_yields_none_free(self):
        u = yt_show.parse_drive_usage({"drives": [{"name": "X", "bytes": 1}]})
        self.assertIsNone(u["X"]["free"])

    def test_parse_empty_payload(self):
        self.assertEqual(yt_show.parse_drive_usage({}), {})


class TestShowLock(unittest.TestCase):
    """The per-show lock is exclusive within/across processes and different
    shows never block each other; releasing frees it."""

    def _lock_path(self, slug):
        return os.path.join(yt_show.renamer._default_support_dir(),
                            ".yt-show-%s.lock" % slug)

    def test_lock_is_exclusive_and_releases(self):
        slug = "zztestlock%d" % os.getpid()
        other = "zztestlock%db" % os.getpid()
        fd1 = yt_show.acquire_show_lock(slug)
        self.addCleanup(lambda: os.path.exists(self._lock_path(slug))
                        and os.remove(self._lock_path(slug)))
        self.addCleanup(lambda: os.path.exists(self._lock_path(other))
                        and os.remove(self._lock_path(other)))
        try:
            self.assertIsNotNone(fd1)
            # A second acquire of the SAME show is refused while fd1 is held.
            self.assertIsNone(yt_show.acquire_show_lock(slug))
            # A DIFFERENT show is not blocked.
            fdo = yt_show.acquire_show_lock(other)
            self.assertIsNotNone(fdo)
            fdo.close()
        finally:
            fd1.close()
        # Once released, the same show can be locked again.
        fd2 = yt_show.acquire_show_lock(slug)
        self.assertIsNotNone(fd2)
        fd2.close()


class TestUI(unittest.TestCase):
    """The plain-text UI must be constructible and drivable without rich, a TTY,
    or a real download/upload (all its methods just write terse stderr lines)."""

    def test_bar_is_fixed_width(self):
        self.assertEqual(len(yt_show._bar(50, 10)), 10)
        self.assertEqual(len(yt_show._bar(0, 10)), 10)
        self.assertEqual(len(yt_show._bar(100, 10)), 10)
        self.assertEqual(len(yt_show._bar(None, 10)), 10)

    def test_plain_ui_drives_without_raising(self):
        ui = yt_show.UI(use_rich=False)
        ui.set_header("My Show", "TV")
        ui.set_capacity(50 * 1000 ** 3, 100 * 1000 ** 3, 50.0)
        ui.set_capacity(None, None, None)   # capacity unknown
        plan = [{"vid": "a", "season": 1, "episode": 1, "title": "One"},
                {"vid": "b", "season": 1, "episode": 2, "title": "Two"}]
        ui.register_episodes(plan)
        ui.set_counts(2, 0, 2)
        ui.update_status((1, 1), "downloading")
        ui.dl_start("S01E01 One", 1000)
        ui.dl_advance(500, 1000, 8_000_000)   # numeric speed (bytes/s)
        ui.dl_finish()
        ui.update_status((1, 1), "uploading")
        ui.up_start("S01E01 One", 1000)
        ui.up_advance(200, 1000, "5.0 MiB/s")  # string speed (from rclone)
        ui.up_finish()
        ui.update_status((1, 1), "done")
        ui.update_status((1, 2), "failed")
        ui.note("noted once")
        with ui.live():                       # no-op Live in plain mode
            pass


class TestStagedCompletePath(unittest.TestCase):
    """Completion is judged ONLY by name shape + marker siblings, never size
    -- these all use empty (0-byte) files since staged_complete_path must not
    care."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.stem = os.path.join(self._tmp.name, "Show S01E01 - Title")

    def _touch(self, suffix):
        path = self.stem + suffix
        open(path, "w").close()
        return path

    def test_none_for_part_alone(self):
        self._touch(".mp4.part")
        self.assertIsNone(yt_show.staged_complete_path(self.stem))

    def test_none_for_merge_crash_residue(self):
        # stem.f299.mp4 + stem.f251.webm: pre-merge streams, no final yet.
        self._touch(".f299.mp4")
        self._touch(".f251.webm")
        self.assertIsNone(yt_show.staged_complete_path(self.stem))

    def test_none_for_in_progress_aria2c(self):
        # aria2c writes the REAL filename with only a .aria2 sibling.
        self._touch(".mp4")
        self._touch(".mp4.aria2")
        self.assertIsNone(yt_show.staged_complete_path(self.stem))

    def test_none_for_mid_rename_temp(self):
        self._touch(".temp.mp4")
        self.assertIsNone(yt_show.staged_complete_path(self.stem))

    def test_complete_stem_mp4_alone(self):
        path = self._touch(".mp4")
        self.assertEqual(yt_show.staged_complete_path(self.stem), path)

    def test_complete_despite_stale_fragment_regression(self):
        # Scenario 8 regression: a stale stem.f137.mp4 sitting beside the real
        # final must NOT be picked (the old hits[0] bug could).
        path = self._touch(".mp4")
        self._touch(".f137.mp4")
        self.assertEqual(yt_show.staged_complete_path(self.stem), path)

    def test_prefers_mp4_over_webm(self):
        self._touch(".webm")
        mp4 = self._touch(".mp4")
        self.assertEqual(yt_show.staged_complete_path(self.stem), mp4)


class TestStagedDownloadStemVidBinding(unittest.TestCase):
    """Regression coverage for the traced aliasing bug: clean_source_title
    collapses 'Episode N'-style titles to None, so a title-less playlist's
    stems are bare '<Show> SxxEyy'. If assignment shifts a (season, episode)
    slot to a DIFFERENT video across an interrupted run + resume (a video
    went private, or another playlist committed into that slot), matching
    staged files by bare stem alone would silently reuse the WRONG video's
    bytes under the new one's name. staged_download_stem binds every staged
    file to its video id so this can never happen."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bare_stem = os.path.join(self._tmp.name, "Show S01E02")

    def test_tags_stem_with_video_id(self):
        self.assertEqual(
            yt_show.staged_download_stem(self.bare_stem, "vid2"),
            self.bare_stem + " [vid2]")

    def test_two_videos_sharing_a_bare_stem_do_not_alias(self):
        # v2's staged download completes under S01E02 (bare stem, title-less
        # source) but is never committed (run interrupted).
        v2_path = yt_show.staged_download_stem(self.bare_stem, "vid2") + ".mp4"
        open(v2_path, "w").close()

        # Resume: the slot is reassigned to a DIFFERENT video, v3 (also
        # title-less -> the SAME bare stem). Before vid-binding this would
        # have matched v2's leftover file and silently reused it as v3.
        got_for_v3 = yt_show.staged_complete_path(
            yt_show.staged_download_stem(self.bare_stem, "vid3"))
        self.assertIsNone(got_for_v3)   # v3 must actually download

        # v2's own tagged lookup still finds its own file (reuse still works
        # for the video it actually belongs to).
        got_for_v2 = yt_show.staged_complete_path(
            yt_show.staged_download_stem(self.bare_stem, "vid2"))
        self.assertEqual(got_for_v2, v2_path)


class TestUploadFileDestName(unittest.TestCase):
    """upload_file must send rclone the caller-supplied dest_name, NEVER
    filepath's own basename -- the physical file carries a staging-only
    ' [<video_id>]' tag (staged_download_stem) that must not reach the
    drive."""

    def test_dest_uses_explicit_dest_name_not_basename(self):
        tagged_path = "/stage/Show Season 1/Show S01E02 [vid2].mp4"
        with mock.patch.object(yt_show.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            rc = yt_show.upload_file(
                tagged_path, "gdrive,team_drive=X:", "Show Season 1",
                "Show S01E02 - Title.mp4")
        self.assertEqual(rc, 0)
        cmd = run.call_args[0][0]
        dest = cmd[cmd.index("moveto") + 2]
        self.assertEqual(
            dest, "gdrive,team_drive=X:Show Season 1/Show S01E02 - Title.mp4")
        self.assertNotIn("vid2", dest)


class TestReconcileStage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.stage = self._tmp.name
        self.display = "My Show"

    def _make(self, folder, name):
        d = os.path.join(self.stage, folder)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        open(path, "w").close()
        return path

    def test_deletes_committed_stem_residue(self):
        folder = yt_show.renamer.canonical_folder_name(self.display, 1)
        stale = self._make(folder, "My Show S01E01 - Old Title.mp4")
        show_rec = {"episodes": {
            "v1": {"season": 1, "episode": 1, "title": "Old Title"}}}
        notes = []
        removed = yt_show.reconcile_stage(self.stage, self.display, show_rec,
                                          plan=None, ui_note=notes.append)
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(any("removed 1 stale" in n for n in notes))

    def test_leaves_plan_stem_partials(self):
        folder = yt_show.renamer.canonical_folder_name(self.display, 1)
        partial = self._make(folder, "My Show S01E02 - New Title.mp4.part")
        show_rec = {"episodes": {}}
        plan = [{"vid": "v2", "season": 1, "episode": 2, "title": "New Title"}]
        notes = []
        removed = yt_show.reconcile_stage(self.stage, self.display, show_rec,
                                          plan=plan, ui_note=notes.append)
        self.assertEqual(removed, 0)
        self.assertTrue(os.path.exists(partial))
        self.assertFalse(notes)   # recognized (plan stem) -> no warning

    def test_warns_but_never_deletes_unrecognized_files(self):
        folder = yt_show.renamer.canonical_folder_name(self.display, 1)
        stray = self._make(folder, "totally unrelated file.mp4")
        show_rec = {"episodes": {
            "v1": {"season": 1, "episode": 1, "title": "Old Title"}}}
        plan = [{"vid": "v2", "season": 1, "episode": 2, "title": "New Title"}]
        notes = []
        removed = yt_show.reconcile_stage(self.stage, self.display, show_rec,
                                          plan=plan, ui_note=notes.append)
        self.assertEqual(removed, 0)
        self.assertTrue(os.path.exists(stray))
        self.assertTrue(any("leftover unrecognized" in n for n in notes))

    def test_classifies_planned_episodes(self):
        # skip-download-upload-directly (complete staged file already there),
        # download (nothing staged yet), delete-stale (committed residue) --
        # all three in one pass.
        folder = yt_show.renamer.canonical_folder_name(self.display, 1)
        complete = self._make(folder, "My Show S01E02 - New Title.mp4")
        self._make(folder, "My Show S01E01 - Old Title.mp4")   # stale residue
        show_rec = {"episodes": {
            "v1": {"season": 1, "episode": 1, "title": "Old Title"}}}
        plan = [{"vid": "v2", "season": 1, "episode": 2, "title": "New Title"},
                {"vid": "v3", "season": 1, "episode": 3, "title": "Third"}]
        removed = yt_show.reconcile_stage(self.stage, self.display, show_rec,
                                          plan=plan, ui_note=lambda *_: None)
        self.assertEqual(removed, 1)   # only the S01E01 residue
        self.assertTrue(os.path.exists(complete))   # S01E02 stays for reuse
        out_stem = os.path.join(
            self.stage, folder,
            yt_show.renamer.canonical_episode_name(
                self.display,
                yt_show.renamer.synth_episode(self.display, 1, 2, "New Title"),
                ""))
        self.assertEqual(yt_show.staged_complete_path(out_stem), complete)
        out_stem3 = os.path.join(
            self.stage, folder,
            yt_show.renamer.canonical_episode_name(
                self.display,
                yt_show.renamer.synth_episode(self.display, 1, 3, "Third"),
                ""))
        self.assertIsNone(yt_show.staged_complete_path(out_stem3))   # download

    def test_recognizes_vid_tagged_staged_files(self):
        # Real staged residue/partials are now always written under the
        # vid-tagged form (staged_download_stem), never the bare stem --
        # reconcile must still classify these correctly: tagged committed
        # residue gets deleted, tagged plan partials are left alone.
        folder = yt_show.renamer.canonical_folder_name(self.display, 1)
        tagged_residue = self._make(
            folder, "My Show S01E01 - Old Title [v1].mp4")
        tagged_partial = self._make(
            folder, "My Show S01E02 - New Title [v2].mp4.part")
        show_rec = {"episodes": {
            "v1": {"season": 1, "episode": 1, "title": "Old Title"}}}
        plan = [{"vid": "v2", "season": 1, "episode": 2, "title": "New Title"}]
        notes = []
        removed = yt_show.reconcile_stage(self.stage, self.display, show_rec,
                                          plan=plan, ui_note=notes.append)
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(tagged_residue))
        self.assertTrue(os.path.exists(tagged_partial))
        # recognized as a tagged plan stem -> no "leftover unrecognized" warning
        self.assertFalse(any("leftover unrecognized" in n for n in notes))

    def test_clean_source_title_is_idempotent(self):
        # reconcile_stage recomputes a committed episode's stem by feeding the
        # ALREADY-cleaned stored title back through clean_source_title (via
        # synth_episode) -- this must be a no-op the second time, or a
        # committed stem would drift from the one that was actually uploaded.
        show = "My Show"
        gnarly = [
            "My Show - Episode 3: The Big One [1080p] #shorts",
            "Weird | Title (Official) — S02E10",
            "Trailing dots... and -- dashes--",
            "Emoji 🎬 Title 🔥 with symbols ★",
        ]
        for t in gnarly:
            once = yt_show.renamer.clean_source_title(t, show)
            twice = yt_show.renamer.clean_source_title(once, show)
            self.assertEqual(twice, once, "not idempotent for %r" % t)


if __name__ == "__main__":
    unittest.main()
