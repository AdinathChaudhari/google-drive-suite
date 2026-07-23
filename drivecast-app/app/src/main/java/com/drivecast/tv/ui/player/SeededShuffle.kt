package com.drivecast.tv.ui.player

/**
 * Deterministic seeded shuffle shared with the server (drivecast/shuffle.py).
 * Same seed + same list MUST give the same order on both sides — the app builds
 * its playback queue locally and the server emits the m3u VLC actually plays;
 * divergence would report progress against the wrong episode. Any change here
 * must be mirrored in shuffle.py and BOTH test vectors.
 */
object SeededShuffle {
    fun <T> shuffle(items: List<T>, seed: Long): List<T> {
        val out = items.toMutableList()
        var s = seed.toULong()
        fun next(): ULong {
            s += 0x9E3779B97F4A7C15uL
            var z = s
            z = (z xor (z shr 30)) * 0xBF58476D1CE4E5B9uL
            z = (z xor (z shr 27)) * 0x94D049BB133111EBuL
            return z xor (z shr 31)
        }
        for (i in out.lastIndex downTo 1) {
            val j = (next() % (i + 1).toULong()).toInt()
            val tmp = out[i]; out[i] = out[j]; out[j] = tmp
        }
        return out
    }
}
