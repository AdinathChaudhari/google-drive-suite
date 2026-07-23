package com.drivecast.tv.ui.home

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.drivecast.tv.api.ContinueItem
import com.drivecast.tv.api.SectionInfo
import com.drivecast.tv.api.Title
import com.drivecast.tv.di.AppContainer
import com.drivecast.tv.di.HomeData
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * Home-screen UI state. [titles] is null only on a true cold start — no cache on [AppContainer]
 * and the first fetch still in flight. After that it is always non-null, even while a refresh is
 * in flight, so a background reload never blanks the screen back to a spinner/skeleton.
 */
data class HomeUiState(
    val titles: List<Title>? = null,
    val sections: List<SectionInfo> = emptyList(),
    val continueItems: List<ContinueItem> = emptyList(),
    val refreshing: Boolean = false,
    val error: String? = null,
)

/**
 * Cache-first: seeds synchronously from [AppContainer.homeCache] when present (this ViewModel was
 * very likely destroyed and recreated by back-navigation, since Compose navigation scopes a
 * ViewModel to its NavBackStackEntry) so the grid renders on the very first frame, then always
 * kicks off a background refresh and writes the result back to the container so the next
 * HomeViewModel instance — after the next back-navigation — can do the same.
 */
class HomeViewModel(private val container: AppContainer) : ViewModel() {

    private val repository = container.repository

    private val _state = MutableStateFlow(
        container.homeCache?.let { cached ->
            HomeUiState(
                titles = cached.titles,
                sections = cached.sections,
                continueItems = cached.continueItems,
            )
        } ?: HomeUiState()
    )
    val state: StateFlow<HomeUiState> = _state.asStateFlow()

    init {
        load()
    }

    /** Re-fetches without ever nulling out `titles` — whatever is already on screen stays put. */
    fun refresh() = load()

    private fun load() {
        viewModelScope.launch {
            _state.update { it.copy(refreshing = true, error = null) }
            runCatching {
                val lib = repository.refresh()
                val sections = runCatching { repository.sections() }.getOrDefault(_state.value.sections)
                val continueItems =
                    runCatching { repository.continueWatching() }.getOrDefault(_state.value.continueItems)
                Triple(lib.titles, sections, continueItems)
            }.onSuccess { (titles, sections, continueItems) ->
                container.homeCache = HomeData(
                    titles = titles,
                    sections = sections,
                    continueItems = continueItems,
                    fetchedAtMs = System.currentTimeMillis(),
                )
                _state.update {
                    it.copy(
                        titles = titles,
                        sections = sections,
                        continueItems = continueItems,
                        refreshing = false,
                        error = null,
                    )
                }
            }.onFailure { e ->
                _state.update {
                    it.copy(refreshing = false, error = "Couldn't load the library. ${e.message ?: ""}".trim())
                }
            }
        }
    }

    /** Optimistically drops a Continue Watching item, then reconciles with the server's list. */
    fun dismissContinueItem(fileId: String) {
        val optimistic = _state.value.continueItems.filterNot { it.fileId == fileId }
        _state.update { it.copy(continueItems = optimistic) }
        viewModelScope.launch {
            repository.removeContinue(fileId)
            val updated = runCatching { repository.continueWatching() }.getOrDefault(optimistic)
            _state.update { it.copy(continueItems = updated) }
            container.homeCache = container.homeCache?.copy(continueItems = updated)
        }
    }

    companion object {
        fun factory(container: AppContainer): ViewModelProvider.Factory = viewModelFactory {
            initializer { HomeViewModel(container) }
        }
    }
}
