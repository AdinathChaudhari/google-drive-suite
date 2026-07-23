"""Unit tests for the pure selection -> rclone filter-rule logic (override model)."""
from gdrive_toolkit.downloader.selection import build_drive_rules, build_jobs, escape_literal


def on(path, is_dir=True):
    return {"path": path, "is_dir": is_dir, "state": "on"}


def off(path, is_dir=True):
    return {"path": path, "is_dir": is_dir, "state": "off"}


def test_single_folder():
    assert build_drive_rules([on("My Folder")]) == ["+ /My Folder/**", "- **"]


def test_single_file():
    assert build_drive_rules([on("Timers/bell.mp3", is_dir=False)]) == \
        ["+ /Timers/bell.mp3", "- **"]


def test_whole_drive_root():
    assert build_drive_rules([on("")]) == ["+ /**", "- **"]


def test_uncheck_file_inside_checked_folder():
    # check folder A, then uncheck A/skip.txt -> exclude that file, keep the rest
    rules = build_drive_rules([on("A"), off("A/skip.txt", is_dir=False)])
    assert rules == ["- /A/skip.txt", "+ /A/**", "- **"]  # deeper rule first


def test_uncheck_subfolder_inside_checked_folder():
    rules = build_drive_rules([on("A"), off("A/sub")])
    assert rules == ["- /A/sub/**", "+ /A/**", "- **"]


def test_check_file_inside_unchecked_folder():
    # whole drive on, folder A off, but keep one file in A
    rules = build_drive_rules([on(""), off("A"), on("A/keep.mp3", is_dir=False)])
    assert rules == ["+ /A/keep.mp3", "- /A/**", "+ /**", "- **"]


def test_deepest_first_ordering():
    rules = build_drive_rules([on("A"), off("A/b"), on("A/b/c.txt", is_dir=False)])
    assert rules == ["+ /A/b/c.txt", "- /A/b/**", "+ /A/**", "- **"]


def test_special_chars_escaped():
    assert escape_literal("weird[1]*.mp4") == "weird\\[1\\]\\*.mp4"
    assert build_drive_rules([on("Season [1]")]) == ["+ /Season \\[1\\]/**", "- **"]


def test_build_jobs_groups_by_drive():
    jobs = build_jobs([
        {"drive_id": "D1", **on("X")},
        {"drive_id": "D2", **on("Y/z.mp3", is_dir=False)},
    ])
    assert set(jobs) == {"D1", "D2"}
    assert jobs["D1"] == ["+ /X/**", "- **"]
    assert jobs["D2"] == ["+ /Y/z.mp3", "- **"]


def test_build_jobs_skips_drive_with_only_off():
    jobs = build_jobs([{"drive_id": "D1", **off("X")}])
    assert jobs == {}


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print("all %d passed" % len(fns))
    sys.exit(0)
