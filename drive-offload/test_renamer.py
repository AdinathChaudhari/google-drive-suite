#!/usr/bin/env python3
"""Headless tests for renamer.py — the TV cleaning/renaming core (plan phases
0/1/2/5). Pure string / relative-path work, no filesystem or network.

    .venv/bin/python3 test_renamer.py     (or: python3 test_renamer.py)

Every emitted name is asserted to pass the VENDORED drivecast contract regexes
(the GATE), so a passing suite means "provably nests + merges" in drivecast.
"""
import unittest

import renamer as r


def _dst_map(planobj):
    """{src: dst} over all ops (file + dir)."""
    return {src: dst for src, dst in planobj.ops}


def _assert_contract(testcase, planobj):
    """Every file op leaf matches the contiguous SxxExx; every folder op leaf
    (and every synthesized wrap parent) passes non-range split_season_suffix."""
    # A file op's parent is either an OLD folder being renamed by a dir_op
    # (its NEW name is validated below) or a NEW wrap-created folder (validated
    # here). Skip the former; contract-check the latter.
    dir_srcs = {src for src, _d in planobj.dir_ops}
    for _src, dst in planobj.file_ops:
        leaf = dst.split("/")[-1]
        stem = leaf.rsplit(".", 1)[0]
        testcase.assertIsNotNone(
            r.contract_detect_episode(stem),
            "file %r rejected by contract" % leaf)
        parent = "/".join(dst.split("/")[:-1])
        if parent and parent not in dir_srcs:
            testcase.assertNotEqual(
                r.contract_split_season_suffix(parent.split("/")[-1]),
                (None, None), "wrap folder %r rejected" % parent)
    for _src, dst in planobj.dir_ops:
        testcase.assertNotEqual(
            r.contract_split_season_suffix(dst.split("/")[-1]),
            (None, None), "folder %r rejected by contract" % dst)


# ---------------------------------------------------------------------------
# Phase 0 — the vendored contract (the GATE)
# ---------------------------------------------------------------------------

class TestContractGate(unittest.TestCase):
    def test_detect_episode_contiguous_only(self):
        # The bug the engine fixes: S03.EP04 is NOT valid to drivecast.
        self.assertEqual(r.contract_detect_episode("PROD Northbound S03E04"), (3, 4))
        self.assertIsNone(r.contract_detect_episode("PROD.Northbound.S03.EP04"))
        self.assertEqual(r.contract_detect_episode("Show 2x01"), (2, 1))

    def test_split_season_suffix_and_range_rejection(self):
        self.assertEqual(r.contract_split_season_suffix("Northbound Season 3"),
                         ("Northbound", 3))
        self.assertEqual(r.contract_split_season_suffix("Foo S02"), ("Foo", 2))
        self.assertEqual(
            r.contract_split_season_suffix("The Bureau Season 1-9 S01-S09"),
            (None, None))

    def test_pure_season(self):
        self.assertEqual(r.contract_pure_season("Season 3"), 3)
        self.assertEqual(r.contract_pure_season("Specials"), 0)
        self.assertIsNone(r.contract_pure_season("Northbound Season 3"))

    def test_version_constant(self):
        self.assertIsInstance(r.CONTRACT_VERSION, int)


# ---------------------------------------------------------------------------
# Phase 1 — R2 episode tokenizer
# ---------------------------------------------------------------------------

class TestEpisodeTokenizer(unittest.TestCase):
    def _se(self, name, folder_season=None):
        ep = r.parse_episode(name, folder_season=folder_season)
        self.assertIsNotNone(ep, name)
        return (ep.season, ep.episode)

    def test_northbound_ep_bug_absorbed(self):
        for name in ("PROD.Northbound.S03.EP04.1080p.mkv",
                     "PROD.Northbound.S03 EP04.mkv",
                     "PROD.Northbound.S03EP04.mkv",
                     "Show.S03.EP.04.mkv"):
            self.assertEqual(self._se(name), (3, 4), name)

    def test_sxxexx_passthrough(self):
        self.assertEqual(self._se("PROD.Northbound.S02E01.First.Light.mkv"), (2, 1))
        self.assertEqual(self._se("show.s3e4.mkv"), (3, 4))

    def test_nxnn(self):
        self.assertEqual(self._se("Some.Show.2x01.Pilot.720p.mkv"), (2, 1))

    def test_episode_only_with_folder_season(self):
        self.assertEqual(self._se("The Show EP04.mkv", folder_season=3), (3, 4))
        # No folder season -> not identifiable (never guess season 1).
        self.assertIsNone(r.parse_episode("The Show EP04.mkv"))

    def test_season_episode_words(self):
        self.assertEqual(self._se("Show Season 2 Episode 5.mkv"), (2, 5))

    def test_multi_episode_span(self):
        ep = r.parse_episode("Show.S01E01-E02.mkv")
        self.assertEqual((ep.season, ep.episode, ep.last_episode), (1, 1, 2))
        name = r.canonical_episode_name("Show", ep, ".mkv")
        self.assertEqual(name, "Show S01E01-E02.mkv")
        self.assertEqual(r.contract_detect_episode(name), (1, 1))  # nests as E01


