#!/usr/bin/env python3
"""Live terminal dashboard for drive-offload Shared Drive usage.

Two modes, both driven by a single `todrive du --json` call (no per-drive
rclone scans):

  content-only (default, fast):
    Redraws a sorted per-drive table of CONTENT size (trashed=false) with fill
    bars, deltas, and an account-total row.

  --trash:
    Adds TRASH and TOTAL (content+trash) columns plus a per-drive FREE column,
    sorted by trash desc so the biggest cleanup targets rise to the top. Trash
    now comes straight from the du payload's trash_bytes, so it costs nothing
    extra. Trashed files in a Shared Drive DO count against that drive's cap.

FREE is per-drive headroom against the 100 GB soft cap (from the du payload's
cap_bytes): FREE = max(0, cap - TOTAL), or "over" for a grandfathered drive
past the cap. Storage is effectively unlimited (the fleet scales by minting
new drives), so there is no global "pool free" number.

Usage:
    ./storage_monitor.py                  # fast content-only live view (60s)
    ./storage_monitor.py --trash          # content|trash|total|free table
    ./storage_monitor.py --trash -i 120   # re-run the trash table every 2 min
    ./storage_monitor.py --once           # single snapshot and exit

Ctrl-C to quit. Watching does not affect the daemon or any upload.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TODRIVE = os.path.join(SCRIPT_DIR, "todrive")

RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
GREEN = "\033[32m"; YELLOW = "\033[33m"; RED = "\033[31m"; CYAN = "\033[36m"; MAG = "\033[35m"
CLEAR = "\033[2J\033[H"; HIDE_CURSOR = "\033[?25l"; SHOW_CURSOR = "\033[?25h"


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000.0:
            return "%.1f%s" % (n, unit)
        n /= 1000.0
    return "%.1fPB" % n


def fetch_du():
    out = subprocess.run([TODRIVE, "du", "--json"], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError("todrive du failed (rc=%s): %s" % (out.returncode, out.stderr.strip()[:200]))
    return json.loads(out.stdout)


def color_for(pct, warn, crit):
    return RED if pct >= crit else (YELLOW if pct >= warn else GREEN)


def bar(pct, width=16):
    filled = min(width, int(round(pct / 100.0 * width)))
    return "█" * filled + "░" * (width - filled)


def render(data, show_trash, prev, args):
    cap = data.get("cap_bytes") or 100_000_000_000
    drives = list(data.get("drives", []))
    has_trash = show_trash

    def content_b(d):
        return int(d.get("bytes", 0))

    def trash_b(d):
        return int(d.get("trash_bytes", 0) or 0) if has_trash else 0

    if args.sort == "name":
        drives.sort(key=lambda d: d.get("name", "").lower())
    elif args.sort == "trash" and has_trash:
        drives.sort(key=lambda d: trash_b(d), reverse=True)
    else:  # content size desc
        drives.sort(key=lambda d: content_b(d), reverse=True)

    cols = shutil.get_terminal_size((110, 40)).columns
    name_w = max(20, min(40, cols - (51 if has_trash else 52)))

    def free_cell(total):
        """(rendered_string, color) for the per-drive FREE headroom column:
        "over" (red) when a grandfathered drive is past the cap, else
        human(cap - total) coloured yellow under 10 GB left, green above."""
        if total > cap:
            return "over", RED
        free = cap - total
        return human(free), (YELLOW if free < 10_000_000_000 else GREEN)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    L = []
    mode = "content + trash" if has_trash else "content"
    L.append(BOLD + CYAN + "  drive-offload — Shared Drive usage" + RESET
             + DIM + "   (%s · %s)" % (mode, now) + RESET)
    L.append("")
    if has_trash:
        L.append(BOLD + "  %-*s %11s %11s %11s %11s" % (name_w, "DRIVE", "CONTENT", "TRASH", "TOTAL", "FREE") + RESET)
    else:
        L.append(BOLD + "  %-*s %11s %7s  %-16s %10s" % (name_w, "DRIVE", "CONTENT", "%", "FILL", "Δ") + RESET)
    L.append("  " + "─" * (name_w + 46))

    tot_c = tot_t = 0
    n_trashy = 0
    for d in drives:
        name = d.get("name", "?")
        cb = content_b(d); tot_c += cb
        disp = name if len(name) <= name_w else name[:name_w - 1] + "…"
        if has_trash:
            tb = trash_b(d); tot_t += tb
            total = cb + tb
            if tb >= 1_000_000_000:  # >= 1 GB of trash worth flagging
                n_trashy += 1
            tcol = MAG if tb >= 1_000_000_000 else (DIM if tb == 0 else "")
            free_s, fc = free_cell(total)
            L.append("  %-*s %11s %s%11s%s %11s %s%11s%s"
                     % (name_w, disp, human(cb),
                        tcol, human(tb), RESET, human(total),
                        fc, free_s, RESET))
        else:
            pct = d.get("pct")
            if pct is None:
                pct = cb / cap * 100.0 if cap else 0.0
            c = color_for(pct, args.warn, args.crit)
            delta = ""
            if prev is not None:
                pb = prev.get(name)
                if pb is not None and pb != cb:
                    diff = cb - pb; sign = "+" if diff > 0 else "-"
                    delta = (YELLOW if diff > 0 else GREEN) + "%s%s" % (sign, human(abs(diff))) + RESET
            L.append("  %-*s %11s %s%6.1f%%%s  %s%-16s%s %10s"
                     % (name_w, disp, human(cb), c, pct, RESET, c, bar(pct), RESET, delta))

    L.append("  " + "─" * (name_w + 46))
    if has_trash:
        grand = tot_c + tot_t
        L.append("  %-*s %11s %s%11s%s %11s %11s"
                 % (name_w, BOLD + "TOTAL (%d)" % len(drives) + RESET,
                    human(tot_c), BOLD + MAG, human(tot_t), RESET,
                    BOLD + human(grand) + RESET, ""))
        L.append("")
        L.append(MAG + "  ♻ %s in trash across %d drive(s) ≥1GB — emptying it reclaims each drive's 100 GB cap" % (human(tot_t), n_trashy) + RESET)
    else:
        L.append("  %-*s %11s   %s" % (name_w, BOLD + "TOTAL (%d)" % len(drives) + RESET,
                                       human(tot_c), DIM + "account proxy" + RESET))
        L.append(DIM + "  content only — run with --trash to see trash + per-drive free" + RESET)
    L.append(DIM + "  Ctrl-C to quit · FREE is per-drive headroom vs the 100 GB cap" + RESET)
    return "\n".join(L)


def snapshot_map(data):
    m = {d.get("name"): int(d.get("bytes", 0)) for d in data.get("drives", [])}
    m["__total__"] = sum(int(d.get("bytes", 0)) for d in data.get("drives", []))
    return m


def main():
    ap = argparse.ArgumentParser(description="Live drive-offload storage dashboard")
    ap.add_argument("-i", "--interval", type=int, default=None, help="refresh seconds (default 60)")
    ap.add_argument("--trash", action="store_true", help="show trash + total + per-drive free (from the same du call)")
    ap.add_argument("--warn", type=float, default=80.0, help="warn %% (content mode, default 80)")
    ap.add_argument("--crit", type=float, default=100.0, help="crit %% (content mode, default 100)")
    ap.add_argument("--sort", choices=("size", "name", "trash"), default=None, help="default: size (content) / trash (--trash)")
    ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
    args = ap.parse_args()

    if args.interval is None:
        args.interval = 60
    if args.sort is None:
        args.sort = "trash" if args.trash else "size"
    if not os.path.exists(TODRIVE):
        sys.exit("todrive not found next to this script: %s" % TODRIVE)

    prev = None
    interactive = sys.stdout.isatty() and not args.once
    if interactive:
        sys.stdout.write(HIDE_CURSOR)
    try:
        while True:
            try:
                data = fetch_du()
            except Exception as e:
                msg = "  fetch error: %s" % e
                if args.once:
                    sys.exit(msg)
                sys.stdout.write((CLEAR if interactive else "") + RED + msg + RESET + "\n")
                sys.stdout.flush(); time.sleep(args.interval); continue

            body = render(data, args.trash, prev, args)
            sys.stdout.write((CLEAR if interactive else "") + body + "\n")
            sys.stdout.flush()
            prev = snapshot_map(data)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if interactive:
            sys.stdout.write(SHOW_CURSOR + "\n"); sys.stdout.flush()


if __name__ == "__main__":
    main()
