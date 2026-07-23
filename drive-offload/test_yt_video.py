"""Tests for yt-video's movie-title cleaner and yt-show's empty-folder upload
path (the one yt-show change yt-video relies on)."""
import importlib.machinery
import importlib.util
import os

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _load(name, filename):
    path = os.path.join(SCRIPT_DIR, filename)
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ytvideo = _load("yt_video_mod", "yt-video")
ytshow = _load("yt_show_mod", "yt-show")


@pytest.mark.parametrize("raw,expected", [
    ("Watch Full Movie Interstellar (2014) HD 1080p x265", "Interstellar (2014)"),
    ("INTERSTELLAR 2014 1080p BluRay x264 AAC", "Interstellar (2014)"),
    ("The Matrix (1999) - Full Movie", "The Matrix (1999)"),
    ("Scary Movie 2000 720p WEBRip", "Scary Movie (2000)"),   # 'Movie' survives
    ("john.wick.2014.1080p.web-dl", "John Wick (2014)"),
    ("Blade Runner 2049 (2017) 4K HDR", "Blade Runner 2049 (2017)"),  # year-in-title kept
])
def test_clean_movie_title_common(raw, expected):
    name, year = ytvideo.clean_movie_title(raw)
    assert ytvideo.format_movie_name(name, year) == expected


def test_format_movie_name_no_year():
    assert ytvideo.format_movie_name("Some Doc", None) == "Some Doc"


def test_no_year_title_passes_through():
    name, year = ytvideo.clean_movie_title("Some Random Vlog No Year Here")
    assert year is None
    assert name == "Some Random Vlog No Year Here"


def _capture_dest(monkeypatch, folder):
    """Run upload_file's no-progress path with a stubbed subprocess.run and
    return the rclone dest argument it built."""
    captured = {}

    class _R:
        returncode = 0

    def _fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return _R()

    monkeypatch.setattr(ytshow.subprocess, "run", _fake_run)
    rc = ytshow.upload_file("/tmp/local.mp4", "gdrive,team_drive=ID:",
                            folder, "Movie (2019).mp4")
    assert rc == 0
    # dest is the 3rd token: [rclone, moveto, <src>, <dest>, ...]
    return captured["cmd"][3]


def test_upload_root_when_folder_empty(monkeypatch):
    # yt-video's plain-file path: empty folder -> straight to the drive root,
    # NO stray leading slash.
    assert _capture_dest(monkeypatch, "") == "gdrive,team_drive=ID:Movie (2019).mp4"


def test_upload_into_folder_unchanged(monkeypatch):
    # yt-show's season-folder path must be untouched by the empty-folder tweak.
    assert (_capture_dest(monkeypatch, "My Show Season 1")
            == "gdrive,team_drive=ID:My Show Season 1/Movie (2019).mp4")
