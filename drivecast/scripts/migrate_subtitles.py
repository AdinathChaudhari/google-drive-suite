#!/usr/bin/env python3
"""Re-validate every already-cached subtitle against the quality gate.

Run manually, never imported at startup:

    ./venv/bin/python scripts/migrate_subtitles.py            # report only
    ./venv/bin/python scripts/migrate_subtitles.py --apply     # quarantine + rewrite

The cache predates :func:`drivecast.subtitles.sanitize_subtitle_bytes`, so it
holds whatever the network handed over — in the author's own cache, two 263-byte
ISP court-order block pages and one 267-byte advertising stub, all three served
to players as subtitles, permanently, because ``cached()`` only checks that the
file exists.

Rejects are **quarantined**, not deleted: ``data/subs/`` is outside version
control and a mistuned threshold has no undo. Each reject is also recorded in
the negative cache under the ``migration`` source, so the next play retries the
real sources instead of re-serving the junk. Accepted files are rewritten in the
canonical form (UTF-8 without a BOM, CRLF) only when that changes their bytes.
"""
import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drivecast import config                      # noqa: E402
from drivecast import subtitles                   # noqa: E402

QUARANTINE_DIRNAME = ".quarantine"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually quarantine rejects and rewrite normalised files")
    ap.add_argument("--subs-dir", default=config.SUBS_DIR)
    args = ap.parse_args()

    subs_dir = args.subs_dir
    if not os.path.isdir(subs_dir):
        print("No subtitle cache at %s — nothing to do." % subs_dir)
        return 0

    quarantine = os.path.join(subs_dir, QUARANTINE_DIRNAME)
    negative = subtitles.NegativeCache()
    names = sorted(n for n in os.listdir(subs_dir)
                   if os.path.isfile(os.path.join(subs_dir, n))
                   and not n.startswith("."))

    kept = rewritten = rejected = 0
    for name in names:
        path = os.path.join(subs_dir, name)
        file_id, ext = os.path.splitext(name)
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as e:
            print("  !! unreadable  %-46s %s" % (name, e))
            continue
        clean, reason = subtitles.sanitize_subtitle_bytes(raw, ext.lower())
        if clean is None:
            rejected += 1
            print("  REJECT %-20s %-46s %d bytes" % (reason, name, len(raw)))
            if args.apply:
                os.makedirs(quarantine, exist_ok=True)
                shutil.move(path, os.path.join(quarantine, name))
                negative.record(file_id, "migration", reason)
            continue
        kept += 1
        if clean != raw:
            rewritten += 1
            if args.apply:
                subtitles._atomic_write(path, clean)

    print("\n%d files: %d kept (%d re-encoded), %d rejected%s"
          % (len(names), kept, rewritten, rejected,
             "" if args.apply else "  [dry run — pass --apply to act]"))
    if rejected and args.apply:
        print("Quarantined into %s — delete that directory once you trust the run."
              % quarantine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