# ---------------------------------------------------------------------------
# Phase 1 — R1/R3/R4/R5 show + title normalization
# ---------------------------------------------------------------------------

class TestShowNormalization(unittest.TestCase):
    def test_dotted_release_name_to_spaces(self):
        ep = r.parse_episode("PROD.Northbound.S03.EP04.1080p.WEB-DL.mkv")
        self.assertEqual(ep.show, "PROD Northbound")

    def test_swat_acronym_preserved(self):
        ep = r.parse_episode("S.W.A.T.S01E01.720p.x264.mkv")
        self.assertEqual(ep.show, "S.W.A.T.")
        folder = r.canonical_folder_name(ep.show, ep.season)
        self.assertEqual(folder, "S.W.A.T. Season 1")
        self.assertNotEqual(r.contract_split_season_suffix(folder), (None, None))

    def test_that_70s_show_year_protection(self):
        ep = r.parse_episode("That.'90s.Test.S02E03.1080p.WEB-DL.mkv")
        self.assertEqual(ep.show, "That '90s Test")
        self.assertIsNone(ep.year)

    def test_real_year_captured_and_stripped_from_display(self):
        ep = r.parse_episode("The.Bureau.2005.S01E01.Pilot.720p.mkv")
        self.assertEqual(ep.show, "The Bureau")
        self.assertEqual(ep.year, 2005)

    def test_site_prefix_stripped(self):
        ep = r.parse_episode("www.Example.com-Show.S01E02.720p.mkv")
        self.assertEqual(ep.show, "Show")

    def test_title_quality_free(self):
        ep = r.parse_episode(
            "PROD.Northbound.S02E01.First.Light.1080p.HYBRID.WEB-DL.AAC2.0.mkv")
        self.assertEqual(ep.title, "First Light")
        ep2 = r.parse_episode(
            "PROD.Northbound.S03.EP04.1080p.WEB-DL.AAC2.0.H.264.ESub-XYZGRP.mkv")
        self.assertIsNone(ep2.title)   # never invent a title


# ---------------------------------------------------------------------------
# Phase 1 — plan() over season folders (the Northbound case)
# ---------------------------------------------------------------------------

class TestPlanSeasonFolder(unittest.TestCase):
    def test_single_season_folder_rename_in_place(self):
        rel = [
            "PROD.Northbound.S02.1080p.WEB-DL-AB1CD/"
            "PROD.Northbound.S02E01.First.Light.1080p.mkv",
            "PROD.Northbound.S02.1080p.WEB-DL-AB1CD/"
            "PROD.Northbound.S02E02.Second.Wind.1080p.mkv",
        ]
        p = r.plan(rel)
        self.assertFalse(p.rejected, p.reason)
        self.assertEqual([o[1] for o in p.dir_ops], ["PROD Northbound Season 2"])
        dst = _dst_map(p)
        # files renamed IN PLACE (parent unchanged; folder op moves them later)
        self.assertEqual(
            dst[rel[0]],
            "PROD.Northbound.S02.1080p.WEB-DL-AB1CD/"
            "PROD Northbound S02E01 - First Light.mkv")
        _assert_contract(self, p)

    def test_ep_bug_folder_yields_valid_episodes(self):
        rel = [
            "PROD.Northbound.S03.1080p.WEB-DL.ESub-XYZGRP/"
            "PROD.Northbound.S03.EP0%d.1080p.WEB-DL.ESub-XYZGRP.mkv" % i
            for i in range(1, 6)
        ]
        p = r.plan(rel)
        self.assertFalse(p.rejected, p.reason)
        self.assertEqual([o[1] for o in p.dir_ops], ["PROD Northbound Season 3"])
        dst = _dst_map(p)
        self.assertTrue(dst[rel[3]].endswith("PROD Northbound S03E04.mkv"))
        _assert_contract(self, p)

    def test_already_canonical_is_noop(self):
        rel = ["Northbound Season 2/Northbound S02E01 - First Light.mkv"]
        p = r.plan(rel)
        self.assertFalse(p.rejected, p.reason)
        self.assertEqual(p.ops, ())


# ---------------------------------------------------------------------------
# Phase 1 — §9 edge cases
# ---------------------------------------------------------------------------

