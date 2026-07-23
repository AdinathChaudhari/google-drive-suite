# Media assets (planned)

This directory holds the suite's visual assets. None are recorded yet — this
is the plan, so paths referenced from READMEs resolve once they land.

All recorded in dark theme, same browser width (~1280px), bookmarks bar
hidden, **after** the `drivedeck.css` unification lands (see
[`design/drivedeck.css`](../../design/drivedeck.css)) — the assets are the
payoff of one shared visual language across all four tools.

| Asset | What it shows | Placement |
|---|---|---|
| `hero.png` | Diagram (not a GIF): a neutral cloud-drive glyph at center, four labeled flows around it in the Drivedeck palette — ↓ Download (checkbox-tree chip), ↑ Upload, ▶ Stream (poster grid + TV frame chips), ⟳ Offload (menu-bar/terminal chip). Dark bg `#0f1014`, periwinkle→violet accents. Also doubles as the GitHub social preview image (1280×640). | Root `README.md`, under the tagline |
| `download.gif` | toolkit: pick a drive → tick folders in the tri-state tree → Start → parallel per-file bars filling → overall bar goes green. | Root `README.md` (suite table) + `toolkit/README.md` |
| `stream.gif` | drivecast: scroll the poster grid → open a show → season picker → click episode → mpv opens playing → cut to the Continue Watching shelf. | `drivecast/README.md` + suite README stream section |
| `firetv.jpg` | The Fire TV home grid — a clean `adb exec-out screencap -p` screenshot framed in a simple TV bezel graphic. | `drivecast-app/README.md` + thumbnail in suite README |
| `offload.gif` | The menu bar: download finishes → "where should this go?" prompt → `⬆ 42%` live in the menu-bar title → freed-space notification. | `drive-offload/README.md` |

Recording notes: GIFs via screen-record → `gifski` (or ffmpeg palettegen),
≤8 MB, ~12s loops. `hero.png` is a diagram, not a montage GIF — a 4-tool
montage is illegible at README width; the diagram states the product thesis
in one glance.
