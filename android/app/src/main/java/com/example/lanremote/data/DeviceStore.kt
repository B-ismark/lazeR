package com.example.lanremote.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import com.example.lanremote.net.Protocol
import org.json.JSONArray
import org.json.JSONObject

/** A saved laptop the user can reconnect to in one tap.
 *  [key] is the base64url 256-bit secret from the QR — present ⇒ encrypted wire.
 *  Empty ⇒ legacy plaintext (manual-code pairing on a trusted network).
 *  [bound] = this laptop has answered a bound handshake (it echoes the phone's
 *  nonce), so an unbound answer from it is refused as a downgrade or replay. */
data class Device(
    val id: String,
    val name: String,
    val ip: String,
    val port: Int,
    val token: String,
    val key: String = "",
    val bound: Boolean = false,
)

/** A laptop found live on the network via mDNS (no token yet). */
data class DiscoveredHost(
    val name: String,
    val ip: String,
    val port: Int,
)

private fun sameKey(a: Device, b: Device) = b.key.isNotBlank() && a.key == b.key
private fun sameAddress(a: Device, b: Device) = a.ip == b.ip && a.port == b.port
private fun keysConflict(a: Device, b: Device) =
    a.key.isNotBlank() && b.key.isNotBlank() && a.key != b.key

/** The saved record [device] belongs to, if any: an exact key first, then the id,
 *  then the address — including an address whose key differs, which is the case
 *  the caller must ask about before replacing that pairing. */
fun savedMatch(list: List<Device>, device: Device): Device? =
    list.firstOrNull { sameKey(it, device) }
        ?: list.firstOrNull { it.id == device.id }
        ?: list.firstOrNull { sameAddress(it, device) }

/**
 * [list] with [device] inserted or replacing the record it matches.
 *
 * A record matches on its key (for QR pairings), or on its stable id or its
 * address when the keys don't say otherwise: a laptop whose IP changed and was
 * scanned again is the same laptop. Every match is folded into one record — the
 * key match, else the id match, else the first — so older duplicates are cleaned
 * up the next time that laptop is saved.
 *
 * A saved key is never replaced by a blank one: saving a typed-code connection
 * to a laptop first paired by QR would otherwise quietly downgrade it to the
 * plaintext wire. [Device.bound] survives while the key is unchanged, since the
 * same laptop doesn't un-learn the bound handshake — unless [rescanned]: a QR scan
 * is the user at the laptop's screen, so the handshake it just made is the truth,
 * and a laptop rolled back to an older build stops being refused as a downgrade.
 */
fun mergeDevice(list: List<Device>, device: Device, rescanned: Boolean = false): List<Device> {
    // Two records with different keys are different laptops, whatever their address
    // says: DHCP hands a sleeping laptop's address to the next one, and folding the
    // two deleted the other laptop's pairing. A shared id with a different key is
    // the one exception — the user confirmed replacing that record's pairing — and
    // only when no record already holds this key (ids are the first "ip:port" a
    // laptop was seen at, so a fresh one can collide with a stale record's).
    val keyHolder = list.any { sameKey(it, device) }
    fun matches(d: Device) = sameKey(d, device) ||
        (!keysConflict(d, device) && (d.id == device.id || sameAddress(d, device))) ||
        (d.id == device.id && !keyHolder)
    val hits = list.filter(::matches)
    if (hits.isEmpty()) return list + device
    val old = hits.firstOrNull { sameKey(it, device) }
        ?: hits.firstOrNull { it.id == device.id }
        ?: hits.first()
    val key = device.key.ifBlank { old.key }
    val merged = device.copy(
        id = old.id,
        key = key,
        bound = device.bound || (!rescanned && old.bound && old.key == key),
    )
    val out = ArrayList<Device>(list.size)
    for (d in list) {
        if (d === old) out.add(merged) else if (!matches(d)) out.add(d)
    }
    return out
}

