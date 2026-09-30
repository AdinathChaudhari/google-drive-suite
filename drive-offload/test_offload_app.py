#!/usr/bin/env python3
"""Headless tests for offload_app's logic classes. No real Motrix, Google, or
UI: a fake aria2 JSON-RPC server on 127.0.0.1:16800 with a mutable fixture, and
injected ask/upload callbacks. Run with the venv python (but rumps is NOT
imported by importing offload_app — the UI import is lazy inside _run_app):

    .venv/bin/python3 test_offload_app.py
"""
import email.message
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import offload_app as app

HOST = "127.0.0.1"


# --- fake aria2 server -----------------------------------------------------

class Fixture:
    """Mutable in-memory set of download objects, keyed by status bucket.
    Also records every RPC (method, params) the fake server receives."""
    def __init__(self):
        self.downloads = []  # list of dicts with a "status" key
        self.calls = []      # [(method, params), ...]

    def by_status(self, statuses):
        return [d for d in self.downloads if d.get("status") in statuses]

    def find(self, gid):
        return next((d for d in self.downloads if d.get("gid") == gid), None)


FIXTURE = Fixture()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        method = body.get("method")
        params = body.get("params") or []
        FIXTURE.calls.append((method, params))
        error = None
        result = []
        if method == "aria2.getVersion":
            result = {"version": "1.36.0"}
        elif method == "aria2.tellActive":
            result = FIXTURE.by_status(["active"])
        elif method == "aria2.tellWaiting":
            result = FIXTURE.by_status(["waiting", "paused"])
        elif method == "aria2.tellStopped":
            result = FIXTURE.by_status(["complete", "error", "removed"])
        elif method in ("aria2.forcePause", "aria2.unpause"):
            result = params[0] if params else ""
        elif method == "aria2.remove":
            # real aria2 errors when the gid isn't active/waiting/paused
            gid = params[0] if params else ""
            dl = FIXTURE.find(gid)
            if dl is None or dl.get("status") in ("complete", "error",
                                                  "removed"):
                error = {"code": 1, "message": "cannot be removed"}
            else:
                result = gid
        elif method == "aria2.removeDownloadResult":
            result = "OK"
        payload = {"jsonrpc": "2.0", "id": body.get("id")}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        resp = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


class ReuseServer(HTTPServer):
    allow_reuse_address = True


def start_server():
    """Start a fake aria2 server on an OS-chosen free port. Returns (srv,
    port)."""
    srv = ReuseServer((HOST, 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port


# --- tests -----------------------------------------------------------------

class TestEsc(unittest.TestCase):
    def test_esc(self):
        self.assertEqual(app.esc('My "Show" S01'), 'My \\"Show\\" S01')
        self.assertEqual(app.esc("It's Fine"), "It's Fine")
        self.assertEqual(app.esc('back\\slash'), 'back\\\\slash')


class TestQuotaClassifier(unittest.TestCase):
    def test_verbatim_real_log_line(self):
        line = ("2026/07/15 07:32:48 ERROR : Eternals: Failed to copy: "
                "googleapi: Error 403: The user's Drive storage quota has "
                "been exceeded., storageQuotaExceeded")
        self.assertTrue(app.is_quota_failure(line))

    def test_marker_variants_mixed_case(self):
        self.assertTrue(app.is_quota_failure(
            "the storage quota has been exceeded"))
        self.assertTrue(app.is_quota_failure("teamDriveFileLimitExceeded"))
        self.assertTrue(app.is_quota_failure("QUOTAEXCEEDED"))

    def test_generic_failures_are_not_quota(self):
        self.assertFalse(app.is_quota_failure(
            "Failed to copy: connection reset by peer"))
        self.assertFalse(app.is_quota_failure("userRateLimitExceeded"))

    def test_empty_and_none(self):
        self.assertFalse(app.is_quota_failure(""))
        self.assertFalse(app.is_quota_failure(None))


class TestTorrentPath(unittest.TestCase):
    def test_torrent_dir_plus_name(self):
        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, "Cool.Show.S01"))
        dl = {"gid": "g1", "dir": tmp,
              "bittorrent": {"info": {"name": "Cool.Show.S01"}},
              "files": [{"path": os.path.join(tmp, "Cool.Show.S01", "e1.mkv")}]}
        self.assertEqual(app.resolve_local_path(dl),
                         os.path.join(tmp, "Cool.Show.S01"))

    def test_single_file(self):
        dl = {"gid": "g2", "dir": "/x",
              "files": [{"path": "/x/movie.mkv"}]}
        self.assertEqual(app.resolve_local_path(dl), "/x/movie.mkv")

    def test_torrent_name_not_on_disk_falls_back_to_dir_name(self):
        dl = {"gid": "g3", "dir": "/dl",
              "bittorrent": {"info": {"name": "NotThere"}},
              "files": [{"path": "/dl/NotThere/a"},
                        {"path": "/dl/NotThere/b"}]}
        self.assertEqual(app.resolve_local_path(dl), "/dl/NotThere")

    def test_sanitized_dir_name_prefers_on_disk_files(self):
        # Regression: a torrent whose name contains a path-illegal char (':')
        # lands on disk under a sanitized name ('_'). The unsanitized
        # <dir>/<name> never exists, so resolve_local_path must trust the
        # files[] common parent -- returning the non-existent name made
        # tree_size read 0 and payload_ready block the upload forever.
        tmp = tempfile.mkdtemp()
        on_disk = os.path.join(tmp, "Ace Ventura_ Pet Detective (1994) [1080p]")
        os.makedirs(on_disk)
        dl = {"gid": "g4", "dir": tmp,
              "bittorrent": {"info": {
                  "name": "Ace Ventura: Pet Detective (1994) [1080p]"}},
              "files": [{"path": os.path.join(on_disk, "movie.mp4")},
                        {"path": os.path.join(on_disk, "poster.jpg")}]}
        self.assertEqual(app.resolve_local_path(dl), on_disk)


class TestPollSequence(unittest.TestCase):
    def setUp(self):
        FIXTURE.downloads = []
        FIXTURE.calls = []
        self.srv, self.port = start_server()
        self.tmp = tempfile.mkdtemp()
        app.DECISIONS_FILE = os.path.join(self.tmp, "decisions.json")
        self.store = app.DecisionStore(app.DECISIONS_FILE)
        self.store.data = {}
        self.store.save()

    def tearDown(self):
        try:
            self.srv.shutdown()
            self.srv.server_close()
        except Exception:
            pass

    def _payload(self, name, size=100):
        """Create a real file under tmp so the on-disk readiness gate (which
        demands the final-name file exist and be >= totalLength) passes.
        size defaults to 100 to match the fixtures' totalLength."""
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        return p

    def _make_poller(self, choice_for):
        self.asks = []
        self.uploads = []
        self.now = [1000.0]  # controllable clock for backoff assertions

        def ask_cb(gid, name):
            self.asks.append((gid, name))
            return choice_for.get(gid, "local")

        def upload_cb(path, drive, gid, name):
            self.uploads.append((path, drive, gid, name))

        client = app.Aria2Client(port=self.port)
        return app.Poller(client, self.store, ask_cb, upload_cb,
                          now_fn=lambda: self.now[0])

    def test_full_sequence(self):
        p = self._make_poller({"gidA": "drive:TestShow", "gidB": "local"})

        # (1) new active download appears -> ask fires once, persists
        showA = self._payload("showA.mkv")
        FIXTURE.downloads = [{
            "gid": "gidA", "status": "active", "totalLength": "100",
            "completedLength": "10", "downloadSpeed": "5",
            "dir": self.tmp, "files": [{"path": showA}]}]
        p.poll_once()
        self.assertEqual(len(self.asks), 1)
        self.assertTrue(self.store.has("gidA"))
        self.assertEqual(self.store.get("gidA")["choice"], "drive:TestShow")
        # persisted to disk
        with open(app.DECISIONS_FILE) as f:
            disk = json.load(f)
        self.assertIn("gidA", disk)

        # poll again while still active -> no second ask
        p.poll_once()
        self.assertEqual(len(self.asks), 1)

        # (2) turns complete with choice drive:TestShow -> upload once
        FIXTURE.downloads[0]["status"] = "complete"
        FIXTURE.downloads[0]["completedLength"] = "100"
        p.poll_once()
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.uploads[0][0], showA)
        self.assertEqual(self.uploads[0][1], "TestShow")
        # handled flag prevents re-fire
        p.poll_once()
        self.assertEqual(len(self.uploads), 1)

        # (3) a "local" download completes -> upload NOT called
        FIXTURE.downloads.append({
            "gid": "gidB", "status": "active", "totalLength": "50",
            "completedLength": "0", "downloadSpeed": "0",
            "dir": "/dl", "files": [{"path": "/dl/keep.iso"}]})
        p.poll_once()  # asks gidB -> local
        self.assertEqual(self.store.get("gidB")["choice"], "local")
        FIXTURE.downloads[-1]["status"] = "complete"
        before = len(self.uploads)
        p.poll_once()
        self.assertEqual(len(self.uploads), before)  # still no new upload

    def test_first_seen_while_seeding_asks_then_uploads(self):
        # Regression (review finding): a torrent FIRST seen when already
        # fully downloaded and seeding (raw status "active", normalized
        # "complete") must still get its destination ask — and, once the
        # decision is drive-bound, upload. Keying the ask off the normalized
        # status silently skipped such downloads forever.
        p = self._make_poller({"gidF": "drive:Films"})
        first = self._payload("first.mkv")
        FIXTURE.downloads = [{
            "gid": "gidF", "status": "active", "totalLength": "100",
            "completedLength": "100", "downloadSpeed": "0",
            "dir": self.tmp, "files": [{"path": first}]}]
        p.poll_once()
        self.assertEqual(len(self.asks), 1)          # asked on first sight
        self.assertEqual(len(self.uploads), 1)       # and uploaded same tick
        self.assertEqual(self.uploads[0][0], first)
        p.poll_once()
        self.assertEqual(len(self.asks), 1)          # no re-ask
        self.assertEqual(len(self.uploads), 1)       # no re-upload

    def test_seeding_torrent_uploads_without_waiting(self):
        # Regression: an aria2 torrent that finished downloading but is
        # still seeding stays status="active" with completedLength ==
        # totalLength. The upload must fire then, not when seeding ends.
        p = self._make_poller({"gidS": "drive:Films"})
        film = self._payload("film.mkv")
        FIXTURE.downloads = [{
            "gid": "gidS", "status": "active", "totalLength": "100",
            "completedLength": "10", "downloadSpeed": "5",
            "dir": self.tmp, "files": [{"path": film}]}]
        p.poll_once()  # asks while downloading
        self.assertEqual(self.store.get("gidS")["choice"], "drive:Films")

        FIXTURE.downloads[0]["completedLength"] = "100"  # done, seeding
        p.poll_once()
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.uploads[0][0], film)
        p.poll_once()  # handled flag prevents re-fire
        self.assertEqual(len(self.uploads), 1)

    def test_failed_upload_not_orphaned_then_retries(self):
        # Regression: a decision must be consumed only when its upload lands.
        # A transient failure (e.g. a create-drive 403) used to mark_handled
        # before dispatch and permanently orphan the decision.
        p = self._make_poller({"gidR": "drive:Films"})
        self._payload("r.mkv")
        FIXTURE.downloads = [{
            "gid": "gidR", "status": "active", "totalLength": "100",
            "completedLength": "10", "downloadSpeed": "5",
            "dir": self.tmp, "files": [{"path": os.path.join(self.tmp,
                                                             "r.mkv")}]}]
        p.poll_once()                        # ask -> drive:Films
        FIXTURE.downloads[0]["status"] = "complete"
        FIXTURE.downloads[0]["completedLength"] = "100"
        p.poll_once()                        # dispatch upload once
        self.assertEqual(len(self.uploads), 1)
        # NOT marked handled at dispatch...
        self.assertFalse(self.store.get("gidR")["handled"])
        # ...and the in-flight guard stops a re-dispatch on the next tick
        p.poll_once()
        self.assertEqual(len(self.uploads), 1)
        # upload fails -> decision stays pending and leaves the in-flight set
        p.upload_done("gidR", success=False)
        self.assertFalse(self.store.get("gidR")["handled"])
        # ...but a retry is now backed off: an immediate poll must NOT
        # re-dispatch (this is what stops per-tick notification spam).
        p.poll_once()
        self.assertEqual(len(self.uploads), 1)
        # once the backoff window elapses, the next poll retries it
        self.now[0] += app.UPLOAD_BACKOFF_BASE + 1
        p.poll_once()
        self.assertEqual(len(self.uploads), 2)
        # this attempt succeeds -> now handled, never dispatched again
        p.upload_done("gidR", success=True)
        self.assertTrue(self.store.get("gidR")["handled"])
        self.now[0] += app.UPLOAD_BACKOFF_CAP  # well past any backoff
        p.poll_once()
        self.assertEqual(len(self.uploads), 2)

    def test_persistent_failure_gives_up_after_cap(self):
        # Regression (adversarial review): a permanently failing upload
        # (revoked token, deleted remote, create-drive 403) must NOT be
        # re-dispatched on every POLL_SECONDS tick forever. After
        # UPLOAD_MAX_ATTEMPTS the gid moves to a terminal failed state and is
        # never dispatched again — no more rclone spawns or 'Upload failed'
        # notifications.
        p = self._make_poller({"gidP": "drive:Films"})
        # raw "active" but fully downloaded (seeding) so it's both asked and
        # normalized to complete in one tick.
        self._payload("p.mkv")
        FIXTURE.downloads = [{
            "gid": "gidP", "status": "active", "totalLength": "100",
            "completedLength": "100", "downloadSpeed": "0",
            "dir": self.tmp, "files": [{"path": os.path.join(self.tmp,
                                                             "p.mkv")}]}]
        # first sight while complete: ask + dispatch attempt 1
        p.poll_once()
        self.assertEqual(len(self.uploads), 1)
        # drive every attempt straight to failure, stepping past each backoff
        for _ in range(app.UPLOAD_MAX_ATTEMPTS - 1):
            p.upload_done("gidP", success=False)
            self.now[0] += app.UPLOAD_BACKOFF_CAP  # skip whatever backoff
            p.poll_once()
        # exactly UPLOAD_MAX_ATTEMPTS dispatches, no more
        self.assertEqual(len(self.uploads), app.UPLOAD_MAX_ATTEMPTS)
        # the last failure trips the cap -> terminal failed, decision unhandled
        p.upload_done("gidP", success=False)
        rec = self.store.get("gidP")
        self.assertTrue(rec["failed"])
        self.assertFalse(rec["handled"])
        # further polls, even far in the future, never re-dispatch
        self.now[0] += 10 * app.UPLOAD_BACKOFF_CAP
        p.poll_once()
        p.poll_once()
        self.assertEqual(len(self.uploads), app.UPLOAD_MAX_ATTEMPTS)

    def test_terminal_failed_cleared_on_restart(self):
        # Regression (adversarial review): a gid stamped failed=True on disk
        # (all attempts burned during, e.g., an overnight outage) must NOT be
        # stuck forever. A fresh DecisionStore (the next app run) clears the
        # retry bookkeeping for not-yet-handled records, so the completed
        # download dispatches again instead of needing a hand-edit.
        self.now = [1000.0]
        film = self._payload("restart.mkv")
        self.store.record("gidX", "restart.mkv", "drive:Films", handled=False)
        self.store.data["gidX"]["failed"] = True
        self.store.data["gidX"]["failures"] = app.UPLOAD_MAX_ATTEMPTS
        self.store.data["gidX"]["next_attempt"] = 0
        self.store.save()
        # a brand-new store instance reads that file (the restart) and resets
        store2 = app.DecisionStore(app.DECISIONS_FILE)
        self.assertFalse(store2.get("gidX").get("failed"))
        self.assertNotIn("failures", store2.get("gidX"))
        uploads = []
        p2 = app.Poller(app.Aria2Client(port=self.port), store2,
                        ask_cb=lambda g, n: "drive:Films",
                        upload_cb=lambda *a: uploads.append(a),
                        now_fn=lambda: self.now[0])
        FIXTURE.downloads = [{
            "gid": "gidX", "status": "complete", "totalLength": "100",
            "completedLength": "100", "downloadSpeed": "0",
            "dir": self.tmp, "files": [{"path": film}]}]
        p2.poll_once()
        self.assertEqual(len(uploads), 1)
        # a HANDLED record is left alone by the reset (already uploaded)
        self.store.record("gidH", "done.mkv", "drive:Films", handled=True)
        self.store.save()
        store3 = app.DecisionStore(app.DECISIONS_FILE)
        self.assertTrue(store3.get("gidH")["handled"])

    def test_give_up_notifies_once(self):
        # Regression (adversarial review): the terminal give-up must fire
        # exactly one notification so a silently-abandoned upload is visible.
        notes = []
        p = self._make_poller({"gidN": "drive:Films"})
        p.notify_cb = lambda *a: notes.append(a)
        self._payload("n.mkv")
        FIXTURE.downloads = [{
            "gid": "gidN", "status": "active", "totalLength": "100",
            "completedLength": "100", "downloadSpeed": "0",
            "dir": self.tmp, "files": [{"path": os.path.join(self.tmp,
                                                             "n.mkv")}]}]
        p.poll_once()                       # ask + dispatch attempt 1
        for _ in range(app.UPLOAD_MAX_ATTEMPTS - 1):
            p.upload_done("gidN", success=False)
            self.now[0] += app.UPLOAD_BACKOFF_CAP
            p.poll_once()
        give_ups = [n for n in notes if n[0] == "Upload gave up"]
        self.assertEqual(len(give_ups), 0)  # not yet given up
        p.upload_done("gidN", success=False)  # trips the cap
        give_ups = [n for n in notes if n[0] == "Upload gave up"]
        self.assertEqual(len(give_ups), 1)
        self.assertEqual(give_ups[0][1], "n.mkv")

    def test_reupload_candidates_filter(self):
        # Offered: kept-local, given-up (failed) drive uploads, and any
        # NOT-handled record with a recorded failure (mid-backoff). NOT
        # offered: already-uploaded, "already:" (found on a drive),
        # still-pending-with-zero-failures, and a handled record that still
        # carries failures>0 (a legacy pre-cleanup record — the regression
        # guard for the handled-records bug).
        self.store.data = {
            "g_local": {"name": "Kept", "choice": "local", "handled": True},
            "g_drive_done": {"name": "Done", "choice": "drive:Films",
                             "handled": True},
            "g_already": {"name": "Found", "choice": "already:Films",
                          "handled": True},
            "g_failed": {"name": "Failed", "choice": "drive:Films",
                         "handled": False, "failed": True},
            "g_pending": {"name": "Pending", "choice": "drive:Films",
                          "handled": False},
            "g_retrying": {"name": "Retrying", "choice": "drive:Films",
                           "handled": False, "failures": 1},
            "g_recovered": {"name": "Recovered", "choice": "drive:Films",
                            "handled": True, "failures": 1},
        }
        cands = app.reupload_candidates(self.store.data)
        self.assertEqual({g for g, _ in cands},
                         {"g_local", "g_failed", "g_retrying"})

    # ---- forget history (privacy) ----

    def test_forget_scrubs_name_but_keeps_the_record(self):
        # forget() overwrites the display name and forces the record
        # terminal-clean, but the gid MUST survive: the record is the only
        # thing keeping poll_once quiet about a torrent still in the engine.
        self.store.record("gidP", "personal name", "drive:Films",
                          handled=False)
        self.store.data["gidP"]["failures"] = 2
        self.store.data["gidP"]["next_attempt"] = 9999
        self.assertTrue(self.store.forget("gidP"))
        rec = self.store.get("gidP")
        self.assertEqual(rec["name"], app.FORGOTTEN_NAME)
        self.assertTrue(rec["handled"])
        self.assertTrue(rec["forgotten"])
        self.assertNotIn("failures", rec)
        self.assertNotIn("next_attempt", rec)
        # persisted, and the scrubbed name is nowhere in the file
        self.assertNotIn("personal name",
                         open(app.DECISIONS_FILE, encoding="utf-8").read())
        # a fresh store (next app run) sees the same thing
        store2 = app.DecisionStore(app.DECISIONS_FILE)
        self.assertEqual(store2.get("gidP")["name"], app.FORGOTTEN_NAME)
        self.assertFalse(self.store.forget("nosuchgid"))

    def test_forgotten_records_leave_both_menus(self):
        # Every shape reupload_candidates offers must stop being offered once
        # forgotten -- forget keeps `choice`, so the "local" clause would
        # otherwise put a "(forgotten)" row straight back in the menu.
        self.store.data = {
            "g_local": {"name": "Kept", "choice": "local", "handled": True},
            "g_failed": {"name": "Failed", "choice": "drive:Films",
                         "handled": False, "failed": True},
            "g_retrying": {"name": "Retrying", "choice": "drive:Films",
                           "handled": False, "failures": 1},
        }
        self.store.save()
        self.assertEqual(len(app.reupload_candidates(self.store.data)), 3)
        for gid in ("g_local", "g_failed", "g_retrying"):
            self.store.forget(gid)
        self.assertEqual(app.reupload_candidates(self.store.data), [])
        self.assertEqual(app.rememberable_items(self.store.data), [])

    def test_rememberable_items_covers_uploaded_and_skips_in_flight(self):
        # The forget menu offers uploaded/pending names too (the point is the
        # NAME, not the payload), sorted case-insensitively, minus anything
        # currently uploading.
        self.store.data = {
            "g1": {"name": "beta", "choice": "drive:Films", "handled": True},
            "g2": {"name": "Alpha", "choice": "local", "handled": True},
            "g3": {"name": "gamma", "choice": "drive:Films",
                   "handled": False},
        }
        self.assertEqual(app.rememberable_items(self.store.data),
                         [("g2", "Alpha"), ("g1", "beta"), ("g3", "gamma")])
        self.assertEqual(
            [g for g, _ in app.rememberable_items(self.store.data,
                                                  exclude={"g3"})],
            ["g2", "g1"])

    def test_forget_does_not_re_ask_a_torrent_still_in_the_engine(self):
        # THE regression this design exists for: deleting the record instead
        # of scrubbing it would make the ask fire again on the next tick --
        # putting the just-forgotten name back on screen -- for any torrent
        # still seeding in the engine.
        p = self._make_poller({"gidK": "local"})
        payload = self._payload("keep.mkv")
        FIXTURE.downloads = [{
            "gid": "gidK", "status": "active", "totalLength": "100",
            "completedLength": "100", "downloadSpeed": "0",
            "dir": self.tmp, "files": [{"path": payload}]}]
        p.poll_once()
        self.assertEqual(len(self.asks), 1)
        self.store.forget("gidK")
        p.poll_once()                       # torrent still seeding
        p.poll_once()
        self.assertEqual(len(self.asks), 1)         # never re-asked
        self.assertEqual(len(self.uploads), 0)      # and never dispatched
        self.assertEqual(self.store.get("gidK")["name"], app.FORGOTTEN_NAME)

    def test_forget_all_is_idempotent_and_counts_only_new_scrubs(self):
        self.store.data = {
            "g1": {"name": "one", "choice": "local", "handled": True},
            "g2": {"name": "two", "choice": "drive:Films", "handled": True},
            "g3": {"name": app.FORGOTTEN_NAME, "choice": "local",
                   "handled": True, "forgotten": True},
        }
        self.store.save()
        self.assertEqual(self.store.forget_all(), 2)   # g3 already scrubbed
        self.assertEqual(self.store.forget_all(), 0)
        self.assertTrue(all(r["name"] == app.FORGOTTEN_NAME
                            for r in self.store.data.values()))
        # explicit subset form
        self.store.record("g4", "four", "local", handled=True)
        self.store.record("g5", "five", "local", handled=True)
        self.assertEqual(self.store.forget_all(["g4", "nosuchgid"]), 1)
        self.assertEqual(self.store.get("g5")["name"], "five")

    def test_same_name_under_two_gids_is_one_row_that_forgets_both(self):
        # rumps keys a menu row by title, so two records sharing a name would
        # collapse to one row and leave the twin on disk -- a name the user
        # believes they forgot. One row, both gids.
        self.store.data = {
            "g_meta": {"name": "same name", "choice": "local",
                       "handled": True},
            "g_real": {"name": "same name", "choice": "drive:Films",
                       "handled": True},
            "g_other": {"name": "other", "choice": "local", "handled": True},
        }
        self.store.save()
        rows = app.group_by_name(app.rememberable_items(self.store.data))
        self.assertEqual([n for n, _ in rows], ["other", "same name"])
        gids = dict(rows)["same name"]
        self.assertEqual(sorted(gids), ["g_meta", "g_real"])
        self.assertEqual(self.store.forget_all(gids), 2)
        self.assertEqual(
            [n for n, _ in
             app.group_by_name(app.rememberable_items(self.store.data))],
            ["other"])

    def test_forgotten_name_is_invisible_to_the_route_miner(self):
        # FORGOTTEN_NAME must carry no season/episode marker, or the Tier-2
        # decisions miner would index every forgotten record under one bogus
        # show and route real episodes off it.
        self.assertIsNone(app.renamer.derive_show_key(app.FORGOTTEN_NAME))

    def test_local_then_requeued_uploads_only_after_success(self):
        # A download kept local (the Spider-Man bug) can later be redirected
        # to a drive: requeue re-arms it, poll dispatches the upload, and it
        # is marked handled ONLY after the upload actually lands.
        p = self._make_poller({"gidL": "local"})
        payload = self._payload("keep.mkv")
        FIXTURE.downloads = [{
            "gid": "gidL", "status": "active", "totalLength": "100",
            "completedLength": "100", "downloadSpeed": "0",
            "dir": self.tmp, "files": [{"path": payload}]}]
        p.poll_once()  # ask -> local; complete same tick -> KEEP local
        self.assertEqual(self.store.get("gidL")["choice"], "local")
        self.assertTrue(self.store.get("gidL")["handled"])
        self.assertEqual(len(self.uploads), 0)
        # user picks it from the Re-upload menu -> a drive
        self.assertTrue(p.requeue_for_upload("gidL", "drive:Films"))
        rec = self.store.get("gidL")
        self.assertEqual(rec["choice"], "drive:Films")
        self.assertFalse(rec["handled"])
        # next poll dispatches the upload (download still complete on disk)
        p.poll_once()
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.uploads[0][1], "Films")
        # NOT marked handled until the upload actually lands
        self.assertFalse(self.store.get("gidL")["handled"])
        p.upload_done("gidL", success=True)
        self.assertTrue(self.store.get("gidL")["handled"])
        p.poll_once()  # handled -> no re-dispatch
        self.assertEqual(len(self.uploads), 1)

    def test_requeue_clears_failed_backoff(self):
        # A drive upload that gave up (terminal failed) is offered again;
        # requeue clears the failed/backoff bookkeeping and re-arms it.
        self.store.record("gidF", "f.mkv", "drive:Films", handled=False)
        self.store.data["gidF"]["failed"] = True
        self.store.data["gidF"]["failures"] = app.UPLOAD_MAX_ATTEMPTS
        self.store.data["gidF"]["next_attempt"] = 9e9
        self.store.save()
        p = self._make_poller({})
        self.assertTrue(p.requeue_for_upload("gidF", "drive:Films"))
        rec = self.store.get("gidF")
        self.assertNotIn("failed", rec)
        self.assertNotIn("failures", rec)
        self.assertNotIn("next_attempt", rec)
        self.assertFalse(rec["handled"])

    def _dispatch_one(self, gid, drive):
        """Ask + dispatch a single complete download bound for `drive`."""
        self._payload("%s.mkv" % gid)
        FIXTURE.downloads = [{
            "gid": gid, "status": "active", "totalLength": "100",
            "completedLength": "100", "downloadSpeed": "0",
            "dir": self.tmp, "files": [{"path": os.path.join(
                self.tmp, "%s.mkv" % gid)}]}]

    def test_quota_failure_terminal_immediately(self):
        # A quota/drive-full failure short-circuits to terminal failed on the
        # FIRST failure — it never retries the same full drive.
        p = self._make_poller({"gidQ": "drive:Full"})
        self._dispatch_one("gidQ", "Full")
        p.poll_once()                        # ask + dispatch attempt 1
        self.assertEqual(len(self.uploads), 1)
        p.upload_done("gidQ", success=False, quota=True)
        rec = self.store.get("gidQ")
        self.assertTrue(rec["failed"])
        self.assertFalse(rec["handled"])
        self.assertEqual(rec["failures"], 1)
        # even far past any backoff window, no retry against the full drive
        self.now[0] += 2 * app.UPLOAD_BACKOFF_CAP
        p.poll_once()
        p.poll_once()
        self.assertEqual(len(self.uploads), 1)

    def test_quota_failure_is_immediate_candidate(self):
        # After a single quota failure the item is a re-upload candidate
        # without burning UPLOAD_MAX_ATTEMPTS.
        p = self._make_poller({"gidQ": "drive:Full"})
        self._dispatch_one("gidQ", "Full")
        p.poll_once()
        p.upload_done("gidQ", success=False, quota=True)
        cands = {g for g, _ in app.reupload_candidates(self.store.data)}
        self.assertIn("gidQ", cands)

    def test_quota_then_requeue_dispatches_new_drive(self):
        # Quota-fail, then a re-pick to another drive: requeue clears the
        # terminal bookkeeping and the next poll dispatches to the new drive.
        p = self._make_poller({"gidQ": "drive:Full"})
        self._dispatch_one("gidQ", "Full")
        p.poll_once()
        p.upload_done("gidQ", success=False, quota=True)
        self.assertTrue(p.requeue_for_upload("gidQ", "drive:Other"))
        rec = self.store.get("gidQ")
        self.assertNotIn("failed", rec)
        self.assertNotIn("failures", rec)
        self.assertNotIn("next_attempt", rec)
        p.poll_once()
        self.assertEqual(len(self.uploads), 2)
        self.assertEqual(self.uploads[-1][1], "Other")
        p.upload_done("gidQ", success=True)
        self.assertTrue(self.store.get("gidQ")["handled"])

    def test_generic_single_failure_is_candidate(self):
        # A plain (non-quota) failure surfaces the item immediately AND keeps
        # the existing backoff retry semantics untouched.
        p = self._make_poller({"gidG": "drive:Films"})
        self._dispatch_one("gidG", "Films")
        p.poll_once()
        self.assertEqual(len(self.uploads), 1)
        p.upload_done("gidG", success=False)
        rec = self.store.get("gidG")
        self.assertFalse(rec.get("failed"))      # not terminal yet
        self.assertFalse(rec["handled"])
        self.assertIn("gidG",
                      {g for g, _ in app.reupload_candidates(self.store.data)})
        # backoff still holds: an immediate poll must NOT re-dispatch
        p.poll_once()
        self.assertEqual(len(self.uploads), 1)
        # after the backoff window, the retry fires (semantics unchanged)
        self.now[0] += app.UPLOAD_BACKOFF_BASE + 1
        p.poll_once()
        self.assertEqual(len(self.uploads), 2)

    def test_failure_then_success_clears_bookkeeping_and_menu(self):
        # A fail-once-then-succeed record must end clean: handled=True with NO
        # failures/failed/next_attempt, and out of the Re-upload menu.
        p = self._make_poller({"gidT": "drive:Films"})
        self._dispatch_one("gidT", "Films")
        p.poll_once()
        p.upload_done("gidT", success=False)     # transient failure
        self.now[0] += app.UPLOAD_BACKOFF_BASE + 1
        p.poll_once()                            # backoff elapsed -> re-dispatch
        self.assertEqual(len(self.uploads), 2)
        p.upload_done("gidT", success=True)      # the retry lands
        rec = self.store.get("gidT")
        self.assertTrue(rec["handled"])
        self.assertNotIn("failures", rec)
        self.assertNotIn("failed", rec)
        self.assertNotIn("next_attempt", rec)
        self.assertNotIn("gidT",
                         {g for g, _ in
                          app.reupload_candidates(self.store.data)})

    def test_mark_failed(self):
        # mark_failed stamps the terminal state on a known gid and no-ops on an
        # unknown one; a restart re-arm (fresh DecisionStore) clears it.
        self.store.record("gidM", "m.mkv", "drive:Films", handled=False)
        self.assertTrue(self.store.mark_failed("gidM"))
        rec = self.store.get("gidM")
        self.assertTrue(rec["failed"])
        self.assertEqual(rec["next_attempt"], 0)
        self.assertEqual(rec["failures"], 1)
        self.assertFalse(self.store.mark_failed("nope"))
        self.assertIsNone(self.store.get("nope"))
        store2 = app.DecisionStore(app.DECISIONS_FILE)
        self.assertFalse(store2.get("gidM").get("failed"))
        self.assertNotIn("failures", store2.get("gidM"))
        self.assertNotIn("next_attempt", store2.get("gidM"))

    def test_mark_handled_clears_bookkeeping(self):
        # mark_handled must drop stale failure bookkeeping so a
        # fail-once-then-succeed record doesn't linger in the menu forever.
        self.store.record("gidH", "h.mkv", "drive:Films", handled=False)
        self.store.record_failure("gidH", 1000.0,
                                  app.UPLOAD_MAX_ATTEMPTS,
                                  app.UPLOAD_BACKOFF_BASE,
                                  app.UPLOAD_BACKOFF_CAP)
        self.store.mark_handled("gidH")
        rec = self.store.get("gidH")
        self.assertTrue(rec["handled"])
        for k in ("failures", "failed", "next_attempt"):
            self.assertNotIn(k, rec)
        # persisted the same way after a reload
        store2 = app.DecisionStore(app.DECISIONS_FILE)
        rec2 = store2.get("gidH")
        self.assertTrue(rec2["handled"])
        for k in ("failures", "failed", "next_attempt"):
            self.assertNotIn(k, rec2)

    def test_aria2_down_returns_clean(self):
        p = self._make_poller({})
        self.srv.shutdown()  # stop fake server
        time.sleep(0.2)
        status = p.poll_once()  # must not raise
        self.assertFalse(status["connected"])


