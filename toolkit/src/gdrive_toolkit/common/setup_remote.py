"""`gdrive-setup` — first-run wizard that picks an rclone remote and writes it
(plus the resolved repo root) to the shared config.

- 0 remotes configured -> exit, telling the user to run `rclone config` first.
- Exactly 1 remote -> auto-selected, no prompt (printed so it's still visible;
  this also makes the whole thing scriptable/non-interactive-testable).
- >1 remotes -> numbered prompt.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .config import load_shared_config, save_shared_config
from .rclone_rc import find_rclone


def _list_remotes() -> list[str]:
    rclone_bin = find_rclone()
    if rclone_bin is None:
        raise SystemExit("rclone not found — install it first (https://rclone.org/install/).")
    proc = subprocess.run([rclone_bin, "listremotes"], capture_output=True, text=True, timeout=15)
    if proc.returncode != 0:
        raise SystemExit("`rclone listremotes` failed: %s" % (proc.stderr or "").strip())
    return [line.strip().rstrip(":") for line in proc.stdout.splitlines() if line.strip()]


def _choose(remotes: list[str]) -> str:
    if len(remotes) == 1:
        print("Using the only configured rclone remote: %s" % remotes[0])
        return remotes[0]

    print("Multiple rclone remotes are configured:")
    for i, r in enumerate(remotes, start=1):
        print("  %d) %s" % (i, r))
    while True:
        choice = input("Pick a remote [1-%d]: " % len(remotes)).strip()
        if choice.isdigit() and 1 <= int(choice) <= len(remotes):
            return remotes[int(choice) - 1]
        print("Invalid choice, try again.")


def main() -> None:
    remotes = _list_remotes()
    if not remotes:
        raise SystemExit(
            "No rclone remotes configured. Run `rclone config` to set one up first, "
            "then re-run `gdrive-setup`."
        )

    remote = _choose(remotes)
    repo_root_path = Path.cwd().resolve()
    repo_root = str(repo_root_path)

    shared = load_shared_config()
    shared["remote"] = remote
    shared["repo_root"] = repo_root

    # If this looks like a `google-drive-suite` checkout (repo_root is a
    # `toolkit/` dir with `drivecast/app.py` next door), also record
    # suite_root so hub/registry.py can pick up the sibling suite members
    # (drivecast, drive-offload, drivecast-app) as built-ins without needing
    # a hub_tools.json entry for any of them.
    suite_candidate = repo_root_path.parent
    if repo_root_path.name == "toolkit" and (suite_candidate / "drivecast" / "app.py").exists():
        shared["suite_root"] = str(suite_candidate)
        print("Detected suite layout — saved suite_root %r to the shared config." % str(suite_candidate))

    save_shared_config(shared)

    print("Saved remote %r and repo_root %r to the shared config." % (remote, repo_root))


if __name__ == "__main__":
    main()
    sys.exit(0)
