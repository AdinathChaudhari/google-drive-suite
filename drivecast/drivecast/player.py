"""External player launch + resume tracking.

Player preference: mpv (on PATH) -> IINA (iina-cli) -> VLC (direct binary).
For mpv/IINA/VLC we never use `open`; we invoke the binary directly so we can
pass flags and know when it exits.

mpv / IINA expose a JSON IPC socket we poll for playback-time so we can save
resume positions. VLC is tracked over its HTTP interface: we launch it with a
loopback HTTP server on a private port + random password, then poll
/requests/status.xml for the current time/length and save exactly like mpv. If
the interface never comes up (older VLC, busy port) we degrade to launch-only.
Infuse (macOS app) is launch-only: we open its infuse:// URL scheme via `open`
— it has no IPC/HTTP interface, so there is no resume tracking, watched-
marking, or autoplay for it.
"""
import base64
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

log = logging.getLogger("drivecast.player")

IINA_CLI = "/Applications/IINA.app/Contents/MacOS/iina-cli"
VLC_BIN = "/Applications/VLC.app/Contents/MacOS/VLC"
INFUSE_APP = "/Applications/Infuse.app"

SOCKET_WAIT_SECONDS = 10.0
POLL_INTERVAL = 3.0
# VLC HTTP interface: a private loopback port (away from the app's 8737) and a
# window to wait for the interface to come up before giving up on tracking.
VLC_HTTP_PORT_START = 8738
VLC_HTTP_TIMEOUT = 2.0        # per-request timeout (seconds)
VLC_STARTUP_GRACE = 20.0      # keep polling this long for the interface to appear

# Network buffering + hardware decode flags: the file is streamed over HTTP from
# Drive, so a generous demuxer cache + readahead hides latency and hiccups, and
# hw decode keeps 4K smooth. These are the biggest playback-speed win.
MPV_CACHE_FLAGS = [
    "--cache=yes",
    "--cache-secs=30",
    "--demuxer-max-bytes=150MiB",
    "--demuxer-max-back-bytes=50MiB",
    "--demuxer-readahead-secs=20",
    "--hwdec=auto-safe",
    "--force-seekable=yes",
    "--network-timeout=30",
]
# IINA takes the same options prefixed with --mpv-.
IINA_CACHE_FLAGS = ["--mpv-" + flag[2:] for flag in MPV_CACHE_FLAGS]


# How close to the end counts as "finished" (so we advance to the next episode
# instead of treating it as a mid-episode quit): the last 10% OR the last ~90s.
FINISH_FRACTION = 0.9
FINISH_TAIL_SECONDS = 90.0


def should_advance(position, duration):
    """Return True if playback reached the end (finished), False if quit early.

    Finished = duration is known (> 0) AND the last position is within the last
    10% of the file OR within ~90s of the end. Unknown / zero duration returns
    False: we can't tell, so we treat it as a mid-episode quit and do NOT
    auto-advance. This is a pure function so the finished-vs-quit rule is
    unit-testable without launching a player.
    """
    try:
        position = float(position)
        duration = float(duration)
    except (TypeError, ValueError):
        return False
    if duration <= 0:
        return False
    if position >= FINISH_FRACTION * duration:
        return True
    return (duration - position) <= FINISH_TAIL_SECONDS


def build_mpv_args(path, sock, resume, name, url, media=None, sub_path=None):
    """Construct the mpv command line (IPC + resume + title + cache/hwdec).

    Audio-only files need --force-window: mpv runs with --no-terminal, so
    without a window there'd be no UI at all to pause/seek/quit from.
    `sub_path` is a LOCAL subtitle file to preload alongside the stream.
    """
    args = [
        path,
        "--input-ipc-server=%s" % sock,
        "--start=%d" % int(resume),
        "--force-media-title=%s" % name,
        "--no-terminal",
        *MPV_CACHE_FLAGS,
    ]
    if media == "audio":
        args.append("--force-window=immediate")
    if sub_path:
        args.append("--sub-file=%s" % sub_path)
    args.append(url)
    return args