# --- Transmission adaptation ------------------------------------------------

def _torrent(**kw):
    """A Transmission torrent-get object with sensible defaults, overridable."""
    t = {"id": 1, "hashString": "abc123", "name": "Cool.Show.S01",
         "status": 4, "percentDone": 0.5, "leftUntilDone": 50,
         "totalSize": 100, "rateDownload": 7, "downloadDir": "/dl",
         "files": [{"name": "Cool.Show.S01/e1.mkv"},
                   {"name": "Cool.Show.S01/e2.mkv"}],
         "isFinished": False}
    t.update(kw)
    return t


class TestTransmissionAdapt(unittest.TestCase):
    def test_shape_and_fields(self):
        dl = app.TransmissionClient._adapt(_torrent())
        # gid is namespaced so it can't collide with an aria2 gid
        self.assertEqual(dl["gid"], "tm-abc123")
        self.assertTrue(dl["gid"].startswith(app.TM_PREFIX))
        self.assertEqual(dl["dir"], "/dl")
        self.assertEqual(dl["bittorrent"]["info"]["name"], "Cool.Show.S01")
        self.assertEqual(dl["totalLength"], 100)
        self.assertEqual(dl["completedLength"], 50)   # total - leftUntilDone
        self.assertEqual(dl["downloadSpeed"], 7)
        # file paths are joined onto the downloadDir
        self.assertEqual([f["path"] for f in dl["files"]],
                         ["/dl/Cool.Show.S01/e1.mkv",
                          "/dl/Cool.Show.S01/e2.mkv"])

    def test_size_when_done_and_fileStats(self):
        # sizeWhenDone (selected bytes) drives totalLength/completedLength so
        # the readiness gate's size check is partial-selection safe; totalSize
        # (which includes deselected files) is only the fallback.
        dl = app.TransmissionClient._adapt(
            _torrent(sizeWhenDone=80, leftUntilDone=0))
        self.assertEqual(dl["totalLength"], 80)
        self.assertEqual(dl["completedLength"], 80)   # sizeWhenDone - left
        # fileStats.wanted propagates to per-file "selected"
        dl = app.TransmissionClient._adapt(
            _torrent(fileStats=[{"wanted": True}, {"wanted": False}]))
        self.assertEqual([f["selected"] for f in dl["files"]],
                         ["true", "false"])
        # no sizeWhenDone -> totalSize fallback keeps the default at 100
        self.assertEqual(
            app.TransmissionClient._adapt(_torrent())["totalLength"], 100)

    def test_adapt_feeds_shared_consumers(self):
        # the adapted dict flows through the same helpers aria2 dicts do
        dl = app.TransmissionClient._adapt(_torrent())
        self.assertEqual(app.download_name(dl), "Cool.Show.S01")
        pct, speed = app.download_progress(dl)
        self.assertAlmostEqual(pct, 50.0)
        self.assertEqual(speed, 7)

    def test_status_mapping_table(self):
        cases = [
            # percentDone complete -> complete even while seeding (status 6)
            (dict(percentDone=1.0, leftUntilDone=0, status=6), "complete"),
            # leftUntilDone==0 & size>0 -> complete even if stopped (status 0)
            (dict(percentDone=0.99, leftUntilDone=0, totalSize=100, status=0),
             "complete"),
            # queued to seed after finishing -> complete
            (dict(percentDone=1.0, leftUntilDone=0, status=5), "complete"),
            # downloading -> active
            (dict(percentDone=0.5, leftUntilDone=50, status=4), "active"),
            # queued to download -> active
            (dict(percentDone=0.0, leftUntilDone=100, status=3), "active"),
            # stopped while still incomplete -> paused
            (dict(percentDone=0.3, leftUntilDone=70, status=0), "paused"),
            # queued to verify / verifying -> waiting
            (dict(percentDone=0.0, leftUntilDone=100, status=1), "waiting"),
            (dict(percentDone=0.9, leftUntilDone=10, status=2), "waiting"),
            # 2026-07-06 incident: a VERIFYING torrent that transiently reports
            # left==0 & pct==1.0 must still read "waiting", never "complete" --
            # pieces aren't trusted and the .part rename hasn't happened yet.
            (dict(percentDone=1.0, leftUntilDone=0, totalSize=100, status=2),
             "waiting"),
            # queued-to-verify with the same transient completeness -> waiting
            (dict(percentDone=1.0, leftUntilDone=0, totalSize=100, status=1),
             "waiting"),
        ]
        for over, expected in cases:
            got = app._tm_status(_torrent(**over))
            self.assertEqual(got, expected, "%s -> %s (got %s)" %
                             (over, expected, got))


class TestTransmissionHandshake(unittest.TestCase):
    def test_409_session_id_handshake(self):
        c = app.TransmissionClient(port=9091)

        # First urlopen raises 409 carrying the session id header; the client
        # must store it and retry, the retry then succeeding.
        hdrs = email.message.Message()
        hdrs["X-Transmission-Session-Id"] = "SESSION-XYZ"
        err = urllib.error.HTTPError(c.url, 409, "Conflict", hdrs, None)

        ok_body = json.dumps(
            {"result": "success", "arguments": {"version": "4.0"}}
        ).encode("utf-8")
        ok_resp = mock.MagicMock()
        ok_resp.read.return_value = ok_body
        ok_resp.__enter__.return_value = ok_resp
        ok_resp.__exit__.return_value = False

        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            if len(calls) == 1:
                raise err
            return ok_resp

        with mock.patch("offload_app.urllib.request.urlopen", fake_urlopen):
            self.assertTrue(c.is_up())

        # exactly two attempts, and the retry carried the session id
        self.assertEqual(len(calls), 2)
        self.assertEqual(c._session_id, "SESSION-XYZ")
        self.assertEqual(
            calls[1].get_header("X-transmission-session-id"), "SESSION-XYZ")

    def test_basic_auth_header(self):
        c = app.TransmissionClient(port=9091, user="me", password="secret")
        h = c._headers()
        self.assertTrue(h["Authorization"].startswith("Basic "))
        import base64
        decoded = base64.b64decode(h["Authorization"].split()[1]).decode()
        self.assertEqual(decoded, "me:secret")


# --- MultiClient ------------------------------------------------------------

class FakeEngine:
    """Minimal is_up()/tell_all() engine for MultiClient tests."""
    def __init__(self, up, downloads=None, raise_on_tell=False):
        self._up = up
        self._downloads = downloads or []
        self._raise = raise_on_tell

    def is_up(self):
        return self._up

    def tell_all(self):
        if self._raise:
            raise RuntimeError("boom")
        return list(self._downloads)


class TestMultiClient(unittest.TestCase):
    def test_merge_with_one_engine_down(self):
        up_engine = FakeEngine(True, [{"gid": "g1"}, {"gid": "g2"}])
        down_engine = FakeEngine(False, [{"gid": "should-not-appear"}])
        mc = app.MultiClient([("Motrix", up_engine),
                              ("Transmission", down_engine)])
        self.assertTrue(mc.is_up())          # any up
        gids = [d["gid"] for d in mc.tell_all()]
        self.assertEqual(gids, ["g1", "g2"])
        self.assertEqual(mc.engine_status(),
                         [("Motrix", True), ("Transmission", False)])

    def test_all_down(self):
        mc = app.MultiClient([("Motrix", FakeEngine(False)),
                              ("Transmission", FakeEngine(False))])
        self.assertFalse(mc.is_up())
        self.assertEqual(mc.tell_all(), [])

    def test_raising_engine_is_skipped_not_fatal(self):
        good = FakeEngine(True, [{"gid": "ok"}])
        bad = FakeEngine(True, raise_on_tell=True)
        mc = app.MultiClient([("Motrix", good), ("Transmission", bad)])
        self.assertEqual([d["gid"] for d in mc.tell_all()], ["ok"])

    def test_for_gid_routing(self):
        aria2 = app.Aria2Client(port=1)
        tm = app.TransmissionClient(port=2)
        mc = app.MultiClient([("Motrix", aria2), ("Transmission", tm)])
        self.assertIs(mc.for_gid("tm-abc"), tm)
        self.assertIs(mc.for_gid("aria2gid1234"), aria2)


# --- upload lifecycle -------------------------------------------------------

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


class TestUploadLifecycle(unittest.TestCase):
    def _run(self, gid, rc):
        calls = []
        client = RecordingClient(RecordingEngine(calls))

        def fake_up(path, drive, bwlimit="", progress_cb=None):
            calls.append(("upload", path, drive))
            return rc, "output"

        got_rc, out = app.perform_upload(
            client, "/p/file", "MyDrive", gid, todrive_up=fake_up)
        return calls, got_rc

    def test_torrent_success_stop_upload_remove_in_order(self):
        calls, rc = self._run("tm-hash", 0)
        self.assertEqual(rc, 0)
        self.assertEqual([c[0] for c in calls], ["stop", "upload", "remove"])
        self.assertEqual(calls[0], ("stop", "tm-hash"))
        self.assertEqual(calls[2], ("remove", "tm-hash"))

    def test_torrent_failure_resumes_and_does_not_remove(self):
        # A failed upload must RESUME (start) the torrent it paused, and never
        # remove it (data is intact — todrive only deletes on success).
        calls, rc = self._run("tm-hash", 1)
        self.assertEqual(rc, 1)
        self.assertEqual([c[0] for c in calls], ["stop", "upload", "start"])
        self.assertEqual(calls[2], ("start", "tm-hash"))
        self.assertNotIn("remove", [c[0] for c in calls])

    def test_aria2_gid_gets_same_lifecycle(self):
        # aria2 torrents seed after completion just like Transmission ones,
        # so the stop -> upload -> remove lifecycle applies to them too
        # (the engine methods are best-effort no-ops for plain downloads).
        calls, rc = self._run("plain-aria2-gid", 0)
        self.assertEqual([c[0] for c in calls], ["stop", "upload", "remove"])
        calls, rc = self._run("plain-aria2-gid", 1)
        self.assertEqual([c[0] for c in calls], ["stop", "upload", "start"])


# --- status normalization -----------------------------------------------

class TestEffectiveStatus(unittest.TestCase):
    def test_seeding_torrent_reads_complete(self):
        # aria2 reports a fully-downloaded torrent as "active" while it
        # seeds; the poller must see "complete" or the upload never fires.
        dl = {"status": "active", "totalLength": "100",
              "completedLength": "100"}
        self.assertEqual(app.effective_status(dl), "complete")

    def test_promotion_whitelist_table(self):
        # fully downloaded: active/waiting/paused promote to complete,
        # terminal statuses pass through untouched
        for status, want in [("active", "complete"), ("waiting", "complete"),
                             ("paused", "complete"), ("complete", "complete"),
                             ("error", "error"), ("removed", "removed")]:
            dl = {"status": status, "totalLength": "100",
                  "completedLength": "100"}
            self.assertEqual(app.effective_status(dl), want, status)

    def test_partial_download_stays_active(self):
        dl = {"status": "active", "totalLength": "100",
              "completedLength": "42"}
        self.assertEqual(app.effective_status(dl), "active")

    def test_zero_total_untouched(self):
        # metadata/magnet downloads have no totalLength yet
        dl = {"status": "active", "totalLength": "0", "completedLength": "0"}
        self.assertEqual(app.effective_status(dl), "active")

    def test_complete_passes_through(self):
        dl = {"status": "complete", "totalLength": "100",
              "completedLength": "100"}
        self.assertEqual(app.effective_status(dl), "complete")

    def test_garbage_lengths_fall_back_to_raw_status(self):
        dl = {"status": "active", "totalLength": "n/a"}
        self.assertEqual(app.effective_status(dl), "active")

    def test_transmission_verifying_never_repromoted(self):
        # Regression (2026-07-06 incident, adversarial review): a Transmission
        # torrent in status 2 (verifying) that transiently reports
        # leftUntilDone == 0 adapts to completedLength >= totalLength.
        # _tm_status correctly maps it to "waiting"; effective_status must NOT
        # re-promote the tm- gid back to "complete" (that undid the fix and
        # handed the poller a live, not-yet-renamed ".part" payload).
        dl = app.TransmissionClient._adapt(
            _torrent(status=2, leftUntilDone=0, sizeWhenDone=100))
        self.assertEqual(dl["completedLength"], dl["totalLength"])
        self.assertEqual(app.effective_status(dl), "waiting")
        # a genuinely finished torrent (seeding, status 6) still reads complete
        done = app.TransmissionClient._adapt(
            _torrent(status=6, percentDone=1.0, leftUntilDone=0,
                     sizeWhenDone=100))
        self.assertEqual(app.effective_status(done), "complete")

    def test_aria2_seeding_still_promoted(self):
        # The aria2 seeding gotcha the function exists for is unchanged: a
        # non-tm gid that is fully downloaded still normalizes to complete.
        dl = {"gid": "aria2xyz", "status": "active", "totalLength": "100",
              "completedLength": "100"}
        self.assertEqual(app.effective_status(dl), "complete")


# --- aria2 torrent lifecycle (wire level) ---------------------------------

class TestAria2Lifecycle(unittest.TestCase):
    """Pin the JSON-RPC methods and param shape the new Aria2Client
    lifecycle hooks put on the wire, against the fake server."""

    def setUp(self):
        FIXTURE.downloads = []
        FIXTURE.calls = []
        self.srv, self.port = start_server()
        self.client = app.Aria2Client(port=self.port)

    def tearDown(self):
        try:
            self.srv.shutdown()
            self.srv.server_close()
        except Exception:
            pass

    def _rpc_calls(self):
        return [(m, p) for m, p in FIXTURE.calls
                if m != "aria2.getVersion"]

    def test_stop_and_start(self):
        self.client.stop_torrent("g1")
        self.client.start_torrent("g1")
        self.assertEqual(self._rpc_calls(),
                         [("aria2.forcePause", ["g1"]),
                          ("aria2.unpause", ["g1"])])

    def test_remove_paused_download(self):
        FIXTURE.downloads = [{"gid": "g1", "status": "paused"}]
        self.client.remove_torrent("g1")
        self.assertEqual(self._rpc_calls(),
                         [("aria2.remove", ["g1"]),
                          ("aria2.removeDownloadResult", ["g1"])])

    def test_remove_still_clears_result_when_remove_errors(self):
        # aria2.remove errors for an already-stopped gid; the second step
        # (removeDownloadResult) must still run so the entry disappears.
        FIXTURE.downloads = [{"gid": "g1", "status": "complete"}]
        self.client.remove_torrent("g1")
        self.assertEqual(self._rpc_calls(),
                         [("aria2.remove", ["g1"]),
                          ("aria2.removeDownloadResult", ["g1"])])


# --- run_todrive_up streaming ----------------------------------------------

FAKE_TODRIVE = r'''#!/usr/bin/env python3
import os, sys
sys.stdout.write("move /dl/x -> gdrive:\n")
sys.stdout.write("Transferred:   \t 105.017 MiB / 1.938 GiB, 5%, "
                 "10.500 MiB/s, ETA 2m58s\n")
sys.stdout.write("Transferred:            0 / 1, 0%\n")
sys.stdout.write("Elapsed time:        10.0s\n")
sys.stdout.write("Transferring:\n")
# rclone refresh chunk without a trailing newline: the " * file" sub-line
# glues onto the front of the next Transferred: line
sys.stdout.write(" *   file.mkv:100% /1.9GiB, 10MiB/s, 0s")
sys.stdout.write("Transferred:   \t 1.938 GiB / 1.938 GiB, 100%, "
                 "10.500 MiB/s, ETA 0s\n")
sys.stdout.buffer.write(b"garbage \xff\xfe byte\n")
sys.stdout.write("bwlimit=%s\n" % os.environ.get("RCLONE_BWLIMIT", ""))
sys.stdout.write("OK: /dl/x\n")
sys.exit(3 if sys.argv[3] == "FAIL" else 0)
'''