/**
 * Saved devices, encrypted at rest under an Android Keystore key.
 *
 * A record holds the token AND the 256-bit pairing key — between them, everything
 * needed to drive the laptop. They used to sit in
 * `/data/data/<pkg>/shared_prefs/lazer_devices.xml` as readable JSON. App sandboxing
 * keeps other apps out, but that file is plainly readable on a rooted device, through
 * a recovery-mode dump, or by anyone with physical access and an unlocked bootloader
 * — so the one secret the whole protocol rests on was the easiest thing to take.
 *
 * The blob is now AES-256-GCM sealed with a key generated in the Keystore, which is
 * hardware-backed where the device supports it and never leaves it. The key is
 * deliberately NOT `setUserAuthenticationRequired`: this is read at launch to
 * auto-reconnect, so a biometric prompt on startup would be wrong, and auth-bound
 * keys are the ones invalidated by lock-screen changes.
 *
 * The manifest sets `allowBackup=false`, so there is no restore-onto-a-different-
 * device path that could hand us an undecryptable blob.
 */
class DeviceStore(context: Context) {

    private val prefs = context.getSharedPreferences("lazer_devices", Context.MODE_PRIVATE)

    /**
     * Read the saved devices.
     *
     * Runs on the main thread (RemoteViewModel builds its initial UiState from it),
     * same as the SharedPreferences read it replaces. The one slow case is the very
     * first call after upgrading, which generates the Keystore key and re-seals the
     * legacy blob — tens of milliseconds on a normal device, once, and kept
     * synchronous on purpose: making it async would mean the connection screen paints
     * an empty "Saved devices" list for a frame before the real one arrives.
     */
    fun load(): List<Device> {
        // Preferred path: the sealed blob.
        prefs.getString(KEY_ENC, null)?.let { blob ->
            SecretBox.open(blob)?.let { return parse(it) }
            // Sealed but unreadable. Nothing can recover it (that is the point of
            // encrypting), so report none rather than crashing — re-pairing is one QR
            // scan. The blob is left in place, so a purely TRANSIENT Keystore fault
            // resolves itself on the next launch. Note the limit of that: this session
            // now believes there are no saved devices, so if the user pairs or deletes
            // one before relaunching, save() overwrites the old blob and the records
            // are gone for good. Not worth guarding against — refusing to save would
            // trade a rare unreadable-blob case for a visible "nothing sticks" bug.
            return emptyList()
        }
        // Legacy plaintext written before this change. Read it, then migrate.
        val legacy = prefs.getString(KEY_PLAIN, null) ?: return emptyList()
        val devices = parse(legacy)
        migrate(legacy)
        return devices
    }

    /** Re-seal a legacy plaintext blob, and only then drop the plaintext.
     *  Ordered that way on purpose: if sealing fails we keep the old value, so an
     *  upgrade can never cost the user their saved devices. */
    private fun migrate(legacy: String) {
        val sealed = SecretBox.seal(legacy) ?: return
        prefs.edit().putString(KEY_ENC, sealed).remove(KEY_PLAIN).apply()
    }

    fun save(devices: List<Device>) {
        val json = JSONArray().apply {
            devices.forEach { d ->
                put(
                    JSONObject()
                        .put("id", d.id)
                        .put("name", d.name)
                        .put("ip", d.ip)
                        .put("port", d.port)
                        .put("token", d.token)
                        .put("key", d.key)
                        .put("bound", d.bound)
                )
            }
        }.toString()

        val sealed = SecretBox.seal(json)
        prefs.edit().apply {
            if (sealed != null) {
                putString(KEY_ENC, sealed)
                remove(KEY_PLAIN)
            } else {
                // Keystore unavailable — essentially unreachable on a healthy Android
                // 7+ device. Fall back to plaintext rather than silently refusing to
                // remember the laptop: that is exactly the behaviour every previous
                // release had, so it is no worse than the status quo, whereas failing
                // closed would be a NEW breakage (tapping a saved device would just
                // never stick, with nothing to explain why).
                putString(KEY_PLAIN, json)
                remove(KEY_ENC)
            }
        }.apply()
    }

