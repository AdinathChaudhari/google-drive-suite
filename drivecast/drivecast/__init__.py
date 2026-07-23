"""drivecast — stream video straight from Google Shared Drives (no downloads)."""
import os

__version__ = "0.2.0"


def _ensure_cli_path():
    """Prepend common Homebrew/local bin dirs to PATH.

    A GUI .app launched from Finder/Spotlight inherits a minimal PATH
    (/usr/bin:/bin:/usr/sbin:/sbin) that omits Homebrew, so bare-name lookups
    for `rclone`, `mpv`, `ffprobe` fail even when they're installed. Prepending
    the usual locations repairs discovery for both subprocess calls and
    shutil.which, whether run as a bundle or from a terminal.
    """
    extra = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]
    current = os.environ.get("PATH", "").split(os.pathsep)
    additions = [d for d in extra if os.path.isdir(d) and d not in current]
    if additions:
        os.environ["PATH"] = os.pathsep.join(additions + current)


_ensure_cli_path()
