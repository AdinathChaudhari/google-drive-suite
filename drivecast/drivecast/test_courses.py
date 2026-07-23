"""Tests for the course-drive classifier (SECTIONS_DESIGN.md §6).

All synthetic — no Drive API is ever contacted. Fixtures echo the real course
drives named in the design (DataCamp, MasterClass, Cora Vane, ...).
"""
from drivecast import courses


# ------------------------------------------------------------- helpers --------

def cvid(fid, name, dur=None, size=1000, parent="c", thumb=None):
    """A walked video dict (as Scanner._walk_title builds for a course drive)."""
    return {"id": fid, "name": name, "size": size, "duration_ms": dur,
            "parent_id": parent, "ancestors": [], "thumb": thumb, "media": "video"}


def cfile(fid, name, mime="application/pdf", size=500, parent="c", thumb=None):
    """A walked non-media extra (pdf/image)."""
    return {"id": fid, "name": name, "mime": mime, "size": size,
            "parent_id": parent, "thumb": thumb}


def cnode(name, videos=(), files=(), subfolders=(), fid="c", drive="drv"):
    return {"id": fid, "name": name, "drive_id": drive, "videos": list(videos),
            "files": list(files), "subfolders": list(subfolders)}


def loosevid(fid, name, size=1000, dur=None, thumb=None):
    f = {"id": fid, "name": name, "mimeType": "video/mp4", "size": str(size)}
    if dur is not None:
        f["videoMediaMetadata"] = {"durationMillis": str(dur)}
    if thumb is not None:
        f["thumbnailLink"] = thumb
    return f


def classify(nodes, loose=(), hints=None, drive_id="drv", drive_name="A Drive"):
    return courses.classify_course_drive(drive_id, drive_name, list(nodes),
                                         list(loose), hints or {})


def only(recs):
    assert len(recs) == 1, recs
    return recs[0]


def ep_titles(season):
    return [e["title"] for e in season["episodes"]]


# ---------------------------------------------- flat NN) course (DataCamp) ----

def test_datacamp_nn_flat_lessons_under_python_subject_container():
    # drive root -> "Python" subject CONTAINER -> two courses, each a flat set
    # of "NN)" lesson files. The container name becomes the shelf; each course
    # is one season (name None) ordered by lesson number.
    intro = cnode("Introduction to Python", fid="introF", videos=[
        cvid("i2", "02) Variables.mp4", parent="introF"),
        cvid("i1", "01) Hello World.mp4", parent="introF"),
        cvid("i3", "03) Lists.mp4", parent="introF"),
    ])
    inter = cnode("Intermediate Python", fid="interF", videos=[
        cvid("m1", "01) Matplotlib.mp4", parent="interF"),
        cvid("m2", "02) Dictionaries.mp4", parent="interF"),
    ])
    python = cnode("Python", fid="pyF", subfolders=[intro, inter])
    recs = classify([python])
    by_title = {r["title"]: r for r in recs}
    assert set(by_title) == {"Introduction to Python", "Intermediate Python"}
    for r in recs:
        assert r["type"] == "show"
        assert r["shelf"] == "Python"          # nearest named container
        assert len(r["seasons"]) == 1
        assert r["seasons"][0]["name"] is None  # flat -> single unnamed season
    intro_rec = by_title["Introduction to Python"]
    assert intro_rec["id"] == "introF"         # id = course FOLDER's drive id
    assert ep_titles(intro_rec["seasons"][0]) == ["Hello World", "Variables", "Lists"]
    assert [e["episode"] for e in intro_rec["seasons"][0]["episodes"]] == [1, 2, 3]


# --------------------------------------------- flat "N - Title" (MasterClass) --

def test_masterclass_n_dash_title_flat():
    node = cnode("Cole Harwick Teaches Space Exploration", fid="mcF", videos=[
        cvid("v3", "3 - Rockets.mp4", parent="mcF"),
        cvid("v1", "1 - Introduction.mp4", parent="mcF"),
        cvid("v2", "2 - The Soyuz.mp4", parent="mcF"),
    ])
    rec = only(classify([node]))
    assert rec["type"] == "show" and rec["shelf"] is None
    assert ep_titles(rec["seasons"][0]) == ["Introduction", "The Soyuz", "Rockets"]


def test_chris_voss_numbering_gap_is_display_order_not_index():
    # Cora Vane skips lesson 13; the gap is fine — episode numbers are 1..N
    # display positions, never the parsed lesson number.
    node = cnode("Cora Vane Teaches Negotiation", fid="cvF", videos=[
        cvid("a", "12) Bargaining.mp4", parent="cvF"),
        cvid("b", "14) Black Swans.mp4", parent="cvF"),
        cvid("c", "15) Summary.mp4", parent="cvF"),
    ])
    rec = only(classify([node]))
    eps = rec["seasons"][0]["episodes"]
    assert [e["episode"] for e in eps] == [1, 2, 3]        # contiguous positions
    assert [e["file_id"] for e in eps] == ["a", "b", "c"]  # ordered by lesson num