class TestRunTodriveUp(unittest.TestCase):
    """Exercise the real streaming loop end-to-end against a fake todrive."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fake = os.path.join(self.tmp, "todrive")
        with open(fake, "w") as f:
            f.write(FAKE_TODRIVE)
        self._orig = app.TODRIVE
        app.TODRIVE = fake

    def tearDown(self):
        app.TODRIVE = self._orig

    def _run(self, drive="MyDrive", bwlimit=""):
        progress = []
        rc, out = app.run_todrive_up("/dl/x", drive, bwlimit=bwlimit,
                                     cwd=self.tmp,
                                     progress_cb=progress.append)
        return rc, out, progress

    def test_success_rc_progress_and_collapsed_output(self):
        rc, out, progress = self._run(bwlimit="5M")
        self.assertEqual(rc, 0)
        # both byte-progress lines reached the callback, parsed
        self.assertEqual([p["pct"] for p in progress], [5, 100])
        self.assertEqual(progress[1]["speed"], "10.500 MiB/s")
        self.assertEqual(progress[1]["eta"], "0s")
        lines = out.splitlines()
        # todrive's own lines survive; env var was plumbed through
        self.assertIn("move /dl/x -> gdrive:", lines)
        self.assertIn("OK: /dl/x", lines)
        self.assertIn("bwlimit=5M", lines)
        # the garbage byte was replaced, not fatal
        self.assertTrue(any("garbage" in ln for ln in lines))
        # repeating stats noise collapsed to ONE final progress line,
        # sliced clean of the glued " * file" prefix
        progress_lines = [ln for ln in lines if "Transferred:" in ln]
        self.assertEqual(len(progress_lines), 1)
        self.assertTrue(progress_lines[0].startswith("Transferred:"))
        self.assertIn("100%", progress_lines[0])
        self.assertNotIn("Elapsed time", out)
        self.assertNotIn("Transferring:", out)

    def test_failure_rc_propagates(self):
        rc, out, progress = self._run(drive="FAIL")
        self.assertEqual(rc, 3)
        self.assertEqual([p["pct"] for p in progress], [5, 100])


# --- rclone progress parsing ---------------------------------------------

class TestProgressParsing(unittest.TestCase):
    def test_byte_progress_line(self):
        m = app._PROGRESS_RE.search(
            "Transferred:   \t 105.017 MiB / 1.938 GiB, 5%, "
            "10.500 MiB/s, ETA 2m58s")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("pct"), "5")
        self.assertEqual(m.group("speed").strip(), "10.500 MiB/s")
        self.assertEqual(m.group("eta"), "2m58s")

    def test_file_count_line_ignored(self):
        self.assertIsNone(
            app._PROGRESS_RE.search("Transferred:            0 / 1, 0%"))

    def test_early_line_without_eta(self):
        m = app._PROGRESS_RE.search(
            "Transferred:              0 B / 1.938 GiB, 0%")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("pct"), "0")

    def test_noise_filter(self):
        for line in ("Transferred:  0 / 1, 0%", "Elapsed time:   10.0s",
                     "Transferring:", " * file.mkv:  5% /1.9GiB", ""):
            self.assertIsNotNone(app._PROGRESS_NOISE_RE.search(line), line)
        for line in ("move /dl/x -> gdrive:", "OK: /dl/x",
                     "2026/07/03 ERROR : boom"):
            self.assertIsNone(app._PROGRESS_NOISE_RE.search(line), line)


# --- config -----------------------------------------------------------------

class TestConfig(unittest.TestCase):
    def test_defaults_when_missing(self):
        self.assertEqual(
            app.read_transmission_rpc("/no/such/config.json"),
            (9091, "", ""))

    def test_reads_keys(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "config.json")
        with open(path, "w") as f:
            json.dump({"transmission_rpc_port": 9999,
                       "transmission_rpc_user": "u",
                       "transmission_rpc_pass": "p"}, f)
        self.assertEqual(app.read_transmission_rpc(path), (9999, "u", "p"))

    def test_partial_config_merges_over_defaults(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "config.json")
        with open(path, "w") as f:
            json.dump({"transmission_rpc_user": "u"}, f)  # no port/pass
        self.assertEqual(app.read_transmission_rpc(path), (9091, "u", ""))


class TestRecoverConfig(unittest.TestCase):
    def test_defaults_when_missing(self):
        self.assertEqual(
            app.read_recover_config("/no/such/config.json"),
            {"prompt_on_failure": True})

    def test_reads_disabled(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "config.json")
        with open(path, "w") as f:
            json.dump({"recover": {"prompt_on_failure": False}}, f)
        self.assertFalse(
            app.read_recover_config(path)["prompt_on_failure"])

    def test_malformed_json_yields_defaults(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "config.json")
        with open(path, "w") as f:
            f.write("{ not json")
        self.assertEqual(
            app.read_recover_config(path), {"prompt_on_failure": True})


# --- frozen-bundle path resolution ------------------------------------------

class TestSupportDir(unittest.TestCase):
    def test_source_run_uses_script_dir(self):
        # Running from source: mutable files stay next to offload_app.py.
        self.assertEqual(app.support_dir(frozen=False), app.SCRIPT_DIR)

    def test_frozen_uses_application_support(self):
        # Frozen (py2app): never write inside the .app bundle.
        self.assertEqual(
            app.support_dir(frozen=True),
            os.path.expanduser(
                "~/Library/Application Support/drive-offload"))

    def test_default_reads_sys_frozen(self):
        # No explicit flag: falls back to sys.frozen (absent in tests).
        self.assertEqual(app.support_dir(), app.SCRIPT_DIR)
        with mock.patch.object(sys, "frozen", "macosx_app", create=True):
            self.assertEqual(app.support_dir(),
                             app.support_dir(frozen=True))

    def test_todrive_stays_next_to_script(self):
        # py2app puts todrive in Resources next to the main script, so it
        # must resolve off SCRIPT_DIR, not the support dir.
        self.assertEqual(app.TODRIVE,
                         os.path.join(app.SCRIPT_DIR, "todrive"))


# --- todrive config resolution (frozen fallback) ----------------------------

TODRIVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "todrive")

# Load the real todrive as a module and print its resolved base_remote, so we
# exercise load_config() without touching rclone. todrive has no .py suffix,
# so it's loaded by explicit file path.
_LOAD_CONFIG_PROBE = (
    "import importlib.util, sys\n"
    "from importlib.machinery import SourceFileLoader\n"
    "loader = SourceFileLoader('todrive_mod', sys.argv[1])\n"
    "spec = importlib.util.spec_from_loader('todrive_mod', loader)\n"
    "m = importlib.util.module_from_spec(spec)\n"
    "loader.exec_module(m)\n"
    "print(m.load_config()['base_remote'])\n")


class TestTodriveConfigResolution(unittest.TestCase):
    """todrive must find the user's real config when frozen: its SCRIPT_DIR is
    Contents/Resources, which ships no config.json, so load_config() falls back
    to the Application Support copy. Each case copies todrive into a scratch
    dir (to control whether an adjacent config.json exists, mirroring the
    frozen 'no config beside the script' layout) and runs its load_config()
    in a subprocess with an isolated HOME so the real ~/Library files (which
    todrive reaches via expanduser) are never read or written."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.scriptdir = tempfile.mkdtemp()
        self.todrive = os.path.join(self.scriptdir, "todrive")
        shutil.copy(TODRIVE_PATH, self.todrive)
        self.appsup = os.path.join(
            self.home, "Library", "Application Support", "drive-offload")
        os.makedirs(self.appsup)

    def _write(self, path, base_remote):
        with open(path, "w") as f:
            json.dump({"base_remote": base_remote}, f)

    def _resolve(self, extra_env=None):
        env = dict(os.environ, HOME=self.home)
        env.pop("TODRIVE_CONFIG", None)
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            [sys.executable, "-c", _LOAD_CONFIG_PROBE, self.todrive],
            cwd=self.scriptdir, env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip(), proc.stderr

    def test_fallback_to_application_support(self):
        # No config.json beside the script (frozen layout) -> the Application
        # Support copy supplies base_remote.
        self._write(os.path.join(self.appsup, "config.json"), "gdriveFROZEN:")
        base, _ = self._resolve()
        self.assertEqual(base, "gdriveFROZEN:")

    def test_adjacent_config_wins(self):
        # Source/CLI layout: config.json beside the script wins and the
        # support copy is never consulted (dev behavior unchanged).
        self._write(os.path.join(self.scriptdir, "config.json"), "gdriveDEV:")
        self._write(os.path.join(self.appsup, "config.json"), "gdriveFROZEN:")
        base, _ = self._resolve()
        self.assertEqual(base, "gdriveDEV:")

    def test_explicit_override_wins(self):
        # TODRIVE_CONFIG (what the app exports) beats both other candidates.
        override = os.path.join(self.home, "override.json")
        self._write(override, "gdriveOVR:")
        self._write(os.path.join(self.scriptdir, "config.json"), "gdriveDEV:")
        self._write(os.path.join(self.appsup, "config.json"), "gdriveFROZEN:")
        base, _ = self._resolve({"TODRIVE_CONFIG": override})
        self.assertEqual(base, "gdriveOVR:")

    def test_defaults_when_no_candidate(self):
        # Nothing anywhere -> built-in default, no error (out-of-the-box CLI).
        base, err = self._resolve()
        self.assertEqual(base, "gdrive:")
        self.assertEqual(err.strip(), "")

    def test_malformed_adjacent_falls_through_with_warning(self):
        # A syntax error in the adjacent config must warn and fall through to
        # the support copy, not silently use defaults.
        with open(os.path.join(self.scriptdir, "config.json"), "w") as f:
            f.write("{ not valid json")
        self._write(os.path.join(self.appsup, "config.json"), "gdriveFROZEN:")
        base, err = self._resolve()
        self.assertEqual(base, "gdriveFROZEN:")
        self.assertIn("config.json", err)

    def test_non_ascii_config_survives_c_locale(self):
        # Regression (adversarial review): the frozen .app runs under the C
        # locale, where a bare open() decodes config.json as ASCII. A valid
        # UTF-8 config carrying any non-ASCII value (here a watch_dirs path,
        # ~/Películas) then raised UnicodeDecodeError (a ValueError subclass),
        # the candidate was skipped with an "ignoring unreadable config"
        # warning, and base_remote silently reverted to the 'gdrive:' default
        # -- the exact empty-drive-picker bug. open(path, encoding="utf-8")
        # fixes it. Mirrors TestOsascriptEncoding's C-locale harness.
        cfg = os.path.join(self.appsup, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"base_remote": "gdriveUTF8:",
                       "watch_dirs": ["~/Películas"]},
                      f, ensure_ascii=False)
        env = dict(os.environ, HOME=self.home, LC_ALL="C", LANG="C",
                   LC_CTYPE="C", PYTHONCOERCECLOCALE="0", PYTHONUTF8="0")
        env.pop("TODRIVE_CONFIG", None)
        env.pop("PYTHONIOENCODING", None)
        proc = subprocess.run(
            [sys.executable, "-X", "utf8=0", "-c", _LOAD_CONFIG_PROBE,
             self.todrive],
            cwd=self.scriptdir, env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # base_remote comes through instead of reverting to the default...
        self.assertEqual(proc.stdout.strip(), "gdriveUTF8:")
        # ...and no candidate was rejected as unreadable
        self.assertNotIn("ignoring unreadable config", proc.stderr)


# --- pre-existing seeding torrents: check drive, ask, snooze -----------------

def _seeding_dl(gid, name, path):
    """A torrent that is raw-active but fully downloaded (seeding)."""
    return {"gid": gid, "status": "active", "totalLength": "100",
            "completedLength": "100", "downloadSpeed": "0",
            "dir": "/dl", "bittorrent": {"info": {"name": name}},
            "files": [{"path": path}]}


class TestExistingTorrentAsk(unittest.TestCase):
    """A download first seen already complete routes to ask_existing_cb with
    snooze semantics, instead of the new-download ask."""

    def setUp(self):
        FIXTURE.downloads = []
        FIXTURE.calls = []
        self.srv, self.port = start_server()
        self.tmp = tempfile.mkdtemp()
        self.store = app.DecisionStore(os.path.join(self.tmp, "d.json"))
        self.asks = []
        self.existing_asks = []
        self.uploads = []
        self.notes = []
        self.existing_answer = [None]

        def ask_cb(gid, name):
            self.asks.append(gid)
            return "local"

        def ask_existing_cb(gid, name):
            self.existing_asks.append(gid)
            return self.existing_answer[0]

        self.poller = app.Poller(
            app.Aria2Client(port=self.port), self.store,
            ask_cb=ask_cb, upload_cb=lambda p, d, g, n:
                self.uploads.append((p, d, g, n)),
            notify_cb=lambda *a: self.notes.append(a),
            ask_existing_cb=ask_existing_cb)

    def tearDown(self):
        try:
            self.srv.shutdown()
            self.srv.server_close()
        except Exception:
            pass

    def test_seeding_routes_to_existing_ask_not_new_ask(self):
        FIXTURE.downloads = [_seeding_dl("gS", "Old.Show", "/dl/old.mkv")]
        self.existing_answer[0] = "local"
        self.poller.poll_once()
        self.assertEqual(self.existing_asks, ["gS"])
        self.assertEqual(self.asks, [])
        self.assertEqual(self.store.get("gS")["choice"], "local")

    def test_partial_download_still_uses_new_ask(self):
        FIXTURE.downloads = [{
            "gid": "gN", "status": "active", "totalLength": "100",
            "completedLength": "10", "downloadSpeed": "5",
            "dir": "/dl", "files": [{"path": "/dl/new.mkv"}]}]
        self.poller.poll_once()
        self.assertEqual(self.asks, ["gN"])
        self.assertEqual(self.existing_asks, [])

    def test_already_on_drive_records_handled_no_upload_no_reask(self):
        FIXTURE.downloads = [_seeding_dl("gA", "Up.Show", "/dl/up.mkv")]
        self.existing_answer[0] = "already:DriveA"
        self.poller.poll_once()
        rec = self.store.get("gA")
        self.assertEqual(rec["choice"], "already:DriveA")
        self.assertTrue(rec["handled"])
        self.assertEqual(self.uploads, [])
        # notified once, never re-asked or re-notified
        self.assertEqual(len(self.notes), 1)
        self.poller.poll_once()
        self.assertEqual(self.existing_asks, ["gA"])
        self.assertEqual(self.uploads, [])
        self.assertEqual(len(self.notes), 1)

    def test_upload_choice_dispatches_and_removal_lifecycle_applies(self):
        # A real on-disk payload so the readiness gate lets the dispatch
        # through (the gate demands the final-name file actually exist).
        send = os.path.join(self.tmp, "send.mkv")
        with open(send, "wb") as f:
            f.write(b"x" * 100)
        FIXTURE.downloads = [_seeding_dl("gU", "Send.Show", send)]
        self.existing_answer[0] = "drive:Films"
        self.poller.poll_once()
        # decision recorded and, being complete, dispatched in the same poll
        self.assertEqual(self.store.get("gU")["choice"], "drive:Films")
        self.assertEqual(self.uploads,
                         [(send, "Films", "gU", "Send.Show")])

    def test_no_answer_snoozes_for_this_run_only(self):
        FIXTURE.downloads = [_seeding_dl("gZ", "Later.Show", "/dl/l.mkv")]
        self.existing_answer[0] = None
        self.poller.poll_once()
        self.poller.poll_once()
        # asked exactly once this run, nothing recorded
        self.assertEqual(self.existing_asks, ["gZ"])
        self.assertFalse(self.store.has("gZ"))
        # a fresh poller (next app run) asks again
        p2 = app.Poller(
            app.Aria2Client(port=self.port), self.store,
            ask_cb=lambda g, n: "local",
            upload_cb=lambda *a: None,
            ask_existing_cb=lambda g, n:
                self.existing_asks.append(g) or "local")
        p2.poll_once()
        self.assertEqual(self.existing_asks, ["gZ", "gZ"])

    def test_ask_error_snoozes_instead_of_recording_local(self):
        FIXTURE.downloads = [_seeding_dl("gE", "Err.Show", "/dl/e.mkv")]

        def boom(gid, name):
            raise RuntimeError("dialog exploded")
        self.poller.ask_existing_cb = boom
        self.poller.poll_once()
        self.assertFalse(self.store.has("gE"))
        self.assertIn("gE", self.poller._snoozed)

    def test_paused_snoozes_existing_but_still_autolocals_new(self):
        self.poller.is_paused_cb = lambda: True
        FIXTURE.downloads = [
            _seeding_dl("gP", "Paused.Show", "/dl/p.mkv"),
            {"gid": "gQ", "status": "active", "totalLength": "100",
             "completedLength": "10", "downloadSpeed": "1",
             "dir": "/dl", "files": [{"path": "/dl/q.mkv"}]}]
        self.poller.poll_once()
        self.assertFalse(self.store.has("gP"))       # snoozed, not recorded
        self.assertIn("gP", self.poller._snoozed)
        self.assertEqual(self.store.get("gQ")["choice"], "local")

    def test_without_existing_cb_falls_back_to_new_ask(self):
        self.poller.ask_existing_cb = None
        FIXTURE.downloads = [_seeding_dl("gF", "Fallback", "/dl/f.mkv")]
        self.poller.poll_once()
        self.assertEqual(self.asks, ["gF"])
        self.assertEqual(self.store.get("gF")["choice"], "local")


class TestAskExistingDialog(unittest.TestCase):
    """ask_existing(): drive pre-check short-circuit and button routing,
    with _osascript mocked out."""

    def test_found_on_drive_skips_dialog(self):
        with mock.patch.object(app, "_osascript") as osa:
            out = app.ask_existing("g", "X", drive_lister=lambda: [],
                                   drive_finder=lambda n: "DriveA")
        self.assertEqual(out, "already:DriveA")
        osa.assert_not_called()

    def test_keep_on_mac_returns_local(self):
        with mock.patch.object(app, "_osascript", return_value=(
                0, "button returned:Keep on Mac, gave up:false", "")):
            out = app.ask_existing("g", "X", drive_lister=lambda: [],
                                   drive_finder=lambda n: "")
        self.assertEqual(out, "local")

    def test_upload_and_remove_goes_through_picker(self):
        replies = iter([
            (0, "button returned:Upload & Remove…, gave up:false", ""),
            (0, "Films", ""),   # choose from list
        ])
        with mock.patch.object(app, "_osascript",
                               side_effect=lambda s: next(replies)):
            out = app.ask_existing("g", "X", drive_lister=lambda: ["Films"],
                                   drive_finder=lambda n: "")
        self.assertEqual(out, "drive:Films")

    def test_timeout_and_picker_cancel_snooze(self):
        with mock.patch.object(app, "_osascript", return_value=(
                0, "button returned:, gave up:true", "")):
            self.assertIsNone(
                app.ask_existing("g", "X", drive_lister=lambda: [],
                                 drive_finder=lambda n: ""))
        replies = iter([
            (0, "button returned:Upload & Remove…, gave up:false", ""),
            (0, "false", ""),   # picker cancelled
        ])
        with mock.patch.object(app, "_osascript",
                               side_effect=lambda s: next(replies)):
            self.assertIsNone(
                app.ask_existing("g", "X", drive_lister=lambda: [],
                                 drive_finder=lambda n: ""))


class TestPickDriveExclude(unittest.TestCase):
    """_pick_drive(exclude=...) drops a just-failed full drive from the list."""

    def test_excluded_drive_absent_from_list(self):
        seen = {}

        def fake(script):
            seen["script"] = script
            return (0, "B", "")

        with mock.patch.object(app, "_osascript", side_effect=fake):
            out = app._pick_drive("x", lambda: ["A", "B"], exclude={"A"})
        self.assertEqual(out, "drive:B")
        # the excluded drive must not be offered; the survivor must be
        self.assertNotIn('"A"', seen["script"])
        self.assertIn('"B"', seen["script"])

    def test_excluding_hinted_drive_drops_default_clause(self):
        hint = app.renamer.RouteResult(drive="A", drive_id=None,
                                       source="cache", confidence=1.0)
        seen = {}

        def fake(script):
            seen["script"] = script
            return (0, "B", "")

        with mock.patch.object(app, "_osascript", side_effect=fake):
            out = app._pick_drive("x", lambda: ["A", "B"],
                                  route_hint=hint, exclude={"A"})
        self.assertEqual(out, "drive:B")
        # the hint's drive was excluded -> no "default items" clause resurrects it
        self.assertNotIn("default items", seen["script"])
        self.assertNotIn('"A"', seen["script"])


class TestDriveHas(unittest.TestCase):
    """drive_has() round-trips through the real todrive subprocess using the
    TODRIVE_TEST_FAKE_EXISTS hook (no network, no rclone)."""

    def _call(self, fake_json, name="Some.Show"):
        env = {"TODRIVE_TEST_FAKE_EXISTS": fake_json,
               # keep todrive off the real user config
               "TODRIVE_CONFIG": os.path.join(tempfile.mkdtemp(), "no.json")}
        with mock.patch.dict(os.environ, env):
            return app.drive_has(name)

    def test_match_returns_drive_name(self):
        self.assertEqual(
            self._call('[{"id": "f1", "name": "Some.Show", "drive": "DriveA"}]'),
            "DriveA")

    def test_no_match_returns_empty(self):
        self.assertEqual(self._call("[]"), "")

    def test_todrive_error_returns_empty(self):
        # malformed fake JSON makes the child crash -> "" (over-ask, not skip)
        self.assertEqual(self._call("{not json"), "")


# --- dialog output decoding under the frozen app's C locale ------------------

class TestOsascriptEncoding(unittest.TestCase):
    """Regression: the .app launched by launchd runs under the C locale, so a
    bare text=True decoded osascript's reply as ASCII — and the ellipsis in
    "button returned:Shared Drive…" (U+2026, bytes e2 80 a6) raised
    UnicodeDecodeError mid-ask, which Poller's catch-all silently turned into
    a "local" decision. _osascript must decode UTF-8 regardless of locale."""

    def test_ellipsis_survives_c_locale(self):
        import subprocess
        code = (
            "import offload_app\n"
            "rc, out, err = offload_app._osascript("
            "'return \"button returned:Shared Drive\\u2026, gave up:false\"')\n"
            "assert rc == 0, (rc, err)\n"
            "print(out.encode('unicode_escape').decode('ascii'))\n")
        env = dict(os.environ, LC_ALL="C", LANG="C", LC_CTYPE="C",
                   PYTHONCOERCECLOCALE="0", PYTHONUTF8="0")
        env.pop("PYTHONIOENCODING", None)
        out = subprocess.check_output(
            [sys.executable, "-X", "utf8=0", "-c", code],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            env=env, text=True, encoding="utf-8", errors="replace")
        self.assertIn("\\u2026", out)


# --- payload-readiness gate (pure function) --------------------------------

class TestPayloadReady(unittest.TestCase):
    """payload_ready(dl, path): the on-disk gate that stops the engine's
    'complete' from being the sole trigger for a destructive rclone move."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _write(self, path, size=100):
        with open(path, "wb") as f:
            f.write(b"x" * size)
        return path

    def test_missing_final_name_the_part_race(self):
        # Transmission reports the FINAL name in files[] while the disk still
        # has "<name>.part" — the exact 2026-07-06 race.
        self._write(os.path.join(self.tmp, "movie.mkv.part"))
        final = os.path.join(self.tmp, "movie.mkv")
        dl = {"files": [{"path": final}], "totalLength": "100"}
        ready, reason = app.payload_ready(dl, self.tmp)
        self.assertFalse(ready)
        self.assertIn(final, reason)

    def test_marker_under_dir_payload_case_insensitive(self):
        # All files[] exist, but an in-progress marker lurks in a subdir; the
        # uppercase .PART asserts the suffix match is case-insensitive.
        os.makedirs(os.path.join(self.tmp, "sub"))
        f1 = self._write(os.path.join(self.tmp, "e1.mkv"))
        self._write(os.path.join(self.tmp, "sub", "e2.mkv.PART"))
        dl = {"files": [{"path": f1}], "totalLength": "0"}
        ready, reason = app.payload_ready(dl, self.tmp)
        self.assertFalse(ready)
        self.assertIn("e2.mkv.PART", reason)

    def test_marker_is_a_directory_bundle(self):
        # Safari's ".download" is a directory bundle, not a file — the scan
        # must test dir names too.
        os.makedirs(os.path.join(self.tmp, "x.download"))
        dl = {"files": [], "totalLength": "0"}
        ready, _ = app.payload_ready(dl, self.tmp)
        self.assertFalse(ready)

    def test_single_file_aria2_sibling(self):
        # The only real gate for aria2 HTTP downloads (they write the final
        # name from byte 0): a "<file>.aria2" control-file sibling.
        f = self._write(os.path.join(self.tmp, "file.bin"))
        sib = f + ".aria2"
        open(sib, "w").close()
        dl = {"files": [{"path": f}], "totalLength": "0"}
        self.assertFalse(app.payload_ready(dl, f)[0])
        os.remove(sib)
        self.assertTrue(app.payload_ready(dl, f)[0])

    def test_undersized_tree_truncation_guard(self):
        f = self._write(os.path.join(self.tmp, "m.mkv"), size=100)
        dl = {"files": [{"path": f}], "totalLength": "200"}
        self.assertFalse(app.payload_ready(dl, f)[0])
        self._write(f, size=200)  # grow to the expected size
        self.assertTrue(app.payload_ready(dl, f)[0])

    def test_unselected_file_skipped(self):
        # A deselected file is excluded from BOTH the files-exist and the size
        # checks, so a partial-selection torrent is still ready.
        a = self._write(os.path.join(self.tmp, "a.mkv"), size=100)
        b = os.path.join(self.tmp, "b.mkv")  # never on disk
        dl = {"files": [{"path": a, "length": 100, "selected": "true"},
                        {"path": b, "length": 900, "selected": "false"}],
              "totalLength": "1000"}
        self.assertTrue(app.payload_ready(dl, self.tmp)[0])

    def test_no_metadata_is_ready(self):
        # totalLength 0 and no per-file lengths -> size gate skipped.
        f = self._write(os.path.join(self.tmp, "n.mkv"))
        dl = {"files": [{"path": f}], "totalLength": "0"}
        self.assertTrue(app.payload_ready(dl, f)[0])

    def test_bittorrent_ignores_aria2_sibling(self):
        # Regression (adversarial review): aria2 can keep a "<file>.aria2"
        # control file through seeding (--force-save), but a BT payload is
        # hash-verified once complete. A dl carrying a "bittorrent" key must be
        # READY despite the sibling; the same dl WITHOUT it (an aria2 HTTP
        # download, for which the sibling really does mean in-progress) must
        # not — otherwise a finished single-file torrent defers forever.
        f = self._write(os.path.join(self.tmp, "movie.mkv"))
        open(f + ".aria2", "w").close()
        bt = {"files": [{"path": f}], "totalLength": "0",
              "bittorrent": {"info": {"name": "movie"}}}
        self.assertTrue(app.payload_ready(bt, f)[0])
        http = {"files": [{"path": f}], "totalLength": "0"}
        self.assertFalse(app.payload_ready(http, f)[0])

    def test_dir_aria2_sibling_blocks_non_bt_only(self):
        # aria2's multi-file control file sits BESIDE the dir as "<dir>.aria2"
        # (the walk never sees it). A non-BT dir payload must block on it; a
        # verified BT one must not.
        d = os.path.join(self.tmp, "Show")
        os.makedirs(d)
        self._write(os.path.join(d, "e1.mkv"))
        open(d + ".aria2", "w").close()
        http = {"files": [], "totalLength": "0"}
        self.assertFalse(app.payload_ready(http, d)[0])
        bt = {"files": [], "totalLength": "0",
              "bittorrent": {"info": {"name": "Show"}}}
        self.assertTrue(app.payload_ready(bt, d)[0])

    def test_deselected_partfile_does_not_block(self):
        # Regression (adversarial review): Transmission's rename-partial-files
        # keeps "<name>.part" forever for a DESELECTED file. A season torrent
        # that is otherwise complete (all wanted episodes under final names)
        # must stay READY despite that leftover .part.
        d = os.path.join(self.tmp, "Season")
        os.makedirs(d)
        e1 = self._write(os.path.join(d, "e1.mkv"), size=100)
        e2 = self._write(os.path.join(d, "e2.mkv"), size=100)
        # e3 deselected after partially downloading -> e3.mkv.part lingers
        self._write(os.path.join(d, "e3.mkv.part"), size=40)
        e3_final = os.path.join(d, "e3.mkv")
        dl = {"files": [{"path": e1, "length": 100, "selected": "true"},
                        {"path": e2, "length": 100, "selected": "true"},
                        {"path": e3_final, "length": 100,
                         "selected": "false"}],
              "totalLength": "200"}
        ready, reason = app.payload_ready(dl, d)
        self.assertTrue(ready, reason)

    def test_selected_partfile_still_blocks(self):
        # The deselected carve-out must NOT weaken the real guard: a ".part"
        # for a SELECTED file (still downloading) still blocks.
        d = os.path.join(self.tmp, "Season2")
        os.makedirs(d)
        e1 = self._write(os.path.join(d, "e1.mkv"), size=100)
        self._write(os.path.join(d, "e2.mkv.part"), size=40)
        e2_final = os.path.join(d, "e2.mkv")  # selected, still .part on disk
        dl = {"files": [{"path": e1, "length": 100, "selected": "true"},
                        {"path": e2_final, "length": 100, "selected": "true"}],
              "totalLength": "200"}
        ready, reason = app.payload_ready(dl, d)
        # blocked either by the missing final name (check 1) or the marker
        self.assertFalse(ready)

    def test_absent_selected_no_marker_is_ready(self):
        # The partial-move incident: rclone's per-file move uploaded+deleted the
        # small files then failed on the .mkv (drive quota). The folder now has
        # only movie.mkv, but the manifest still lists all 3 selected files and
        # NO in-progress marker exists for the gone ones. Readiness keys to the
        # stable on-disk folder: ready. (Old code failed twice -- "missing on
        # disk" in check 1, then "size < expected" in check 3.)
        mkv = self._write(os.path.join(self.tmp, "movie.mkv"), size=100)
        txt = os.path.join(self.tmp, "readme.txt")            # moved + deleted
        jpg = os.path.join(self.tmp, "poster.jpg")            # moved + deleted
        dl = {"files": [{"path": mkv, "length": 100, "selected": "true"},
                        {"path": txt, "length": 358, "selected": "true"},
                        {"path": jpg, "length": 53000, "selected": "true"}],
              "totalLength": "53458"}
        ready, reason = app.payload_ready(dl, self.tmp)
        self.assertTrue(ready, reason)

    def test_absent_with_part_marker_still_blocks_single_file(self):
        # THE July-6 regression pin for the relaxed path: a single-file payload
        # whose final name is missing but has a "<name>.part" beside it must
        # still block -- check 2's walk never scans a file payload's non-.aria2
        # siblings, so check 1's per-file marker probe is the sole cover.
        final = os.path.join(self.tmp, "movie.mkv")   # never written
        self._write(final + ".part", size=40)
        dl = {"files": [{"path": final}], "totalLength": "100"}
        ready, reason = app.payload_ready(dl, final)
        self.assertFalse(ready)
        self.assertIn(final, reason)

    def test_absent_marker_case_insensitive(self):
        # Verifies the listdir lowercased-name scan, not exact-case exists():
        # a "<name>.PART" on disk must still block even though os.path.exists(
        # p + ".part") would miss it on a case-sensitive filesystem.
        final = os.path.join(self.tmp, "movie.mkv")   # never written
        self._write(final + ".PART", size=40)
        dl = {"files": [{"path": final}], "totalLength": "100"}
        ready, reason = app.payload_ready(dl, final)
        self.assertFalse(ready)

    def test_all_selected_absent_not_ready(self):
        # Every selected file already moved/cleaned (empty folder, no markers):
        # nothing to move, so not-ready via the at-least-one-present guard --
        # guards against dispatching rclone on an already-emptied payload.
        a = os.path.join(self.tmp, "a.mkv")
        b = os.path.join(self.tmp, "b.mkv")
        dl = {"files": [{"path": a, "length": 100, "selected": "true"},
                        {"path": b, "length": 200, "selected": "true"}],
              "totalLength": "300"}
        ready, reason = app.payload_ready(dl, self.tmp)
        self.assertFalse(ready)
        self.assertIn("nothing on disk", reason)

    def test_metadata_magnet_still_not_ready(self):
        # An aria2 magnet still fetching metadata: files[0].path is a
        # "[METADATA]name" that never lands on disk and carries no marker. The
        # guard keeps it not-ready, pinning the docstring's magnet behavior.
        meta = os.path.join(self.tmp, "[METADATA]name")
        dl = {"files": [{"path": meta}], "totalLength": "0"}
        ready, reason = app.payload_ready(dl, meta)
        self.assertFalse(ready)

    def test_absent_dropped_from_expected_but_present_truncation_caught(self):
        # Truncation detection for a PRESENT file survives the relaxation, and
        # an absent file's length no longer inflates the expected sum.
        mkv = self._write(os.path.join(self.tmp, "m.mkv"), size=50)  # length 100
        txt = os.path.join(self.tmp, "t.txt")   # absent, length 900
        dl = {"files": [{"path": mkv, "length": 100, "selected": "true"},
                        {"path": txt, "length": 900, "selected": "true"}],
              "totalLength": "1000"}
        ready, reason = app.payload_ready(dl, self.tmp)
        self.assertFalse(ready)       # size 50 < expected 100 (mkv's own length)
        self._write(mkv, size=100)    # grow mkv to whole; txt's 900 excluded
        ready, reason = app.payload_ready(dl, self.tmp)
        self.assertTrue(ready, reason)

    def test_absent_file_marker_is_dir_bundle_blocks(self):
        # The per-file probe must match a DIRECTORY marker too (Safari's
        # ".download" bundle for a missing selected file), mirroring
        # test_marker_is_a_directory_bundle.
        final = os.path.join(self.tmp, "movie.mkv")   # never written
        os.makedirs(final + ".download")
        dl = {"files": [{"path": final}], "totalLength": "0"}
        ready, reason = app.payload_ready(dl, final)
        self.assertFalse(ready)

    def test_malformed_length_degrades_not_raises(self):
        # Pins the blocking-review fix: a non-numeric files[].length must hit
        # the retained try/except -> expected = 0 (check 3 skipped) rather than
        # raise out of payload_ready and wedge the poller. Both with an absent
        # sibling and in the absent-empty variant (today's degradation path).
        mkv = self._write(os.path.join(self.tmp, "m.mkv"), size=100)
        txt = os.path.join(self.tmp, "t.txt")   # absent, no marker
        dl = {"files": [{"path": mkv, "length": "abc", "selected": "true"},
                        {"path": txt, "length": 900, "selected": "true"}],
              "totalLength": "1000"}
        ready, reason = app.payload_ready(dl, self.tmp)
        self.assertTrue(ready, reason)
        solo = {"files": [{"path": mkv, "length": "abc", "selected": "true"}],
                "totalLength": "1000"}
        ready, reason = app.payload_ready(solo, self.tmp)
        self.assertTrue(ready, reason)


# --- readiness gate wired into the poller ----------------------------------

class TestReadyGateDispatch(unittest.TestCase):
    """The gate sits in poll_once before dispatch: a not-ready payload is
    skipped WITHOUT consuming the decision, flips to a single dispatch when it
    becomes ready, and fires exactly one stuck notification if it never does."""

    def setUp(self):
        FIXTURE.downloads = []
        FIXTURE.calls = []
        self.srv, self.port = start_server()
        self.tmp = tempfile.mkdtemp()
        # point the log at a tmp file so we can assert no per-tick spam
        self._orig_log = app.LOG_FILE
        app.LOG_FILE = os.path.join(self.tmp, "app.log")
        self.store = app.DecisionStore(os.path.join(self.tmp, "d.json"))
        self.uploads = []
        self.notes = []
        self.now = [1000.0]
        self.poller = app.Poller(
            app.Aria2Client(port=self.port), self.store,
            ask_cb=lambda g, n: "drive:Films",
            upload_cb=lambda p, d, g, n: self.uploads.append((p, d, g, n)),
            notify_cb=lambda *a: self.notes.append(a),
            now_fn=lambda: self.now[0])

    def tearDown(self):
        app.LOG_FILE = self._orig_log
        try:
            self.srv.shutdown()
            self.srv.server_close()
        except Exception:
            pass

    def _dl(self):
        # complete download whose files[] names the FINAL name while the disk
        # only has "<name>.part" (the race).
        final = os.path.join(self.tmp, "m.mkv")
        return {"gid": "gG", "status": "active", "totalLength": "100",
                "completedLength": "100", "downloadSpeed": "0",
                "dir": self.tmp, "files": [{"path": final}]}

    def _log_lines(self, needle):
        try:
            with open(app.LOG_FILE) as f:
                return [ln for ln in f if needle in ln]
        except OSError:
            return []

    def test_not_ready_blocks_without_consuming(self):
        with open(os.path.join(self.tmp, "m.mkv.part"), "wb") as f:
            f.write(b"x" * 100)
        FIXTURE.downloads = [self._dl()]
        self.poller.poll_once()
        self.poller.poll_once()
        self.assertEqual(self.uploads, [])
        self.assertFalse(self.store.get("gG")["handled"])
        self.assertNotIn("gG", self.poller._uploading)
        self.assertIn("gG", self.poller._not_ready)
        # NOTREADY logged once, not per tick
        self.assertEqual(len(self._log_lines("NOTREADY")), 1)

    def test_flips_ready_dispatches_exactly_once(self):
        part = os.path.join(self.tmp, "m.mkv.part")
        with open(part, "wb") as f:
            f.write(b"x" * 100)
        FIXTURE.downloads = [self._dl()]
        self.poller.poll_once()          # not ready -> deferred
        self.assertEqual(self.uploads, [])
        os.rename(part, os.path.join(self.tmp, "m.mkv"))  # verified rename
        self.poller.poll_once()          # ready -> one dispatch
        self.assertEqual(len(self.uploads), 1)
        self.assertNotIn("gG", self.poller._not_ready)
        self.assertEqual(len(self._log_lines("READY gid=gG payload")), 1)
        self.poller.poll_once()          # in-flight guard -> still one
        self.assertEqual(len(self.uploads), 1)

    def test_stuck_notification_fires_once(self):
        with open(os.path.join(self.tmp, "m.mkv.part"), "wb") as f:
            f.write(b"x" * 100)
        FIXTURE.downloads = [self._dl()]
        self.poller.poll_once()                      # arms the timer
        self.assertEqual(self.notes, [])
        self.now[0] += app.STUCK_NOTIFY_SECONDS + 1
        self.poller.poll_once()                      # crosses the threshold
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(self.notes[0][0], "Upload waiting")
        # further polls, even further in the future, never re-notify
        self.now[0] += 10 * app.STUCK_NOTIFY_SECONDS
        self.poller.poll_once()
        self.poller.poll_once()
        self.assertEqual(len(self.notes), 1)


# --- todrive up: last-hop guard + empty-dir cleanup ------------------------

_TODRIVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "todrive")


def _load_todrive():
    """Load the real todrive (no .py suffix) as a module by file path, so its
    cmd_up runs in-process with subprocess.call monkeypatched (no rclone)."""
    import importlib.util
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("todrive_mod", _TODRIVE_PATH)
    spec = importlib.util.spec_from_loader("todrive_mod", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


class TestTodriveUp(unittest.TestCase):
    def setUp(self):
        import argparse
        self.argparse = argparse
        self.mod = _load_todrive()
        self.tmp = tempfile.mkdtemp()
        self.cfg = dict(self.mod.DEFAULT_CONFIG)

    def _args(self, paths, keep=False, allow_partial=False,
             no_overflow=False):
        return self.argparse.Namespace(paths=paths, keep=keep,
                                       allow_partial=allow_partial,
                                       no_overflow=no_overflow)

    def _fake_call(self, deletes=None, rc=0):
        """A subprocess.call stand-in: records the cmd, optionally deletes the
        given paths (simulating rclone move leaving empty dirs), returns rc."""
        recorded = {}

        def call(cmd):
            recorded["cmd"] = cmd
            for p in (deletes or []):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return rc
        return call, recorded

    def _fake_popen(self, deletes=None, rc=0):
        """A subprocess.Popen stand-in for cmd_up's rewritten
        Popen(cmd, stderr=PIPE, text=True) call (todrive FEATURE B):
        records the cmd, optionally deletes the given paths (simulating
        rclone move leaving empty dirs behind), and reports rc via .wait()
        after an empty .stderr stream. Same shape as _fake_call, speaking
        the interface the rewritten cmd_up actually calls."""
        recorded = {}

        class FakeProc:
            def __init__(self, cmd, **kw):
                recorded["cmd"] = cmd
                self.stderr = iter(())
                for p in (deletes or []):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

            def wait(self):
                return rc

        return (lambda cmd, **kw: FakeProc(cmd, **kw)), recorded

    def test_marker_blocks_without_allow_partial(self):
        import contextlib
        import io
        src = os.path.join(self.tmp, "show")
        os.makedirs(src)
        open(os.path.join(src, "e1.mkv.part"), "w").close()
        call, _rec = self._fake_call()
        buf = io.StringIO()
        with mock.patch.object(self.mod, "resolve_id", return_value="D1"), \
                mock.patch.object(self.mod.subprocess, "call", call), \
                contextlib.redirect_stderr(buf):
            rc = self.mod.cmd_up(self.cfg, self._args([src, "Films"]))
        self.assertEqual(rc, 1)
        self.assertNotIn("cmd", _rec)   # rclone never invoked
        self.assertIn("e1.mkv.part", buf.getvalue())
        self.assertIn("--allow-partial", buf.getvalue())

    def test_allow_partial_forces_through(self):
        src = os.path.join(self.tmp, "show")
        os.makedirs(src)
        open(os.path.join(src, "e1.mkv.part"), "w").close()
        popen, rec = self._fake_popen(rc=0)
        # --no-overflow: this test is about the marker/allow-partial gate,
        # orthogonal to FEATURE B's cap routing -- kept on the exact legacy
        # resolve path (byte-identical) so it needs no drive-usage fixture.
        with mock.patch.object(self.mod, "resolve_id", return_value="D1"), \
                mock.patch.object(self.mod.subprocess, "Popen", popen):
            rc = self.mod.cmd_up(
                self.cfg, self._args([src, "Films"], allow_partial=True,
                                     no_overflow=True))
        self.assertEqual(rc, 0)
        self.assertIn("cmd", rec)       # rclone WAS invoked

    def test_single_file_markers_block(self):
        # a .aria2 sibling blocks
        f = os.path.join(self.tmp, "movie.mkv")
        open(f, "w").close()
        open(f + ".aria2", "w").close()
        call, rec = self._fake_call()
        with mock.patch.object(self.mod, "resolve_id", return_value="D1"), \
                mock.patch.object(self.mod.subprocess, "call", call):
            rc = self.mod.cmd_up(self.cfg, self._args([f, "Films"]))
        self.assertEqual(rc, 1)
        self.assertNotIn("cmd", rec)
        # a src file NAMED with a marker suffix blocks
        cr = os.path.join(self.tmp, "part.crdownload")
        open(cr, "w").close()
        call2, rec2 = self._fake_call()
        with mock.patch.object(self.mod, "resolve_id", return_value="D1"), \
                mock.patch.object(self.mod.subprocess, "call", call2):
            rc = self.mod.cmd_up(self.cfg, self._args([cr, "Films"]))
        self.assertEqual(rc, 1)
        self.assertNotIn("cmd", rec2)

    def test_dir_named_marker_blocks(self):
        # Regression (adversarial review): Safari's ".download" is a DIRECTORY
        # bundle. find_incomplete_markers must test the src's OWN basename, not
        # only its children — a dir named "movie.mkv.download" whose contents
        # are clean-named must block and never reach rclone.
        src = os.path.join(self.tmp, "movie.mkv.download")
        os.makedirs(src)
        open(os.path.join(src, "Info.plist"), "w").close()
        open(os.path.join(src, "movie.mkv"), "w").close()
        call, rec = self._fake_call()
        with mock.patch.object(self.mod, "resolve_id", return_value="D1"), \
                mock.patch.object(self.mod.subprocess, "call", call):
            rc = self.mod.cmd_up(self.cfg, self._args([src, "Films"]))
        self.assertEqual(rc, 1)
        self.assertNotIn("cmd", rec)    # rclone never invoked

    def test_dir_aria2_sibling_blocks(self):
        # Regression (adversarial review): aria2 stores a multi-file torrent's
        # control file as "<dir>.aria2", a sibling of the payload dir. The
        # dir-walk never sees it, so a mid-download multi-file torrent (all
        # files preallocated under final names) would upload truncated data
        # unless the sibling is tested explicitly.
        src = os.path.join(self.tmp, "Show")
        os.makedirs(src)
        open(os.path.join(src, "e1.mkv"), "w").close()
        open(src + ".aria2", "w").close()
        call, rec = self._fake_call()
        with mock.patch.object(self.mod, "resolve_id", return_value="D1"), \
                mock.patch.object(self.mod.subprocess, "call", call):
            rc = self.mod.cmd_up(self.cfg, self._args([src, "Films"]))
        self.assertEqual(rc, 1)
        self.assertNotIn("cmd", rec)

    def test_empty_tree_cleanup_after_move(self):
        src = os.path.join(self.tmp, "show")
        os.makedirs(os.path.join(src, "a", "b"))
        f1 = os.path.join(src, "a", "b", "f1")
        f2 = os.path.join(src, "f2")
        open(f1, "w").close()
        open(f2, "w").close()
        popen, rec = self._fake_popen(deletes=[f1, f2], rc=0)
        with mock.patch.object(self.mod, "resolve_id", return_value="D1"), \
                mock.patch.object(self.mod.subprocess, "Popen", popen):
            rc = self.mod.cmd_up(
                self.cfg, self._args([src, "Films"], no_overflow=True))
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(src))       # empty tree removed
        self.assertIn("--delete-empty-src-dirs", rec["cmd"])

    def test_keep_copy_leaves_everything(self):
        src = os.path.join(self.tmp, "show")
        os.makedirs(os.path.join(src, "a"))
        f1 = os.path.join(src, "a", "f1")
        open(f1, "w").close()
        popen, rec = self._fake_popen(deletes=[], rc=0)  # copy touches nothing
        with mock.patch.object(self.mod, "resolve_id", return_value="D1"), \
                mock.patch.object(self.mod.subprocess, "Popen", popen):
            rc = self.mod.cmd_up(
                self.cfg, self._args([src, "Films"], keep=True,
                                     no_overflow=True))
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(f1))         # nothing removed
        self.assertNotIn("--delete-empty-src-dirs", rec["cmd"])

    def test_non_empty_leftovers_preserved_with_warning(self):
        import contextlib
        import io
        src = os.path.join(self.tmp, "show")
        os.makedirs(os.path.join(src, "a"))
        f1 = os.path.join(src, "a", "f1")
        f2 = os.path.join(src, "f2")
        open(f1, "w").close()
        open(f2, "w").close()
        popen, _rec = self._fake_popen(deletes=[f2], rc=0)  # f1 left behind
        buf = io.StringIO()
        with mock.patch.object(self.mod, "resolve_id", return_value="D1"), \
                mock.patch.object(self.mod.subprocess, "Popen", popen), \
                contextlib.redirect_stderr(buf):
            rc = self.mod.cmd_up(
                self.cfg, self._args([src, "Films"], no_overflow=True))
        self.assertEqual(rc, 0)                      # never fail over leftovers
        self.assertTrue(os.path.exists(src))
        self.assertIn("leftovers", buf.getvalue())

    def test_oversized_src_fails_only_itself_batch_continues(self):
        # Regression (adversarial review): an un-routable src (payload > cap)
        # must fail JUST that file -- failures += 1; continue -- not sys.exit
        # and abort the whole multi-src `up` batch. The fitting src after it
        # must still upload.
        import contextlib
        import io
        big = os.path.join(self.tmp, "huge.mkv")
        ok = os.path.join(self.tmp, "small.mkv")
        open(big, "w").close()
        open(ok, "w").close()
        cap = self.mod.SHARED_DRIVE_CAP_BYTES

        # huge.mkv reports oversized; small.mkv is tiny and fits "Films".
        def fake_size(p):
            return cap + 1 if p == big else 5
        env = {"TODRIVE_TEST_FAKE_DRIVES":
               json.dumps([{"id": "D1", "name": "Films"}]),
               "TODRIVE_TEST_FAKE_USAGE":
               json.dumps({"D1": {"bytes": 10, "objects": 1}})}

        uploaded = []

        class FakeProc:
            def __init__(self, cmd, **kw):
                uploaded.append(cmd)
                self.stderr = iter(())

            def wait(self):
                return 0

        buf = io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.mod, "local_size", fake_size), \
                mock.patch.object(self.mod.subprocess, "Popen",
                                  lambda cmd, **kw: FakeProc(cmd, **kw)), \
                contextlib.redirect_stderr(buf):
            rc = self.mod.cmd_up(self.cfg, self._args([big, ok, "Films"]))

        self.assertEqual(rc, 1)                       # batch reports a failure
        self.assertIn("exceeds the 100 GB", buf.getvalue())  # huge.mkv errored
        self.assertEqual(len(uploaded), 1)            # exactly one upload ran
        self.assertIn(ok, uploaded[0])                # ...and it was small.mkv
        self.assertNotIn(big, uploaded[0])            # huge.mkv never uploaded


# --- FEATURE B: overflow chain naming ---------------------------------------

class TestOverflowChain(unittest.TestCase):
    """overflow_root/next_overflow: suffix-anchored chain naming, against the
    real todrive module (SourceFileLoader probe, no .py suffix)."""

    def setUp(self):
        self.mod = _load_todrive()

    def test_chain_from_root(self):
        m = self.mod
        self.assertEqual(m.next_overflow("Movies"), "Movies overflow")
        self.assertEqual(m.next_overflow("Movies overflow"),
                         "Movies overflow 2")
        self.assertEqual(m.next_overflow("Movies overflow 2"),
                         "Movies overflow 3")

    def test_mid_chain_start_advances_forward(self):
        # starting directly from a mid-chain link (e.g. uploading straight to
        # a full "Movies overflow 5") still advances forward, never loops
        # back to the first link.
        self.assertEqual(self.mod.next_overflow("Movies overflow 5"),
                         "Movies overflow 6")

    def test_midstring_overflow_untouched(self):
        # a drive literally named "Overflow Test" does not match the
        # trailing-suffix regex -- only a TRAILING " overflow[ N]" does.
        m = self.mod
        self.assertEqual(m.overflow_root("Overflow Test"), "Overflow Test")
        self.assertEqual(m.next_overflow("Overflow Test"),
                         "Overflow Test overflow")

    def test_overflow_root_recovers_base(self):
        m = self.mod
        self.assertEqual(m.overflow_root("Movies overflow 2"), "Movies")
        self.assertEqual(m.overflow_root("Movies overflow"), "Movies")
        self.assertEqual(m.overflow_root("Movies"), "Movies")

    def test_chain_never_doubles_the_suffix(self):
        m = self.mod
        chain = ["Movies"]
        for _ in range(5):
            chain.append(m.next_overflow(chain[-1]))
        self.assertEqual(chain, ["Movies", "Movies overflow",
                                 "Movies overflow 2", "Movies overflow 3",
                                 "Movies overflow 4", "Movies overflow 5"])
        self.assertTrue(all("overflow overflow" not in c for c in chain))

    def test_idempotent_from_any_starting_link(self):
        # next_overflow is a pure function of its own position, whatever link
        # of the chain you start from -- calling it twice from two different
        # starting points that happen to land on the same link yields the
        # same next hop.
        m = self.mod
        self.assertEqual(m.next_overflow("Movies overflow 3"),
                         m.next_overflow(m.overflow_root("Movies overflow 3")
                                        + " overflow 3"))


# --- FEATURE A: drive_usage / `todrive du` ----------------------------------

class TestDriveUsage(unittest.TestCase):
    """drive_usage()/`todrive du --json`/plain, via the real todrive
    subprocess with the TODRIVE_TEST_FAKE_DRIVES/_USAGE hooks (TestDriveHas
    idiom): no network, no rclone."""

    def _run(self, args, drives, usage):
        env = dict(os.environ,
                   TODRIVE_TEST_FAKE_DRIVES=json.dumps(drives),
                   TODRIVE_TEST_FAKE_USAGE=json.dumps(usage),
                   TODRIVE_CONFIG=os.path.join(tempfile.mkdtemp(), "no.json"))
        proc = subprocess.run([sys.executable, TODRIVE_PATH] + args,
                              env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        return proc.returncode, proc.stdout, proc.stderr

    def test_json_shape_bytes_objects_pct_cap(self):
        drives = [{"id": "D1", "name": "Movies"},
                  {"id": "D2", "name": "Empty"}]
        usage = {"D1": {"bytes": 50 * 1000**3, "objects": 7}}
        rc, out, err = self._run(["du", "--json"], drives, usage)
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual(data["cap_bytes"], 100 * 1000**3)
        by_id = {d["id"]: d for d in data["drives"]}
        self.assertEqual(by_id["D1"]["bytes"], 50 * 1000**3)
        self.assertEqual(by_id["D1"]["objects"], 7)
        self.assertEqual(by_id["D1"]["pct"], 50.0)
        # a drive with no files still gets a zero-filled row
        self.assertEqual(by_id["D2"]["bytes"], 0)
        self.assertEqual(by_id["D2"]["objects"], 0)
        self.assertEqual(by_id["D2"]["pct"], 0.0)

    def test_grandfathered_over_cap_plain_mode_tag_and_exit_0(self):
        drives = [{"id": "D1", "name": "Old"}]
        usage = {"D1": {"bytes": 120 * 1000**3, "objects": 3}}
        rc, out, err = self._run(["du"], drives, usage)
        self.assertEqual(rc, 0, err)   # grandfathered is reported, not an error
        self.assertIn("(grandfathered)", out)
        self.assertIn("Old", out)

    def test_plain_mode_columns_sorted_by_lowercase_name_and_footer(self):
        drives = [{"id": "D1", "name": "Zulu"}, {"id": "D2", "name": "Alpha"}]
        usage = {}
        rc, out, err = self._run(["du"], drives, usage)
        self.assertEqual(rc, 0, err)
        self.assertIn("NAME", out)
        self.assertIn("USED", out)
        self.assertIn("%CAP", out)
        self.assertIn("OBJECTS", out)
        self.assertLess(out.index("Alpha"), out.index("Zulu"))
        self.assertIn("cap 100 GB/drive", out)

    def test_json_carries_trash_keys_verbatim(self):
        drives = [{"id": "D1", "name": "DriveH"}]
        usage = {"D1": {"bytes": 0, "objects": 0,
                        "trash_bytes": 56 * 1000**3, "trash_objects": 10}}
        rc, out, err = self._run(["du", "--json"], drives, usage)
        self.assertEqual(rc, 0, err)
        d = json.loads(out)["drives"][0]
        self.assertEqual(d["bytes"], 0)        # content-only, unchanged
        self.assertEqual(d["objects"], 0)
        self.assertEqual(d["pct"], 0.0)
        self.assertEqual(d["trash_bytes"], 56 * 1000**3)
        self.assertEqual(d["trash_objects"], 10)

    def test_json_defaults_trash_keys_for_pre_trash_payload(self):
        """A fake-usage payload without trash_* (a pre-trash persisted /
        legacy shape) still emits trash_bytes/trash_objects defaulted to 0."""
        drives = [{"id": "D1", "name": "Movies"}]
        usage = {"D1": {"bytes": 50 * 1000**3, "objects": 7}}
        rc, out, err = self._run(["du", "--json"], drives, usage)
        self.assertEqual(rc, 0, err)
        d = json.loads(out)["drives"][0]
        self.assertEqual(d["bytes"], 50 * 1000**3)
        self.assertEqual(d["trash_bytes"], 0)
        self.assertEqual(d["trash_objects"], 0)


class TestDriveUsageFallback(unittest.TestCase):
    """drive_usage()'s network path (FAKE_USAGE hook deliberately OFF so the
    real scan runs, urlopen mocked): Google's corpora=allDrives listing 500s
    mid-pagination on large accounts, so the fast path must retry and then
    fall back to a reliable per-drive scan rather than erroring out. This is
    the exact failure the live smoke test hit."""

    def setUp(self):
        self.mod = _load_todrive()
        self.drives = [{"id": "D1", "name": "Movies"},
                       {"id": "D2", "name": "TV"}]
        # D1 has one 5-byte file; D2 is empty.
        self.per_drive = {"D1": [{"driveId": "D1", "size": "5"}], "D2": []}

    def _resp(self, payload):
        class R:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def read(s):
                return json.dumps(payload).encode("utf-8")
        return R()

    def _per_drive_reply(self, url):
        import urllib.parse as up
        did = up.parse_qs(up.urlparse(url).query)["driveId"][0]
        return self._resp({"files": self.per_drive.get(did, [])})

    def _run_with_urlopen(self, fake_urlopen):
        with mock.patch.object(self.mod, "get_drives",
                               return_value=self.drives), \
                mock.patch.object(self.mod, "get_token", return_value="tok"), \
                mock.patch.object(self.mod.urllib.request, "urlopen",
                                  fake_urlopen), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TODRIVE_TEST_FAKE_USAGE", None)
            return self.mod.drive_usage({})

    def test_alldrives_500_falls_back_to_per_drive(self):
        import urllib.error
        calls = {"alldrives": 0}

        def fake_urlopen(req, *a, **kw):
            url = req.full_url
            if "corpora=allDrives" in url:
                calls["alldrives"] += 1
                raise urllib.error.HTTPError(url, 500, "err", None, None)
            return self._per_drive_reply(url)

        usage = self._run_with_urlopen(fake_urlopen)
        # allDrives was retried (USAGE_SCAN_RETRIES + 1 attempts) before falling
        # back -- not abandoned after one failure.
        self.assertEqual(calls["alldrives"], self.mod.USAGE_SCAN_RETRIES + 1)
        self.assertEqual(usage["D1"], {"bytes": 5, "objects": 1,
                                       "trash_bytes": 0, "trash_objects": 0})
        self.assertEqual(usage["D2"], {"bytes": 0, "objects": 0,
                                       "trash_bytes": 0, "trash_objects": 0})

    def test_alldrives_incomplete_search_falls_back_to_per_drive(self):
        def fake_urlopen(req, *a, **kw):
            url = req.full_url
            if "corpora=allDrives" in url:
                # a truncated/inconsistent result, not an error
                return self._resp({"files": [], "incompleteSearch": True})
            return self._per_drive_reply(url)

        usage = self._run_with_urlopen(fake_urlopen)
        self.assertEqual(usage["D1"], {"bytes": 5, "objects": 1,
                                       "trash_bytes": 0, "trash_objects": 0})
        self.assertEqual(usage["D2"], {"bytes": 0, "objects": 0,
                                       "trash_bytes": 0, "trash_objects": 0})

    def test_scan_buckets_content_vs_trash_and_query_drops_trashed_false(self):
        """The single scan buckets each file by its 'trashed' flag into
        content vs. trash, and the files.list query drops "trashed = false"
        (so trashed files are returned) while adding 'trashed' to the mask."""
        seen = {"urls": []}

        def fake_urlopen(req, *a, **kw):
            url = req.full_url
            seen["urls"].append(url)
            if "corpora=allDrives" in url:
                return self._resp({"files": [
                    {"driveId": "D1", "size": "5"},
                    {"driveId": "D1", "size": "7", "trashed": True},
                    {"driveId": "D2", "size": "3", "trashed": False},
                ]})
            return self._per_drive_reply(url)

        usage = self._run_with_urlopen(fake_urlopen)
        self.assertEqual(usage["D1"], {"bytes": 5, "objects": 1,
                                       "trash_bytes": 7, "trash_objects": 1})
        self.assertEqual(usage["D2"], {"bytes": 3, "objects": 1,
                                       "trash_bytes": 0, "trash_objects": 0})
        # 'bytes'/'objects' stay CONTENT-only; the trashed file rode into
        # trash_*, proving the query no longer filters trashed out.
        all_url = next(u for u in seen["urls"] if "corpora=allDrives" in u)
        self.assertIn("trashed", all_url)          # in the fields mask
        self.assertNotIn("trashed+%3D+false", all_url)  # q clause gone

    def test_drive_id_scoped_scan_returns_four_key_record(self):
        """A drive_id-scoped scan (pick_destination's pre-flight) still returns
        the 4-key record via the setdefault zero-fill."""
        def fake_urlopen(req, *a, **kw):
            return self._per_drive_reply(req.full_url)

        with mock.patch.object(self.mod, "get_drives",
                               return_value=self.drives), \
                mock.patch.object(self.mod, "get_token", return_value="tok"), \
                mock.patch.object(self.mod.urllib.request, "urlopen",
                                  fake_urlopen), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TODRIVE_TEST_FAKE_USAGE", None)
            usage = self.mod.drive_usage({}, drive_id="D1")
        self.assertEqual(usage["D1"], {"bytes": 5, "objects": 1,
                                       "trash_bytes": 0, "trash_objects": 0})


# --- FEATURE B: pick_destination cap pre-flight -----------------------------

class TestPickDestination(unittest.TestCase):
    """pick_destination()'s cap pre-flight routing, exercised in-process via
    the TODRIVE_TEST_FAKE_DRIVES/_USAGE/_CREATE hooks -- no network, no
    rclone. get_token has no test hook (unlike get_drives/create_drive), so
    any scenario that mints a genuinely NEW drive also mocks it directly."""

    def setUp(self):
        self.mod = _load_todrive()
        self.cap = self.mod.SHARED_DRIVE_CAP_BYTES

    def _env(self, drives, usage, create="1"):
        return {"TODRIVE_TEST_FAKE_DRIVES": json.dumps(drives),
                "TODRIVE_TEST_FAKE_USAGE": json.dumps(usage),
                "TODRIVE_TEST_FAKE_CREATE": create}

    def test_fits_returns_unchanged(self):
        env = self._env([{"id": "D1", "name": "Movies"}],
                        {"D1": {"bytes": 10, "objects": 1}})
        with mock.patch.dict(os.environ, env):
            name, drive_id = self.mod.pick_destination({}, "Movies", 5, {})
        self.assertEqual((name, drive_id), ("Movies", "D1"))

    def test_full_mints_and_chooses_overflow(self):
        env = self._env([{"id": "D1", "name": "Movies"}],
                        {"D1": {"bytes": self.cap, "objects": 1}})
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.mod, "get_token", return_value="tok"):
            name, drive_id = self.mod.pick_destination({}, "Movies", 1, {})
        self.assertEqual(name, "Movies overflow")
        self.assertEqual(drive_id, "NEW999")   # TODRIVE_TEST_FAKE_CREATE=1

    def test_overflow_also_full_advances_to_overflow_2(self):
        env = self._env(
            [{"id": "D1", "name": "Movies"},
             {"id": "D2", "name": "Movies overflow"}],
            {"D1": {"bytes": self.cap, "objects": 1},
             "D2": {"bytes": self.cap, "objects": 1}})
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.mod, "get_token", return_value="tok"):
            name, drive_id = self.mod.pick_destination({}, "Movies", 1, {})
        self.assertEqual(name, "Movies overflow 2")
        self.assertEqual(drive_id, "NEW999")

    def test_grandfathered_base_routes_onward_base_cache_untouched(self):
        env = self._env([{"id": "D1", "name": "Old"}],
                        {"D1": {"bytes": self.cap + 50 * 1000**3,
                                "objects": 9}})
        cache = {}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.mod, "get_token", return_value="tok"):
            name, drive_id = self.mod.pick_destination({}, "Old", 1, cache)
        self.assertEqual(name, "Old overflow")
        self.assertEqual(drive_id, "NEW999")
        # never shrunk/blocked/warned -- the base's measured usage in the
        # cache is left exactly as scanned.
        self.assertEqual(cache["D1"], self.cap + 50 * 1000**3)

    def test_payload_over_cap_raises_before_any_create(self):
        # An oversized payload can't fit any drive: pick_destination raises
        # DestinationError (a recoverable per-src failure cmd_up catches),
        # NOT a bare sys.exit that would abort a whole multi-src batch.
        env = self._env([{"id": "D1", "name": "Movies"}], {})
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.mod, "create_drive") as create, \
                self.assertRaises(self.mod.DestinationError):
            self.mod.pick_destination({}, "Movies", self.cap + 1, {})
        create.assert_not_called()

    def test_chain_cap_respected(self):
        # every link from the root through well past OVERFLOW_CHAIN_MAX
        # pre-exists and is already full, so pick_destination must give up
        # (raise DestinationError) rather than loop forever or silently
        # succeed past the bound.
        drives = [{"id": "D0", "name": "Movies"},
                  {"id": "D1", "name": "Movies overflow"}]
        usage = {"D0": {"bytes": self.cap, "objects": 1},
                 "D1": {"bytes": self.cap, "objects": 1}}
        for i in range(2, self.mod.OVERFLOW_CHAIN_MAX + 5):
            did = "D%d" % i
            drives.append({"id": did, "name": "Movies overflow %d" % i})
            usage[did] = {"bytes": self.cap, "objects": 1}
        env = self._env(drives, usage)
        with mock.patch.dict(os.environ, env), \
                self.assertRaises(self.mod.DestinationError):
            self.mod.pick_destination({}, "Movies", 1, {})


# --- FEATURE B: --no-overflow reproduces byte-identical legacy routing ------

class TestNoOverflowLegacyPath(unittest.TestCase):
    """--no-overflow must reproduce today's exact pre-overflow-routing
    behavior: one fixed resolve_id call outside the per-src loop, no cap
    check, no ROUTED line -- pick_destination must not even be consulted."""

    def setUp(self):
        self.mod = _load_todrive()
        self.tmp = tempfile.mkdtemp()
        self.cfg = dict(self.mod.DEFAULT_CONFIG)

    def test_no_overflow_skips_pick_destination_entirely(self):
        import argparse
        import contextlib
        import io
        src = os.path.join(self.tmp, "f.mkv")
        with open(src, "w") as f:
            f.write("x")
        args = argparse.Namespace(paths=[src, "Movies"], keep=False,
                                  allow_partial=False, no_overflow=True)
        resolve_calls = []

        def fake_resolve(cfg, name, create):
            resolve_calls.append(name)
            return "D1"

        def fake_pick(*a, **k):
            raise AssertionError("pick_destination must not be called "
                                 "under --no-overflow")

        class FakeProc:
            def __init__(self, cmd, **kw):
                self.stderr = iter(())

            def wait(self):
                return 0

        buf = io.StringIO()
        with mock.patch.object(self.mod, "resolve_id", fake_resolve), \
                mock.patch.object(self.mod, "pick_destination", fake_pick), \
                mock.patch.object(self.mod.subprocess, "Popen",
                                  lambda cmd, **kw: FakeProc(cmd)), \
                contextlib.redirect_stdout(buf):
            rc = self.mod.cmd_up(self.cfg, args)
        self.assertEqual(rc, 0)
        self.assertEqual(resolve_calls, ["Movies"])   # exactly once
        self.assertNotIn("ROUTED:", buf.getvalue())


# --- FEATURE B: cmd_up mid-upload quota reroute -----------------------------

FAKE_RCLONE_QUOTA_THEN_OK = r'''#!/usr/bin/env python3
import os, sys
log = os.environ["FAKE_RCLONE_LOG"]
with open(log, "a") as f:
    f.write("\t".join(sys.argv[1:]) + "\n")
with open(log) as f:
    n = sum(1 for _ in f)
if n == 1:
    sys.stderr.write("Error 403: storageQuotaExceeded\n")
    sys.exit(3)
sys.exit(0)
'''

FAKE_RCLONE_NONQUOTA_FAIL = r'''#!/usr/bin/env python3
import os, sys
log = os.environ["FAKE_RCLONE_LOG"]
with open(log, "a") as f:
    f.write("\t".join(sys.argv[1:]) + "\n")
sys.stderr.write("Error: connection reset by peer\n")
sys.exit(1)
'''


class TestCmdUpMidUploadReroute(unittest.TestCase):
    """cmd_up's mid-upload quota-marker reroute (FEATURE B), against a fake
    rclone binary (TestRunTodriveUp fake-binary pattern) that records argv
    and controls its own exit code/stderr per invocation."""

    def setUp(self):
        import argparse
        self.argparse = argparse
        self.mod = _load_todrive()
        self.tmp = tempfile.mkdtemp()
        self.cfg = dict(self.mod.DEFAULT_CONFIG)
        self.fakebin = os.path.join(self.tmp, "fake_rclone")
        self.logpath = os.path.join(self.tmp, "calls.log")

    def _write_fake(self, script):
        with open(self.fakebin, "w") as f:
            f.write(script)
        os.chmod(self.fakebin, 0o755)

    def _args(self, src):
        return self.argparse.Namespace(paths=[src, "Movies"], keep=False,
                                       allow_partial=False,
                                       no_overflow=False)

    def test_quota_error_reroutes_to_overflow_then_succeeds(self):
        import contextlib
        import io
        self._write_fake(FAKE_RCLONE_QUOTA_THEN_OK)
        src = os.path.join(self.tmp, "movie.mkv")
        with open(src, "w") as f:
            f.write("x")
        env = {"TODRIVE_TEST_FAKE_DRIVES": json.dumps(
                   [{"id": "D1", "name": "Movies"}]),
               "TODRIVE_TEST_FAKE_USAGE": json.dumps(
                   {"D1": {"bytes": 0, "objects": 0}}),
               "TODRIVE_TEST_FAKE_CREATE": "1",
               "FAKE_RCLONE_LOG": self.logpath}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.mod, "RCLONE", self.fakebin), \
                mock.patch.object(self.mod, "get_token", return_value="tok"), \
                contextlib.redirect_stdout(buf):
            rc = self.mod.cmd_up(self.cfg, self._args(src))
        self.assertEqual(rc, 0)
        self.assertIn("ROUTED: %s -> Movies overflow" % src, buf.getvalue())
        with open(self.logpath) as f:
            calls = f.read().splitlines()
        self.assertEqual(len(calls), 2)               # retried exactly once
        self.assertNotIn("team_drive=NEW999", calls[0])
        self.assertIn("team_drive=NEW999", calls[1])   # 2nd hits the overflow

    def test_non_quota_error_does_not_reroute(self):
        import contextlib
        import io
        self._write_fake(FAKE_RCLONE_NONQUOTA_FAIL)
        src = os.path.join(self.tmp, "movie2.mkv")
        with open(src, "w") as f:
            f.write("x")
        env = {"TODRIVE_TEST_FAKE_DRIVES": json.dumps(
                   [{"id": "D1", "name": "Movies"}]),
               "TODRIVE_TEST_FAKE_USAGE": json.dumps(
                   {"D1": {"bytes": 0, "objects": 0}}),
               "FAKE_RCLONE_LOG": self.logpath}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.mod, "RCLONE", self.fakebin), \
                mock.patch.object(self.mod, "create_drive") as create, \
                contextlib.redirect_stdout(buf):
            rc = self.mod.cmd_up(self.cfg, self._args(src))
        self.assertEqual(rc, 1)                        # today's behavior
        self.assertNotIn("ROUTED:", buf.getvalue())
        create.assert_not_called()
        with open(self.logpath) as f:
            calls = f.read().splitlines()
        self.assertEqual(len(calls), 1)                # no retry


# --- run_todrive_up: a ROUTED line survives the progress-noise filter ------

FAKE_TODRIVE_ROUTED = r'''#!/usr/bin/env python3
import sys
sys.stdout.write("move /dl/x -> gdrive:\n")
sys.stdout.write("Transferred:   \t 0 B / 1.9 GiB, 0%\n")
sys.stdout.write("ROUTED: /dl/x -> Movies overflow\n")
sys.stdout.write("Transferred:   \t 1.9 GiB / 1.9 GiB, 100%, "
                 "10.0 MiB/s, ETA 0s\n")
sys.stdout.write("OK: /dl/x\n")
sys.exit(0)
'''


class TestRunTodriveUpRoutedPassthrough(unittest.TestCase):
    """Regression: a `ROUTED: <src> -> <final>` line printed by todrive
    mid-stream (FEATURE B) must survive run_todrive_up's noise-collapsing
    filter (_PROGRESS_NOISE_RE) so _upload_worker can see it in
    combined_output -- it must not be eaten as a stray progress line."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fake = os.path.join(self.tmp, "todrive")
        with open(fake, "w") as f:
            f.write(FAKE_TODRIVE_ROUTED)
        self._orig = app.TODRIVE
        app.TODRIVE = fake

    def tearDown(self):
        app.TODRIVE = self._orig

    def test_routed_line_survives_progress_noise_filter(self):
        rc, out = app.run_todrive_up("/dl/x", "Movies", cwd=self.tmp)
        self.assertEqual(rc, 0)
        self.assertIn("ROUTED: /dl/x -> Movies overflow", out.splitlines())


# --- offload_app.list_drive_usage subprocess round-trip ---------------------

class TestListDriveUsage(unittest.TestCase):
    """list_drive_usage() round-trips through the real todrive subprocess
    using the TODRIVE_TEST_FAKE_DRIVES/_USAGE hooks (TestDriveHas idiom): no
    network, no rclone."""

    def _call(self, drives_json, usage_json):
        env = {"TODRIVE_TEST_FAKE_DRIVES": drives_json,
               "TODRIVE_TEST_FAKE_USAGE": usage_json,
               "TODRIVE_CONFIG": os.path.join(tempfile.mkdtemp(), "no.json")}
        with mock.patch.dict(os.environ, env):
            return app.list_drive_usage()

    def test_success_round_trip(self):
        data = self._call(
            json.dumps([{"id": "D1", "name": "Movies"}]),
            json.dumps({"D1": {"bytes": 50 * 1000**3, "objects": 4}}))
        self.assertIsNotNone(data)
        self.assertEqual(data["cap_bytes"], 100 * 1000**3)
        self.assertEqual(data["drives"][0]["name"], "Movies")
        self.assertEqual(data["drives"][0]["bytes"], 50 * 1000**3)

    def test_trash_keys_survive_round_trip(self):
        data = self._call(
            json.dumps([{"id": "D1", "name": "DriveH"}]),
            json.dumps({"D1": {"bytes": 0, "objects": 0,
                               "trash_bytes": 56 * 1000**3,
                               "trash_objects": 10}}))
        self.assertIsNotNone(data)
        self.assertEqual(data["drives"][0]["trash_bytes"], 56 * 1000**3)
        self.assertEqual(data["drives"][0]["trash_objects"], 10)

    def test_todrive_error_returns_none_and_logs(self):
        tmp = tempfile.mkdtemp()
        orig_log = app.LOG_FILE
        app.LOG_FILE = os.path.join(tmp, "app.log")
        try:
            data = self._call("{not valid json", "{}")
            self.assertIsNone(data)
            with open(app.LOG_FILE) as f:
                logged = f.read()
            self.assertIn("list_drive_usage error", logged)
        finally:
            app.LOG_FILE = orig_log


# --- FEATURE B: ROUTED handling at the Poller/DecisionStore level ----------

class TestRoutedUploadHandling(unittest.TestCase):
    """The DecisionStore/Poller-level effect of _upload_worker's rc==0 ROUTED
    branch (offload_app.py ~1958-1968): parse the LAST 'ROUTED: ' line from
    the upload's output and, if the final drive differs from the one asked
    for, repoint the stored choice before marking the decision handled.

    _upload_worker itself lives on the rumps-guarded OffloadApp class, which
    can't be instantiated headlessly here (rumps is not installed in this
    test environment -- the same reason no existing test in this file ever
    constructs OffloadApp; _run_app() imports rumps lazily precisely so the
    rest of the module stays importable/testable without it). This exercises
    the exact sequence of DecisionStore/Poller calls _upload_worker performs,
    mirroring the TestPollSequence fixtures used throughout this file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = app.DecisionStore(os.path.join(self.tmp, "d.json"))

    def _handle_upload_result(self, gid, drive, output, poller):
        final_drive = drive
        for line in output.splitlines():
            if line.startswith("ROUTED: "):
                final_drive = line.rsplit(" -> ", 1)[-1].strip()
        if final_drive != drive:
            self.store.set_choice(gid, "drive:%s" % final_drive)
        poller.upload_done(gid, success=True)
        return final_drive

    def test_routed_output_repoints_choice_and_marks_handled(self):
        poller = app.Poller(app.Aria2Client(port=1), self.store,
                            ask_cb=lambda g, n: "local",
                            upload_cb=lambda *a: None)
        self.store.record("gid1", "Movie", "drive:Movies", handled=False)
        output = ("move /p -> gdrive:\n"
                  "ROUTED: x -> Movies overflow\n"
                  "OK: /p")
        final = self._handle_upload_result("gid1", "Movies", output, poller)
        self.assertEqual(final, "Movies overflow")
        rec = self.store.get("gid1")
        self.assertEqual(rec["choice"], "drive:Movies overflow")
        self.assertTrue(rec["handled"])

    def test_control_run_without_routed_leaves_choice_untouched(self):
        poller = app.Poller(app.Aria2Client(port=1), self.store,
                            ask_cb=lambda g, n: "local",
                            upload_cb=lambda *a: None)
        self.store.record("gid2", "Movie2", "drive:Movies", handled=False)
        output = "move /p2 -> gdrive:\nOK: /p2"
        final = self._handle_upload_result("gid2", "Movies", output, poller)
        self.assertEqual(final, "Movies")
        rec = self.store.get("gid2")
        self.assertEqual(rec["choice"], "drive:Movies")   # untouched
        self.assertTrue(rec["handled"])


# --- FEATURE A: AppState.drive_usage persistence ----------------------------

class TestAppStateDriveUsage(unittest.TestCase):
    def test_default_none(self):
        tmp = tempfile.mkdtemp()
        state = app.AppState(os.path.join(tmp, "state.json"))
        self.assertIsNone(state.drive_usage)

    def test_setter_persists_and_survives_reload(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "state.json")
        state = app.AppState(path)
        payload = {"ts": 12345, "cap_bytes": 100 * 1000**3,
                   "drives": [{"id": "D1", "name": "Movies", "bytes": 1,
                              "objects": 1, "trash_bytes": 2,
                              "trash_objects": 1, "pct": 0.0}]}
        state.drive_usage = payload
        self.assertEqual(state.drive_usage, payload)
        # a fresh AppState instance (the next app run) reads it back
        state2 = app.AppState(path)
        self.assertEqual(state2.drive_usage, payload)


class TestFormatStorageTable(unittest.TestCase):
    """format_storage_table() is PURE (no rumps/AppKit, no I/O) so it is
    exercised directly. _storage_menu is a thin monospaced-font wrapper over
    it (the NSAttributedString rendering can't be unit-tested headless)."""

    CAP = 100 * 1000**3

    def test_empty_and_fully_filtered_return_empty_list(self):
        self.assertEqual(app.format_storage_table([], self.CAP), [])
        # content==0 AND trash==0 -> filtered out -> nothing survives
        self.assertEqual(app.format_storage_table(
            [{"name": "Empty", "bytes": 0, "trash_bytes": 0}], self.CAP), [])

    def test_trash_only_drive_passes_filter_with_headroom(self):
        # DriveH: 0 content + 56 GB trash -> shown, FREE = cap - trash = 44 GB
        rows = app.format_storage_table(
            [{"name": "DriveH", "bytes": 0, "trash_bytes": 56 * 1000**3}],
            self.CAP)
        drive_row = rows[1]
        self.assertIn("DriveH", drive_row)
        self.assertIn("56.0 GB", drive_row)   # TRASH
        self.assertIn("44.0 GB", drive_row)   # FREE headroom

    def test_sort_total_desc_with_name_tiebreak(self):
        drives = [
            {"name": "Small", "bytes": 10 * 1000**3, "trash_bytes": 0},
            {"name": "Big", "bytes": 80 * 1000**3, "trash_bytes": 0},
            {"name": "Mid", "bytes": 40 * 1000**3, "trash_bytes": 0},
        ]
        rows = app.format_storage_table(drives, self.CAP)
        body = rows[1:4]
        self.assertTrue(body[0].startswith("Big"))
        self.assertTrue(body[1].startswith("Mid"))
        self.assertTrue(body[2].startswith("Small"))

    def test_over_cap_and_exact_cap_free_markers(self):
        drives = [
            {"name": "Over", "bytes": 120 * 1000**3, "trash_bytes": 0},
            {"name": "AtCap", "bytes": self.CAP, "trash_bytes": 0},
        ]
        rows = app.format_storage_table(drives, self.CAP)
        over_row = next(r for r in rows if r.startswith("Over"))
        atcap_row = next(r for r in rows if r.startswith("AtCap"))
        self.assertTrue(over_row.rstrip().endswith("over"))
        self.assertTrue(atcap_row.rstrip().endswith("0.0 B"))  # not "over"

    def test_cap_none_renders_every_free_as_question_mark(self):
        drives = [{"name": "A", "bytes": 5, "trash_bytes": 0},
                  {"name": "B", "bytes": 7, "trash_bytes": 3}]
        rows = app.format_storage_table(drives, None)
        for r in rows[1:3]:   # the two drive rows
            self.assertTrue(r.rstrip().endswith("?"))

    def test_pre_trash_snapshot_degrades_without_keyerror(self):
        # a persisted snapshot lacking trash_bytes -> TRASH 0, FREE = cap-content
        rows = app.format_storage_table(
            [{"name": "Movies", "bytes": 40 * 1000**3}], self.CAP)
        drive_row = rows[1]
        self.assertIn("0.0 B", drive_row)     # TRASH defaulted
        self.assertIn("60.0 GB", drive_row)   # FREE = 100 - 40

    def test_footer_aggregates_and_blank_free(self):
        drives = [
            {"name": "DriveH", "bytes": 0, "trash_bytes": 56 * 1000**3},
            {"name": "Movies", "bytes": 80 * 1000**3,
             "trash_bytes": 5 * 1000**3},
        ]
        rows = app.format_storage_table(drives, self.CAP)
        footer = rows[-2]   # last table line before the reclaimable note
        self.assertIn("TOTAL (2)", footer)
        self.assertIn("80.0 GB", footer)    # summed CONTENT
        self.assertIn("61.0 GB", footer)    # summed TRASH
        self.assertIn("141.0 GB", footer)   # summed TOTAL
        # FREE column blank in the footer: no per-drive-pool sum
        self.assertFalse(footer.rstrip().endswith("over"))

    def test_reclaimable_note_present_iff_trash(self):
        with_trash = app.format_storage_table(
            [{"name": "DriveH", "bytes": 0, "trash_bytes": 56 * 1000**3}],
            self.CAP)
        self.assertTrue(with_trash[-1].startswith("Trash reclaimable:"))
        self.assertIn("56.0 GB", with_trash[-1])
        no_trash = app.format_storage_table(
            [{"name": "Movies", "bytes": 40 * 1000**3, "trash_bytes": 0}],
            self.CAP)
        self.assertFalse(any(l.startswith("Trash reclaimable:")
                             for l in no_trash))

    def test_all_table_lines_equal_length_and_aligned(self):
        drives = [
            {"name": "DriveH", "bytes": 0, "trash_bytes": 56 * 1000**3},
            {"name": "TV Shows", "bytes": 120 * 1000**3, "trash_bytes": 0},
            {"name": "Movies", "bytes": 80 * 1000**3,
             "trash_bytes": 5 * 1000**3},
        ]
        rows = app.format_storage_table(drives, self.CAP)
        # the reclaimable note is freeform; every OTHER line is a table line
        table = [l for l in rows if not l.startswith("Trash reclaimable:")]
        # Uniform column widths + right-aligned numeric columns mean every
        # table line is the same length; that identical length IS the column
        # alignment (each numeric column ends at a fixed index off the end).
        widths = {len(l) for l in table}
        self.assertEqual(len(widths), 1, "table lines differ in length")
        header = table[0]
        # separator is a full-width run of '-'
        sep = next(l for l in table if set(l) == {"-"})
        self.assertEqual(len(sep), len(header))
        # Header + drive rows have their FREE value flush against the right
        # edge (last char non-space); the footer's FREE is deliberately blank.
        drive_rows = [l for l in table[1:] if set(l) != {"-"}
                      and not l.startswith("TOTAL (")]
        for l in [header] + drive_rows:
            self.assertNotEqual(l[-1], " ", "FREE not right-aligned: %r" % l)

    # ---- sorting (sort_key + descending) ----

    def _drive_rows(self, rows):
        """Just the per-drive table rows: drop header, separator, footer, note."""
        return [l for l in rows[1:]
                if set(l.strip()) != {"-"}
                and not l.startswith("TOTAL (")
                and not l.startswith("Trash reclaimable:")]

    def test_sort_by_name_both_directions(self):
        drives = [
            {"name": "Charlie", "bytes": 10 * 1000**3, "trash_bytes": 0},
            {"name": "alpha", "bytes": 80 * 1000**3, "trash_bytes": 0},
            {"name": "Bravo", "bytes": 40 * 1000**3, "trash_bytes": 0},
        ]
        asc = self._drive_rows(app.format_storage_table(
            drives, self.CAP, sort_key="name", descending=False))
        self.assertTrue(asc[0].startswith("alpha"))   # case-insensitive
        self.assertTrue(asc[1].startswith("Bravo"))
        self.assertTrue(asc[2].startswith("Charlie"))
        desc = self._drive_rows(app.format_storage_table(
            drives, self.CAP, sort_key="name", descending=True))
        self.assertTrue(desc[0].startswith("Charlie"))
        self.assertTrue(desc[2].startswith("alpha"))

    def test_sort_by_content_vs_trash_differ(self):
        drives = [
            {"name": "A", "bytes": 90 * 1000**3, "trash_bytes": 1 * 1000**3},
            {"name": "B", "bytes": 5 * 1000**3, "trash_bytes": 70 * 1000**3},
        ]
        by_content = self._drive_rows(app.format_storage_table(
            drives, self.CAP, sort_key="content", descending=True))
        self.assertTrue(by_content[0].startswith("A"))    # 90 > 5 content
        by_trash = self._drive_rows(app.format_storage_table(
            drives, self.CAP, sort_key="trash", descending=True))
        self.assertTrue(by_trash[0].startswith("B"))      # 70 > 1 trash

    def test_sort_by_free_direction(self):
        drives = [
            {"name": "Full", "bytes": 95 * 1000**3, "trash_bytes": 0},   # free 5
            {"name": "Over", "bytes": 130 * 1000**3, "trash_bytes": 0},  # free -30
            {"name": "Roomy", "bytes": 5 * 1000**3, "trash_bytes": 0},   # free 95
        ]
        asc = self._drive_rows(app.format_storage_table(
            drives, self.CAP, sort_key="free", descending=False))
        self.assertTrue(asc[0].startswith("Over"))    # least (negative) free first
        self.assertTrue(asc[1].startswith("Full"))
        self.assertTrue(asc[2].startswith("Roomy"))   # most free last
        desc = self._drive_rows(app.format_storage_table(
            drives, self.CAP, sort_key="free", descending=True))
        self.assertTrue(desc[0].startswith("Roomy"))  # most free first

    def test_sort_unknown_key_falls_back_to_total(self):
        drives = [
            {"name": "Small", "bytes": 10 * 1000**3, "trash_bytes": 0},
            {"name": "Big", "bytes": 80 * 1000**3, "trash_bytes": 0},
        ]
        rows = self._drive_rows(app.format_storage_table(
            drives, self.CAP, sort_key="bogus", descending=True))
        self.assertTrue(rows[0].startswith("Big"))

    def test_sort_numeric_tiebreak_stays_name_asc_both_directions(self):
        drives = [
            {"name": "Zeta", "bytes": 20 * 1000**3, "trash_bytes": 0},
            {"name": "Alpha", "bytes": 20 * 1000**3, "trash_bytes": 0},
        ]
        for desc in (True, False):
            rows = self._drive_rows(app.format_storage_table(
                drives, self.CAP, sort_key="total", descending=desc))
            self.assertTrue(rows[0].startswith("Alpha"),
                            "equal totals must tiebreak name-asc (desc=%s)" % desc)

    def test_sort_by_free_with_cap_none_does_not_crash(self):
        drives = [
            {"name": "A", "bytes": 10 * 1000**3, "trash_bytes": 0},
            {"name": "B", "bytes": 80 * 1000**3, "trash_bytes": 0},
        ]
        rows = self._drive_rows(app.format_storage_table(
            drives, None, sort_key="free", descending=True))
        # cap None -> free falls back to -total; desc -> smallest total first
        self.assertTrue(rows[0].startswith("A"))


# --- partial-torrent mode (season-by-season downloads) ----------------------
#
# A multi-season torrent where the user ticks one season at a time: the
# selected files finish and the torrent reads "complete" although other files
# are wanted=false. Those must be COPIED to the drive as they finish (never
# moved, torrent untouched); only the final all-selected pass moves.

class PartialFakeClient:
    """Minimal Transmission-shaped client: tell_all returns adapted torrents."""
    def __init__(self, engine):
        self.dls = []
        self.engine = engine

    def is_up(self):
        return True

    def tell_all(self):
        return list(self.dls)

    def for_gid(self, gid):
        return self.engine


_PT_FILES = ["S1/e1.mkv", "S1/e2.mkv", "S2/e1.mkv", "S2/e2.mkv",
             "S3/e1.mkv", "S3/e2.mkv"]
_PT_TOP = "Show (2007)"


class PartialBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._old_log = app.LOG_FILE
        app.LOG_FILE = os.path.join(self.tmp, "app.log")
        self.addCleanup(setattr, app, "LOG_FILE", self._old_log)
        self.dpath = os.path.join(self.tmp, "decisions.json")
        self.store = app.DecisionStore(self.dpath)
        self.calls = []
        self.client = PartialFakeClient(RecordingEngine(self.calls))
        self.now = [1000.0]
        self.moves = []      # 4-arg whole-dir upload_cb dispatches
        self.partials = []   # partial_upload_cb dispatches
        self.notes = []
        self.poller = self._poller()
        self.store.record("tm-pt1", _PT_TOP, "drive:Films")

    def _poller(self, store=None, with_partial_cb=True):
        return app.Poller(
            self.client, store or self.store,
            ask_cb=lambda g, n: "local",
            upload_cb=lambda *a: self.moves.append(a),
            notify_cb=lambda *a: self.notes.append(a),
            now_fn=lambda: self.now[0],
            partial_upload_cb=((lambda *a: self.partials.append(a))
                               if with_partial_cb else None))

    def top(self):
        return os.path.join(self.tmp, _PT_TOP)

    def write(self, rel, size=10, part=False):
        p = os.path.join(self.top(), rel + (".part" if part else ""))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        return p

    def torrent(self, wanted, done, status=4, left=None):
        """wanted/done: sets of files. Done files also get real bytes on disk;
        a wanted-but-unfinished file only gets a '.part' (Transmission style)."""
        files, stats = [], []
        for rel in _PT_FILES:
            files.append({"name": "%s/%s" % (_PT_TOP, rel), "length": 10,
                          "bytesCompleted": 10 if rel in done else 3})
            stats.append({"wanted": rel in wanted})
        if left is None:
            left = 10 * len([r for r in wanted if r not in done])
        return _torrent(hashString="pt1", name=_PT_TOP, status=status,
                        downloadDir=self.tmp, files=files, fileStats=stats,
                        totalSize=60, sizeWhenDone=10 * len(wanted),
                        leftUntilDone=left,
                        percentDone=1.0 if left == 0 else 0.5)

    def set_state(self, wanted, done, **kw):
        for rel in _PT_FILES:
            if rel in done:
                self.write(rel)
                try:
                    os.remove(os.path.join(self.top(), rel + ".part"))
                except OSError:
                    pass
            elif rel in wanted:
                self.write(rel, size=3, part=True)
        self.client.dls = [app.TransmissionClient._adapt(
            self.torrent(set(wanted), set(done), **kw))]

    def finish_partial(self, success=True, quota=False):
        """Play the worker: report the last dispatched batch's outcome."""
        _path, drive, gid, _name, rels, _pinned = self.partials[-1]
        final = app.parse_routed_drive("", drive)
        self.poller.upload_partial_done(gid, success, rels, final, quota=quota)


S1 = {"S1/e1.mkv", "S1/e2.mkv"}
S2 = {"S2/e1.mkv", "S2/e2.mkv"}
S3 = {"S3/e1.mkv", "S3/e2.mkv"}


class TestPartialDetection(unittest.TestCase):
    def test_is_partial_selection(self):
        f = lambda sel: {"path": "/x", "selected": sel}  # noqa: E731
        tm = lambda *fs: {"engine": "transmission", "files": list(fs)}  # noqa: E731
        self.assertTrue(app.is_partial_selection(tm(f("true"), f("false"))))
        self.assertFalse(app.is_partial_selection(tm(f("true"), f("true"))))
        self.assertFalse(app.is_partial_selection(tm(f("false"))))
        self.assertFalse(app.is_partial_selection(tm(f("true"))))
        self.assertFalse(app.is_partial_selection({}))

    def test_aria2_shape_is_never_partial(self):
        # aria2/Motrix: deselected extras are normal; whole-dir move applies.
        f = lambda sel: {"path": "/x", "selected": sel}  # noqa: E731
        self.assertFalse(app.is_partial_selection(
            {"files": [f("true"), f("false")]}))

    def test_adapt_tags_engine(self):
        self.assertEqual(app.TransmissionClient._adapt(_torrent())["engine"],
                         "transmission")

    def test_adapt_carries_per_file_completed_length(self):
        dl = app.TransmissionClient._adapt(_torrent(
            files=[{"name": "a/x", "length": 9, "bytesCompleted": 4},
                   {"name": "a/y", "length": 9}],
            fileStats=[{"wanted": True}, {"wanted": True, "bytesCompleted": 9}]))
        self.assertEqual(dl["files"][0]["completedLength"], 4)
        self.assertEqual(dl["files"][1]["completedLength"], 9)  # fileStats
        dl = app.TransmissionClient._adapt(_torrent())
        self.assertNotIn("completedLength", dl["files"][0])  # unknown != done


class TestPendingPartialFiles(PartialBase):
    def _pending(self, wanted, done, uploaded=()):
        self.set_state(wanted, done)
        dl = self.client.dls[0]
        return app.pending_partial_files(dl, self.top(), uploaded)

    def test_only_selected_complete_files(self):
        self.assertEqual(self._pending(S1 | S3, S1),
                         ["S1/e1.mkv", "S1/e2.mkv"])

    def test_part_sibling_excludes_file(self):
        self.set_state(S1, S1)
        self.write("S1/e2.mkv", part=True)   # marker next to a "complete" file
        dl = self.client.dls[0]
        self.assertEqual(app.pending_partial_files(dl, self.top()),
                         ["S1/e1.mkv"])

    def test_incomplete_bytes_exclude_file(self):
        self.set_state(S1, S1)
        dl = self.client.dls[0]
        dl["files"][1]["completedLength"] = 5     # engine says 5 of 10
        self.assertEqual(app.pending_partial_files(dl, self.top()),
                         ["S1/e1.mkv"])

    def test_unknown_progress_or_missing_file_excluded(self):
        self.set_state(S1, S1)
        dl = self.client.dls[0]
        del dl["files"][0]["completedLength"]
        os.remove(os.path.join(self.top(), "S1/e2.mkv"))
        self.assertEqual(app.pending_partial_files(dl, self.top()), [])

    def test_truncated_file_on_disk_excluded(self):
        self.set_state(S1, S1)
        self.write("S1/e2.mkv", size=4)   # engine says 10/10, disk has 4
        self.assertEqual(app.pending_partial_files(
            self.client.dls[0], self.top()), ["S1/e1.mkv"])

    def test_rclone_mangled_names_deferred(self):
        # names --files-from would mangle are left to the final pass
        for name in ("#x.mkv", ";x.mkv", " x.mkv"):
            dl = {"files": [
                {"path": os.path.join(self.top(), name), "length": 10,
                 "completedLength": 10, "selected": "true"},
                {"path": os.path.join(self.top(), "ok.mkv"), "length": 10,
                 "completedLength": 10, "selected": "true"}]}
            self.write(name)
            self.write("ok.mkv")
            self.assertEqual(app.pending_partial_files(dl, self.top()),
                             ["ok.mkv"], name)

    def test_file_named_like_incomplete_marker_excluded(self):
        for name in ("x.mkv.part", "x.mkv.!qB"):
            dl = {"files": [
                {"path": os.path.join(self.top(), name), "length": 10,
                 "completedLength": 10, "selected": "true"},
                {"path": os.path.join(self.top(), "ok.mkv"), "length": 10,
                 "completedLength": 10, "selected": "true"}]}
            self.write(name)
            self.write("ok.mkv")
            self.assertEqual(app.pending_partial_files(dl, self.top()),
                             ["ok.mkv"], name)

    def test_dotdot_prefixed_name_is_not_an_escape(self):
        dl = {"files": [
            {"path": os.path.join(self.top(), "..hidden.mkv"), "length": 10,
             "completedLength": 10, "selected": "true"}]}
        self.write("..hidden.mkv")
        self.assertEqual(app.pending_partial_files(dl, self.top()),
                         ["..hidden.mkv"])
        dl["files"][0]["path"] = os.path.join(self.tmp, "elsewhere.mkv")
        self.assertEqual(app.pending_partial_files(dl, self.top()), [])

    def test_already_uploaded_excluded(self):
        self.assertEqual(self._pending(S1 | S2, S1 | S2, uploaded=S1),
                         ["S2/e1.mkv", "S2/e2.mkv"])


class TestPartialPoll(PartialBase):
    def test_s1_done_while_s3_downloading_copies_only_s1(self):
        # S1 finished, S3 still downloading (status active), S2 unselected.
        self.set_state(S1 | S3, S1, status=4)
        self.poller.poll_once()
        self.assertEqual(len(self.partials), 1)
        path, drive, gid, _name, rels, pinned = self.partials[0]
        self.assertEqual(path, self.top())
        self.assertEqual((drive, gid, pinned), ("Films", "tm-pt1", False))
        self.assertEqual(sorted(rels), ["S1/e1.mkv", "S1/e2.mkv"])
        # copy, never move: no whole-dir dispatch, no engine stop/remove
        self.assertEqual(self.moves, [])
        self.assertEqual(self.calls, [])
        self.assertFalse(self.store.get("tm-pt1")["handled"])

    def test_complete_status_also_copies_not_moves(self):
        # The reported bug: selection finished -> "complete" -> whole-dir move.
        self.set_state(S1, S1, status=6, left=0)
        self.assertEqual(self.client.dls[0]["status"], "complete")
        self.poller.poll_once()
        self.assertEqual(len(self.partials), 1)
        self.assertEqual(self.moves, [])
        self.assertEqual(self.calls, [])

    def test_in_flight_then_done_does_not_reupload(self):
        self.set_state(S1 | S3, S1)
        self.poller.poll_once()
        self.poller.poll_once()               # still mid-upload
        self.assertEqual(len(self.partials), 1)
        self.finish_partial()
        self.assertEqual(sorted(self.store.get("tm-pt1")["uploaded_files"]),
                         ["S1/e1.mkv", "S1/e2.mkv"])
        self.assertEqual(self.store.get("tm-pt1")["partial_drive"], "Films")
        self.poller.poll_once()
        self.poller.poll_once()
        self.assertEqual(len(self.partials), 1)   # nothing new -> nothing
        self.assertFalse(self.store.get("tm-pt1")["handled"])
        self.assertEqual([n[0] for n in self.notes], ["Partial upload"])
        self.assertIn("kept local", self.notes[0][2])

    def test_second_season_uploads_only_season_two(self):
        self.set_state(S1 | S3, S1)
        self.poller.poll_once()
        self.finish_partial()
        # user now selects S2 (S3 deselected); S2 finishes
        self.set_state(S1 | S2, S1 | S2, status=6, left=0)
        self.poller.poll_once()
        self.assertEqual(len(self.partials), 2)
        rels, pinned = self.partials[1][4], self.partials[1][5]
        self.assertEqual(sorted(rels), ["S2/e1.mkv", "S2/e2.mkv"])
        self.assertTrue(pinned)               # drive pinned after batch 1
        self.finish_partial()
        self.assertEqual(len(self.store.get("tm-pt1")["uploaded_files"]), 4)
        self.assertEqual(self.moves, [])
        self.assertEqual(self.calls, [])

    def test_later_batches_target_the_drive_batch_one_landed_on(self):
        self.set_state(S1 | S3, S1)
        self.poller.poll_once()
        _p, _d, gid, _n, rels, _pin = self.partials[0]
        self.poller.upload_partial_done(gid, True, rels, "Films 2")  # routed
        self.set_state(S1 | S2, S1 | S2)
        self.poller.poll_once()
        self.assertEqual(self.partials[1][1], "Films 2")   # not the choice
        self.assertTrue(self.partials[1][5])                # pinned

    def test_nothing_pending_does_nothing_and_not_handled(self):
        self.set_state(S1, set(), status=4)      # nothing finished yet
        self.poller.poll_once()
        self.assertEqual(self.partials, [])
        self.assertFalse(self.store.get("tm-pt1")["handled"])
        self.assertEqual(self.poller._uploading, set())

    def test_verifying_torrent_never_uploads(self):
        self.set_state(S1, S1, status=2)          # verifying -> "waiting"
        self.poller.poll_once()
        self.assertEqual(self.partials, [])
        self.assertEqual(self.moves, [])

    def test_local_choice_and_no_callback_never_move(self):
        self.set_state(S1, S1, status=6, left=0)
        p2 = self._poller(with_partial_cb=False)
        p2.poll_once()
        self.assertEqual((self.moves, self.partials), ([], []))
        self.assertFalse(self.store.get("tm-pt1")["handled"])

    def test_partial_uploader_error_releases_guard(self):
        self.set_state(S1, S1)
        def boom(*a):
            raise RuntimeError("x")
        self.poller.partial_upload_cb = boom
        self.poller.poll_once()
        self.assertEqual(self.poller._uploading, set())

    def test_final_pass_all_selected_moves_same_drive_no_rename(self):
        # batch 1 lands on an overflow drive
        self.set_state(S1 | S3, S1)
        self.poller.poll_once()
        _p, _d, gid, _n, rels, _pin = self.partials[0]
        self.poller.upload_partial_done(gid, True, rels, "Films 2")
        self.assertEqual(self.store.get(gid)["partial_drive"], "Films 2")
        # everything selected and complete -> not partial anymore
        everything = set(_PT_FILES)
        self.set_state(everything, everything, status=6, left=0)
        self.poller.poll_once()
        self.assertEqual(len(self.partials), 1)     # no more copies
        self.assertEqual(len(self.moves), 1)        # the existing move path
        path, drive, mgid, _name = self.moves[0]
        self.assertEqual((path, mgid), (self.top(), gid))
        self.assertEqual(drive, "Films 2")          # pinned to the copy drive
        # worker-side plan: skip the rename hook, pin the drive
        rename_cfg = {"enabled": True, "dry_run": False}
        plan = app.final_pass_plan(self.store.get(gid), drive, rename_cfg)
        self.assertEqual(plan, ("Films 2", None, True))
        # ...and perform_upload really does the whole-dir move with the pin
        seen = {}

        def fake_up(p, d, bw, progress_cb=None, **kw):
            seen.update(path=p, drive=d, kw=kw)
            return 0, ""
        rc, _ = app.perform_upload(
            self.client, path, plan[0], gid, todrive_up=fake_up,
            rename_cfg=plan[1], cache=None, no_overflow=plan[2])
        self.assertEqual(rc, 0)
        self.assertEqual(seen["path"], self.top())   # as-is, not renamed
        self.assertEqual(seen["kw"], {"no_overflow": True})
        self.assertEqual(self.calls, [("stop", gid), ("remove", gid)])

    def test_final_plan_unchanged_without_partial_uploads(self):
        cfg = {"enabled": True}
        self.assertEqual(app.final_pass_plan({"choice": "drive:A"}, "A", cfg),
                         ("A", cfg, False))
        self.assertEqual(app.final_pass_plan(None, "A", cfg), ("A", cfg, False))

    def test_non_partial_complete_behaves_as_before(self):
        everything = set(_PT_FILES)
        self.set_state(everything, everything, status=6, left=0)
        self.poller.poll_once()
        self.assertEqual(self.partials, [])
        self.assertEqual(len(self.moves), 1)
        self.assertEqual(self.moves[0][1], "Films")

    def test_failure_backoff_then_retry(self):
        self.set_state(S1 | S3, S1)
        self.poller.poll_once()
        self.finish_partial(success=False)
        rec = self.store.get("tm-pt1")
        self.assertEqual(rec["partial_failures"], 1)
        self.assertNotIn("uploaded_files", rec)     # written only on success
        self.assertGreater(rec["partial_next_attempt"], self.now[0])
        self.poller.poll_once()
        self.assertEqual(len(self.partials), 1)     # inside backoff window
        self.now[0] += app.UPLOAD_BACKOFF_BASE + 1
        self.poller.poll_once()
        self.assertEqual(len(self.partials), 2)     # retried
        self.finish_partial()                        # success resets state
        rec = self.store.get("tm-pt1")
        self.assertNotIn("partial_failures", rec)
        self.assertNotIn("partial_next_attempt", rec)

    def test_quota_failure_is_terminal_and_gives_up_after_max(self):
        self.set_state(S1, S1)
        self.poller.poll_once()
        self.finish_partial(success=False, quota=True)
        self.assertTrue(self.store.get("tm-pt1")["partial_failed"])
        self.poller.poll_once()
        self.assertEqual(len(self.partials), 1)
        # partial failure state is separate: it must NOT gate the final move
        rec = self.store.get("tm-pt1")
        self.assertTrue(rec["partial_failed"])
        self.assertNotIn("failed", rec)
        self.assertNotIn("failures", rec)
        everything = set(_PT_FILES)
        self.set_state(everything, everything, status=6, left=0)
        self.poller.poll_once()
        self.assertEqual(len(self.moves), 1)

    def test_partial_failures_do_not_spend_final_move_attempts(self):
        self.set_state(S1 | S3, S1)
        for _ in range(3):
            self.poller.poll_once()
            self.finish_partial(success=False)
            self.now[0] += 10 * app.UPLOAD_BACKOFF_CAP
        rec = self.store.get("tm-pt1")
        self.assertEqual(rec["partial_failures"], 3)
        self.assertNotIn("failures", rec)

    def test_aria2_deselected_files_still_moved(self):
        # Regression: aria2/Motrix torrents with deselected extras keep the
        # old whole-dir move (no engine tag -> never partial).
        self.set_state(S1, S1, status=6, left=0)
        for dl in self.client.dls:
            dl.pop("engine")
        self.poller.poll_once()
        self.assertEqual(self.partials, [])
        self.assertEqual(len(self.moves), 1)

    def test_restart_persists_uploaded_files_and_drive(self):
        self.set_state(S1 | S3, S1)
        self.poller.poll_once()
        self.finish_partial()
        store2 = app.DecisionStore(self.dpath)      # app restart
        rec = store2.get("tm-pt1")
        self.assertEqual(sorted(rec["uploaded_files"]),
                         ["S1/e1.mkv", "S1/e2.mkv"])
        self.assertEqual(rec["partial_drive"], "Films")
        self.partials.clear()
        p2 = self._poller(store=store2)
        p2.poll_once()
        self.assertEqual(self.partials, [])          # S1 not re-uploaded
        self.set_state(S1 | S2, S1 | S2)
        p2.poll_once()
        self.assertEqual(sorted(self.partials[0][4]),
                         ["S2/e1.mkv", "S2/e2.mkv"])

    def test_restart_rearms_failed_but_keeps_partial_state(self):
        self.set_state(S1, S1)
        self.poller.poll_once()
        self.finish_partial()
        self.store.mark_failed("tm-pt1")
        self.store.record_partial_failure("tm-pt1", 0, 5, 1, 1, quota=True)
        rec = app.DecisionStore(self.dpath).get("tm-pt1")
        self.assertNotIn("failed", rec)              # load()'s reset intact
        self.assertNotIn("partial_failed", rec)
        self.assertEqual(len(rec["uploaded_files"]), 2)

    def test_old_decisions_json_without_partial_keys_loads(self):
        with open(self.dpath, "w") as f:
            json.dump({"tm-pt1": {"name": "n", "choice": "drive:Films",
                                  "handled": False}}, f)
        store2 = app.DecisionStore(self.dpath)
        self.set_state(S1, S1)
        self._poller(store=store2).poll_once()
        self.assertEqual(len(self.partials), 1)

    def test_forget_scrubs_and_requeue_repick_clears_pin(self):
        self.store.add_partial_upload("tm-pt1", ["S1/e1.mkv"], "Films 2")
        self.assertTrue(self.store.requeue("tm-pt1", "drive:Films 2"))
        self.assertEqual(self.store.get("tm-pt1")["uploaded_files"],
                         ["S1/e1.mkv"])              # same drive keeps it
        self.assertTrue(self.store.requeue("tm-pt1", "drive:Other"))
        self.assertNotIn("uploaded_files", self.store.get("tm-pt1"))
        self.assertNotIn("partial_drive", self.store.get("tm-pt1"))
        self.store.add_partial_upload("tm-pt1", ["S1/e1.mkv"], "Films")
        self.store.forget("tm-pt1")
        self.assertNotIn("uploaded_files", self.store.get("tm-pt1"))


class TestPerformPartialUpload(unittest.TestCase):
    def test_copy_only_with_list_file_and_no_engine(self):
        seen = {}

        def fake_up(path, drive, bw, progress_cb=None, **kw):
            with open(kw["files_from"], encoding="utf-8") as f:
                seen["list"] = f.read().splitlines()
            seen.update(path=path, drive=drive, kw=kw)
            return 0, "ok"
        rc, out = app.perform_partial_upload(
            "/dl/Show", "Films", ["S1/e1.mkv", "S1/e2.mkv"],
            todrive_up=fake_up)
        self.assertEqual((rc, out), (0, "ok"))
        self.assertEqual(seen["list"], ["S1/e1.mkv", "S1/e2.mkv"])
        self.assertTrue(seen["kw"]["keep"])
        self.assertNotIn("no_overflow", seen["kw"])
        self.assertFalse(os.path.exists(seen["kw"]["files_from"]))  # cleaned
        app.perform_partial_upload("/dl/Show", "Films", ["a"],
                                   todrive_up=fake_up, pinned=True)
        self.assertTrue(seen["kw"]["no_overflow"])

    def test_parse_routed_drive(self):
        self.assertEqual(app.parse_routed_drive("x\nOK: y", "A"), "A")
        self.assertEqual(app.parse_routed_drive(
            "ROUTED: /p -> A 2\nROUTED: /p -> A 3", "A"), "A 3")

    def test_run_todrive_up_argv_extras(self):
        cmds = []

        class FakeProc:
            def __init__(self, cmd, **kw):
                cmds.append(cmd)
                self.stdout = mock.MagicMock()
                self.stdout.__iter__.return_value = iter(())

            def wait(self):
                return 0
        with mock.patch.object(app.subprocess, "Popen", FakeProc):
            app.run_todrive_up("/p", "D")
            app.run_todrive_up("/p", "D", keep=True, files_from="/l",
                               no_overflow=True)
        self.assertEqual(cmds[0][-3:], ["up", "/p", "D"])   # unchanged
        self.assertIn("--keep", cmds[1])
        self.assertEqual(cmds[1][cmds[1].index("--files-from") + 1], "/l")
        self.assertIn("--no-overflow", cmds[1])


class TestTodriveFilesFrom(unittest.TestCase):
    def setUp(self):
        import argparse
        self.argparse = argparse
        self.mod = _load_todrive()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = dict(self.mod.DEFAULT_CONFIG)
        self.src = os.path.join(self.tmp, "Show (2007)")
        os.makedirs(os.path.join(self.src, "S1"))
        with open(os.path.join(self.src, "S1", "e1.mkv"), "wb") as f:
            f.write(b"x" * 10)
        # an unselected file's leftover .part must NOT block a partial copy
        with open(os.path.join(self.src, "S2e1.mkv.part"), "wb") as f:
            f.write(b"x" * 5)
        self.lst = os.path.join(self.tmp, "list.txt")
        with open(self.lst, "w") as f:
            f.write("S1/e1.mkv\n")

    def _args(self, paths, **kw):
        d = dict(paths=paths, keep=True, allow_partial=False,
                 no_overflow=True, files_from=self.lst)
        d.update(kw)
        return self.argparse.Namespace(**d)

    def _run(self, args, popen_rc=0):
        import contextlib
        import io
        cmds = []

        class FakeProc:
            def __init__(self, cmd, **kw):
                cmds.append(cmd)
                self.stderr = iter(())

            def wait(self):
                return popen_rc
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.mod, "resolve_id", return_value="D1"), \
                mock.patch.object(self.mod.subprocess, "Popen", FakeProc), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = self.mod.cmd_up(self.cfg, args)
        return rc, cmds, err.getvalue()

    def test_builds_rclone_copy_with_files_from(self):
        rc, cmds, _ = self._run(self._args([self.src, "Films"]))
        self.assertEqual(rc, 0)
        c = cmds[0]
        self.assertEqual(c[1], "copy")               # never move
        self.assertEqual(c[2], self.src)
        self.assertTrue(c[3].endswith("Show (2007)/"))  # same dest as a move
        self.assertEqual(c[c.index("--files-from") + 1], self.lst)
        self.assertNotIn("--delete-empty-src-dirs", c)
        self.assertTrue(os.path.exists(os.path.join(self.src, "S1", "e1.mkv")))

    def test_unlisted_part_file_does_not_block_but_listed_marker_does(self):
        rc, cmds, _ = self._run(self._args([self.src, "Films"]))
        self.assertEqual(rc, 0)                      # S2e1.mkv.part ignored
        open(os.path.join(self.src, "S1", "e1.mkv.part"), "w").close()
        rc, cmds, err = self._run(self._args([self.src, "Films"]))
        self.assertEqual(rc, 1)
        self.assertEqual(cmds, [])
        self.assertIn("e1.mkv.part", err)
        # without --files-from the whole-tree scan blocks on the unlisted .part
        rc, cmds, _ = self._run(self._args([self.src, "Films"],
                                           files_from=None, keep=True))
        self.assertEqual(rc, 1)

    def test_requires_keep_and_single_dir(self):
        self.assertEqual(self._run(self._args([self.src, "Films"],
                                              keep=False))[0], 2)
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        self.assertEqual(self._run(self._args([self.src, other, "Films"]))[0],
                         2)
        f = os.path.join(self.src, "S1", "e1.mkv")
        self.assertEqual(self._run(self._args([f, "Films"]))[0], 2)

    def test_overflow_sizing_counts_only_listed_files(self):
        sizes = []

        def fake_pick(cfg, name, payload_bytes, usage_cache):
            sizes.append(payload_bytes)
            return name, "D1"
        with mock.patch.object(self.mod, "pick_destination", fake_pick):
            rc, _c, _e = self._run(self._args([self.src, "Films"],
                                              no_overflow=False))
        self.assertEqual(rc, 0)
        self.assertEqual(sizes, [10])                # not 15 (.part excluded)

    def test_build_up_argv_pure(self):
        c = self.mod.build_up_argv("move", "/s", "r:/d/", ["--x"], True)
        self.assertEqual(c[1:4], ["move", "/s", "r:/d/"])
        self.assertIn("--delete-empty-src-dirs", c)
        self.assertNotIn("--files-from", c)
        c = self.mod.build_up_argv("copy", "/s", "r:/d/", [], True, "/l")
        self.assertEqual(c[-3:-1], ["--files-from", "/l"])
        self.assertEqual(c[-1], "-P")


# --- folder-level logic: free after upload / auto-advance / done ------------
#
# Partial mode only COPIES finished files; on a 460 GB disk a 179 GB torrent
# then fills it. Each season folder is freed once it is verifiably on the drive
# (check -> untick -> confirm -> delete), then the next one is ticked.

_FL_TOP = "Show (2007)"
_FL_FILES = ["Season 1/e1.mkv", "Season 1/e2.mkv",
             "Season 3/e1.mkv", "Season 3/e2.mkv",
             "Season 4/e1.mkv", "Season 4/e2.mkv",
             "Season 10/e1.mkv",
             "Featurettes/f1.mkv",
             "readme.txt"]


def _fl(folder):
    return {f for f in _FL_FILES if f.startswith(folder + "/")}


GIB = 1024 ** 3


class FakeFolderEngine:
    """Records set_files_wanted / start / remove and keeps the torrent model
    in the test (base.wanted) truthful, so the poller sees its own effects."""
    def __init__(self, base):
        self.base = base
        self.ignore_untick = False

    def set_files_wanted(self, gid, indices, wanted):
        b = self.base
        b.events.append(("tick" if wanted else "untick",
                         sorted(_FL_FILES[i] for i in indices),
                         [os.path.exists(os.path.join(b.top(), f))
                          for f in (_FL_FILES[i] for i in indices)]))
        if self.ignore_untick and not wanted:
            return
        for i in indices:
            (b.wanted.add if wanted else b.wanted.discard)(_FL_FILES[i])
        b.rebuild()

    def get_files_wanted(self, gid):
        self.base.events.append(("confirm",))
        return [f in self.base.wanted for f in _FL_FILES]

    def start_torrent(self, gid):
        self.base.events.append(("start", gid))

    def stop_torrent(self, gid):
        self.base.events.append(("stop", gid))

    def remove_torrent(self, gid):
        self.base.events.append(("remove", gid))
        if not self.base.remove_is_noop:
            self.base.client.dls = []


class FolderBase(PartialBase):
    cfg = {"partial_free_after_upload": True, "partial_auto_advance": True,
           "partial_free_margin_gb": 5}
    free_bytes = 100 * GIB

    def setUp(self):
        super().setUp()
        self.wanted, self.have = set(), set()
        self.events = []
        self.verifies = []
        self.verify_result = (True, "ok")
        self.remove_is_noop = False
        self.engine = FakeFolderEngine(self)
        self.client.engine = self.engine
        self.free = [self.free_bytes]
        self.ran = []
        self.poller = self._fl_poller(self.store, spawn=lambda fn: fn())

    def _fl_poller(self, store, spawn, cfg=None):
        def verify(local, drive, subpath, excludes):
            self.verifies.append((local, drive, subpath, tuple(excludes)))
            self.events.append(("verify", os.path.basename(local),
                                os.path.exists(local)))
            return self.verify_result
        return app.Poller(
            self.client, store, ask_cb=lambda g, n: "local",
            upload_cb=lambda *a: self.moves.append(a),
            notify_cb=lambda *a: self.notes.append(a),
            now_fn=lambda: self.now[0],
            partial_upload_cb=lambda *a: self.partials.append(a),
            partial_cfg=cfg if cfg is not None else self.cfg,
            verify_cb=verify,
            disk_usage_cb=lambda p: mock.Mock(free=self.free[0]),
            spawn_cb=spawn)

    def rebuild(self):
        files, stats = [], []
        for rel in _FL_FILES:
            files.append({"name": "%s/%s" % (_FL_TOP, rel), "length": 10,
                          "bytesCompleted": 10 if rel in self.have else 3})
            stats.append({"wanted": rel in self.wanted})
        left = 10 * len([r for r in self.wanted if r not in self.have])
        self.client.dls = [app.TransmissionClient._adapt(_torrent(
            hashString="pt1", name=_FL_TOP, status=4 if left else 6,
            downloadDir=self.tmp, files=files, fileStats=stats,
            totalSize=10 * len(_FL_FILES), sizeWhenDone=10 * len(self.wanted),
            leftUntilDone=left, percentDone=1.0 if left == 0 else 0.5))]

    def set_state(self, wanted, have, uploaded=()):
        """Sets the model + writes disk (complete files; '.part' for wanted
        but unfinished) + records `uploaded` in decisions.json."""
        self.wanted, self.have = set(wanted), set(have)
        for rel in _FL_FILES:
            if rel in self.have:
                self.write(rel)
            elif rel in self.wanted:
                self.write(rel, size=3, part=True)
        if uploaded:
            self.store.add_partial_upload("tm-pt1", sorted(uploaded), "Films")
        self.rebuild()

    def ops(self, kind):
        return [e for e in self.events if e[0] == kind]

    def notified(self, title):
        return [n for n in self.notes if n[0] == title]


S1F, S3F, S4F = _fl("Season 1"), _fl("Season 3"), _fl("Season 4")


class TestFreeAfterUpload(FolderBase):
    cfg = {"partial_free_after_upload": True, "partial_auto_advance": False,
           "partial_free_margin_gb": 5}

    def test_happy_path_order_check_untick_confirm_delete(self):
        self.set_state(S3F | S4F, S3F, uploaded=S3F)   # Season 4 downloading
        self.poller.poll_once()
        # verify on the resolved drive/path, folder still there
        self.assertEqual(self.verifies, [(
            os.path.join(self.top(), "Season 3"), "Films",
            "%s/Season 3" % _FL_TOP, ())])
        kinds = [e[0] for e in self.events]
        self.assertEqual(kinds, ["verify", "untick", "confirm"])
        self.assertEqual(self.events[0][2], True)      # existed at check time
        self.assertEqual(self.events[1][1], sorted(S3F))
        self.assertEqual(self.events[1][2], [True, True])  # untick BEFORE delete
        # ...and only then deleted
        self.assertFalse(os.path.exists(os.path.join(self.top(), "Season 3")))
        self.assertTrue(os.path.isdir(os.path.join(self.top(), "Season 4")))
        rec = self.store.get("tm-pt1")
        self.assertEqual(rec["freed_folders"], ["Season 3"])
        self.assertEqual(len(self.notified("Season freed")), 1)
        self.assertNotIn("tm-pt1", self.poller._uploading)
        # nothing else touched: still no move, no engine stop/remove
        self.assertEqual(self.moves, [])
        self.assertEqual(self.ops("remove") + self.ops("stop"), [])
        # a later poll neither re-verifies nor re-notifies
        self.poller.poll_once()
        self.assertEqual(len(self.verifies), 1)
        self.assertEqual(len(self.notified("Season freed")), 1)

    def test_verify_fails_nothing_unticked_or_deleted(self):
        self.set_state(S3F | S4F, S3F, uploaded=S3F)   # Season 4 downloading
        self.verify_result = (False, "rclone check rc=1")
        self.poller.poll_once()
        self.assertEqual([e[0] for e in self.events], ["verify"])
        self.assertTrue(os.path.isdir(os.path.join(self.top(), "Season 3")))
        rec = self.store.get("tm-pt1")
        self.assertNotIn("freed_folders", rec)
        self.assertEqual(rec["free_failures"], 1)
        self.assertEqual(len(self.notified("Season cleanup failed")), 1)

    def test_untick_not_confirmed_no_delete(self):
        self.set_state(S3F | S4F, S3F, uploaded=S3F)   # Season 4 downloading
        self.engine.ignore_untick = True
        self.poller.poll_once()
        self.assertEqual([e[0] for e in self.events],
                         ["verify", "untick", "confirm"])
        self.assertTrue(os.path.isdir(os.path.join(self.top(), "Season 3")))
        self.assertEqual(len(os.listdir(os.path.join(self.top(), "Season 3"))), 2)
        self.assertNotIn("freed_folders", self.store.get("tm-pt1"))

    def test_symlinked_folder_is_never_deleted(self):
        self.set_state(S3F | S4F, S3F, uploaded=S3F)   # Season 4 downloading
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "keep.txt"), "w") as f:
            f.write("precious")
        real = os.path.join(self.top(), "Season 3")
        shutil.rmtree(real)
        os.symlink(outside, real)
        self.poller.poll_once()
        self.assertEqual(self.events, [])               # refused pre-verify
        self.assertTrue(os.path.exists(os.path.join(outside, "keep.txt")))
        self.assertTrue(os.path.islink(real))
        self.assertNotIn("freed_folders", self.store.get("tm-pt1"))

    def test_safe_child_dir(self):
        top = os.path.join(self.tmp, "root")
        os.makedirs(os.path.join(top, "a", "b"))
        os.makedirs(os.path.join(self.tmp, "elsewhere"))
        os.symlink(os.path.join(self.tmp, "elsewhere"),
                   os.path.join(top, "lnk"))
        self.assertTrue(app.safe_child_dir(os.path.join(top, "a"), top))
        self.assertFalse(app.safe_child_dir(os.path.join(top, "a", "b"), top))
        self.assertFalse(app.safe_child_dir(top, top))
        self.assertFalse(app.safe_child_dir(os.path.join(top, "lnk"), top))
        self.assertFalse(app.safe_child_dir(
            os.path.join(top, "a", "..", ".."), top))
        self.assertFalse(app.safe_child_dir(os.path.join(top, "nope"), top))

    def test_not_fully_uploaded_is_not_freed(self):
        # e1 is on the drive, e2 is still downloading (so no copy is owed
        # either, which would pre-empt the folder logic)
        self.set_state(S3F | S4F, {"Season 3/e1.mkv"},
                       uploaded={"Season 3/e1.mkv"})
        self.poller.poll_once()
        self.assertEqual(self.events, [])
        self.assertTrue(os.path.isdir(os.path.join(self.top(), "Season 3")))

    def test_partly_selected_folder_is_not_freed(self):
        self.set_state((S3F - {"Season 3/e2.mkv"}) | S4F, S3F | S4F,
                       uploaded=S3F)
        self.poller.poll_once()
        self.assertEqual(self.events, [])

    def test_absent_folder_counts_as_freed_idempotently(self):
        self.set_state({"readme.txt"}, {"readme.txt"},   # Season 3 never on disk
                       uploaded=S3F | {"readme.txt"})
        self.poller.poll_once()
        self.assertEqual(self.events, [])            # no rclone, no RPC
        self.assertEqual(self.store.get("tm-pt1")["freed_folders"],
                         ["Season 3"])
        self.poller.poll_once()
        self.assertEqual(self.store.get("tm-pt1")["freed_folders"],
                         ["Season 3"])

    def _absent_ticked_s3(self, remote):
        # Season 3 ticked + complete + uploaded, then deleted by hand.
        self.set_state(S3F | S4F, S3F, uploaded=S3F)
        shutil.rmtree(os.path.join(self.top(), "Season 3"))
        self.listed = []

        def sizes(drive, subpath):
            self.listed.append((drive, subpath))
            return remote
        self.poller.remote_sizes_cb = sizes

    def test_ticked_folder_deleted_by_hand_is_unticked(self):
        self._absent_ticked_s3({"e1.mkv": 10, "e2.mkv": 10})
        self.poller.poll_once()
        self.assertEqual(self.listed, [("Films", "%s/Season 3" % _FL_TOP)])
        self.assertEqual([e[0] for e in self.events], ["untick", "confirm"])
        self.assertEqual(self.events[0][1], sorted(S3F))
        self.assertTrue(S3F.isdisjoint(self.wanted))
        self.assertEqual(self.store.get("tm-pt1")["freed_folders"],
                         ["Season 3"])
        self.assertTrue(os.path.isdir(os.path.join(self.top(), "Season 4")))
        self.assertEqual(len(self.notified("Season freed")), 1)

    def test_deleted_by_hand_but_not_on_drive_stays_ticked(self):
        self._absent_ticked_s3({"e1.mkv": 10, "e2.mkv": 7})   # e2 short
        self.poller.poll_once()
        self.assertEqual(self.events, [])
        self.assertTrue(S3F <= self.wanted)
        self.assertNotIn("freed_folders", self.store.get("tm-pt1"))
        self.assertEqual(self.store.get("tm-pt1")["free_failures"], 1)

    def test_deleted_by_hand_listing_fails_stays_ticked(self):
        self._absent_ticked_s3(None)
        self.poller.poll_once()
        self.assertEqual(self.events, [])
        self.assertNotIn("freed_folders", self.store.get("tm-pt1"))

    def test_deleted_by_hand_untick_not_confirmed(self):
        self._absent_ticked_s3({"e1.mkv": 10, "e2.mkv": 10})
        self.engine.ignore_untick = True
        self.poller.poll_once()
        self.assertEqual([e[0] for e in self.events], ["untick", "confirm"])
        self.assertNotIn("freed_folders", self.store.get("tm-pt1"))

    def test_remote_sizes_parses_lsjson(self):
        run = mock.Mock(return_value=mock.Mock(
            returncode=0, stdout='[{"Path":"a/e1.mkv","Size":5}]'))
        got = app.remote_sizes("D", "Top/S", resolve=lambda d: "r:",
                               runner=run)
        self.assertEqual(got, {"a/e1.mkv": 5})
        self.assertEqual(run.call_args[0][0][1:],
                         ["lsjson", "-R", "--files-only", "r:Top/S"])
        run.return_value = mock.Mock(returncode=3, stdout="")
        self.assertIsNone(app.remote_sizes("D", "x", resolve=lambda d: "r:",
                                           runner=run))
        self.assertIsNone(app.remote_sizes("D", "x", resolve=lambda d: None,
                                           runner=run))

    def test_freed_folders_survive_restart(self):
        self.set_state(S3F | S4F, S3F, uploaded=S3F)   # Season 4 downloading
        self.poller.poll_once()
        store2 = app.DecisionStore(self.dpath)
        self.assertEqual(store2.get("tm-pt1")["freed_folders"], ["Season 3"])
        self.events.clear()
        p2 = self._fl_poller(store2, spawn=lambda fn: fn())
        p2.poll_once()
        self.assertEqual(self.events, [])            # nothing redone

    def test_failure_backoff_is_separate_and_resets_on_restart(self):
        self.set_state(S3F | S4F, S3F, uploaded=S3F)   # Season 4 downloading
        self.verify_result = (False, "boom")
        self.poller.poll_once()
        rec = self.store.get("tm-pt1")
        self.assertGreater(rec["free_next_attempt"], self.now[0])
        for k in ("partial_failed", "partial_failures", "failed", "failures",
                  "next_attempt"):
            self.assertNotIn(k, rec)
        self.poller.poll_once()
        self.assertEqual(len(self.verifies), 1)      # inside the window
        self.verify_result = (True, "ok")
        self.now[0] += 100000
        self.poller.poll_once()
        self.assertEqual(self.store.get("tm-pt1")["freed_folders"],
                         ["Season 3"])
        self.assertNotIn("free_failures", self.store.get("tm-pt1"))
        # terminal state re-arms on restart
        self.store.record_free_failure("tm-pt1", 0, 1, 1, 1)
        self.assertTrue(self.store.get("tm-pt1")["free_failed"])
        self.assertNotIn("free_failed",
                         app.DecisionStore(self.dpath).get("tm-pt1"))

    def test_free_toggle_off_does_nothing(self):
        self.set_state(S3F | S4F, S3F, uploaded=S3F)   # Season 4 downloading
        p = self._fl_poller(self.store, lambda fn: fn(), cfg={
            "partial_free_after_upload": False, "partial_auto_advance": False})
        p.poll_once()
        self.assertEqual(self.events, [])
        self.assertTrue(os.path.isdir(os.path.join(self.top(), "Season 3")))

    def test_rootfile_is_not_a_folder(self):
        self.set_state({"readme.txt"} | S4F, {"readme.txt"} | S4F,
                       uploaded={"readme.txt"})
        self.poller.poll_once()
        self.assertEqual(self.events, [])
        self.assertTrue(os.path.exists(os.path.join(self.top(), "readme.txt")))

    def test_work_runs_off_the_poll_thread_and_never_overlaps(self):
        self.set_state(S3F | S4F, S3F, uploaded=S3F)   # Season 4 downloading
        queued = []
        p = self._fl_poller(self.store, spawn=queued.append,
                            cfg={"partial_free_after_upload": True,
                                 "partial_auto_advance": True,
                                 "partial_free_margin_gb": 5})
        p.poll_once()
        self.assertEqual(len(queued), 1)
        self.assertEqual(self.events, [])            # nothing ran inline
        self.assertTrue(os.path.isdir(os.path.join(self.top(), "Season 3")))
        self.assertIn("tm-pt1", p._uploading)
        # more polls while in flight: no second task, no copy, no advance
        self.set_state(S3F | S4F, S3F | S4F)
        self.write("Season 4/e3.mkv")
        p.poll_once()
        p.poll_once()
        self.assertEqual(len(queued), 1)
        self.assertEqual(self.partials, [])
        queued.pop()()
        self.assertNotIn("tm-pt1", p._uploading)
        self.assertEqual(self.store.get("tm-pt1")["freed_folders"],
                         ["Season 3"])


