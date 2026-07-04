package com.example.lanremote.data

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/** A saved laptop the user can reconnect to in one tap.
 *  [key] is the base64url 256-bit secret from the QR — present ⇒ encrypted wire.
 *  Empty ⇒ legacy plaintext (manual-code pairing on a trusted network).
 *  [rendezvous] is the "host:port" of the coordinator for off-LAN access — set
 *  from the QR's &r= param. Present (with a key) ⇒ we can reach this laptop when
 *  it isn't on the local network. */
data class Device(
    val id: String,
    val name: String,
    val ip: String,
    val port: Int,
    val token: String,
    val key: String = "",
    val rendezvous: String = "",
)

/** A laptop found live on the network via mDNS (no token yet). */
data class DiscoveredHost(
    val name: String,
    val ip: String,
    val port: Int,
)

/** Simple JSON-in-SharedPreferences persistence for saved devices. */
class DeviceStore(context: Context) {

    private val prefs = context.getSharedPreferences("lazer_devices", Context.MODE_PRIVATE)

    fun load(): List<Device> {
        val raw = prefs.getString(KEY, null) ?: return emptyList()
        return try {
            val arr = JSONArray(raw)
            (0 until arr.length()).map { i ->
                val o = arr.getJSONObject(i)
                Device(
                    id = o.getString("id"),
                    name = o.getString("name"),
                    ip = o.getString("ip"),
                    port = o.optInt("port", 50505),
                    token = o.getString("token"),
                    key = o.optString("key", ""),
                    rendezvous = o.optString("rendezvous", ""),
                )
            }
        } catch (e: Exception) {
            emptyList()
        }
    }

    fun save(devices: List<Device>) {
        val arr = JSONArray()
        devices.forEach { d ->
            arr.put(
                JSONObject()
                    .put("id", d.id)
                    .put("name", d.name)
                    .put("ip", d.ip)
                    .put("port", d.port)
                    .put("token", d.token)
                    .put("key", d.key)
                    .put("rendezvous", d.rendezvous)
            )
        }
        prefs.edit().putString(KEY, arr.toString()).apply()
    }

    /** Insert or replace, keyed by the stable [Device.id] (falling back to ip:port
     *  for legacy records saved before ids were stable). Matching by id — not the
     *  live address — lets a saved laptop's IP be refreshed in place when DHCP moves
     *  it, instead of leaving a stale duplicate. Returns the new list. */
    fun upsert(device: Device): List<Device> {
        val list = load().toMutableList()
        val idx = list.indexOfFirst {
            it.id == device.id || (it.ip == device.ip && it.port == device.port)
        }
        if (idx >= 0) list[idx] = device else list.add(device)
        save(list)
        return list
    }

    fun delete(id: String): List<Device> {
        val list = load().filterNot { it.id == id }
        save(list)
        return list
    }

    private companion object {
        const val KEY = "devices_json"
    }
}