    private fun parse(raw: String): List<Device> = try {
        val arr = JSONArray(raw)
        (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Device(
                id = o.getString("id"),
                name = o.getString("name"),
                ip = o.getString("ip"),
                port = o.optInt("port", Protocol.DEFAULT_PORT),
                token = o.getString("token"),
                // A "rendezvous" key may be present in records written before off-LAN
                // access was removed; it is simply ignored, and dropped on next save.
                key = o.optString("key", ""),
                bound = o.optBoolean("bound", false),
            )
        }
    } catch (e: Exception) {
        emptyList()
    }

    /** Insert or replace; see [mergeDevice]. Returns the new list. */
    fun upsert(device: Device, rescanned: Boolean = false): List<Device> {
        val list = mergeDevice(load(), device, rescanned)
        save(list)
        return list
    }

    fun delete(id: String): List<Device> {
        val list = load().filterNot { it.id == id }
        save(list)
        return list
    }

    private companion object {
        const val KEY_PLAIN = "devices_json"   // legacy, migrated away on first load
        const val KEY_ENC = "devices_enc"      // base64( iv(12) | ciphertext+tag )
    }
}

/**
 * AES-256-GCM seal/open under a Keystore-held key. Every call is best-effort and
 * returns null on failure — callers decide what that means, because "can't decrypt"
 * and "can't encrypt" want different handling.
 */
private object SecretBox {

    private const val PROVIDER = "AndroidKeyStore"
    private const val ALIAS = "lazer_devices_v1"
    private const val TRANSFORM = "AES/GCM/NoPadding"
    private const val IV_BYTES = 12
    private const val TAG_BITS = 128

    fun seal(plain: String): String? = try {
        val cipher = Cipher.getInstance(TRANSFORM)
        // Do NOT pass a GCMParameterSpec here: AndroidKeyStore requires that IT
        // generate the IV for encryption and throws if one is supplied. Read it back
        // off the cipher and store it alongside the ciphertext.
        cipher.init(Cipher.ENCRYPT_MODE, secretKey())
        val iv = cipher.iv
        // open() reads the IV back as a fixed 12 bytes, so a provider that chose a
        // different GCM IV length would produce a blob nothing could ever decrypt.
        // AndroidKeyStore always uses 12, but fail here rather than persist that.
        if (iv == null || iv.size != IV_BYTES) throw IllegalStateException("unexpected IV size")
        val ct = cipher.doFinal(plain.toByteArray(Charsets.UTF_8))
        Base64.encodeToString(iv + ct, Base64.NO_WRAP)
    } catch (e: Exception) {
        null
    }

    // Block body, not an expression body: the length guard below needs an early
    // `return`, which Kotlin forbids inside `= try { ... }`.
    fun open(blob: String): String? {
        return try {
            val raw = Base64.decode(blob, Base64.NO_WRAP)
            if (raw.size <= IV_BYTES) return null
            val cipher = Cipher.getInstance(TRANSFORM)
            cipher.init(
                Cipher.DECRYPT_MODE, secretKey(),
                GCMParameterSpec(TAG_BITS, raw, 0, IV_BYTES),
            )
            String(cipher.doFinal(raw, IV_BYTES, raw.size - IV_BYTES), Charsets.UTF_8)
        } catch (e: Exception) {
            null
        }
    }

    /** The existing Keystore key, or a freshly generated one. */
    private fun secretKey(): SecretKey {
        val ks = KeyStore.getInstance(PROVIDER).apply { load(null) }
        (ks.getEntry(ALIAS, null) as? KeyStore.SecretKeyEntry)?.secretKey?.let { return it }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, PROVIDER)
        gen.init(
            KeyGenParameterSpec.Builder(
                ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build()
        )
        return gen.generateKey()
    }
}
