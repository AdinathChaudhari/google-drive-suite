package com.drivecast.tv.data

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "server_config")

/** Persisted server pairing: base URL, access token, subtitle preference. */
data class ServerConfig(
    val baseUrl: String? = null,
    val token: String? = null,
    val subtitlesEnabled: Boolean = true,
) {
    val isConfigured: Boolean get() = !baseUrl.isNullOrBlank() && !token.isNullOrBlank()
}

class ServerConfigStore(private val context: Context) {

    private object Keys {
        val BASE_URL = stringPreferencesKey("base_url")
        val TOKEN = stringPreferencesKey("token")
        val SUBTITLES = booleanPreferencesKey("subtitles_enabled")
    }

    val config: Flow<ServerConfig> = context.dataStore.data.map { prefs ->
        ServerConfig(
            baseUrl = prefs[Keys.BASE_URL],
            token = prefs[Keys.TOKEN],
            subtitlesEnabled = prefs[Keys.SUBTITLES] ?: true,
        )
    }

    suspend fun save(baseUrl: String, token: String) {
        context.dataStore.edit { prefs ->
            prefs[Keys.BASE_URL] = baseUrl
            prefs[Keys.TOKEN] = token
        }
    }

    suspend fun setSubtitlesEnabled(enabled: Boolean) {
        context.dataStore.edit { prefs -> prefs[Keys.SUBTITLES] = enabled }
    }

    suspend fun clear() {
        context.dataStore.edit { it.clear() }
    }
}
