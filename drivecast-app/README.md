# drivecast for Fire TV

A native Android TV / Amazon Fire TV client for a
[drivecast](https://github.com/AdinathChaudhari/drivecast) media server. It's a pure HTTP client: it browses your server's library, plays video over the
server's range-aware stream endpoint, tracks watch progress, resumes where you left off,
and plays a show as a continuous playlist so the next episode is one button away — all
from your couch with the remote.

Built with Kotlin, Jetpack Compose for TV (`androidx.tv`), and Media3 / ExoPlayer.

## What it does

- **Setup / pairing** — scans your local network for the server, or lets you type its IP,
  then validates the access token.
- **Home** — a tabbed, tiled layout that mirrors the web UI: section tabs (Entertainment,
  Courses, …) across the top — horizontally scrollable, so a long list of sections never
  crowds out the grid below — a section-scoped "Continue Watching" row, a category filter
  (All / Movies / TV Shows / Documentaries / …) on Entertainment, and a poster-tile grid —
  no more one long scroll.
- **Detail** — movie play / start-over (plus an **Extras** section listing any featurettes /
  bonus clips, each played as a single item); for shows, a **scrollable season rail** (so a
  show with many seasons never hides its episodes) over an episode list with watched and
  in-progress markers, plus a **Shuffle** button that plays the whole show in random order.
- **Player** — hands playback to **VLC** by default when it's installed (its software
  decoders cover formats this Fire TV's hardware decoder rejects). For a show it hands VLC a
  **playlist** of the remaining episodes, so VLC's own **Next / Previous** buttons walk
  through the season — the next episode is already primed. It resumes from your last position,
  sideloads subtitles when the server has them, reports progress back to the server, and still
  offers a cancelable "Up next" autoplay. Falls back to the built-in ExoPlayer when VLC isn't
  installed.
- **Feel** — a 10-foot UI built to feel first-party: one signature focus treatment on every
  card (a slight grow, white outline, soft glow), rows that glide so the focused tile holds a
  steady keyline, Back returning to the exact card you left, a branded splash instead of a
  blank frame on launch, shimmer skeletons instead of spinners, a softly blurred ambient
  backdrop that settles in behind the home grid, and a full-bleed hero on the detail screen.
  Every focus lane declares a fallback, so a D-pad press always moves focus on the first try —
  even after the card it would have returned to is gone.

## Requirements

- A running drivecast server with **Remote Access enabled** (Settings → Remote Access in
  drivecast). That screen shows the server's access **token** — you'll need it to pair.
- The Fire TV and the server on the **same local network** (for auto-discovery; you can also
  type the server's IP manually).
- **Optional but recommended: VLC** (`org.videolan.vlc`), installed from the Amazon Appstore.
  When present, it becomes the default player for wider codec support; the built-in player is
  used automatically when it isn't installed.

## Pairing flow

1. Launch the app. It scans your `/24` network on port `8737` for the server.
2. Pick the discovered server, or choose **enter the server IP** and type it (e.g.
   `192.168.1.50`).
3. Enter the **access token** from the drivecast Remote Access screen and press **Pair**.
   - "Remote access is off" → turn on Remote Access in drivecast, then retry.
   - "Wrong token" → re-check the token in drivecast.
4. On success the pairing is saved and you land on Home. It persists across launches.

## Installing on a Fire TV Stick

### 1. Enable developer options on the Fire TV

1. **Settings → My Fire TV → About** and click the device name (e.g. "Fire TV Stick") seven
   times until it says you're a developer.
2. Back out to **Settings → My Fire TV → Developer options** and turn on:
   - **ADB debugging** → On
   - **Apps from Unknown Sources** → On (needed for the Downloader method below)
3. Note the device's IP under **Settings → My Fire TV → About → Network**.

### 2a. Install over ADB (recommended for development)

From a machine with the Android SDK / `adb` installed, on the same network:

```bash
# Connect to the Fire TV (accept the on-screen "Allow USB debugging" prompt the first time)
adb connect <fire-tv-ip>:5555

# Build and install the release APK (recommended — R8-minified, noticeably smoother)
./gradlew :app:assembleRelease
adb install -r app/build/outputs/apk/release/app-release.apk

# …or the debug APK for development
./gradlew installDebug
```

The app appears under **Your Apps & Channels** on the Fire TV home screen. Launch it with the
remote.

The release build is signed with the debug keystore on purpose: this app is sideloaded rather
than store-distributed, so release upgrades install over previous builds without losing your
pairing.

### 2b. Sideload with the Downloader app (no computer needed)

1. On the Fire TV, install **Downloader** from the Amazon Appstore.
2. Open Downloader and enter the latest release APK's URL:

   ```
   github.com/AdinathChaudhari/drivecast-app/releases/latest/download/drivecast-app.apk
   ```

3. Let it download, then choose **Install** when prompted. (This is why "Apps from
   Unknown Sources" must be on.)
4. Prefer your own build? Host it on any HTTP server the Fire TV can reach — e.g. from the
   repo: `python3 -m http.server 8000 -d app/build/outputs/apk/debug/` and enter
   `http://<your-computer-ip>:8000/app-debug.apk` in Downloader instead.

## Keeping the Mac awake (and letting it sleep)

The server holds a macOS power assertion while a stream is active, so the Mac
won't sleep out from under a movie. The app answers the server's keep-awake
handshake so this stays honest: once you've started playback, it checks in with
the server, and if playback has been paused/stopped long enough that the server
is about to let the Mac sleep, it shows an **"Are you still watching?"** prompt.
Choose **Yes, keep watching** to keep the Mac awake for another couple of
minutes, or **No** to let it sleep right away (which also exits the player if
it's open). Leave it and the Mac sleeps on its own — a paused TV never keeps
your Mac awake forever, and an actively-watching TV always does.

## Building from source

Requirements: JDK 17+ and the Android SDK (platform 34, build-tools 34.0.0). Create a
`local.properties` with your SDK path:

```
sdk.dir=/path/to/Android/sdk
```

Then:

```bash
./gradlew :app:assembleDebug      # development build
./gradlew :app:assembleRelease    # R8-minified — what you want on the Stick
# outputs: app/build/outputs/apk/{debug,release}/
```

## Project layout

- `app/src/main/java/com/drivecast/tv/`
  - `api/` — Retrofit interface, DTOs, token interceptor
  - `data/` — DataStore config, library repository, LAN discovery
  - `ui/setup` · `ui/home` · `ui/detail` · `ui/player` · `ui/common` · `ui/theme`
  - `di/AppContainer.kt` — manual dependency container
  - `MainActivity.kt` — Compose navigation host

## Notes

- The app talks to the server over plain HTTP on the LAN (`usesCleartextTraffic`). Keep the
  server on a trusted network.
- The access token is sent as a `?token=` query parameter on every request, matching the
  server's remote-access contract.