def build_iina_args(path, sock, resume, name, url, sub_path=None):
    """Construct the IINA command line (mpv-prefixed IPC + resume + cache/hwdec)."""
    args = [
        path,
        "--mpv-input-ipc-server=%s" % sock,
        "--mpv-start=%d" % int(resume),
        "--mpv-force-media-title=%s" % name,
        *IINA_CACHE_FLAGS,
    ]
    if sub_path:
        args.append("--mpv-sub-file=%s" % sub_path)
    args.append(url)
    return args


def build_vlc_args(path, resume, name, url, http_port, http_password, sub_path=None):
    """Construct the VLC command line with its HTTP interface + resume enabled.

    The HTTP interface lets us poll playback position (see parse_vlc_status);
    --start-time resumes and --play-and-exit closes VLC when the file ends.
    """
    args = [
        path,
        "--extraintf", "http",
        "--http-host", "127.0.0.1",
        "--http-port", str(http_port),
        "--http-password", http_password,
        "--start-time=%d" % int(resume),
        "--play-and-exit",
        "--meta-title", name,
    ]
    if sub_path:
        args += ["--sub-file", sub_path]
    args.append(url)
    return args


def build_infuse_url(url, resume=0, name=None, sub_url=None):
    """Construct the infuse:// x-callback URL for launch-only playback.

    Infuse has no IPC/HTTP interface, so this is the whole integration:
    `open` hands the URL to Infuse.app. `position` resumes (seconds),
    `filename` gives Infuse a real name for metadata (our /stream URLs are
    opaque file ids), `sub` is an HTTP URL to a subtitle file. Pure function
    so the encoding is unit-testable.
    """
    parts = ["infuse://x-callback-url/play?url=%s" % urllib.parse.quote(url, safe="")]
    if resume and int(resume) > 0:
        parts.append("position=%d" % int(resume))
    if name:
        parts.append("filename=%s" % urllib.parse.quote(name, safe=""))
    if sub_url:
        parts.append("sub=%s" % urllib.parse.quote(sub_url, safe=""))
    return "&".join(parts)


def parse_vlc_status(xml_text):
    """Parse VLC's /requests/status.xml into (time, length) floats.

    Returns (None, None) on any parse failure or missing fields. Robust to the
    many extra fields VLC includes — we only read <time> and <length>.
    """
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, TypeError):
        return (None, None)

    def _num(text):
        try:
            return float(text)
        except (TypeError, ValueError):
            return None

    return (_num(root.findtext("time")), _num(root.findtext("length")))


def _find_free_port(start=VLC_HTTP_PORT_START, tries=50):
    """Return the first bindable loopback port at/after `start`, else `start`."""
    for port in range(start, start + tries):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    return start


def detect_player(preference="auto"):
    """Return (kind, path); kind in {"mpv","iina","vlc","infuse"} or (None, None).

    "auto" tries mpv -> IINA -> VLC only: Infuse is launch-only (no position
    tracking), so it must be chosen explicitly, never auto-picked.
    """
    if preference in ("mpv", "iina", "vlc", "infuse"):
        order = [preference]
    else:
        order = ["mpv", "iina", "vlc"]
    for kind in order:
        if kind == "mpv":
            path = shutil.which("mpv")
            if path:
                return "mpv", path
        elif kind == "iina":
            if os.path.exists(IINA_CLI):
                return "iina", IINA_CLI
        elif kind == "vlc":
            if os.path.exists(VLC_BIN):
                return "vlc", VLC_BIN
        elif kind == "infuse":
            if os.path.exists(INFUSE_APP):
                return "infuse", INFUSE_APP
    return None, None


class PlayerError(Exception):
    pass


