# TUNING.md

Fine-tuning reference for the `fix/nav-finesse` finesse pass (WI-001..WI-007). Documents the current value of every tunable knob touched this pass, with file:line, feel, and safe range, so future tuning doesn't require re-reading source.

**PROJECT INVARIANT:** every `tvFocusRestorer` keeps an `onRestoreFailed` fallback — do not remove it while tuning (`FocusKit.kt:29-46`, landed WI-001).

All paths are relative to `app/src/main/java/com/drivecast/tv/`.

## 1. Focus indicator — `ui/common/PosterCard.kt`

| Param | file:line | Current value | Feels like | Safe range |
|---|---|---|---|---|
| focusedScale | PosterCard.kt:93 | 1.06f **[WI-004]** | Subtle lift, not a jump | 1.05 – 1.12 |
| pressedScale | PosterCard.kt:93 | 0.95f | Tactile press-in | 0.92 – 0.98 |
| Border width | PosterCard.kt:96 | 2.5.dp | Crisp, visible outline | 1.5 – 3.0 |
| Border alpha | PosterCard.kt:96 | White @ 0.85 | Near-solid, not glary | 0.7 – 1.0 |
| Border inset | PosterCard.kt:97 | 3.dp | Border sits just inside the card edge | 0 – 4 |
| Border corner radius | PosterCard.kt:98 | 10.dp | Soft-rounded frame | 6 – 12 |
| Glow color/alpha | PosterCard.kt:102 | Accent @ 0.35 | Warm halo, not neon | 0.2 – 0.5 |
| Glow elevation | PosterCard.kt:102 | 10.dp **[WI-004]** | Gentle lift; keeps GPU/frame budget on 1GB Sticks | 8 – 20 (lower for frame budget) |
| Poster image crossfade | PosterCard.kt:127 | tween(200) | Quick fade-in, no pop | 0 – 300 |

## 2. Pills / rows

| Param | file:line | Current value | Feels like | Safe range |
|---|---|---|---|---|
| PillButton focusedScale | HomeScreen.kt:705 | 1.025f | Barely-there nudge | 1.02 – 1.05 |
| PillButton color anim | HomeScreen.kt:695, 700 | tween(`MotionTokens.DurationShort` = 200) | Snappy selected/unselected swap | 120 – 250 |
| SeasonPill color anim | DetailScreen.kt:649, 654 | tween(`MotionTokens.DurationShort` = 200) | Snappy selected swap | 120 – 250 |
| EpisodeRow focusedScale | DetailScreen.kt:677 | 1.02f | Minimal row-item lift | 1.01 – 1.04 |

## 3. Motion tokens — `ui/theme/MotionTokens.kt`

| Param | file:line | Current value | Feels like | Safe range |
|---|---|---|---|---|
| DurationShort | MotionTokens.kt:15 | 200 | Pill/color state swaps | 120 – 250 |
| DurationMedium | MotionTokens.kt:16 | 300 | Loading crossfades, grid reflow | — |
| DurationLong | MotionTokens.kt:17 | 500 | (currently unused by any pass-touched knob) | — |

Easings defined alongside (MotionTokens.kt:10-13): `Emphasized`, `EmphasizedDecelerate` (entrances), `EmphasizedAccelerate` (exits), `StandardDecelerate`.

## 4. Nav / tab transitions

| Param | file:line | Current value | Feels like | Safe range |
|---|---|---|---|---|
| Nav enter (fadeIn+scaleIn) | MainActivity.kt:96-101 | tween(210), **no delayMillis** **[WI-003]** | Immediate, no dead-air before motion starts | dur 150 – 250, delay 0 |
| Nav popEnter (fadeIn+scaleIn) | MainActivity.kt:106-111 | tween(210), **no delayMillis** **[WI-003]** | Same as above, back nav | dur 150 – 250, delay 0 |
| Nav exit | MainActivity.kt:103-104 | fadeOut(tween(90)) | Quick outgoing fade | — |
| Nav popExit | MainActivity.kt:113-115 | fadeOut(tween(90)) + scaleOut(0.96f, tween(210)) | — | — |
| Tab AnimatedContent enter (fadeIn+scaleIn) | HomeScreen.kt:307-308 | tween(210), **no delayMillis** **[WI-003]** | Tab switch feels instant | dur 150 – 250, delay 0 |
| Tab AnimatedContent exit | HomeScreen.kt:309 | fadeOut(tween(90)) | Quick outgoing fade | — |
| Home loading Crossfade | HomeScreen.kt:141 | tween(300) | Skeleton→content settle | — |
| Detail loading Crossfade | DetailScreen.kt:114 | tween(300) | Skeleton→content settle | — |
| Season Crossfade | DetailScreen.kt:602 | tween(220) | Episode pane swap on season change | 150 – 300 |
| Grid item placement | HomeScreen.kt:465 | tween(300, Emphasized) | Chip-filtered items glide to new slot | — |

