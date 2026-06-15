package com.example.lanremote.data

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Discovers LazeR laptops on the LAN via mDNS/NSD (service type "_lazer._udp.").
 * Resolves are serialized because NsdManager allows only one at a time.
 */
class Discovery(context: Context) {

    private val nsd = context.getSystemService(Context.NSD_SERVICE) as NsdManager
    private val found = LinkedHashMap<String, DiscoveredHost>()
    private val resolveQueue = ConcurrentLinkedQueue<NsdServiceInfo>()
    private val resolving = AtomicBoolean(false)

    private var discoveryListener: NsdManager.DiscoveryListener? = null
    private var onChange: ((List<DiscoveredHost>) -> Unit)? = null

    fun start(onChange: (List<DiscoveredHost>) -> Unit) {
        stop()
        this.onChange = onChange
        found.clear()

        val listener = object : NsdManager.DiscoveryListener {
            override fun onStartDiscoveryFailed(t: String?, e: Int) {}
            override fun onStopDiscoveryFailed(t: String?, e: Int) {}
            override fun onDiscoveryStarted(t: String?) {}
            override fun onDiscoveryStopped(t: String?) {}

            override fun onServiceFound(info: NsdServiceInfo) {
                resolveQueue.add(info)
                pump()
            }

            override fun onServiceLost(info: NsdServiceInfo) {
                if (found.remove(info.serviceName) != null) emit()
            }
        }
        discoveryListener = listener
        try {
            nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
        } catch (e: Exception) {
            // discovery unavailable; QR/manual still work
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
        resolveQueue.clear()
        resolving.set(false)
        onChange = null
    }

    private fun pump() {
        if (!resolving.compareAndSet(false, true)) return
        val next = resolveQueue.poll()
        if (next == null) {
            resolving.set(false)
            return
        }
        nsd.resolveService(next, object : NsdManager.ResolveListener {
            override fun onResolveFailed(info: NsdServiceInfo, errorCode: Int) {
                resolving.set(false)
                pump()
            }

            override fun onServiceResolved(info: NsdServiceInfo) {
                val host = info.host?.hostAddress
                if (host != null) {
                    found[info.serviceName] = DiscoveredHost(
                        name = info.serviceName ?: host,
                        ip = host,
                        port = info.port,
                    )
                    emit()
                }
                resolving.set(false)
                pump()
            }
        })
    }

    private fun emit() {
        onChange?.invoke(found.values.toList())
    }

    private companion object {
        const val SERVICE_TYPE = "_lazer._udp."
    }
}
