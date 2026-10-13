package org.activitytracker.installationkey

import java.security.SecureRandom
import java.security.KeyStoreException
import javax.crypto.SecretKey
import javax.crypto.spec.SecretKeySpec

/** JVM-only test substitute, never an Android Keystore fallback. */
class SyntheticInstallationKeyStore(
    private val keyBytes: () -> ByteArray = { ByteArray(32).also { SecureRandom().nextBytes(it) } },
    private val ivBytes: () -> ByteArray = { ByteArray(12).also { SecureRandom().nextBytes(it) } },
) : InstallationKeyStore {
    private val keys = mutableMapOf<String, SecretKey?>()
    private var unavailable = false

    /** Simulates a transient provider outage without changing any key. */
    fun setUnavailable(value: Boolean) { unavailable = value }

    private fun requireAvailable() {
        if (unavailable) throw KeyStoreException()
    }

    override fun create(alias: String): KeyResult<KeyCreation> = keyOperation {
        requireAvailable()
        if (keys.containsKey(alias)) return@keyOperation KeyResult.Failure(KeyFailure.KEY_EXISTS)
        val bytes = keyBytes()
        require(bytes.size == 32)
        keys[alias] = SecretKeySpec(bytes, "AES")
        KeyResult.Success(KeyCreation(strongBox = false))
    }

    override fun keyStatus(alias: String): KeyStatus = when {
        unavailable -> KeyStatus.UNAVAILABLE
        !keys.containsKey(alias) -> KeyStatus.MISSING
        keys[alias] == null -> KeyStatus.UNUSABLE
        else -> KeyStatus.PRESENT
    }

    fun invalidate(alias: String) {
        require(keys.containsKey(alias))
        keys[alias] = null
    }

    private fun withKey(alias: String, operation: (SecretKey) -> ByteArray): KeyResult<ByteArray> = keyOperation {
        requireAvailable()
        if (!keys.containsKey(alias)) return@keyOperation KeyResult.Failure(KeyFailure.MISSING_KEY)
        val key = keys[alias] ?: return@keyOperation KeyResult.Failure(KeyFailure.UNUSABLE_KEY)
        KeyResult.Success(operation(key))
    }

    override fun seal(alias: String, plaintext: ByteArray, aad: ByteArray): KeyResult<ByteArray> = withKey(alias) {
        val iv = ivBytes()
        require(iv.size == 12)
        SealedBoxV1.seal(it, plaintext, aad, iv)
    }

    override fun open(alias: String, sealed: ByteArray, aad: ByteArray): KeyResult<ByteArray> {
        SealedBoxV1.formatFailure(sealed)?.let { return KeyResult.Failure(it) }
        return withKey(alias) { SealedBoxV1.open(it, sealed, aad) }
    }

    override fun delete(alias: String): KeyResult<Unit> = keyOperation {
        requireAvailable()
        if (!keys.containsKey(alias)) return@keyOperation KeyResult.Failure(KeyFailure.MISSING_KEY)
        keys.remove(alias)
        KeyResult.Success(Unit)
    }
}
