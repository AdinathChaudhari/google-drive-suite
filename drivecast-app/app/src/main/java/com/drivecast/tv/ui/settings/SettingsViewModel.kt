package com.drivecast.tv.ui.settings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.drivecast.tv.api.SectionInfo
import com.drivecast.tv.data.toTabPatch
import com.drivecast.tv.di.AppContainer
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * Settings-screen UI state. [tabs] is the editable, in-memory ordered list of every server tab —
 * loaded via [com.drivecast.tv.data.LibraryRepository.sections] (server order, including tabs with
 * no titles yet), not [com.drivecast.tv.ui.home.HomeViewModel]'s has-titles-filtered subset.
 * [renameTarget] is non-null only while the rename Dialog is open, holding the [SectionInfo] being
 * renamed so its stable `key` survives even if [tabs] reorders underneath it while the dialog is
 * up. [saveVersion] increments on every *successful* persist; [SettingsScreen] watches it to fire
 * its own one-shot "tabs changed" nav signal back to Home without a separate event channel.
 */
data class SettingsUiState(
    val tabs: List<SectionInfo> = emptyList(),
    val loading: Boolean = true,
    val saving: Boolean = false,
    val error: String? = null,
    val renameTarget: SectionInfo? = null,
    val saveVersion: Int = 0,
)

/**
 * Backs the TV Settings screen's tab list/reorder/rename (LIST, RENAME, REORDER — see the feature
 * spec). LIST loads the full server-ordered tab list via
 * [com.drivecast.tv.data.LibraryRepository.sections] (GET /api/sections). Every mutation (a
 * discrete D-pad reorder swap, or a rename) is applied to the in-memory [SettingsUiState.tabs]
 * first, then round-tripped in full via
 * [com.drivecast.tv.data.LibraryRepository.saveTabs] (POST /api/settings) — mirroring the web UI's
 * currentTabsPayload() contract exactly via [com.drivecast.tv.data.toTabPatch]: every tab's own
 * `key` is preserved (renames never re-slugify it, so `drive_sections` assignments never break),
 * order is whatever [tabs] currently holds, and accent/accent2 are echoed back so the server's
 * `validate_tabs` doesn't re-palette on an unrelated rename/reorder save.
 *
 * Scoped like [com.drivecast.tv.ui.home.HomeViewModel] — manual DI via [factory], no nav argument
 * needed beyond the [AppContainer].
 */
class SettingsViewModel(private val container: AppContainer) : ViewModel() {

    private val repository = container.repository

    private val _state = MutableStateFlow(SettingsUiState())
    val state: StateFlow<SettingsUiState> = _state.asStateFlow()

    init {
        load()
    }

    /** Retry from the empty/error [com.drivecast.tv.ui.common.StatusView] action. */
    fun reload() = load()

    private fun load() {
        viewModelScope.launch {
            _state.update { it.copy(loading = it.tabs.isEmpty(), error = null) }
            runCatching { repository.sections() }
                .onSuccess { list ->
                    _state.update { it.copy(tabs = list, loading = false, error = null) }
                }
                .onFailure { e ->
                    _state.update {
                        it.copy(loading = false, error = "Couldn't load tabs. ${e.message ?: ""}".trim())
                    }
                }
        }
    }

    /** Discrete D-pad reorder: swaps [index] with the row above it (a no-op at the top). */
    fun moveUp(index: Int) = swap(index - 1, index)

    /** Discrete D-pad reorder: swaps [index] with the row below it (a no-op at the bottom). */
    fun moveDown(index: Int) = swap(index, index + 1)

    private fun swap(a: Int, b: Int) {
        val list = _state.value.tabs
        if (a < 0 || b >= list.size || a >= b) return
        val reordered = list.toMutableList().apply {
            this[a] = list[b]
            this[b] = list[a]
        }
        _state.update { it.copy(tabs = reordered) }
        persist()
    }

    /** Opens the rename Dialog for [target]. */
    fun openRename(target: SectionInfo) {
        _state.update { it.copy(renameTarget = target) }
    }

    /** Closes the rename Dialog without changing anything. */
    fun dismissRename() {
        _state.update { it.copy(renameTarget = null) }
    }

    /**
     * Commits the rename Dialog: relabels the tab under [SettingsUiState.renameTarget] by its
     * stable `key` (the key itself never changes) and persists. A blank name is treated as a
     * cancel — the dialog closes with no change and no save.
     */
    fun confirmRename(newLabel: String) {
        val target = _state.value.renameTarget ?: return
        val trimmed = newLabel.trim()
        if (trimmed.isEmpty()) {
            _state.update { it.copy(renameTarget = null) }
            return
        }
        val renamed = _state.value.tabs.map { if (it.key == target.key) it.copy(label = trimmed) else it }
        _state.update { it.copy(tabs = renamed, renameTarget = null) }
        persist()
    }

    private fun persist() {
        val patch = toTabPatch(_state.value.tabs)
        viewModelScope.launch {
            _state.update { it.copy(saving = true, error = null) }
            runCatching { repository.saveTabs(patch) }
                .onSuccess { resp ->
                    _state.update {
                        if (resp.ok) {
                            it.copy(tabs = resp.tabs, saving = false, error = null, saveVersion = it.saveVersion + 1)
                        } else {
                            it.copy(saving = false, error = "Couldn't save changes.")
                        }
                    }
                }
                .onFailure { e ->
                    _state.update {
                        it.copy(saving = false, error = "Couldn't save changes. ${e.message ?: ""}".trim())
                    }
                }
        }
    }

    companion object {
        fun factory(container: AppContainer): ViewModelProvider.Factory = viewModelFactory {
            initializer { SettingsViewModel(container) }
        }
    }
}