class TestNumericRangeFolder(unittest.TestCase):
    def test_range_folder_never_emitted(self):
        # A whole-series pack: the range folder itself is NOT renamed to one
        # season; its files split into per-season <Show> Season N folders.
        rel = [
            "The.Bureau.Season.1-9.S01-S09/The.Bureau.S01E01.Pilot.720p.mkv",
            "The.Bureau.Season.1-9.S01-S09/The.Bureau.S02E01.720p.mkv",
        ]
        self.assertIsNone(r._folder_season("The.Bureau.Season.1-9.S01-S09"))
        p = r.plan(rel)
        self.assertFalse(p.rejected, p.reason)
        # no emitted folder is a range, and files scatter to S01/S02
        for _s, dst in p.ops:
            self.assertNotRegex(dst, r"\d+\s*-\s*\d+")
        dsts = set(_dst_map(p).values())
        self.assertIn("The Bureau Season 1/The Bureau S01E01 - Pilot.mkv", dsts)
        self.assertIn("The Bureau Season 2/The Bureau S02E01.mkv", dsts)
        # old emptied pack folder marked for removal
        self.assertIn("The.Bureau.Season.1-9.S01-S09", p.deletes)
        _assert_contract(self, p)


class TestLooseFile(unittest.TestCase):
    def test_single_loose_file_wrapped(self):
        # One bare episode file must be wrapped in a season folder (a lone
        # marked video fails drivecast's >=2 rule otherwise).
        rel = ["Meltdown.S01E03.Open.Wide.1080p.WEB-DL.mkv"]
        p = r.plan(rel)
        self.assertFalse(p.rejected, p.reason)
        self.assertEqual(
            _dst_map(p)[rel[0]],
            "Meltdown Season 1/Meltdown S01E03 - Open Wide.mkv")
        self.assertEqual(p.dir_ops, ())   # wrap parent auto-created, no dir op
        _assert_contract(self, p)


class TestSubtitleLockstep(unittest.TestCase):
    def test_subtitle_follows_its_video(self):
        rel = [
            "Show.S01/Show.S01E01.Pilot.1080p.WEB-DL.mkv",
            "Show.S01/Show.S01E01.en.srt",
        ]
        p = r.plan(rel)
        self.assertFalse(p.rejected, p.reason)
        dst = _dst_map(p)
        self.assertEqual(dst[rel[0]], "Show.S01/Show S01E01 - Pilot.mkv")
        # subtitle gets the video's stem + preserved language suffix
        self.assertEqual(dst[rel[1]], "Show.S01/Show S01E01 - Pilot.en.srt")


class TestJunkPrune(unittest.TestCase):
    def test_junk_pruned_control_files_kept(self):
        rel = [
            "Show.S01/Show.S01E01.720p.mkv",
            "Show.S01/._Show.S01E01.720p.mkv",   # AppleDouble twin
            "Show.S01/.DS_Store",
            "Show.S01/info.nfo",
            "Show.S01/sample.mkv",
            "Show.S01/Show.S01E01.720p.mkv.part",   # control file — KEEP
        ]
        p = r.plan(rel)
        self.assertFalse(p.rejected, p.reason)
        for junk in ("Show.S01/._Show.S01E01.720p.mkv", "Show.S01/.DS_Store",
                     "Show.S01/info.nfo", "Show.S01/sample.mkv"):
            self.assertIn(junk, p.deletes)
        # .part is load-bearing and must never be pruned
        self.assertNotIn("Show.S01/Show.S01E01.720p.mkv.part", p.deletes)


class TestR13Gate(unittest.TestCase):
    def test_duplicate_destination_rejects_plan(self):
        # A PROPER + the original both normalize to the same stem -> collision.
        rel = [
            "Show.S01/Show.S01E01.1080p.WEB-DL.mkv",
            "Show.S01/Show.S01E01.PROPER.1080p.WEB-DL.mkv",
        ]
        p = r.plan(rel)
        self.assertTrue(p.rejected)
        self.assertIn("same destination", p.reason)
        self.assertEqual(p.ops, ())   # nothing emitted -> caller uploads as-is

    def test_show_override_applies(self):
        rel = ["PROD.Northbound.S03.WEB-DL/PROD.Northbound.S03.EP04.WEB-DL.mkv"]
        p = r.plan(rel, show_override="Northbound")
        self.assertFalse(p.rejected, p.reason)
        self.assertEqual([o[1] for o in p.dir_ops], ["Northbound Season 3"])
        self.assertTrue(_dst_map(p)[rel[0]].endswith("Northbound S03E04.mkv"))


# ---------------------------------------------------------------------------
# identify()
# ---------------------------------------------------------------------------

