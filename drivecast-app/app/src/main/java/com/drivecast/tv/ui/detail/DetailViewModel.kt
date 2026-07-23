package com.drivecast.tv.ui.detail

import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.createSavedStateHandle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.drivecast.tv.api.Title
import com.drivecast.tv.api.WatchedProgress
import com.drivecast.tv.di.AppContainer
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import java.io.IOException

/**
 * Detail-screen UI state. [title] is null only on a true cold start (first fetch still in
 * flight, or it failed before ever landing); once non-null it is never nulled out again, so a
 * background reload (e.g. [refreshProgress] on resume) never blanks the screen back to a
 * skeleton or error view.
 */
data class DetailUiState(
    val title: Title? = null,
    val progress: Map<String, WatchedProgress> = emptyMap(),
    val loading: Boolean = true,
    val error: String? = null,
    val networkError: Boolean = false,
)

/**
 * Scoped to the detail destination's [androidx.navigation.NavBackStackEntry] via the default
 * `viewModel()` factory lookup (manual DI via [factory], matching [com.drivecast.tv.ui.home.HomeViewModel]).
 * Detail stays on the nav back stack while its player destination is pushed on top, so
 * detail -> player -> back finds this exact instance alive: the title and watched-progress map
 * are never refetched, and there is never a spinner on return. [titleId] comes from the nav
 * argument via [SavedStateHandle] rather than a constructor parameter, so the factory needs no
 * screen-specific arguments beyond the [AppContainer].
 */
class DetailViewModel(
    private val container: AppContainer,
    savedStateHandle: SavedStateHandle,
) : ViewModel() {

    private val titleId: String = checkNotNull(savedStateHandle["titleId"]) {
        "DetailViewModel requires a titleId nav argument"
    }

    private val _state = MutableStateFlow(DetailUiState())
    val state: StateFlow<DetailUiState> = _state.asStateFlow()

    init {
        load()
    }

    /** Retry from [com.drivecast.tv.ui.common.StatusView]'s primary action. */
    fun reload() = load()

    private fun load() {
        viewModelScope.launch {
            _state.update { it.copy(loading = it.title == null, error = null, networkError = false) }
            runCatching {
                val fetched = container.repository.title(titleId)
                val progress =
                    runCatching { container.repository.watchedMap().progress }.getOrDefault(_state.value.progress)
                fetched to progress
            }.onSuccess { (fetched, progress) ->
                _state.update {
                    val resolved = fetched ?: it.title
                    it.copy(
                        title = resolved,
                        progress = progress,
                        loading = false,
                        error = if (resolved == null) "Title not found." else null,
                        networkError = false,
                    )
                }
            }.onFailure { e ->
                _state.update {
                    it.copy(
                        loading = false,
                        // Whatever is already on screen stays put; only a true cold-start
                        // failure (no title ever landed) surfaces the error state.
                        error = if (it.title == null) (e.message ?: "Couldn't load this title.") else null,
                        networkError = it.title == null && e is IOException,
                    )
                }
            }
        }
    }

    /**
     * Silently refreshes only the watched-progress map as an in-place update — called on
     * detail-screen resume (e.g. back from the player) so checkmarks/percentages tick over
     * without ever refetching the title or touching [DetailUiState.loading]/[DetailUiState.error].
     */
    fun refreshProgress() {
        viewModelScope.launch {
            runCatching { container.repository.watchedMap().progress }
                .onSuccess { progress -> _state.update { it.copy(progress = progress) } }
        }
    }

    companion object {
        fun factory(container: AppContainer): ViewModelProvider.Factory = viewModelFactory {
            initializer { DetailViewModel(container, createSavedStateHandle()) }
        }
    }
}