def test_unnumbered_lessons_sort_after_numbered_alphabetically():
    node = cnode("Course", fid="uF", videos=[
        cvid("z", "Conclusion.mp4", parent="uF"),
        cvid("a", "01) Intro.mp4", parent="uF"),
        cvid("m", "Appendix.mp4", parent="uF"),
    ])
    rec = only(classify([node]))
    assert [e["file_id"] for e in rec["seasons"][0]["episodes"]] == ["a", "m", "z"]


# --------------------------------------------- numbered modules -> seasons ----

def test_numbered_module_subfolders_become_named_ordered_seasons():
    m2 = cnode("02) Advanced", fid="m2F", videos=[
        cvid("b1", "01) Deep Nets.mp4", parent="m2F")])
    m1 = cnode("01) Basics", fid="m1F", videos=[
        cvid("a1", "01) Neurons.mp4", parent="m1F"),
        cvid("a2", "02) Layers.mp4", parent="m1F")])
    extra = cnode("Bonus", fid="bF", videos=[
        cvid("x1", "Wrap up.mp4", parent="bF")])            # unnumbered -> last
    course = cnode("Deep Learning", fid="dlF", subfolders=[m2, m1, extra])
    rec = only(classify([course]))
    names = [(s["season"], s["name"]) for s in rec["seasons"]]
    assert names == [(1, "01) Basics"), (2, "02) Advanced"), (3, "Bonus")]


# ------------------------------------------------------- wrapper recursion ----

def test_part1_wrapper_titles_from_outer_folder():
    # "Python Data Science Toolbox / (Part 1) / lessons": the wrapper's single
    # non-empty child is the course, but the meaningful title is the OUTER name.
    part1 = cnode("(Part 1)", fid="p1F", videos=[
        cvid("l1", "01) Functions.mp4", parent="p1F"),
        cvid("l2", "02) Lambdas.mp4", parent="p1F")])
    wrapper = cnode("Python Data Science Toolbox", fid="wF", subfolders=[part1])
    rec = only(classify([wrapper]))
    assert rec["title"] == "Python Data Science Toolbox"
    assert rec["id"] == "p1F"                  # id = the real course folder
    assert len(rec["seasons"][0]["episodes"]) == 2


def test_video_lectures_wrapper():
    lectures = cnode("Video Lectures", fid="vlF", videos=[
        cvid("v1", "01) Plot.mp4", parent="vlF"),
        cvid("v2", "02) Character.mp4", parent="vlF")])
    author = cnode("Jane Prescott", fid="jpF", subfolders=[lectures])
    rec = only(classify([author]))
    assert rec["title"] == "Jane Prescott"


def test_ethical_hacking_double_wrap():
    # Two nested wrappers; site-prefix stripped from the outer title.
    inner = cnode("Complete_Python_3_Bootcamp", fid="cpF", videos=[
        cvid("v1", "01) Setup.mp4", parent="cpF"),
        cvid("v2", "02) Strings.mp4", parent="cpF")])
    mid = cnode("[FreeCoursesOnline.Me] Ethical Hacking", fid="ehF",
                subfolders=[inner])
    rec = only(classify([mid]))
    assert rec["title"] == "Ethical Hacking"   # outermost, site prefix stripped
    assert rec["id"] == "cpF"


def test_wrapper_chain_beyond_depth_cap_yields_nothing():
    # A pathological nest deeper than MAX_DEPTH must not crash and must not
    # invent a record.
    node = cnode("course", fid="leaf", videos=[
        cvid("a", "01) A.mp4", parent="leaf"),
        cvid("b", "02) B.mp4", parent="leaf")])
    for i in range(courses.MAX_DEPTH + 3):
        node = cnode("wrap%d" % i, fid="w%d" % i, subfolders=[node])
    assert classify([node]) == []


# ------------------------------------------------------- container -> shelf ----

def test_sessions_container_sets_shelf():
    c1 = cnode("Negotiation", fid="c1F", videos=[
        cvid("a1", "01) A.mp4", parent="c1F"), cvid("a2", "02) B.mp4", parent="c1F")])
    c2 = cnode("Storytelling", fid="c2F", videos=[
        cvid("b1", "01) A.mp4", parent="c2F"), cvid("b2", "02) B.mp4", parent="c2F")])
    sessions = cnode("Sessions", fid="sesF", subfolders=[c1, c2])
    recs = classify([sessions])
    assert {r["title"] for r in recs} == {"Negotiation", "Storytelling"}
    assert all(r["shelf"] == "Sessions" for r in recs)