## 5. Backdrop dwell / blur — `ui/home/HomeScreen.kt`

| Param | file:line | Current value | Feels like | Safe range |
|---|---|---|---|---|
| Dwell debounce before backdrop swap | HomeScreen.kt:250 | 400ms | Backdrop only follows a real pause, not every pass-over | 250 – 600 (KEEP) |
| Backdrop Crossfade | HomeScreen.kt:502 | tween(320) **[WI-005]** | Smooth backdrop cut, tightened from 500 | 300 – 600 |
| Blur decode size | HomeScreen.kt:511 | 384×216 **[WI-005]** | Cheaper decode, no visible quality loss under blur | 320×180 – 480×270 |
| Blur radius | HomeScreen.kt:512 | 18 **[WI-005]** | Soft wash, less GPU cost than 25 | 15 – 30 |
| Backdrop image alpha | HomeScreen.kt:522 | 0.35 | Subtle wash behind scrim | 0.25 – 0.5 |

## 6. Season dwell — `ui/detail/DetailScreen.kt`

| Param | file:line | Current value | Feels like | Safe range |
|---|---|---|---|---|
| Dwell-commit (pass-over) | DetailScreen.kt:473 | 250ms | Season only commits after a real pause | 200 – 400 (pass-over only; explicit SELECT commits instantly, **WI-006**) |
| Scroll-to-top on season change | DetailScreen.kt:485 | `animateScrollToItem(0)` | Episode list resets to top | — |

## 7. Pivot keyline — `ui/common/FocusKit.kt`

| Param | file:line | Current value | Feels like | Safe range |
|---|---|---|---|---|
| parentFraction | FocusKit.kt:57 (default), call site HomeScreen.kt:258 | 0.10f | Focused card holds near top of viewport | 0.08 – 0.25 (unchanged this pass) |
| Already-visible no-op | FocusKit.kt:65 | `if (offset >= 0f && offset + size <= containerSize) return 0f` **[WI-007]** | No re-pin animation on D-pad moves within an already-visible viewport | — |

## 8. Grid / layout geometry — `ui/home/HomeScreen.kt`

| Param | file:line | Current value | Feels like | Safe range |
|---|---|---|---|---|
| Grid cell size | HomeScreen.kt:375 | `GridCells.Adaptive(160.dp)` | Poster grid density | 140 – 180 |
| Grid contentPadding | HomeScreen.kt:376 | start 48 / end 48 / **top 8** / bottom 48 **[WI-002]** | Symmetric headroom above shelf header | — |
| Grid spacing | HomeScreen.kt:377-378 | spacedBy(16) | — | — |
| Continue LazyRow contentPadding | HomeScreen.kt:408 | vertical = 24.dp **[WI-002]** | Room for focus scale/glow without clipping | — |
| Continue header→row spacer | HomeScreen.kt:404 | 4.dp **[WI-002]** | Compensates for the new row padding above | — |
| ContinueCard width | HomeScreen.kt:727 | 140.dp | — | — |
| LibraryTile width | HomeScreen.kt:638 | 160.dp | — | — |

## 9. Up-Next countdown

| Param | file:line | Current value | Feels like | Safe range |
|---|---|---|---|---|
| Countdown | PlayerViewModel.kt:166, ExternalPlayerViewModel.kt:310 | `5 downTo 1` | 5-second grace before auto-advance | 5 – 15 |
| Ring divisor | PlayerScreen.kt:436 | `/ 5f` | Ring drains exactly over the countdown | **MUST equal countdown seconds** |
| Ring tick | PlayerScreen.kt:437 | tween(1000, Linear) | Smooth 1Hz drain, not a jumping digit | — |

## 10. Coil / cache — `di/AppContainer.kt`

| Param | file:line | Current value | Feels like | Safe range |
|---|---|---|---|---|
| Global image crossfade | AppContainer.kt:70 | 200 | — | — |
| Bitmap config | AppContainer.kt:68 | RGB_565 | Halves bitmap memory on 1GB Sticks | keep |
| Memory cache | AppContainer.kt:73 | maxSizePercent(0.20) | — | 0.15 – 0.25 |
| Disk cache | AppContainer.kt:79 | maxSizeBytes(128MB) | — | 64 – 256MB |
