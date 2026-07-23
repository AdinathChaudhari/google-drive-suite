#!/usr/bin/env python3
"""Unit tests for hub_core.py — no real processes, no real network.

Every test injects fake probes/dirs; nothing here touches an actual port,
launchctl, or pgrep on this machine.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from gdrive_toolkit.hub import hub_core
from gdrive_toolkit.hub.hub_core import HubError, Status


class TmpDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hub_core_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# is_installed
# ---------------------------------------------------------------------------

class TestIsInstalled(TmpDirTestCase):
    def test_installed_if_all_present(self):
        (self.tmp / "app.py").write_text("x")
        (self.tmp / "venv" / "bin").mkdir(parents=True)
        (self.tmp / "venv" / "bin" / "python").write_text("x")
        tool = {
            "dir": str(self.tmp),
            "installed_if": ["app.py", "venv/bin/python"],
        }
        self.assertTrue(hub_core.is_installed(tool))

    def test_installed_if_missing_one(self):
        (self.tmp / "app.py").write_text("x")
        tool = {
            "dir": str(self.tmp),
            "installed_if": ["app.py", "venv/bin/python"],
        }
        self.assertFalse(hub_core.is_installed(tool))

    def test_installed_if_any_true_when_one_present(self):
        (self.tmp / "plist_dir").mkdir()
        plist = self.tmp / "plist_dir" / "com.example.plist"
        plist.write_text("x")
        app = self.tmp / "not_there.app"
        tool = {
            "dir": str(self.tmp),
            "installed_if_any": [str(plist), str(app)],
        }
        self.assertTrue(hub_core.is_installed(tool))

    def test_installed_if_any_false_when_none_present(self):
        tool = {
            "dir": str(self.tmp),
            "installed_if_any": [
                str(self.tmp / "nope.plist"),
                str(self.tmp / "nope.app"),
            ],
        }
        self.assertFalse(hub_core.is_installed(tool))

    def test_relative_paths_join_to_dir(self):
        (self.tmp / "app.py").write_text("x")
        tool = {"dir": str(self.tmp), "installed_if": ["app.py"]}
        self.assertTrue(hub_core.is_installed(tool))

    def test_dir_less_tool_is_not_installed_no_crash(self):
        # A tool dict without a "dir" key (e.g. an external registry entry)
        # must not KeyError — treated as not locally installed.
        tool = {"kind": "external", "label": "Some External Tool"}
        self.assertFalse(hub_core.is_installed(tool))


# ---------------------------------------------------------------------------
# resolve_port
# ---------------------------------------------------------------------------

class TestResolvePort(TmpDirTestCase):
    def test_reads_port_from_config(self):
        cfg_path = self.tmp / "config.json"
        cfg_path.write_text(json.dumps({"port": 9999}))
        tool = {"port": {"config": str(cfg_path), "key": "port", "default": 1111}}
        self.assertEqual(hub_core.resolve_port(tool), 9999)

    def test_falls_back_to_default_when_config_missing(self):
        tool = {
            "port": {
                "config": str(self.tmp / "does_not_exist.json"),
                "key": "port",
                "default": 8747,
            }
        }
        self.assertEqual(hub_core.resolve_port(tool), 8747)

    def test_falls_back_to_default_when_key_missing(self):
        cfg_path = self.tmp / "config.json"
        cfg_path.write_text(json.dumps({"other_key": 1}))
        tool = {"port": {"config": str(cfg_path), "key": "port", "default": 8748}}
        self.assertEqual(hub_core.resolve_port(tool), 8748)

    def test_falls_back_to_default_when_config_is_bad_json(self):
        cfg_path = self.tmp / "config.json"
        cfg_path.write_text("{not json")
        tool = {"port": {"config": str(cfg_path), "key": "port", "default": 8737}}
        self.assertEqual(hub_core.resolve_port(tool), 8737)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

class TestStatus(TmpDirTestCase):
    def _web_tool(self, installed_if_present=True):
        cfg_path = self.tmp / "config.json"
        cfg_path.write_text(json.dumps({"port": 8747}))
        if installed_if_present:
            (self.tmp / "app.py").write_text("x")
        return {
            "id": "web-tool",
            "label": "Web Tool",
            "kind": "web",
            "dir": str(self.tmp),
            "installed_if": ["app.py"],
            "port": {"config": str(cfg_path), "key": "port", "default": 8747},
        }

    def test_web_not_installed(self):
        tool = self._web_tool(installed_if_present=False)
        self.assertEqual(hub_core.status(tool), Status.NOT_INSTALLED)

    def test_web_running_when_tcp_connects(self):
        tool = self._web_tool()
        self.assertEqual(
            hub_core.status(tool, tcp_probe=lambda host, port: True),
            Status.RUNNING,
        )

    def test_web_stopped_when_tcp_refused(self):
        tool = self._web_tool()
        self.assertEqual(
            hub_core.status(tool, tcp_probe=lambda host, port: False),
            Status.STOPPED,
        )

    def _menubar_tool(self, installed=True):
        tool = {
            "id": "menubar-tool",
            "label": "Menubar Tool",
            "kind": "menubar",
            "dir": str(self.tmp),
            "installed_if_any": [str(self.tmp / "marker.plist")],
            "launchd_label": "com.example.tool",
            "process_pattern": r"example\.tool",
        }
        if installed:
            (self.tmp / "marker.plist").write_text("x")
        return tool

    def test_menubar_not_installed(self):
        tool = self._menubar_tool(installed=False)
        self.assertEqual(hub_core.status(tool), Status.NOT_INSTALLED)

    def test_menubar_running_via_launchd(self):
        tool = self._menubar_tool()
        self.assertEqual(
            hub_core.status(
                tool,
                launchd_probe=lambda label: True,
                pgrep_probe=lambda pattern: False,
            ),
            Status.RUNNING,
        )

    def test_menubar_running_via_pgrep_fallback(self):
        tool = self._menubar_tool()
        self.assertEqual(
            hub_core.status(
                tool,
                launchd_probe=lambda label: False,
                pgrep_probe=lambda pattern: True,
            ),
            Status.RUNNING,
        )

    def test_menubar_stopped_when_both_probes_false(self):
        tool = self._menubar_tool()
        self.assertEqual(
            hub_core.status(
                tool,
                launchd_probe=lambda label: False,
                pgrep_probe=lambda pattern: False,
            ),
            Status.STOPPED,
        )

    def test_external_kind_is_inert_no_probing_no_crash(self):
        # An explicit kind="external" tool (no dir, no port, no launchd
        # fields at all) must return EXTERNAL without touching any probe.
        tool = {"id": "ext-tool", "label": "External Tool", "kind": "external"}
        self.assertEqual(
            hub_core.status(
                tool,
                tcp_probe=lambda h, p: (_ for _ in ()).throw(AssertionError("should not probe")),
                launchd_probe=lambda l: (_ for _ in ()).throw(AssertionError("should not probe")),
                pgrep_probe=lambda p: (_ for _ in ()).throw(AssertionError("should not probe")),
            ),
            Status.EXTERNAL,
        )

    def test_unknown_kind_is_inert_no_crash(self):
        # A forward-compat unknown kind (e.g. from a newer hub_tools.json)
        # must degrade to EXTERNAL instead of raising HubError.
        tool = {"id": "mystery-tool", "label": "Mystery Tool", "kind": "totally-new-kind"}
        self.assertEqual(hub_core.status(tool), Status.EXTERNAL)

    def test_dir_less_entry_status_no_crash(self):
        # A dir-less tool dict of unknown kind: status() must not KeyError
        # on tool["dir"] anywhere in the call chain.
        tool = {"id": "dirless-tool", "label": "Dirless Tool", "kind": "external"}
        self.assertEqual(hub_core.status(tool), Status.EXTERNAL)

    def test_status_accepts_precomputed_installed_flag(self):
        # installed=False short-circuits before any probe runs.
        tool = self._web_tool(installed_if_present=True)
        self.assertEqual(
            hub_core.status(
                tool,
                installed=False,
                tcp_probe=lambda host, port: True,
            ),
            Status.NOT_INSTALLED,
        )


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------

class TestLaunch(TmpDirTestCase):
    def test_launch_external_kind_is_noop_no_crash(self):
        tool = {"id": "ext-tool", "label": "External Tool", "kind": "external"}
        msg = hub_core.launch(tool)
        self.assertIn("external", msg)

    def test_launch_dir_less_unknown_kind_is_noop_no_crash(self):
        tool = {"id": "mystery-tool", "label": "Mystery Tool", "kind": "totally-new-kind"}
        msg = hub_core.launch(tool)
        self.assertIn("external", msg)

    def test_launch_web_is_noop_when_already_running(self):
        tool = {
            "id": "web-tool",
            "label": "Web Tool",
            "kind": "web",
            "dir": str(self.tmp),
            "launch": {"argv": ["true"]},
        }
        calls = []
        msg = hub_core.launch(
            tool,
            status_fn=lambda t: Status.RUNNING,
            popen=lambda *a, **k: calls.append((a, k)),
        )
        self.assertIn("already running", msg)
        self.assertEqual(calls, [])

    def test_launch_web_raises_when_not_installed(self):
        tool = {
            "id": "web-tool",
            "label": "Web Tool",
            "kind": "web",
            "dir": str(self.tmp),
            "launch": {"argv": ["true"]},
        }
        with self.assertRaises(HubError):
            hub_core.launch(tool, status_fn=lambda t: Status.NOT_INSTALLED)

    def test_launch_web_popens_with_start_new_session(self):
        tool = {
            "id": "web-tool",
            "label": "Web Tool",
            "kind": "web",
            "dir": str(self.tmp),
            "launch": {"argv": ["venv/bin/python", "app.py"]},
        }
        calls = []

        def fake_popen(argv, **kwargs):
            calls.append((argv, kwargs))
            return object()

        msg = hub_core.launch(
            tool,
            status_fn=lambda t: Status.STOPPED,
            popen=fake_popen,
        )
        self.assertIn("launching", msg)
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertEqual(argv, ["venv/bin/python", "app.py"])
        self.assertEqual(kwargs["cwd"], str(self.tmp))
        self.assertTrue(kwargs["start_new_session"])

    def test_launch_menubar_kickstarts_when_loaded(self):
        tool = {
            "id": "menubar-tool",
            "label": "Menubar Tool",
            "kind": "menubar",
            "dir": str(self.tmp),
            "launchd_label": "com.example.tool",
        }
        run_calls = []

        class FakeCompleted:
            def __init__(self, returncode):
                self.returncode = returncode

        def fake_run(argv, **kwargs):
            run_calls.append(argv)
            # first call: launchctl print -> loaded (returncode 0)
            return FakeCompleted(0)

        msg = hub_core.launch(
            tool,
            status_fn=lambda t: Status.STOPPED,
            run=fake_run,
        )
        self.assertIn("kickstart", msg)
        self.assertTrue(any("kickstart" in " ".join(c) for c in run_calls))
        # Never Popen for a launchd-labeled tool.

    def test_launch_menubar_never_popens_even_if_supplied(self):
        tool = {
            "id": "menubar-tool",
            "label": "Menubar Tool",
            "kind": "menubar",
            "dir": str(self.tmp),
            "launchd_label": "com.example.tool",
        }

        class FakeCompleted:
            returncode = 0

        popen_calls = []
        hub_core.launch(
            tool,
            status_fn=lambda t: Status.STOPPED,
            run=lambda argv, **k: FakeCompleted(),
            popen=lambda *a, **k: popen_calls.append((a, k)),
        )
        self.assertEqual(popen_calls, [])

    def test_launch_menubar_bootstraps_when_plist_on_disk_not_loaded(self):
        # Simulate: launchctl print fails (not loaded), but a plist exists.
        fake_home = self.tmp / "home"
        (fake_home / "Library" / "LaunchAgents").mkdir(parents=True)
        plist = fake_home / "Library" / "LaunchAgents" / "com.example.tool.plist"
        plist.write_text("x")

        tool = {
            "id": "menubar-tool",
            "label": "Menubar Tool",
            "kind": "menubar",
            "dir": str(self.tmp),
            "launchd_label": "com.example.tool",
        }

        class FakeCompleted:
            def __init__(self, returncode):
                self.returncode = returncode

        run_calls = []

        def fake_run(argv, **kwargs):
            run_calls.append(argv)
            if "print" in argv:
                return FakeCompleted(1)  # not loaded
            return FakeCompleted(0)

        orig_expanduser = Path.expanduser

        def fake_expanduser(self_path):
            s = str(self_path)
            if s.startswith("~/Library/LaunchAgents"):
                return fake_home / "Library" / "LaunchAgents" / self_path.name
            return orig_expanduser(self_path)

        Path.expanduser = fake_expanduser
        try:
            msg = hub_core.launch(
                tool,
                status_fn=lambda t: Status.STOPPED,
                run=fake_run,
            )
        finally:
            Path.expanduser = orig_expanduser

        self.assertIn("bootstrap", msg)


# ---------------------------------------------------------------------------
# open_ui
# ---------------------------------------------------------------------------

class TestOpenUi(TmpDirTestCase):
    def test_returns_none_for_menubar_kind(self):
        tool = {"kind": "menubar"}
        self.assertIsNone(hub_core.open_ui(tool, status_fn=lambda t: Status.RUNNING))

    def test_returns_none_when_not_running(self):
        cfg_path = self.tmp / "config.json"
        cfg_path.write_text(json.dumps({"port": 8747}))
        tool = {
            "kind": "web",
            "port": {"config": str(cfg_path), "key": "port", "default": 8747},
        }
        self.assertIsNone(hub_core.open_ui(tool, status_fn=lambda t: Status.STOPPED))

    def test_returns_url_when_running(self):
        cfg_path = self.tmp / "config.json"
        cfg_path.write_text(json.dumps({"port": 8747}))
        tool = {
            "kind": "web",
            "port": {"config": str(cfg_path), "key": "port", "default": 8747},
        }
        url = hub_core.open_ui(tool, status_fn=lambda t: Status.RUNNING)
        self.assertEqual(url, "http://127.0.0.1:8747/")

    def test_does_not_open_browser_by_default(self):
        # Regression guard: /api/open must be side-effect-free by default so
        # polling/tests never pop a real browser window.
        from gdrive_toolkit.hub import hub_core as hc

        cfg_path = self.tmp / "config.json"
        cfg_path.write_text(json.dumps({"port": 8747}))
        tool = {
            "kind": "web",
            "port": {"config": str(cfg_path), "key": "port", "default": 8747},
        }
        called = []
        orig = hc.webbrowser.open
        hc.webbrowser.open = lambda url: called.append(url)
        try:
            hc.open_ui(tool, status_fn=lambda t: Status.RUNNING)
        finally:
            hc.webbrowser.open = orig
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
