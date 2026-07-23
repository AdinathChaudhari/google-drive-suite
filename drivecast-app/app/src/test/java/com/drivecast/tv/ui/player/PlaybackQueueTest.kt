package com.drivecast.tv.ui.player

import com.drivecast.tv.api.Episode
import com.drivecast.tv.api.Season
import com.drivecast.tv.api.Title
import org.junit.Assert.assertEquals
import org.junit.Test

class PlaybackQueueTest {

    private fun ep(id: String, num: Int? = null) = Episode(episode = num, fileId = id, name = id)

    private val show = Title(
        id = "showB",
        type = "show",
        title = "The Bear",
        seasons = listOf(
            Season(season = 1, episodes = listOf(ep("e1", 1), ep("e2", 2))),
            Season(season = 2, episodes = listOf(ep("e3", 1))),
            // Extras pseudo-season (bonus clip) — must be excluded from the main queue.
            Season(season = 0, name = "Featurettes", extras = true, episodes = listOf(ep("x1"))),
        ),
    )

    @Test
    fun mainEpisode_buildsFullQueueExcludingExtras() {
        val q = PlaybackQueue.build(show, "e2")
        assertEquals(listOf("e1", "e2", "e3"), q.map { it.fileId })
        assertEquals(1, PlaybackQueue.startIndex(q, "e2"))
    }

    @Test
    fun extrasClip_playsOnlyThatClip() {
        // Regression guard: clicking a show's featurette must play just that clip,
        // not fall through to S01E01.
        val q = PlaybackQueue.build(show, "x1")
        assertEquals(listOf("x1"), q.map { it.fileId })
    }

    @Test
    fun shuffle_matchesSeededShuffleOfMainEpisodes() {
        val seed = 123456789L
        val q = PlaybackQueue.build(show, "e1", shuffle = true, seed = seed)
        val expected = SeededShuffle.shuffle(listOf("e1", "e2", "e3"), seed)
        assertEquals(expected, q.map { it.fileId })
        assertEquals(0, PlaybackQueue.startIndex(q, "e1", shuffle = true))
    }
}