class TestAutoAdvance(FolderBase):
    cfg = {"partial_free_after_upload": False, "partial_auto_advance": True,
           "partial_free_margin_gb": 5}

    def _state_after_season3(self):
        # Season 1 already freed earlier; Season 3 finished + uploaded.
        self.store.add_freed_folder("tm-pt1", "Season 1")
        self.set_state(S3F, S3F, uploaded=S1F | S3F)

    def test_picks_season_4_after_3_not_featurettes(self):
        self._state_after_season3()
        self.poller.poll_once()
        self.assertEqual(self.events[0][:2], ("tick", sorted(S4F)))
        self.assertEqual(self.events[1], ("start", "tm-pt1"))
        self.assertEqual(self.wanted, S3F | S4F)     # user's Season 3 untouched
        self.assertEqual(len(self.notified("Next season started")), 1)
        self.assertNotIn("tm-pt1", self.poller._uploading)

    def test_orders_by_season_number_then_natural(self):
        order = sorted(["Featurettes", "Season 10", "Season 4", "S02",
                        "Extras 10", "Extras 2", "Series 3", "Specials"],
                       key=app.folder_order_key)
        self.assertEqual(order, ["S02", "Series 3", "Season 4", "Season 10",
                                 "Extras 2", "Extras 10", "Featurettes",
                                 "Specials"])
        self.assertEqual(app.season_number("Show S04 1080p"), 4)
        self.assertIsNone(app.season_number("Extras 2"))
        self.assertIsNone(app.season_number("Specials"))

    def test_skips_user_selected_folders_and_never_unticks(self):
        # user ticked Season 3 AND Season 4, both finished + uploaded
        self.store.add_freed_folder("tm-pt1", "Season 1")
        self.set_state(S3F | S4F, S3F | S4F, uploaded=S3F | S4F)
        self.poller.poll_once()
        self.assertEqual(self.events[0][:2], ("tick", ["Season 10/e1.mkv"]))
        self.assertEqual(self.ops("untick"), [])

    def test_waits_while_selection_still_downloading(self):
        self.set_state(S3F | S4F, S3F, uploaded=S3F)   # Season 4 incomplete
        self.poller.poll_once()
        self.assertEqual(self.events, [])

    def test_waits_while_a_copy_is_still_owed(self):
        self.set_state(S3F, S3F)                       # finished, NOT uploaded
        self.poller.poll_once()
        self.assertEqual(self.ops("tick"), [])

    def test_skips_folder_with_uploaded_files_or_freed(self):
        self.store.add_freed_folder("tm-pt1", "Season 1")
        self.store.add_freed_folder("tm-pt1", "Season 4")
        self.set_state(S3F, S3F, uploaded=S3F | {"Season 10/e1.mkv"})
        self.poller.poll_once()
        self.assertEqual(self.events[0][:2], ("tick", ["Featurettes/f1.mkv"]))

    def test_low_space_notifies_once_then_advances_when_freed(self):
        self._state_after_season3()
        self.free[0] = 1 * GIB                          # < 20 B + 5 GiB margin
        self.poller.poll_once()
        self.poller.poll_once()
        self.assertEqual(self.ops("tick"), [])
        low = self.notified("Next season waiting")
        self.assertEqual(len(low), 1)
        self.assertIn("Not enough space to start Season 4", low[0][2])
        self.free[0] = 50 * GIB
        self.poller.poll_once()
        self.assertEqual(len(self.ops("tick")), 1)

    def test_margin_is_configurable(self):
        self._state_after_season3()
        self.free[0] = 3 * GIB
        p = self._fl_poller(self.store, lambda fn: fn(), cfg={
            "partial_auto_advance": True, "partial_free_margin_gb": 1})
        p.poll_once()
        self.assertEqual(len(self.ops("tick")), 1)

    def test_advance_toggle_off_does_nothing(self):
        self._state_after_season3()
        p = self._fl_poller(self.store, lambda fn: fn(), cfg={
            "partial_auto_advance": False})
        p.poll_once()
        self.assertEqual(self.events, [])

    def test_free_then_advance_across_two_ticks(self):
        self.set_state(S3F, S3F, uploaded=S3F)
        p = self._fl_poller(self.store, lambda fn: fn(), cfg={
            "partial_free_after_upload": True, "partial_auto_advance": True})
        p.poll_once()                                   # frees Season 3
        self.assertEqual(self.ops("tick"), [])
        self.assertFalse(os.path.exists(os.path.join(self.top(), "Season 3")))
        p.poll_once()                                   # then advances
        self.assertEqual(self.ops("tick")[0][1], sorted(_fl("Season 1")))

    def test_advance_error_uses_free_backoff_keys(self):
        self._state_after_season3()

        def boom(*a):
            raise RuntimeError("rpc down")
        self.engine.set_files_wanted = boom
        self.poller.poll_once()
        rec = self.store.get("tm-pt1")
        self.assertEqual(rec["free_failures"], 1)
        self.assertNotIn("failures", rec)
        self.assertNotIn("tm-pt1", self.poller._uploading)


