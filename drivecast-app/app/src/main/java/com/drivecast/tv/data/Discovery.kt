package com.drivecast.tv.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.net.Inet4Address
import java.net.NetworkInterface
import java.util.concurrent.TimeUnit

/** A drivecast server found on the LAN. */
data class DiscoveredServer(
    val ip: String,
    val port: Int,
    val baseUrl: String,
    val remoteEnabled: Boolean,
)

/**
 * LAN discovery: probe every host on the device's /24 for an unauthenticated
 * `GET /api/ping` that answers `{"app":"drivecast", ...}` on port 8737. Uses a
 * dedicated short-timeout OkHttpClient and bounded concurrency so a full sweep
 * finishes in a few seconds even when most hosts are dark.
 */
class Discovery(private val port: Int = 8737) {

    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(300, TimeUnit.MILLISECONDS)
        .readTimeout(400, TimeUnit.MILLISECONDS)
        .callTimeout(700, TimeUnit.MILLISECONDS)
        .retryOnConnectionFailure(false)
        .build()

    /** Return every drivecast server reachable on the local /24, in address order. */
    suspend fun scan(): List<DiscoveredServer> = coroutineScope {
        val prefixes = localSubnetPrefixes()
        if (prefixes.isEmpty()) return@coroutineScope emptyList()
        val gate = Semaphore(64)
        val results = prefixes.flatMap { prefix ->
            (1..254).map { host ->
                val ip = "$prefix.$host"
                async(Dispatchers.IO) {
                    gate.withPermit { probe(ip) }
                }
            }
        }.awaitAll()
        results.filterNotNull().distinctBy { it.ip }
    }

    /** Probe a single explicit host (used by the manual-IP path in setup). */
    suspend fun probe(ip: String): DiscoveredServer? = withContext(Dispatchers.IO) {
        val base = "http://$ip:$port"
        val request = Request.Builder()
            .url("$base/api/ping")
            .header("Connection", "close")
            .build()
        try {
            client.newCall(request).execute().use { resp ->
                val body = resp.body?.string().orEmpty()
                if (resp.isSuccessful && body.contains("\"drivecast\"")) {
                    DiscoveredServer(
                        ip = ip,
                        port = port,
                        baseUrl = base,
                        remoteEnabled = body.contains("\"remote\": true") ||
                            body.contains("\"remote\":true"),
                    )
                } else null
            }
        } catch (_: Exception) {
            null
        }
    }

    /** The /24 network prefixes (e.g. "192.168.1") for each non-loopback IPv4 interface. */
    private fun localSubnetPrefixes(): List<String> {
        val prefixes = mutableListOf<String>()
        try {
            for (iface in NetworkInterface.getNetworkInterfaces()) {
                if (!iface.isUp || iface.isLoopback) continue
                for (addr in iface.inetAddresses) {
                    if (addr is Inet4Address && !addr.isLoopbackAddress) {
                        val ip = addr.hostAddress ?: continue
                        val prefix = ip.substringBeforeLast('.', "")
                        if (prefix.isNotEmpty() && prefix !in prefixes) prefixes.add(prefix)
                    }
                }
            }
        } catch (_: Exception) {
            // No network access enumerated; caller falls back to manual entry.
        }
        return prefixes
    }
}
