#!/usr/bin/env python3
"""Headless tests for the TV rename engine wired into offload_app (plan phases
3/4/6/7): RenameCache round-trip, continuity route() mining/matching/year-guard,
HOOK B dry-run (logs but uploads as-is), HOOK B live (renames a fake local
payload into the canonical + nesting layout), and journal crash-recovery /
rollback (remove-not-resume).

    .venv/bin/python3 test_rename_hooks.py   (or: python3 -m unittest ...)

No real Motrix/Google/UI: the on-disk work runs against tmp dirs, and the
engine lifecycle uses the same RecordingClient/Engine fakes as
test_offload_app. rumps is NOT imported (offload_app's UI import is lazy).
"""
import os
import tempfile
import unittest

import offload_app as app
import renamer as r


# --- fakes (mirror test_offload_app's engine lifecycle recorders) ----------

class RecordingEngine:
    def __init__(self, calls):
        self.calls = calls

    def stop_torrent(self, gid):
        self.calls.append(("stop", gid))

    def start_torrent(self, gid):
        self.calls.append(("start", gid))

    def remove_torrent(self, gid):
        self.calls.append(("remove", gid))


class RecordingClient:
    def __init__(self, engine):
        self.engine = engine

    def for_gid(self, gid):
        return self.engine


def _write_tree(base, relpaths):
    """Create empty files (with 1 byte so a size gate wouldn't trip) at each
    relpath under base. Returns the list of absolute paths."""
    out = []
    for rel in relpaths:
        p = os.path.join(base, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"x")
        out.append(p)
    return out


def _tree(base):
    """Sorted set of relpaths (files) under base, '/'-joined."""
    out = set()
    for root, _dirs, files in os.walk(base):
        for fn in files:
            out.add(os.path.relpath(os.path.join(root, fn), base)
                    .replace(os.sep, "/"))
    return out


# ---------------------------------------------------------------------------
# RenameCache round-trip
# ---------------------------------------------------------------------------

class TestRenameCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "rename_cache.json")

    def test_record_lookup_alias_and_persistence(self):
        c = r.RenameCache(self.path)
        c.record_show("northbound", display="Northbound", aliases=["prodnorthbound"],
                      drive="DriveB", drive_id="0AG", layout="shared")
        # lookup by slug, and by alias
        self.assertEqual(c.lookup("northbound")["display"], "Northbound")
        self.assertEqual(c.lookup("prodnorthbound")["drive"], "DriveB")
        self.assertIsNone(c.lookup("nope"))
        # lookup returns a COPY (mutating it must not touch the cache)
        rec = c.lookup("northbound")
        rec["drive"] = "MUTATED"
        self.assertEqual(c.lookup("northbound")["drive"], "DriveB")

        c.add_alias("northbound", "northboundweb")
        self.assertIn("northboundweb", c.lookup("northbound")["aliases"])

        c.record_season("northbound", 2, "gid-1", episodes=5,
                        source_pattern="PROD.Northbound.S02Exx-AB1CD")
        c.append_history("northbound", [
            {"gid": "gid-1", "original": "PROD.Northbound.S02E01.mkv",
             "renamed": "Northbound S02E01 - First Light.mkv", "ts": 0}])

        # reload from disk -> everything survives atomically
        c2 = r.RenameCache(self.path)
        rec2 = c2.lookup("northbound")
        self.assertEqual(rec2["seasons"]["2"]["episodes"], 5)
        self.assertEqual(rec2["seasons"]["2"]["gid"], "gid-1")
        self.assertEqual(len(rec2["history"]), 1)
        self.assertEqual(rec2["folder_format"], "{show} Season {season}")

    def test_record_show_never_clobbers_seasons(self):
        c = r.RenameCache(self.path)
        c.record_show("s", display="Show", drive="DriveA")
        c.record_season("s", 1, "g", 3, "pat")
        # a second confirm updates the stub but keeps the recorded season
        c.record_show("s", display="Show Renamed", drive="DriveI")
        rec = c.lookup("s")
        self.assertEqual(rec["display"], "Show Renamed")
        self.assertEqual(rec["drive"], "DriveI")
        self.assertIn("1", rec["seasons"])


# ---------------------------------------------------------------------------
# Continuity route(): mining + matching + year/qualifier guard
# ---------------------------------------------------------------------------