class TestIdentify(unittest.TestCase):
    def test_identify_season_folder(self):
        rel = [
            "PROD.Northbound.S02.1080p-AB1CD/PROD.Northbound.S02E01.First.Light.mkv",
            "PROD.Northbound.S02.1080p-AB1CD/PROD.Northbound.S02E02.mkv",
        ]
        ident = r.identify(rel)
        self.assertTrue(ident.is_tv)
        self.assertEqual(ident.show, "PROD Northbound")
        self.assertEqual(ident.show_slug, "prodnorthbound")
        self.assertEqual(ident.season, 2)
        self.assertEqual(ident.layout_hint, "season_folder")

    def test_identify_non_tv(self):
        ident = r.identify("Blade.Runner.2049.2017.1080p.BluRay.mkv")
        self.assertFalse(ident.is_tv)
        self.assertEqual(ident.layout_hint, "none")


# ---------------------------------------------------------------------------
# Sequence planning — clean_source_title / synth_episode / plan_from_sequence
# (the yt-show layer: pre-assigned season/episode, no filename markers)
# ---------------------------------------------------------------------------

class TestSequencePlanning(unittest.TestCase):
    def test_clean_source_title_strips_show_prefix(self):
        self.assertEqual(
            r.clean_source_title("My Show - Real Title", "My Show"),
            "Real Title")

    def test_clean_source_title_strips_leading_ep_token(self):
        title = r.clean_source_title("Ep 3: The Reveal", "")
        self.assertIsNotNone(title)
        self.assertNotIn("Ep 3", title)

    def test_clean_source_title_excises_embedded_sxxexx(self):
        title = r.clean_source_title("Recap S02E07 stuff", "")
        self.assertIsNotNone(title)
        self.assertNotIn("S02E07", title)

    def test_clean_source_title_drops_hashtags_and_pipes(self):
        title = r.clean_source_title("Title | Channel #shorts", "")
        self.assertIsNotNone(title)
        self.assertNotIn("|", title)
        self.assertNotIn("#", title)

    def test_clean_source_title_empty_after_clean_is_none(self):
        self.assertIsNone(r.clean_source_title("#justtags", ""))

    def test_synth_episode_builds_cleaned_episode(self):
        ep = r.synth_episode("My Show", 1, 2, "My Show - Cool Title")
        self.assertEqual(ep.season, 1)
        self.assertEqual(ep.episode, 2)
        self.assertEqual(ep.title, "Cool Title")

    def test_plan_from_sequence_single_season(self):
        items = [
            ("v1", 1, 1, "First Title", ".mp4"),
            ("v2", 1, 2, "Second Title", ".mp4"),
            ("v3", 1, 3, "Third Title", ".mp4"),
        ]
        p = r.plan_from_sequence(items, "My Show")
        self.assertFalse(p.rejected, p.reason)
        self.assertEqual(len(p.ops), 3)
        for _src, dst in p.ops:
            self.assertTrue(dst.startswith("My Show Season 1/"), dst)
            leaf = dst.split("/")[-1]
            stem = leaf.rsplit(".", 1)[0]
            self.assertIsNotNone(r._C_SXXEXX_RE.search(stem), stem)
        _assert_contract(self, p)

    def test_plan_from_sequence_duplicate_destination_rejects(self):
        items = [
            ("v1", 1, 1, "Same Title", ".mp4"),
            ("v2", 1, 1, "Same Title", ".mp4"),
        ]
        p = r.plan_from_sequence(items, "My Show")
        self.assertTrue(p.rejected)
        self.assertEqual(p.ops, ())

    def test_plan_from_sequence_cross_season(self):
        items = [
            ("v1", 1, 1, "Season One Title", ".mp4"),
            ("v2", 2, 1, "Season Two Title", ".mp4"),
        ]
        p = r.plan_from_sequence(items, "My Show")
        self.assertFalse(p.rejected, p.reason)
        dsts = _dst_map(p)
        self.assertTrue(dsts["v1"].startswith("My Show Season 1/"))
        self.assertTrue(dsts["v2"].startswith("My Show Season 2/"))
        _assert_contract(self, p)

    def test_clean_source_title_preserves_ordinary_words(self):
        # Words in the scene-release noise vocabulary (web/complete/extended/
        # multi) are ordinary in real titles and must NOT truncate the title.
        self.assertEqual(
            r.clean_source_title("Caught in the Web of Lies", ""),
            "Caught in the Web of Lies")
        self.assertEqual(
            r.clean_source_title("The Complete Beginner Guide", ""),
            "The Complete Beginner Guide")

    def test_plan_from_sequence_rejects_range_like_show_name(self):
        # A show name with digit-dash-digit ("9-1-1") makes "9-1-1 Season 1"
        # trip drivecast's range regex; the folder gate must reject the plan
        # rather than upload a folder drivecast would scatter into movies.
        p = r.plan_from_sequence([("v1", 1, 1, "Pilot", ".mp4")], "9-1-1")
        self.assertTrue(p.rejected)
        self.assertEqual(p.ops, ())


if __name__ == "__main__":
    unittest.main(verbosity=2)
