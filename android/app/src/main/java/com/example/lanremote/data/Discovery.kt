package com.example.lanremote.data

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Handler
import android.os.Looper
import com.example.lanremote.net.Protocol
import java.util.ArrayDeque

/** Where a discovery run stands, for the connect screen's empty state. */
enum class DiscoveryStatus { Idle, Searching, Failed }

/**
 * Discovers LazeR laptops on the LAN via mDNS/NSD (service type "_lazer._udp.").
 * Resolves are serialized because NsdManager allows only one at a time.
 *
 * NSD calls back on its own binder threads while start()/stop() run on the main
 * thread, so every field below is guarded by [lock]. Each start() is a new
 * generation, and a callback from an earlier one — a resolve still in flight when
 * the scan was restarted — is dropped rather than adding a host to, or releasing
 * the resolve slot of, the run that replaced it. That old resolve still holds
 * NsdManager's one slot until it ends, so the new run's first resolve can be
 * refused as already active; it is retried shortly instead of losing the host.
 */
class Discovery(context: Context) {

    private val nsd = context.getSystemService(Context.NSD_SERVICE) as NsdManager
    private val lock = Any()
    private val found = LinkedHashMap<String, DiscoveredHost>()
    private val resolveQueue = ArrayDeque<NsdServiceInfo>()
    private val busyRetries = HashMap<String, Int>()
    private val main = Handler(Looper.getMainLooper())
    private var resolving = false
    private var generation = 0

    private var discoveryListener: NsdManager.DiscoveryListener? = null
    private var onChange: ((List<DiscoveredHost>) -> Unit)? = null
    private var onStatus: ((DiscoveryStatus) -> Unit)? = null

    fun start(
        onChange: (List<DiscoveredHost>) -> Unit,
        onStatus: (DiscoveryStatus) -> Unit = {},
    ) {
        stop()
        val gen: Int
        synchronized(lock) {
            gen = ++generation
            this.onChange = onChange
            this.onStatus = onStatus
            found.clear()
            busyRetries.clear()
        }
        // No empty emit here: the reconnect loop dials the hosts the previous run
        // found while this one is still resolving.
        onStatus(DiscoveryStatus.Searching)

        val listener = object : NsdManager.DiscoveryListener {
            override fun onStartDiscoveryFailed(t: String?, e: Int) = status(gen, DiscoveryStatus.Failed)
            override fun onStopDiscoveryFailed(t: String?, e: Int) {}
            override fun onDiscoveryStarted(t: String?) {}
            override fun onDiscoveryStopped(t: String?) {}

            override fun onServiceFound(info: NsdServiceInfo) {
                synchronized(lock) {
                    if (gen != generation) return
                    resolveQueue.add(info)
                }
                pump(gen)
            }

            override fun onServiceLost(info: NsdServiceInfo) {
                val changed = synchronized(lock) {
                    gen == generation && found.remove(info.serviceName) != null
                }
                if (changed) emit(gen)
            }
        }
        discoveryListener = listener
        try {
            nsd.discoverServices(Protocol.SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
        } catch (e: Exception) {
            // Discovery unavailable; QR still works. Say so, rather than letting
            // "searching" and "nothing there" look the same.
            status(gen, DiscoveryStatus.Failed)
        }
    }

    fun stop() {
        discoveryListener?.let {
            try {
                nsd.stopServiceDiscovery(it)
            } catch (_: Exception) {
            }
        }
        discoveryListener = null
        val cb = synchronized(lock) {
            generation++
            resolveQueue.clear()
            resolving = false
            onChange = null
            onStatus.also { onStatus = null }
        }
        cb?.invoke(DiscoveryStatus.Idle)
    }

    // Both deliver under the lock: checked outside it, a stop() or restart could land
    // between the generation check and the call, and an old run's hosts or status
    // would overwrite the new run's. The callbacks only write UI state.
    private fun status(gen: Int, s: DiscoveryStatus) {
        synchronized(lock) { if (gen == generation) onStatus?.invoke(s) }
    }

    private fun pump(gen: Int) {
        val next = synchronized(lock) {
            if (gen != generation || resolving) return
            val n = resolveQueue.poll() ?: return
            resolving = true
            n
        }
        try {
            nsd.resolveService(next, object : NsdManager.ResolveListener {
                override fun onResolveFailed(info: NsdServiceInfo, errorCode: Int) {
                    if (errorCode != NsdManager.FAILURE_ALREADY_ACTIVE) return done(gen)
                    val retry = synchronized(lock) {
                        val n = busyRetries[next.serviceName] ?: 0
                        (gen == generation && n < BUSY_RETRIES).also {
                            if (it) busyRetries[next.serviceName] = n + 1
                        }
                    }
                    if (!retry) return done(gen)
                    main.postDelayed({
                        synchronized(lock) { if (gen == generation) resolveQueue.addFirst(next) }
                        done(gen)
                    }, BUSY_RETRY_MS)
                }

                override fun onServiceResolved(info: NsdServiceInfo) {
                    val host = info.host?.hostAddress
                    val added = host != null && synchronized(lock) {
                        if (gen != generation) return@synchronized false
                        found[info.serviceName] = DiscoveredHost(
                            name = info.serviceName ?: host,
                            ip = host,
                            port = info.port,
                        )
                        true
                    }
                    if (added) emit(gen)
                    done(gen)
                }
            })
        } catch (e: Exception) {
            done(gen)
        }
    }

    /** A resolve finished: free the slot (only if it is still this run's) and go on. */
    private fun done(gen: Int) {
        synchronized(lock) {
            if (gen != generation) return
            resolving = false
        }
        pump(gen)
    }

    private fun emit(gen: Int) {
        synchronized(lock) {
            if (gen == generation) onChange?.invoke(found.values.toList())
        }
    }

    private companion object {
        const val BUSY_RETRIES = 5
        const val BUSY_RETRY_MS = 300L
    }
}