class TestRoute(unittest.TestCase):
    def test_cache_tier1_wins(self):
        tmp = tempfile.mkdtemp()
        c = r.RenameCache(os.path.join(tmp, "rc.json"))
        c.record_show("northbound", display="Northbound", aliases=["prodnorthbound"],
                      drive="DriveB", drive_id="0AG")
        hint = r.route("prodnorthbound", [], c, {})
        self.assertIsNotNone(hint)
        self.assertEqual(hint.drive, "DriveB")
        self.assertEqual(hint.drive_id, "0AG")
        self.assertEqual(hint.source, "cache")
        self.assertEqual(hint.confidence, 1.0)

    def test_mine_decisions_exact_and_containment(self):
        decisions = {
            "g1": {"name": "PROD.Northbound.S02E01.First.Light.1080p.WEB-DL-AB1CD",
                   "choice": "drive:DriveB", "handled": True},
            # a movie: no episode/season marker -> must be ignored by the miner
            "g2": {"name": "The.Matrix.1999.1080p.BluRay.mkv",
                   "choice": "drive:DriveA", "handled": True},
        }
        exact = r.route("prodnorthbound", [], None, decisions)
        self.assertEqual(exact.drive, "DriveB")
        self.assertEqual(exact.source, "decisions")
        self.assertEqual(exact.confidence, 1.0)
        # containment: the clean "Northbound" slug still resolves to the messy
        # "PROD Northbound" decision (<=1 vanity token).
        contained = r.route("northbound", [], None, decisions)
        self.assertEqual(contained.drive, "DriveB")
        self.assertLess(contained.confidence, 1.0)

    def test_already_choice_is_mined(self):
        decisions = {"g": {"name": "Foo.S01E01.720p.mkv",
                           "choice": "already:Films", "handled": True}}
        self.assertEqual(r.route("foo", [], None, decisions).drive, "Films")

    def test_local_and_non_tv_decisions_never_route(self):
        decisions = {
            "g1": {"name": "Foo.S01E01.720p.mkv", "choice": "local",
                   "handled": True},
            "g2": {"name": "Some.Album.FLAC", "choice": "drive:Music",
                   "handled": True},
        }
        self.assertIsNone(r.route("foo", [], None, decisions))
        self.assertIsNone(r.route("somealbum", [], None, decisions))

    def test_year_qualifier_guard_blocks_cross_wire(self):
        # "The Bureau 2005" is on DriveA. A fresh "The Bureau UK" must NOT wire onto
        # it (distinguishers disagree), while a matching "The Bureau 2005" does.
        decisions = {"g": {"name": "The.Bureau.2005.S01E01.Pilot.720p.mkv",
                           "choice": "drive:DriveA", "handled": True}}
        self.assertIsNone(r.route("thebureauuk", [], None, decisions))
        self.assertEqual(r.route("thebureau2005", [], None, decisions).drive,
                         "DriveA")

    def test_no_match_returns_none(self):
        decisions = {"g": {"name": "Breaking.Bad.S01E01.720p.mkv",
                           "choice": "drive:DriveC", "handled": True}}
        self.assertIsNone(r.route("severance", [], None, decisions))


# ---------------------------------------------------------------------------
# HOOK B — dry-run: builds the plan, logs it, uploads AS-IS (no disk change)
# ---------------------------------------------------------------------------

class TestHookBDryRun(unittest.TestCase):
    def test_dry_run_uploads_as_is_and_leaves_disk_untouched(self):
        tmp = tempfile.mkdtemp()
        sup = tempfile.mkdtemp()
        folder = ("PROD.Northbound.S03.1080p.WEB-DL.ESub-XYZGRP")
        rels = ["%s/PROD.Northbound.S03.EP0%d.1080p.WEB-DL.ESub-XYZGRP.mkv"
                % (folder, i) for i in range(1, 6)]
        _write_tree(tmp, rels)
        payload = os.path.join(tmp, folder)
        before = _tree(tmp)

        cache = r.RenameCache(os.path.join(sup, "rc.json"))
        hook = app._apply_rename_hook(
            payload, "gid-1", "PROD Northbound S03", "DriveB",
            {"enabled": True, "dry_run": True, "confirm_new_shows": False},
            cache, confirm_cb=None, support_dir=sup)

        self.assertFalse(hook["renamed"])
        self.assertEqual(hook["upload_path"], payload)
        self.assertEqual(_tree(tmp), before)      # nothing moved on disk
        # dry-run must not persist a season into the cache
        self.assertIsNone(cache.lookup("prodnorthbound"))
        # no journal left behind
        self.assertFalse(os.path.exists(
            r.journal_default_path(sup)))

    def test_disabled_is_upload_as_is(self):
        tmp = tempfile.mkdtemp()
        sup = tempfile.mkdtemp()
        _write_tree(tmp, ["X.S01E01.mkv"])
        payload = os.path.join(tmp, "X.S01E01.mkv")
        hook = app._apply_rename_hook(
            payload, "g", "X", "Z", {"enabled": False}, None, None,
            support_dir=sup)
        self.assertFalse(hook["renamed"])
        self.assertEqual(hook["upload_path"], payload)


