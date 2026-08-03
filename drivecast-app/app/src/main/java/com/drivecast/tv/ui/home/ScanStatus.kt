package com.drivecast.tv.ui.home

import com.drivecast.tv.api.RefreshStatus

/**
 * Header status line for a running scan. Mirrors the web UI (`app.js` § `pollScan`)
 * and the menubar (`_poll`) exactly, including the established contract:
 * a cache-only rebuild reports total=0, which is NOT "0 of 0 scanned" — render
 * "Updating library…" rather than a fraction.
 */
fun scanStatusLabel(st: RefreshStatus): String {
    val base = when {
        st.total == 0 -> "Updating library…"
        st.scopeNames.isNotEmpty() && st.scopeNames.size <= 3 ->
            "Refreshing ${st.scopeNames.joinToString(", ")}… ${st.scanned}/${st.total}"
        else -> "Scanning drives… ${st.scanned}/${st.total}"
    }
    return if (st.added > 0) "$base · +${st.added} new" else base
}

/**
 * Completion notice for a finished scan (no error): a scan that finds nothing to add or remove
 * previously said nothing at all. Kept to a single short clause each so it can't reintroduce the
 * header-squeeze this round exists to fix (MAJOR 1) — see [scanStatusLabel] for the running-scan
 * line this hands off from.
 */
fun scanCompleteNotice(added: Int, removed: Int): String = when {
    added > 0 && removed > 0 -> "+$added new · $removed removed"
    added > 0 -> "+$added new"
    removed > 0 -> "$removed removed"
    else -> "Library up to date"
}
