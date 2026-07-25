#!/usr/bin/env python3
"""Benchmark rclone transfer concurrency against a Google Shared Drive.

Answers the question the toolkit's defaults were never measured against:
does parallelism actually make Drive uploads/downloads faster, or (as with
yt-show's aria2c-vs-native finding) does a single stream win?

Four sweeps, each varying ONE knob with everything else pinned:

  up-big     one large file, transfers=1, sweep --drive-chunk-size
  up-many    many small files, chunk pinned, sweep --transfers
  down-big   one large file, transfers=1, sweep --multi-thread-streams
  down-many  many small files, streams=1, sweep --transfers

Fairness: each run gets a fresh destination (empty remote subdir / empty local
dir) so no run benefits from a skipped transfer. Each phase re-runs its FIRST
config last as a drift control -- if the two timings disagree by more than
--drift-tol, the network moved under the sweep and the numbers are suspect.

Usage:
    scripts/bench-transfers.py --drive-name <empty-scratch-drive> --yes
    scripts/bench-transfers.py --drive-name <scratch> --big-size 1G --repeat 2 --yes
    scripts/bench-transfers.py --drive-name <scratch> --phase up-big --yes

The remote defaults to whatever `gdrive-setup` wrote to the toolkit's shared
config; pass --remote to override or if the toolkit isn't installed.

Nothing is written outside the scratch remote path and a local temp dir, and
both are purged on exit (including --drive-use-trash=false on the remote, so
the benchmark leaves no trash to count against the 100 GB drive cap).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

RCLONE = shutil.which("rclone") or "rclone"

# Flags held constant across EVERY run so the swept knob is the only variable.
COMMON = ["--retries", "3", "--low-level-retries", "10", "--checkers", "8",
          "--stats", "0", "--use-json-log", "--log-level", "NOTICE"]

# ---------------------------------------------------------------------------
# size parsing / formatting
# ---------------------------------------------------------------------------

_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3}


def parse_size(s: str) -> int:
    s = s.strip().upper().rstrip("IB") or "0"
    if s and s[-1] in _UNITS:
        return int(float(s[:-1]) * _UNITS[s[-1]])
    return int(float(s))


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return "%.1f %s" % (n, unit)
        n /= 1024


def mibs(nbytes: int, secs: float) -> float:
    return (nbytes / 1024**2) / secs if secs > 0 else 0.0


# ---------------------------------------------------------------------------
# rclone plumbing
# ---------------------------------------------------------------------------

def run_rclone(args: list[str], verbose: bool) -> tuple[int, str]:
    cmd = [RCLONE] + args
    if verbose:
        sys.stderr.write("    $ %s\n" % " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stderr or "") + (p.stdout or "")


def default_remote() -> str:
    """The remote `gdrive-setup` configured, if the toolkit is set up here.

    Read directly rather than importing gdrive_toolkit: this script is meant
    to run from a bare checkout with nothing pip-installed.
    """
    path = (os.path.expanduser("~/Library/Application Support/"
                               "gdrive_toolkit/config.json"))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("remote", "") or ""
    except (OSError, ValueError):
        return ""


def list_drives(remote: str) -> list[dict]:
    rc, out = run_rclone(["backend", "drives", "%s:" % remote], False)
    if rc != 0:
        sys.exit("rclone backend drives failed:\n%s" % out[:800])
    raw = json.loads(out)
    if isinstance(raw, dict):
        raw = raw.get("drives", [])
    return [{"id": d["id"], "name": d.get("name", d["id"])} for d in raw]


def resolve_drive(remote: str, name: str | None, drive_id: str | None) -> tuple[str, str]:
    if drive_id:
        return drive_id, drive_id
    drives = list_drives(remote)
    matches = [d for d in drives if d["name"] == name]
    if not matches:
        near = ", ".join(sorted(d["name"] for d in drives)[:12])
        sys.exit("No shared drive named %r. Some that exist: %s ..." % (name, near))
    return matches[0]["id"], matches[0]["name"]


def fs(remote: str, drive_id: str, extra: dict | None = None) -> str:
    parts = ["team_drive=%s" % drive_id]
    for k, v in (extra or {}).items():
        parts.append("%s=%s" % (k, v))
    return "%s,%s:" % (remote, ",".join(parts))


def purge(remote: str, drive_id: str, path: str, verbose: bool) -> None:
    # --drive-use-trash=false: a benchmark must not park GBs in the drive's
    # trash, which still counts against the 100 GB shared-drive cap.
    run_rclone(["purge", fs(remote, drive_id) + path,
                "--drive-use-trash=false"] + COMMON, verbose)


# ---------------------------------------------------------------------------
# payloads
# ---------------------------------------------------------------------------

def make_payloads(root: str, big_size: int, small_count: int, small_size: int) -> tuple[str, str]:
    """Write an incompressible big-file payload and a many-small-files payload.

    Random bytes, not zeros: a sparse or trivially-compressible file would let
    the filesystem or any transport-level compression flatter the result.
    """
    big_dir = os.path.join(root, "payload-big")
    many_dir = os.path.join(root, "payload-many")
    os.makedirs(big_dir, exist_ok=True)
    os.makedirs(many_dir, exist_ok=True)

    chunk = 8 * 1024**2
    big_path = os.path.join(big_dir, "big.bin")
    sys.stderr.write("  generating %s big file ...\n" % human(big_size))
    with open(big_path, "wb") as fh:
        written = 0
        while written < big_size:
            n = min(chunk, big_size - written)
            fh.write(os.urandom(n))
            written += n

    sys.stderr.write("  generating %d x %s small files ...\n"
                     % (small_count, human(small_size)))
    blob = os.urandom(small_size)
    for i in range(small_count):
        # Vary the first bytes so the files are not byte-identical (Drive
        # dedupes nothing, but identical content invites future confusion).
        with open(os.path.join(many_dir, "f%04d.bin" % i), "wb") as fh:
            fh.write(i.to_bytes(8, "big") + blob[8:])
    return big_dir, many_dir


# ---------------------------------------------------------------------------
# one timed run
# ---------------------------------------------------------------------------

def timed_copy(src: str, dst: str, extra_flags: list[str], nbytes: int,
               verbose: bool) -> dict:
    t0 = time.monotonic()
    rc, out = run_rclone(["copy", src, dst] + extra_flags + COMMON, verbose)
    elapsed = time.monotonic() - t0
    return {"ok": rc == 0, "secs": round(elapsed, 2),
            "mibs": round(mibs(nbytes, elapsed), 2),
            "error": "" if rc == 0 else out.strip()[-500:]}


def sweep(label: str, knob: str, values: list[str], build,
          nbytes: int, drift_tol: float, verbose: bool) -> list[dict]:
    """Run one config per value, then re-run values[0] as a drift control."""
    rows = []
    order = list(values) + [values[0]]
    for i, val in enumerate(order):
        control = i == len(order) - 1
        tag = "%s=%s%s" % (knob, val, "  (drift control)" if control else "")
        sys.stderr.write("  [%s] %s ... " % (label, tag))
        src, dst, flags, cleanup = build(val, i)
        res = timed_copy(src, dst, flags, nbytes, verbose)
        cleanup()
        if not res["ok"]:
            sys.stderr.write("FAILED\n    %s\n" % res["error"][:300])
        else:
            sys.stderr.write("%6.1fs  %7.2f MiB/s\n" % (res["secs"], res["mibs"]))
        res.update({"sweep": label, "knob": knob, "value": val,
                    "control": control})
        rows.append(res)

    first = next((r for r in rows if not r["control"] and r["ok"]), None)
    ctrl = rows[-1] if rows[-1]["ok"] else None
    if first and ctrl and first["mibs"] > 0:
        drift = abs(ctrl["mibs"] - first["mibs"]) / first["mibs"]
        if drift > drift_tol:
            sys.stderr.write("  ! [%s] network drifted %.0f%% between the first "
                             "run and its repeat -- treat this sweep as noise "
                             "and re-run with --repeat 3\n" % (label, drift * 100))
        for r in rows:
            r["drift"] = round(drift, 3)
    return rows


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Benchmark rclone concurrency knobs against a Shared Drive.")
    ap.add_argument("--remote", default=default_remote(),
                    help="rclone remote name (default: the toolkit's "
                         "configured remote, if there is one)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--drive-name", help="shared drive to use as scratch "
                                        "(must be EMPTY -- it gets purged)")
    g.add_argument("--drive-id", help="shared drive id instead of a name")
    ap.add_argument("--dest-path", default="rclone-bench",
                    help="scratch folder inside the drive (purged on exit)")
    ap.add_argument("--big-size", default="512M", help="big-file payload size")
    ap.add_argument("--small-count", type=int, default=60)
    ap.add_argument("--small-size", default="8M")
    ap.add_argument("--chunks", default="8M,64M,128M,256M",
                    help="--drive-chunk-size values for up-big")
    ap.add_argument("--transfers", default="1,4,8,16",
                    help="--transfers values for up-many / down-many")
    ap.add_argument("--streams", default="1,2,4,8",
                    help="--multi-thread-streams values for down-big")
    ap.add_argument("--pin-chunk", default="64M",
                    help="chunk size pinned during the up-many sweep")
    ap.add_argument("--phase", action="append",
                    choices=["up-big", "up-many", "down-big", "down-many"],
                    help="run only these sweeps (repeatable; default all)")
    ap.add_argument("--repeat", type=int, default=1,
                    help="run the whole matrix N times; report the median")
    ap.add_argument("--drift-tol", type=float, default=0.20,
                    help="flag a sweep if its control run differs by more "
                         "than this fraction (default 0.20 = 20%%)")
    ap.add_argument("--out", default="", help="write raw results as JSON here")
    ap.add_argument("--keep-local", action="store_true",
                    help="keep the generated payloads (they are large)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="echo every rclone command")
    ap.add_argument("--yes", action="store_true",
                    help="skip the transfer-volume confirmation")
    args = ap.parse_args(argv)

    if shutil.which("rclone") is None:
        sys.stderr.write("rclone not on PATH (brew install rclone)\n")
        return 2
    if not args.remote:
        sys.stderr.write("No rclone remote. Pass --remote <name>, or run "
                         "`gdrive-setup` to configure one.\n")
        return 2

    phases = args.phase or ["up-big", "up-many", "down-big", "down-many"]
    chunks = [c.strip() for c in args.chunks.split(",") if c.strip()]
    transfers = [t.strip() for t in args.transfers.split(",") if t.strip()]
    streams = [s.strip() for s in args.streams.split(",") if s.strip()]

    big_size = parse_size(args.big_size)
    small_size = parse_size(args.small_size)
    many_size = small_size * args.small_count

    # Volume estimate: each sweep is len(values)+1 runs (the +1 is the control),
    # plus one untimed seeding upload per download sweep.
    est = 0
    if "up-big" in phases:
        est += (len(chunks) + 1) * big_size
    if "up-many" in phases:
        est += (len(transfers) + 1) * many_size
    if "down-big" in phases:
        est += big_size + (len(streams) + 1) * big_size
    if "down-many" in phases:
        est += many_size + (len(transfers) + 1) * many_size
    est *= args.repeat

    drive_id, drive_label = resolve_drive(args.remote, args.drive_name, args.drive_id)

    sys.stderr.write(
        "\nrclone transfer benchmark\n"
        "  remote/drive : %s -> %s (%s)\n"
        "  scratch path : %s\n"
        "  payloads     : big %s | many %d x %s = %s\n"
        "  sweeps       : %s   (repeat %d)\n"
        "  est. traffic : ~%s\n\n"
        % (args.remote, drive_label, drive_id, args.dest_path,
           human(big_size), args.small_count, human(small_size), human(many_size),
           ", ".join(phases), args.repeat, human(est)))

    if not args.yes:
        try:
            if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        except EOFError:
            sys.stderr.write("Non-interactive; pass --yes to run.\n")
            return 1

    tmp_root = tempfile.mkdtemp(prefix="rclone-bench-")
    all_rows: list[dict] = []
    try:
        big_dir, many_dir = make_payloads(tmp_root, big_size,
                                          args.small_count, small_size)
        base = fs(args.remote, drive_id)
        seeded: dict[str, bool] = {}

        def seed(kind: str, local_dir: str) -> str:
            """Upload a payload once (untimed) so download sweeps have a source."""
            path = "%s/src-%s" % (args.dest_path, kind)
            if not seeded.get(kind):
                sys.stderr.write("  seeding remote source for download sweeps (%s) ...\n" % kind)
                purge(args.remote, drive_id, path, args.verbose)
                rc, out = run_rclone(
                    ["copy", local_dir, base + path, "--transfers", "8",
                     "--drive-chunk-size", args.pin_chunk] + COMMON, args.verbose)
                if rc != 0:
                    sys.exit("seeding upload failed:\n%s" % out[-800:])
                seeded[kind] = True
            return base + path

        for rep in range(args.repeat):
            if args.repeat > 1:
                sys.stderr.write("\n--- pass %d/%d ---\n" % (rep + 1, args.repeat))

            if "up-big" in phases:
                def build_up_big(val, i, _rep=rep):
                    dest = "%s/up-big-%d-%d" % (args.dest_path, _rep, i)
                    return (big_dir, base + dest,
                            ["--transfers", "1", "--drive-chunk-size", val],
                            lambda: purge(args.remote, drive_id, dest, args.verbose))
                all_rows += sweep("up-big", "chunk", chunks, build_up_big,
                                  big_size, args.drift_tol, args.verbose)

            if "up-many" in phases:
                def build_up_many(val, i, _rep=rep):
                    dest = "%s/up-many-%d-%d" % (args.dest_path, _rep, i)
                    return (many_dir, base + dest,
                            ["--transfers", val,
                             "--drive-chunk-size", args.pin_chunk],
                            lambda: purge(args.remote, drive_id, dest, args.verbose))
                all_rows += sweep("up-many", "transfers", transfers, build_up_many,
                                  many_size, args.drift_tol, args.verbose)

            if "down-big" in phases:
                src = seed("big", big_dir)

                def build_down_big(val, i, _rep=rep, _src=src):
                    dest = os.path.join(tmp_root, "dl-big-%d-%d" % (_rep, i))
                    # Cutoff forced well under the payload so every value of
                    # --multi-thread-streams actually takes effect.
                    return (_src, dest,
                            ["--transfers", "1", "--multi-thread-streams", val,
                             "--multi-thread-cutoff", "16M"],
                            lambda: shutil.rmtree(dest, ignore_errors=True))
                all_rows += sweep("down-big", "streams", streams, build_down_big,
                                  big_size, args.drift_tol, args.verbose)

            if "down-many" in phases:
                src = seed("many", many_dir)

                def build_down_many(val, i, _rep=rep, _src=src):
                    dest = os.path.join(tmp_root, "dl-many-%d-%d" % (_rep, i))
                    return (_src, dest,
                            ["--transfers", val, "--multi-thread-streams", "1",
                             "--fast-list"],
                            lambda: shutil.rmtree(dest, ignore_errors=True))
                all_rows += sweep("down-many", "transfers", transfers,
                                  build_down_many, many_size, args.drift_tol,
                                  args.verbose)
    finally:
        sys.stderr.write("\ncleaning up ...\n")
        purge(args.remote, drive_id, args.dest_path, args.verbose)
        if not args.keep_local:
            shutil.rmtree(tmp_root, ignore_errors=True)
        else:
            sys.stderr.write("  payloads kept at %s\n" % tmp_root)

    report(all_rows)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(all_rows, fh, indent=2)
        sys.stderr.write("\nraw results -> %s\n" % args.out)
    return 0


def report(rows: list[dict]) -> None:
    """Median MiB/s per (sweep, value), controls excluded, winner marked."""
    if not rows:
        print("no results")
        return
    by: dict[tuple[str, str, str], list[float]] = {}
    for r in rows:
        if r["control"] or not r["ok"]:
            continue
        by.setdefault((r["sweep"], r["knob"], r["value"]), []).append(r["mibs"])

    print()
    for sweep_name in ("up-big", "up-many", "down-big", "down-many"):
        keys = [k for k in by if k[0] == sweep_name]
        if not keys:
            continue
        knob = keys[0][1]
        speeds = {k[2]: statistics.median(by[k]) for k in keys}
        best = max(speeds, key=speeds.get)
        baseline = speeds.get(list(speeds)[0], 0)
        print("## %s  (sweeping --%s)" % (sweep_name, knob))
        print("| %-10s | %10s | %8s |" % (knob, "MiB/s", "vs first"))
        print("|%s|%s|%s|" % ("-" * 12, "-" * 12, "-" * 10))
        for val in speeds:
            rel = (speeds[val] / baseline) if baseline else 0
            mark = "  <-- fastest" if val == best else ""
            print("| %-10s | %10.2f | %7.2fx |%s" % (val, speeds[val], rel, mark))
        spread = (max(speeds.values()) / min(speeds.values())) if min(speeds.values()) else 0
        verdict = ("no meaningful difference -- pick the simplest value"
                   if spread < 1.15 else "%s is %.2fx the slowest" % (best, spread))
        print("-> %s\n" % verdict)


if __name__ == "__main__":
    sys.exit(main())