def test_nested_containers_use_nearest_named_container_as_shelf():
    course = cnode("Rust", fid="rsF", videos=[
        cvid("a", "01) A.mp4", parent="rsF"), cvid("b", "02) B.mp4", parent="rsF")])
    coding = cnode("Coding", fid="cdF", subfolders=[course])
    top = cnode("Courses", fid="csF", subfolders=[coding])
    # "Courses/Coding" both have a single child, so each reads as a wrapper —
    # to test the container/shelf path give Coding two courses.
    course2 = cnode("Go", fid="goF", videos=[
        cvid("c", "01) A.mp4", parent="goF"), cvid("d", "02) B.mp4", parent="goF")])
    coding = cnode("Coding", fid="cdF", subfolders=[course, course2])
    top = cnode("Courses", fid="csF", subfolders=[coding])
    recs = classify([top])
    assert {r["title"] for r in recs} == {"Rust", "Go"}
    assert all(r["shelf"] == "Coding" for r in recs)   # nearest, not "Courses"


# ------------------------------------------------- materials & resources ------

def test_class_workbook_pdf_is_material_not_lesson_zero():
    node = cnode("Course", fid="wkF", videos=[
        cvid("v1", "01) Intro.mp4", parent="wkF"),
        cvid("v2", "02) Deep.mp4", parent="wkF")],
        files=[cfile("pdf0", "00.Class Workbook.pdf", parent="wkF")])
    rec = only(classify([node]))
    assert [e["episode"] for e in rec["seasons"][0]["episodes"]] == [1, 2]
    assert "resources" not in rec["seasons"][0]["episodes"][0]
    assert rec["materials"] == [{"file_id": "pdf0", "name": "00.Class Workbook.pdf",
                                 "size": 500, "mime": "application/pdf"}]


def test_timestamp_prefixed_pdf_is_material_not_a_lesson():
    # "1576544307-..." parses as a number > 999 -> lesson_number None -> it can
    # never become a lesson; it lands in materials.
    node = cnode("Course", fid="tsF", videos=[
        cvid("v1", "01) Intro.mp4", parent="tsF"),
        cvid("v2", "02) Body.mp4", parent="tsF")],
        files=[cfile("cv", "1576544307-cv_complete.pdf", parent="tsF")])
    rec = only(classify([node]))
    assert [e["file_id"] for e in rec["seasons"][0]["episodes"]] == ["v1", "v2"]
    assert [m["name"] for m in rec["materials"]] == ["1576544307-cv_complete.pdf"]


def test_same_lesson_number_pdfs_attach_as_episode_resources():
    # "3 a - ..." / "3 b - ..." pdfs sit next to the lesson-3 video -> resources.
    node = cnode("Course", fid="rF", videos=[
        cvid("v3", "3 - Tactical Empathy.mp4", parent="rF"),
        cvid("v1", "1 - Intro.mp4", parent="rF"),
        cvid("v2", "2 - Mirroring.mp4", parent="rF")],
        files=[cfile("pa", "3 a - Cheatsheet.pdf", parent="rF"),
               cfile("pb", "3 b - Worksheet.pdf", parent="rF")])
    rec = only(classify([node]))
    eps = {e["file_id"]: e for e in rec["seasons"][0]["episodes"]}
    res = eps["v3"]["resources"]
    assert [r["file_id"] for r in res] == ["pa", "pb"]
    assert all(set(r) == {"file_id", "name", "mime"} for r in res)
    assert "resources" not in eps["v1"] and "resources" not in eps["v2"]
    assert "materials" not in rec                 # both pdfs consumed as resources


# ------------------------------------------------------- single course --------

def test_single_course_hint_makes_drive_one_course_with_module_seasons():
    m1 = cnode("Module One", fid="m1F", videos=[
        cvid("a1", "01) A.mp4", parent="m1F")])
    m2 = cnode("Module Two", fid="m2F", videos=[
        cvid("b1", "01) B.mp4", parent="m2F")])
    rec = only(classify([m1, m2], hints={"single_course": True},
                        drive_id="AILearning", drive_name="AI Learning Hub"))
    assert rec["id"] == "AILearning"           # id = drive id (drive-as-course)
    assert rec["title"] == "AI Learning Hub"
    assert [(s["season"], s["name"]) for s in rec["seasons"]] == \
        [(1, "Module One"), (2, "Module Two")]


def test_single_course_heuristic_when_root_folders_numbered():
    # Majority of root folders lead with a module number -> single course, even
    # with no hint. Modules ordered by their number.
    m5 = cnode("05) C-Suite Communication", fid="m5F", videos=[
        cvid("a", "01) A.mp4", parent="m5F")])
    m1 = cnode("01) Foundations", fid="m1F", videos=[
        cvid("b", "01) B.mp4", parent="m1F")])
    rec = only(classify([m5, m1], drive_name="Executive Program"))
    assert rec["title"] == "Executive Program"
    assert [s["name"] for s in rec["seasons"]] == ["01) Foundations",
                                                   "05) C-Suite Communication"]


