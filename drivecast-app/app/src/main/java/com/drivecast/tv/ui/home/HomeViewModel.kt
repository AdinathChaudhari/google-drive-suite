package com.drivecast.tv.ui.home

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.drivecast.tv.api.ContinueItem
import com.drivecast.tv.api.RefreshStatus
import com.drivecast.tv.api.SectionInfo
import com.drivecast.tv.api.Title
import com.drivecast.tv.data.RescanStart
import com.drivecast.tv.di.AppContainer
import com.drivecast.tv.di.HomeData
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
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
    val sort: SortSpec = SortSpec(),
    val group: GroupKey = GroupKey.NONE,
    val watchedMap: Map<String, Double> = emptyMap(),
    val scan: ScanUi = ScanUi(),
)

/** Live-scan chrome: [label] is the progress line while running; [notice] is a
 *  transient (5s) result/rejection message. Never both non-null. */
data class ScanUi(
    val running: Boolean = false,
    val label: String? = null,
    val notice: String? = null,
    val noticeIsError: Boolean = false,
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
        (container.homeCache?.let { cached ->
            HomeUiState(
                titles = cached.titles,
                sections = cached.sections,
                continueItems = cached.continueItems,
                watchedMap = cached.watchedMap,
            )
        } ?: HomeUiState()).let { base ->
            container.viewPrefs?.let { base.copy(sort = it.sort, group = it.group) } ?: base
        }
    )
    val state: StateFlow<HomeUiState> = _state.asStateFlow()

    private var pollJob: Job? = null
    private var noticeJob: Job? = null
    private var startingRescan = false
    // Anchors the 30-min poll cap to when the scan STARTED, not when startPolling() (re)launches
    // the poll job — set once per scan (rescan()/load()'s re-attach), never touched by
    // resumePolling(), so a background/foreground cycle mid-scan can't reset the deadline.
    private var scanStartedAtMs = 0L

    init {
        if (container.viewPrefs == null) {
            viewModelScope.launch {
                val cfg = container.configStore.config.first()
                val key = SortKey.fromId(cfg.sortKeyId)
                val prefs = SortAndGroupPrefs(
                    sort = SortSpec(key, cfg.sortAscending ?: key.defaultAscending),
                    group = GroupKey.fromId(cfg.groupId),
                )
                container.viewPrefs = prefs
                _state.update { it.copy(sort = prefs.sort, group = prefs.group) }
            }
        }
        load()
    }

    /** Re-fetches without ever nulling out `titles` — whatever is already on screen stays put. */
    fun refresh() = load()

    private data class Loaded(
        val titles: List<Title>,
        val sections: List<SectionInfo>,
        val continueItems: List<ContinueItem>,
        val watched: Map<String, Double>,
        val scanning: Boolean,
    )

    private fun load() {
        viewModelScope.launch {
            _state.update { it.copy(refreshing = true, error = null) }
            runCatching {
                val lib = repository.refresh()
                val sections = runCatching { repository.sections() }.getOrDefault(_state.value.sections)
                val continueItems =
                    runCatching { repository.continueWatching() }.getOrDefault(_state.value.continueItems)
                val watched = runCatching { repository.watchedMap().map }.getOrDefault(_state.value.watchedMap)
                Loaded(lib.titles, sections, continueItems, watched, lib.scanning)
            }.onSuccess { (titles, sections, continueItems, watched, scanning) ->
                container.homeCache = HomeData(
                    titles = titles,
                    sections = sections,
                    continueItems = continueItems,
                    fetchedAtMs = System.currentTimeMillis(),
                    watchedMap = watched,
                )
                _state.update {
                    it.copy(
                        titles = titles,
                        sections = sections,
                        continueItems = continueItems,
                        watchedMap = watched,
                        refreshing = false,
                        error = null,
                    )
                }
                // Re-attach to a scan already running server-side (process death / cold start
                // mid-scan) — same trick as the web UI's app.js line 462. The real start time
                // isn't known here (the server doesn't report one), so the 30-min cap starts
                // counting from this re-attach — the best available anchor for this rare path.
                if (scanning) {
                    scanStartedAtMs = System.currentTimeMillis()
                    _state.update { it.copy(scan = it.scan.copy(running = true, label = it.scan.label ?: "Updating library…")) }
                    startPolling()
                }
            }.onFailure { e ->
                _state.update {
                    it.copy(refreshing = false, error = "Couldn't load the library. ${e.message ?: ""}".trim())
                }
            }
        }
    }

    /** Header ⟳: ask the server for a REAL full rescan, then watch it. */
    fun rescan() {
        if (startingRescan || _state.value.scan.running) return   // ignore re-presses
        startingRescan = true
        viewModelScope.launch {
            try {
                when (val r = runCatching { repository.startRescan() }
                    .getOrElse { RescanStart.Rejected("Couldn't reach the server. ${it.message ?: ""}".trim()) }) {
                    RescanStart.Started, RescanStart.AlreadyRunning -> {
                        scanStartedAtMs = System.currentTimeMillis()
                        _state.update { it.copy(scan = ScanUi(running = true, label = "Updating library…")) }
                        startPolling()
                    }
                    is RescanStart.Rejected -> showNotice(r.message, isError = true)
                }
            } finally { startingRescan = false }
        }
    }

    private fun startPolling() {
        if (pollJob?.isActive == true) return
        pollJob = viewModelScope.launch {
            var failures = 0
            // Anchored to scanStartedAtMs (set once when the scan began), NOT recomputed here —
            // resumePolling() calls startPolling() again after every background/foreground
            // cycle, and a `now + 30min` deadline would reset on each one (MINOR 7).
            val deadlineMs = scanStartedAtMs + 30L * 60 * 1000   // absolute cap: 30 min
            // Poll FIRST, check the deadline AFTER: a (re)started loop (e.g. resumePolling()
            // after the app was backgrounded past the cap) must ask the server what's actually
            // happening before ever surfacing the timeout notice — otherwise resuming past a
            // stale deadline reports "taking unusually long" even when the scan already finished
            // while the app was away, instead of settling into the normal completion path below.
            while (true) {
                val st = runCatching { repository.rescanStatus() }.getOrNull()
                when {
                    st == null -> {
                        failures++
                        if (failures >= 10) {                               // ~12s unreachable
                            _state.update { it.copy(scan = ScanUi()) }
                            showNotice("Lost contact with the server during the scan.", isError = true)
                            return@launch
                        }
                    }
                    !st.running -> { finishScan(st); return@launch }
                    else -> {
                        failures = 0
                        _state.update { it.copy(scan = ScanUi(running = true, label = scanStatusLabel(st))) }
                    }
                }
                if (System.currentTimeMillis() >= deadlineMs) {
                    _state.update { it.copy(scan = ScanUi()) }
                    showNotice("Scan is taking unusually long — check the server.", isError = true)
                    return@launch
                }
                delay(1_200)                                                // mirrors web's 1200ms
            }
        }
    }

    private fun finishScan(st: RefreshStatus) {
        _state.update { it.copy(scan = ScanUi()) }
        // st.error is a raw Python str(e) (library.py:1521) — can run to hundreds of chars.
        // Truncated here (not just at render time) so it can't starve the header's ⟳/⚙ icons.
        if (st.error != null) showNotice("Scan finished with issues: ${st.error.take(60)}", isError = true)
        else showNotice(scanCompleteNotice(st.added, st.removed), isError = false)
        load()                                                              // reload library+sections+continue, refreshes homeCache
    }

    private fun showNotice(msg: String, isError: Boolean) {
        noticeJob?.cancel()
        noticeJob = viewModelScope.launch {
            _state.update { it.copy(scan = it.scan.copy(notice = msg, noticeIsError = isError)) }
            delay(5_000)
            _state.update { it.copy(scan = it.scan.copy(notice = null)) }
        }
    }

    /** Backgrounded mid-poll: stop hitting the network; scan.running stays true so resume can re-attach. */
    fun pausePolling() { pollJob?.cancel(); pollJob = null }
    fun resumePolling() { if (_state.value.scan.running) startPolling() }

    /** Re-fetches just the watched map (never nulls out anything else already on screen) — called
     *  when the user picks SortKey.WATCHED and again whenever Home resumes WHILE that sort is
     *  active (the same ON_START hook that drives [resumePolling], including a resume after
     *  playback, but gated there on sort == WATCHED so the other three sort keys — which never
     *  read this map — don't get reordered under a just-restored scroll position), so "Recently
     *  watched" never shows an order stale from before the last thing the user watched. */
    fun refreshWatchedMap() {
        viewModelScope.launch {
            val watched = runCatching { repository.watchedMap().map }.getOrNull() ?: return@launch
            _state.update { it.copy(watchedMap = watched) }
            container.homeCache = container.homeCache?.copy(watchedMap = watched)
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

    fun pickSort(picked: SortKey) {
        val next = nextSortSpec(_state.value.sort, picked)
        applyPrefs(SortAndGroupPrefs(next, _state.value.group))
        // watchedMap is otherwise only fetched inside load() — re-fetch here so picking this
        // sort never shows a stale order for the rest of the ViewModel's life (MAJOR 2).
        if (picked == SortKey.WATCHED) refreshWatchedMap()
    }

    fun pickGroup(picked: GroupKey) {
        applyPrefs(SortAndGroupPrefs(_state.value.sort, picked))
    }

    private fun applyPrefs(prefs: SortAndGroupPrefs) {
        container.viewPrefs = prefs
        _state.update { it.copy(sort = prefs.sort, group = prefs.group) }
        viewModelScope.launch {
            container.configStore.saveViewPrefs(prefs.sort.key.id, prefs.sort.ascending, prefs.group.id)
        }
    }

    companion object {
        fun factory(container: AppContainer): ViewModelProvider.Factory = viewModelFactory {
            initializer { HomeViewModel(container) }
        }
    }
}