class TestPartialDone(FolderBase):
    cfg = {"partial_free_after_upload": True, "partial_auto_advance": False,
           "partial_free_margin_gb": 5}

    def _all_on_drive(self):
        # every folder already freed; only a root file (+ a .part marker of an
        # uploaded file, + an empty dir) is left locally.
        for f in ("Season 1", "Season 3", "Season 4", "Season 10",
                  "Featurettes"):
            self.store.add_freed_folder("tm-pt1", f)
        self.set_state({"readme.txt"}, {"readme.txt"},
                       uploaded=set(_FL_FILES))
        os.makedirs(os.path.join(self.top(), "Season 3"))
        self.write("Season 3/e1.mkv", size=2, part=True)
        os.makedirs(os.path.join(self.top(), "empty"))

    def test_completion_verifies_removes_torrent_deletes_root_marks_handled(
            self):
        self._all_on_drive()
        self.poller.poll_once()
        self.assertEqual(self.verifies, [(
            self.top(), "Films", _FL_TOP, ("*.part",))])
        self.assertEqual([e[0] for e in self.events], ["verify", "remove"])
        self.assertFalse(os.path.exists(self.top()))
        rec = self.store.get("tm-pt1")
        self.assertTrue(rec["handled"])
        self.assertEqual(self.moves, [])
        self.assertEqual(len(self.notified("Torrent finished")), 1)
        self.poller.poll_once()                         # torrent is gone
        self.assertEqual(len(self.verifies), 1)

    def test_completion_with_root_already_gone_skips_check(self):
        self._all_on_drive()
        shutil.rmtree(self.top())
        self.poller.poll_once()
        self.assertEqual(self.verifies, [])
        self.assertEqual([e[0] for e in self.events], ["remove"])
        self.assertTrue(self.store.get("tm-pt1")["handled"])

    def test_unaccounted_local_file_blocks_completion(self):
        self._all_on_drive()
        self.write("stray.txt")
        self.poller.poll_once()
        self.assertEqual(self.events, [])
        self.assertTrue(os.path.exists(os.path.join(self.top(), "stray.txt")))
        self.assertFalse(self.store.get("tm-pt1")["handled"])
        self.assertEqual(self.store.get("tm-pt1")["free_failures"], 1)

    def test_failed_check_blocks_completion(self):
        self._all_on_drive()
        self.verify_result = (False, "rc=1")
        self.poller.poll_once()
        self.assertEqual(self.ops("remove"), [])
        self.assertTrue(os.path.exists(self.top()))

    def test_torrent_still_present_after_remove_keeps_data(self):
        self._all_on_drive()
        self.remove_is_noop = True
        self.poller.poll_once()
        self.assertTrue(os.path.exists(os.path.join(self.top(), "readme.txt")))
        self.assertFalse(self.store.get("tm-pt1")["handled"])

    def test_not_everything_uploaded_is_not_done(self):
        self._all_on_drive()
        rec = self.store.get("tm-pt1")
        rec["uploaded_files"].remove("readme.txt")
        self.poller.poll_once()
        self.assertEqual(self.ops("remove"), [])

    def test_toggle_off_keeps_the_torrent(self):
        self._all_on_drive()
        p = self._fl_poller(self.store, lambda fn: fn(), cfg={
            "partial_free_after_upload": False})
        p.poll_once()
        self.assertEqual(self.events, [])
        self.assertTrue(os.path.exists(self.top()))

    def test_tree_leftovers(self):
        self.write("a/x.mkv")
        self.write("a/x.mkv.part")
        self.write("a/y.mkv.part")
        os.makedirs(os.path.join(self.top(), "emptydir"))
        left = app.tree_leftovers(self.top(), {"a/x.mkv"})
        self.assertEqual(sorted(left), [os.path.join("a", "y.mkv.part")])