def test_single_course_step_subfolders_flatten_in_step_order():
    # A module's "Step N)" subfolders flatten into its lesson list in Step order,
    # with an unnumbered subfolder last.
    step2 = cnode("Step 2) Build", fid="s2F", videos=[
        cvid("b", "01) Build A.mp4", parent="s2F")])
    step1 = cnode("Step 1) Plan", fid="s1F", videos=[
        cvid("a", "01) Plan A.mp4", parent="s1F")])
    misc = cnode("Visualizations & Objectives", fid="voF", videos=[
        cvid("z", "Overview.mp4", parent="voF")])
    module = cnode("05) C-Suite", fid="mF", subfolders=[step2, step1, misc])
    rec = only(classify([module], hints={"single_course": True},
                        drive_name="Exec"))
    season = only(rec["seasons"])
    assert season["name"] == "05) C-Suite"
    assert [e["file_id"] for e in season["episodes"]] == ["a", "b", "z"]


# ------------------------------------------------------------- cover image ----

def test_only_image_in_course_folder_becomes_thumb():
    node = cnode("Course", fid="ciF", videos=[
        cvid("v1", "01) A.mp4", parent="ciF"),
        cvid("v2", "02) B.mp4", parent="ciF")],
        files=[cfile("img", "Gemini_Generated_abc.png", mime="image/png",
                     parent="ciF", thumb="https://lh3/thumb=s220")])
    rec = only(classify([node]))
    assert rec["_thumb"] == "https://lh3/thumb=s220"
    assert "materials" not in rec              # image is a cover, not a material


def test_named_cover_wins_over_other_images():
    node = cnode("Course", fid="cvF", videos=[
        cvid("v1", "01) A.mp4", parent="cvF"),
        cvid("v2", "02) B.mp4", parent="cvF")],
        files=[cfile("i1", "screenshot.jpg", mime="image/jpeg", parent="cvF",
                     thumb="https://lh3/shot"),
               cfile("i2", "Cover.jpg", mime="image/jpeg", parent="cvF",
                     thumb="https://lh3/cover")])
    rec = only(classify([node]))
    assert rec["_thumb"] == "https://lh3/cover"


def test_multiple_images_no_cover_leaves_thumb_none():
    node = cnode("Course", fid="miF", videos=[
        cvid("v1", "01) A.mp4", parent="miF"),
        cvid("v2", "02) B.mp4", parent="miF")],
        files=[cfile("i1", "a.jpg", mime="image/jpeg", parent="miF"),
               cfile("i2", "b.jpg", mime="image/jpeg", parent="miF")])
    rec = only(classify([node]))
    assert rec["_thumb"] is None


# ---------------------------------------------------------------- edge cases --

def test_zero_media_course_yields_no_record():
    # A folder with only pdfs / images (no playable video) is not a course.
    node = cnode("Just Notes", fid="jnF",
                 files=[cfile("p", "notes.pdf", parent="jnF"),
                        cfile("i", "cover.jpg", mime="image/jpeg", parent="jnF")])
    assert classify([node]) == []


def test_single_video_folder_is_not_a_course():
    node = cnode("Lonely", fid="loF", videos=[cvid("v", "01) Only.mp4", parent="loF")])
    assert classify([node]) == []


def test_loose_root_videos_fold_into_a_drive_named_course():
    recs = classify([], loose=[loosevid("g1", "01) A.mp4", dur=1000),
                               loosevid("g2", "02) B.mp4")],
                    drive_name="Odds and Ends")
    rec = only(recs)
    assert rec["id"] == "drv" and rec["title"] == "Odds and Ends"
    assert rec["seasons"][0]["name"] is None
    assert [e["file_id"] for e in rec["seasons"][0]["episodes"]] == ["g1", "g2"]
    assert rec["seasons"][0]["episodes"][0]["duration_ms"] == 1000


def test_record_shape_matches_library_show_records():
    node = cnode("Course", fid="shF", videos=[
        cvid("v1", "01) A.mp4", parent="shF"), cvid("v2", "02) B.mp4", parent="shF")])
    rec = only(classify([node]))
    for k in ("id", "type", "title", "year", "drive_id", "folder_id", "poster",
              "tmdb_id", "overview", "quality", "shelf", "media", "_thumb",
              "seasons"):
        assert k in rec, k
    assert rec["type"] == "show" and rec["media"] == "video"
    assert rec["year"] is None and rec["quality"] is None
    ep = rec["seasons"][0]["episodes"][0]
    assert set(ep) == {"title", "episode", "file_id", "name", "duration_ms",
                       "size", "parent_id"}
