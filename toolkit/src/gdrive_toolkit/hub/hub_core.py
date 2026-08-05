"""hub_core.py — form-factor-independent core: detection, status, launch.

Stdlib only. No flask/rumps import here — this module is shared by both a
menubar front-end and a web-dashboard front-end (or any other future one).

Every probe (TCP connect, launchctl, pgrep) is a keyword argument with a real
default implementation, so tests can inject fakes without touching real
processes or the network.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import webbrowser
from enum import Enum
from pathlib import Path
from typing import Callable, Optional


class Status(Enum):
    NOT_INSTALLED = "not_installed"
    STOPPED = "stopped"
    RUNNING = "running"
    # Inert placeholder for a registry entry the hub can show but never
    # probes or launches: an explicit kind="external" tool, or any kind this
    # version of hub_core doesn't recognize (forward-compat with a newer
    # hub_tools.json entry). Rendered greyed-out/non-clickable in menubar.py.
    EXTERNAL = "external"


class HubError(Exception):
    """Raised when launch() cannot start or control a tool."""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _tool_dir(tool: dict) -> Optional[Path]:
    """None for a dir-less tool dict (e.g. an external/unknown-kind entry
    that never needs a local install path) instead of KeyError."""
    d = tool.get("dir")
    if d is None:
        return None
    return Path(d).expanduser()


def _resolve_rel(tool_dir: Path, rel: str) -> Path:
    """A path from installed_if/installed_if_any: expanduser, then join to
    tool_dir if still relative."""
    p = Path(rel).expanduser()
    if not p.is_absolute():
        p = tool_dir / p
    return p


# ---------------------------------------------------------------------------
# is_installed
# ---------------------------------------------------------------------------

def is_installed(tool: dict, *, exists: Callable[[Path], bool] = Path.exists) -> bool:
    tool_dir = _tool_dir(tool)
    if tool_dir is None:
        # A dir-less tool dict (external/unknown kind) has no local install
        # to check — treat it as not locally installed rather than KeyError.
        return False

    if "installed_if" in tool:
        return all(exists(_resolve_rel(tool_dir, rel)) for rel in tool["installed_if"])

    if "installed_if_any" in tool:
        return any(exists(_resolve_rel(tool_dir, rel)) for rel in tool["installed_if_any"])

    # No installation predicate declared: assume present (shouldn't happen
    # for a well-formed registry entry).
    return True


# ---------------------------------------------------------------------------
# resolve_port
# ---------------------------------------------------------------------------

def resolve_port(tool: dict) -> int:
    """The port to probe for a kind="web" tool.

    Only "default" is required. "config"/"key" are the *override* half of the
    spec — a tool whose port is hardcoded in its own source has no config file
    to read and legitimately declares just {"default": N}.

    Treating config/key as required was a real outage: the KeyError escaped
    through status(), and menubar.refresh() catches any exception there into a
    permanently greyed "<label> — error" row. So a hand-written hub_tools.json
    entry lost its menu item with nothing in the log to say why.
    """
    port_spec = tool.get("port")
    if not port_spec:
        raise HubError("tool %r has no 'port' spec" % tool.get("id"))

    if "default" not in port_spec:
        raise HubError(
            "tool %r has a 'port' spec with no 'default'" % tool.get("id"))
    default = port_spec["default"]

    config = port_spec.get("config")
    key = port_spec.get("key")
    if not config or not key:
        return int(default)
    config_path = Path(config).expanduser()

    try:
        with config_path.open("r") as f:
            data = json.load(f)
        return int(data[key])
    except Exception:
        return int(default)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def _default_tcp_probe(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _default_launchd_probe(label: str) -> bool:
    """True if `launchctl print gui/<uid>/<label>` reports state = running."""
    uid = os.getuid()
    try:
        result = subprocess.run(
            ["launchctl", "print", "gui/%d/%s" % (uid, label)],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    return "state = running" in result.stdout


def _default_pgrep_probe(pattern: str) -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def status(
    tool: dict,
    *,
    installed: Optional[bool] = None,
    tcp_probe: Callable[[str, int], bool] = _default_tcp_probe,
    launchd_probe: Callable[[str], bool] = _default_launchd_probe,
    pgrep_probe: Callable[[str], bool] = _default_pgrep_probe,
) -> Status:
    kind = tool.get("kind")

    # An explicit external entry, or any kind this version of hub_core
    # doesn't recognize (forward-compat with a newer hub_tools.json written
    # for a future hub_core), is inert: no probing, no crash.
    if kind == "external" or kind not in ("web", "menubar"):
        return Status.EXTERNAL

    if installed is None:
        installed = is_installed(tool)
    if not installed:
        return Status.NOT_INSTALLED

    if kind == "web":
        port = resolve_port(tool)
        if tcp_probe("127.0.0.1", port):
            return Status.RUNNING
        return Status.STOPPED

    # kind == "menubar"
    label = tool.get("launchd_label")
    if label and launchd_probe(label):
        return Status.RUNNING
    pattern = tool.get("process_pattern")
    if pattern and pgrep_probe(pattern):
        return Status.RUNNING
    return Status.STOPPED


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------

def _log_path(tool_id: str) -> Path:
    from ..common.config import LOG_DIR
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / ("%s.log" % tool_id)


def _launchd_loaded(label: str, *, launchctl_print: Callable[[str], bool]) -> bool:
    return launchctl_print(label)


def launch(
    tool: dict,
    *,
    status_fn: Callable[[dict], Status] = status,
    popen: Callable[..., object] = subprocess.Popen,
    run: Callable[..., object] = subprocess.run,
) -> str:
    # Re-probe RUNNING at call time -> no-op. Never trust cached status.
    current = status_fn(tool)
    if current == Status.EXTERNAL:
        return "%s is external — nothing to launch" % tool.get("label", tool.get("id", "tool"))
    if current == Status.RUNNING:
        return "%s already running" % tool["label"]
    if current == Status.NOT_INSTALLED:
        raise HubError("%s is not installed" % tool["label"])

    tool_dir = _tool_dir(tool)

    if tool["kind"] == "web":
        argv = tool["launch"]["argv"]
        log_file = _log_path(tool["id"])
        with open(log_file, "ab") as log_f:
            popen(
                argv,
                cwd=str(tool_dir),
                stdout=log_f,
                stderr=log_f,
                start_new_session=True,
            )
        return "launching %s" % tool["label"]

    if tool["kind"] == "menubar":
        label = tool.get("launchd_label")
        if label:
            uid = os.getuid()
            plist_path = Path("~/Library/LaunchAgents/%s.plist" % label).expanduser()

            loaded = run(
                ["launchctl", "print", "gui/%d/%s" % (uid, label)],
                capture_output=True, text=True, timeout=3,
            )
            is_loaded = getattr(loaded, "returncode", 1) == 0

            if is_loaded:
                run(["launchctl", "kickstart", "-k", "gui/%d/%s" % (uid, label)],
                    capture_output=True, text=True, timeout=5)
                return "restarting %s via launchctl kickstart" % tool["label"]

            if plist_path.exists():
                run(["launchctl", "bootstrap", "gui/%d" % uid, str(plist_path)],
                    capture_output=True, text=True, timeout=5)
                return "bootstrapping %s via launchctl" % tool["label"]

            app_candidates = [
                _resolve_rel(tool_dir, rel)
                for rel in tool.get("installed_if_any", [])
                if str(rel).endswith(".app")
            ]
            app_path = next((p for p in app_candidates if p.exists()), None)
            if app_path is not None:
                run(["open", "-a", str(app_path)], capture_output=True, text=True, timeout=5)
                return "opening %s" % tool["label"]

            raise HubError(
                "%s has a launchd_label but no loaded agent, plist, or .app found"
                % tool["label"]
            )
        else:
            # menubar tool without a launchd label: not supported in v1
            # (would require a plain Popen, which is fine only when there's
            # no launchd agent to conflict with).
            raise HubError(
                "%s is a menubar tool with no launchd_label — no launch path defined"
                % tool["label"]
            )

    raise HubError("tool %r has unknown kind %r" % (tool.get("id"), tool.get("kind")))


# ---------------------------------------------------------------------------
# stop / restart
# ---------------------------------------------------------------------------
#
# WHY STOP USES `bootout` AND NOT `launchctl kill`
#
# Both menubar agents in this suite set KeepAlive to the dict form
# {SuccessfulExit: false} so that each app's own "Quit" menu item — a clean
# exit 0 — actually sticks instead of being resurrected by launchd. But that
# predicate treats death-by-signal as an UNSUCCESSFUL exit, so
# `launchctl kill SIGTERM gui/<uid>/<label>` would be relaunched immediately:
# exactly the "I quit it and it came back" bug, reintroduced from the outside.
#
# `bootout` removes the job from the user's launchd domain entirely, so there
# is no KeepAlive predicate left to evaluate. It also escalates to SIGKILL on
# its own, which is what makes it the recovery path for a WEDGED app whose
# menu bar no longer responds to clicks (its own Quit item is unreachable
# then, so an outside stop is the only way).
#
# The mirror of bootout is `bootstrap`, which launch() already does whenever
# it finds a plist on disk that isn't loaded — so stop()/launch() compose into
# a working off/on pair with no extra state.

def _launchd_label_or_raise(tool: dict, action: str) -> str:
    """The tool's launchd label, or a HubError explaining why `action` can't
    apply. Keeps stop()/restart() honest about their narrow scope: only
    launchd-managed menubar agents, never web tools (nothing owns their
    lifecycle) or inert external entries."""
    kind = tool.get("kind")
    label = tool.get("label", tool.get("id", "tool"))

    if kind == "external" or kind not in ("web", "menubar"):
        return_msg = "%s is external — nothing to %s" % (label, action)
        raise HubError(return_msg)
    if kind == "web":
        raise HubError(
            "%s is a web tool — %s is only defined for launchd-managed "
            "menubar agents (close its terminal or browser tab instead)"
            % (label, action)
        )

    launchd_label = tool.get("launchd_label")
    if not launchd_label:
        raise HubError(
            "%s has no launchd_label — nothing for %s to act on" % (label, action)
        )
    return launchd_label


def stop(
    tool: dict,
    *,
    status_fn: Callable[[dict], Status] = status,
    run: Callable[..., object] = subprocess.run,
) -> str:
    """Unload a launchd-managed menubar agent so it stays down.

    Deliberately does NOT re-probe status first, unlike launch(): a wedged app
    can report RUNNING while being completely unresponsive, and booting out an
    already-stopped job is harmless (bootout on an unloaded label just returns
    non-zero, which we swallow). Being able to stop something the status probe
    disagrees about is the whole point of this action.
    """
    launchd_label = _launchd_label_or_raise(tool, "stop")
    uid = os.getuid()

    result = run(
        ["launchctl", "bootout", "gui/%d/%s" % (uid, launchd_label)],
        capture_output=True, text=True, timeout=10,
    )
    rc = getattr(result, "returncode", 0)
    # rc 3 / EINVAL == "no such process" — the job was already unloaded, which
    # is the requested end state, so report success rather than an error.
    if rc not in (0, 3):
        stderr = (getattr(result, "stderr", "") or "").strip()
        raise HubError(
            "could not stop %s (launchctl bootout rc=%s%s)"
            % (tool["label"], rc, ": %s" % stderr if stderr else "")
        )
    return "stopped %s — it will stay down until you start it again" % tool["label"]


def restart(
    tool: dict,
    *,
    status_fn: Callable[[dict], Status] = status,
    run: Callable[..., object] = subprocess.run,
    launch_fn: Optional[Callable[..., str]] = None,
) -> str:
    """Restart a launchd-managed menubar agent.

    `kickstart -k` kills any running instance and starts a fresh one in a
    single step, which is the fix for the wedged-menu-bar case. If the job
    isn't loaded at all (e.g. after a previous stop()), fall through to
    launch(), which bootstraps the plist back in.
    """
    launchd_label = _launchd_label_or_raise(tool, "restart")
    uid = os.getuid()

    loaded = run(
        ["launchctl", "print", "gui/%d/%s" % (uid, launchd_label)],
        capture_output=True, text=True, timeout=3,
    )
    if getattr(loaded, "returncode", 1) != 0:
        # Not loaded — this is a start, not a restart. launch() owns the
        # bootstrap/open-the-.app fallback chain; don't duplicate it here.
        fn = launch_fn if launch_fn is not None else launch
        return fn(tool, status_fn=lambda t: Status.STOPPED, run=run)

    result = run(
        ["launchctl", "kickstart", "-k", "gui/%d/%s" % (uid, launchd_label)],
        capture_output=True, text=True, timeout=10,
    )
    rc = getattr(result, "returncode", 0)
    if rc != 0:
        stderr = (getattr(result, "stderr", "") or "").strip()
        raise HubError(
            "could not restart %s (launchctl kickstart rc=%s%s)"
            % (tool["label"], rc, ": %s" % stderr if stderr else "")
        )
    return "restarted %s" % tool["label"]


# ---------------------------------------------------------------------------
# open_ui
# ---------------------------------------------------------------------------

def open_ui(
    tool: dict,
    *,
    status_fn: Callable[[dict], Status] = status,
    also_open_browser: bool = False,
) -> Optional[str]:
    """web + RUNNING: return the http URL. Returns None otherwise.

    The web dashboard front-end uses the returned URL to `window.open()` it
    client-side (so /api/open/<id> is a pure, side-effect-free GET — no
    server-side browser pops during tests or polling). Pass
    also_open_browser=True for a front-end (e.g. a future menubar) that wants
    the server process itself to open the browser.
    """
    if tool["kind"] != "web":
        return None
    if status_fn(tool) != Status.RUNNING:
        return None
    url = "http://127.0.0.1:%d/" % resolve_port(tool)
    if also_open_browser:
        webbrowser.open(url)
    return url