class TestFreedFolderMoveGuard(FolderBase):
    def test_reticked_freed_folder_is_never_whole_dir_moved(self):
        # The user re-ticked a freed folder: every file is ticked + complete
        # (not partial any more), but Season 1 was freed. The whole-dir move
        # would face a gutted tree, so the torrent stays partial-managed.
        self.store.add_freed_folder("tm-pt1", "Season 1")
        allf = set(_FL_FILES)
        self.set_state(allf, allf)
        p = self._fl_poller(self.store, lambda fn: fn(), cfg={})
        p.poll_once()
        self.assertEqual(self.moves, [])
        self.assertEqual(self.ops("stop") + self.ops("remove"), [])

    def test_reticked_freed_folder_is_freed_again_not_stranded(self):
        # Re-downloaded folder that is already on the drive: the folder logic
        # keeps running for a torrent with freed_folders even though nothing
        # is unticked any more, so the disk is reclaimed again.
        self.store.add_freed_folder("tm-pt1", "Season 1")
        allf = set(_FL_FILES)
        self.set_state(allf, allf, uploaded=allf)
        p = self._fl_poller(self.store, lambda fn: fn(), cfg={
            "partial_free_after_upload": True})
        p.poll_once()
        self.assertEqual(self.moves, [])
        self.assertEqual(self.ops("untick")[0][1], sorted(S1F))
        self.assertFalse(os.path.exists(os.path.join(self.top(), "Season 1")))

    def test_explicit_move_guard_for_freed_records(self):
        # Belt and braces in the whole-dir branch itself: an engine-complete
        # single-file download whose record has freed_folders is not moved.
        f = self.write("only.mkv")
        self.client.dls = [{"gid": "tm-pt1", "engine": "aria2",
                            "status": "complete", "dir": self.tmp,
                            "totalLength": 10, "completedLength": 10,
                            "bittorrent": {"info": {"name": _FL_TOP}},
                            "files": [{"path": f, "length": 10,
                                       "completedLength": 10,
                                       "selected": "true"}]}]
        self.store.add_freed_folder("tm-pt1", "Season 1")
        self.poller.poll_once()
        self.assertEqual(self.moves, [])
        # same record without freed_folders DOES move (guard is the only gap)
        self.store.get("tm-pt1").pop("freed_folders")
        self.poller.poll_once()
        self.assertEqual(len(self.moves), 1)

    def test_same_torrent_without_freed_folders_still_moves(self):
        allf = set(_FL_FILES)
        self.set_state(allf, allf)
        self.poller.poll_once()
        self.assertEqual(len(self.moves), 1)