# ---------------------------------------------------------------------------
# HOOK B — live: renames a fake payload into the canonical + nesting layout
# ---------------------------------------------------------------------------

class TestHookBLive(unittest.TestCase):
    def _live_cfg(self):
        return {"enabled": True, "dry_run": False, "confirm_new_shows": False}

    def test_season_folder_renamed_and_nested(self):
        tmp = tempfile.mkdtemp()
        sup = tempfile.mkdtemp()
        folder = "PROD.Northbound.S03.1080p.WEB-DL.ESub-XYZGRP"
        rels = ["%s/PROD.Northbound.S03.EP0%d.1080p.WEB-DL.ESub-XYZGRP.mkv"
                % (folder, i) for i in range(1, 6)]
        _write_tree(tmp, rels)
        payload = os.path.join(tmp, folder)

        cache = r.RenameCache(os.path.join(sup, "rc.json"))
        hook = app._apply_rename_hook(
            payload, "gid-1", "PROD Northbound S03", "DriveB", self._live_cfg(),
            cache, confirm_cb=None, support_dir=sup)

        self.assertTrue(hook["renamed"])
        new_root = os.path.join(tmp, "PROD Northbound Season 3")
        self.assertEqual(hook["upload_path"], new_root)
        # exact canonical tree, and it nests (every file is a valid episode).
        want = {"PROD Northbound Season 3/PROD Northbound S03E0%d.mkv" % i
                for i in range(1, 6)}
        self.assertEqual(_tree(tmp), want)
        for f in _tree(tmp):
            leaf = os.path.basename(f).rsplit(".", 1)[0]
            self.assertIsNotNone(r.contract_detect_episode(leaf))
        folder_leaf = "PROD Northbound Season 3"
        self.assertNotEqual(r.contract_split_season_suffix(folder_leaf),
                            (None, None))
        # the old messy folder is gone
        self.assertFalse(os.path.exists(payload))

        # finalize records the season + history in the cache
        hook["finalize"]()
        rec = cache.lookup("prodnorthbound")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["seasons"]["3"]["episodes"], 5)
        self.assertEqual(len(rec["history"]), 5)
        # journal cleared on finalize
        self.assertFalse(os.path.exists(r.journal_default_path(sup)))

    def test_single_loose_file_wrapped(self):
        tmp = tempfile.mkdtemp()
        sup = tempfile.mkdtemp()
        rel = "Meltdown.S01E03.Open.Wide.1080p.WEB-DL.mkv"
        _write_tree(tmp, [rel])
        payload = os.path.join(tmp, rel)
        hook = app._apply_rename_hook(
            payload, "g", "Meltdown", "DriveD", self._live_cfg(), None, None,
            support_dir=sup)
        self.assertTrue(hook["renamed"])
        self.assertEqual(
            _tree(tmp),
            {"Meltdown Season 1/Meltdown S01E03 - Open Wide.mkv"})
        self.assertEqual(hook["upload_path"],
                         os.path.join(tmp, "Meltdown Season 1"))

    def test_show_override_from_cache_display(self):
        # a cached show renames silently using its pinned display name.
        tmp = tempfile.mkdtemp()
        sup = tempfile.mkdtemp()
        folder = "PROD.Northbound.S03.WEB-DL"
        _write_tree(tmp, ["%s/PROD.Northbound.S03.EP04.WEB-DL.mkv" % folder])
        payload = os.path.join(tmp, folder)
        cache = r.RenameCache(os.path.join(sup, "rc.json"))
        cache.record_show("prodnorthbound", display="Northbound",
                          aliases=["prodnorthbound"], drive="DriveB")
        hook = app._apply_rename_hook(
            payload, "g", "x", "DriveB", self._live_cfg(), cache, None,
            support_dir=sup)
        self.assertTrue(hook["renamed"])
        self.assertEqual(_tree(tmp),
                         {"Northbound Season 3/Northbound S03E04.mkv"})

    def test_confirm_cancel_uploads_as_is(self):
        tmp = tempfile.mkdtemp()
        sup = tempfile.mkdtemp()
        folder = "PROD.Northbound.S03.WEB-DL"
        _write_tree(tmp, ["%s/PROD.Northbound.S03.EP04.WEB-DL.mkv" % folder])
        payload = os.path.join(tmp, folder)
        before = _tree(tmp)
        hook = app._apply_rename_hook(
            payload, "g", "x", "DriveB",
            {"enabled": True, "dry_run": False, "confirm_new_shows": True},
            r.RenameCache(os.path.join(sup, "rc.json")),
            confirm_cb=lambda name, samples: None,   # user cancels/timeout
            support_dir=sup)
        self.assertFalse(hook["renamed"])
        self.assertEqual(_tree(tmp), before)

    def test_confirm_edit_becomes_override(self):
        tmp = tempfile.mkdtemp()
        sup = tempfile.mkdtemp()
        folder = "PROD.Northbound.S03.WEB-DL"
        _write_tree(tmp, ["%s/PROD.Northbound.S03.EP04.WEB-DL.mkv" % folder])
        payload = os.path.join(tmp, folder)
        seen = {}

        def confirm(name, samples):
            seen["name"] = name
            seen["samples"] = samples
            return "Northbound"                       # user trims the vanity tag

        cache = r.RenameCache(os.path.join(sup, "rc.json"))
        hook = app._apply_rename_hook(
            payload, "g", "x", "DriveB",
            {"enabled": True, "dry_run": False, "confirm_new_shows": True},
            cache, confirm_cb=confirm, support_dir=sup)
        self.assertEqual(seen["name"], "PROD Northbound")   # derived default shown
        self.assertTrue(hook["renamed"])
        self.assertEqual(_tree(tmp),
                         {"Northbound Season 3/Northbound S03E04.mkv"})
        # confirm seeded a cache stub with the edited display + drive
        self.assertEqual(cache.lookup("prodnorthbound")["display"], "Northbound")
        self.assertEqual(cache.lookup("prodnorthbound")["drive"], "DriveB")


