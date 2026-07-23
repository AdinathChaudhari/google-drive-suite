"""Deterministic seeded shuffle shared with the Fire TV app (SeededShuffle.kt).

Same seed + same input list MUST produce the same order on both sides — the app
builds its VLC playback queue locally and the server emits the m3u playlist that
VLC actually plays; if the two orders diverge, progress is reported against the
wrong episode. Any change here MUST be mirrored in SeededShuffle.kt and in BOTH
test vectors (test_shuffle.py / SeededShuffleTest.kt)."""

MASK = 0xFFFFFFFFFFFFFFFF


def seeded_shuffle(items, seed):
    out = list(items)
    s = seed & MASK

    def _next():
        nonlocal s
        s = (s + 0x9E3779B97F4A7C15) & MASK
        z = s
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
        return (z ^ (z >> 31)) & MASK

    for i in range(len(out) - 1, 0, -1):
        j = _next() % (i + 1)
        out[i], out[j] = out[j], out[i]
    return out