class TestFolderHelpers(unittest.TestCase):
    def test_rclone_check_argv(self):
        self.assertEqual(
            app.rclone_check_argv("/l/S 1", "r,team_drive=ID:", "Show/S 1",
                                  rclone="rc"),
            ["rc", "check", "--one-way", "/l/S 1", "r,team_drive=ID:Show/S 1"])
        self.assertEqual(
            app.rclone_check_argv("/l", "r:", "Show", ("*.part",))[-2:],
            ["--exclude", "*.part"])

    def test_todrive_resolve_uses_config_env_and_last_line(self):
        seen = {}

        def runner(argv, **kw):
            seen.update(argv=argv, env=kw["env"])
            return mock.Mock(returncode=0, stdout="noise\nr,team_drive=X:\n")
        self.assertEqual(app.todrive_resolve("Films", runner=runner),
                         "r,team_drive=X:")
        self.assertEqual(seen["argv"][0], sys.executable)
        self.assertEqual(seen["argv"][2:], ["resolve", "Films"])
        self.assertEqual(seen["env"]["TODRIVE_CONFIG"], app.CONFIG_FILE)
        self.assertIsNone(app.todrive_resolve(
            "Films", runner=lambda *a, **k: mock.Mock(returncode=1,
                                                      stdout="")))

    def test_verify_on_drive_only_rc_zero_counts(self):
        ran = []

        def runner(argv, **kw):
            ran.append(argv)
            return mock.Mock(returncode=self.rc, stdout="", stderr="diff")
        for rc, want in ((0, True), (1, False), (7, False)):
            self.rc = rc
            ok, _d = app.verify_on_drive(
                "/l", "Films", "Show/S1", resolve=lambda d: "r:", runner=runner)
            self.assertEqual(ok, want)
        self.assertEqual(ran[0][1:3], ["check", "--one-way"])
        self.assertEqual(ran[0][-1], "r:Show/S1")
        ok, _d = app.verify_on_drive("/l", "Films", "x",
                                     resolve=lambda d: None, runner=runner)
        self.assertFalse(ok)
        self.assertEqual(len(ran), 3)                   # unresolved: no rclone

    def test_read_partial_config(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "config.json")
        self.assertEqual(app.read_partial_config(path),
                         {"partial_free_after_upload": True,
                          "partial_auto_advance": True,
                          "partial_free_margin_gb": 5})
        with open(path, "w") as f:
            json.dump({"partial_free_after_upload": False,
                       "partial_auto_advance": "no",     # wrong type: default
                       "partial_free_margin_gb": 12.5}, f)
        self.assertEqual(app.read_partial_config(path),
                         {"partial_free_after_upload": False,
                          "partial_auto_advance": True,
                          "partial_free_margin_gb": 12.5})

    def test_bare_poller_has_folder_logic_off(self):
        p = app.Poller(None, None, None, None)
        self.assertFalse(p.partial_cfg["partial_free_after_upload"])
        self.assertFalse(p.partial_cfg["partial_auto_advance"])