# ---------------------------------------------------------------------------
# Journal crash-recovery + rollback (Phase 7)
# ---------------------------------------------------------------------------

class TestJournalRecovery(unittest.TestCase):
    def _make_applied_payload(self, tmp, sup):
        """Live-apply a rename WITHOUT clearing the journal (simulates a crash
        after apply, before a verified upload)."""
        folder = "PROD.Northbound.S03.1080p.WEB-DL.ESub-XYZGRP"
        rels = ["%s/PROD.Northbound.S03.EP0%d.1080p.WEB-DL.ESub-XYZGRP.mkv"
                % (folder, i) for i in range(1, 4)]
        _write_tree(tmp, rels)
        payload = os.path.join(tmp, folder)
        base_dir, relpaths = r.scan_payload(payload)
        planobj = r.plan(relpaths)
        renames = r.build_rename_ops(base_dir, planobj)
        jpath = r.journal_default_path(sup)
        r.write_journal(jpath, "gid-1", base_dir, renames)
        r.apply_rename(base_dir, planobj)
        return rels, jpath

    def test_replay_restores_originals_and_clears_journal(self):
        tmp = tempfile.mkdtemp()
        sup = tempfile.mkdtemp()
        original_rels, jpath = self._make_applied_payload(tmp, sup)
        # sanity: the payload is currently in the RENAMED state
        self.assertIn("PROD Northbound Season 3/PROD Northbound S03E01.mkv",
                      _tree(tmp))
        self.assertTrue(os.path.exists(jpath))

        found = r.replay_journal(jpath, log=lambda *a: None)
        self.assertTrue(found)
        # original names restored, renamed tree gone, journal cleared
        self.assertEqual(_tree(tmp), set(original_rels))
        self.assertFalse(os.path.exists(jpath))
        self.assertFalse(os.path.exists(
            os.path.join(tmp, "PROD Northbound Season 3")))

    def test_replay_noop_without_journal(self):
        sup = tempfile.mkdtemp()
        self.assertFalse(
            r.replay_journal(r.journal_default_path(sup), log=lambda *a: None))


