"""Unit tests for plan.py (local-selection -> upload-job planning).

Uses real tmp files/dirs on disk (paths must exist — same contract as the app:
sources come from an osascript picker, always real paths).
"""
import os
import shutil
import tempfile

from gdrive_toolkit.uploader.plan import PlanError, build_upload_jobs, escape_literal


def _mkdir(*parts):
    p = os.path.join(*parts)
    os.makedirs(p, exist_ok=True)
    return p


def _mkfile(path, content=b"x"):
    with open(path, "wb") as fh:
        fh.write(content)
    return path


def test_special_chars_escaped():
    assert escape_literal("weird[1]*.mp4") == "weird\\[1\\]\\*.mp4"
    assert escape_literal("a{b}c?d\\e") == "a\\{b\\}c\\?d\\\\e"


def test_dir_becomes_one_job_preserving_name():
    root = tempfile.mkdtemp()
    try:
        show = _mkdir(root, "Show")
        _mkfile(os.path.join(show, "ep1.mp4"), b"12345")
        _mkfile(os.path.join(show, "ep2.mp4"), b"1234567890")

        jobs = build_upload_jobs([{"path": show, "is_dir": True}], "Incoming")
        assert len(jobs) == 1
        j = jobs[0]
        assert j.src_fs == os.path.normpath(show)
        assert j.dst_path == "Incoming/Show"
        assert j.filter_rules is None
        assert j.label == "Show"
        assert j.size == 15
    finally:
        shutil.rmtree(root)


def test_dir_job_dest_root_when_dest_path_empty():
    root = tempfile.mkdtemp()
    try:
        show = _mkdir(root, "Show")
        jobs = build_upload_jobs([{"path": show, "is_dir": True}], "")
        assert jobs[0].dst_path == "Show"
    finally:
        shutil.rmtree(root)


def test_loose_files_grouped_by_parent_into_one_filtered_job():
    root = tempfile.mkdtemp()
    try:
        _mkfile(os.path.join(root, "a.txt"), b"12")
        _mkfile(os.path.join(root, "b.txt"), b"1234")

        sources = [
            {"path": os.path.join(root, "a.txt"), "is_dir": False},
            {"path": os.path.join(root, "b.txt"), "is_dir": False},
        ]
        jobs = build_upload_jobs(sources, "Dest")
        assert len(jobs) == 1
        j = jobs[0]
        assert j.src_fs == os.path.normpath(root)
        assert j.dst_path == "Dest"
        assert j.filter_rules == ["+ /a.txt", "+ /b.txt", "- **"]
        assert j.size == 6
        assert len(j.sources) == 2
    finally:
        shutil.rmtree(root)


def test_files_from_different_parents_produce_separate_jobs():
    root = tempfile.mkdtemp()
    try:
        p1 = _mkdir(root, "p1")
        p2 = _mkdir(root, "p2")
        _mkfile(os.path.join(p1, "a.txt"))
        _mkfile(os.path.join(p2, "b.txt"))

        sources = [
            {"path": os.path.join(p1, "a.txt"), "is_dir": False},
            {"path": os.path.join(p2, "b.txt"), "is_dir": False},
        ]
        jobs = build_upload_jobs(sources, "Dest")
        assert len(jobs) == 2
        srcs = {j.src_fs for j in jobs}
        assert srcs == {os.path.normpath(p1), os.path.normpath(p2)}
    finally:
        shutil.rmtree(root)


def test_dedup_file_child_of_selected_dir():
    root = tempfile.mkdtemp()
    try:
        show = _mkdir(root, "Show")
        loose = os.path.join(show, "extra.txt")
        _mkfile(loose)

        sources = [
            {"path": show, "is_dir": True},
            {"path": loose, "is_dir": False},
        ]
        jobs = build_upload_jobs(sources, "Dest")
        # the loose file inside Show is redundant — the dir job already covers it
        assert len(jobs) == 1
        assert jobs[0].src_fs == os.path.normpath(show)
    finally:
        shutil.rmtree(root)


def test_dedup_nested_selected_dir():
    root = tempfile.mkdtemp()
    try:
        parent = _mkdir(root, "Parent")
        child = _mkdir(parent, "Child")

        sources = [
            {"path": parent, "is_dir": True},
            {"path": child, "is_dir": True},
        ]
        jobs = build_upload_jobs(sources, "Dest")
        assert len(jobs) == 1
        assert jobs[0].src_fs == os.path.normpath(parent)
    finally:
        shutil.rmtree(root)


def test_dedup_exact_duplicate_path():
    root = tempfile.mkdtemp()
    try:
        f = _mkfile(os.path.join(root, "a.txt"))
        sources = [
            {"path": f, "is_dir": False},
            {"path": f, "is_dir": False},
        ]
        jobs = build_upload_jobs(sources, "Dest")
        assert len(jobs) == 1
        assert len(jobs[0].sources) == 1
    finally:
        shutil.rmtree(root)


def test_basename_collision_between_two_dirs_raises():
    root1 = tempfile.mkdtemp()
    root2 = tempfile.mkdtemp()
    try:
        d1 = _mkdir(root1, "Show")
        d2 = _mkdir(root2, "Show")
        sources = [
            {"path": d1, "is_dir": True},
            {"path": d2, "is_dir": True},
        ]
        try:
            build_upload_jobs(sources, "Dest")
            assert False, "expected PlanError"
        except PlanError:
            pass
    finally:
        shutil.rmtree(root1)
        shutil.rmtree(root2)


def test_basename_collision_between_two_loose_files_raises():
    root1 = tempfile.mkdtemp()
    root2 = tempfile.mkdtemp()
    try:
        f1 = _mkfile(os.path.join(root1, "same.txt"))
        f2 = _mkfile(os.path.join(root2, "same.txt"))
        sources = [
            {"path": f1, "is_dir": False},
            {"path": f2, "is_dir": False},
        ]
        try:
            build_upload_jobs(sources, "Dest")
            assert False, "expected PlanError"
        except PlanError:
            pass
    finally:
        shutil.rmtree(root1)
        shutil.rmtree(root2)


def test_special_chars_in_filenames_are_escaped_in_filter_rule():
    root = tempfile.mkdtemp()
    try:
        name = "weird[1]*.mp4"
        _mkfile(os.path.join(root, name))
        sources = [{"path": os.path.join(root, name), "is_dir": False}]
        jobs = build_upload_jobs(sources, "Dest")
        assert jobs[0].filter_rules == ["+ /weird\\[1\\]\\*.mp4", "- **"]
    finally:
        shutil.rmtree(root)


def test_label_for_multiple_files_mentions_count_and_parent():
    root = tempfile.mkdtemp()
    try:
        _mkfile(os.path.join(root, "a.txt"))
        _mkfile(os.path.join(root, "b.txt"))
        _mkfile(os.path.join(root, "c.txt"))
        sources = [
            {"path": os.path.join(root, n), "is_dir": False} for n in ("a.txt", "b.txt", "c.txt")
        ]
        jobs = build_upload_jobs(sources, "Dest")
        assert "3 files" in jobs[0].label
    finally:
        shutil.rmtree(root)


def test_staged_flag_is_preserved_on_sources():
    root = tempfile.mkdtemp()
    try:
        show = _mkdir(root, "Show")
        sources = [{"path": show, "is_dir": True, "staged": True}]
        jobs = build_upload_jobs(sources, "Dest")
        assert jobs[0].sources[0]["staged"] is True
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print("all %d passed" % len(fns))
    sys.exit(0)