class TestTransmissionFileWanted(unittest.TestCase):
    def _client(self, reply):
        c = app.TransmissionClient(port=9091)
        sent = []

        def fake_call(method, arguments=None):
            sent.append((method, arguments))
            if isinstance(reply, Exception):
                raise reply
            return reply
        c._call = fake_call
        return c, sent

    def test_set_files_wanted_payloads(self):
        c, sent = self._client({})
        c.set_files_wanted("tm-abc", [0, 2], False)
        c.set_files_wanted("tm-abc", [1], True)
        self.assertEqual(sent, [
            ("torrent-set", {"ids": ["abc"], "files-unwanted": [0, 2]}),
            ("torrent-set", {"ids": ["abc"], "files-wanted": [1]})])

    def test_errors_propagate(self):
        c, _ = self._client(RuntimeError("transmission error: nope"))
        with self.assertRaises(RuntimeError):
            c.set_files_wanted("tm-abc", [0], False)
        with self.assertRaises(RuntimeError):
            c.get_files_wanted("tm-abc")

    def test_get_files_wanted(self):
        c, sent = self._client({"torrents": [{"fileStats": [
            {"wanted": True}, {"wanted": False}]}]})
        self.assertEqual(c.get_files_wanted("tm-abc"), [True, False])
        self.assertEqual(sent[0][1]["fields"], ["fileStats"])
        c, _ = self._client({"torrents": []})
        with self.assertRaises(RuntimeError):
            c.get_files_wanted("tm-abc")


if __name__ == "__main__":
    unittest.main(verbosity=2)