# ---------------------------------------------------------------------------
# perform_upload: live rename then FAILED upload -> rollback, remove-not-resume
# ---------------------------------------------------------------------------

class TestPerformUploadRenameFailure(unittest.TestCase):
    def _payload(self, tmp):
        folder = "PROD.Northbound.S03.1080p.WEB-DL.ESub-XYZGRP"
        rels = ["%s/PROD.Northbound.S03.EP0%d.1080p.WEB-DL.ESub-XYZGRP.mkv"
                % (folder, i) for i in range(1, 4)]
        _write_tree(tmp, rels)
        return os.path.join(tmp, folder), rels

    def test_renamed_then_failed_rolls_back_and_does_not_resume(self):
        tmp = tempfile.mkdtemp()
        sup = tempfile.mkdtemp()
        payload, original_rels = self._payload(tmp)
        calls = []
        client = RecordingClient(RecordingEngine(calls))
        seen = {}

        def fake_up(path, drive, bwlimit="", progress_cb=None):
            seen["path"] = path
            # payload must be RENAMED at the moment we "upload"
            seen["tree"] = _tree(tmp)
            return 1, "boom"   # simulate a failed upload

        rc, _out = app.perform_upload(
            client, payload, "DriveB", "tm-hash", todrive_up=fake_up,
            name="PROD Northbound S03",
            rename_cfg={"enabled": True, "dry_run": False,
                        "confirm_new_shows": False},
            cache=r.RenameCache(os.path.join(sup, "rc.json")),
            confirm_cb=None, support_dir=sup)

        self.assertEqual(rc, 1)
        # rename happened before the (failed) upload
        self.assertEqual(seen["path"],
                         os.path.join(tmp, "PROD Northbound Season 3"))
        self.assertIn("PROD Northbound Season 3/PROD Northbound S03E01.mkv",
                      seen["tree"])
        # remove-not-resume: the torrent is stopped, never started/removed
        self.assertEqual([c[0] for c in calls], ["stop"])
        # rollback restored the original names, journal cleared
        self.assertEqual(_tree(tmp), set(original_rels))
        self.assertFalse(os.path.exists(r.journal_default_path(sup)))

    def test_renamed_then_success_finalizes_and_removes(self):
        tmp = tempfile.mkdtemp()
        sup = tempfile.mkdtemp()
        payload, _ = self._payload(tmp)
        calls = []
        client = RecordingClient(RecordingEngine(calls))
        cache = r.RenameCache(os.path.join(sup, "rc.json"))

        def fake_up(path, drive, bwlimit="", progress_cb=None):
            return 0, "ok"

        rc, _out = app.perform_upload(
            client, payload, "DriveB", "tm-hash", todrive_up=fake_up,
            name="PROD Northbound S03",
            rename_cfg={"enabled": True, "dry_run": False,
                        "confirm_new_shows": False},
            cache=cache, confirm_cb=None, support_dir=sup)

        self.assertEqual(rc, 0)
        self.assertEqual([c[0] for c in calls], ["stop", "remove"])
        # renamed tree persists (uploaded), season recorded, journal cleared
        self.assertIn("PROD Northbound Season 3/PROD Northbound S03E01.mkv",
                      _tree(tmp))
        self.assertEqual(cache.lookup("prodnorthbound")["seasons"]["3"]
                         ["episodes"], 3)
        self.assertFalse(os.path.exists(r.journal_default_path(sup)))

    def test_no_rename_failure_still_resumes(self):
        # rename disabled: the classic failure path (resume the torrent).
        tmp = tempfile.mkdtemp()
        sup = tempfile.mkdtemp()
        payload, _ = self._payload(tmp)
        calls = []
        client = RecordingClient(RecordingEngine(calls))
        rc, _out = app.perform_upload(
            client, payload, "DriveB", "tm-hash",
            todrive_up=lambda p, d, b="", progress_cb=None: (1, "x"),
            rename_cfg={"enabled": False}, support_dir=sup)
        self.assertEqual([c[0] for c in calls], ["stop", "start"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