class PlayerManager:
    def __init__(self, cfg, history, base_url):
        self.cfg = cfg
        self.history = history
        self.base_url = base_url.rstrip("/")
        # Optional sync file_id -> cached subtitle path lookup (set by the
        # server) so autoplay-advanced episodes pick up cached subs too.
        self.sub_cache_lookup = None
        self._session = None  # dict for the currently tracked session
        self._session_lock = threading.Lock()

    def stream_url(self, file_id):
        return "%s/stream/%s" % (self.base_url, file_id)

    def play(self, file_id, name, duration_ms=None, drive_id=None, parent_id=None,
             queue=None, media=None, sub_path=None):
        """Launch the configured player at the resume position.

        `queue` is an optional ordered list of upcoming items
        [{file_id, name, duration_ms}] to play AFTER this one (autoplay). When
        this file finishes, the next queue item launches automatically, chaining
        through the whole queue. If `autoplay_next` config is off the queue is
        ignored and only this item plays.

        Returns {"player": kind, "resumed_from": seconds}.
        """
        kind, path = detect_player(self.cfg.get("player", "auto"))
        if not kind:
            raise PlayerError(
                "No supported player found. Install mpv for best results: brew install mpv"
            )

        # Autoplay disabled -> ignore the queue entirely (play only this item).
        if not self.cfg.get("autoplay_next", True):
            queue = None

        # Stop any prior tracked poller before starting a new session.
        self._stop_current_session()

        resume = self._launch(kind, path, file_id, name, duration_ms,
                              drive_id, parent_id, list(queue or []), media=media,
                              sub_path=sub_path)
        return {"player": kind, "resumed_from": resume}

    # ---- launchers ----

    def _launch(self, kind, path, file_id, name, duration_ms, drive_id, parent_id, queue,
                media=None, sub_path=None):
        """Resolve resume/url/duration and dispatch to the per-player launcher.

        Shared by play() and the autoplay-advance path so both start a session
        identically. Returns the resume position (seconds).
        """
        resume = self.history.resume_position(file_id)
        url = self.stream_url(file_id)
        duration = (float(duration_ms) / 1000.0) if duration_ms else None
        if kind == "mpv":
            self._launch_mpv(path, file_id, name, url, resume, duration, drive_id,
                             parent_id, queue, media=media, sub_path=sub_path)
        elif kind == "iina":
            self._launch_iina(path, file_id, name, url, resume, duration, drive_id,
                              parent_id, queue, sub_path=sub_path)
        elif kind == "vlc":
            self._launch_vlc(path, file_id, name, url, resume, duration, drive_id,
                             parent_id, queue, sub_path=sub_path)
        else:  # infuse
            self._launch_infuse(file_id, name, url, resume, duration,
                                drive_id, parent_id, sub_path=sub_path)
        return resume

    def _launch_mpv(self, path, file_id, name, url, resume, duration, drive_id,
                    parent_id, queue, media=None, sub_path=None):
        sock = "/tmp/drivecast-%s.sock" % uuid.uuid4().hex[:12]
        args = build_mpv_args(path, sock, resume, name, url, media=media,
                              sub_path=sub_path)
        proc = subprocess.Popen(args)
        self._start_poller("mpv", path, proc, sock, file_id, name, duration,
                           drive_id, parent_id, queue)

    def _launch_iina(self, path, file_id, name, url, resume, duration, drive_id,
                     parent_id, queue, sub_path=None):
        sock = "/tmp/drivecast-%s.sock" % uuid.uuid4().hex[:12]
        args = build_iina_args(path, sock, resume, name, url, sub_path=sub_path)
        proc = subprocess.Popen(args)
        self._start_poller("iina", path, proc, sock, file_id, name, duration,
                           drive_id, parent_id, queue)

    def _launch_vlc(self, path, file_id, name, url, resume, duration, drive_id,
                    parent_id, queue, sub_path=None):
        http_port = _find_free_port()
        http_password = uuid.uuid4().hex
        args = build_vlc_args(path, resume, name, url, http_port, http_password,
                              sub_path=sub_path)
        try:
            proc = subprocess.Popen(args)
        except OSError as exc:
            log.debug("VLC launch failed: %r", exc)
            self.history.update(file_id, name=name, drive_id=drive_id,
                                parent_id=parent_id, force=True)
            return
        self._start_vlc_poller(path, proc, http_port, http_password, file_id, name,
                               duration, drive_id, parent_id, queue)

    def _launch_infuse(self, file_id, name, url, resume, duration, drive_id,
                       parent_id, sub_path=None):
        """Open the stream in Infuse via its URL scheme. LAUNCH-ONLY.

        Infuse exposes no IPC/HTTP interface, so we can't poll position: no
        Continue Watching updates, no watched-on-finish, no autoplay-next.
        We seed a history entry so the item still shows up with its metadata,
        then hand off to `open` and forget about it.
        """
        sub_url = None
        if sub_path:
            # Infuse can't read our sandboxed local cache path; hand it the
            # server's own subtitle endpoint instead.
            sub_url = "%s/api/subtitles/%s" % (self.base_url, file_id)
        infuse_url = build_infuse_url(url, resume=resume, name=name, sub_url=sub_url)
        self.history.update(file_id, name=name, drive_id=drive_id,
                            parent_id=parent_id, duration=duration, force=True)
        try:
            subprocess.Popen(["open", infuse_url])
        except OSError as exc:
            log.debug("Infuse launch failed: %r", exc)

    # ---- IPC poller (mpv / IINA) ----

    def _stop_current_session(self):
        with self._session_lock:
            sess = self._session
            self._session = None
        if sess:
            sess["stop"].set()

    def _advance(self, kind, path, queue, drive_id, parent_id, stop):
        """Launch the next queued item after the current one finished.

        Called from a poller's finally block ONLY when should_advance said the
        file finished. Skips if a newer session superseded this one (stop set),
        if autoplay is off, or if the queue is empty. Chains the remainder of the
        queue onto the next item so playback carries through the whole list.
        """
        if stop.is_set():
            return  # a newer play() replaced this session; don't chain.
        if not self.cfg.get("autoplay_next", True):
            return
        if not queue:
            return
        nxt, rest = queue[0], queue[1:]
        sub_path = None
        if self.sub_cache_lookup:
            try:
                sub_path = self.sub_cache_lookup(nxt.get("file_id"))
            except Exception:  # pragma: no cover - defensive
                sub_path = None
        try:
            self._launch(kind, path, nxt.get("file_id"),
                         nxt.get("name") or nxt.get("file_id"),
                         nxt.get("duration_ms"), drive_id, parent_id, rest,
                         media=nxt.get("media"), sub_path=sub_path)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("autoplay advance failed: %r", exc)

    def _start_poller(self, kind, path, proc, sock, file_id, name, duration,
                      drive_id, parent_id, queue):
        stop = threading.Event()
        session = {"stop": stop, "sock": sock, "proc": proc, "file_id": file_id}
        with self._session_lock:
            self._session = session
        t = threading.Thread(
            target=self._poll_loop,
            args=(kind, path, proc, sock, file_id, name, duration, drive_id,
                  parent_id, queue, stop),
            daemon=True,
        )
        t.start()

    def _poll_loop(self, kind, path, proc, sock, file_id, name, duration,
                   drive_id, parent_id, queue, stop):
        # Seed an entry so it appears immediately / carries metadata.
        self.history.update(file_id, name=name, drive_id=drive_id,
                            parent_id=parent_id, duration=duration, force=True)

        conn = self._wait_for_socket(sock, stop)
        if conn is None:
            log.debug("%s IPC socket never appeared for %s; launch-only mode", kind, file_id)
            self._cleanup_socket(sock)
            return

        last_pos = 0.0
        got_duration = duration
        try:
            while not stop.is_set():
                if proc.poll() is not None:
                    break  # player exited
                pos = self._ipc_get(conn, "playback-time")
                if pos is not None:
                    last_pos = float(pos)
                    if got_duration is None:
                        d = self._ipc_get(conn, "duration")
                        if d:
                            got_duration = float(d)
                    self.history.update(file_id, name=name, drive_id=drive_id,
                                        parent_id=parent_id, position=last_pos,
                                        duration=got_duration)
                # Wait POLL_INTERVAL but wake early if asked to stop.
                if stop.wait(POLL_INTERVAL):
                    break
        except Exception as exc:
            log.debug("poller error for %s: %r", file_id, exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass
            # Final save: mark watched + advance if we reached the end.
            final_dur = got_duration or 0.0
            finished = should_advance(last_pos, final_dur)
            if finished:
                self.history.update(file_id, name=name, drive_id=drive_id,
                                    parent_id=parent_id, position=last_pos,
                                    duration=final_dur, force=True)
                self.history.mark_watched(file_id, True)
            else:
                self.history.update(file_id, name=name, drive_id=drive_id,
                                    parent_id=parent_id, position=last_pos,
                                    duration=got_duration, force=True)
            self._cleanup_socket(sock)
            if finished:
                self._advance(kind, path, queue, drive_id, parent_id, stop)

    # ---- HTTP poller (VLC) ----

    def _start_vlc_poller(self, path, proc, http_port, http_password, file_id, name,
                          duration, drive_id, parent_id, queue):
        stop = threading.Event()
        session = {"stop": stop, "proc": proc, "file_id": file_id}
        with self._session_lock:
            self._session = session
        t = threading.Thread(
            target=self._vlc_poll_loop,
            args=(path, proc, http_port, http_password, file_id, name, duration,
                  drive_id, parent_id, queue, stop),
            daemon=True,
        )
        t.start()

    def _vlc_poll_loop(self, path, proc, http_port, http_password, file_id, name,
                       duration, drive_id, parent_id, queue, stop):
        # Seed an entry immediately (also the launch-only fallback if the HTTP
        # interface never answers).
        self.history.update(file_id, name=name, drive_id=drive_id,
                            parent_id=parent_id, duration=duration, force=True)

        status_url = "http://127.0.0.1:%d/requests/status.xml" % http_port
        # VLC's HTTP interface uses HTTP Basic auth: empty username, our password.
        token = base64.b64encode((":" + http_password).encode("utf-8")).decode("ascii")
        headers = {"Authorization": "Basic " + token}

        last_pos = 0.0
        got_duration = duration
        ever_connected = False
        deadline = time.time() + VLC_STARTUP_GRACE
        try:
            while not stop.is_set():
                if proc.poll() is not None:
                    break  # VLC exited
                pos, length = self._vlc_status(status_url, headers)
                if pos is not None:
                    ever_connected = True
                    last_pos = pos
                    if got_duration is None and length:
                        got_duration = length
                    self.history.update(file_id, name=name, drive_id=drive_id,
                                        parent_id=parent_id, position=last_pos,
                                        duration=got_duration)
                elif not ever_connected and time.time() > deadline:
                    # Interface never came up: leave launch-only and stop polling.
                    log.debug("VLC HTTP interface unreachable for %s; launch-only", file_id)
                    return
                if stop.wait(POLL_INTERVAL):
                    break
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("VLC poller error for %s: %r", file_id, exc)
        finally:
            # Only VLC instances whose HTTP interface answered give us a position
            # to judge finished-vs-quit; launch-only VLC can't autoplay.
            if ever_connected:
                final_dur = got_duration or 0.0
                finished = should_advance(last_pos, final_dur)
                if finished:
                    self.history.update(file_id, name=name, drive_id=drive_id,
                                        parent_id=parent_id, position=last_pos,
                                        duration=final_dur, force=True)
                    self.history.mark_watched(file_id, True)
                else:
                    self.history.update(file_id, name=name, drive_id=drive_id,
                                        parent_id=parent_id, position=last_pos,
                                        duration=got_duration, force=True)
                if finished:
                    self._advance("vlc", path, queue, drive_id, parent_id, stop)

    def _vlc_status(self, status_url, headers):
        """GET VLC's status.xml and return (time, length); (None, None) on error."""
        try:
            req = urllib.request.Request(status_url, headers=headers)
            with urllib.request.urlopen(req, timeout=VLC_HTTP_TIMEOUT) as resp:
                body = resp.read()
            return parse_vlc_status(body.decode("utf-8", "replace"))
        except Exception:
            return (None, None)

    def _wait_for_socket(self, sock, stop, timeout=SOCKET_WAIT_SECONDS):
        deadline = time.time() + timeout
        while time.time() < deadline and not stop.is_set():
            if os.path.exists(sock):
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(2.0)
                    s.connect(sock)
                    return s
                except OSError:
                    pass
            if stop.wait(0.25):
                return None
        return None

    def _ipc_get(self, conn, prop):
        """Send a get_property command and return its value, or None."""
        cmd = json.dumps({"command": ["get_property", prop]}) + "\n"
        try:
            conn.sendall(cmd.encode("utf-8"))
            conn.settimeout(2.0)
            buf = b""
            # mpv may emit event lines; read until we see a line with our result.
            deadline = time.time() + 2.0
            while time.time() < deadline:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except ValueError:
                        continue
                    if "error" in msg and "data" in msg:
                        if msg.get("error") == "success":
                            return msg.get("data")
                        return None
            return None
        except OSError:
            return None

    def _cleanup_socket(self, sock):
        try:
            if os.path.exists(sock):
                os.unlink(sock)
        except OSError:
            pass
